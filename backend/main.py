"""
APSEN - Backend FastAPI v2.0
Bridge MQTT → MySQL + API REST para Dashboard (read-only) e IHM (manutenção)

Tópicos MQTT consumidos:
  apsen/os/nova                 ← SAP publica nova Ordem de Saída
  apsen/ia/atribuicao           ← IA atribui medicamentos aos dispensers
  apsen/dispenser/carregamento  ← CV monitora carregamento dos dispensers
  apsen/dispenser/pronto        ← Dispenser carregado e pronto
  apsen/dispenser/evento        ← Cada remédio dispensado
  apsen/dispenser/status        ← Status geral dos dispensers
  apsen/cnc/status              ← Status e posição da CNC
  apsen/manut/temperatura       ← Leituras de temperatura (sensores)
  apsen/manut/uso               ← Leituras de desgaste/horas de uso

O backend NÃO controla nenhum sistema. Apenas armazena e serve dados.
"""
import asyncio
import collections
import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from auth import criar_token, decodificar_token, verificar_senha
from config import settings
from database import (
    atualizar_item_os, atualizar_status_ordem,
    get_alarmes, get_cnc_recentes, get_dispensas, get_dispensas_recentes,
    get_historico_ordens, get_historico_sensor, get_log_manutencao,
    get_ordem_ativa, get_ultimas_leituras, get_usuario,
    init_db, resolver_alarme, salvar_alarme, salvar_cnc_evento,
    salvar_dispensa, salvar_leitura_sensor, salvar_manutencao, salvar_ordem,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [BACKEND] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Estado em memória (snapshot do sistema) ────────────────────────────────────
_estado = {
    # CNC
    "cnc": {
        "status": "idle",
        "os_id": None,
        "dispenser_alvo": None,
        "posicao_x": 0.0,
        "posicao_y": 0.0,
        "ciclo_atual": 0,
        "total_ciclos": 0,
    },
    # OS ativa
    "os_ativa": None,       # dict com os_id, descricao, itens[], status
    # Dispensers (1-6)
    "dispensers": {
        str(i): {
            "status": "idle",
            "medicamento": None,
            "quantidade_dispensada": 0,
            "quantidade_alvo": 0,
        }
        for i in range(1, 7)
    },
    # Atribuições da IA para a OS atual
    "atribuicao_ia": [],
    # Alarmes ativos (contagem)
    "alarmes_ativos": 0,
}
_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None

# Buffer em memória de eventos recentes (para dashboard em tempo real)
_log_eventos: collections.deque = collections.deque(maxlen=100)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(tipo: str, msg: str, dados: dict = None):
    _log_eventos.appendleft({"tipo": tipo, "msg": msg, "dados": dados or {}, "ts": _ts()})


# ── WebSocket Manager ──────────────────────────────────────────────────────────
class WSManager:
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
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


_ws_manager = WSManager()
_broadcast_q: asyncio.Queue = asyncio.Queue()


async def _broadcast_worker():
    while True:
        data = await _broadcast_q.get()
        await _ws_manager.broadcast(data)


def _enfileirar_broadcast(data: dict):
    if _loop:
        _loop.call_soon_threadsafe(_broadcast_q.put_nowait, data)


# ── Handlers MQTT ──────────────────────────────────────────────────────────────

def _handle_os_nova(payload: dict):
    """SAP publica nova Ordem de Saída."""
    os_id       = payload.get("os_id")
    descricao   = payload.get("descricao", "")
    medicamentos = payload.get("medicamentos", [])

    if not os_id or not medicamentos:
        logger.warning("[MQTT] OS inválida recebida — ignorando.")
        return

    salvar_ordem(os_id, descricao, medicamentos, payload)
    with _lock:
        _estado["os_ativa"] = {
            "os_id": os_id,
            "descricao": descricao,
            "status": "aguardando",
            "itens": medicamentos,
        }
    _log("os_nova", f"OS {os_id} recebida da SAP — {len(medicamentos)} medicamento(s).")
    logger.info(f"[OS] {os_id} registrada: {descricao}")


def _handle_ia_atribuicao(payload: dict):
    """IA publicou a atribuição de dispensers para a OS."""
    os_id = payload.get("os_id")
    atribuicoes = payload.get("atribuicoes", [])
    with _lock:
        _estado["atribuicao_ia"] = atribuicoes
        if _estado["os_ativa"] and _estado["os_ativa"].get("os_id") == os_id:
            _estado["os_ativa"]["status"] = "atribuicao_recebida"
    _log("ia_atribuicao", f"IA atribuiu {len(atribuicoes)} dispenser(s) para OS {os_id}.", payload)
    logger.info(f"[IA] Atribuição para {os_id}: {atribuicoes}")


def _handle_dispenser_carregamento(payload: dict):
    """CV monitora carregamento do dispenser."""
    disp_id  = str(payload.get("dispenser_id", "?"))
    status   = payload.get("status", "")
    med      = payload.get("medicamento", "")
    os_id    = payload.get("os_id", "")
    falha    = payload.get("motivo_falha")

    with _lock:
        if disp_id in _estado["dispensers"]:
            _estado["dispensers"][disp_id]["medicamento"] = med
            _estado["dispensers"][disp_id]["status"] = f"carregando_{status}"

    if status == "erro":
        salvar_alarme(
            fonte=f"dispenser_{disp_id}",
            tipo="erro_carregamento",
            descricao=falha or f"Erro ao carregar medicamento no dispenser {disp_id}",
        )
        with _lock:
            _estado["alarmes_ativos"] += 1
        _log("alarme", f"ERRO carregamento dispenser {disp_id}: {falha}")
        logger.error(f"[DISP-{disp_id}] Erro de carregamento: {falha}")
    else:
        _log("carregamento", f"Dispenser {disp_id} carregando {med} — CV: {status}")
        logger.info(f"[DISP-{disp_id}] Carregamento {status}: {med}")


def _handle_dispenser_pronto(payload: dict):
    """Dispenser carregado e pronto para dispensar."""
    disp_id = str(payload.get("dispenser_id", "?"))
    med     = payload.get("medicamento", "")
    alvo    = payload.get("quantidade_alvo", 0)

    with _lock:
        if disp_id in _estado["dispensers"]:
            _estado["dispensers"][disp_id].update({
                "status": "pronto",
                "medicamento": med,
                "quantidade_alvo": alvo,
                "quantidade_dispensada": 0,
            })

    _log("dispenser_pronto", f"Dispenser {disp_id} pronto — {med} × {alvo}")
    logger.info(f"[DISP-{disp_id}] PRONTO para dispensar {med} × {alvo}")


def _handle_dispenser_evento(payload: dict):
    """Cada remédio dispensado para a caixa."""
    os_id    = payload.get("os_id", "")
    disp_id  = payload.get("dispenser_id")
    med      = payload.get("medicamento", "")
    qtd_disp = payload.get("quantidade_dispensada", 0)  # acumulado
    qtd_alvo = payload.get("quantidade_alvo", 0)
    validado = payload.get("validado", True)
    falha    = payload.get("motivo_falha")

    # Persiste no banco
    salvar_dispensa(os_id, disp_id, med, qtd_disp, qtd_alvo, validado, falha)

    disp_key = str(disp_id)
    with _lock:
        if disp_key in _estado["dispensers"]:
            _estado["dispensers"][disp_key]["quantidade_dispensada"] = qtd_disp
            if qtd_disp >= qtd_alvo:
                _estado["dispensers"][disp_key]["status"] = "concluido"

    if not validado:
        salvar_alarme(
            fonte=f"dispenser_{disp_id}",
            tipo="falha_validacao_ia",
            descricao=falha or f"Remédio rejeitado pela IA no dispenser {disp_id}",
        )
        with _lock:
            _estado["alarmes_ativos"] += 1
        _log("falha_ia", f"Dispenser {disp_id} falha IA: {falha}")
    else:
        _log("dispensa", f"Dispenser {disp_id} dispensou {qtd_disp}/{qtd_alvo} × {med}")

    # Atualiza item da OS
    if os_id:
        novo_status = "concluido" if qtd_disp >= qtd_alvo else "em_andamento"
        try:
            atualizar_item_os(os_id, disp_id, qtd_disp, novo_status)
        except Exception as exc:
            logger.warning(f"[DB] Erro ao atualizar item OS: {exc}")


def _handle_dispenser_status(payload: dict):
    """Status geral de um dispenser."""
    disp_id = str(payload.get("dispenser_id", "?"))
    status  = payload.get("status", "idle")
    with _lock:
        if disp_id in _estado["dispensers"]:
            _estado["dispensers"][disp_id]["status"] = status


def _handle_cnc_status(payload: dict):
    """Status e posição da CNC."""
    os_id   = payload.get("os_id")
    sts     = payload.get("status", "idle")
    disp_al = payload.get("dispenser_alvo")
    pos_x   = payload.get("posicao_x", 0.0)
    pos_y   = payload.get("posicao_y", 0.0)
    ciclo   = payload.get("ciclo_atual", 0)
    total   = payload.get("total_ciclos", 0)

    with _lock:
        _estado["cnc"].update({
            "status": sts,
            "os_id": os_id,
            "dispenser_alvo": disp_al,
            "posicao_x": pos_x,
            "posicao_y": pos_y,
            "ciclo_atual": ciclo,
            "total_ciclos": total,
        })
        # Atualiza status da OS ativa
        if _estado["os_ativa"] and os_id and _estado["os_ativa"].get("os_id") == os_id:
            if sts == "concluido":
                _estado["os_ativa"]["status"] = "concluida"
            elif sts not in ("idle",):
                _estado["os_ativa"]["status"] = "em_andamento"

    # Persiste no banco (somente mudanças relevantes)
    if sts in ("posicionado", "concluido", "erro", "movendo"):
        try:
            salvar_cnc_evento(os_id, sts, disp_al, pos_x, pos_y, ciclo, total)
        except Exception as exc:
            logger.warning(f"[DB] Erro ao salvar evento CNC: {exc}")

    if sts == "concluido" and os_id:
        try:
            atualizar_status_ordem(os_id, "concluida")
        except Exception as exc:
            logger.warning(f"[DB] Erro ao fechar OS {os_id}: {exc}")
        _log("cnc_concluido", f"CNC concluiu OS {os_id}")

    if sts == "erro":
        falha = payload.get("mensagem", "Erro desconhecido na CNC")
        try:
            salvar_alarme("cnc", "erro_cnc", falha)
        except Exception:
            pass
        with _lock:
            _estado["alarmes_ativos"] += 1
        _log("alarme", f"ERRO CNC: {falha}")


def _handle_manut_temperatura(payload: dict):
    componente = payload.get("componente", "desconhecido")
    valor      = payload.get("valor_c", 0.0)
    try:
        salvar_leitura_sensor(componente, "temperatura", valor, "°C")
    except Exception as exc:
        logger.warning(f"[DB] Sensor temperatura: {exc}")


def _handle_manut_uso(payload: dict):
    componente = payload.get("componente", "desconhecido")
    tipo       = payload.get("tipo", "horas_uso")
    valor      = payload.get("valor", 0.0)
    unidade    = payload.get("unidade", "h")
    try:
        salvar_leitura_sensor(componente, tipo, valor, unidade)
    except Exception as exc:
        logger.warning(f"[DB] Sensor uso: {exc}")


# ── MQTT Client ────────────────────────────────────────────────────────────────
_HANDLERS = {
    "apsen/os/nova":                _handle_os_nova,
    "apsen/ia/atribuicao":          _handle_ia_atribuicao,
    "apsen/dispenser/carregamento": _handle_dispenser_carregamento,
    "apsen/dispenser/pronto":       _handle_dispenser_pronto,
    "apsen/dispenser/evento":       _handle_dispenser_evento,
    "apsen/dispenser/status":       _handle_dispenser_status,
    "apsen/cnc/status":             _handle_cnc_status,
    "apsen/manut/temperatura":      _handle_manut_temperatura,
    "apsen/manut/uso":              _handle_manut_uso,
}


def _on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe("apsen/#")
        logger.info("MQTT conectado — subscrito em apsen/#")


def _on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        topic   = msg.topic

        handler = _HANDLERS.get(topic)
        if handler:
            handler(payload)

        with _lock:
            snap = {k: v for k, v in _estado.items()}

        _enfileirar_broadcast({"tipo": "estado", **snap})

    except json.JSONDecodeError:
        logger.warning(f"[MQTT] Payload não-JSON em {msg.topic}")
    except Exception as exc:
        logger.error(f"[MQTT] Erro em {msg.topic}: {exc}", exc_info=True)


_mqtt = mqtt.Client(client_id="apsen-backend-v2")
_mqtt.on_connect = _on_connect
_mqtt.on_message = _on_message


# ── App FastAPI ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_event_loop()
    init_db()
    _mqtt.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=60)
    _mqtt.loop_start()
    task = asyncio.create_task(_broadcast_worker())
    logger.info("Backend APSEN v2.0 iniciado.")
    yield
    task.cancel()
    _mqtt.loop_stop()
    _mqtt.disconnect()


app = FastAPI(title="APSEN Backend v2", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── Auth helpers (somente para IHM) ───────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)


def _get_tecnico(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token ausente")
    payload = decodificar_token(creds.credentials)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")
    return payload


# ── Modelos ────────────────────────────────────────────────────────────────────
class LoginReq(BaseModel):
    username: str
    senha: str


class ManutencaoReq(BaseModel):
    tipo: str
    componente: str
    descricao: str


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — DASHBOARD (sem autenticação — read-only público)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/estado")
def get_estado():
    """Snapshot completo do estado atual do sistema."""
    with _lock:
        return dict(_estado)


@app.get("/os/ativa")
def os_ativa():
    """OS em andamento com itens e progresso de cada dispenser."""
    ordem = get_ordem_ativa()
    if not ordem:
        return {"os_ativa": None}
    return {"os_ativa": ordem}


@app.get("/os/historico")
def os_historico(limite: int = 20):
    return get_historico_ordens(limite)


@app.get("/dispensas")
def dispensas(os_id: str = None, limite: int = 100):
    if os_id:
        return get_dispensas(os_id, limite)
    return get_dispensas_recentes(limite)


@app.get("/cnc/historico")
def cnc_historico(limite: int = 50):
    return get_cnc_recentes(limite)


@app.get("/alarmes")
def alarmes(resolvido: bool = False, limite: int = 50):
    return get_alarmes(resolvido=resolvido, limite=limite)


@app.get("/log/eventos")
def log_eventos(limite: int = 50):
    """Log em memória de eventos recentes (dashboard tempo real)."""
    return list(_log_eventos)[:limite]


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — IHM MANUTENÇÃO (requer autenticação)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/auth/login")
def login(req: LoginReq):
    user = get_usuario(req.username)
    if not user or not verificar_senha(req.senha, user["senha_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciais inválidas")
    token = criar_token(user["username"], user["nome_completo"])
    return {"token": token, "username": user["username"], "nome": user["nome_completo"]}


@app.get("/auth/me")
def me(user=Depends(_get_tecnico)):
    return {"username": user["sub"], "nome": user["nome"]}


@app.get("/manutencao/sensores")
def manut_sensores(user=Depends(_get_tecnico)):
    """Última leitura de cada componente+tipo."""
    return get_ultimas_leituras()


@app.get("/manutencao/sensores/{componente}")
def manut_sensor_hist(componente: str, tipo: str = "temperatura", limite: int = 60,
                      user=Depends(_get_tecnico)):
    return get_historico_sensor(componente, tipo, limite)


@app.get("/manutencao/log")
def manut_log(limite: int = 100, user=Depends(_get_tecnico)):
    return get_log_manutencao(limite)


@app.post("/manutencao/log")
def manut_registrar(req: ManutencaoReq, user=Depends(_get_tecnico)):
    return salvar_manutencao(req.tipo, req.componente, req.descricao, user["sub"])


@app.get("/manutencao/alarmes")
def manut_alarmes(resolvido: bool = False, limite: int = 100, user=Depends(_get_tecnico)):
    return get_alarmes(resolvido=resolvido, limite=limite)


@app.put("/manutencao/alarmes/{alarme_id}/resolver")
def manut_resolver_alarme(alarme_id: int, user=Depends(_get_tecnico)):
    resolver_alarme(alarme_id)
    return {"ok": True}


# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await _ws_manager.connect(ws)
    # Envia estado atual ao conectar
    with _lock:
        snap = dict(_estado)
    await ws.send_text(json.dumps({"tipo": "estado", **snap}, default=str))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _ws_manager.disconnect(ws)
