"""
APSEN - Vision Adapter v1.0
Bridge bidirecional entre o Computador Central e o Vision Simulator.

Câmera dos Dispensers: leitura de QR Code, DataMatrix e código de barras.
  → Identifica e valida o produto carregado em cada slot.

Câmera da Mesa: detecção de posição, contagem visual e validação da operação.
  → Confirma que os produtos foram dispensados corretamente na mesa.

Fluxo entrada (← Central):
  POST /comandos/capturar/dispenser  → solicita leitura da câmera do dispenser
  POST /comandos/capturar/mesa       → solicita leitura da câmera da mesa

Fluxo saída (← Vision Simulator):
  POST /eventos  → normaliza e encaminha para central POST /api/v1/eventos/visao

Contrato de normalização:
  Eventos recebidos do simulator são repassados ao Central sem alteração de schema,
  garantindo que o adapter possa ser substituído por hardware real sem mudar o Central.
"""
import logging
import os
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [VISION-ADAPTER] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

CENTRAL_URL       = os.getenv("CENTRAL_URL",        "http://central-computer:8000")
VISION_SIM_URL    = os.getenv("VISION_SIM_URL",     "http://vision-simulator:8202")
TIMEOUT_CMD       = float(os.getenv("TIMEOUT_CMD",  "20"))
TIMEOUT_EVENT     = float(os.getenv("TIMEOUT_EVENT", "5"))

app = FastAPI(title="APSEN Vision Adapter v1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Pydantic Models ────────────────────────────────────────────────────────────

class CapturarDispenserReq(BaseModel):
    """Comando do Central: fotografar câmera do dispenser para slot X."""
    slot_id: int
    os_id: str
    sku_esperado: str = ""
    medicamento_esperado: str = ""
    quantidade_esperada: int = 0


class CapturarMesaReq(BaseModel):
    """Comando do Central: fotografar câmera da mesa após dispensa no slot X."""
    slot_id: int
    os_id: str
    quantidade_esperada: int = 0
    posicao_x: float = 0.0
    posicao_y: float = 0.0


class EventoVisionReq(BaseModel):
    """Evento recebido do Vision Simulator."""
    tipo: str
    camera: str                    # "dispenser" | "mesa"
    slot_id: Optional[int] = None
    os_id: Optional[str] = None

    model_config = ConfigDict(extra="allow")


# ── Helpers HTTP ───────────────────────────────────────────────────────────────

async def _post_sim(path: str, payload: dict, timeout: float = TIMEOUT_CMD) -> dict:
    """Envia comando ao vision-simulator. Lança HTTPException em falha."""
    url = VISION_SIM_URL + path
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(url, json=payload, timeout=timeout)
        if r.status_code >= 300:
            raise HTTPException(502, f"Vision simulator retornou {r.status_code}: {r.text[:200]}")
        return r.json()
    except httpx.RequestError as exc:
        raise HTTPException(503, f"Vision simulator indisponível: {exc}")


async def _post_central(payload: dict) -> bool:
    """Encaminha evento ao Central. Silencioso em falha (não aborta o fluxo de visão)."""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                CENTRAL_URL + "/api/v1/eventos/visao",
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
    return {"status": "ok", "service": "apsen-vision-adapter"}


@app.post("/comandos/capturar/dispenser")
async def capturar_dispenser(req: CapturarDispenserReq):
    """
    Dispara captura da câmera do dispenser para o slot especificado.
    Valida QR Code / DataMatrix / Código de Barras do produto carregado.
    """
    logger.info(
        "[CMD] CAPTURAR DISPENSER slot=%d | sku=%s | med=%s | qtd=%d | OS=%s",
        req.slot_id, req.sku_esperado, req.medicamento_esperado,
        req.quantidade_esperada, req.os_id,
    )
    resultado = await _post_sim(
        "/executar/capturar/dispenser",
        {
            "slot_id":             req.slot_id,
            "os_id":               req.os_id,
            "sku_esperado":        req.sku_esperado,
            "medicamento_esperado": req.medicamento_esperado,
            "quantidade_esperada": req.quantidade_esperada,
        },
    )
    return {"ok": True, "simulador": resultado}


@app.post("/comandos/capturar/mesa")
async def capturar_mesa(req: CapturarMesaReq):
    """
    Dispara captura da câmera da mesa após dispensa no slot especificado.
    Detecta presença, quantidade e posição dos produtos na mesa.
    """
    logger.info(
        "[CMD] CAPTURAR MESA slot=%d | qtd_esperada=%d | pos=(%.1f,%.1f) | OS=%s",
        req.slot_id, req.quantidade_esperada,
        req.posicao_x, req.posicao_y, req.os_id,
    )
    resultado = await _post_sim(
        "/executar/capturar/mesa",
        {
            "slot_id":           req.slot_id,
            "os_id":             req.os_id,
            "quantidade_esperada": req.quantidade_esperada,
            "posicao_x":         req.posicao_x,
            "posicao_y":         req.posicao_y,
        },
    )
    return {"ok": True, "simulador": resultado}


# ── Endpoint de Eventos (Simulator → Adapter → Central) ───────────────────────

@app.post("/eventos")
async def receber_evento(req: EventoVisionReq):
    """
    Recebe resultado de captura do vision-simulator e encaminha ao Central.
    O adapter não interpreta o resultado — repassa integralmente.
    """
    payload = req.model_dump()
    tipo    = payload.get("tipo", "?")
    camera  = payload.get("camera", "?")
    slot_id = payload.get("slot_id", "?")
    os_id   = payload.get("os_id", "")

    log_extra = f"| OS {os_id}" if os_id else ""
    logger.info("[EVT] %-28s ← cam=%-9s slot=%s %s", tipo, camera, slot_id, log_extra)

    ok = await _post_central(payload)
    if not ok:
        logger.warning("[FWD] Evento '%s' cam=%s slot=%s não chegou ao Central.",
                       tipo, camera, slot_id)

    return {"ok": True, "encaminhado": ok}
