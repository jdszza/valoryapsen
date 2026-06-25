"""
APSEN - CNC Adapter v1.0
Bridge bidirecional entre o Computador Central e o CNC Simulator.

Fluxo entrada (← Central):
  POST /comandos/mover    → repassa para cnc-simulator POST /executar/mover
  POST /comandos/homing   → repassa para cnc-simulator POST /executar/homing

Fluxo saída (← CNC Simulator):
  POST /eventos           → normaliza e encaminha para central-computer POST /api/v1/eventos/cnc
"""
import logging
import os
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [CNC-ADAPTER] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CENTRAL_URL      = os.getenv("CENTRAL_URL",      "http://central-computer:8000")
CNC_SIM_URL      = os.getenv("CNC_SIM_URL",      "http://cnc-simulator:8200")
TIMEOUT_CMD      = float(os.getenv("TIMEOUT_CMD",   "15"))
TIMEOUT_EVENT    = float(os.getenv("TIMEOUT_EVENT", "5"))

app = FastAPI(title="APSEN CNC Adapter v1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Pydantic Models ────────────────────────────────────────────────────────────

class ComandoMoverReq(BaseModel):
    dispenser_alvo: int
    os_id: str
    posicao_x: float
    posicao_y: float
    ciclo_atual: int = 0
    total_ciclos: int = 0


class ComandoHomingReq(BaseModel):
    os_id: str


class EventoReq(BaseModel):
    tipo: str
    os_id: Optional[str] = None
    dispenser_alvo: Optional[int] = None
    model_config = ConfigDict(extra="allow")


# ── Helper HTTP ────────────────────────────────────────────────────────────────

async def _post_sim(path: str, payload: dict, timeout: float = TIMEOUT_CMD) -> dict:
    url = CNC_SIM_URL + path
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(url, json=payload, timeout=timeout)
        if r.status_code >= 300:
            raise HTTPException(502, f"CNC simulator retornou {r.status_code}: {r.text[:200]}")
        return r.json()
    except httpx.RequestError as exc:
        raise HTTPException(503, f"CNC simulator indisponível: {exc}")


async def _post_central(payload: dict) -> bool:
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                CENTRAL_URL + "/api/v1/eventos/cnc",
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
    return {"status": "ok", "service": "apsen-cnc-adapter"}


@app.post("/comandos/mover")
async def cmd_mover(req: ComandoMoverReq):
    logger.info("[CMD] MOVER → D%d (%.1f, %.1f) ciclo %d/%d OS %s",
                req.dispenser_alvo, req.posicao_x, req.posicao_y,
                req.ciclo_atual, req.total_ciclos, req.os_id)
    resultado = await _post_sim(
        "/executar/mover",
        {
            "dispenser_alvo": req.dispenser_alvo,
            "os_id":          req.os_id,
            "posicao_x":      req.posicao_x,
            "posicao_y":      req.posicao_y,
            "ciclo_atual":    req.ciclo_atual,
            "total_ciclos":   req.total_ciclos,
        },
    )
    return {"ok": True, "simulador": resultado}


@app.post("/comandos/homing")
async def cmd_homing(req: ComandoHomingReq):
    logger.info("[CMD] HOMING ← OS %s", req.os_id)
    resultado = await _post_sim("/executar/homing", {"os_id": req.os_id})
    return {"ok": True, "simulador": resultado}


# ── Endpoint de Eventos (Simulator → Adapter → Central) ───────────────────────

@app.post("/eventos")
async def receber_evento(req: EventoReq):
    """Recebe eventos do cnc-simulator e repassa ao Central."""
    payload    = req.model_dump()
    tipo       = payload.get("tipo", "?")
    disp_alvo  = payload.get("dispenser_alvo", "?")
    os_id      = payload.get("os_id", "")

    log_extra = f"| OS {os_id}" if os_id else ""
    logger.info("[EVT] %-12s ← D%s %s", tipo, disp_alvo, log_extra)

    ok = await _post_central(payload)
    if not ok:
        logger.warning("[FWD] Evento '%s' não chegou ao Central.", tipo)

    return {"ok": True, "encaminhado": ok}
