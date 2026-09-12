"""
APSEN - Weight Adapter v1.0
Bridge bidirecional entre o Computador Central e o Weight Simulator (HX711).

Fluxo entrada (← Central):
  POST /comandos/tara     → repassa ao weight-simulator POST /executar/tara
  POST /comandos/pesar    → repassa ao weight-simulator POST /executar/pesar
                            (leva `quantidade_esperada` E `quantidade_real`;
                             ver docstring de `cmd_pesar`)

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
    quantidade_esperada: int               # alvo da OS — base do peso ESPERADO
    quantidade_real: Optional[int] = None  # o que o dispenser soltou — vira peso na mesa
    peso_unitario_g: float
    # Campo de DEMONSTRAÇÃO, ausente no caminho normal (ver
    # `central-computer/injecao.py`). O adapter não o interpreta: repassa como
    # veio, do mesmo jeito que faz com as duas quantidades da pesagem. Quem
    # decide é o central; quem executa é o simulador, num ramo isolado.
    injetar_falha: Optional[str] = None


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


# ── Encaminhamento do evento ao Central ───────────────────────────────────────
#
# Este é o ÚNICO caminho de volta ao orquestrador. Ele postava uma vez e, em
# falha, só logava — e quem paga por um evento perdido não é o adapter: é a OS.
# O orquestrador fica bloqueado em `aguardar_evento` esperando o `dispensado`
# do slot, estoura `TIMEOUT_DISPENSA` e aborta uma OS cujo hardware fez tudo
# certo. O caminho de IDA (orquestrador → adapter) sempre retentou 3x; era só a
# volta que não.
#
# A política é a mesma do `_post` do orquestrador, e por isso o critério
# também: retenta em falha de rede, timeout e 5xx — as três em que tentar de
# novo pode dar outro resultado. 4xx é recusa determinística (um 422 é payload
# que não passa na validação, e não passará na terceira tentativa); insistir só
# encheria o log com a mesma linha três vezes.
#
# O custo é o tempo de resposta AO SIMULADOR: com o central fora do ar, este
# handler pode segurar a requisição por até
# `_TENTATIVAS_EVENTO * TIMEOUT_EVENT + 2 * _ESPERA_ENTRE_TENTATIVAS_S`. O
# simulador desiste antes (o `requests.post` dele tem timeout de 5s) e o
# encaminhamento segue até o fim assim mesmo — o que é exatamente o desejado:
# quem precisa do evento é o orquestrador, e o simulador ignora o retorno
# (`_evento` é fire-and-forget).
_TENTATIVAS_EVENTO = 3
_ESPERA_ENTRE_TENTATIVAS_S = 1.0


def _vale_retentar(status: int) -> bool:
    """5xx é o lado de lá falhando; 408/429 é ele pedindo para esperar."""
    return status >= 500 or status in (408, 429)


async def _post_central(payload: dict) -> bool:
    """Encaminha evento ao Central, com retry. Nunca lança — devolve False.

    False significa "o evento NÃO chegou depois de `_TENTATIVAS_EVENTO`
    tentativas", e quem chama já registra isso no log. Não aborta o fluxo do
    adapter: ele não tem o que fazer com a informação, e derrubar a resposta ao
    simulador não traria o evento de volta.
    """
    url = CENTRAL_URL + "/api/v1/eventos/peso"
    for tentativa in range(_TENTATIVAS_EVENTO):
        try:
            r = await _client.post(url, json=payload, timeout=TIMEOUT_EVENT)
            if r.status_code < 300:
                return True
            if not _vale_retentar(r.status_code):
                logger.warning("[FWD] Central recusou o evento (status %d) — "
                               "recusa definitiva, sem retry.", r.status_code)
                return False
            logger.warning("[FWD] Central respondeu %d (tentativa %d/%d).",
                           r.status_code, tentativa + 1, _TENTATIVAS_EVENTO)
        except Exception as exc:
            logger.warning("[FWD] Falha ao encaminhar evento ao Central "
                           "(tentativa %d/%d): %s", tentativa + 1, _TENTATIVAS_EVENTO, exc)
        if tentativa < _TENTATIVAS_EVENTO - 1:
            await asyncio.sleep(_ESPERA_ENTRE_TENTATIVAS_S)
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
    """Solicita leitura de peso após dispensa no slot especificado.

    As duas quantidades atravessam o adapter sem interpretação: o simulador
    incrementa a mesa pela REAL e mede o desvio contra a ESPERADA. Repassar só
    a esperada — o contrato antigo — fazia a balança comparar o valor consigo
    mesma, cega a qualquer falha de dispensa.

    `quantidade_real` ausente cai na esperada, preservando o contrato antigo.
    """
    quantidade_real = (
        req.quantidade_esperada if req.quantidade_real is None else req.quantidade_real
    )
    logger.info(
        "[CMD] PESAR slot=%d | qtd esperada=%d real=%d | peso_unit=%.1fg | OS=%s",
        req.slot_id, req.quantidade_esperada, quantidade_real,
        req.peso_unitario_g, req.os_id,
    )
    resultado = await _post_sim(
        "/executar/pesar",
        {
            "os_id":               req.os_id,
            "slot_id":             req.slot_id,
            "quantidade_esperada": req.quantidade_esperada,
            "quantidade_real":     quantidade_real,
            "peso_unitario_g":     req.peso_unitario_g,
            "injetar_falha":       req.injetar_falha,
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
