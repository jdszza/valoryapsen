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

CENTRAL_URL       = os.getenv("CENTRAL_URL",        "http://central-computer:8000")
VISION_SIM_URL    = os.getenv("VISION_SIM_URL",     "http://vision-simulator:8202")
TIMEOUT_CMD       = float(os.getenv("TIMEOUT_CMD",  "20"))
TIMEOUT_EVENT     = float(os.getenv("TIMEOUT_EVENT", "5"))
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
VISAO_DISP_INTERVALO_CATALOGO_S = float(os.getenv("VISAO_DISP_INTERVALO_CATALOGO_S", "2"))
# Folga depois de o catálogo novo valer: a estação ainda precisa de alguns
# frames com ele para o veredito da zona assentar.
VISAO_DISP_ASSENTAMENTO_S = float(os.getenv("VISAO_DISP_ASSENTAMENTO_S", "2"))
# Teto para emitir o evento. O central espera TIMEOUT_VISAO_DISPENSER (30 s):
# emitir uma falha antes disso é o que deixa o motivo chegar ao histórico, em
# vez de um timeout sem causa.
VISAO_DISP_PRAZO_S = float(os.getenv("VISAO_DISP_PRAZO_S", "20"))
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
    logger.info("[STARTUP] câmera da mesa: %s | câmeras dos dispensers: %s | "
                "comandos → %s", VISAO_MESA_FONTE, VISAO_DISPENSER_FONTE,
                VISION_SIM_URL)
    if VISAO_DISPENSER_FONTE == "estacao":
        logger.info("[STARTUP] estações dos dispensers: esq=%s dir=%s | %d "
                    "etiqueta(s) | catálogo a cada %.1f s + %.1f s de assentamento",
                    VISAO_DISP_ESQ_URL, VISAO_DISP_DIR_URL, len(ETIQUETAS),
                    VISAO_DISP_INTERVALO_CATALOGO_S, VISAO_DISP_ASSENTAMENTO_S)
    await _wait_for_upstream("vision-simulator", VISION_SIM_URL + "/ping")
    yield
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
    # `central-computer/injecao.py`). O adapter não o interpreta: repassa como
    # veio, do mesmo jeito que faz com as duas quantidades da pesagem. Quem
    # decide é o central; quem executa é o simulador, num ramo isolado.
    injetar_falha: Optional[str] = None


class CapturarMesaReq(BaseModel):
    """Comando do Central: fotografar a mesa (balança) após dispensa no slot X."""
    slot_id: int
    os_id: str
    quantidade_esperada: int = 0
    posicao_x: float = 0.0
    posicao_y: float = 0.0
    # Campo de DEMONSTRAÇÃO, ausente no caminho normal (ver
    # `central-computer/injecao.py`). O adapter não o interpreta: repassa como
    # veio, do mesmo jeito que faz com as duas quantidades da pesagem. Quem
    # decide é o central; quem executa é o simulador, num ramo isolado.
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
                      sku_lido: Optional[str] = None, confianca: float = 0.0) -> dict:
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


async def _evento_da_linha(req: "CapturarDispenserReq", camera: str, linha: dict) -> dict:
    tipo, motivo = ponte.traduzir(linha, req.medicamento_esperado)
    if motivo:
        return _evento_dispenser(tipo, req, camera, motivo=motivo, linha=linha)
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
                             confianca=confianca)


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
        elif m_ref is None:
            m_ref = m

        if (m is not None and m_ref is not None
                and _relogio() - _t_catalogo >= folga
                and (m - m_ref).total_seconds() >= exigido):
            return await _evento_da_linha(req, camera, linha)

        if _relogio() >= fim:
            break
        await _dormir(_SONDAGEM_S)

    if not alcancou:
        motivo = "camera_indisponivel"
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
    logger.info("[PONTE] D%d → %s%s", req.slot_id, evento["tipo"],
                f" ({evento['motivo']})" if evento.get("motivo") else "")
    if not await _post_central(evento):
        logger.warning("[FWD] Leitura D%d (%s) não chegou ao Central.",
                       req.slot_id, evento["tipo"])


def _capturar_na_estacao(req: "CapturarDispenserReq") -> dict:
    """Responde NA HORA e lê em segundo plano: o central espera 5 s pela
    resposta, e a leitura leva o intervalo do catálogo mais o assentamento."""
    if not 1 <= req.slot_id <= NUM_SLOTS:
        raise HTTPException(400, f"slot_id deve ser 1-{NUM_SLOTS}")
    camera = ponte.camera_do_slot(req.slot_id, NUM_SLOTS)
    _registrar_na_os(req.os_id, req.slot_id, req.medicamento_esperado)
    if req.injetar_falha:
        logger.warning("[PONTE] injetar_falha=%r ignorado: a estação real reporta "
                       "o que a câmera viu.", req.injetar_falha)
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
    ok = all(v == "ok" for v in checks.values())
    return {"status": "ok" if ok else "degradado", "checks": checks}


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
    resultado = await _post_sim(
        "/executar/capturar/mesa",
        {
            "slot_id":           req.slot_id,
            "os_id":             req.os_id,
            "quantidade_esperada": req.quantidade_esperada,
            "posicao_x":         req.posicao_x,
            "posicao_y":         req.posicao_y,
            "injetar_falha":     req.injetar_falha,
        },
    )
    return {"ok": True, "simulador": resultado}


# ── O que as estações dos dispensers chamam ───────────────────────────────────

@app.get("/api/visao/catalogo")
def visao_catalogo():
    """O catálogo da OS corrente, no formato de `Catalogo.de_itens` da estação.

    Sem token, como a estação sempre buscou: ela não manda cabeçalho nenhum.
    """
    return _catalogo


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

    log_extra = f"| OS {os_id}" if os_id else ""
    logger.info("[EVT] %-28s ← cam=%-9s slot=%s %s", tipo, camera, slot_id, log_extra)

    ok = await _post_central(payload)
    if not ok:
        logger.warning("[FWD] Evento '%s' cam=%s slot=%s não chegou ao Central.",
                       tipo, camera, slot_id)

    return {"ok": True, "encaminhado": ok}
