"""
APSEN - CNC Adapter v1.1
Bridge bidirecional entre o Computador Central e a mesa CNC.

Fluxo entrada (← Central):
  POST /comandos/mover    → `mover`  na mesa CNC
  POST /comandos/homing   → `homing` na mesa CNC

Fluxo saída (← mesa CNC):
  POST /eventos           → normaliza e encaminha para central-computer POST /api/v1/eventos/cnc

A mesa CNC atende por DOIS transportes, e quem escolhe é `CNC_TRANSPORTE`:

  "http"   (default) — o `cnc-simulator`, como sempre. É o que roda no Docker,
                       no CI e em qualquer máquina sem hardware.
  "serial" — o firmware da mesa, por uma porta USB (`CNC_SERIAL_URL`). Ver
             `docs/PROTOCOLO_SERIAL.md` e `serial_link.py`.

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
from pydantic import BaseModel, ConfigDict, field_validator

import serial_link

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [CNC-ADAPTER] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CENTRAL_URL      = os.getenv("CENTRAL_URL",      "http://central-computer:8000")
CNC_SIM_URL      = os.getenv("CNC_SIM_URL",      "http://cnc-simulator:8200")
TIMEOUT_CMD      = float(os.getenv("TIMEOUT_CMD",   "15"))
TIMEOUT_EVENT    = float(os.getenv("TIMEOUT_EVENT", "5"))
# Quantos dispensers a célula tem. Declarado UMA vez no compose
# (`${NUM_SLOTS:-8}`) e lido por todos: nenhuma faixa escrita à mão, e nenhuma
# mensagem de erro com um "1-6" desatualizado mandando quem lê o log procurar
# o problema na faixa errada.
NUM_SLOTS        = int(os.getenv("NUM_SLOTS", "8"))

SUBSISTEMA       = "cnc"


def _transporte() -> str:
    """Valor desconhecido cai no default COM aviso, nunca em silêncio.

    "http" é o default de propósito: a suíte, o CI e a demonstração em Docker
    não podem mudar de resultado por causa desta feature.
    """
    escolhido = os.getenv("CNC_TRANSPORTE", "http").strip().lower()
    if escolhido not in ("http", "serial"):
        logger.warning("[CFG] CNC_TRANSPORTE=%r desconhecido — usando 'http'.",
                       escolhido)
        return "http"
    return escolhido


TRANSPORTE       = _transporte()
SERIAL_URL       = os.getenv("CNC_SERIAL_URL", "").strip()
SERIAL_BAUD      = int(os.getenv("CNC_SERIAL_BAUD", "115200"))
ACK_TIMEOUT_S    = float(os.getenv("CNC_ACK_TIMEOUT_S", "2"))

_client: httpx.AsyncClient | None = None
_link: "serial_link.LinkSerial | None" = None
_loop: asyncio.AbstractEventLoop | None = None


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


def _exigir_url(transporte: str, url: str, variavel: str) -> None:
    """Transporte serial ligado sem porta fixada RECUSA subir.

    Com a URL vazia o `serial_link` varre TODAS as portas, e cada sondagem
    abre a porta por até `PROBE_ASSENTAR_S + PROBE_ESPERA_S` (~9,5 s). Na
    célula montada são cinco placas e cinco processos: enquanto um deles
    segura a COM da CNC para conferir, o cnc-adapter toma `ACCESS_DENIED` na
    própria — boot não-determinístico em que uma placa às vezes não é achada,
    e o log de cada processo mostra só a metade dele.

    Recusar é a resposta certa AQUI, e não um aviso: este processo existe para
    falar com UMA porta, e sem ela não faz nada de útil. Serviço que não sobe
    trava, por `depends_on`, quem espera por ele — e é justamente o que se quer
    quando a bancada está mal configurada, em vez de uma OS que morre por
    timeout num slot íntegro.

    A varredura continua existindo e continua sendo o caminho de quem tem UMA
    placa na mesa: basta não ligar o transporte serial de mais nada.
    """
    if transporte == "serial" and not url:
        raise RuntimeError(
            f"{variavel} está vazia com o transporte serial ligado. Com cinco "
            f"placas na célula, a varredura automática faz os processos "
            f"disputarem as portas uns dos outros — fixe a COM no Windows e "
            f"defina {variavel} (ex.: COM4, /dev/ttyUSB0 ou rfc2217://host:porta)."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, _link, _loop
    _client = httpx.AsyncClient()
    _loop = asyncio.get_running_loop()
    logger.info("[STARTUP] httpx.AsyncClient criado | transporte=%s", TRANSPORTE)
    _exigir_url(TRANSPORTE, SERIAL_URL, "CNC_SERIAL_URL")
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
        await _wait_for_upstream("cnc-simulator", CNC_SIM_URL + "/ping")
    yield
    if _link is not None:
        _link.parar()
    await _client.aclose()
    logger.info("[SHUTDOWN] httpx.AsyncClient encerrado")


app = FastAPI(title="APSEN CNC Adapter v1.1", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Pydantic Models ────────────────────────────────────────────────────────────

class ComandoMoverReq(BaseModel):
    """A mesa não recebe coordenadas — recebe QUAL receita e QUAL parada.

    `posicao_x`/`posicao_y` saíram: o roteiro é MEDIDO na bancada e gravado na
    placa, waypoint a waypoint, indexado pelo dispenser. Em troca, a placa
    REPORTA a posição alcançada no evento `posicionado`, e é ela que o central
    registra. O waypoint é por dispenser e não por posição na rota porque a
    rota é decidida pelo orquestrador em tempo de execução e muda quando um
    slot sai da OS.
    """
    dispenser_alvo: int
    os_id: str
    receita: str
    ciclo_atual: int = 0
    total_ciclos: int = 0
    # ACEITAS e IGNORADAS, e a assimetria é de propósito. Aceitar custa nada e
    # evita 422 num chamador que ainda as mande; RELAYÁ-LAS custaria o que esta
    # frente acabou de tirar do caminho — um par de coordenadas na linha serial
    # que o firmware tem de ignorar. "Tem de ignorar" é exatamente como dois
    # lados passam a discordar em silêncio: bastaria um dia alguém preferir o
    # número que veio ao waypoint medido, e a mesa iria para onde o central acha
    # que o slot está. A posição atravessa na direção contrária — no evento
    # `posicionado`, medida pela máquina.
    posicao_x: float | None = None
    posicao_y: float | None = None

    @field_validator("receita")
    @classmethod
    def _receita_valida(cls, v: str) -> str:
        # A faixa A–J é a da NVS da placa (dez slots, uma por ordem padrão).
        # Recusar aqui é 422 na hora; deixar passar custaria a ida à placa para
        # voltar com `receita_desconhecida` — o mesmo "não", mais caro.
        v = (v or "").strip().upper()
        if len(v) != 1 or not "A" <= v <= "J":
            raise ValueError("receita deve ser uma letra de A a J")
        return v

    @field_validator("dispenser_alvo")
    @classmethod
    def _dispenser_na_faixa(cls, v: int) -> int:
        if not 1 <= v <= NUM_SLOTS:
            raise ValueError(f"dispenser_alvo deve ser 1-{NUM_SLOTS}")
        return v


class ComandoHomingReq(BaseModel):
    # O HOME da máquina é o zero que o homing dela estabelece contra os fins de
    # curso; o central não o dita mais. As coordenadas ficam OPCIONAIS e não
    # obrigatórias: recusar um chamador que ainda as mande seria quebrar sem
    # ganho, e o simulador tem o próprio default para quando não vêm.
    os_id: str
    posicao_x: float | None = None
    posicao_y: float | None = None


class EstadoCelulaReq(BaseModel):
    """O que a MESA precisa saber da célula: há trava, e de qual slot.

    Mesmo modelo, mesmos nomes de campo e mesmo teto do `estado_celula` das
    telas TFT — tradução nenhuma. O central manda um payload só para as duas
    placas, e traduzir de um lado é onde os dois passam a divergir depois.
    """
    trava_ativa: bool
    trava_slot_id: Optional[int] = None
    os_id: str = ""
    trava_resumo: str = ""


class EventoReq(BaseModel):
    tipo: str
    os_id: Optional[str] = None
    dispenser_alvo: Optional[int] = None
    model_config = ConfigDict(extra="allow")


# ── Helper HTTP ────────────────────────────────────────────────────────────────

async def _post_sim(path: str, payload: dict, timeout: float = TIMEOUT_CMD) -> dict:
    url = CNC_SIM_URL + path
    try:
        r = await _client.post(url, json=payload, timeout=timeout)
        if r.status_code >= 300:
            raise HTTPException(502, f"CNC simulator retornou {r.status_code}: {r.text[:200]}")
        return r.json()
    except httpx.RequestError as exc:
        raise HTTPException(503, f"CNC simulator indisponível: {exc}")


# ── Uma porta de saída, dois transportes ──────────────────────────────────────
#
# O NOME do comando é o mesmo nos dois caminhos — é o `cmd` da linha serial e a
# chave desta tabela —, e os campos do payload são os mesmos que o simulador já
# recebe. Renomear qualquer um deles no serial obrigaria o adapter a traduzir, e
# uma tradução é o lugar onde os dois lados divergem depois; é a mesma razão
# pela qual o evento atravessa daqui para o central sem interpretação.

_ROTAS_SIM = {
    "mover":         "/executar/mover",
    "homing":        "/executar/homing",
    "estado_celula": "/executar/estado-celula",
}

# Teto do resumo que vai para a placa, igual ao do dispenser-adapter. O motivo
# formatado do central passa de 240 caracteres e NÃO viaja: mandá-lo acoplaria
# o formato de mensagem do central ao que a mesa consegue registrar, e criaria
# um segundo ponto de truncamento para algo cosmético.
TRAVA_RESUMO_MAX = 48


async def _enviar(comando: str, payload: dict) -> dict:
    """Despacha o comando pelo transporte configurado.

    O erro que sobe é o MESMO nos dois: o orquestrador não distingue firmware de
    simulador, e não deveria. Recusa da ponta de lá é 502, ponta de lá
    inalcançável é 503 — exatamente o que `_post_sim` sempre levantou.
    """
    if TRANSPORTE != "serial":
        return await _post_sim(_ROTAS_SIM[comando], payload)
    if _link is None:
        raise HTTPException(503, "Mesa CNC: transporte serial não iniciado")
    try:
        # `enviar_comando` BLOQUEIA até o ACK, e `Serial.write`/`read` são
        # síncronos: chamá-los no event loop congelaria o adapter inteiro,
        # inclusive o /ping que o healthcheck do compose usa como portão. É a
        # mesma regra que o central aplica ao banco ("Nada de banco no event
        # loop"), e `tests/test_serial_link.py` a cobra por AST.
        return await asyncio.to_thread(_link.enviar_comando, comando, payload)
    except serial_link.AckNegativo as exc:
        raise HTTPException(502, f"Mesa CNC recusou '{comando}': {exc}")
    except serial_link.ErroLink as exc:
        raise HTTPException(503, f"Mesa CNC indisponível: {exc}")


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
    url = CENTRAL_URL + "/api/v1/eventos/cnc"
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
    return {"status": "ok", "service": "apsen-cnc-adapter"}


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
        alvos.insert(0, ("cnc-simulator", CNC_SIM_URL + "/ping"))
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
        checks["placa-cnc"] = "ok" if estado.get("conectado") else "desconectada"

    ok = all(v == "ok" for v in checks.values())
    corpo["status"] = "ok" if ok else "degradado"
    return corpo


@app.post("/comandos/mover")
async def cmd_mover(req: ComandoMoverReq):
    logger.info("[CMD] MOVER → D%d receita %s ciclo %d/%d OS %s",
                req.dispenser_alvo, req.receita,
                req.ciclo_atual, req.total_ciclos, req.os_id)
    resultado = await _enviar(
        "mover",
        {
            "dispenser_alvo": req.dispenser_alvo,
            "os_id":          req.os_id,
            "receita":        req.receita,
            "ciclo_atual":    req.ciclo_atual,
            "total_ciclos":   req.total_ciclos,
        },
    )
    return _resposta(resultado)


@app.post("/comandos/estado-celula")
async def cmd_estado_celula(req: EstadoCelulaReq):
    """A trava do Triple Check chegando à mesa.

    Ela é a peça fisicamente sobre a bancada onde o supervisor vai mexer — e,
    sob o ciclo por relógio, é também a peça que continua andando sozinha se
    ninguém a avisar. Com a trava ativa a mesa interrompe o movimento, volta ao
    HOME e passa a RECUSAR `mover` até a liberação.

    Espelha `cmd_estado_celula` do dispenser-adapter: mesmo modelo, mesmos
    campos, nenhuma tradução.
    """
    resumo = " ".join(str(req.trava_resumo or "").split())[:TRAVA_RESUMO_MAX]
    logger.info("[CMD] ESTADO-CELULA trava=%s slot=%s os=%s '%s'",
                req.trava_ativa, req.trava_slot_id, req.os_id, resumo)
    resultado = await _enviar(
        "estado_celula",
        {
            "trava_ativa":   req.trava_ativa,
            "trava_slot_id": req.trava_slot_id,
            "os_id":         req.os_id,
            "trava_resumo":  resumo,
        },
    )
    return _resposta(resultado)


@app.post("/comandos/homing")
async def cmd_homing(req: ComandoHomingReq):
    logger.info("[CMD] HOMING ← OS %s", req.os_id)
    resultado = await _enviar(
        "homing",
        {k: v for k, v in req.model_dump().items() if v is not None},
    )
    return _resposta(resultado)


# ── Endpoint de Eventos (mesa CNC → Adapter → Central) ────────────────────────

async def _encaminhar_evento(req: EventoReq) -> bool:
    """Porta ÚNICA de saída do evento — os dois transportes passam por aqui.

    O payload atravessa o MESMO modelo Pydantic vindo do simulador e vindo da
    placa, e é isso que garante que o central receba de um firmware exatamente o
    byte que recebe hoje do simulador. Normalizar de um lado só abriria a
    divergência que este adapter existe para não ter — e o central não mudaria
    para acomodá-la, porque o sintoma seria um campo faltando, não um erro.
    """
    payload   = req.model_dump()
    tipo      = payload.get("tipo", "?")
    disp_alvo = payload.get("dispenser_alvo", "?")
    os_id     = payload.get("os_id", "")

    log_extra = f"| OS {os_id}" if os_id else ""
    logger.info("[EVT] %-12s ← D%s %s", tipo, disp_alvo, log_extra)

    ok = await _post_central(payload)
    if not ok:
        logger.warning("[FWD] Evento '%s' não chegou ao Central.", tipo)
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
    """Recebe eventos do cnc-simulator e repassa ao Central."""
    ok = await _encaminhar_evento(req)
    return {"ok": True, "encaminhado": ok}
