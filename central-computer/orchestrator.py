"""
APSEN - Orquestrador Central v1.0
Toda a inteligência de negócio: fila de OS, IA de atribuição de slots,
planejamento de rota CNC, orquestração de carregamento e dispensa.

Fluxo de uma OS:
  1. OS chega via POST /api/v1/ordens → entra na fila
  2. IA atribui slots (nearest residual → slot vazio)
  3. Central comanda carregamento paralelo de todos os slots
  4. Aguarda todos "carregado" (evento do dispenser-adapter)
  5. Para cada slot na rota otimizada (nearest-neighbor):
       a. Comanda CNC: mover para dispenser X
       b. Aguarda "posicionado"
       c. Comanda dispenser: dispensar
       d. Aguarda "dispensado"
  6. Comanda CNC: homing
  7. OS concluída → DB atualizado → broadcast WebSocket
"""
import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Optional

import httpx

from config import settings
from database import (
    atribuir_dispenser_item,
    atualizar_item_os,
    atualizar_status_ordem,
    get_peso_medicamento,
    salvar_alarme,
    salvar_cnc_evento,
    salvar_dispensa,
    salvar_dispenser_estado,
    limpar_dispenser_estado,
)

logger = logging.getLogger(__name__)

# ── Posições físicas dos dispensers na mesa (mm) ───────────────────────────────
POSICOES: dict[int, tuple[float, float]] = {
    1: (0.0,   0.0),
    2: (120.0, 0.0),
    3: (240.0, 0.0),
    4: (360.0, 0.0),
    5: (480.0, 0.0),
    6: (600.0, 0.0),
}
HOME: tuple[float, float] = (0.0, -50.0)
NUM_SLOTS = 6

# ── Estado compartilhado (referência injetada pelo main.py) ───────────────────
_estado: dict = {}
_lock: object = None          # threading.Lock injetado pelo main
_broadcast_fn = None          # função de broadcast injetada pelo main
_loop: Optional[asyncio.AbstractEventLoop] = None  # event loop (para call_soon_threadsafe)

# ── Fila e eventos de orquestração ────────────────────────────────────────────
_os_queue: asyncio.Queue = asyncio.Queue()
_pending_events: dict[str, asyncio.Event] = {}
_pending_data: dict[str, dict] = {}

# ── Trava de erro (Triple Check) ───────────────────────────────────────────────
# Quando ativada, a OS atual fica suspensa aguardando intervenção de supervisor.
_trava_ativa: bool = False
_trava_evento: Optional[asyncio.Event] = None   # set() para liberar a trava
_trava_motivo: str = ""
_trava_slot_id: Optional[int] = None
_trava_os_id: Optional[str] = None


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


_client: Optional[httpx.AsyncClient] = None


def inicializar(estado: dict, lock, broadcast_fn, loop: asyncio.AbstractEventLoop):
    """Chamado pelo main.py no startup para injetar dependências."""
    global _estado, _lock, _broadcast_fn, _loop, _client
    _estado       = estado
    _lock         = lock
    _broadcast_fn = broadcast_fn
    _loop         = loop
    _client       = httpx.AsyncClient()
    logger.info("[ORCH] httpx.AsyncClient criado")


async def encerrar():
    """Chamado pelo main.py no shutdown para fechar o cliente HTTP."""
    global _client
    if _client:
        await _client.aclose()
        logger.info("[ORCH] httpx.AsyncClient encerrado")


# ── API de trava ───────────────────────────────────────────────────────────────

def get_trava_estado() -> dict:
    """Retorna o estado atual da trava para o dashboard/IHM."""
    return {
        "ativa":    _trava_ativa,
        "motivo":   _trava_motivo,
        "slot_id":  _trava_slot_id,
        "os_id":    _trava_os_id,
    }


def liberar_trava(liberado_por: str) -> bool:
    """
    Libera a trava de erro — chamado pelo endpoint de admin.
    Thread-safe: usa call_soon_threadsafe para setar o evento no loop correto.
    Retorna False se não há trava ativa.
    """
    global _trava_ativa, _trava_evento, _trava_motivo, _trava_slot_id, _trava_os_id
    if not _trava_ativa or _trava_evento is None:
        return False
    logger.warning("[TRAVA] Trava liberada por '%s'.", liberado_por)
    _trava_ativa   = False
    _trava_motivo  = ""
    _trava_slot_id = None
    _trava_os_id   = None
    if _loop and not _loop.is_closed():
        _loop.call_soon_threadsafe(_trava_evento.set)
    else:
        _trava_evento.set()
    return True


async def _ativar_trava(os_id: str, slot_id: Optional[int], motivo: str) -> asyncio.Event:
    """
    Ativa a trava de erro: suspende a OS até intervenção de supervisor.
    Retorna o Event que será aguardado pelo orquestrador.
    Deve ser chamado DENTRO do event loop (é async).
    """
    global _trava_ativa, _trava_evento, _trava_motivo, _trava_slot_id, _trava_os_id
    _trava_ativa   = True
    _trava_motivo  = motivo
    _trava_slot_id = slot_id
    _trava_os_id   = os_id
    _trava_evento  = asyncio.Event()
    logger.error(
        "[TRAVA] ⛔ TRAVA ATIVADA — OS=%s slot=%s motivo=%s",
        os_id, slot_id, motivo,
    )
    # Avisa todos os clientes conectados (dashboard, IHM) via WebSocket
    if _broadcast_fn:
        _broadcast_fn()
    try:
        await asyncio.to_thread(
            salvar_alarme, "triple_check", "trava_ativada",
            f"OS {os_id} slot {slot_id}: {motivo}",
        )
    except Exception as e:
        logger.warning("[DB] salvar_alarme trava: %s", e)
    return _trava_evento


# ── Sistema de eventos async ───────────────────────────────────────────────────

def registrar_evento(chave: str) -> asyncio.Event:
    """Registra um evento pendente. Deve ser chamado ANTES de enviar o comando."""
    evt = asyncio.Event()
    _pending_events[chave] = evt
    return evt


async def aguardar_evento(chave: str, timeout: float) -> Optional[dict]:
    """Aguarda um evento previamente registrado. Retorna dados ou None (timeout)."""
    evt = _pending_events.get(chave)
    if evt is None:
        # Não foi pré-registrado — registrar agora (pode perder eventos rápidos)
        evt = asyncio.Event()
        _pending_events[chave] = evt
    try:
        await asyncio.wait_for(evt.wait(), timeout=timeout)
        return _pending_data.pop(chave, {})
    except asyncio.TimeoutError:
        logger.warning(f"[ORCH] Timeout aguardando evento '{chave}' ({timeout}s)")
        return None
    finally:
        _pending_events.pop(chave, None)
        _pending_data.pop(chave, None)


def notificar_evento(chave: str, dados: dict):
    """
    Chamado pelos handlers de evento (sync ou async) para desbloquear o orquestrador.
    Usa call_soon_threadsafe para garantir que asyncio.Event.set() seja executado
    no event loop thread — seguro independentemente do contexto do chamador.
    """
    if chave not in _pending_events:
        return  # Ninguém aguardando — ignora
    _pending_data[chave] = dados
    evt = _pending_events[chave]
    if _loop and not _loop.is_closed():
        _loop.call_soon_threadsafe(evt.set)
    else:
        evt.set()  # fallback (nunca deve ocorrer em produção)


# ── IA: Atribuição de slots ────────────────────────────────────────────────────

def atribuir_slots(medicamentos: list, estado_dispensers: dict) -> Optional[list]:
    """
    Decide qual slot recebe qual medicamento.
    Prioridade 1: slot com o mesmo medicamento e residual suficiente.
    Prioridade 2: slot vazio (medicamento=None ou quantidade=0).
    Retorna lista de atribuições ou None se não há slots suficientes.
    """
    atribuicoes: list[dict] = []
    slots_reservados: set[int] = set()

    for item in medicamentos:
        med = item["medicamento"]
        sku = item.get("sku", "")
        cat = item.get("categoria", "")
        qtd = item["quantidade"]
        slot_escolhido: Optional[int] = None

        # Passo 1: slot com mesmo medicamento e residual > 0
        for slot_id in range(1, NUM_SLOTS + 1):
            if slot_id in slots_reservados:
                continue
            disp = estado_dispensers.get(str(slot_id), {})
            if disp.get("medicamento") == med and (disp.get("quantidade", 0) or 0) > 0:
                slot_escolhido = slot_id
                logger.info("[IA] %s → D%d (residual=%d)", med, slot_id, disp.get("quantidade", 0))
                break

        # Passo 2: slot livre (sem medicamento ou quantidade=0)
        if slot_escolhido is None:
            for slot_id in range(1, NUM_SLOTS + 1):
                if slot_id in slots_reservados:
                    continue
                disp = estado_dispensers.get(str(slot_id), {})
                med_atual = disp.get("medicamento")
                qtd_atual = disp.get("quantidade", 0) or 0
                if med_atual is None or qtd_atual == 0:
                    slot_escolhido = slot_id
                    logger.info("[IA] %s → D%d (slot livre)", med, slot_id)
                    break

        if slot_escolhido is None:
            logger.error("[IA] Sem slot disponível para '%s'! Reservados: %s", med, slots_reservados)
            return None

        slots_reservados.add(slot_escolhido)
        atribuicoes.append({
            "dispenser_id": slot_escolhido,
            "medicamento":  med,
            "sku":          sku,
            "categoria":    cat,
            "quantidade":   qtd,
        })

    return atribuicoes


# ── Planejamento de rota CNC (nearest-neighbor) ────────────────────────────────

def planejar_rota(dispenser_ids: list[int], pos_inicial: tuple[float, float]) -> list[int]:
    """Ordena os dispensers para minimizar distância percorrida pela CNC."""
    restantes = list(dispenser_ids)
    ordem: list[int] = []
    pos = pos_inicial

    while restantes:
        mais_proximo = min(
            restantes,
            key=lambda d: math.hypot(POSICOES[d][0] - pos[0], POSICOES[d][1] - pos[1]),
        )
        ordem.append(mais_proximo)
        restantes.remove(mais_proximo)
        pos = POSICOES[mais_proximo]

    return ordem


# ── Comandos HTTP aos adapters ─────────────────────────────────────────────────

async def _post(url: str, payload: dict, timeout: float = 10.0) -> bool:
    """POST HTTP com retry básico. Retorna True se 2xx."""
    for tentativa in range(3):
        try:
            r = await _client.post(url, json=payload, timeout=timeout)
            if r.status_code < 300:
                return True
            logger.warning("[HTTP] %s → status %d (tentativa %d)", url, r.status_code, tentativa + 1)
        except Exception as exc:
            logger.warning("[HTTP] %s falhou (tentativa %d): %s", url, tentativa + 1, exc)
        if tentativa < 2:
            await asyncio.sleep(1.0)
    return False


async def cmd_carregar(disp_id: int, medicamento: str, sku: str, categoria: str,
                       quantidade: int, os_id: str) -> bool:
    return await _post(
        settings.DISPENSER_ADAPTER_URL + "/comandos/carregar",
        {
            "dispenser_id": disp_id,
            "medicamento":  medicamento,
            "sku":          sku,
            "categoria":    categoria,
            "quantidade":   quantidade,
            "os_id":        os_id,
        },
    )


async def cmd_dispensar(disp_id: int, os_id: str) -> bool:
    return await _post(
        settings.DISPENSER_ADAPTER_URL + "/comandos/dispensar",
        {"dispenser_id": disp_id, "os_id": os_id},
    )


async def cmd_limpar(disp_id: int, solicitado_por: str) -> bool:
    return await _post(
        settings.DISPENSER_ADAPTER_URL + "/comandos/limpar",
        {"dispenser_id": disp_id, "solicitado_por": solicitado_por},
    )


async def cmd_mover(disp_id: int, os_id: str, ciclo: int, total: int) -> bool:
    px, py = POSICOES[disp_id]
    return await _post(
        settings.CNC_ADAPTER_URL + "/comandos/mover",
        {
            "dispenser_alvo": disp_id,
            "os_id":          os_id,
            "posicao_x":      px,
            "posicao_y":      py,
            "ciclo_atual":    ciclo,
            "total_ciclos":   total,
        },
    )


async def cmd_homing(os_id: str) -> bool:
    return await _post(
        settings.CNC_ADAPTER_URL + "/comandos/homing",
        {"os_id": os_id},
    )


async def cmd_visao_dispenser(slot_id: int, sku: str, medicamento: str,
                               quantidade: int, os_id: str) -> bool:
    """Solicita captura da câmera do dispenser para validar produto carregado."""
    return await _post(
        settings.VISION_ADAPTER_URL + "/comandos/capturar/dispenser",
        {
            "slot_id":             slot_id,
            "os_id":               os_id,
            "sku_esperado":        sku,
            "medicamento_esperado": medicamento,
            "quantidade_esperada": quantidade,
        },
        timeout=5.0,   # comando é rápido — o resultado chega via evento async
    )


async def cmd_visao_mesa(slot_id: int, os_id: str, quantidade: int,
                          pos_x: float, pos_y: float) -> bool:
    """Solicita captura da câmera da mesa para validar produtos dispensados."""
    return await _post(
        settings.VISION_ADAPTER_URL + "/comandos/capturar/mesa",
        {
            "slot_id":           slot_id,
            "os_id":             os_id,
            "quantidade_esperada": quantidade,
            "posicao_x":         pos_x,
            "posicao_y":         pos_y,
        },
        timeout=5.0,
    )


async def cmd_tara(os_id: str) -> bool:
    """Zera a balança HX711 antes de iniciar as dispensas da OS."""
    return await _post(
        settings.WEIGHT_ADAPTER_URL + "/comandos/tara",
        {"os_id": os_id},
        timeout=8.0,
    )


async def cmd_pesar(slot_id: int, os_id: str, quantidade: int, peso_unitario_g: float) -> bool:
    """Solicita pesagem após dispensa de um slot."""
    return await _post(
        settings.WEIGHT_ADAPTER_URL + "/comandos/pesar",
        {
            "os_id":               os_id,
            "slot_id":             slot_id,
            "quantidade_esperada": quantidade,
            "peso_unitario_g":     peso_unitario_g,
        },
        timeout=5.0,
    )


# ── Processamento de uma OS ────────────────────────────────────────────────────

async def _processar_os(os_payload: dict):
    os_id        = os_payload["os_id"]
    medicamentos = os_payload.get("medicamentos", [])
    descricao    = os_payload.get("descricao", "")

    logger.info("\n%s\n[ORCH] OS INICIADA: %s | %d medicamentos\n%s",
                "=" * 60, os_id, len(medicamentos), "=" * 60)

    # ── Estado: em andamento ─────────────────────────────────────────────────
    with _lock:
        _estado["os_ativa"] = {
            "os_id":    os_id,
            "descricao": descricao,
            "status":   "em_andamento",
            "itens":    medicamentos,
        }
        disp_snapshot = {k: dict(v) for k, v in _estado["dispensers"].items()}

    # ── 1. Atribuição de slots (IA) ──────────────────────────────────────────
    atribuicoes = atribuir_slots(medicamentos, disp_snapshot)
    if atribuicoes is None:
        logger.error("[ORCH] OS %s REJEITADA — sem slots disponíveis.", os_id)
        try:
            atualizar_status_ordem(os_id, "erro")
            salvar_alarme("orchestrator", "sem_slot", f"OS {os_id}: sem slots disponíveis")
        except Exception as e:
            logger.warning("[DB] %s", e)
        with _lock:
            _estado["os_ativa"] = None
        return

    log_atrib = " | ".join(f"D{a['dispenser_id']}←{a['medicamento']}×{a['quantidade']}"
                           for a in atribuicoes)
    logger.info("[ORCH] Atribuição: %s", log_atrib)

    # Registra atribuição no DB (async — não bloqueia o event loop)
    for a in atribuicoes:
        try:
            await asyncio.to_thread(
                atribuir_dispenser_item, os_id, a["medicamento"], a["dispenser_id"]
            )
        except Exception as e:
            logger.warning("[DB] atribuir_dispenser_item: %s", e)

    # Atualiza estado em memória com atribuições
    with _lock:
        _estado["atribuicao_ia"] = atribuicoes
        for a in atribuicoes:
            key = str(a["dispenser_id"])
            if key in _estado["dispensers"]:
                _estado["dispensers"][key].update({
                    "os_id":           os_id,
                    "quantidade_alvo": a["quantidade"],
                    "medicamento":     a["medicamento"],
                    "sku":             a.get("sku"),
                    "categoria":       a.get("categoria"),
                    "status":          "aguardando_carga",
                })
    # Broadcast imediato para o dashboard ver atribuição de slots
    if _broadcast_fn:
        _broadcast_fn()

    # Enriquece atribuições com peso unitário (lookup paralelo no DB)
    pesos = await asyncio.gather(*[
        asyncio.to_thread(get_peso_medicamento, a["medicamento"])
        for a in atribuicoes
    ])
    for a, peso in zip(atribuicoes, pesos):
        a["peso_unitario_g"] = peso

    # ── 2. Planejamento de rota ──────────────────────────────────────────────
    rota = planejar_rota([a["dispenser_id"] for a in atribuicoes], HOME)
    logger.info("[ORCH] Rota CNC: %s", " → ".join(f"D{d}" for d in rota))

    # ── 3. Carregar dispensers em paralelo ───────────────────────────────────
    # Registrar eventos ANTES de enviar comandos (evita race condition)
    chaves_carga = [f"{os_id}:carregado:{a['dispenser_id']}" for a in atribuicoes]
    for chave in chaves_carga:
        registrar_evento(chave)

    for a in atribuicoes:
        ok = await cmd_carregar(
            a["dispenser_id"], a["medicamento"], a.get("sku", ""),
            a.get("categoria", ""), a["quantidade"], os_id,
        )
        if not ok:
            logger.error("[ORCH] Falha ao enviar comando carregar para D%d", a["dispenser_id"])

    logger.info("[ORCH] Comandos de carregamento enviados. Aguardando dispensers prontos...")

    # Aguardar todos prontos (em paralelo)
    resultados_carga = await asyncio.gather(*[
        aguardar_evento(chave, settings.TIMEOUT_CARREGAMENTO)
        for chave in chaves_carga
    ])

    # Verificar se algum falhou no carregamento
    for i, resultado in enumerate(resultados_carga):
        a = atribuicoes[i]
        if resultado is None:
            logger.error("[ORCH] Timeout carregamento D%d (%s). Abortando OS %s.",
                         a["dispenser_id"], a["medicamento"], os_id)
            await _abortar_os(os_id, "erro_carregamento")
            return
        if resultado.get("tipo") == "erro":
            logger.error("[ORCH] ERRO carregamento D%d: %s. Abortando OS %s.",
                         a["dispenser_id"], resultado.get("descricao", ""), os_id)
            await _abortar_os(os_id, "erro_carregamento")
            return

    logger.info("[ORCH] Todos os dispensers prontos. Iniciando validação de visão.")

    # ── 3b. Scan da câmera dos dispensers (paralelo, não-bloqueante) ─────────
    # Registrar eventos ANTES de solicitar scans
    chaves_visao_disp = [f"{os_id}:visao_dispenser:{a['dispenser_id']}" for a in atribuicoes]
    for chave in chaves_visao_disp:
        registrar_evento(chave)

    # Dispara scans em paralelo para todos os dispensers carregados
    for a in atribuicoes:
        ok = await cmd_visao_dispenser(
            a["dispenser_id"], a.get("sku", ""), a["medicamento"],
            a["quantidade"], os_id,
        )
        if not ok:
            logger.warning("[ORCH] Não foi possível solicitar scan câmera dispenser D%d", a["dispenser_id"])

    # Aguarda resultados dos scans (com timeout próprio)
    resultados_visao_disp = await asyncio.gather(*[
        aguardar_evento(chave, settings.TIMEOUT_VISAO_DISPENSER)
        for chave in chaves_visao_disp
    ])

    # ── Processa resultados da câmera dispenser ────────────────────────────────
    # SKU errado (divergencia) → BLOQUEANTE: trava + retry até operador corrigir
    # Falha de leitura / timeout → alarme informativo, não bloqueia
    slots_sku_errado: list = []  # lista de (atribuicao, resultado)

    for i, res in enumerate(resultados_visao_disp):
        a = atribuicoes[i]
        if res is None:
            logger.warning(
                "[ORCH] Timeout visão dispenser D%d — sem validação de SKU.", a["dispenser_id"]
            )
        elif res.get("tipo") == "leitura_dispenser_divergencia":
            logger.error(
                "[ORCH] ⛔ SKU ERRADO D%d: lido=%s esperado=%s — trava ativa.",
                a["dispenser_id"], res.get("sku_lido"), res.get("sku_esperado"),
            )
            slots_sku_errado.append((a, res))
        elif res.get("tipo") == "leitura_dispenser_falha":
            logger.warning(
                "[ORCH] ALARME câmera D%d: falha de leitura (continua sem validação SKU).",
                a["dispenser_id"],
            )
        else:
            logger.info(
                "[ORCH] Visão D%d OK — SKU=%s conf=%.0f%%",
                a["dispenser_id"], res.get("sku_lido", ""), (res.get("confianca", 0) or 0) * 100,
            )

    # Loop de bloqueio: mantém trava até todos os slots com SKU errado serem corrigidos
    while slots_sku_errado:
        a_err, res_err = slots_sku_errado[0]
        motivo = (
            f"SKU errado no dispenser D{a_err['dispenser_id']}: "
            f"lido={res_err.get('sku_lido', '?')} | esperado={res_err.get('sku_esperado', '?')} — "
            f"remova o medicamento incorreto e libere a trava para re-escanear."
        )
        logger.error("[ORCH] ⛔ %s", motivo)

        with _lock:
            _estado["trava"] = {
                "ativa":   True,
                "os_id":   os_id,
                "slot_id": a_err["dispenser_id"],
                "motivo":  motivo,
            }
        if _broadcast_fn:
            _broadcast_fn()

        evento_lib = await _ativar_trava(os_id, a_err["dispenser_id"], motivo)
        logger.warning(
            "[ORCH] Aguardando operador corrigir dispenser D%d (OS %s)…",
            a_err["dispenser_id"], os_id,
        )
        await evento_lib.wait()

        with _lock:
            _estado["trava"] = {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""}
        if _broadcast_fn:
            _broadcast_fn()

        # Re-escaneia todos os slots que ainda têm divergência
        logger.info("[ORCH] Trava liberada. Re-escaneando %d slot(s) com SKU errado…", len(slots_sku_errado))
        atribuicoes_retry = [a for a, _ in slots_sku_errado]
        chaves_retry = [f"{os_id}:visao_dispenser:{a['dispenser_id']}" for a in atribuicoes_retry]
        for chave in chaves_retry:
            registrar_evento(chave)
        for a in atribuicoes_retry:
            await cmd_visao_dispenser(
                a["dispenser_id"], a.get("sku", ""), a["medicamento"], a["quantidade"], os_id,
            )
        resultados_retry = await asyncio.gather(*[
            aguardar_evento(chave, settings.TIMEOUT_VISAO_DISPENSER)
            for chave in chaves_retry
        ])

        # Verifica se ainda há divergência após a correção
        slots_sku_errado = []
        for a, res in zip(atribuicoes_retry, resultados_retry):
            if res is None:
                logger.warning("[ORCH] Timeout re-scan D%d — assumindo corrigido.", a["dispenser_id"])
            elif res.get("tipo") == "leitura_dispenser_divergencia":
                logger.error(
                    "[ORCH] ⛔ Ainda SKU errado em D%d após re-scan. Nova trava.", a["dispenser_id"]
                )
                slots_sku_errado.append((a, res))
            else:
                logger.info("[ORCH] Re-scan D%d OK — SKU=%s.", a["dispenser_id"], res.get("sku_lido", ""))

    logger.info("[ORCH] Validação de visão concluída. Realizando tara da balança.")

    # ── 3c. Tara da balança HX711 (antes de qualquer dispensa) ───────────────
    chave_tara = f"{os_id}:tara"
    registrar_evento(chave_tara)
    ok_tara = await cmd_tara(os_id)
    if ok_tara:
        resultado_tara = await aguardar_evento(chave_tara, settings.TIMEOUT_PESO)
        if resultado_tara is None:
            logger.warning("[ORCH] Timeout tara balança — continuando sem pesagem.")
        elif resultado_tara.get("tipo") == "erro_sensor":
            logger.warning("[ORCH] Sensor de peso indisponível — continuando sem pesagem.")
        else:
            logger.info("[ORCH] Tara OK. Offset=%.1fg", resultado_tara.get("peso_tara_g", 0))
    else:
        _pending_events.pop(chave_tara, None)
        logger.warning("[ORCH] Weight adapter indisponível — continuando sem pesagem.")

    logger.info("[ORCH] Iniciando ciclo CNC.")

    # ── 4. Ciclo CNC: mover → dispensar para cada slot na ordem ─────────────
    for seq, disp_id in enumerate(rota, start=1):
        a = next(x for x in atribuicoes if x["dispenser_id"] == disp_id)
        total = len(rota)

        logger.info("[ORCH] Ciclo %d/%d → D%d (%s × %d)",
                    seq, total, disp_id, a["medicamento"], a["quantidade"])

        # 4a. Mover CNC
        chave_pos = f"{os_id}:posicionado:{disp_id}"
        registrar_evento(chave_pos)

        ok = await cmd_mover(disp_id, os_id, seq, total)
        if not ok:
            logger.error("[ORCH] Falha ao enviar cmd mover para D%d.", disp_id)
            await _abortar_os(os_id, "erro_cnc")
            return

        resultado_pos = await aguardar_evento(chave_pos, settings.TIMEOUT_POSICIONAMENTO)
        if resultado_pos is None:
            logger.error("[ORCH] Timeout CNC posicionando em D%d.", disp_id)
            await _abortar_os(os_id, "erro_cnc")
            return
        if resultado_pos.get("tipo") == "erro":
            logger.error("[ORCH] ERRO CNC ao mover para D%d: %s.",
                         disp_id, resultado_pos.get("descricao", ""))
            await _abortar_os(os_id, "erro_cnc")
            return

        logger.info("[ORCH] CNC posicionada em D%d. Disparando dispensa.", disp_id)

        # 4b. Dispensar
        chave_disp = f"{os_id}:dispensado:{disp_id}"
        registrar_evento(chave_disp)

        ok = await cmd_dispensar(disp_id, os_id)
        if not ok:
            logger.error("[ORCH] Falha ao enviar cmd dispensar para D%d.", disp_id)
            await _abortar_os(os_id, "erro_dispenser")
            return

        resultado_disp = await aguardar_evento(chave_disp, settings.TIMEOUT_DISPENSA)
        if resultado_disp is None:
            logger.error("[ORCH] Timeout dispensando D%d.", disp_id)
            await _abortar_os(os_id, "erro_dispenser")
            return
        if resultado_disp.get("tipo") == "erro":
            logger.error("[ORCH] ERRO dispensa D%d: %s.",
                         disp_id, resultado_disp.get("descricao", ""))
            await _abortar_os(os_id, "erro_dispenser")
            return

        logger.info("[ORCH] D%d dispensou %d/%d × %s.",
                    disp_id,
                    resultado_disp.get("quantidade_dispensada", 0),
                    a["quantidade"],
                    a["medicamento"])

        # 4c. Scan da câmera da mesa (não-bloqueante)
        chave_visao_mesa = f"{os_id}:visao_mesa:{disp_id}"
        registrar_evento(chave_visao_mesa)

        pos_x = resultado_pos.get("posicao_x", 0.0)
        pos_y = resultado_pos.get("posicao_y", 0.0)
        ok = await cmd_visao_mesa(disp_id, os_id, a["quantidade"], pos_x, pos_y)
        if not ok:
            logger.warning("[ORCH] Não foi possível solicitar scan câmera mesa D%d", disp_id)

        resultado_mesa = await aguardar_evento(chave_visao_mesa, settings.TIMEOUT_VISAO_MESA)
        if resultado_mesa is None:
            logger.warning(
                "[ORCH] Timeout câmera mesa D%d — continuando sem validação de contagem.", disp_id
            )
        elif resultado_mesa.get("tipo") in ("leitura_mesa_falha", "leitura_mesa_divergencia"):
            logger.warning(
                "[ORCH] ALARME câmera mesa D%d: %s | esp=%d det=%d",
                disp_id, resultado_mesa.get("tipo"),
                resultado_mesa.get("quantidade_esperada", 0),
                resultado_mesa.get("quantidade_detectada", 0),
            )
        else:
            logger.info(
                "[ORCH] Câmera mesa D%d OK — detectado=%d conf=%.0f%%",
                disp_id,
                resultado_mesa.get("quantidade_detectada", 0),
                (resultado_mesa.get("confianca", 0) or 0) * 100,
            )

        # 4d. Pesagem HX711 (bloqueante se Triple Check divergir)
        chave_peso = f"{os_id}:peso:{disp_id}"
        registrar_evento(chave_peso)
        peso_unit = a.get("peso_unitario_g", 50.0)
        resultado_peso: Optional[dict] = None
        ok_p = await cmd_pesar(disp_id, os_id, a["quantidade"], peso_unit)
        if ok_p:
            resultado_peso = await aguardar_evento(chave_peso, settings.TIMEOUT_PESO)
            if resultado_peso is None:
                logger.warning("[ORCH] Timeout pesagem D%d.", disp_id)
            elif resultado_peso.get("tipo") == "peso_divergencia":
                logger.warning(
                    "[ORCH] ALARME PESO D%d: esp=%.1fg med=%.1fg (desvio=%.1f%%)",
                    disp_id,
                    resultado_peso.get("peso_esperado_g", 0),
                    resultado_peso.get("peso_medido_g", 0),
                    resultado_peso.get("desvio_pct", 0),
                )
            elif resultado_peso.get("tipo") == "erro_sensor":
                logger.warning("[ORCH] Sensor de peso indisponível para D%d.", disp_id)
            else:
                logger.info(
                    "[ORCH] Peso D%d OK — %.1fg (desvio=%.1f%%)",
                    disp_id,
                    resultado_peso.get("peso_medido_g", 0),
                    resultado_peso.get("desvio_pct", 0),
                )
        else:
            _pending_events.pop(chave_peso, None)
            logger.warning("[ORCH] Weight adapter indisponível para D%d.", disp_id)

        # ── 4e. TRIPLE CHECK — valida as 3 fontes (dispenser, câmera mesa, balança) ──
        # Regra: se 2 ou mais fontes indicam divergência → ativar trava de emergência.
        qtd_esperada = a["quantidade"]
        qtd_dispensada = resultado_disp.get("quantidade_dispensada", qtd_esperada)

        def _check_triple() -> tuple[int, list[str]]:
            divergencias: list[str] = []
            # Fonte 1 — dispenser
            if qtd_dispensada != qtd_esperada:
                divergencias.append(
                    f"dispenser: dispensou {qtd_dispensada} de {qtd_esperada} esperados"
                )
            # Fonte 2 — câmera mesa
            if resultado_mesa is not None and resultado_mesa.get("tipo") in (
                "leitura_mesa_falha", "leitura_mesa_divergencia"
            ):
                det = resultado_mesa.get("quantidade_detectada", "?")
                divergencias.append(f"câmera_mesa: detectou {det} de {qtd_esperada}")
            # Fonte 3 — balança
            if resultado_peso is not None and resultado_peso.get("tipo") == "peso_divergencia":
                desvio = resultado_peso.get("desvio_pct") or 0
                divergencias.append(f"balança: desvio={desvio:.1f}%")
            return len(divergencias), divergencias

        n_div, causas = _check_triple()
        if n_div >= 2:
            motivo_trava = (
                f"Triple Check FALHOU ({n_div}/3 fontes divergentes) — D{disp_id}: "
                + "; ".join(causas)
            )
            logger.error("[ORCH] ⛔ %s", motivo_trava)
            with _lock:
                _estado["trava"] = {
                    "ativa": True,
                    "os_id": os_id,
                    "slot_id": disp_id,
                    "motivo": motivo_trava,
                }
            if _broadcast_fn:
                _broadcast_fn()
            # Aguarda liberação manual por supervisor/admin — bloqueia aqui
            evento_liberacao = await _ativar_trava(os_id, disp_id, motivo_trava)
            logger.warning("[ORCH] Aguardando liberação da trava (OS %s, D%d)…", os_id, disp_id)
            await evento_liberacao.wait()
            logger.info("[ORCH] Trava liberada. Retomando OS %s a partir de D%d.", os_id, disp_id)
            with _lock:
                _estado["trava"] = {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""}
            if _broadcast_fn:
                _broadcast_fn()
        elif n_div == 1:
            logger.warning(
                "[ORCH] Triple Check: 1/3 fonte diverge (D%d) — alarme registrado, OS prossegue. %s",
                disp_id, causas[0],
            )

    # ── 5. CNC retorna para home ─────────────────────────────────────────────
    logger.info("[ORCH] Ciclo completo. CNC retornando para HOME.")
    await cmd_homing(os_id)

    # ── 6. OS concluída ──────────────────────────────────────────────────────
    try:
        await asyncio.to_thread(atualizar_status_ordem, os_id, "concluida")
    except Exception as e:
        logger.warning("[DB] atualizar_status_ordem: %s", e)

    with _lock:
        _estado["os_ativa"] = None
        _estado["atribuicao_ia"] = []
        for a in atribuicoes:
            key = str(a["dispenser_id"])
            if key in _estado["dispensers"]:
                _estado["dispensers"][key]["os_id"] = None
                _estado["dispensers"][key]["status"] = "idle"

    logger.info("\n%s\n[ORCH] OS CONCLUÍDA: %s\n%s", "=" * 60, os_id, "=" * 60)
    if _broadcast_fn:
        _broadcast_fn()


def _limpar_eventos_os(os_id: str):
    """Remove todos os eventos pendentes associados a uma OS (evita memory leak e notificações cruzadas)."""
    prefixo = f"{os_id}:"
    chaves_remover = [k for k in list(_pending_events.keys()) if k.startswith(prefixo)]
    for chave in chaves_remover:
        _pending_events.pop(chave, None)
        _pending_data.pop(chave, None)
    if chaves_remover:
        logger.debug("[ORCH] Limpou %d evento(s) pendente(s) da OS %s.", len(chaves_remover), os_id)


async def _abortar_os(os_id: str, motivo: str):
    logger.error("[ORCH] Abortando OS %s — motivo: %s", os_id, motivo)
    _limpar_eventos_os(os_id)
    try:
        await asyncio.to_thread(atualizar_status_ordem, os_id, "erro")
        await asyncio.to_thread(
            salvar_alarme, "orchestrator", motivo, f"OS {os_id} abortada: {motivo}"
        )
    except Exception as e:
        logger.warning("[DB] _abortar_os: %s", e)

    with _lock:
        _estado["os_ativa"] = None
        _estado["atribuicao_ia"] = []
        for key in _estado["dispensers"]:
            _estado["dispensers"][key]["status"] = "idle"
            _estado["dispensers"][key]["os_id"] = None

    if _broadcast_fn:
        _broadcast_fn()


# ── Loop principal do orquestrador ────────────────────────────────────────────

async def enfileirar_os(os_payload: dict):
    """Chamado pelo endpoint POST /api/v1/ordens."""
    await _os_queue.put(os_payload)
    pos = _os_queue.qsize()
    with _lock:
        _estado["fila_tamanho"] = pos
        if os_payload["os_id"] not in _estado["fila_os"]:
            _estado["fila_os"].append(os_payload["os_id"])
    logger.info("[ORCH] OS %s enfileirada (posição %d).", os_payload["os_id"], pos)


async def loop_orquestrador():
    """Loop infinito — consome a fila de OS uma por vez."""
    logger.info("[ORCH] Orquestrador iniciado.")
    while True:
        os_payload = await _os_queue.get()
        with _lock:
            os_id = os_payload["os_id"]
            if os_id in _estado["fila_os"]:
                _estado["fila_os"].remove(os_id)
            _estado["fila_tamanho"] = _os_queue.qsize()
        try:
            await _processar_os(os_payload)
        except Exception as exc:
            logger.error("[ORCH] Exceção não tratada em _processar_os: %s", exc, exc_info=True)
            try:
                await asyncio.to_thread(
                    atualizar_status_ordem, os_payload.get("os_id", "?"), "erro"
                )
            except Exception:
                pass
            with _lock:
                _estado["os_ativa"] = None
        finally:
            _os_queue.task_done()
