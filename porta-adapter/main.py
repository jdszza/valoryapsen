"""
APSEN - Porta Adapter v1.0
Bridge bidirecional entre o Computador Central e a porta NR-12.

Fluxo entrada (← Central):
  POST /comandos/porta/liberar  → `porta_liberar` na placa da porta
  POST /comandos/porta/negar    → `porta_negar`
  POST /comandos/porta/estado   → `porta_estado`

Fluxo saída (← porta):
  eventos da placa              → POST /api/v1/eventos/porta no central

A placa da porta (RC522 + fins de curso ES1 teto / ES2 frente + LED "pode
abrir") tem USB PRÓPRIA no mini PC, e este processo é o dono dela
(`docs/PROTOCOLO_SERIAL.md` §9). Ela já falou por UART com a placa das 8 telas
TFT, que repassava tudo ao dispenser-adapter; o desenho saiu, e a lógica que
morava lá veio para cá sem mudar de comportamento. A razão é de segurança:
travar ou regravar a placa das telas cegava a vigia da porta — e link perdido
é porta aberta, a célula inteira parada por causa de uma tela. Com um processo
e uma COM só dela, a porta tem o seu `/health` e cai sozinha.

Só há UM transporte, o serial. Os outros adapters têm `<SUB>_TRANSPORTE=http`
como default porque têm simulador; a porta não tem — fingir um ACK aqui faria o
central autorizar uma porta imaginária. Por isso este serviço não está no
compose: ele roda no host, como os outros adapters com placa (o Docker
Desktop do Windows não repassa COM para container), por `iniciar_host.bat`.

O adapter traduz TRANSPORTE, não decide nada da porta: o conteúdo atravessa
cru, na ordem em que chegou. O que ele soma é o LINK — a USB da porta e a
idade do último heartbeat viram UM estado, e só a transição sobe.
"""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

import serial_link

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [PORTA-ADAPTER] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CENTRAL_URL          = os.getenv("CENTRAL_URL",   "http://central-computer:8000")
TIMEOUT_EVENT        = float(os.getenv("TIMEOUT_EVENT", "5"))

SUBSISTEMA           = "porta"
SERIAL_URL           = os.getenv("PORTA_SERIAL_URL", "").strip()
SERIAL_BAUD          = int(os.getenv("PORTA_SERIAL_BAUD", "115200"))
ACK_TIMEOUT_S        = float(os.getenv("PORTA_ACK_TIMEOUT_S", "2"))

# Heartbeat da porta mais velho que isto = link perdido, mesmo com a USB de pé:
# a placa travou e parou de falar. São cinco heartbeats de 500 ms — folga para
# um atraso de escalonamento do host sem virar parada da célula por ruído.
PORTA_HEARTBEAT_MAX_S = float(os.getenv("PORTA_HEARTBEAT_MAX_S", "2.5"))
# O heartbeat chega a cada 500 ms e não sobe a cada 500 ms: sobe na MUDANÇA e,
# sem mudança, no máximo a cada tanto — como sinal de vida. É ele que deixa o
# central distinguir "porta parada, tudo fechado" de "adapter morto".
PORTA_ESTADO_REENVIO_S = float(os.getenv("PORTA_ESTADO_REENVIO_S", "5"))

ROTA_EVENTOS         = "/api/v1/eventos/porta"

_client: httpx.AsyncClient | None = None
_link: "serial_link.LinkSerial | None" = None
_loop: asyncio.AbstractEventLoop | None = None


def _exigir_url(url: str, variavel: str) -> None:
    """Sem porta fixada, este serviço RECUSA subir.

    Com a URL vazia o `serial_link` varre TODAS as portas, e cada sondagem abre
    a porta por até `PROBE_ASSENTAR_S + PROBE_ESPERA_S` (~9,5 s). Na célula
    montada são cinco placas e cinco processos: enquanto um deles segura a COM
    da CNC para conferir, o cnc-adapter toma `ACCESS_DENIED` na própria — boot
    não-determinístico em que uma placa às vezes não é achada, e o log de cada
    processo mostra só a metade dele.

    Recusar é a resposta certa AQUI, e não um aviso: este processo existe para
    falar com UMA porta, e sem ela não faz nada de útil. E, ao contrário dos
    outros adapters, não há transporte http para cair: a varredura nem é uma
    opção, porque a porta é a única placa deste serviço e ela tem COM fixa.
    """
    if not url:
        raise RuntimeError(
            f"{variavel} está vazia. Com cinco placas na célula, a varredura "
            f"automática faz os processos disputarem as portas uns dos outros — "
            f"fixe a COM no Windows e defina {variavel} (ex.: COM4, /dev/ttyUSB0 "
            f"ou rfc2217://host:porta)."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _link, _loop
    _exigir_url(SERIAL_URL, "PORTA_SERIAL_URL")
    _client = httpx.AsyncClient()
    _loop = asyncio.get_running_loop()
    logger.info("[STARTUP] porta serial %s @ %d | central %s",
                SERIAL_URL, SERIAL_BAUD, CENTRAL_URL)
    _link = serial_link.LinkSerial(
        subsistema=SUBSISTEMA, url=SERIAL_URL, baud=SERIAL_BAUD,
        ack_timeout_s=ACK_TIMEOUT_S, ao_receber_evento=_evento_da_placa,
    )
    _link.iniciar()
    # Sem esperar a placa: ela pode não estar plugada, e o adapter tem que
    # subir assim mesmo para o /ping responder. Quem conta a verdade sobre a
    # porta é o /health — e, para o central, a vigia abaixo.
    vigia = asyncio.create_task(_laco_vigia_porta())
    yield
    vigia.cancel()
    _link.parar()
    await _client.aclose()
    logger.info("[SHUTDOWN] httpx.AsyncClient encerrado")


app = FastAPI(title="APSEN Porta Adapter v1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Encaminhamento do evento ao Central ───────────────────────────────────────
#
# A mesma política dos outros quatro adapters, e por isso o mesmo critério: o
# evento é o ÚNICO caminho de volta ao orquestrador, e um `porta_aberta` perdido
# é uma pessoa com a mão na célula sem que o central saiba. Retenta em falha de
# rede, timeout e 5xx — as três em que tentar de novo pode dar outro resultado.
# 4xx é recusa determinística; insistir só encheria o log com a mesma linha
# três vezes. `tests/test_adapters.py` compara os cinco entre si.
_TENTATIVAS_EVENTO = 3
_ESPERA_ENTRE_TENTATIVAS_S = 1.0


def _vale_retentar(status: int) -> bool:
    """5xx é o lado de lá falhando; 408/429 é ele pedindo para esperar."""
    return status >= 500 or status in (408, 429)


async def _post_central(payload: dict) -> bool:
    """Encaminha evento ao Central, com retry. Nunca lança — devolve False."""
    url = CENTRAL_URL + ROTA_EVENTOS
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


# ── A porta NR-12: o link é UM estado, e só a transição sobe ─────────────────

def _porta_vazia() -> dict:
    return {
        # None = ainda não se sabe (boot). Só `True` depois do primeiro `estado`.
        "link_ok": None,
        "ultimo_estado": None,
        "ultimo_estado_em": None,       # monotonic
        "ultimo_estado_ts": None,       # o `ts` da placa, para o /health
        "encaminhado_em": None,         # monotonic do último `estado` que subiu
        "assinatura_encaminhada": None,
        "usb_conectada": None,
        "desde": time.monotonic(),
        "eventos": 0,
        "perdas": 0,
    }


_porta: dict = _porta_vazia()
# Serializa o TRATAMENTO inteiro de cada evento, e não só cada POST: o
# `porta_link_ok` tem de chegar ANTES do `estado` que o provou, e nenhum evento
# seguinte pode se intercalar entre os dois. Dois `run_coroutine_threadsafe`
# seguidos se cruzariam no `await` do POST; `asyncio.Lock` é FIFO, então a
# ordem em que as linhas chegaram da placa é a ordem em que sobem. Criado no
# loop, na primeira vez.
_porta_lock: "asyncio.Lock | None" = None


def _ordem() -> asyncio.Lock:
    global _porta_lock
    if _porta_lock is None:
        _porta_lock = asyncio.Lock()
    return _porta_lock


def _ts_agora() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _assinatura(payload: dict) -> str:
    return json.dumps({k: v for k, v in payload.items() if k != "ts"},
                      sort_keys=True, ensure_ascii=False, default=str)


async def _postar_porta(payload: dict) -> bool:
    """Sobe um evento. Só é chamado com `_ordem()` já tomado."""
    ok = await _post_central(payload)
    if not ok:
        logger.warning("[PORTA] evento '%s' não chegou ao Central.", payload.get("tipo"))
    return ok


async def _porta_link_perdido(motivo: str) -> None:
    """Link perdido = porta aberta, para o central. Sobe UMA vez por queda."""
    async with _ordem():
        await _link_perdido_sob_ordem(motivo)


async def _link_perdido_sob_ordem(motivo: str) -> None:
    if _porta["link_ok"] is False:
        return
    _porta["link_ok"] = False
    _porta["perdas"] += 1
    _porta["assinatura_encaminhada"] = None   # o próximo `estado` sobe sempre
    logger.warning("[PORTA] link da porta PERDIDO — %s.", motivo)
    await _postar_porta({"origem": "porta", "tipo": "porta_link_perdido",
                         "ts": _ts_agora()})


async def _tratar_evento_porta(payload: dict) -> None:
    """Um evento da placa da porta, já no event loop.

    O conteúdo sobe CRU. As duas únicas decisões são de transporte: o link e o
    heartbeat repetido.
    """
    async with _ordem():
        await _tratar_sob_ordem(payload)


async def _tratar_sob_ordem(payload: dict) -> None:
    _porta["eventos"] += 1
    tipo = payload.get("tipo")

    if tipo in ("porta_link_perdido", "porta_link_ok"):
        # Os dois são DESTE adapter. Vindos da placa, são de um firmware antigo
        # (ou da placa das telas, que repassava a porta): o link é decidido
        # aqui, e subir a opinião dela duplicaria a transição no central.
        logger.warning("[PORTA] '%s' vindo da placa ignorado — a vigia do link "
                       "é deste adapter.", tipo)
        return

    if tipo == "estado":
        agora = time.monotonic()
        _porta["ultimo_estado"] = payload
        _porta["ultimo_estado_em"] = agora
        _porta["ultimo_estado_ts"] = payload.get("ts")
        if _porta["link_ok"] is not True:
            _porta["link_ok"] = True
            logger.info("[PORTA] link da porta OK.")
            await _postar_porta({"origem": "porta", "tipo": "porta_link_ok",
                                 "ts": _ts_agora()})
        assinatura = _assinatura(payload)
        repetido = assinatura == _porta["assinatura_encaminhada"]
        recente = (_porta["encaminhado_em"] is not None
                   and agora - _porta["encaminhado_em"] < PORTA_ESTADO_REENVIO_S)
        if repetido and recente:
            return
        _porta["assinatura_encaminhada"] = assinatura
        _porta["encaminhado_em"] = agora

    logger.info("[PORTA] %s %s", tipo,
                {k: v for k, v in payload.items() if k not in ("origem", "tipo", "ts")})
    await _postar_porta(payload)


def _evento_da_placa(payload: dict) -> None:
    """Chamado NA THREAD LEITORA do `serial_link` — nunca no event loop.

    O salto para o loop é `run_coroutine_threadsafe`, na ordem em que as linhas
    chegaram: é o que deixa o `_post_central` (httpx async, com o retry que a
    suíte cobra) ser o mesmo dos outros adapters, em vez de ganhar uma segunda
    implementação síncrona.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("tipo"), str):
        logger.warning("[SERIAL] evento fora do contrato, descartado: %r", payload)
        return
    loop = _loop
    if loop is None:
        logger.warning("[SERIAL] evento chegou antes do event loop — descartado.")
        return
    asyncio.run_coroutine_threadsafe(_tratar_evento_porta(payload), loop)


async def _vigiar_porta_uma_vez() -> None:
    """Uma rodada da vigia: USB da porta e idade do heartbeat.

    Separada do laço para que a suíte a chame sem esperar relógio. Ninguém do
    outro lado do cabo avisa que caiu — quem perdeu o link não tem como dizer
    que o perdeu —, então quem vigia é este lado.
    """
    conectada = bool(_link is not None and _link.conectado)
    antes = _porta["usb_conectada"]
    _porta["usb_conectada"] = conectada

    if not conectada:
        await _porta_link_perdido("a USB da porta caiu")
        return
    if antes is False:
        # A USB voltou: pede um `estado` na hora em vez de esperar o próximo
        # heartbeat. O `porta_link_ok` só sobe com o `estado` que chegar.
        logger.info("[PORTA] USB da porta de volta — pedindo `porta_estado`.")
        try:
            await asyncio.to_thread(_link.enviar_comando, "porta_estado", {})
        except serial_link.ErroLink as exc:
            logger.warning("[PORTA] porta_estado na reconexão falhou: %s", exc)

    referencia = _porta["ultimo_estado_em"] or _porta["desde"]
    if _porta["link_ok"] is not False and time.monotonic() - referencia > PORTA_HEARTBEAT_MAX_S:
        await _porta_link_perdido(
            f"nenhum `estado` da porta há mais de {PORTA_HEARTBEAT_MAX_S:.1f}s")


async def _laco_vigia_porta() -> None:
    while True:
        try:
            await _vigiar_porta_uma_vez()
        except Exception:  # noqa: BLE001 — a vigia é única e não pode morrer
            logger.exception("[PORTA] falha na vigia")
        await asyncio.sleep(0.25)


# ── Comandos (Central → Adapter → porta) ──────────────────────────────────────

# Os três comandos da porta. O NOME é o `cmd` da linha serial, e o mesmo do §9
# — nada é traduzido no caminho.
_COMANDOS_PORTA = {
    "porta_liberar": "/comandos/porta/liberar",
    "porta_negar":   "/comandos/porta/negar",
    "porta_estado":  "/comandos/porta/estado",
}


async def _enviar_porta(comando: str, payload: dict) -> dict:
    """Comando à porta. Recusa da placa é 502, placa inalcançável é 503 — os
    mesmos de todo adapter: o orquestrador não distingue um do outro."""
    if _link is None:
        raise HTTPException(503, "Porta NR-12: transporte serial não iniciado")
    try:
        # `enviar_comando` BLOQUEIA até o ACK, e `Serial.write`/`read` são
        # síncronos: chamá-los no event loop congelaria o adapter inteiro,
        # inclusive o /ping. `tests/test_porta_adapter.py` cobra por AST.
        return await asyncio.to_thread(_link.enviar_comando, comando, payload)
    except serial_link.AckNegativo as exc:
        raise HTTPException(502, f"Porta recusou '{comando}': {exc}")
    except serial_link.ErroLink as exc:
        raise HTTPException(503, f"Porta indisponível: {exc}")


class PortaLiberarReq(BaseModel):
    janela_ms: int

    @field_validator("janela_ms")
    @classmethod
    def _faixa(cls, v: int) -> int:
        # A placa limita 1..60 s; recusar aqui é 422 com o nome do campo, em
        # vez de a placa corrigir em silêncio um número que alguém digitou.
        if not 1000 <= v <= 60000:
            raise ValueError("janela_ms deve ser 1000-60000")
        return v


class PortaNegarReq(BaseModel):
    motivo: str


@app.post("/comandos/porta/liberar")
async def cmd_porta_liberar(req: PortaLiberarReq):
    logger.info("[CMD] PORTA LIBERAR janela=%d ms", req.janela_ms)
    resultado = await _enviar_porta("porta_liberar", {"janela_ms": req.janela_ms})
    return {"ok": True, "porta": resultado}


@app.post("/comandos/porta/negar")
async def cmd_porta_negar(req: PortaNegarReq):
    logger.info("[CMD] PORTA NEGAR motivo=%s", req.motivo)
    # 60 é o que cabe no `MAX_MOTIVO_LEN` (64) do firmware, com folga.
    resultado = await _enviar_porta("porta_negar", {"motivo": req.motivo[:60]})
    return {"ok": True, "porta": resultado}


@app.post("/comandos/porta/estado")
async def cmd_porta_estado():
    resultado = await _enviar_porta("porta_estado", {})
    return {"ok": True, "porta": resultado}


# ── Diagnóstico ───────────────────────────────────────────────────────────────

def _estado_porta_health() -> dict:
    ultimo = _porta["ultimo_estado_em"]
    return {
        "link_ok": _porta["link_ok"],
        "usb_conectada": _porta["usb_conectada"],
        "ultimo_heartbeat_ts": _porta["ultimo_estado_ts"],
        "idade_heartbeat_s": None if ultimo is None else round(time.monotonic() - ultimo, 2),
        "heartbeat_max_s": PORTA_HEARTBEAT_MAX_S,
        "ultimo_estado": _porta["ultimo_estado"],
        "eventos": _porta["eventos"],
        "perdas_de_link": _porta["perdas"],
    }


@app.get("/ping")
def ping():
    return {"status": "ok", "service": "apsen-porta-adapter"}


@app.get("/health")
async def health():
    """Diagnóstico: o central e a porta — nunca no /ping.

    O /ping diz que este processo está de pé, e é o que o pré-voo sonda;
    atrelá-lo ao hardware faria um cabo solto parecer um processo morto, e
    "inicie o .bat" seria a ação errada para um cabo solto.
    """
    checks: dict[str, str] = {}
    try:
        r = await _client.get(CENTRAL_URL + "/ping", timeout=3.0)
        checks["central-computer"] = "ok" if r.status_code < 300 else f"http_{r.status_code}"
    except Exception as exc:
        checks["central-computer"] = f"erro: {exc}"

    estado = _link.estado() if _link is not None else {"conectado": False}
    checks["placa-porta"] = "ok" if estado.get("conectado") else "desconectada"
    porta = _estado_porta_health()
    checks["link-porta"] = "ok" if porta["link_ok"] is True else "perdido"

    ok = all(v == "ok" for v in checks.values())
    return {"status": "ok" if ok else "degradado", "checks": checks,
            "serial": estado, "porta": porta}
