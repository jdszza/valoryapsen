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

Há uma SEGUNDA placa, numa SEGUNDA porta, e o dono das duas é este adapter:
as 8 telas TFT (`dispenser_tft`, `DISPENSER_TFT_TRANSPORTE`). Acionar os 8
mecanismos, desenhar 8 telas e manter a serial não cabe num ESP só. Este
adapter já vê todo comando que desce e todo evento que sobe do slot — tudo que
as telas precisam mostrar —, então é ele quem as espelha:

  POST /comandos/estado-celula  → `estado_celula` na placa das telas (só nela)
  carregar/dispensar/limpar e os eventos carregado/dispensado/limpeza_ok/erro
                                → `slot` na placa das telas, na TRANSIÇÃO

Falha da placa das telas NUNCA muda o caminho do dispenser: não recusa comando,
não atrasa ACK, não impede o evento de chegar ao central. Tela errada é
cosmética; dispensa atrasada não é. E os eventos DELA (`telemetria`, `erro`)
ficam aqui, em log e no /health — o central não tem endpoint de tela.

E a placa das telas carrega a PORTA NR-12 (`docs/PROTOCOLO_SERIAL.md` §9): a
ESP da porta fala por UART com ela, e ela repassa pela USB. Para este adapter a
porta é mais uma ORIGEM de evento (`"origem":"porta"`) e mais três comandos:

  POST /comandos/porta/liberar  → `porta_liberar` (através da placa das telas)
  POST /comandos/porta/negar    → `porta_negar`
  POST /comandos/porta/estado   → `porta_estado`
  eventos com origem "porta"    → POST /api/v1/eventos/porta no central

O adapter traduz TRANSPORTE, não decide nada da porta: o conteúdo atravessa
cru. O que ele soma é o LINK — a USB das telas, a vigia da placa das telas e a
idade do último heartbeat viram UM estado, e só a transição sobe.
"""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, field_validator

import serial_link

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [DISP-ADAPTER] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CENTRAL_URL          = os.getenv("CENTRAL_URL",          "http://central-computer:8000")
DISPENSER_SIM_URL    = os.getenv("DISPENSER_SIM_URL",    "http://dispenser-simulator:8201")
TIMEOUT_CMD          = float(os.getenv("TIMEOUT_CMD",    "15"))
TIMEOUT_EVENT        = float(os.getenv("TIMEOUT_EVENT",  "5"))

# Quantos dispensers a célula tem. Declarado UMA vez no compose
# (`${NUM_SLOTS:-8}`) e lido por todos: nenhuma faixa escrita à mão, e nenhuma
# mensagem de erro com um "1-6" desatualizado mandando quem lê o log procurar o
# problema na faixa errada. Este adapter era o único dos três que não o lia.
NUM_SLOTS            = int(os.getenv("NUM_SLOTS", "8"))
# Teto de unidades por comando. Não é regra de negócio — as ordens padrão vão
# até 15 —, é o teto do que um MECANISMO faz num comando só: além dele, o que
# chegou é erro de quem montou o payload, e o lugar de dizer isso é aqui.
MAX_UNIDADES         = int(os.getenv("MAX_UNIDADES", "500"))

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

# ── A segunda placa: as 8 telas TFT (`dispenser_tft`) ─────────────────────────
#
# "http" = DESLIGADO, nenhuma tela. A placa das telas só existe por serial, e
# o default mantém este adapter EXATAMENTE como era antes dela — a suíte, o
# CI e a demonstração em Docker não mudam de resultado. Com "http",
# `estado_celula` ainda desce ao simulador (que só loga), para que a perna de
# cima não saiba qual transporte está embaixo — sem a rota, o http tomaria 404
# e viraria recusa determinística enquanto o serial funciona; `slot` não vai
# a lugar nenhum: o simulador É o dispenser e já sabe o que tem em cada slot.
TFT_SUBSISTEMA       = "dispenser_tft"


def _transporte_tft() -> str:
    escolhido = os.getenv("DISPENSER_TFT_TRANSPORTE", "http").strip().lower()
    if escolhido not in ("http", "serial"):
        logger.warning("[CFG] DISPENSER_TFT_TRANSPORTE=%r desconhecido — usando "
                       "'http' (telas desligadas).", escolhido)
        return "http"
    return escolhido


TFT_TRANSPORTE       = _transporte_tft()
TFT_SERIAL_URL       = os.getenv("DISPENSER_TFT_SERIAL_URL", "").strip()
TFT_SERIAL_BAUD      = int(os.getenv("DISPENSER_TFT_SERIAL_BAUD", "115200"))
TFT_ACK_TIMEOUT_S    = float(os.getenv("DISPENSER_TFT_ACK_TIMEOUT_S", "2"))

# Teto do `trava_resumo` que vai às telas. Ele sai da CATEGORIA da divergência
# ("divergência de peso", "contagem divergente"), nunca da string formatada
# do central, que passa de 240 caracteres: mandá-la acoplaria o formato de
# mensagem do central à largura de uma tela e criaria um segundo ponto de
# truncamento para algo cosmético. O motivo completo é do display de 7" e da
# web, onde o supervisor decide; a tela do slot responde uma pergunta só: é
# este slot? O adapter corta em 48 também — o teto é do contrato, não de quem
# chama.
TRAVA_RESUMO_MAX     = 48

# ── A porta NR-12, atrás da placa das telas (§9) ─────────────────────────────
#
# Heartbeat da porta mais velho que isto = link perdido, mesmo com a USB das
# telas de pé. A placa das telas já vigia em 1,5 s; este é o vigia de CIMA, que
# cobre a própria placa das telas travada (ela não avisaria nada).
PORTA_HEARTBEAT_MAX_S = float(os.getenv("PORTA_HEARTBEAT_MAX_S", "2.5"))
# O heartbeat chega a cada 500 ms e não sobe a cada 500 ms: sobe na MUDANÇA e,
# sem mudança, no máximo a cada tanto — como sinal de vida. É ele que deixa o
# central distinguir "porta parada, tudo fechado" de "adapter morto".
PORTA_ESTADO_REENVIO_S = float(os.getenv("PORTA_ESTADO_REENVIO_S", "5"))

_client: httpx.AsyncClient | None = None
_link: "serial_link.LinkSerial | None" = None
_link_tft: "serial_link.LinkSerial | None" = None
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


def _exigir_url(transporte: str, url: str, variavel: str) -> None:
    """Transporte serial ligado sem porta fixada RECUSA subir.

    Com a URL vazia o `serial_link` varre TODAS as portas, e cada sondagem abre
    a porta por até `PROBE_ASSENTAR_S + PROBE_ESPERA_S` (~9,5 s). Na célula
    montada são cinco placas e cinco processos: enquanto um deles segura a COM
    da CNC para conferir, o cnc-adapter toma `ACCESS_DENIED` na própria — boot
    não-determinístico em que uma placa às vezes não é achada, e o log de cada
    processo mostra só a metade dele.

    Recusar é a resposta certa AQUI, e não um aviso: este processo existe para
    falar com as portas, e sem elas não faz nada de útil. Serviço que não sobe
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
    global _client, _link, _link_tft, _loop
    _client = httpx.AsyncClient()
    _loop = asyncio.get_running_loop()
    logger.info("[STARTUP] httpx.AsyncClient criado | transporte=%s | telas=%s",
                TRANSPORTE, TFT_TRANSPORTE)
    _exigir_url(TRANSPORTE, SERIAL_URL, "DISPENSER_SERIAL_URL")
    _exigir_url(TFT_TRANSPORTE, TFT_SERIAL_URL, "DISPENSER_TFT_SERIAL_URL")
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
    if TFT_TRANSPORTE == "serial":
        # A segunda porta. Outro `LinkSerial`, outro `cmd_id` monotônico, outra
        # thread leitora — e o mesmo `serial_link.py`, sem mudança nenhuma.
        _link_tft = serial_link.LinkSerial(
            subsistema=TFT_SUBSISTEMA, url=TFT_SERIAL_URL, baud=TFT_SERIAL_BAUD,
            ack_timeout_s=TFT_ACK_TIMEOUT_S, ao_receber_evento=_evento_da_placa_tft,
        )
        _link_tft.iniciar()
        # A vigia da porta NR-12 só existe onde existe porta.
        vigia_porta = asyncio.create_task(_laco_vigia_porta())
    yield
    if TFT_TRANSPORTE == "serial":
        vigia_porta.cancel()
    if _link is not None:
        _link.parar()
    if _link_tft is not None:
        _link_tft.parar()
    await _client.aclose()
    logger.info("[SHUTDOWN] httpx.AsyncClient encerrado")


app = FastAPI(title="APSEN Dispenser Adapter v1.1", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Pydantic Models ────────────────────────────────────────────────────────────

# ── A faixa é conferida NA BORDA, como no cnc-adapter ─────────────────────────
#
# Este adapter não validava nada: `dispenser_id` aceitava 0, -3 e 99, e
# `quantidade` aceitava negativo. Cada um desses sai do adapter e vira um modo
# de falhar diferente, todos longe daqui:
#
#   - slot fora da faixa no transporte HTTP vira um 4xx do simulador, que o
#     `_post_sim` traduz em 502 — "a ponta de lá recusou", quando quem estava
#     errado era o payload desta ponta;
#   - no transporte SERIAL é pior: a placa recusa com ACK negativo depois de o
#     comando ter ido e voltado pelo cabo, e o orquestrador perde o prazo do
#     ACK num comando que nunca poderia dar certo;
#   - `quantidade` negativa no `carregar` desce para o firmware, que grava
#     `sl.quantidade` negativa — e a partir daí o `residual` do `dispensado`
#     mente para o central, sem erro em lugar nenhum.
#
# Recusar aqui é 422 na hora, com o nome do campo. É a mesma escolha que o
# `_receita_valida` do cnc-adapter já registra: deixar passar custaria a ida à
# ponta de lá para voltar com o mesmo "não", mais caro.

def _slot_na_faixa(v: int) -> int:
    if not 1 <= v <= NUM_SLOTS:
        raise ValueError(f"dispenser_id deve ser 1-{NUM_SLOTS}")
    return v


class ComandoCarregarReq(BaseModel):
    dispenser_id: int
    medicamento: str
    sku: str = ""
    categoria: str = ""
    quantidade: int
    os_id: str

    @field_validator("dispenser_id")
    @classmethod
    def _faixa(cls, v: int) -> int:
        return _slot_na_faixa(v)

    @field_validator("quantidade")
    @classmethod
    def _quantidade_positiva(cls, v: int) -> int:
        if not 1 <= v <= MAX_UNIDADES:
            raise ValueError(f"quantidade deve ser 1-{MAX_UNIDADES}")
        return v


class ComandoDispensarReq(BaseModel):
    dispenser_id: int
    os_id: str

    @field_validator("dispenser_id")
    @classmethod
    def _faixa(cls, v: int) -> int:
        return _slot_na_faixa(v)
    # Campo de DEMONSTRAÇÃO, ausente no caminho normal (ver
    # `central-computer/injecao.py`). O adapter não o interpreta: repassa como
    # veio, do mesmo jeito que faz com as duas quantidades da pesagem. Quem
    # decide é o central; quem executa é o simulador, num ramo isolado.
    injetar_falha: Optional[str] = None


class ComandoLimparReq(BaseModel):
    dispenser_id: int
    solicitado_por: str = "sistema"

    @field_validator("dispenser_id")
    @classmethod
    def _faixa(cls, v: int) -> int:
        return _slot_na_faixa(v)


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


# ── A placa das telas: comandos, espelho dos slots e eventos que ficam aqui ───
#
# `slot` vai SÓ por serial (sem telas, não há o que pintar). `estado_celula`
# tem rota no simulador para o transporte http não virar 404.
_COMANDOS_TFT = {
    "slot":          None,
    "estado_celula": "/executar/estado-celula",
}


def _resumo_trava(texto) -> str:
    """`trava_resumo` pronto para a tela: uma linha, no máximo TRAVA_RESUMO_MAX."""
    limpo = " ".join(str(texto or "").split())
    return limpo[:TRAVA_RESUMO_MAX]


async def _tft_http(rota: str, campos: dict) -> bool:
    try:
        r = await _client.post(DISPENSER_SIM_URL + rota, json=campos, timeout=TIMEOUT_CMD)
        if r.status_code >= 300:
            logger.warning("[TFT] simulador respondeu %d a %s", r.status_code, rota)
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — cosmético: loga e segue
        logger.warning("[TFT] %s indisponível: %s", rota, exc)
        return False


async def _tft_serial(comando: str, campos: dict) -> bool:
    try:
        # `to_thread`, como `_enviar`: `enviar_comando` bloqueia até o ACK e
        # serial nunca roda no event loop.
        await asyncio.to_thread(_link_tft.enviar_comando, comando, campos)
        return True
    except serial_link.ErroLink as exc:
        logger.warning("[TFT] '%s' não chegou às telas: %s", comando, exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TFT] '%s' falhou: %s", comando, exc)
        return False


# As tasks em voo, guardadas por referência forte.
#
# `asyncio.create_task` NÃO guarda uma: o loop mantém só uma referência fraca
# enquanto a corrotina está suspensa, e o coletor pode recolher a task no meio
# do caminho — o envio some sem log nenhum, e o sintoma é uma tela de slot
# parada no estado anterior. É raro e é intermitente, que é o pior par: some
# quando alguém vai procurar.
#
# O `discard` no callback é o que impede o conjunto de virar um vazamento: sem
# ele, `_enviar_tft` acrescentaria um objeto por transição de slot, para
# sempre.
_tarefas_tft: set = set()


def _agendar(corrotina):
    tarefa = asyncio.create_task(corrotina)
    _tarefas_tft.add(tarefa)
    tarefa.add_done_callback(_tarefas_tft.discard)
    return tarefa


def _enviar_tft(comando: str, campos: dict):
    """Agenda o envio à placa das telas. Devolve a Task, ou None se não há telas.

    Fire-and-forget DE PROPÓSITO: quem chama está no caminho da dispensa — o
    handler que acabou de aceitar `dispensar`, ou o que está encaminhando
    `dispensado` ao central — e a placa das telas não pode segurá-lo nem por
    um ACK. Tela errada é cosmética; dispensa atrasada não é. O resultado só
    interessa a quem quiser esperá-lo (o endpoint `estado-celula`), e mesmo
    esse só o loga.
    """
    if TFT_TRANSPORTE != "serial":
        rota = _COMANDOS_TFT[comando]
        if rota is None:
            return None
        return _agendar(_tft_http(rota, campos))
    if _link_tft is None:
        logger.warning("[TFT] '%s' descartado: porta das telas não iniciada", comando)
        return None
    return _agendar(_tft_serial(comando, campos))


# O que cada tela mostra, slot a slot. É a memória que este adapter já tinha
# de graça — ele vê todo comando e todo evento do slot — e é ela que vira o
# comando `slot`, só na transição. Nada periódico: o canal é 115200 baud e a
# regra de não competir com o caminho crítico está no docs/PROTOCOLO_SERIAL.md.
def _slot_vazio() -> dict:
    return {"medicamento": "", "sku": "", "categoria": "", "quantidade_alvo": 0,
            "quantidade_dispensada": 0, "quantidade_residual": 0,
            "status": "idle", "os_id": ""}


_slots: dict[int, dict] = {}


def _atualizar_slot(slot_id: int, **campos) -> dict:
    registro = _slots.setdefault(slot_id, _slot_vazio())
    registro.update({k: v for k, v in campos.items() if v is not None})
    return registro


def _espelhar_slot(slot_id: int) -> None:
    s = _slots.get(slot_id) or _slot_vazio()
    _enviar_tft("slot", {
        "dispenser_id":          slot_id,
        "medicamento":           s["medicamento"],
        "sku":                   s["sku"],
        "categoria":             s["categoria"],
        "quantidade_alvo":       s["quantidade_alvo"],
        "quantidade_dispensada": s["quantidade_dispensada"],
        "quantidade_residual":   s["quantidade_residual"],
        "status":                s["status"],
        "os_id":                 s["os_id"],
    })


def _espelhar_evento_no_slot(payload: dict) -> None:
    """Transições da placa dos mecanismos que mudam o que a tela mostra.

    `status`/`telemetria` (periódicos) NÃO passam por aqui: o que eles trazem a
    tela já mostra, e mandá-los seria despejo periódico no canal das telas.
    """
    tipo = payload.get("tipo")
    slot_id = payload.get("dispenser_id")
    if not isinstance(slot_id, int) or isinstance(slot_id, bool):
        return
    if tipo == "carregado":
        _atualizar_slot(slot_id, status="pronto",
                        medicamento=payload.get("medicamento"),
                        sku=payload.get("sku"), categoria=payload.get("categoria"),
                        quantidade_alvo=payload.get("quantidade_total"),
                        quantidade_dispensada=0,
                        quantidade_residual=payload.get("quantidade_residual"),
                        os_id=payload.get("os_id"))
    elif tipo == "dispensado":
        _atualizar_slot(slot_id, status="concluido",
                        quantidade_dispensada=payload.get("quantidade_dispensada"),
                        quantidade_alvo=payload.get("quantidade_alvo"),
                        quantidade_residual=payload.get("quantidade_residual"),
                        os_id=payload.get("os_id"))
    elif tipo == "limpeza_ok":
        _slots[slot_id] = {**_slot_vazio(), "status": "limpo"}
    elif tipo == "erro":
        _atualizar_slot(slot_id, status="erro")
    else:
        return
    _espelhar_slot(slot_id)


# Eventos DA PLACA DAS TELAS. Não vão ao central: ele não tem endpoint de tela
# e não decide nada com isso — despejá-los em /api/v1/eventos/dispenser
# misturaria duas placas num histórico que hoje é de uma. Ficam em log e no
# /health.
_tft_estado: dict = {"ultima_telemetria": None, "ultimo_erro": None, "erros": 0}


def _evento_da_placa_tft(payload: dict) -> None:
    """Chamado NA THREAD LEITORA da porta das telas.

    Os eventos DAS TELAS nunca chegam ao central. Os da PORTA NR-12 — que vêm
    pela mesma USB, marcados com `"origem":"porta"` — chegam todos, pela rota
    irmã e na ordem em que vieram.
    """
    if payload.get("origem") == "porta":
        loop = _loop
        if loop is None:
            logger.warning("[PORTA] evento chegou antes do event loop — descartado.")
            return
        asyncio.run_coroutine_threadsafe(_tratar_evento_porta(payload), loop)
        return
    tipo = payload.get("tipo")
    if tipo == "telemetria":
        _tft_estado["ultima_telemetria"] = payload
        logger.info("[TFT] telemetria: %s tela(s) ok, brilho %s%%",
                    payload.get("telas_ok"), payload.get("brilho_pct"))
    elif tipo == "erro":
        _tft_estado["ultimo_erro"] = payload
        _tft_estado["erros"] += 1
        logger.warning("[TFT] erro na tela D%s: %s — %s", payload.get("dispenser_id"),
                       payload.get("codigo_erro"), payload.get("descricao"))
    else:
        logger.warning("[TFT] evento desconhecido descartado: %r", tipo)


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
        "tft_conectada": None,
        "desde": time.monotonic(),
        "eventos": 0,
        "perdas": 0,
    }


_porta: dict = _porta_vazia()
# Serializa o envio ao central: o `porta_link_ok` tem de chegar ANTES do
# `estado` que o provou, e dois `run_coroutine_threadsafe` seguidos podem se
# cruzar no `await` do POST. `asyncio.Lock` é FIFO — a ordem de chegada é a de
# saída. Criado no loop, na primeira vez.
_porta_lock: "asyncio.Lock | None" = None


def _ts_agora() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _assinatura(payload: dict) -> str:
    return json.dumps({k: v for k, v in payload.items() if k != "ts"},
                      sort_keys=True, ensure_ascii=False, default=str)


async def _postar_porta(payload: dict) -> bool:
    global _porta_lock
    if _porta_lock is None:
        _porta_lock = asyncio.Lock()
    async with _porta_lock:
        ok = await _post_central(payload, ROTA_EVENTOS_PORTA)
    if not ok:
        logger.warning("[PORTA] evento '%s' não chegou ao Central.", payload.get("tipo"))
    return ok


async def _porta_link_perdido(motivo: str) -> None:
    """Link perdido = porta aberta, para o central. Sobe UMA vez por queda."""
    if _porta["link_ok"] is False:
        return
    _porta["link_ok"] = False
    _porta["perdas"] += 1
    _porta["assinatura_encaminhada"] = None   # o próximo `estado` sobe sempre
    logger.warning("[PORTA] link da porta PERDIDO — %s.", motivo)
    await _postar_porta({"origem": "porta", "tipo": "porta_link_perdido",
                         "ts": _ts_agora()})


async def _tratar_evento_porta(payload: dict) -> None:
    """Um evento da porta (ou da vigia da placa das telas), já no event loop.

    O conteúdo sobe CRU. As duas únicas decisões são de transporte: o link e o
    heartbeat repetido.
    """
    _porta["eventos"] += 1
    tipo = payload.get("tipo")

    if tipo == "porta_link_perdido":
        await _porta_link_perdido("a placa das telas parou de receber o heartbeat")
        return
    if tipo == "porta_link_ok":
        # A volta só é declarada pelo `estado` que a placa repassa logo depois.
        logger.info("[PORTA] a placa das telas voltou a ouvir a porta.")
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


async def _vigiar_porta_uma_vez() -> None:
    """Uma rodada da vigia de cima: USB das telas e idade do heartbeat.

    Separada do laço para que a suíte a chame sem esperar relógio.
    """
    if TFT_TRANSPORTE != "serial":
        return
    conectada = bool(_link_tft is not None and _link_tft.conectado)
    antes = _porta["tft_conectada"]
    _porta["tft_conectada"] = conectada

    if not conectada:
        # A porta deixa de ser vigiada quando a placa das telas some.
        await _porta_link_perdido("a USB da placa das telas caiu")
        return
    if antes is False:
        # A USB voltou: pede um `estado` na hora em vez de esperar o próximo
        # heartbeat. O `porta_link_ok` só sobe com o `estado` que chegar.
        logger.info("[PORTA] USB das telas de volta — pedindo `porta_estado`.")
        try:
            await asyncio.to_thread(_link_tft.enviar_comando, "porta_estado", {})
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


# Os três comandos da porta. O NOME é o `cmd` da linha serial, que a placa das
# telas reconhece pelo prefixo `porta_` e desce à porta como veio.
_COMANDOS_PORTA = {
    "porta_liberar": "/comandos/porta/liberar",
    "porta_negar":   "/comandos/porta/negar",
    "porta_estado":  "/comandos/porta/estado",
}


async def _enviar_porta(comando: str, payload: dict) -> dict:
    """Comando à porta, através da placa das telas. Mesmos 502/503 de sempre.

    Sem as telas por serial não existe porta: o simulador não tem uma, e
    fingir um ACK aqui faria o central destravar uma porta imaginária.
    """
    if TFT_TRANSPORTE != "serial" or _link_tft is None:
        raise HTTPException(503, "Porta NR-12 indisponível: ela viaja pela placa das "
                                 "telas, e DISPENSER_TFT_TRANSPORTE não é serial.")
    try:
        return await asyncio.to_thread(_link_tft.enviar_comando, comando, payload)
    except serial_link.AckNegativo as exc:
        raise HTTPException(502, f"Porta recusou '{comando}': {exc}")
    except serial_link.ErroLink as exc:
        raise HTTPException(503, f"Porta indisponível: {exc}")


def _estado_porta_health() -> dict:
    ultimo = _porta["ultimo_estado_em"]
    return {
        "link_ok": _porta["link_ok"],
        "tft_conectada": _porta["tft_conectada"],
        "ultimo_heartbeat_ts": _porta["ultimo_estado_ts"],
        "idade_heartbeat_s": None if ultimo is None else round(time.monotonic() - ultimo, 2),
        "ultimo_estado": _porta["ultimo_estado"],
        "eventos": _porta["eventos"],
        "perdas_de_link": _porta["perdas"],
    }


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


ROTA_EVENTOS          = "/api/v1/eventos/dispenser"
ROTA_EVENTOS_PORTA    = "/api/v1/eventos/porta"


async def _post_central(payload: dict, rota: str = ROTA_EVENTOS) -> bool:
    """Encaminha evento ao Central, com retry. Nunca lança — devolve False.

    False significa "o evento NÃO chegou depois de `_TENTATIVAS_EVENTO`
    tentativas", e quem chama já registra isso no log. Não aborta o fluxo do
    adapter: ele não tem o que fazer com a informação, e derrubar a resposta ao
    simulador não traria o evento de volta.

    `rota` existe para a porta NR-12, que tem rota irmã no central: a mesma
    política de retry, e não uma segunda implementação dela.
    """
    url = CENTRAL_URL + rota
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

    corpo = {"transporte": TRANSPORTE, "transporte_tft": TFT_TRANSPORTE, "checks": checks}
    if TRANSPORTE == "serial":
        estado = _link.estado() if _link is not None else {"conectado": False}
        corpo["serial"] = estado
        checks["placa-dispenser"] = "ok" if estado.get("conectado") else "desconectada"
    # A SEGUNDA porta, separada por subsistema e no mesmo formato de
    # `link.estado()`. Os eventos da placa das telas moram aqui também — é o
    # único lugar em que aparecem, porque não vão ao central.
    if TFT_TRANSPORTE == "serial":
        estado_tft = _link_tft.estado() if _link_tft is not None else {"conectado": False}
        corpo["serial_tft"] = estado_tft
        checks["placa-dispenser-tft"] = "ok" if estado_tft.get("conectado") else "desconectada"
    corpo["telas"] = dict(_tft_estado)
    if TFT_TRANSPORTE == "serial":
        corpo["porta"] = _estado_porta_health()

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
    # Estado que este adapter acabou de comandar: a tela do slot muda AGORA,
    # sem esperar o `carregado` — que pode levar segundos.
    _atualizar_slot(req.dispenser_id, status="carregando", medicamento=req.medicamento,
                    sku=req.sku, categoria=req.categoria, quantidade_alvo=req.quantidade,
                    quantidade_dispensada=0, os_id=req.os_id)
    _espelhar_slot(req.dispenser_id)
    return _resposta(resultado)


@app.post("/comandos/dispensar")
async def cmd_dispensar(req: ComandoDispensarReq):
    logger.info("[CMD] DISPENSAR D%d ← OS %s", req.dispenser_id, req.os_id)
    resultado = await _enviar(
        "dispensar",
        {"dispenser_id": req.dispenser_id, "os_id": req.os_id,
         "injetar_falha": req.injetar_falha},
    )
    _atualizar_slot(req.dispenser_id, status="dispensando", os_id=req.os_id)
    _espelhar_slot(req.dispenser_id)
    return _resposta(resultado)


@app.post("/comandos/limpar")
async def cmd_limpar(req: ComandoLimparReq):
    logger.info("[CMD] LIMPAR D%d por '%s'", req.dispenser_id, req.solicitado_por)
    resultado = await _enviar(
        "limpar",
        {"dispenser_id": req.dispenser_id, "solicitado_por": req.solicitado_por},
    )
    _atualizar_slot(req.dispenser_id, status="limpando")
    _espelhar_slot(req.dispenser_id)
    return _resposta(resultado)


class EstadoCelulaReq(BaseModel):
    """O que as 8 telas precisam saber da célula: há trava, e de quem é.

    `trava_resumo` é a CATEGORIA da divergência, com teto de TRAVA_RESUMO_MAX
    — nunca o motivo formatado do central. O adapter corta em 48 também.
    """
    trava_ativa: bool
    trava_slot_id: Optional[int] = None
    os_id: str = ""
    trava_resumo: str = ""


@app.post("/comandos/estado-celula")
async def cmd_estado_celula(req: EstadoCelulaReq):
    """`estado_celula` vai SÓ para a placa das telas.

    A placa dos mecanismos não tem tela e não precisa saber da trava — quem
    para a dispensa é o orquestrador, não ela. Responde 200 mesmo com as telas
    fora do ar: o corpo diz o que aconteceu, e quem chama (o central, uma vez,
    com timeout curto) só loga. Uma trava não pode ficar mais lenta por causa
    de uma tela.
    """
    resumo = _resumo_trava(req.trava_resumo)
    logger.info("[CMD] ESTADO-CELULA trava=%s slot=%s os=%s '%s'",
                req.trava_ativa, req.trava_slot_id, req.os_id, resumo)
    tarefa = _enviar_tft("estado_celula", {
        "trava_ativa":   req.trava_ativa,
        "trava_slot_id": req.trava_slot_id,
        "os_id":         req.os_id,
        "trava_resumo":  resumo,
    })
    if tarefa is None:
        return {"ok": True, "telas": "desligadas"}
    entregue = await tarefa
    return {"ok": True, "telas": "ok" if entregue else "falha"}


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

    # As telas primeiro — agendado, não esperado: o `slot` sai em paralelo com
    # o POST ao central, e uma placa de telas fora do ar não segura o evento.
    _espelhar_evento_no_slot(payload)

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


# ── Comandos da porta NR-12 (Central → Adapter → placa das telas → porta) ────

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
    resultado = await _enviar_porta("porta_negar", {"motivo": req.motivo[:60]})
    return {"ok": True, "porta": resultado}


@app.post("/comandos/porta/estado")
async def cmd_porta_estado():
    resultado = await _enviar_porta("porta_estado", {})
    return {"ok": True, "porta": resultado}
