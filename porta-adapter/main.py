"""
APSEN - Porta Adapter v2.0
Tradutor SÓ DE LEITURA entre a porta NR-12 em campo e o Computador Central.

Em campo, desde 22/10/2026, a porta são DUAS ESPs (`docs/PROTOCOLO_SERIAL.md`
§10):

    ESP da PORTA (nr12_oficial) ──Serial2──> ESP RECEPTOR ──USB (COMx)──> este adapter ──REST──> central
    RC522 + ES1 teto + ES2 frente           repassa as linhas, idênticas
    decide a tag SOZINHA

A porta fala linhas de TEXTO (`EVT:<CODIGO>[;UID=<HEX>] | <texto>` e a linha
dos fins de curso), não JSON, e a tag autoriza a abertura SOZINHA: o LED "pode
abrir" acende ao mesmo tempo em que o `EVT:PAUSA` sai. Este adapter abre a COM
do RECEPTOR, lê, e TRADUZ cada linha nos eventos do vocabulário de sempre
(`porta_texto.py`), que sobem a `POST /api/v1/eventos/porta` como antes. O
central não precisou mudar de lógica — só ganhou a flag `autorizada_pela_porta`.

**Nada desce.** Nem à porta, nem ao receptor: não há `porta_liberar`,
`porta_negar`, `porta_estado`, ping, pong, ACK nem `cmd_id`, e o leitor
(`leitor_linhas.py`) não tem método de escrita. A porta em campo é autônoma, e
um byte que descesse por engano poderia pô-la em modo registro.

**Não há heartbeat**, e isso muda o que é "link": a porta fica muda enquanto
nada acontece. Link ok = a USB do receptor aberta E o estado da porta
CONHECIDO — e o estado começa desconhecido, porque não há a quem perguntar.
Nada sobe até uma linha que o prove. Com o link ok, o `estado` é SINTETIZADO
a partir do modelo e reenviado a cada `PORTA_ESTADO_REENVIO_S`: é o que mantém
a vigia de silêncio do central satisfeita.

**O ponto cego**, aceito no protótipo: a ESP da porta travada, ou o fio
TX2→RX2 rompido, COM a porta parada, não é detectável — silêncio é o normal, e
a USB do receptor de pé não prova nada sobre a porta. A correção exige regravar
a porta (uma linha de vida periódica, ex. `EVT:VIVO`).

Roda no host (o Docker Desktop do Windows não repassa COM para container), por
`iniciar_host.bat`, e não há simulador: fingir a porta faria o central confiar
numa porta imaginária.
"""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import leitor_linhas
import porta_texto

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [PORTA-ADAPTER] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CENTRAL_URL          = os.getenv("CENTRAL_URL",   "http://central-computer:8000")
TIMEOUT_EVENT        = float(os.getenv("TIMEOUT_EVENT", "5"))

SERIAL_URL           = os.getenv("PORTA_SERIAL_URL", "").strip()
SERIAL_BAUD          = int(os.getenv("PORTA_SERIAL_BAUD", "115200"))

# O `estado` sintetizado é reenviado a cada tanto com o link ok, mesmo sem
# mudança — a porta não tem heartbeat, e é este reenvio que deixa o central
# distinguir "porta parada, tudo fechado" de "adapter morto".
PORTA_ESTADO_REENVIO_S = float(os.getenv("PORTA_ESTADO_REENVIO_S", "5"))
# Quanto o `porta_aberta` espera a linha dos fins de curso, que diz QUAL porta
# abriu. Ela sai na mesma volta do `loop()` da porta, a poucos ms.
PORTA_ESPERA_SENSORES_S = float(os.getenv("PORTA_ESPERA_SENSORES_S", "0.3"))

ROTA_EVENTOS         = "/api/v1/eventos/porta"

_client: httpx.AsyncClient | None = None
_leitor: "leitor_linhas.LeitorLinhas | None" = None
_loop: asyncio.AbstractEventLoop | None = None
_modelo = porta_texto.ModeloPorta()


def _exigir_url(url: str, variavel: str) -> None:
    """Sem porta fixada, este serviço RECUSA subir.

    Na célula montada são cinco placas e cinco processos, e uma varredura
    abriria as COMs dos outros — boot não-determinístico em que uma placa às
    vezes não é achada. Este processo existe para ler UMA porta, a do receptor,
    e sem ela não faz nada de útil.
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
    global _client, _leitor, _loop
    _exigir_url(SERIAL_URL, "PORTA_SERIAL_URL")
    _client = httpx.AsyncClient()
    _loop = asyncio.get_running_loop()
    logger.info("[STARTUP] receptor da porta em %s @ %d (só leitura) | central %s",
                SERIAL_URL, SERIAL_BAUD, CENTRAL_URL)
    _leitor = leitor_linhas.LeitorLinhas(SERIAL_URL, _linha_da_usb, baud=SERIAL_BAUD)
    _leitor.iniciar()
    logger.info("[STARTUP] estado da porta DESCONHECIDO até a primeira linha que o "
                "prove — passe a tag.")
    # Sem esperar a placa: o /ping tem de responder com o receptor fora.
    vigia = asyncio.create_task(_laco_vigia_porta())
    yield
    vigia.cancel()
    _leitor.parar()
    await _client.aclose()
    logger.info("[SHUTDOWN] httpx.AsyncClient encerrado")


app = FastAPI(title="APSEN Porta Adapter v2.0", lifespan=lifespan)
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


# ── A porta: o link é UM estado, e só a transição sobe ───────────────────────

def _porta_vazia() -> dict:
    return {
        # None = ainda não se sabe (boot). True só com a USB aberta E o estado
        # da porta conhecido.
        "link_ok": None,
        "usb_conectada": None,
        # A conexão da USB a que o que se sabe da porta pertence. Outra
        # conexão = a USB caiu e voltou, talvez mais rápido que a vigia.
        "conexao": None,
        "ultima_linha_porta_em": None,      # monotonic
        "estado_enviado_em": None,          # monotonic do último `estado` que subiu
        "eventos": 0,
        "perdas": 0,
        "linhas_receptor": 0,
        "linhas_desconhecidas": 0,
    }


_porta: dict = _porta_vazia()
# Serializa o TRATAMENTO inteiro de cada linha, e não só cada POST: o
# `porta_link_ok` tem de chegar ANTES dos eventos da linha que o provou, e o
# `estado` do REARME_OK antes do `rearme`. `asyncio.Lock` é FIFO, então a ordem
# em que as linhas chegaram do receptor é a ordem em que sobem.
_porta_lock: "asyncio.Lock | None" = None


def _ordem() -> asyncio.Lock:
    global _porta_lock
    if _porta_lock is None:
        _porta_lock = asyncio.Lock()
    return _porta_lock


def _ts_agora() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


async def _postar_porta(payload: dict) -> bool:
    """Sobe um evento. Só é chamado com `_ordem()` já tomado."""
    _porta["eventos"] += 1
    if payload.get("tipo") == "estado":
        _porta["estado_enviado_em"] = time.monotonic()
    else:
        logger.info("[PORTA] → %s %s", payload.get("tipo"),
                    {k: v for k, v in payload.items() if k not in ("origem", "tipo", "ts")})
    ok = await _post_central(payload)
    if not ok:
        logger.warning("[PORTA] evento '%s' não chegou ao Central.", payload.get("tipo"))
    return ok


async def _link_perdido_sob_ordem(motivo: str) -> None:
    """Link perdido = porta aberta, para o central. Sobe UMA vez por queda, e
    o que se sabia da porta deixa de valer."""
    _modelo.esquecer()
    _porta["estado_enviado_em"] = None
    if _porta["link_ok"] is False:
        return
    _porta["link_ok"] = False
    _porta["perdas"] += 1
    logger.warning("[PORTA] link da porta PERDIDO — %s. Estado volta a "
                   "DESCONHECIDO até uma linha que o prove (passe a tag).", motivo)
    await _postar_porta({"origem": "porta", "tipo": "porta_link_perdido",
                         "ts": _ts_agora()})


async def _conferir_conexao(numero: int) -> None:
    if _porta["conexao"] is not None and numero != _porta["conexao"]:
        await _link_perdido_sob_ordem("a USB do receptor caiu e voltou")
    _porta["conexao"] = numero


async def _tratar_linha(texto: str, agora: float, conexao: int = 0) -> None:
    """Uma linha da USB do receptor, já no event loop."""
    async with _ordem():
        await _tratar_sob_ordem(texto, agora, conexao)


async def _tratar_sob_ordem(texto: str, agora: float, conexao: int) -> None:
    await _conferir_conexao(conexao)
    linha = porta_texto.interpretar_linha(texto)
    if linha is None:
        if porta_texto.linha_do_receptor(texto):
            _porta["linhas_receptor"] += 1
        else:
            _porta["linhas_desconhecidas"] += 1
        logger.debug("[RECEPTOR] %s", texto)
        return
    if linha.tipo == "receptor_sem_dados":
        logger.warning("[RECEPTOR] %s", texto.strip())
    else:
        _porta["ultima_linha_porta_em"] = agora
        logger.info("[PORTA] %s", texto.strip())

    eventos = _modelo.aplicar(linha, agora, ts=_ts_agora())
    if _modelo.conhecido and _porta["link_ok"] is not True:
        _porta["link_ok"] = True
        logger.info("[PORTA] estado da porta CONHECIDO — link ok.")
        await _postar_porta({"origem": "porta", "tipo": "porta_link_ok",
                             "ts": _ts_agora()})
    for evento in eventos:
        await _postar_porta(evento)


def _linha_da_usb(texto: str) -> None:
    """Chamado NA THREAD LEITORA — nunca no event loop.

    O salto para o loop é `run_coroutine_threadsafe`, na ordem em que as linhas
    chegaram: é o que deixa o `_post_central` (httpx async, com o retry que a
    suíte cobra) ser o mesmo dos outros adapters. O instante e a conexão são
    capturados AQUI, na chegada, e não quando o loop chegar à linha.
    """
    loop = _loop
    if loop is None:
        logger.warning("[RECEPTOR] linha chegou antes do event loop — descartada.")
        return
    conexao = getattr(_leitor, "conexoes", 0)
    asyncio.run_coroutine_threadsafe(_tratar_linha(texto, time.monotonic(), conexao), loop)


async def _vigiar_porta_uma_vez() -> None:
    """Uma rodada da vigia: USB do receptor, `porta_aberta` sem a linha dos
    fins de curso no prazo, e o reenvio do `estado` sintetizado.

    Separada do laço para que a suíte a chame sem esperar relógio. Não há
    vigia de heartbeat: a porta não tem heartbeat.
    """
    conectada = bool(_leitor is not None and _leitor.conectado)
    _porta["usb_conectada"] = conectada
    async with _ordem():
        if not conectada:
            _porta["conexao"] = None
            await _link_perdido_sob_ordem("a USB do receptor caiu")
            return
        await _conferir_conexao(getattr(_leitor, "conexoes", 0))
        agora = time.monotonic()
        for evento in _modelo.vencer_pendente(agora, PORTA_ESPERA_SENSORES_S,
                                              ts=_ts_agora()):
            await _postar_porta(evento)
        if _porta["link_ok"] is not True:
            return
        enviado = _porta["estado_enviado_em"]
        if enviado is None or agora - enviado >= PORTA_ESTADO_REENVIO_S:
            estado = _modelo.estado_evento(_ts_agora())
            if estado is not None:
                await _postar_porta(estado)


async def _laco_vigia_porta() -> None:
    while True:
        try:
            await _vigiar_porta_uma_vez()
        except Exception:  # noqa: BLE001 — a vigia é única e não pode morrer
            logger.exception("[PORTA] falha na vigia")
        await asyncio.sleep(0.25)


# ── Comandos (Central → Adapter) — nenhum desce às ESPs ───────────────────────

_ESTADO_DESCONHECIDO = "estado da porta ainda desconhecido — passe a tag"
_PORTA_AUTONOMA = ("porta autônoma: a autorização é da própria porta e nada é "
                   "enviado às ESPs")


@app.post("/comandos/porta/estado")
async def cmd_porta_estado():
    """O central pede um `estado` (rearme recusado): ele sai do MODELO, sem
    tocar na serial — não há a quem perguntar."""
    async with _ordem():
        estado = _modelo.estado_evento(_ts_agora()) if _porta["link_ok"] is True else None
        if estado is None:
            raise HTTPException(503, _ESTADO_DESCONHECIDO)
        await _postar_porta(estado)
    return {"ok": True, "enviado_a_placa": False}


@app.post("/comandos/porta/liberar")
async def cmd_porta_liberar():
    """409, e não um ACK falso: se o central voltar a pedir para liberar, o
    erro aparece em vez de ele achar que acendeu um LED."""
    raise HTTPException(409, _PORTA_AUTONOMA)


@app.post("/comandos/porta/negar")
async def cmd_porta_negar():
    raise HTTPException(409, _PORTA_AUTONOMA)


# ── Diagnóstico ───────────────────────────────────────────────────────────────

def _estado_porta_health() -> dict:
    ultima = _porta["ultima_linha_porta_em"]
    return {
        "link_ok": _porta["link_ok"],
        "usb_conectada": _porta["usb_conectada"],
        "estado_conhecido": _modelo.conhecido,
        "modelo": _modelo.resumo(),
        "idade_ultima_linha_porta_s": None if ultima is None
        else round(time.monotonic() - ultima, 2),
        "ultimo_codigo_bancada": _modelo.ultimo_codigo_bancada,
        "receptor_sem_dados": _modelo.receptor_sem_dados,
        "linhas_desconhecidas": _porta["linhas_desconhecidas"],
        "codigos_desconhecidos": _modelo.codigos_desconhecidos,
        "linhas_receptor": _porta["linhas_receptor"],
        "eventos": _porta["eventos"],
        "perdas_de_link": _porta["perdas"],
        "reenvio_estado_s": PORTA_ESTADO_REENVIO_S,
    }


@app.get("/ping")
def ping():
    return {"status": "ok", "service": "apsen-porta-adapter"}


@app.get("/health")
async def health():
    """Diagnóstico: o central, a USB do receptor e o estado da porta — nunca
    no /ping, que diz só que este processo está de pé."""
    checks: dict[str, str] = {}
    try:
        r = await _client.get(CENTRAL_URL + "/ping", timeout=3.0)
        checks["central-computer"] = "ok" if r.status_code < 300 else f"http_{r.status_code}"
    except Exception as exc:
        checks["central-computer"] = f"erro: {exc}"

    serial = _leitor.estado() if _leitor is not None else {"conectado": False}
    checks["receptor-usb"] = "ok" if serial.get("conectado") else "desconectada"
    checks["estado-porta"] = ("conhecido" if _modelo.conhecido
                              else "desconhecido — passe a tag")
    ok = (checks["central-computer"] == "ok" and checks["receptor-usb"] == "ok"
          and _modelo.conhecido)
    return {"status": "ok" if ok else "degradado", "checks": checks,
            "serial": serial, "porta": _estado_porta_health()}
