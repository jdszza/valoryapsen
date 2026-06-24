"""
APSEN - Backend FastAPI v2.1
Bridge MQTT → MySQL + API REST para Dashboard (read-only) e IHM (manutenção)

Tópicos MQTT consumidos:
  apsen/os/nova                 ← SAP publica nova Ordem de Saída
  apsen/ia/atribuicao           ← IA atribui medicamentos aos dispensers
  apsen/dispenser/carregamento  ← CV monitora carregamento dos dispensers
  apsen/dispenser/pronto        ← Dispenser carregado e pronto
  apsen/dispenser/evento        ← Cada remédio dispensado
  apsen/dispenser/status        ← Status geral dos dispensers
  apsen/dispenser/limpeza_ok    ← Dispenser confirmou limpeza (novo)
  apsen/cnc/status              ← Status e posição da CNC
  apsen/manut/temperatura       ← Leituras de temperatura (sensores)
  apsen/manut/uso               ← Leituras de desgaste/horas de uso

O backend NÃO controla nenhum sistema. Exceto: publica limpar dispenser via MQTT.
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
    atribuir_dispenser_item, atualizar_item_os, atualizar_status_ordem,
    atualizar_usuario, criar_usuario,
    get_alarmes, get_cnc_recentes, get_dispensas, get_dispensas_recentes,
    get_dispenser_estado, get_dispensers_estado,
    get_historico_ordens, get_historico_sensor, get_log_manutencao,
    get_ordem_ativa, get_ordem_por_id, get_ultimas_leituras,
    get_usuario, get_usuarios,
    init_db, limpar_dispenser_estado, listar_categorias, listar_medicamentos,
    resolver_alarme,
    salvar_alarme, salvar_cnc_evento, salvar_dispensa, salvar_dispenser_estado,
    salvar_leitura_sensor, salvar_manutencao, salvar_ordem,
    toggle_usuario_ativo,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [BACKEND] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Estado em memória (snapshot do sistema) ────────────────────────────────────
_estado = {
    "cnc": {
        "status": "idle",
        "os_id": None,
        "dispenser_alvo": None,
        "posicao_x": 0.0,
        "posicao_y": 0.0,
        "ciclo_atual": 0,
        "total_ciclos": 0,
    },
    "os_ativa": None,
    "dispensers": {
        str(i): {
            "status":                "idle",
            "medicamento":           None,   # preenchido dinamicamente
            "sku":                   None,
            "categoria":             None,
            "quantidade":            0,      # estoque residual no slot
            "quantidade_alvo":       0,
            "quantidade_dispensada": 0,
            "quantidade_residual":   0,
            "os_id":                 None,
        }
        for i in range(1, 7)
    },
    # Fila de OS (FIFO) — rastreada no backend para exibição no dashboard
    "fila_os": [],        # lista de os_id aguardando processamento
    "fila_tamanho": 0,    # contagem para exibição rápida
    "atribuicao_ia": [],
    "alarmes_ativos": 0,
}
_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None
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
    os_id        = payload.get("os_id")
    descricao    = payload.get("descricao", "")
    medicamentos = payload.get("medicamentos", [])

    if not os_id or not medicamentos:
        logger.warning("[MQTT] OS inválida recebida — ignorando.")
        return

    # Persiste no banco (não falha a memória se houver erro de DB)
    try:
        salvar_ordem(os_id, descricao, medicamentos, payload)
    except Exception as exc:
        logger.error(f"[DB] Erro ao salvar OS {os_id}: {exc}", exc_info=True)

    # Atualiza estado em memória (sempre, independente do DB)
    with _lock:
        # Se não há OS ativa, esta vira a ativa; senão vai para a fila de rastreio
        if _estado["os_ativa"] is None:
            _estado["os_ativa"] = {
                "os_id":    os_id,
                "descricao": descricao,
                "status":   "aguardando",
                "categoria": payload.get("categoria", ""),
                "itens":    medicamentos,
            }
        else:
            # OS vai para a fila do dispenser_simulator — rastreia aqui para exibição
            if os_id not in _estado["fila_os"]:
                _estado["fila_os"].append(os_id)
                _estado["fila_tamanho"] = len(_estado["fila_os"])

    _log("os_nova", f"OS {os_id} recebida da SAP — {len(medicamentos)} medicamento(s).")
    logger.info(f"[OS] {os_id} | {descricao}")


def _handle_ia_atribuicao(payload: dict):
    os_id      = payload.get("os_id")
    atribuicoes = payload.get("atribuicoes", [])

    with _lock:
        _estado["atribuicao_ia"] = atribuicoes
        # Se esta OS era a ativa, torna-a em andamento e registra os dispensers atribuídos
        if _estado["os_ativa"] and _estado["os_ativa"].get("os_id") == os_id:
            _estado["os_ativa"]["status"] = "em_andamento"
            _estado["os_ativa"]["atribuicoes"] = atribuicoes
        # Se estava na fila, promove para ativa
        elif os_id in _estado["fila_os"]:
            _estado["fila_os"].remove(os_id)
            _estado["fila_tamanho"] = len(_estado["fila_os"])
            _estado["os_ativa"] = {
                "os_id":       os_id,
                "status":      "em_andamento",
                "atribuicoes": atribuicoes,
            }

    # Atualiza dispenser_id nos itens do DB (roteamento dinâmico)
    for a in atribuicoes:
        try:
            atribuir_dispenser_item(os_id, a.get("medicamento", ""), a["dispenser_id"])
        except Exception as exc:
            logger.warning(f"[DB] Erro ao atribuir dispenser item: {exc}")

    _log("ia_atribuicao", f"Roteamento: {len(atribuicoes)} slot(s) para OS {os_id}.", payload)
    log_str = " | ".join(
        f"D{a['dispenser_id']}←{a.get('medicamento','?')}×{a.get('quantidade','?')}"
        for a in atribuicoes
    )
    logger.info(f"[IA] {os_id}: {log_str}")


def _handle_dispenser_carregamento(payload: dict):
    disp_id  = str(payload.get("dispenser_id", "?"))
    sts      = payload.get("status", "")
    med      = payload.get("medicamento", "")
    falha    = payload.get("motivo_falha")

    with _lock:
        if disp_id in _estado["dispensers"]:
            _estado["dispensers"][disp_id]["medicamento"] = med
            _estado["dispensers"][disp_id]["status"] = f"carregando_{sts}"

    if sts == "erro":
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
        _log("carregamento", f"Dispenser {disp_id} carregando {med} — CV: {sts}")
        logger.info(f"[DISP-{disp_id}] Carregamento {sts}: {med}")


def _handle_dispenser_pronto(payload: dict):
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
    os_id    = payload.get("os_id", "")
    disp_id  = payload.get("dispenser_id")
    med      = payload.get("medicamento", "")
    qtd_disp = payload.get("quantidade_dispensada", 0)
    qtd_alvo = payload.get("quantidade_alvo", 0)
    validado = payload.get("validado", True)
    falha    = payload.get("motivo_falha")
    residual = payload.get("quantidade_residual", 0)

    salvar_dispensa(os_id, disp_id, med, qtd_disp, qtd_alvo, validado, falha)

    disp_key = str(disp_id)
    with _lock:
        if disp_key in _estado["dispensers"]:
            _estado["dispensers"][disp_key]["quantidade_dispensada"] = qtd_disp
            _estado["dispensers"][disp_key]["quantidade_residual"] = residual
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
        _log("dispensa", f"Dispenser {disp_id} dispensou {qtd_disp}/{qtd_alvo} × {med} | residual={residual}")

    if os_id:
        novo_status = "concluido" if qtd_disp >= qtd_alvo else "em_andamento"
        try:
            atualizar_item_os(os_id, disp_id, qtd_disp, novo_status)
        except Exception as exc:
            logger.warning(f"[DB] Erro ao atualizar item OS: {exc}")

    # Persiste quantidade residual + medicamento (slot dinâmico — sem medicamento fixo)
    if disp_id and qtd_disp >= qtd_alvo:
        try:
            categoria = payload.get("categoria")
            salvar_dispenser_estado(disp_id, residual, os_id, med or None, categoria)
        except Exception as exc:
            logger.warning(f"[DB] Erro ao salvar estado dispenser {disp_id}: {exc}")


def _handle_dispenser_status(payload: dict):
    """Atualiza snapshot em memória com estado completo do slot (dinâmico)."""
    disp_id = str(payload.get("dispenser_id", "?"))
    with _lock:
        if disp_id in _estado["dispensers"]:
            _estado["dispensers"][disp_id].update({
                "status":      payload.get("status", "idle"),
                "medicamento": payload.get("medicamento"),
                "sku":         payload.get("sku"),
                "categoria":   payload.get("categoria"),
                "quantidade":  payload.get("quantidade", 0),
                "os_id":       payload.get("os_id"),
            })


def _handle_dispenser_limpeza_ok(payload: dict):
    """Dispenser confirmou limpeza manual."""
    disp_id = payload.get("dispenser_id")
    if not disp_id:
        return
    disp_key = str(disp_id)
    with _lock:
        if disp_key in _estado["dispensers"]:
            _estado["dispensers"][disp_key].update({
                "status": "limpo",
                "quantidade_dispensada": 0,
                "quantidade_alvo": 0,
                "quantidade_residual": 0,
            })
    try:
        limpar_dispenser_estado(disp_id)
    except Exception as exc:
        logger.warning(f"[DB] Erro ao limpar estado dispenser {disp_id}: {exc}")
    _log("limpeza_ok", f"Dispenser {disp_id} limpo com sucesso.")
    logger.info(f"[DISP-{disp_id}] Limpeza confirmada.")


def _handle_cnc_status(payload: dict):
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
        if _estado["os_ativa"] and os_id and _estado["os_ativa"].get("os_id") == os_id:
            if sts == "concluido":
                _estado["os_ativa"]["status"] = "concluida"
            elif sts not in ("idle",):
                _estado["os_ativa"]["status"] = "em_andamento"

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
        # Libera slot de OS ativa (próxima da fila será ativada quando chegar atribuicao_ia)
        with _lock:
            if _estado["os_ativa"] and _estado["os_ativa"].get("os_id") == os_id:
                _estado["os_ativa"] = None

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


def _handle_os_concluida(payload: dict):
    """OS processada com sucesso — libera slot de os_ativa e rastreia fila."""
    os_id          = payload.get("os_id")
    fila_restante  = payload.get("fila_restante", 0)
    with _lock:
        if _estado["os_ativa"] and _estado["os_ativa"].get("os_id") == os_id:
            _estado["os_ativa"] = None
        # Remove da fila de rastreio se ainda estava lá
        if os_id in _estado["fila_os"]:
            _estado["fila_os"].remove(os_id)
        _estado["fila_tamanho"] = len(_estado["fila_os"])
    _log("os_concluida", f"OS {os_id} concluída. Fila restante: {fila_restante}.")


# ── MQTT Client ────────────────────────────────────────────────────────────────
_HANDLERS = {
    "apsen/os/nova":                _handle_os_nova,
    "apsen/os/concluida":           _handle_os_concluida,
    "apsen/ia/atribuicao":          _handle_ia_atribuicao,
    "apsen/dispenser/carregamento": _handle_dispenser_carregamento,
    "apsen/dispenser/pronto":       _handle_dispenser_pronto,
    "apsen/dispenser/evento":       _handle_dispenser_evento,
    "apsen/dispenser/status":       _handle_dispenser_status,
    "apsen/dispenser/limpeza_ok":   _handle_dispenser_limpeza_ok,
    "apsen/dispenser/limpeza_erro": lambda p: _log("limpeza_erro", p.get("mensagem", "Erro limpeza")),
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
    logger.info("Backend APSEN v2.1 iniciado.")
    yield
    task.cancel()
    _mqtt.loop_stop()
    _mqtt.disconnect()


app = FastAPI(title="APSEN Backend v2.1", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── Auth helpers ───────────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)


def _get_tecnico(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token ausente")
    payload = decodificar_token(creds.credentials)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")
    return payload


def _get_admin(user=Depends(_get_tecnico)):
    if user.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requer perfil admin")
    return user


# ── Modelos ────────────────────────────────────────────────────────────────────
class LoginReq(BaseModel):
    username: str
    senha: str


class ManutencaoReq(BaseModel):
    tipo: str
    componente: str
    descricao: str


class StatusOSReq(BaseModel):
    status: str  # aguardando | em_andamento | concluida | erro | cancelada


class UsuarioReq(BaseModel):
    username: str
    senha: str
    nome_completo: str
    role: str = "manutencao"


class UsuarioUpdateReq(BaseModel):
    nome_completo: Optional[str] = None
    role: Optional[str] = None
    nova_senha: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — DASHBOARD (sem autenticação — read-only)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/ping")
def ping():
    return {"status": "ok", "service": "apsen-backend"}


@app.get("/estado")
def get_estado():
    with _lock:
        return dict(_estado)


@app.get("/os/ativa")
def os_ativa():
    ordem = get_ordem_ativa()
    if not ordem:
        return {"os_ativa": None}
    return {"os_ativa": ordem}


@app.get("/os/historico")
def os_historico(limite: int = 50):
    return get_historico_ordens(limite)


@app.get("/os/{os_id}")
def os_detalhe(os_id: str):
    """Retorna OS completa com payload_json e itens."""
    ordem = get_ordem_por_id(os_id)
    if not ordem:
        raise HTTPException(404, "OS não encontrada")
    return ordem


@app.get("/medicamentos")
def get_medicamentos(categoria: str = None):
    """
    Catálogo completo de medicamentos APSEN.
    Query param opcional: ?categoria=snc | cardiologia | infectologia | ...
    Retorna lista com: nome, sku, categoria, categoria_desc, dimensao
    """
    return listar_medicamentos(categoria)


@app.get("/medicamentos/categorias")
def get_categorias():
    """Lista as 12 categorias terapêuticas com total de medicamentos cada."""
    return listar_categorias()


@app.get("/dispensas")
def dispensas(os_id: str = None, limite: int = 100):
    if os_id:
        return get_dispensas(os_id, limite)
    return get_dispensas_recentes(limite)


@app.get("/dispensers/estado")
def dispensers_estado():
    """Estado atual de todos os 6 dispensers (quantidade residual, medicamento)."""
    return get_dispensers_estado()


@app.get("/cnc/historico")
def cnc_historico(limite: int = 50):
    return get_cnc_recentes(limite)


@app.get("/alarmes")
def alarmes(resolvido: bool = False, limite: int = 50):
    return get_alarmes(resolvido=resolvido, limite=limite)


@app.get("/log/eventos")
def log_eventos(limite: int = 50):
    return list(_log_eventos)[:limite]


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — IHM MANUTENÇÃO (requer autenticação)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/auth/login")
def login(req: LoginReq):
    user = get_usuario(req.username)
    if not user or not verificar_senha(req.senha, user["senha_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciais inválidas")
    role  = user.get("role", "manutencao")
    token = criar_token(user["username"], user["nome_completo"], role)
    return {
        "token": token,
        "username": user["username"],
        "nome": user["nome_completo"],
        "role": role,
    }


@app.get("/auth/me")
def me(user=Depends(_get_tecnico)):
    return {"username": user["sub"], "nome": user["nome"], "role": user.get("role")}


# ── OS ─────────────────────────────────────────────────────────────────────────

@app.put("/ordens/{os_id}/status")
def alterar_status_os(os_id: str, req: StatusOSReq, user=Depends(_get_tecnico)):
    """Altera o status de uma OS manualmente (IHM)."""
    validos = {"aguardando", "em_andamento", "concluida", "erro", "cancelada"}
    if req.status not in validos:
        raise HTTPException(400, f"Status inválido. Use: {validos}")
    atualizar_status_ordem(os_id, req.status)
    _log("os_status", f"OS {os_id} → {req.status} por {user['sub']}")
    return {"ok": True, "os_id": os_id, "status": req.status}


# ── Sensores ───────────────────────────────────────────────────────────────────

@app.get("/manutencao/sensores")
def manut_sensores(user=Depends(_get_tecnico)):
    return get_ultimas_leituras()


@app.get("/manutencao/sensores/{componente}")
def manut_sensor_hist(componente: str, tipo: str = "temperatura", limite: int = 60,
                      user=Depends(_get_tecnico)):
    return get_historico_sensor(componente, tipo, limite)


# ── Log manutenção ──────────────────────────────────────────────────────────────

@app.get("/manutencao/log")
def manut_log(limite: int = 100, user=Depends(_get_tecnico)):
    return get_log_manutencao(limite)


@app.post("/manutencao/log")
def manut_registrar(req: ManutencaoReq, user=Depends(_get_tecnico)):
    return salvar_manutencao(req.tipo, req.componente, req.descricao, user["sub"])


# ── Alarmes ────────────────────────────────────────────────────────────────────

@app.get("/manutencao/alarmes")
def manut_alarmes(resolvido: bool = False, limite: int = 100, user=Depends(_get_tecnico)):
    return get_alarmes(resolvido=resolvido, limite=limite)


@app.put("/manutencao/alarmes/{alarme_id}/resolver")
def manut_resolver_alarme(alarme_id: int, user=Depends(_get_tecnico)):
    resolver_alarme(alarme_id)
    return {"ok": True}


# ── Dispensers ─────────────────────────────────────────────────────────────────

@app.post("/manutencao/dispensers/{dispenser_id}/limpar")
def manut_limpar_dispenser(dispenser_id: int, user=Depends(_get_tecnico)):
    """Publica comando de limpeza no MQTT. Bloqueado se slot está em operação."""
    if dispenser_id not in range(1, 7):
        raise HTTPException(400, "dispenser_id deve ser 1-6")

    # Verifica se o slot está em operação ativa
    with _lock:
        d_info   = _estado["dispensers"].get(str(dispenser_id), {})
        d_status = d_info.get("status", "idle")
        os_ativa = _estado["os_ativa"]

    STATUS_BLOQUEADOS = {"carregando", "pronto", "dispensando"}
    if d_status in STATUS_BLOQUEADOS:
        med = d_info.get("medicamento", f"Dispenser {dispenser_id}")
        os_id_ativo = (os_ativa or {}).get("os_id", "?")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Dispenser {dispenser_id} ({med}) está em operação "
            f"(status: '{d_status}') na OS {os_id_ativo}. "
            "Aguarde a contagem finalizar antes de limpar."
        )

    payload = json.dumps({
        "dispenser_id":  dispenser_id,
        "solicitado_por": user["sub"],
        "timestamp":     _ts(),
    })
    _mqtt.publish("apsen/dispenser/limpar", payload)

    salvar_manutencao(
        tipo="limpeza_dispenser",
        componente=f"dispenser_{dispenser_id}",
        descricao=f"Limpeza manual solicitada pelo técnico {user['sub']}",
        tecnico=user["sub"],
    )
    _log("limpeza_solicitada", f"Limpeza D{dispenser_id} por {user['sub']}")
    return {
        "ok": True,
        "dispenser_id": dispenser_id,
        "msg": "Comando enviado. Aguardando confirmação do dispenser.",
    }


# ── Usuários (somente admin) ───────────────────────────────────────────────────

@app.get("/manutencao/usuarios")
def listar_usuarios(user=Depends(_get_admin)):
    return get_usuarios()


@app.post("/manutencao/usuarios")
def criar_novo_usuario(req: UsuarioReq, user=Depends(_get_admin)):
    resultado = criar_usuario(req.username, req.senha, req.nome_completo, req.role)
    if not resultado.get("ok"):
        raise HTTPException(409, resultado.get("erro", "Erro ao criar usuário"))
    return resultado


@app.put("/manutencao/usuarios/{username}")
def editar_usuario(username: str, req: UsuarioUpdateReq, user=Depends(_get_admin)):
    resultado = atualizar_usuario(
        username,
        nome_completo=req.nome_completo,
        role=req.role,
        nova_senha=req.nova_senha,
    )
    if not resultado.get("ok"):
        raise HTTPException(400, resultado.get("erro", "Erro ao atualizar"))
    return resultado


@app.put("/manutencao/usuarios/{username}/desativar")
def desativar_usuario(username: str, user=Depends(_get_admin)):
    if username == user["sub"]:
        raise HTTPException(400, "Não pode desativar a própria conta")
    return toggle_usuario_ativo(username, False)


@app.put("/manutencao/usuarios/{username}/ativar")
def ativar_usuario(username: str, user=Depends(_get_admin)):
    return toggle_usuario_ativo(username, True)


# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await _ws_manager.connect(ws)
    with _lock:
        snap = dict(_estado)
    await ws.send_text(json.dumps({"tipo": "estado", **snap}, default=str))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _ws_manager.disconnect(ws)
