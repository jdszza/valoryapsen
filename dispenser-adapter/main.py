"""
APSEN - Dispenser Adapter v1.1
Bridge bidirecional entre o Computador Central e os 8 dispensers.

Fluxo entrada (← Central):
  POST /comandos/carregar   → `carregar`  nos dispensers
  POST /comandos/dispensar  → `dispensar` nos dispensers
  POST /comandos/limpar     → `limpar`    nos dispensers

Fluxo saída (← dispensers):
  POST /eventos             → normaliza e encaminha para central-computer POST /api/v1/eventos/dispenser

Os dispensers atendem por DOIS transportes, e quem escolhe é
`DISPENSER_TRANSPORTE`:

  "http"   (default) — o `dispenser-simulator`, como sempre. É o que roda no
                       Docker, no CI e em qualquer máquina sem hardware.
  "serial" — o firmware dos 8 dispensers, por UMA porta USB
             (`DISPENSER_SERIAL_URL`). Ver `docs/PROTOCOLO_SERIAL.md` e
             `serial_link.py`.

A perna de CIMA não sabe qual dos dois está embaixo: os endpoints, os modelos
Pydantic, o payload do evento e o `_post_central` são os mesmos nos dois casos.
É o que permite trocar o simulador por firmware sem tocar no central.
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

import serial_link

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [DISP-ADAPTER] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CENTRAL_URL          = os.getenv("CENTRAL_URL",          "http://central-computer:8000")
DISPENSER_SIM_URL    = os.getenv("DISPENSER_SIM_URL",    "http://dispenser-simulator:8201")
TIMEOUT_CMD          = float(os.getenv("TIMEOUT_CMD",    "15"))
TIMEOUT_EVENT        = float(os.getenv("TIMEOUT_EVENT",  "5"))

SUBSISTEMA           = "dispenser"


def _transporte() -> str:
    """Valor desconhecido cai no default COM aviso, nunca em silêncio.

    "http" é o default de propósito: a suíte, o CI e a demonstração em Docker
    não podem mudar de resultado por causa desta feature.
    """
    escolhido = os.getenv("DISPENSER_TRANSPORTE", "http").strip().lower()
    if escolhido not in ("http", "serial"):
        logger.warning("[CFG] DISPENSER_TRANSPORTE=%r desconhecido — usando 'http'.",
                       escolhido)
        return "http"
    return escolhido


TRANSPORTE           = _transporte()
SERIAL_URL           = os.getenv("DISPENSER_SERIAL_URL", "").strip()
SERIAL_BAUD          = int(os.getenv("DISPENSER_SERIAL_BAUD", "115200"))
ACK_TIMEOUT_S        = float(os.getenv("DISPENSER_ACK_TIMEOUT_S", "2"))

_client: httpx.AsyncClient | None = None
_link: "serial_link.LinkSerial | None" = None
_loop: asyncio.AbstractEventLoop | None = None


async def _wait_for_upstream(name: str, url: str, retries: int = 30, interval: float = 2.0):
    """Aguarda serviço upstream ficar disponível antes de servir tráfego."""
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
    global _client, _link, _loop
    _client = httpx.AsyncClient()
    _loop = asyncio.get_running_loop()
    logger.info("[STARTUP] httpx.AsyncClient criado | transporte=%s", TRANSPORTE)
    if TRANSPORTE == "serial":
        _link = serial_link.LinkSerial(
            subsistema=SUBSISTEMA, url=SERIAL_URL, baud=SERIAL_BAUD,
            ack_timeout_s=ACK_TIMEOUT_S, ao_receber_evento=_evento_da_placa,
        )
        _link.iniciar()
        # Sem `_wait_for_upstream`: a placa pode não estar plugada, e o adapter
        # tem que subir assim mesmo para o /ping do healthcheck responder.
        # Quem conta a verdade sobre a porta é o /health.
    else:
        await _wait_for_upstream("dispenser-simulator", DISPENSER_SIM_URL + "/ping")
    yield
    if _link is not None:
        _link.parar()
    await _client.aclose()
    logger.info("[SHUTDOWN] httpx.AsyncClient encerrado")


app = FastAPI(title="APSEN Dispenser Adapter v1.1", lifespan=lifespan)
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
    # Campo de DEMONSTRAÇÃO, ausente no caminho normal (ver
    # `central-computer/injecao.py`). O adapter não o interpreta: repassa como
    # veio, do mesmo jeito que faz com as duas quantidades da pesagem. Quem
    # decide é o central; quem executa é o simulador, num ramo isolado.
    injetar_falha: Optional[str] = None


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
        r = await _client.post(url, json=payload, timeout=timeout)
        if r.status_code >= 300:
            raise HTTPException(502, f"Simulator retornou {r.status_code}: {r.text[:200]}")
        return r.json()
    except httpx.RequestError as exc:
        raise HTTPException(503, f"Dispenser simulator indisponível: {exc}")


# ── Uma porta de saída, dois transportes ──────────────────────────────────────
#
# O NOME do comando é o mesmo nos dois caminhos — é o `cmd` da linha serial e a
# chave desta tabela —, e os campos do payload são os mesmos que o simulador já
# recebe. Renomear qualquer um deles no serial obrigaria o adapter a traduzir, e
# uma tradução é o lugar onde os dois lados divergem depois; é a mesma razão
# pela qual o evento atravessa daqui para o central sem interpretação.

_ROTAS_SIM = {
    "carregar":  "/executar/carregar",
    "dispensar": "/executar/dispensar",
    "limpar":    "/executar/limpar",
}


async def _enviar(comando: str, payload: dict) -> dict:
    """Despacha o comando pelo transporte configurado.

    O erro que sobe é o MESMO nos dois: o orquestrador não distingue firmware de
    simulador, e não deveria. Recusa da ponta de lá é 502, ponta de lá
    inalcançável é 503 — exatamente o que `_post_sim` sempre levantou.
    """
    if TRANSPORTE != "serial":
        return await _post_sim(_ROTAS_SIM[comando], payload)
    if _link is None:
        raise HTTPException(503, "Dispensers: transporte serial não iniciado")
    try:
        # `enviar_comando` BLOQUEIA até o ACK, e `Serial.write`/`read` são
        # síncronos: chamá-los no event loop congelaria o adapter inteiro,
        # inclusive o /ping que o healthcheck do compose usa como portão. É a
        # mesma regra que o central aplica ao banco ("Nada de banco no event
        # loop"), e `tests/test_serial_link.py` a cobra por AST.
        return await asyncio.to_thread(_link.enviar_comando, comando, payload)
    except serial_link.AckNegativo as exc:
        raise HTTPException(502, f"Dispensers recusaram '{comando}': {exc}")
    except serial_link.ErroLink as exc:
        raise HTTPException(503, f"Dispensers indisponíveis: {exc}")


def _resposta(resultado: dict) -> dict:
    """Corpo devolvido ao orquestrador.

    Ele só lê o status HTTP (`_post` do orquestrador devolve True/False e ignora
    o corpo), então isto é para quem lê o log — e por isso a chave diz de ONDE
    veio a confirmação. O caminho HTTP continua respondendo exatamente o que
    respondia antes desta feature.
    """
    chave = "placa" if TRANSPORTE == "serial" else "simulador"
    return {"ok": True, chave: resultado}


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
    url = CENTRAL_URL + "/api/v1/eventos/dispenser"
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
    return {"status": "ok", "service": "apsen-dispenser-adapter"}


@app.get("/health")
async def health():
    """Diagnóstico: conectividade com o central e com a ponta de baixo.

    É AQUI que o estado da porta serial aparece — nunca no /ping. O /ping diz
    que este processo está de pé, e é isso que o compose usa como portão;
    atrelá-lo ao hardware faria um cabo solto marcar o serviço como unhealthy e
    derrubar em cascata quem depende dele.
    """
    checks: dict[str, str] = {}
    alvos = [("central-computer", CENTRAL_URL + "/ping")]
    if TRANSPORTE != "serial":
        alvos.insert(0, ("dispenser-simulator", DISPENSER_SIM_URL + "/ping"))
    for name, url in alvos:
        try:
            r = await _client.get(url, timeout=3.0)
            checks[name] = "ok" if r.status_code < 300 else f"http_{r.status_code}"
        except Exception as exc:
            checks[name] = f"erro: {exc}"

    corpo = {"transporte": TRANSPORTE, "checks": checks}
    if TRANSPORTE == "serial":
        estado = _link.estado() if _link is not None else {"conectado": False}
        corpo["serial"] = estado
        checks["placa-dispenser"] = "ok" if estado.get("conectado") else "desconectada"

    ok = all(v == "ok" for v in checks.values())
    corpo["status"] = "ok" if ok else "degradado"
    return corpo


@app.post("/comandos/carregar")
async def cmd_carregar(req: ComandoCarregarReq):
    logger.info("[CMD] CARREGAR D%d ← OS %s (%s × %d)",
                req.dispenser_id, req.os_id, req.medicamento, req.quantidade)
    resultado = await _enviar(
        "carregar",
        {
            "dispenser_id": req.dispenser_id,
            "medicamento":  req.medicamento,
            "sku":          req.sku,
            "categoria":    req.categoria,
            "quantidade":   req.quantidade,
            "os_id":        req.os_id,
        },
    )
    return _resposta(resultado)


@app.post("/comandos/dispensar")
async def cmd_dispensar(req: ComandoDispensarReq):
    logger.info("[CMD] DISPENSAR D%d ← OS %s", req.dispenser_id, req.os_id)
    resultado = await _enviar(
        "dispensar",
        {"dispenser_id": req.dispenser_id, "os_id": req.os_id,
         "injetar_falha": req.injetar_falha},
    )
    return _resposta(resultado)


@app.post("/comandos/limpar")
async def cmd_limpar(req: ComandoLimparReq):
    logger.info("[CMD] LIMPAR D%d por '%s'", req.dispenser_id, req.solicitado_por)
    resultado = await _enviar(
        "limpar",
        {"dispenser_id": req.dispenser_id, "solicitado_por": req.solicitado_por},
    )
    return _resposta(resultado)


# ── Endpoint de Eventos (dispensers → Adapter → Central) ──────────────────────

async def _encaminhar_evento(req: EventoReq) -> bool:
    """Porta ÚNICA de saída do evento — os dois transportes passam por aqui.

    O payload atravessa o MESMO modelo Pydantic vindo do simulador e vindo da
    placa, e é isso que garante que o central receba de um firmware exatamente o
    byte que recebe hoje do simulador. Normalizar de um lado só abriria a
    divergência que este adapter existe para não ter — e o central não mudaria
    para acomodá-la, porque o sintoma seria um campo faltando, não um erro.
    """
    payload = req.model_dump()
    tipo    = payload.get("tipo", "?")
    disp_id = payload.get("dispenser_id", "?")
    os_id   = payload.get("os_id", "")

    log_extra = f"| OS {os_id}" if os_id else ""
    logger.info("[EVT] %-12s ← D%s %s", tipo, disp_id, log_extra)

    ok = await _post_central(payload)
    if not ok:
        logger.warning("[FWD] Evento '%s' D%s não chegou ao Central.", tipo, disp_id)
    return ok


def _evento_da_placa(payload: dict) -> None:
    """Chamado NA THREAD LEITORA do `serial_link` — nunca no event loop.

    O salto para o loop é `run_coroutine_threadsafe`: é o que deixa o
    `_post_central` (httpx async, com o retry que a suíte cobra) ser o mesmo dos
    dois transportes, em vez de ganhar uma segunda implementação síncrona.
    """
    try:
        req = EventoReq(**payload)
    except Exception as exc:  # noqa: BLE001 — ValidationError
        logger.warning("[SERIAL] evento fora do contrato, descartado: %s", exc)
        return
    loop = _loop
    if loop is None:
        logger.warning("[SERIAL] evento chegou antes do event loop — descartado.")
        return
    asyncio.run_coroutine_threadsafe(_encaminhar_evento(req), loop)


@app.post("/eventos")
async def receber_evento(req: EventoReq):
    """Recebe eventos normalizados do dispenser-simulator e os repassa ao Central."""
    ok = await _encaminhar_evento(req)
    return {"ok": True, "encaminhado": ok}
