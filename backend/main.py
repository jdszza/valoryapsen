"""
APSEN - Backend FastAPI
MQTT Bridge + Auth JWT + OS + Manutenção + WebSocket
"""
import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt
from fastapi import Body, Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from auth import (criar_token, decodificar_token, tem_permissao, verificar_senha)
from config import settings
from erp_integration import processar_novo_lote_bg
from database import (
    atualizar_os, atualizar_usuario, criar_os, criar_usuario,
    get_alarmes, get_historico, get_log_manutencao, get_lote_atual,
    get_ocorrencias, get_os, get_usuario, init_db, listar_os,
    listar_usuarios, registrar_manutencao, registrar_ocorrencia,
    salvar_contagem, salvar_evento,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Estado em memória ──────────────────────────────────────────────────────────
estado = {
    "contagem": 0, "meta": 1000, "lote_id": "LOTE-001",
    "status": "idle", "velocidade": 0, "alarme": None,
    "ultima_atualizacao": None,
}
_estado_lock = threading.Lock()  # protege acesso ao dict `estado` entre threads
_loop: Optional[asyncio.AbstractEventLoop] = None  # referência ao event loop asyncio

# ── WebSocket Manager ──────────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        msg = json.dumps(data, default=str)
        for ws in list(self.active):
            try:
                await ws.send_text(msg)
            except Exception:
                self.active.remove(ws)

manager = ConnectionManager()
broadcast_queue: asyncio.Queue = asyncio.Queue()

# ── MQTT ───────────────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe("apsen/#")
        logger.info("MQTT conectado")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        topic = msg.topic

        with _estado_lock:
            if topic == "apsen/contagem":
                estado["contagem"]   = payload.get("valor", estado["contagem"])
                estado["velocidade"] = payload.get("velocidade", 0)
                estado["ultima_atualizacao"] = datetime.now(timezone.utc).isoformat()
                salvar_contagem(estado["lote_id"], estado["contagem"], estado["velocidade"])

            elif topic == "apsen/status":
                estado["status"] = payload.get("status", estado["status"])
                estado["alarme"] = payload.get("alarme")
                if payload.get("evento"):
                    salvar_evento(estado["lote_id"], payload["evento"],
                                  payload.get("detalhe", ""))

            elif topic == "apsen/lote":
                novo_lote_id = payload.get("lote_id", estado["lote_id"])
                novo_meta    = payload.get("meta",    estado["meta"])
                novo_produto = payload.get("produto", "Produto APSEN")
                estado["lote_id"]  = novo_lote_id
                estado["meta"]     = novo_meta
                estado["contagem"] = 0
                # Gerar OS automaticamente fora do lock (operação de DB)
                _novo_lote = (novo_lote_id, novo_produto, novo_meta)
            else:
                _novo_lote = None

            snapshot = dict(estado)

        # Criar OS e notificar ERP em background (fora do _estado_lock)
        if topic == "apsen/lote" and _novo_lote:
            processar_novo_lote_bg(*_novo_lote, criado_por="sistema/mqtt")

        # call_soon_threadsafe é o único modo thread-safe de enfileirar
        # tarefas num asyncio.Queue a partir de uma thread não-asyncio (como a do MQTT)
        if _loop is not None:
            _loop.call_soon_threadsafe(broadcast_queue.put_nowait,
                                       {"tipo": "estado", **snapshot})
    except Exception as e:
        logger.error(f"MQTT msg error: {e}")

mqtt_client = mqtt.Client(client_id="apsen-backend")
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

async def broadcast_worker():
    while True:
        data = await broadcast_queue.get()
        await manager.broadcast(data)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_event_loop()  # salva ref antes de lançar a thread MQTT
    init_db()
    mqtt_client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()
    task = asyncio.create_task(broadcast_worker())
    logger.info("Backend APSEN iniciado")
    yield
    task.cancel()
    mqtt_client.loop_stop()
    mqtt_client.disconnect()

app = FastAPI(title="APSEN Backend", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── Auth helpers ───────────────────────────────────────────────────────────────
bearer = HTTPBearer(auto_error=False)

def get_current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token ausente")
    payload = decodificar_token(creds.credentials)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido ou expirado")
    return payload

def require_role(role_minimo: str):
    def _dep(user=Depends(get_current_user)):
        if not tem_permissao(user["role"], role_minimo):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Permissão insuficiente")
        return user
    return _dep

# ── Modelos ────────────────────────────────────────────────────────────────────
class LoginReq(BaseModel):
    username: str
    senha: str

class UsuarioReq(BaseModel):
    username: str
    senha: str
    role: str
    nome_completo: str

class OSReq(BaseModel):
    produto: str
    lote_id: str
    meta: int
    responsavel: str

class OSUpdateReq(BaseModel):
    status: Optional[str] = None
    responsavel: Optional[str] = None

class OcorrenciaReq(BaseModel):
    tipo: str
    descricao: str
    contagem: Optional[int] = None

class ManutencaoReq(BaseModel):
    tipo: str
    descricao: str
    componente: Optional[str] = ""

# ═══════════════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════════════
@app.post("/auth/login")
def login(req: LoginReq):
    user = get_usuario(req.username)
    if not user or not verificar_senha(req.senha, user["senha_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciais inválidas")
    token = criar_token(user["username"], user["role"], user["nome_completo"])
    return {"token": token, "role": user["role"],
            "nome": user["nome_completo"], "username": user["username"]}

@app.get("/auth/me")
def me(user=Depends(get_current_user)):
    return {"username": user["sub"], "role": user["role"], "nome": user["nome"]}

@app.get("/usuarios")
def get_usuarios(user=Depends(require_role("admin"))):
    return listar_usuarios()

@app.post("/usuarios")
def novo_usuario(req: UsuarioReq, user=Depends(require_role("admin"))):
    return criar_usuario(req.username, req.senha, req.role, req.nome_completo)

@app.put("/usuarios/{username}")
def editar_usuario(username: str, campos: dict = Body(...), user=Depends(require_role("admin"))):
    return atualizar_usuario(username, campos)

# ═══════════════════════════════════════════════════════════════════════════════
# MÁQUINA / MQTT
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/estado")
def get_estado(user=Depends(get_current_user)):
    with _estado_lock:
        return dict(estado)

@app.get("/historico")
def historico(lote_id: str = None, limite: int = 200, user=Depends(get_current_user)):
    return get_historico(lote_id=lote_id, limite=limite)

@app.get("/lote")
def lote_atual(user=Depends(get_current_user)):
    return get_lote_atual(estado["lote_id"])

@app.post("/cmd/lote")
def novo_lote(
    lote_id: str,
    meta: int,
    produto: str = "Produto APSEN",
    user=Depends(require_role("operador")),
):
    mqtt_client.publish(
        "apsen/cmd/lote",
        json.dumps({"lote_id": lote_id, "meta": meta, "produto": produto}),
    )
    with _estado_lock:
        estado.update({"lote_id": lote_id, "meta": meta, "contagem": 0})
    # Gerar OS e notificar ERP em background (não bloqueia o response)
    processar_novo_lote_bg(lote_id, produto, meta, criado_por=user["sub"])
    return {"ok": True, "lote_id": lote_id, "meta": meta, "produto": produto}

@app.post("/cmd/reset")
def reset(user=Depends(require_role("operador"))):
    mqtt_client.publish("apsen/cmd/reset", json.dumps({"reset": True}))
    estado["contagem"] = 0
    return {"ok": True}

@app.post("/cmd/status")
def set_status(cmd: str, user=Depends(require_role("operador"))):
    if cmd not in {"start", "pause", "stop"}:
        raise HTTPException(400, "Comando inválido")
    mqtt_client.publish("apsen/cmd/status", json.dumps({"cmd": cmd}))
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════════════════════
# ORDENS DE SERVIÇO
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/ordens")
def get_ordens(status_os: str = None, user=Depends(get_current_user)):
    return listar_os(status=status_os)

@app.get("/ordens/{os_id}")
def get_ordem(os_id: str, user=Depends(get_current_user)):
    os = get_os(os_id)
    if not os:
        raise HTTPException(404, "OS não encontrada")
    return os

@app.post("/ordens")
def nova_ordem(req: OSReq, user=Depends(require_role("operador"))):
    return criar_os(req.produto, req.lote_id, req.meta, req.responsavel, user["sub"])

@app.put("/ordens/{os_id}")
def atualizar_ordem(os_id: str, req: OSUpdateReq, user=Depends(require_role("operador"))):
    campos = {k: v for k, v in req.model_dump().items() if v is not None}
    return atualizar_os(os_id, campos, user["sub"])

@app.get("/ordens/{os_id}/ocorrencias")
def get_log_os(os_id: str, user=Depends(get_current_user)):
    return get_ocorrencias(os_id)

@app.post("/ordens/{os_id}/ocorrencias")
def add_ocorrencia(os_id: str, req: OcorrenciaReq, user=Depends(require_role("operador"))):
    registrar_ocorrencia(os_id, req.tipo, req.descricao, user["sub"], req.contagem)
    return {"ok": True}

# ═══════════════════════════════════════════════════════════════════════════════
# MANUTENÇÃO
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/manutencao/alarmes")
def get_alarmes_route(limite: int = 100, user=Depends(require_role("manutencao"))):
    return get_alarmes(limite)

@app.get("/manutencao/log")
def get_log(limite: int = 100, user=Depends(require_role("manutencao"))):
    return get_log_manutencao(limite)

@app.post("/manutencao/log")
def add_log(req: ManutencaoReq, user=Depends(require_role("manutencao"))):
    return registrar_manutencao(req.tipo, req.descricao, user["sub"], req.componente or "")

@app.post("/manutencao/cmd")
def cmd_manutencao(cmd: str, componente: str = "", user=Depends(require_role("manutencao"))):
    mqtt_client.publish("apsen/manutencao/cmd",
                        json.dumps({"cmd": cmd, "componente": componente, "usuario": user["sub"]}))
    registrar_manutencao("comando", f"CMD '{cmd}' em '{componente}'", user["sub"], componente)
    return {"ok": True}

@app.get("/manutencao/diagnostico")
def diagnostico(user=Depends(require_role("manutencao"))):
    return {
        "estado_maquina": estado,
        "mqtt_conectado": mqtt_client.is_connected(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    token = ws.query_params.get("token")
    if not token or not decodificar_token(token):
        # WebSocket deve ser aceito antes de poder ser fechado
        await ws.accept()
        await ws.close(code=4001)
        return
    await manager.connect(ws)
    with _estado_lock:
        snapshot = dict(estado)
    await ws.send_text(json.dumps({"tipo": "estado", **snapshot}, default=str))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
