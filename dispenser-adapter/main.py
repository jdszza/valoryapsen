"""
APSEN - Dispenser Adapter v1.0
Bridge bidirecional entre o Computador Central e o Dispenser Simulator.

Fluxo entrada (← Central):
  POST /comandos/carregar   → repassa para dispenser-simulator POST /executar/carregar
  POST /comandos/dispensar  → repassa para dispenser-simulator POST /executar/dispensar
  POST /comandos/limpar     → repassa para dispenser-simulator POST /executar/limpar

Fluxo saída (← Dispenser Simulator):
  POST /eventos             → normaliza e encaminha para central-computer POST /api/v1/eventos/dispenser
"""
import logging
import os
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [DISP-ADAPTER] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CENTRAL_URL          = os.getenv("CENTRAL_URL",          "http://central-computer:8000")
DISPENSER_SIM_URL    = os.getenv("DISPENSER_SIM_URL",    "http://dispenser-simulator:8201")
TIMEOUT_CMD          = float(os.getenv("TIMEOUT_CMD",    "15"))
TIMEOUT_EVENT        = float(os.getenv("TIMEOUT_EVENT",  "5"))

app = FastAPI(title="APSEN Dispenser Adapter v1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Pydantic Models ────────────────────────────────────────────────────────────

class ComandoCarregarReq(BaseModel):
    dispenser_id: int
    medicamento: str
    sku: str = ""
    categoria: str = ""
    quantidade: int
    os_id: str


class ComandoDispensarReq(BaseModel):
    dispenser_id: int
    os_id: str


class ComandoLimparReq(BaseModel):
    dispenser_id: int
    solicitado_por: str = "sistema"


class EventoReq(BaseModel):
    tipo: str
    dispenser_id: Optional[int] = None
    os_id: Optional[str] = None
    model_config = ConfigDict(extra="allow")


# ── Helper HTTP ────────────────────────────────────────────────────────────────

async def _post_sim(path: str, payload: dict, timeout: float = TIMEOUT_CMD) -> dict:
    """Envia comando ao simulador. Lança HTTPException em falha."""
    url = DISPENSER_SIM_URL + path
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(url, json=payload, timeout=timeout)
        if r.status_code >= 300:
            raise HTTPException(502, f"Simulator retornou {r.status_code}: {r.text[:200]}")
        return r.json()
    except httpx.RequestError as exc:
        raise HTTPException(503, f"Dispenser simulator indisponível: {exc}")


async def _post_central(payload: dict) -> bool:
    """Encaminha evento ao Central. Silencioso em falha (não aborta o fluxo)."""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                CENTRAL_URL + "/api/v1/eventos/dispenser",
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
    return {"status": "ok", "service": "apsen-dispenser-adapter"}


@app.post("/comandos/carregar")
async def cmd_carregar(req: ComandoCarregarReq):
    logger.info("[CMD] CARREGAR D%d ← OS %s (%s × %d)",
                req.dispenser_id, req.os_id, req.medicamento, req.quantidade)
    resultado = await _post_sim(
        "/executar/carregar",
        {
            "dispenser_id": req.dispenser_id,
            "medicamento":  req.medicamento,
            "sku":          req.sku,
            "categoria":    req.categoria,
            "quantidade":   req.quantidade,
            "os_id":        req.os_id,
        },
    )
    return {"ok": True, "simulador": resultado}


@app.post("/comandos/dispensar")
async def cmd_dispensar(req: ComandoDispensarReq):
    logger.info("[CMD] DISPENSAR D%d ← OS %s", req.dispenser_id, req.os_id)
    resultado = await _post_sim(
        "/executar/dispensar",
        {"dispenser_id": req.dispenser_id, "os_id": req.os_id},
    )
    return {"ok": True, "simulador": resultado}


@app.post("/comandos/limpar")
async def cmd_limpar(req: ComandoLimparReq):
    logger.info("[CMD] LIMPAR D%d por '%s'", req.dispenser_id, req.solicitado_por)
    resultado = await _post_sim(
        "/executar/limpar",
        {"dispenser_id": req.dispenser_id, "solicitado_por": req.solicitado_por},
    )
    return {"ok": True, "simulador": resultado}


# ── Endpoint de Eventos (Simulator → Adapter → Central) ───────────────────────

@app.post("/eventos")
async def receber_evento(req: EventoReq):
    """Recebe eventos normalizados do dispenser-simulator e os repassa ao Central."""
    payload = req.model_dump()
    tipo    = payload.get("tipo", "?")
    disp_id = payload.get("dispenser_id", "?")
    os_id   = payload.get("os_id", "")

    log_extra = f"| OS {os_id}" if os_id else ""
    logger.info("[EVT] %-12s ← D%s %s", tipo, disp_id, log_extra)

    ok = await _post_central(payload)
    if not ok:
        logger.warning("[FWD] Evento '%s' D%s não chegou ao Central.", tipo, disp_id)

    return {"ok": True, "encaminhado": ok}
