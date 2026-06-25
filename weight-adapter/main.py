"""
APSEN - Weight Adapter v1.0
Bridge bidirecional entre o Computador Central e o Weight Simulator (HX711).

Fluxo entrada (← Central):
  POST /comandos/tara     → repassa ao weight-simulator POST /executar/tara
  POST /comandos/pesar    → repassa ao weight-simulator POST /executar/pesar

Fluxo saída (← Weight Simulator):
  POST /eventos           → normaliza e encaminha ao central POST /api/v1/eventos/peso
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WEIGHT-ADAPTER] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

CENTRAL_URL     = os.getenv("CENTRAL_URL",      "http://central-computer:8000")
WEIGHT_SIM_URL  = os.getenv("WEIGHT_SIM_URL",   "http://weight-simulator:8203")
TIMEOUT_CMD     = float(os.getenv("TIMEOUT_CMD",   "10"))
TIMEOUT_EVENT   = float(os.getenv("TIMEOUT_EVENT", "5"))

_client: httpx.AsyncClient | None = None


async def _wait_for_upstream(name: str, url: str, retries: int = 30, interval: float = 2.0):
    for i in range(retries):
        try:
            r = await _client.get(url, timeout=3.0)
            if r.status_code < 300:
                logger.info("[HEALTH] %s disponível após %d tentativa(s).", name, i + 1)
                return
        except Exception:
            pass
        logger.warning("[HEALTH] %s indisponível — aguardando (tentativa %d/%d)...", name, i + 1, retries)
        await asyncio.sleep(interval)
    logger.error("[HEALTH] %s não ficou disponível em %ds. Continuando assim mesmo.", name, retries * interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient()
    logger.info("[STARTUP] httpx.AsyncClient criado")
    await _wait_for_upstream("weight-simulator", WEIGHT_SIM_URL + "/ping")
    yield
    await _client.aclose()
    logger.info("[SHUTDOWN] httpx.AsyncClient encerrado")


app = FastAPI(title="APSEN Weight Adapter v1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Pydantic Models ────────────────────────────────────────────────────────────

class TaraReq(BaseModel):
    os_id: str = ""


class PesarReq(BaseModel):
    os_id: str
    slot_id: int
    quantidade_esperada: int
    peso_unitario_g: float


class EventoReq(BaseModel):
    tipo: str
    os_id: Optional[str] = None
    slot_id: Optional[int] = None
    model_config = ConfigDict(extra="allow")


# ── Helpers HTTP ───────────────────────────────────────────────────────────────

async def _post_sim(path: str, payload: dict, timeout: float = TIMEOUT_CMD) -> dict:
    url = WEIGHT_SIM_URL + path
    try:
        r = await _client.post(url, json=payload, timeout=timeout)
        if r.status_code >= 300:
            raise HTTPException(502, f"Weight simulator retornou {r.status_code}: {r.text[:200]}")
        return r.json()
    except httpx.RequestError as exc:
        raise HTTPException(503, f"Weight simulator indisponível: {exc}")


async def _post_central(payload: dict) -> bool:
    try:
        r = await _client.post(
            CENTRAL_URL + "/api/v1/eventos/peso",
            json=payload,
            timeout=TIMEOUT_EVENT,
        )
        return r.status_code < 300
    except Exception as exc:
        logger.warning("[FWD] Falha ao encaminhar evento ao Central: %s", exc)
        return False


# ── Endpoints de Comandos (Central → Adapter → Simulator) ─────────────────────

@app.get("/ping")
def ping():
    return {"status": "ok", "service": "apsen-weight-adapter"}


@app.get("/health")
async def health():
    checks = {}
    for name, url in [
        ("weight-simulator", WEIGHT_SIM_URL + "/ping"),
        ("central-computer", CENTRAL_URL + "/ping"),
    ]:
        try:
            r = await _client.get(url, timeout=3.0)
            checks[name] = "ok" if r.status_code < 300 else f"http_{r.status_code}"
        except Exception as exc:
            checks[name] = f"erro: {exc}"
    ok = all(v == "ok" for v in checks.values())
    return {"status": "ok" if ok else "degradado", "checks": checks}


@app.post("/comandos/tara")
async def cmd_tara(req: TaraReq):
    """Zera a balança antes de uma OS."""
    logger.info("[CMD] TARA ← OS=%s", req.os_id)
    resultado = await _post_sim("/executar/tara", {"os_id": req.os_id})
    return {"ok": True, "simulador": resultado}


@app.post("/comandos/pesar")
async def cmd_pesar(req: PesarReq):
    """Solicita leitura de peso após dispensa no slot especificado."""
    logger.info(
        "[CMD] PESAR slot=%d | qtd=%d | peso_unit=%.1fg | OS=%s",
        req.slot_id, req.quantidade_esperada, req.peso_unitario_g, req.os_id,
    )
    resultado = await _post_sim(
        "/executar/pesar",
        {
            "os_id":               req.os_id,
            "slot_id":             req.slot_id,
            "quantidade_esperada": req.quantidade_esperada,
            "peso_unitario_g":     req.peso_unitario_g,
        },
    )
    return {"ok": True, "simulador": resultado}


# ── Endpoint de Eventos (Simulator → Adapter → Central) ───────────────────────

@app.post("/eventos")
async def receber_evento(req: EventoReq):
    """Recebe resultado de pesagem do weight-simulator e encaminha ao Central."""
    payload = req.model_dump()
    tipo    = payload.get("tipo", "?")
    slot_id = payload.get("slot_id", "?")
    os_id   = payload.get("os_id", "")

    log_extra = f"| OS {os_id}" if os_id else ""
    logger.info("[EVT] %-22s ← slot=%s %s", tipo, slot_id, log_extra)

    ok = await _post_central(payload)
    if not ok:
        logger.warning("[FWD] Evento '%s' slot=%s não chegou ao Central.", tipo, slot_id)

    return {"ok": True, "encaminhado": ok}
