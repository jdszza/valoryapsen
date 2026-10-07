"""
APSEN - Vision Adapter v1.0
Bridge bidirecional entre o Computador Central e o Vision Simulator.

Câmeras dos Dispensers (duas, uma por fileira): leitura de QR Code, DataMatrix
e código de barras.
  → Identificam e validam o produto carregado em cada slot.
  → QUAL das duas olha o slot é decidido pelo vision-simulator, a partir do
    slot_id (ver `camera_do_slot`): o lado é característica física da bancada.
    O adapter manda só o slot e recebe de volta o campo `camera` preenchido.

Câmera da Mesa (a da balança): detecção de posição, contagem visual e validação
da operação.
  → Confirma que os produtos foram dispensados corretamente na mesa de coleta,
    onde fica a célula de carga HX711.

Fluxo entrada (← Central):
  POST /comandos/capturar/dispenser  → solicita leitura da câmera do lado do slot
  POST /comandos/capturar/mesa       → solicita leitura da câmera da mesa

Fluxo saída (← Vision Simulator):
  POST /eventos  → normaliza e encaminha para central POST /api/v1/eventos/visao

Contrato de normalização:
  Eventos recebidos do simulator são repassados ao Central sem alteração de schema,
  garantindo que o adapter possa ser substituído por hardware real sem mudar o Central.

Com as câmeras reais (VISAO_*_FONTE=estacao):
  - a estação da MESA fala o mesmo contrato do simulador — VISION_SIM_URL passa
    a apontar para ela e nada mais muda;
  - a estação dos DISPENSERS não recebe comando: ela julga cada zona sem parar
    contra um catálogo que busca aqui. O adapter serve esse catálogo (montado
    com o que o central mandou validar na OS corrente), lê o veredito no
    `/api/estado` dela e emite o `leitura_dispenser_*`. Ver `_leitura` e
    `ponte_dispensers.py`.
"""
import asyncio
import logging
import math
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

import ponte_dispensers as ponte

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [VISION-ADAPTER] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)



def _cfg_float(nome: str, padrao: float, minimo: float, maximo: float) -> float:
    """Número do ambiente com faixa. Nunca derruba o adapter.

    `float(os.getenv(...))` cru fazia um `2,5` no `.env` — que é como se digita
    em pt-BR — derrubar o adapter NO IMPORT, em laço de restart, com o
    traceback apontando para este arquivo e não para o `.env`. Vírgula é aceita
    como decimal; vazio, lixo ou fora da faixa caem no default com ERROR, que
    diz qual variável e qual valor.
    """
    bruto = os.getenv(nome)
    if bruto is None:
        return padrao
    try:
        valor = float(bruto.strip().replace(",", "."))
    except ValueError:
        logger.error("%s=%r não é número — usando %s.", nome, bruto, padrao)
        return padrao
    if not (minimo <= valor <= maximo) or math.isnan(valor):
        logger.error("%s=%r fora da faixa %s..%s — usando %s.",
                     nome, bruto, minimo, maximo, padrao)
        return padrao
    return valor


CENTRAL_URL       = os.getenv("CENTRAL_URL",        "http://central-computer:8000")
VISION_SIM_URL    = os.getenv("VISION_SIM_URL",     "http://vision-simulator:8202")
TIMEOUT_CMD       = _cfg_float("TIMEOUT_CMD", 20.0, 0.5, 120.0)
TIMEOUT_EVENT     = _cfg_float("TIMEOUT_EVENT", 5.0, 0.5, 120.0)
# Faixa de slots da célula — a MESMA env var do central e dos simuladores.
NUM_SLOTS         = int(os.getenv("NUM_SLOTS", "8"))


# ── De onde vem cada câmera ───────────────────────────────────────────────────
#
# "simulador" é o vision-simulator; "estacao" é a estação de visão real. A
# fonte da mesa NÃO muda o caminho do comando — com "estacao", o
# /comandos/capturar/mesa continua indo para VISION_SIM_URL, que aí é o
# endereço da estação. O que ela decide é qual telemetria é verdadeira: o
# vision-simulator segue no ar como rollback e segue mandando temperatura das
# três câmeras a cada 60 s. Nenhuma estação real emite telemetria hoje, então
# toda telemetria de uma câmera que já é real foi inventada pelo simulador — e
# o central a gravaria em `leituras_sensores` ao lado do dado de verdade.
FONTES_VALIDAS = ("simulador", "estacao")


def _fonte(nome: str) -> str:
    """Valor inválido cai em "simulador" com ERROR, nunca derruba o adapter.

    "simulador" é o lado que não descarta nada: errar a grafia deixa passar a
    telemetria inventada, que é visível no histórico, em vez de calar uma
    câmera.
    """
    bruto = os.getenv(nome, "simulador")
    valor = bruto.strip().lower()
    if valor not in FONTES_VALIDAS:
        logger.error("%s=%r inválido (esperado: %s) — usando 'simulador'.",
                     nome, bruto, " | ".join(FONTES_VALIDAS))
        return "simulador"
    return valor


VISAO_MESA_FONTE      = _fonte("VISAO_MESA_FONTE")
VISAO_DISPENSER_FONTE = _fonte("VISAO_DISPENSER_FONTE")

# ── Quem está do outro lado de VISION_SIM_URL ─────────────────────────────────
#
# A fonte da mesa e o endereço são duas variáveis, e esquecer uma delas passa
# calado: com VISAO_MESA_FONTE=estacao e VISION_SIM_URL no default, a contagem
# SORTEADA do simulador entra no Triple Check como se fosse a câmera real. Os
# dois serviços dizem quem são no `/ping`, e o adapter confere.
SERVICO_POR_FONTE = {"estacao": "apsen-vision-station",
                     "simulador": "apsen-vision-simulator"}
_INTERVALO_IDENTIDADE_S = 30.0
# confere: None = ainda não se sabe (upstream fora do ar); True/False = medido.
_identidade_mesa: dict = {"esperado": SERVICO_POR_FONTE[VISAO_MESA_FONTE],
                          "recebido": None, "confere": None}


def _telemetria_inventada(componente) -> bool:
    """A telemetria deste componente vem de uma câmera que já é real?"""
    if componente == "camera_mesa":
        return VISAO_MESA_FONTE == "estacao"
    if componente in ("camera_dispenser_esq", "camera_dispenser_dir"):
        return VISAO_DISPENSER_FONTE == "estacao"
    if componente == "processador_visao":
        # O processador simulado roda as três câmeras: com qualquer uma delas
        # real, a temperatura dele não descreve mais nada que exista.
        return "estacao" in (VISAO_MESA_FONTE, VISAO_DISPENSER_FONTE)
    return False


# ── A estação dos dispensers (VISAO_DISPENSER_FONTE=estacao) ──────────────────
#
# A lógica pura (etiquetas, catálogo, tradução) está em `ponte_dispensers.py`.
# Aqui ficam o estado da OS corrente, os endpoints que as estações chamam e a
# espera pela leitura.
VISAO_DISP_ESQ_URL = os.getenv("VISAO_DISP_ESQ_URL", "http://host.docker.internal:8301").rstrip("/")
VISAO_DISP_DIR_URL = os.getenv("VISAO_DISP_DIR_URL", "http://host.docker.internal:8302").rstrip("/")
# TEM de ser igual ao `backend.intervalo_catalogo` do parametros.json das duas
# estações: é com ele que se calcula quando a estação já buscou o catálogo
# novo. Menor que o dela, a leitura sai julgada pelo catálogo da OS ANTERIOR.
VISAO_DISP_INTERVALO_CATALOGO_S = _cfg_float("VISAO_DISP_INTERVALO_CATALOGO_S", 2.0, 0.5, 60.0)
# Folga depois de o catálogo novo valer: a estação ainda precisa de alguns
# frames com ele para o veredito da zona assentar.
VISAO_DISP_ASSENTAMENTO_S = _cfg_float("VISAO_DISP_ASSENTAMENTO_S", 2.0, 0.0, 30.0)
# Teto para emitir o evento. O central espera TIMEOUT_VISAO_DISPENSER (30 s):
# emitir uma falha antes disso é o que deixa o motivo chegar ao histórico, em
# vez de um timeout sem causa.
VISAO_DISP_PRAZO_S = _cfg_float("VISAO_DISP_PRAZO_S", 20.0, 2.0, 120.0)
# Janela de estabilidade: quantas amostras de `momento`s DISTINTOS o veredito
# precisa (o `momento` da estação tem resolução de 1 s; mínimo 3). Ver
# `ponte.decidir`.
VISAO_DISP_JANELA_S = _cfg_float("VISAO_DISP_JANELA_S", 3.0, 1.0, 30.0)
AMOSTRAS_JANELA = max(3, math.ceil(VISAO_DISP_JANELA_S))
_TIMEOUT_VISAO_DISPENSER_CENTRAL = _cfg_float("TIMEOUT_VISAO_DISPENSER", 30.0, 1.0, 600.0)
if VISAO_DISP_PRAZO_S >= _TIMEOUT_VISAO_DISPENSER_CENTRAL:
    logger.warning("VISAO_DISP_PRAZO_S=%.1f não é menor que o TIMEOUT_VISAO_DISPENSER "
                   "do central (%.1f): a falha com motivo chegaria depois de o central "
                   "desistir — no histórico ficaria só um timeout sem causa.",
                   VISAO_DISP_PRAZO_S, _TIMEOUT_VISAO_DISPENSER_CENTRAL)


def _modo_nao_cadastrado() -> str:
    bruto = os.getenv("VISAO_DISP_NAO_CADASTRADO", ponte.NAO_CADASTRADO_FALHA)
    valor = bruto.strip().lower()
    if valor not in ponte.MODOS_NAO_CADASTRADO:
        logger.error("VISAO_DISP_NAO_CADASTRADO=%r inválido (esperado: %s) — usando %r.",
                     bruto, " | ".join(ponte.MODOS_NAO_CADASTRADO),
                     ponte.NAO_CADASTRADO_FALHA)
        return ponte.NAO_CADASTRADO_FALHA
    return valor


# Código lido na zona que não está na tabela de etiquetas: falha (default) ou
# divergência. Ver `ponte.traduzir`.
VISAO_DISP_NAO_CADASTRADO = _modo_nao_cadastrado()
_SONDAGEM_S = 0.5
_TIMEOUT_ESTADO_S = 2.0
# A cache de nome → SKU do central. Recarrega por idade e quando um nome não é
# achado (medicamento cadastrado depois do último GET).
_VALIDADE_SKUS_S = 60.0

VISAO_ETIQUETAS_ARQ = os.getenv(
    "VISAO_ETIQUETAS_ARQ", str(Path(__file__).with_name("etiquetas.json")))
ETIQUETAS = ponte.carregar_etiquetas(VISAO_ETIQUETAS_ARQ)

# Estado da OS corrente. `_t_catalogo` é quando o CONTEÚDO servido mudou pela
# última vez (relógio do adapter): é dele que se conta o intervalo até as
# estações terem buscado o catálogo novo.
_os_corrente: Optional[str] = None
_slots_os: dict[int, str] = {}
_catalogo: dict = ponte.montar_catalogo({}, ETIQUETAS)
_t_catalogo: float = time.monotonic()

_skus_central: dict[str, str] = {}
_t_skus: Optional[float] = None

# Leituras em andamento. Guardadas para a task não ser recolhida pelo GC no
# meio — o asyncio só guarda referência fraca de quem foi `create_task`.
_leituras: set = set()
# Tarefas de fundo do lifespan (espera pelo upstream, conferência de identidade).
_tarefas_fundo: set = set()

# Quando cada estação dos dispensers buscou o catálogo pela rota do SEU lado
# (`/estacoes/{lado}/api/visao/catalogo`), no relógio do adapter. É a prova de
# que ela tem o catálogo desta OS — o relógio sozinho só supõe. Lado ausente =
# estação ainda na URL antiga: vale o critério de antes, com um aviso.
_ultima_busca: dict[str, float] = {}
_avisou_url_antiga: set[str] = set()

# ── Estação da mesa: a memória da OS e a injeção ──────────────────────────────
# OS cujas leituras a estação já emitiu (LRU). Se ela tem leitura aqui mas a
# estação não a conhece mais (`/status`), a estação perdeu o acumulado.
_MAX_OS_MEMORIA = 50
_mesa_registrou: "OrderedDict[str, bool]" = OrderedDict()
_ressincronizar: set[tuple[str, int]] = set()
# Injeção feita pelo ADAPTER sobre o evento verdadeiro (ver `capturar_mesa`).
_injecao_mesa: dict[tuple[str, int], str] = {}


def _relogio() -> float:
    """Relógio do adapter. Função, e não `time.monotonic` direto, para a suíte."""
    return time.monotonic()


async def _dormir(segundos: float) -> None:
    await asyncio.sleep(segundos)


def _registrar_na_os(os_id: str, slot_id: int, medicamento: str) -> None:
    """Grava slot → medicamento da OS corrente e refaz o catálogo servido.

    OS diferente zera o mapa: o catálogo é SÓ da OS corrente.
    """
    global _os_corrente, _slots_os, _catalogo, _t_catalogo
    if os_id != _os_corrente:
        _os_corrente = os_id
        _slots_os = {}
    _slots_os[slot_id] = medicamento
    novo = ponte.montar_catalogo(_slots_os, ETIQUETAS)
    if novo != _catalogo:
        _catalogo = novo
        _t_catalogo = _relogio()
        logger.info("[CATALOGO] OS %s: %s", os_id, ", ".join(
            f"D{m['dispenser']}={m['nome']}" for m in novo["medicamentos"]
            if m["dispenser"] < ponte.BASE_FANTASMA) or "(nenhum slot com etiqueta)")
        for item in novo["incompletos"]:
            logger.warning("[CATALOGO] D%s (%s) fora do catálogo: %s",
                           item["dispenser"], item["nome"], item["motivo"])


_client: httpx.AsyncClient | None = None


async def _wait_for_upstream(name: str, url: str, retries: int = 30, interval: float = 2.0):
    """Só LOGA quando o upstream aparece. Roda em segundo plano: na célula
    montada o Docker sobe ANTES das estações do host, e esperar aqui segurava o
    adapter sem servir por até ~150 s — o compose o marcava unhealthy e não
    subia o erp-simulator."""
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


async def _conferir_upstream() -> Optional[bool]:
    """GET {VISION_SIM_URL}/ping e compara `service` com o que a fonte da mesa
    espera. Devolve o resultado (None = upstream fora, não se sabe)."""
    esperado = SERVICO_POR_FONTE[VISAO_MESA_FONTE]
    try:
        r = await _client.get(VISION_SIM_URL + "/ping", timeout=3.0)
        servico = r.json().get("service") if r.status_code < 300 else None
    except Exception:
        servico = None
    if servico is None:
        _identidade_mesa.update(recebido=None, confere=None)
        return None
    confere = servico == esperado
    if not confere and _identidade_mesa.get("recebido") != servico:
        certo = next((f for f, s in SERVICO_POR_FONTE.items() if s == servico), None)
        logger.error("[IDENTIDADE] VISAO_MESA_FONTE=%s espera %r, mas VISION_SIM_URL=%s "
                     "responde como %r.%s", VISAO_MESA_FONTE, esperado, VISION_SIM_URL,
                     servico, f" Corrija para VISAO_MESA_FONTE={certo} ou aponte "
                     f"VISION_SIM_URL para o {esperado}." if certo else "")
    elif confere and _identidade_mesa.get("confere") is not True:
        logger.info("[IDENTIDADE] VISION_SIM_URL=%s é o %s, como VISAO_MESA_FONTE=%s "
                    "pede.", VISION_SIM_URL, servico, VISAO_MESA_FONTE)
    _identidade_mesa.update(recebido=servico, confere=confere)
    return confere


async def _vigiar_upstream() -> None:
    while True:
        try:
            await _conferir_upstream()
        except Exception:
            logger.exception("[IDENTIDADE] erro ao conferir o upstream.")
        await asyncio.sleep(_INTERVALO_IDENTIDADE_S)


def _em_fundo(coro) -> None:
    tarefa = asyncio.create_task(coro)
    _tarefas_fundo.add(tarefa)
    tarefa.add_done_callback(_tarefas_fundo.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient()
    logger.info("[STARTUP] httpx.AsyncClient criado")
    logger.info("[STARTUP] câmera da mesa: %s | câmeras dos dispensers: %s | "
                "comandos → %s", VISAO_MESA_FONTE, VISAO_DISPENSER_FONTE,
                VISION_SIM_URL)
    if VISAO_DISPENSER_FONTE == "estacao":
        logger.info("[STARTUP] estações dos dispensers: esq=%s dir=%s | %d "
                    "etiqueta(s) | catálogo a cada %.1f s + %.1f s de assentamento",
                    VISAO_DISP_ESQ_URL, VISAO_DISP_DIR_URL, len(ETIQUETAS),
                    VISAO_DISP_INTERVALO_CATALOGO_S, VISAO_DISP_ASSENTAMENTO_S)
    # Nada aqui BLOQUEIA o yield: o adapter serve /ping desde já.
    _em_fundo(_wait_for_upstream("vision-simulator", VISION_SIM_URL + "/ping"))
    _em_fundo(_vigiar_upstream())
    yield
    for tarefa in list(_tarefas_fundo):
        tarefa.cancel()
    await _client.aclose()
    logger.info("[SHUTDOWN] httpx.AsyncClient encerrado")


app = FastAPI(title="APSEN Vision Adapter v1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Pydantic Models ────────────────────────────────────────────────────────────

class CapturarDispenserReq(BaseModel):
    """Comando do Central: fotografar o slot X com a câmera do lado dele.

    Não há campo `camera`: o simulador (ou, amanhã, o driver do hardware)
    deriva o lado do próprio slot. Deixar o central escolher a câmera abriria
    a chance de pedir a leitura de D7 para a câmera da esquerda.
    """
    slot_id: int
    os_id: str
    sku_esperado: str = ""
    medicamento_esperado: str = ""
    quantidade_esperada: int = 0
    # Campo de DEMONSTRAÇÃO, ausente no caminho normal (ver
    # `central-computer/injecao.py`). Com o simulador, o adapter repassa como
    # veio. Com a estação REAL, quem executa é o adapter, reescrevendo o evento
    # verdadeiro (`_injetar_no_evento_dispenser`): a estação não recebe comando.
    injetar_falha: Optional[str] = None


class CapturarMesaReq(BaseModel):
    """Comando do Central: fotografar a mesa (balança) após dispensa no slot X."""
    slot_id: int
    os_id: str
    quantidade_esperada: int = 0
    posicao_x: float = 0.0
    posicao_y: float = 0.0
    # Informativos (o orquestrador manda para o log da estação): os slots que
    # esta foto confere e a quantidade só do slot. Repassados como vieram.
    slots_cobertos: Optional[list[int]] = None
    quantidade_slot: Optional[int] = None
    # Campo de DEMONSTRAÇÃO, ausente no caminho normal (ver
    # `central-computer/injecao.py`). Com o simulador, repassado como veio. Com
    # a estação REAL, ele NÃO segue: a estação injetaria sem medir e sem
    # registrar o total, e o slot seguinte veria os dois slots juntos. O adapter
    # guarda o pedido e reescreve o evento verdadeiro (ver `capturar_mesa`).
    injetar_falha: Optional[str] = None


class EventoVisionReq(BaseModel):
    """Evento recebido do Vision Simulator."""
    tipo: str
    camera: str                    # "dispenser_esq" | "dispenser_dir" | "mesa"
    slot_id: Optional[int] = None
    os_id: Optional[str] = None

    model_config = ConfigDict(extra="allow")


# ── Helpers HTTP ───────────────────────────────────────────────────────────────

async def _post_sim(path: str, payload: dict, timeout: float = TIMEOUT_CMD) -> dict:
    """Envia comando ao vision-simulator. Lança HTTPException em falha."""
    url = VISION_SIM_URL + path
    try:
        r = await _client.post(url, json=payload, timeout=timeout)
        if r.status_code >= 300:
            raise HTTPException(502, f"Vision simulator retornou {r.status_code}: {r.text[:200]}")
        return r.json()
    except httpx.RequestError as exc:
        raise HTTPException(503, f"Vision simulator indisponível: {exc}")


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
    url = CENTRAL_URL + "/api/v1/eventos/visao"
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


# ── Leitura de SKU na estação dos dispensers ──────────────────────────────────

async def _get_json(url: str, timeout: float):
    r = await _client.get(url, timeout=timeout)
    if r.status_code >= 300:
        raise RuntimeError(f"{url} → HTTP {r.status_code}")
    return r.json()


async def _sku_do_central(nome) -> Optional[str]:
    """O SKU que o CENTRAL dá a este nome, de `GET /medicamentos`.

    O "sku" da estação é o código da etiqueta (`MED-004`), que ninguém no
    central reconhece; a trava de SKU mostra `lido=<sku_lido>` ao supervisor,
    então o que vai ali tem de ser o SKU do catálogo do central. Central fora
    do ar devolve None e quem chama cai no código da etiqueta.
    """
    global _skus_central, _t_skus
    chave = ponte.normalizar_nome(nome)
    velha = _t_skus is None or _relogio() - _t_skus > _VALIDADE_SKUS_S
    if velha or chave not in _skus_central:
        try:
            linhas = await _get_json(CENTRAL_URL + "/medicamentos", 3.0)
            _skus_central = {ponte.normalizar_nome(l.get("nome")): l.get("sku")
                             for l in linhas if l.get("nome") and l.get("sku")}
            _t_skus = _relogio()
        except Exception as exc:
            logger.warning("[PONTE] catálogo do central indisponível (%s) — "
                           "sku_lido vai com o código da etiqueta.", exc)
    return _skus_central.get(chave)


def _evento_dispenser(tipo: str, req: "CapturarDispenserReq", camera: str, *,
                      motivo: Optional[str] = None, linha: Optional[dict] = None,
                      sku_lido: Optional[str] = None, confianca: float = 0.0,
                      amostras: Optional[dict] = None) -> dict:
    """O evento no contrato que `_handle_evento_visao` e o passo 3b já leem."""
    evento = {
        "tipo":           tipo,
        "camera":         camera,
        "slot_id":        req.slot_id,
        "os_id":          req.os_id,
        "sku_esperado":   req.sku_esperado,
        "match_sku":      tipo == "leitura_dispenser_ok",
        "confianca":      confianca,
        "falha_injetada": False,
        "ts":             datetime.now(timezone.utc).isoformat(),
        "fonte":          "estacao",
    }
    if motivo:
        evento["motivo"] = motivo
    if amostras is not None:
        evento["amostras"] = amostras
    if linha is not None:
        evento.update({
            "medicamento_lido": linha.get("medicamento"),
            "sku_lido":         sku_lido,
            "codigo_etiqueta":  linha.get("sku"),
            "veredito_estacao": linha.get("veredito"),
            "estacao":          linha.get("estacao"),
            "momento_leitura":  linha.get("momento"),
        })
    return evento


async def _evento_das_amostras(req: "CapturarDispenserReq", camera: str,
                               amostras: list[dict]) -> dict:
    tipo, motivo, linha, contagens = ponte.decidir(
        amostras, req.medicamento_esperado, VISAO_DISP_NAO_CADASTRADO)
    if motivo:
        return _evento_dispenser(tipo, req, camera, motivo=motivo, linha=linha,
                                 amostras=contagens)
    nome = linha.get("medicamento")
    if not nome:
        sku_lido = ponte.SKU_DESCONHECIDO
    else:
        sku_lido = (await _sku_do_central(nome)) or linha.get("sku") or nome
    try:
        confianca = max(0.0, min(1.0, float(linha.get("confianca") or 0.0)))
    except (TypeError, ValueError):
        confianca = 0.0
    return _evento_dispenser(tipo, req, camera, linha=linha, sku_lido=sku_lido,
                             confianca=confianca, amostras=contagens)


def _lado(camera: str) -> str:
    return "esq" if camera == ponte.CAM_ESQ else "dir"


def _estacao_tem_catalogo_atual(camera: str) -> bool:
    """A estação deste lado buscou o catálogo DEPOIS de o conteúdo mudar?

    O relógio sozinho só SUPÕE: a estação engole timeout e catálogo recusado
    sem uma linha de log e segue julgando pelo da OS anterior — e aí o
    medicamento certo desta OS, que na anterior era de outro slot, sai
    ERRO_POSICAO. A busca pela rota do lado é a prova. Estação que nunca usou a
    rota nova (URL antiga no parametros.json) cai no critério de antes.
    """
    lado = _lado(camera)
    ultima = _ultima_busca.get(lado)
    if ultima is None:
        if lado not in _avisou_url_antiga:
            _avisou_url_antiga.add(lado)
            logger.warning("[PONTE] a estação %s nunca buscou o catálogo em "
                           "/estacoes/%s/api/visao/catalogo — confira backend.url no "
                           "parametros.json dela. Sem a prova da busca, vale só o "
                           "relógio.", lado, lado)
        return True
    return ultima > _t_catalogo


async def _leitura(req: "CapturarDispenserReq", camera: str) -> dict:
    """Espera a estação julgar o slot com o catálogo DESTA OS, e traduz.

    O frescor é o que decide se a leitura vale. A estação só reavalia com o
    catálogo novo depois de buscá-lo, o que acontece a cada
    `VISAO_DISP_INTERVALO_CATALOGO_S`. Ler o `/api/estado` na hora do comando
    devolveria o veredito dado com o catálogo da OS ANTERIOR — "OK" para o
    medicamento que estava certo na OS passada. Então a leitura só vale quando:

    - o catálogo atual está servido há pelo menos intervalo + assentamento
      (relógio do adapter: é ele que sabe quando o conteúdo mudou); e
    - o `momento` da linha do slot avançou esse mesmo tanto desde o comando
      (relógio da ESTAÇÃO: compará-lo com o nosso confundiria fuso e
      relógio desacertado com câmera parada).

    Se o catálogo muda no meio (chegou o comando de outro slot da mesma OS), a
    contagem recomeça. Re-scan da trava de SKU: o catálogo não mudou, então só
    o assentamento conta.

    Além do relógio, a estação do lado tem de ter BUSCADO o catálogo depois de
    ele mudar (`_estacao_tem_catalogo_atual`). E a decisão não sai de uma
    amostra: o `/api/estado` é por frame, então colhem-se `AMOSTRAS_JANELA`
    linhas de `momento`s distintos e `ponte.decidir` julga a janela.
    """
    motivo = ponte.motivo_do_incompleto(_catalogo, req.slot_id)
    if motivo is not None:
        # A estação não tem como reconhecer este slot — perguntar a ela só
        # gastaria o prazo para devolver um veredito sem sentido.
        codigo = ("sem_etiqueta_cadastrada" if motivo == ponte.MOTIVO_SEM_ETIQUETA
                  else "medicamento_repetido_na_os")
        logger.warning("[PONTE] D%d (%s) sem conferência: %s.",
                       req.slot_id, req.medicamento_esperado, motivo)
        return _evento_dispenser("leitura_dispenser_falha", req, camera, motivo=codigo)

    url = (VISAO_DISP_ESQ_URL if camera == ponte.CAM_ESQ else VISAO_DISP_DIR_URL) + "/api/estado"
    folga = VISAO_DISP_INTERVALO_CATALOGO_S + VISAO_DISP_ASSENTAMENTO_S
    fim = _relogio() + VISAO_DISP_PRAZO_S
    t_ref = None
    m_ref = None
    exigido = 0
    alcancou = viu_linha = False
    amostras: list[dict] = []

    while True:
        try:
            estado = await _get_json(url, _TIMEOUT_ESTADO_S)
            alcancou = True
        except Exception as exc:
            logger.debug("[PONTE] %s inacessível: %s", url, exc)
            estado = None
        linha = ponte.linha_do_slot(estado, req.slot_id)
        viu_linha = viu_linha or linha is not None
        m = ponte.momento(linha)

        if t_ref != _t_catalogo:
            # Primeira volta, ou o catálogo mudou durante a espera: a
            # referência de tempo da estação recomeça daqui.
            t_ref = _t_catalogo
            ja_valia = _relogio() - t_ref >= folga
            exigido = math.ceil(VISAO_DISP_ASSENTAMENTO_S if ja_valia else folga)
            m_ref = m
            amostras = []
        elif m_ref is None:
            m_ref = m

        if (m is not None and m_ref is not None
                and _relogio() - _t_catalogo >= folga
                and (m - m_ref).total_seconds() >= exigido
                and _estacao_tem_catalogo_atual(camera)
                and (not amostras or ponte.momento(amostras[-1]) != m)):
            amostras.append(linha)
            if len(amostras) >= AMOSTRAS_JANELA:
                return await _evento_das_amostras(req, camera, amostras)

        if _relogio() >= fim:
            break
        await _dormir(_SONDAGEM_S)

    if not alcancou:
        motivo = "camera_indisponivel"
    elif not _estacao_tem_catalogo_atual(camera):
        motivo = "estacao_sem_catalogo_atual"
    elif not viu_linha:
        motivo = "zona_nao_calibrada"
    else:
        motivo = "camera_sem_imagem"
    logger.warning("[PONTE] D%d sem leitura em %.0f s: %s.",
                   req.slot_id, VISAO_DISP_PRAZO_S, motivo)
    return _evento_dispenser("leitura_dispenser_falha", req, camera, motivo=motivo)


async def _ler_e_emitir(req: "CapturarDispenserReq", camera: str) -> None:
    """EXATAMENTE um evento por comando, em todo caminho.

    Nenhum, e o central espera os 30 s inteiros sem motivo no histórico; dois,
    e ele decide duas vezes sobre o mesmo slot. Por isso a exceção também vira
    evento — de falha, que não trava nada.
    """
    try:
        evento = await _leitura(req, camera)
    except Exception:
        logger.exception("[PONTE] erro ao ler D%d na estação.", req.slot_id)
        evento = _evento_dispenser("leitura_dispenser_falha", req, camera,
                                   motivo="erro_interno")
    if req.injetar_falha:
        evento = _injetar_no_evento_dispenser(evento, req.injetar_falha)
    logger.info("[PONTE] D%d → %s%s", req.slot_id, evento["tipo"],
                f" ({evento['motivo']})" if evento.get("motivo") else "")
    if not await _post_central(evento):
        logger.warning("[FWD] Leitura D%d (%s) não chegou ao Central.",
                       req.slot_id, evento["tipo"])


# ── Injeção de falha com as câmeras REAIS ─────────────────────────────────────
#
# Com o simulador, o campo `injetar_falha` atravessa o adapter sem
# interpretação, e quem executa é o simulador. Com a estação real não há quem
# execute: a dos dispensers não recebe comando, e a da mesa, com
# ACEITAR_INJECAO=1, devolveria um número SEM medir nem registrar o total —
# o slot seguinte veria os dois slots juntos, e a trava cairia no slot errado.
# Então, com a câmera real, quem injeta é o ADAPTER, reescrevendo o evento
# VERDADEIRO. É uma mudança deliberada do "o adapter não interpreta o campo":
# a alternativa era o botão da demonstração não fazer nada, calado.
# Os valores são os do simulador, para a tela mostrar o mesmo que se armou.
SKU_INJETADO = "APSEN-INJETADO-000"
INJECAO_SKU_DISPENSER = "sku_dispenser"
INJECAO_FALHA_LEITURA_DISPENSER = "falha_leitura_dispenser"
INJECAO_DIVERGENCIA_MESA = "divergencia_mesa"


def _injetar_no_evento_dispenser(evento: dict, tipo: str) -> dict:
    if tipo == INJECAO_SKU_DISPENSER:
        novo = {**evento, "tipo": "leitura_dispenser_divergencia",
                "sku_lido": SKU_INJETADO, "match_sku": False, "falha_injetada": True}
        novo.pop("motivo", None)
    elif tipo == INJECAO_FALHA_LEITURA_DISPENSER:
        novo = {**evento, "tipo": "leitura_dispenser_falha", "motivo": "injetada",
                "match_sku": False, "confianca": 0.0, "falha_injetada": True}
    else:
        logger.warning("[INJECAO] injetar_falha=%r desconhecido para a câmera do "
                       "dispenser — ignorado.", tipo)
        return evento
    logger.warning("[INJECAO] D%s: %s injetada sobre a leitura real (%s).",
                   evento.get("slot_id"), tipo, evento.get("tipo"))
    return novo


def _injetar_no_evento_mesa(payload: dict, tipo: str) -> dict:
    """Uma a MAIS sobre a contagem verdadeira — a mesma regra do simulador.

    Falha (a estação não mediu) é encaminhada intacta: não há o que injetar
    sobre o que não foi medido.
    """
    if payload.get("tipo") not in ("leitura_mesa_ok", "leitura_mesa_divergencia"):
        logger.warning("[INJECAO] mesa D%s: %s pedida, mas a leitura real foi %s — "
                       "encaminhada sem injeção.", payload.get("slot_id"), tipo,
                       payload.get("tipo"))
        return payload
    esperado = payload.get("quantidade_esperada")
    try:
        esperado = int(esperado)
    except (TypeError, ValueError):
        logger.warning("[INJECAO] mesa D%s sem quantidade_esperada numérica — "
                       "encaminhada sem injeção.", payload.get("slot_id"))
        return payload
    logger.warning("[INJECAO] mesa D%s: divergência injetada sobre a leitura real "
                   "(detectou %s de %d).", payload.get("slot_id"),
                   payload.get("quantidade_detectada"), esperado)
    return {**payload, "tipo": "leitura_mesa_divergencia",
            "quantidade_detectada_real": payload.get("quantidade_detectada"),
            "quantidade_detectada": esperado + 1, "delta": 1,
            "falha_injetada": True}


def _capturar_na_estacao(req: "CapturarDispenserReq") -> dict:
    """Responde NA HORA e lê em segundo plano: o central espera 5 s pela
    resposta, e a leitura leva o intervalo do catálogo mais o assentamento."""
    if not 1 <= req.slot_id <= NUM_SLOTS:
        raise HTTPException(400, f"slot_id deve ser 1-{NUM_SLOTS}")
    camera = ponte.camera_do_slot(req.slot_id, NUM_SLOTS)
    _registrar_na_os(req.os_id, req.slot_id, req.medicamento_esperado)
    tarefa = asyncio.create_task(_ler_e_emitir(req, camera))
    _leituras.add(tarefa)
    tarefa.add_done_callback(_leituras.discard)
    return {"ok": True, "camera": camera, "slot_id": req.slot_id,
            "msg": f"Lendo dispenser {req.slot_id} na estação {camera}"}


# ── Endpoints de Comandos (Central → Adapter → Simulator) ─────────────────────

@app.get("/ping")
def ping():
    return {"status": "ok", "service": "apsen-vision-adapter"}


@app.get("/health")
async def health():
    """Verifica conectividade com vision-simulator e central-computer — e, com a
    fonte dos dispensers na estação real, com as duas estações."""
    checks = {}
    alvos = [
        ("vision-simulator", VISION_SIM_URL + "/ping"),
        ("central-computer", CENTRAL_URL + "/ping"),
    ]
    if VISAO_DISPENSER_FONTE == "estacao":
        alvos += [("estacao-dispensers-esq", VISAO_DISP_ESQ_URL + "/api/saude"),
                  ("estacao-dispensers-dir", VISAO_DISP_DIR_URL + "/api/saude")]
    for name, url in alvos:
        try:
            r = await _client.get(url, timeout=3.0)
            checks[name] = "ok" if r.status_code < 300 else f"http_{r.status_code}"
        except Exception as exc:
            checks[name] = f"erro: {exc}"
    await _conferir_upstream()
    ok = (all(v == "ok" for v in checks.values())
          and _identidade_mesa["confere"] is not False)
    agora = _relogio()
    resposta = {
        "status": "ok" if ok else "degradado",
        "checks": checks,
        "fontes": {"mesa": VISAO_MESA_FONTE, "dispensers": VISAO_DISPENSER_FONTE},
        "identidade_mesa": dict(_identidade_mesa),
        "comandos_mesa_para": VISION_SIM_URL,
        "etiquetas": {"quantidade": len(ETIQUETAS),
                      "nomes": [e["nome"] for e in ETIQUETAS]},
        "busca_catalogo_idade_s": {
            lado: (round(agora - _ultima_busca[lado], 1) if lado in _ultima_busca
                   else None) for lado in ("esq", "dir")},
    }
    if VISAO_MESA_FONTE == "estacao":
        resposta["estacao_mesa"] = await _status_estacao_mesa()
    return resposta


async def _status_estacao_mesa() -> Optional[dict]:
    """{pronta, fila, envios_falhados} do `/status` da estação da mesa, ou None."""
    try:
        status = await _get_json(VISION_SIM_URL + "/status", 2.0)
    except Exception:
        return None
    cameras = status.get("cameras") or [{}]
    return {"pronta": bool((cameras[0] or {}).get("pronta")),
            "fila": status.get("fila"),
            "envios_falhados": (status.get("envios") or {}).get("falhados")}


@app.post("/comandos/capturar/dispenser")
async def capturar_dispenser(req: CapturarDispenserReq):
    """
    Dispara captura da câmera de dispenser que cobre o slot especificado.
    Valida QR Code / DataMatrix / Código de Barras do produto carregado.
    """
    logger.info(
        "[CMD] CAPTURAR DISPENSER slot=%d | sku=%s | med=%s | qtd=%d | OS=%s",
        req.slot_id, req.sku_esperado, req.medicamento_esperado,
        req.quantidade_esperada, req.os_id,
    )
    if VISAO_DISPENSER_FONTE == "estacao":
        return _capturar_na_estacao(req)
    resultado = await _post_sim(
        "/executar/capturar/dispenser",
        {
            "slot_id":             req.slot_id,
            "os_id":               req.os_id,
            "sku_esperado":        req.sku_esperado,
            "medicamento_esperado": req.medicamento_esperado,
            "quantidade_esperada": req.quantidade_esperada,
            "injetar_falha":       req.injetar_falha,
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
    if _identidade_mesa["confere"] is False:
        # Câmera real contando de mentira é pior que câmera ausente: o 503 vira
        # "comando não aceito" no central, e a contagem sorteada não entra no
        # Triple Check. Identidade DESCONHECIDA (upstream fora) segue como antes.
        raise HTTPException(503, f"VISAO_MESA_FONTE={VISAO_MESA_FONTE} mas "
                                 f"VISION_SIM_URL={VISION_SIM_URL} responde como "
                                 f"{_identidade_mesa['recebido']!r} — corrija o .env "
                                 f"e recrie o vision-adapter.")
    injetar = req.injetar_falha
    if VISAO_MESA_FONTE == "estacao":
        await _conferir_memoria_da_estacao(req.os_id, req.slot_id)
        if injetar:
            if injetar == INJECAO_DIVERGENCIA_MESA:
                _injecao_mesa[(req.os_id, req.slot_id)] = injetar
            else:
                logger.warning("[INJECAO] injetar_falha=%r desconhecido para a câmera "
                               "da mesa — ignorado.", injetar)
            injetar = None
    resultado = await _post_sim(
        "/executar/capturar/mesa",
        {
            "slot_id":           req.slot_id,
            "os_id":             req.os_id,
            "quantidade_esperada": req.quantidade_esperada,
            "posicao_x":         req.posicao_x,
            "posicao_y":         req.posicao_y,
            "slots_cobertos":    req.slots_cobertos,
            "quantidade_slot":   req.quantidade_slot,
            "injetar_falha":     injetar,
        },
    )
    return {"ok": True, "simulador": resultado}


async def _conferir_memoria_da_estacao(os_id: str, slot_id: int) -> None:
    """A estação da mesa ainda conhece esta OS?

    O acumulado dela vive só em memória e expira em VALIDADE_OS_H: um restart
    no meio da OS, ou uma trava esperando supervisor além disso, faz a foto
    seguinte comparar a caixa INTEIRA com o esperado de um slot — "a mais",
    trava. A estação não publica uptime, mas o adapter vê passar toda leitura
    que ela emitiu: se já houve leitura desta OS e o `/status` não a lista mais,
    a memória se perdeu, e o evento desta foto sai com `ressincronizar: true`.
    O comando segue normalmente — é a foto que recria o total. `/status` fora
    do ar não bloqueia nada.
    """
    if os_id not in _mesa_registrou:
        return
    try:
        status = await _get_json(VISION_SIM_URL + "/status", 2.0)
        conhecidas = status.get("acumulado_por_os") or {}
    except Exception as exc:
        logger.info("[MEMORIA] /status da estação da mesa indisponível (%s) — "
                    "sem conferência do acumulado.", exc)
        return
    if str(os_id) not in conhecidas:
        _ressincronizar.add((os_id, slot_id))
        logger.warning("[MEMORIA] a estação da mesa não tem mais o acumulado da OS %s "
                       "(reiniciada ou expirada): a foto de D%d será marcada "
                       "ressincronizar.", os_id, slot_id)


def _lembrar_leitura_mesa(os_id: str) -> None:
    _mesa_registrou[os_id] = True
    _mesa_registrou.move_to_end(os_id)
    while len(_mesa_registrou) > _MAX_OS_MEMORIA:
        _mesa_registrou.popitem(last=False)


# ── O que as estações dos dispensers chamam ───────────────────────────────────

def _catalogo_servido() -> dict:
    """O que a estação recebe: SÓ `medicamentos`.

    `incompletos` é uso interno — a estação imprime uma linha por item dele a
    cada busca (a cada 2 s), e com a maioria dos itens das ordens padrão sem
    etiqueta o console dela virava ruído.
    """
    return {"medicamentos": _catalogo["medicamentos"]}


@app.get("/api/visao/catalogo")
def visao_catalogo():
    """O catálogo da OS corrente, no formato de `Catalogo.de_itens` da estação.

    Sem token, como a estação sempre buscou: ela não manda cabeçalho nenhum.
    Rota antiga, sem lado: fica para o rollback de quem ainda não trocou o
    backend.url (ver `/estacoes/{lado}/...`).
    """
    return _catalogo_servido()


_LADOS = ("esq", "dir")


@app.get("/estacoes/{lado}/api/visao/catalogo")
def visao_catalogo_do_lado(lado: str):
    """A mesma resposta, registrando QUAL estação buscou e quando.

    Cada estação aponta o `backend.url` para `/estacoes/<lado>` — é
    configuração, não código da estação: ela monta a URL como
    `{backend.url}/api/visao/catalogo`.
    """
    if lado not in _LADOS:
        raise HTTPException(404, f"lado {lado!r} desconhecido (esq | dir)")
    _ultima_busca[lado] = _relogio()
    return _catalogo_servido()


@app.post("/estacoes/{lado}/api/visao/estoque")
async def visao_estoque_do_lado(lado: str, request: Request):
    if lado not in _LADOS:
        raise HTTPException(404, f"lado {lado!r} desconhecido (esq | dir)")
    return await visao_estoque(request)


@app.post("/api/visao/estoque")
async def visao_estoque(request: Request):
    """Aceita e descarta o estoque medido pela estação.

    Quem manda no estoque dos dispensers é o central (decisão de set/2026). A
    resposta é 2xx porque a estação reenvia para sempre o que não recebeu 2xx.
    """
    try:
        corpo = await request.json()
    except Exception:
        corpo = {}
    leituras = corpo.get("leituras") if isinstance(corpo, dict) else None
    logger.debug("[ESTOQUE] estação %s publicou %d leitura(s) — descartadas.",
                 (corpo or {}).get("estacao") if isinstance(corpo, dict) else "?",
                 len(leituras) if isinstance(leituras, list) else 0)
    return {"ok": True, "resultados": []}


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

    if tipo == "telemetria" and _telemetria_inventada(payload.get("componente")):
        logger.debug("[EVT] telemetria de %s descartada — a câmera é real e "
                     "quem a mandou foi o simulador.", payload.get("componente"))
        return {"ok": True, "encaminhado": False, "descartado": "telemetria_simulada"}

    if tipo.startswith("leitura_mesa_") and VISAO_MESA_FONTE == "estacao" and os_id:
        chave = (os_id, slot_id)
        if chave in _ressincronizar:
            _ressincronizar.discard(chave)
            payload["ressincronizar"] = True
        injecao = _injecao_mesa.pop(chave, None)
        if injecao:
            payload = _injetar_no_evento_mesa(payload, injecao)
            tipo = payload.get("tipo", tipo)
        if req.tipo in ("leitura_mesa_ok", "leitura_mesa_divergencia"):
            # A estação registrou o total desta OS (é o que ok/divergência
            # significam nela) — mesmo que a injeção tenha reescrito o evento.
            _lembrar_leitura_mesa(os_id)

    log_extra = f"| OS {os_id}" if os_id else ""
    logger.info("[EVT] %-28s ← cam=%-9s slot=%s %s", tipo, camera, slot_id, log_extra)

    ok = await _post_central(payload)
    if not ok:
        logger.warning("[FWD] Evento '%s' cam=%s slot=%s não chegou ao Central.",
                       tipo, camera, slot_id)

    return {"ok": True, "encaminhado": ok}
