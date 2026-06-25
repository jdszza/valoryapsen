"""
APSEN - Computador Central v3.1
Orquestrador ativo: recebe OS do order-generator, comanda adapters,
consolida eventos, persiste no DB, serve dashboard/IHM via REST + WebSocket.

Comunicação: REST/HTTP/WebSocket — sem MQTT.

FIX v3.1:
  - asyncio.get_running_loop() (era get_event_loop(), deprecado em 3.10+)
  - Handlers de evento agora são async (evita bloquear o event loop)
  - DB writes via asyncio.to_thread() fora do _lock
  - notificar_evento via loop.call_soon_threadsafe (seguro de qualquer contexto)

Endpoints de entrada (dos adapters e order-generator):
  POST /api/v1/ordens              ← order-generator
  POST /api/v1/eventos/dispenser   ← dispenser-adapter
  POST /api/v1/eventos/cnc         ← cnc-adapter
  POST /api/v1/eventos/visao       ← vision-adapter

Endpoints de leitura (dashboard, ihm_web):
  GET  /estado, /os/*, /dispensers/estado, /medicamentos, ...
  WS   /ws
"""
import asyncio
import collections
import copy
import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

import orchestrator as orch
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
                    format="%(asctime)s [CENTRAL] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Estado em memória ──────────────────────────────────────────────────────────
_estado = {
    "cnc": {
        "status":         "idle",
        "os_id":          None,
        "dispenser_alvo": None,
        "posicao_x":      0.0,
        "posicao_y":      0.0,
        "ciclo_atual":    0,
        "total_ciclos":   0,
    },
    "os_ativa":     None,
    "dispensers": {
        str(i): {
            "status":                "idle",
            "medicamento":           None,
            "sku":                   None,
            "categoria":             None,
            "quantidade":            0,
            "quantidade_alvo":       0,
            "quantidade_dispensada": 0,
            "quantidade_residual":   0,
            "os_id":                 None,
        }
        for i in range(1, 7)
    },
    "fila_os":       [],
    "fila_tamanho":  0,
    "atribuicao_ia": [],
    "alarmes_ativos": 0,
    # Última leitura de cada câmera (atualizado por _handle_evento_visao)
    "visao": {
        "camera_dispenser": {
            "ultima_leitura": None,   # tipo do último evento
            "slot_id":        None,
            "match_sku":      None,
            "confianca":      None,
            "ts":             None,
        },
        "camera_mesa": {
            "ultima_leitura":      None,
            "slot_id":             None,
            "quantidade_detectada": None,
            "quantidade_esperada":  None,
            "confianca":           None,
            "ts":                  None,
        },
    },
}
_lock = threading.Lock()
_log_eventos: collections.deque = collections.deque(maxlen=100)

# ── Event loop global (set na lifespan) ───────────────────────────────────────
_loop: Optional[asyncio.AbstractEventLoop] = None


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
    """Thread-safe: pode ser chamado de qualquer thread."""
    if _loop and not _loop.is_closed():
        _loop.call_soon_threadsafe(_broadcast_q.put_nowait, data)


def _broadcast_estado():
    with _lock:
        snap = copy.deepcopy(_estado)
    _enfileirar_broadcast({"tipo": "estado", **snap})


# ── Helpers DB async ──────────────────────────────────────────────────────────
async def _db(fn, *args):
    """Executa função síncrona de DB em threadpool — não bloqueia o event loop."""
    try:
        await asyncio.to_thread(fn, *args)
    except Exception as exc:
        logger.warning("[DB] %s(%s): %s", fn.__name__, args[:2], exc)


# ── Handlers de eventos vindos dos adapters ────────────────────────────────────

async def _handle_evento_dispenser(payload: dict):
    """
    Processa eventos do dispenser-adapter e notifica o orquestrador.
    Atualização de estado (rápida, sob _lock) é síncrona.
    Escrita em DB é assíncrona (asyncio.to_thread) — não bloqueia o loop.
    Tipos: status, carregado, dispensado, erro, limpeza_ok, telemetria
    """
    tipo    = payload.get("tipo", "status")
    disp_id = payload.get("dispenser_id")
    os_id   = payload.get("os_id")

    if disp_id is None:
        return

    disp_key = str(disp_id)
    db_tasks = []  # (fn, args) a executar fora do lock

    # ── Atualiza estado em memória (síncrono, sem await) ──────────────────
    with _lock:
        if disp_key not in _estado["dispensers"]:
            return
        d = _estado["dispensers"][disp_key]

        if tipo == "status":
            med = payload.get("medicamento")
            qty = payload.get("quantidade", 0)
            d.update({
                "status":      payload.get("status", d["status"]),
                "medicamento": med,
                "sku":         payload.get("sku"),
                "categoria":   payload.get("categoria"),
                "quantidade":  qty,
                "os_id":       payload.get("os_id", d["os_id"]),
            })
            med_db = med if (qty or 0) > 0 else None
            cat_db = payload.get("categoria") if (qty or 0) > 0 else None
            db_tasks.append((salvar_dispenser_estado,
                             (int(disp_id), qty or 0, os_id, med_db, cat_db)))

        elif tipo == "carregado":
            med = payload.get("medicamento")
            qty = payload.get("quantidade_total", payload.get("quantidade", 0))
            d.update({
                "status":      "pronto",
                "medicamento": med,
                "quantidade":  qty,
                "os_id":       os_id,
            })
            db_tasks.append((salvar_dispenser_estado,
                             (int(disp_id), qty or 0, os_id,
                              med if (qty or 0) > 0 else None,
                              payload.get("categoria"))))

        elif tipo == "dispensado":
            qtd_disp  = payload.get("quantidade_dispensada", 0)
            qtd_alvo  = payload.get("quantidade_alvo", 0)
            residual  = payload.get("quantidade_residual", 0)
            # Dispenser envia 'falha_mecanica' (bool) — 'validado' é derivado
            falha_mec = payload.get("falha_mecanica", False)
            validado  = not falha_mec and (qtd_disp >= qtd_alvo)
            falha     = payload.get("motivo_falha")
            med       = d.get("medicamento", payload.get("medicamento", ""))

            d.update({
                "status":                "concluido" if qtd_disp >= qtd_alvo else "dispensando",
                "quantidade_dispensada": qtd_disp,
                "quantidade_residual":   residual,
                "quantidade":            residual,
            })
            if residual == 0:
                d["medicamento"] = None
                d["sku"]         = None
                d["categoria"]   = None

            med_db = d["medicamento"] if residual > 0 else None
            cat_db = d.get("categoria") if residual > 0 else None
            db_tasks.append((salvar_dispensa,
                             (os_id, disp_id, med, qtd_disp, qtd_alvo, validado, falha)))
            db_tasks.append((salvar_dispenser_estado,
                             (int(disp_id), residual, os_id, med_db, cat_db)))
            if os_id:
                status_item = "concluido" if qtd_disp >= qtd_alvo else "em_andamento"
                db_tasks.append((atualizar_item_os, (os_id, disp_id, qtd_disp, status_item)))
            if not validado:
                db_tasks.append((salvar_alarme,
                                 (f"dispenser_{disp_id}", "falha_validacao",
                                  falha or f"Remédio rejeitado D{disp_id}")))
                _estado["alarmes_ativos"] = _estado.get("alarmes_ativos", 0) + 1

        elif tipo == "erro":
            d["status"] = "erro"
            descricao = payload.get("descricao", f"Erro no dispenser {disp_id}")
            db_tasks.append((salvar_alarme,
                             (f"dispenser_{disp_id}",
                              payload.get("codigo_erro", "erro"), descricao)))
            _estado["alarmes_ativos"] = _estado.get("alarmes_ativos", 0) + 1
            _log("alarme", f"ERRO D{disp_id}: {descricao}")

        elif tipo == "limpeza_ok":
            d.update({"status": "limpo", "medicamento": None, "sku": None,
                      "categoria": None, "quantidade": 0, "os_id": None})
            db_tasks.append((limpar_dispenser_estado, (int(disp_id),)))
            _log("limpeza_ok", f"D{disp_id} limpo.")

        elif tipo == "telemetria":
            componente = payload.get("componente", f"dispenser_{disp_id}")
            valor      = payload.get("valor_c", payload.get("valor", 0.0))
            unidade    = payload.get("unidade", "°C")
            tipo_leit  = payload.get("tipo_leitura", "temperatura")
            db_tasks.append((salvar_leitura_sensor,
                             (componente, tipo_leit, valor, unidade)))

    # ── Escreve no DB (async, fora do lock) ───────────────────────────────
    for fn, args in db_tasks:
        await _db(fn, *args)

    # ── Notifica orquestrador (no event loop, seguro) ─────────────────────
    if os_id:
        if tipo == "carregado":
            orch.notificar_evento(f"{os_id}:carregado:{disp_id}", payload)
        elif tipo == "dispensado":
            orch.notificar_evento(f"{os_id}:dispensado:{disp_id}", payload)
        elif tipo == "erro":
            orch.notificar_evento(f"{os_id}:carregado:{disp_id}", {**payload, "tipo": "erro"})
            orch.notificar_evento(f"{os_id}:dispensado:{disp_id}", {**payload, "tipo": "erro"})

    _log(f"disp_{tipo}", f"D{disp_id}: {tipo}", payload)
    _broadcast_estado()


async def _handle_evento_cnc(payload: dict):
    """
    Processa eventos do cnc-adapter.
    Estado atualizado síncronamente sob _lock; DB writes via asyncio.to_thread.
    Tipos: movendo, posicionado, concluido, erro, retornando, telemetria
    """
    tipo       = payload.get("tipo", "movendo")
    os_id      = payload.get("os_id")
    disp_alvo  = payload.get("dispenser_alvo")
    pos_x      = payload.get("posicao_x", 0.0)
    pos_y      = payload.get("posicao_y", 0.0)
    ciclo      = payload.get("ciclo_atual", 0)
    total      = payload.get("total_ciclos", 0)
    db_tasks   = []

    with _lock:
        _estado["cnc"].update({
            "status":         tipo,
            "os_id":          os_id,
            "dispenser_alvo": disp_alvo,
            "posicao_x":      pos_x,
            "posicao_y":      pos_y,
            "ciclo_atual":    ciclo,
            "total_ciclos":   total,
        })
        if _estado["os_ativa"] and os_id:
            if _estado["os_ativa"].get("os_id") == os_id and tipo == "concluido":
                _estado["os_ativa"]["status"] = "concluida"

        if tipo in ("posicionado", "concluido", "erro", "movendo"):
            db_tasks.append((salvar_cnc_evento,
                             (os_id, tipo, disp_alvo, pos_x, pos_y, ciclo, total)))

        if tipo == "erro":
            descricao = payload.get("descricao", "Erro desconhecido na CNC")
            db_tasks.append((salvar_alarme,
                             ("cnc", payload.get("codigo_erro", "erro_cnc"), descricao)))
            _estado["alarmes_ativos"] = _estado.get("alarmes_ativos", 0) + 1
            _log("alarme", f"ERRO CNC: {descricao}")

        elif tipo == "telemetria":
            componente = payload.get("componente", "cnc")
            valor      = payload.get("valor", 0.0)
            unidade    = payload.get("unidade", "°C")
            tipo_leit  = payload.get("tipo_leitura", "temperatura")
            db_tasks.append((salvar_leitura_sensor,
                             (componente, tipo_leit, valor, unidade)))

    # DB fora do lock
    for fn, args in db_tasks:
        await _db(fn, *args)

    # Notifica orquestrador
    if os_id and disp_alvo is not None:
        if tipo == "posicionado":
            orch.notificar_evento(f"{os_id}:posicionado:{disp_alvo}", payload)
        elif tipo == "erro":
            orch.notificar_evento(f"{os_id}:posicionado:{disp_alvo}",
                                  {**payload, "tipo": "erro"})

    _log(f"cnc_{tipo}", f"CNC {tipo} | D{disp_alvo} | ({pos_x:.1f},{pos_y:.1f})")
    _broadcast_estado()


async def _handle_evento_visao(payload: dict):
    """
    Processa eventos do vision-adapter.
    Tipos (câmera dispenser): leitura_dispenser_ok, leitura_dispenser_falha, leitura_dispenser_divergencia
    Tipos (câmera mesa):      leitura_mesa_ok, leitura_mesa_falha, leitura_mesa_divergencia
    Tipo  (telemetria):       telemetria (temperatura dos componentes de câmera)

    Estado atualizado síncronamente sob _lock.
    Alarmes gerados assincronamente via asyncio.to_thread.
    Orquestrador notificado via notificar_evento (call_soon_threadsafe).
    """
    tipo    = payload.get("tipo", "")
    camera  = payload.get("camera", "")
    slot_id = payload.get("slot_id")
    os_id   = payload.get("os_id")
    db_tasks = []

    with _lock:
        # ── Câmera dos Dispensers ──────────────────────────────────────────
        if camera == "dispenser" and tipo in (
            "leitura_dispenser_ok",
            "leitura_dispenser_falha",
            "leitura_dispenser_divergencia",
        ):
            _estado["visao"]["camera_dispenser"].update({
                "ultima_leitura": tipo,
                "slot_id":        slot_id,
                "match_sku":      payload.get("match_sku"),
                "confianca":      payload.get("confianca"),
                "ts":             payload.get("ts"),
            })

            if tipo == "leitura_dispenser_falha":
                descricao = (f"Falha câmera dispenser slot {slot_id}: "
                             f"{payload.get('motivo', 'desconhecido')}")
                db_tasks.append((salvar_alarme,
                                 (f"camera_dispenser_{slot_id}",
                                  "falha_leitura_dispenser", descricao)))
                _estado["alarmes_ativos"] = _estado.get("alarmes_ativos", 0) + 1
                _log("alarme", descricao)

            elif tipo == "leitura_dispenser_divergencia":
                descricao = (f"SKU incorreto slot {slot_id}: "
                             f"esperado={payload.get('sku_esperado','?')} "
                             f"lido={payload.get('sku_lido','?')}")
                db_tasks.append((salvar_alarme,
                                 (f"camera_dispenser_{slot_id}",
                                  "divergencia_sku", descricao)))
                _estado["alarmes_ativos"] = _estado.get("alarmes_ativos", 0) + 1
                _log("alarme", descricao)

        # ── Câmera da Mesa ─────────────────────────────────────────────────
        elif camera == "mesa" and tipo in (
            "leitura_mesa_ok",
            "leitura_mesa_falha",
            "leitura_mesa_divergencia",
        ):
            _estado["visao"]["camera_mesa"].update({
                "ultima_leitura":       tipo,
                "slot_id":              slot_id,
                "quantidade_detectada": payload.get("quantidade_detectada"),
                "quantidade_esperada":  payload.get("quantidade_esperada"),
                "confianca":            payload.get("confianca"),
                "ts":                   payload.get("ts"),
            })

            if tipo == "leitura_mesa_falha":
                descricao = (f"Câmera mesa não detectou produto slot {slot_id}: "
                             f"{payload.get('motivo', 'desconhecido')}")
                db_tasks.append((salvar_alarme,
                                 (f"camera_mesa_{slot_id}",
                                  "falha_deteccao_mesa", descricao)))
                _estado["alarmes_ativos"] = _estado.get("alarmes_ativos", 0) + 1
                _log("alarme", descricao)

            elif tipo == "leitura_mesa_divergencia":
                det = payload.get("quantidade_detectada", 0)
                esp = payload.get("quantidade_esperada", 0)
                descricao = (f"Contagem incorreta slot {slot_id}: "
                             f"esperado={esp} detectado={det} "
                             f"(Δ={det - esp:+d})")
                db_tasks.append((salvar_alarme,
                                 (f"camera_mesa_{slot_id}",
                                  "divergencia_contagem", descricao)))
                _estado["alarmes_ativos"] = _estado.get("alarmes_ativos", 0) + 1
                _log("alarme", descricao)

        # ── Telemetria das câmeras ──────────────────────────────────────────
        elif tipo == "telemetria":
            componente = payload.get("componente", "camera_sistema")
            valor      = payload.get("valor", 0.0)
            unidade    = payload.get("unidade", "°C")
            tipo_leit  = payload.get("tipo_leitura", "temperatura")
            db_tasks.append((salvar_leitura_sensor,
                             (componente, tipo_leit, valor, unidade)))

    # ── DB fora do lock ────────────────────────────────────────────────────
    for fn, args in db_tasks:
        await _db(fn, *args)

    # ── Notifica orquestrador ──────────────────────────────────────────────
    if os_id and slot_id is not None:
        # Dispenser: qualquer resultado (ok, falha, divergência) desbloqueia o await
        if tipo.startswith("leitura_dispenser_"):
            orch.notificar_evento(f"{os_id}:visao_dispenser:{slot_id}", payload)
        # Mesa: idem
        elif tipo.startswith("leitura_mesa_"):
            orch.notificar_evento(f"{os_id}:visao_mesa:{slot_id}", payload)

    _log(f"visao_{tipo}", f"cam={camera} slot={slot_id}", payload)
    _broadcast_estado()


# ── FastAPI Lifespan ───────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    # FIX: get_running_loop() em vez de get_event_loop() (deprecado em Python ≥3.10)
    _loop = asyncio.get_running_loop()

    await asyncio.to_thread(init_db)

    # Injeta loop no orquestrador para notificar_evento thread-safe
    orch.inicializar(_estado, _lock, _broadcast_estado, _loop)

    task_broadcast = asyncio.create_task(_broadcast_worker())
    task_orch      = asyncio.create_task(orch.loop_orquestrador())

    logger.info("Computador Central APSEN v3.1 iniciado.")
    yield

    task_broadcast.cancel()
    task_orch.cancel()


app = FastAPI(title="APSEN Computador Central v3.1", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

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


# ── Pydantic Models ────────────────────────────────────────────────────────────
class LoginReq(BaseModel):
    username: str
    senha: str


class ManutencaoReq(BaseModel):
    tipo: str
    componente: str
    descricao: str


class StatusOSReq(BaseModel):
    status: str


class UsuarioReq(BaseModel):
    username: str
    senha: str
    nome_completo: str
    role: str = "manutencao"


class UsuarioUpdateReq(BaseModel):
    nome_completo: Optional[str] = None
    role: Optional[str] = None
    nova_senha: Optional[str] = None


class NovaOSReq(BaseModel):
    os_id: str
    descricao: str = ""
    categoria: str = ""
    medicamentos: list


class EventoDispenserReq(BaseModel):
    tipo: str
    dispenser_id: Optional[int] = None
    os_id: Optional[str] = None

    model_config = {"extra": "allow"}


class EventoCNCReq(BaseModel):
    tipo: str
    os_id: Optional[str] = None
    dispenser_alvo: Optional[int] = None

    model_config = {"extra": "allow"}


class EventoVisionReq(BaseModel):
    tipo: str
    camera: str                     # "dispenser" | "mesa" | "sistema"
    slot_id: Optional[int] = None
    os_id: Optional[str] = None

    model_config = {"extra": "allow"}


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — RECEBIMENTO (adapters e order-generator)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/v1/ordens")
async def receber_ordem(req: NovaOSReq):
    """Recebe nova OS do order-generator."""
    os_id        = req.os_id
    medicamentos = req.medicamentos

    if not os_id or not medicamentos:
        raise HTTPException(400, "os_id e medicamentos são obrigatórios")

    try:
        await asyncio.to_thread(
            salvar_ordem, os_id, req.descricao, medicamentos, req.model_dump()
        )
    except Exception as exc:
        logger.error("[DB] Erro ao salvar OS %s: %s", os_id, exc)

    with _lock:
        posicao_fila = len(_estado["fila_os"]) + (1 if _estado["os_ativa"] else 0)

    await orch.enfileirar_os(req.model_dump())

    _log("os_nova", f"OS {os_id} recebida — {len(medicamentos)} medicamento(s)")
    logger.info("[API] OS %s recebida.", os_id)
    _broadcast_estado()

    return {"aceita": True, "os_id": os_id, "posicao_fila": posicao_fila + 1}


@app.post("/api/v1/eventos/dispenser")
async def evento_dispenser(req: EventoDispenserReq):
    """Recebe eventos normalizados do dispenser-adapter."""
    await _handle_evento_dispenser(req.model_dump())
    return {"ok": True}


@app.post("/api/v1/eventos/cnc")
async def evento_cnc(req: EventoCNCReq):
    """Recebe eventos normalizados do cnc-adapter."""
    await _handle_evento_cnc(req.model_dump())
    return {"ok": True}


@app.post("/api/v1/eventos/visao")
async def evento_visao(req: EventoVisionReq):
    """Recebe resultados de captura do vision-adapter (câmera dispenser e câmera mesa)."""
    await _handle_evento_visao(req.model_dump())
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — DASHBOARD (sem autenticação, read-only)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/ping")
def ping():
    return {"status": "ok", "service": "apsen-central-computer"}


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
    ordem = get_ordem_por_id(os_id)
    if not ordem:
        raise HTTPException(404, "OS não encontrada")
    return ordem


@app.get("/medicamentos")
def get_medicamentos(categoria: str = None):
    return listar_medicamentos(categoria)


@app.get("/medicamentos/categorias")
def get_categorias():
    return listar_categorias()


@app.get("/dispensas")
def dispensas(os_id: str = None, limite: int = 100):
    if os_id:
        return get_dispensas(os_id, limite)
    return get_dispensas_recentes(limite)


@app.get("/dispensers/estado")
def dispensers_estado():
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


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — IHM MANUTENÇÃO (requer JWT)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/auth/login")
def login(req: LoginReq):
    user = get_usuario(req.username)
    if not user or not verificar_senha(req.senha, user["senha_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciais inválidas")
    role  = user.get("role", "manutencao")
    token = criar_token(user["username"], user["nome_completo"], role)
    return {
        "token":    token,
        "username": user["username"],
        "nome":     user["nome_completo"],
        "role":     role,
    }


@app.get("/auth/me")
def me(user=Depends(_get_tecnico)):
    return {"username": user["sub"], "nome": user["nome"], "role": user.get("role")}


@app.put("/ordens/{os_id}/status")
def alterar_status_os(os_id: str, req: StatusOSReq, user=Depends(_get_tecnico)):
    validos = {"aguardando", "em_andamento", "concluida", "erro", "cancelada"}
    if req.status not in validos:
        raise HTTPException(400, f"Status inválido. Use: {validos}")
    atualizar_status_ordem(os_id, req.status)
    _log("os_status", f"OS {os_id} → {req.status} por {user['sub']}")
    return {"ok": True, "os_id": os_id, "status": req.status}


@app.get("/manutencao/sensores")
def manut_sensores(user=Depends(_get_tecnico)):
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


@app.post("/manutencao/dispensers/{dispenser_id}/limpar")
async def manut_limpar_dispenser(dispenser_id: int, user=Depends(_get_tecnico)):
    """Envia comando de limpeza ao dispenser-adapter. Bloqueado se slot está em operação."""
    if dispenser_id not in range(1, 7):
        raise HTTPException(400, "dispenser_id deve ser 1-6")

    with _lock:
        d_info   = _estado["dispensers"].get(str(dispenser_id), {})
        d_status = d_info.get("status", "idle")
        os_ativa = _estado["os_ativa"]

    STATUS_BLOQUEADOS = {"carregando", "pronto", "dispensando", "aguardando_carga"}
    if d_status in STATUS_BLOQUEADOS:
        med      = d_info.get("medicamento", f"Dispenser {dispenser_id}")
        os_atual = (os_ativa or {}).get("os_id", "?")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Dispenser {dispenser_id} ({med}) em operação "
            f"(status: '{d_status}') na OS {os_atual}.",
        )

    ok = await orch.cmd_limpar(dispenser_id, user["sub"])
    if not ok:
        raise HTTPException(503, "Dispenser-adapter indisponível")

    await asyncio.to_thread(
        salvar_manutencao,
        "limpeza_dispenser",
        f"dispenser_{dispenser_id}",
        f"Limpeza manual por {user['sub']}",
        user["sub"],
    )
    _log("limpeza_solicitada", f"Limpeza D{dispenser_id} por {user['sub']}")
    return {"ok": True, "dispenser_id": dispenser_id,
            "msg": "Comando enviado. Aguardando confirmação do dispenser."}


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
    resultado = atualizar_usuario(username, req.nome_completo, req.role, req.nova_senha)
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
