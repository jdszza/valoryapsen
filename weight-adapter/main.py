"""
APSEN - Weight Adapter v1.1
Bridge bidirecional entre o Computador Central e a balança HX711.

Fluxo entrada (← Central):
  POST /comandos/tara     → `tara`  na balança
  POST /comandos/pesar    → `pesar` na balança
                            (leva `quantidade_esperada` E `quantidade_real`;
                             ver docstring de `cmd_pesar`)

Fluxo saída (← balança):
  POST /eventos           → normaliza e encaminha ao central POST /api/v1/eventos/peso

A balança atende por DOIS transportes, e quem escolhe é `WEIGHT_TRANSPORTE`:

  "http"   (default) — o `weight-simulator`, como sempre. É o que roda no
                       Docker, no CI e em qualquer máquina sem hardware.
  "serial" — o firmware da balança HX711, por uma porta USB
             (`WEIGHT_SERIAL_URL`). Ver `docs/PROTOCOLO_SERIAL.md` e
             `serial_link.py`.

A perna de CIMA não sabe qual dos dois está embaixo: os endpoints, os modelos
Pydantic, o payload do evento e o `_post_central` são os mesmos nos dois casos.
É o que permite trocar o simulador por firmware sem tocar no central.

O firmware (`weight/balanca2_3/`) é uma balança de bancada ANTES de ser um
periférico da célula: ele conta por peso, tem calibração por canal e uma tela
de configuração que sempre foi o Monitor Serial. Isso lhe dá uma segunda
família de eventos — `boot`, `peso`, `contagem`, `cfg`, `estado`,
`tara_balanca`, `erro_balanca` — que **não vão ao central**: ele não tem
endpoint para elas e não decide nada com elas. Ficam aqui, em memória, e saem
por `GET /balanca`. É a mesma regra que o `dispenser-adapter` já aplica aos
eventos das telas TFT (`docs/PROTOCOLO_SERIAL.md` §6).

O par disso são os comandos de bancada (`POST /bancada/*`): com o transporte
serial ligado, este processo é o DONO da porta, e ninguém mais abre o Monitor
Serial. Sem eles, configurar o peso unitário passaria a exigir parar o adapter.
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WEIGHT-ADAPTER] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

CENTRAL_URL     = os.getenv("CENTRAL_URL",      "http://central-computer:8000")
WEIGHT_SIM_URL  = os.getenv("WEIGHT_SIM_URL",   "http://weight-simulator:8203")
TIMEOUT_CMD     = float(os.getenv("TIMEOUT_CMD",   "10"))
TIMEOUT_EVENT   = float(os.getenv("TIMEOUT_EVENT", "5"))

# Quantos dispensers a célula tem. Declarado UMA vez no compose
# (`${NUM_SLOTS:-8}`) e lido por todos: nenhuma faixa escrita à mão, e nenhuma
# mensagem de erro com um "1-6" desatualizado mandando quem lê o log procurar o
# problema na faixa errada.
NUM_SLOTS       = int(os.getenv("NUM_SLOTS", "8"))
MAX_UNIDADES    = int(os.getenv("MAX_UNIDADES", "500"))
# Teto de peso de UMA unidade. 0 é o valor que quebra a conta inteira: o peso
# esperado vira 0 g, o desvio percentual vira divisão por zero ou 0%, e a
# balança para de poder divergir — a terceira fonte do Triple Check sai de
# campo sem que nada acuse.
MAX_PESO_UNITARIO_G = float(os.getenv("MAX_PESO_UNITARIO_G", "5000"))

SUBSISTEMA      = "weight"


def _transporte() -> str:
    """Valor desconhecido cai no default COM aviso, nunca em silêncio.

    "http" é o default de propósito: a suíte, o CI e a demonstração em Docker
    não podem mudar de resultado por causa desta feature.
    """
    escolhido = os.getenv("WEIGHT_TRANSPORTE", "http").strip().lower()
    if escolhido not in ("http", "serial"):
        logger.warning("[CFG] WEIGHT_TRANSPORTE=%r desconhecido — usando 'http'.",
                       escolhido)
        return "http"
    return escolhido


TRANSPORTE      = _transporte()
SERIAL_URL      = os.getenv("WEIGHT_SERIAL_URL", "").strip()
SERIAL_BAUD     = int(os.getenv("WEIGHT_SERIAL_BAUD", "115200"))
ACK_TIMEOUT_S   = float(os.getenv("WEIGHT_ACK_TIMEOUT_S", "2"))

_client: httpx.AsyncClient | None = None
_link: "serial_link.LinkSerial | None" = None
_loop: asyncio.AbstractEventLoop | None = None


# ── A segunda família de eventos da balança ───────────────────────────────────
#
# Estes são da BANCADA, não da OS: o stream de peso, o resultado da contagem
# por peso, a configuração gravada na NVS e o estado da máquina de contagem.
# Eles param aqui. Despejá-los em `/api/v1/eventos/peso` misturaria duas
# conversas num histórico que hoje é só de pesagem de OS — e o central gravaria
# o buraco sem erro, porque o evento atravessa sem interpretação.
#
# O corolário é o que torna a lista verificável: um `tipo` que não esteja aqui
# É encaminhado. Esquecer de acrescentar um evento novo falha para o lado
# visível (uma linha estranha no central), nunca para o lado mudo.
_EVENTOS_BANCADA = frozenset({
    "boot", "peso", "contagem", "cfg", "estado", "tara_balanca", "erro_balanca",
})

# Último exemplar de cada um, mais o que o adapter concluiu sozinho. Escrito na
# THREAD LEITORA e lido no event loop: cada chave recebe o dicionário inteiro
# de uma vez, e quem lê tira uma cópia — não há atualização parcial para alguém
# enxergar pela metade.
_bancada: dict = {
    "boot": None, "peso": None, "contagem": None, "cfg": None,
    "estado": None, "tara_balanca": None, "erro_balanca": None,
    # A tara deixa de ser confiável no instante em que a placa reinicia: o
    # `setup()` do firmware tara sozinho depois de 5 s, e se havia peso na mesa
    # a tara levou o peso junto. Quem reinicia a placa não é este adapter
    # (DTR/RTS saem desligados antes do `open()`, justamente para não
    # reiniciá-la), então todo `boot` que chega aqui é um boot que ninguém
    # pediu — queda de energia, botão de reset, ou outro processo abrindo a
    # porta. Não bloqueia nada: quem decide é quem lê.
    "tara_confiavel": True,
    "boots_vistos": 0,
}


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
    _exigir_url(TRANSPORTE, SERIAL_URL, "WEIGHT_SERIAL_URL")
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
        await _wait_for_upstream("weight-simulator", WEIGHT_SIM_URL + "/ping")
    yield
    if _link is not None:
        _link.parar()
    await _client.aclose()
    logger.info("[SHUTDOWN] httpx.AsyncClient encerrado")


app = FastAPI(title="APSEN Weight Adapter v1.1", lifespan=lifespan)
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

    # A faixa é conferida NA BORDA, como no cnc-adapter. Este adapter não
    # validava nada, e cada campo torto vira um modo de falhar diferente e
    # longe daqui: `slot_id` fora da faixa vira 502 do simulador ("a ponta de
    # lá recusou", quando quem errou foi esta), quantidade negativa vira peso
    # esperado negativo, e `peso_unitario_g` igual a 0 zera o peso esperado —
    # a balança deixa de poder divergir e a terceira fonte do Triple Check sai
    # de campo em silêncio.

    @field_validator("slot_id")
    @classmethod
    def _slot_na_faixa(cls, v: int) -> int:
        if not 1 <= v <= NUM_SLOTS:
            raise ValueError(f"slot_id deve ser 1-{NUM_SLOTS}")
        return v

    @field_validator("quantidade_esperada", "quantidade_real")
    @classmethod
    def _quantidade_na_faixa(cls, v):
        # `quantidade_real` é opcional: ausente cai em `quantidade_esperada`
        # nos três pontos do caminho (contrato antigo), e `None` não é erro.
        if v is None:
            return v
        if not 0 <= v <= MAX_UNIDADES:
            raise ValueError(f"quantidade deve ser 0-{MAX_UNIDADES}")
        return v

    @field_validator("peso_unitario_g")
    @classmethod
    def _peso_unitario_positivo(cls, v: float) -> float:
        if not 0 < v <= MAX_PESO_UNITARIO_G:
            raise ValueError(f"peso_unitario_g deve ser >0 e <= {MAX_PESO_UNITARIO_G}")
        return v
    # Campo de DEMONSTRAÇÃO, ausente no caminho normal (ver
    # `central-computer/injecao.py`). O adapter não o interpreta: repassa como
    # veio, do mesmo jeito que faz com as duas quantidades da pesagem. Quem
    # decide é o central; quem executa é o simulador, num ramo isolado.
    injetar_falha: Optional[str] = None


class PesoUnitarioReq(BaseModel):
    """Peso de UMA unidade, em gramas — a base de toda contagem por peso."""
    # SEM validador de faixa aqui, e a ausência é decisão: quem recusa
    # `valor_g <= 0` é o handler `bancada_peso_unitario`, com 422 e um teste
    # próprio (`tests/test_balanca_serial.py`). Duplicar a regra no modelo
    # mudaria ONDE ela é aplicada — o 422 passaria a sair da validação do
    # FastAPI —, e a checagem do handler viraria código morto que ninguém
    # percebe ter parado de valer.
    valor_g: float


class StreamReq(BaseModel):
    on: bool = True


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


# ── Uma porta de saída, dois transportes ──────────────────────────────────────
#
# O NOME do comando é o mesmo nos dois caminhos — é o `cmd` da linha serial e a
# chave desta tabela —, e os campos do payload são os mesmos que o simulador já
# recebe. Renomear qualquer um deles no serial obrigaria o adapter a traduzir, e
# uma tradução é o lugar onde os dois lados divergem depois; é a mesma razão
# pela qual o evento atravessa daqui para o central sem interpretação.

_ROTAS_SIM = {
    "tara":  "/executar/tara",
    "pesar": "/executar/pesar",
}


# Os comandos de BANCADA moram numa tabela à parte, e não em `_ROTAS_SIM`, por
# uma razão concreta: eles não existem no `weight-simulator`. Uma entrada em
# `_ROTAS_SIM` promete as duas pernas — o mesmo comando funcionando por HTTP e
# por serial —, e prometê-la aqui daria 404 no transporte que é o default.
# São da PLACA, e por isso só existem quando há placa. É a mesma geometria do
# `_COMANDOS_TFT` no `dispenser-adapter`.
_COMANDOS_BANCADA = {
    "peso_unitario":   "define o peso de uma unidade (g) e grava na NVS",
    "tara_recipiente": "mede o pote vazio e o desconta da contagem",
    "tara_canais":     "zera os offsets do HX711 (calibração, grava na NVS)",
    "contar":          "espera estabilizar e conta por peso",
    "config":          "publica a configuração de contagem",
    "stream":          "liga/desliga o stream periódico de peso",
}


async def _enviar_bancada(comando: str, campos: dict) -> dict:
    """Comando de bancada — serial e só serial.

    Sem placa não há o que configurar, e responder 503 dizendo isso é o que
    separa "não há balança nesta montagem" de "a balança recusou". O erro é o
    mesmo vocabulário do `_enviar`: recusa da ponta de lá é 502, ponta de lá
    inalcançável é 503.
    """
    if TRANSPORTE != "serial":
        raise HTTPException(
            503,
            f"Comando de bancada '{comando}' exige WEIGHT_TRANSPORTE=serial — "
            f"o weight-simulator não tem balança para configurar.",
        )
    if _link is None:
        raise HTTPException(503, "Balança: transporte serial não iniciado")
    try:
        return await asyncio.to_thread(_link.enviar_comando, comando, campos)
    except serial_link.AckNegativo as exc:
        raise HTTPException(502, f"Balança recusou '{comando}': {exc}")
    except serial_link.ErroLink as exc:
        raise HTTPException(503, f"Balança indisponível: {exc}")


async def _enviar(comando: str, payload: dict) -> dict:
    """Despacha o comando pelo transporte configurado.

    O erro que sobe é o MESMO nos dois: o orquestrador não distingue firmware de
    simulador, e não deveria. Recusa da ponta de lá é 502, ponta de lá
    inalcançável é 503 — exatamente o que `_post_sim` sempre levantou.
    """
    if TRANSPORTE != "serial":
        return await _post_sim(_ROTAS_SIM[comando], payload)
    if _link is None:
        raise HTTPException(503, "Balança: transporte serial não iniciado")
    try:
        # `enviar_comando` BLOQUEIA até o ACK, e `Serial.write`/`read` são
        # síncronos: chamá-los no event loop congelaria o adapter inteiro,
        # inclusive o /ping que o healthcheck do compose usa como portão. É a
        # mesma regra que o central aplica ao banco ("Nada de banco no event
        # loop"), e `tests/test_serial_link.py` a cobra por AST.
        return await asyncio.to_thread(_link.enviar_comando, comando, payload)
    except serial_link.AckNegativo as exc:
        raise HTTPException(502, f"Balança recusou '{comando}': {exc}")
    except serial_link.ErroLink as exc:
        raise HTTPException(503, f"Balança indisponível: {exc}")


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
    """Diagnóstico: conectividade com o central e com a ponta de baixo.

    É AQUI que o estado da porta serial aparece — nunca no /ping. O /ping diz
    que este processo está de pé, e é isso que o compose usa como portão;
    atrelá-lo ao hardware faria um cabo solto marcar o serviço como unhealthy e
    derrubar em cascata quem depende dele.
    """
    checks: dict[str, str] = {}
    alvos = [("central-computer", CENTRAL_URL + "/ping")]
    if TRANSPORTE != "serial":
        alvos.insert(0, ("weight-simulator", WEIGHT_SIM_URL + "/ping"))
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
        checks["placa-balanca"] = "ok" if estado.get("conectado") else "desconectada"
        # Diagnóstico, não portão: tara suspeita NÃO derruba o /health. Ela é
        # uma pendência do operador (esvaziar a mesa e tarar), e marcar o
        # serviço como degradado por causa dela faria o compose tratar uma
        # decisão humana como falha de infraestrutura.
        corpo["bancada"] = {
            "tara_confiavel": _bancada["tara_confiavel"],
            "boots_vistos":   _bancada["boots_vistos"],
            "ultimo_estado":  (_bancada["estado"] or {}).get("estado"),
        }

    ok = all(v == "ok" for v in checks.values())
    corpo["status"] = "ok" if ok else "degradado"
    return corpo


@app.post("/comandos/tara")
async def cmd_tara(req: TaraReq):
    """Zera a balança antes de uma OS."""
    logger.info("[CMD] TARA ← OS=%s", req.os_id)
    resultado = await _enviar("tara", {"os_id": req.os_id})
    # Uma tara aceita é exatamente o ato que o `boot` invalidou, então é ela
    # que devolve a confiança — e não um botão à parte, que alguém clicaria
    # sem ter esvaziado a mesa.
    _bancada["tara_confiavel"] = True
    return _resposta(resultado)


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
    resultado = await _enviar(
        "pesar",
        {
            "os_id":               req.os_id,
            "slot_id":             req.slot_id,
            "quantidade_esperada": req.quantidade_esperada,
            "quantidade_real":     quantidade_real,
            "peso_unitario_g":     req.peso_unitario_g,
            "injetar_falha":       req.injetar_falha,
        },
    )
    return _resposta(resultado)


# ── Endpoint de Eventos (balança → Adapter → Central) ─────────────────────────

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
    slot_id = payload.get("slot_id", "?")
    os_id   = payload.get("os_id", "")

    log_extra = f"| OS {os_id}" if os_id else ""
    logger.info("[EVT] %-22s ← slot=%s %s", tipo, slot_id, log_extra)

    ok = await _post_central(payload)
    if not ok:
        logger.warning("[FWD] Evento '%s' slot=%s não chegou ao Central.", tipo, slot_id)
    return ok


def _registrar_bancada(payload: dict) -> None:
    """Guarda o evento de bancada e para por aqui. Roda na thread leitora."""
    tipo = payload.get("tipo")
    _bancada[tipo] = payload

    if tipo == "boot":
        _bancada["boots_vistos"] = _bancada["boots_vistos"] + 1
        _bancada["tara_confiavel"] = False
        logger.warning(
            "[SERIAL] a balança REINICIOU (fw=%s). Este adapter não a reinicia: "
            "abrir a porta não a reseta, de propósito. O `setup()` dela tara "
            "sozinho depois de 5s — se havia peso na mesa, a tara levou o peso "
            "junto. Mande POST /comandos/tara com a mesa vazia antes da próxima "
            "OS. (/balanca: tara_confiavel=false)", payload.get("fw"),
        )
    elif tipo == "erro_balanca":
        logger.warning("[SERIAL] balança: %s (cmd=%s)",
                       payload.get("msg"), payload.get("cmd"))


def _evento_da_placa(payload: dict) -> None:
    """Chamado NA THREAD LEITORA do `serial_link` — nunca no event loop.

    O salto para o loop é `run_coroutine_threadsafe`: é o que deixa o
    `_post_central` (httpx async, com o retry que a suíte cobra) ser o mesmo dos
    dois transportes, em vez de ganhar uma segunda implementação síncrona.

    O desvio da bancada vem ANTES da validação: estes eventos não são de OS, e
    fazê-los passar pelo `EventoReq` só para serem descartados do outro lado
    daria a impressão de que um dia eles subiriam.
    """
    if payload.get("tipo") in _EVENTOS_BANCADA:
        _registrar_bancada(payload)
        return
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
    """Recebe resultado de pesagem do weight-simulator e encaminha ao Central."""
    ok = await _encaminhar_evento(req)
    return {"ok": True, "encaminhado": ok}


# ── A balança como BANCADA (só existe com placa) ──────────────────────────────

@app.get("/balanca")
async def balanca():
    """Último de cada evento de bancada, como a placa o mandou.

    Cru, sem interpretação — a mesma regra do evento que atravessa até o
    central. Quem lê é quem está na bancada: `peso` é o stream ao vivo, `cfg`
    é o que está gravado na NVS da placa, `contagem` é o último resultado da
    contagem por peso, e `tara_confiavel` é a única conclusão que este adapter
    tira sozinho.

    Com `WEIGHT_TRANSPORTE=http` tudo vem nulo, e isso não é erro: não há
    balança de bancada atrás do weight-simulator.
    """
    return {
        "transporte": TRANSPORTE,
        **{chave: _bancada[chave] for chave in _bancada},
    }


@app.post("/bancada/peso-unitario")
async def bancada_peso_unitario(req: PesoUnitarioReq):
    """Define o peso de UMA unidade, em gramas, e grava na NVS da placa.

    A validação é daqui, e não só da placa: `valor_g <= 0` desliga a contagem
    por peso inteira (`countByWeight` devolve `NO_UNIT_WEIGHT`), e um zero que
    chegasse lá viraria uma balança que não conta mais — sem erro em lugar
    nenhum até alguém pedir uma contagem.
    """
    if not (req.valor_g > 0) or req.valor_g != req.valor_g:   # NaN
        raise HTTPException(422, "valor_g tem que ser maior que zero")
    logger.info("[BANCADA] peso unitário ← %.4f g", req.valor_g)
    return {"ok": True, "placa": await _enviar_bancada("peso_unitario",
                                                       {"valor_g": req.valor_g})}


@app.post("/bancada/tara-recipiente")
async def bancada_tara_recipiente():
    """Mede o pote VAZIO e o desconta de toda contagem seguinte."""
    logger.info("[BANCADA] tara do recipiente")
    return {"ok": True, "placa": await _enviar_bancada("tara_recipiente", {})}


@app.post("/bancada/tara-canais")
async def bancada_tara_canais():
    """Zera os offsets do HX711 — é CALIBRAÇÃO, e vai para a NVS.

    Diferente de `POST /comandos/tara`, que só move o zero lógico da mesa para
    a OS. Confundir as duas é tarar o hardware com peso em cima e levar o peso
    junto, permanentemente.
    """
    logger.info("[BANCADA] tara dos canais (calibração)")
    return {"ok": True, "placa": await _enviar_bancada("tara_canais", {})}


@app.post("/bancada/contar")
async def bancada_contar():
    """Espera a mesa estabilizar e conta por peso. Recusado sem peso unitário."""
    logger.info("[BANCADA] contar")
    return {"ok": True, "placa": await _enviar_bancada("contar", {})}


@app.post("/bancada/config")
async def bancada_config():
    """Pede à placa que publique a config gravada — ela volta como `cfg`."""
    return {"ok": True, "placa": await _enviar_bancada("config", {})}


@app.post("/bancada/stream")
async def bancada_stream(req: StreamReq):
    """Liga/desliga o stream periódico de peso (5 Hz).

    Desligar é o que se faz quando a linha precisa ficar livre para os ACKs —
    num canal de 115200 baud, despejo periódico compete com o caminho crítico
    da OS.
    """
    logger.info("[BANCADA] stream ← %s", "on" if req.on else "off")
    return {"ok": True, "placa": await _enviar_bancada("stream", {"on": req.on})}
