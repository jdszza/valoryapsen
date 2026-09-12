"""
APSEN - Orquestrador Central v1.0
Toda a inteligência de negócio: fila de OS, IA de atribuição de slots,
planejamento de rota CNC, orquestração de carregamento e dispensa.

Fluxo de uma OS:
  1. OS chega via POST /api/v1/ordens → entra na fila
  2. IA atribui slots (nearest residual → slot vazio → slot a limpar)
  2b. Slots marcados para limpeza são esvaziados e confirmados antes da carga
  3. Central comanda carregamento paralelo de todos os slots
  4. Aguarda todos "carregado" (evento do dispenser-adapter)
  5. Para cada slot na rota otimizada (nearest-neighbor):
       a. Comanda CNC: mover para dispenser X
       b. Aguarda "posicionado"
       c. Comanda dispenser: dispensar
       d. Aguarda "dispensado"
       e. Triple Check: dispenser × câmera da mesa × balança — 1 fonte
          divergente já ativa a trava (ver `avaliar_triple_check`)
  6. Comanda CNC: homing
  7. OS concluída → DB atualizado → broadcast WebSocket
"""
import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import NamedTuple, Optional

import httpx

import injecao
from config import settings
from database import (
    atribuir_dispenser_item,
    atualizar_status_ordem,
    cancelar_ordens_pendentes,
    get_peso_medicamento,
    limpar_dispenser_estado,
    limpar_historico,
    salvar_alarme,
)

logger = logging.getLogger(__name__)

# ── Geometria da célula (mm) ──────────────────────────────────────────────────
# Duas fileiras de dispensers frente a frente, com o corredor da CNC no meio:
#
#        D1      D2      D3      D4          ← fileira esquerda (y = -150)
#   ·  ─────────────────────────────────     ← corredor (y = 0), HOME em x=-120
#        D5      D6      D7      D8          ← fileira direita  (y = +150)
#        x=0    x=120   x=240   x=360
#
# Estes quatro números são a ÚNICA fonte do mapa: `POSICOES` é derivada deles, e
# o cnc-simulator não tem cópia nenhuma (recebe x/y em cada comando). Mudar o
# passo, o afastamento ou o nº de slots não exige tocar em mais nada.
NUM_SLOTS         = settings.NUM_SLOTS         # total, somando as duas fileiras
SLOTS_POR_FILEIRA = NUM_SLOTS // 2             # NUM_SLOTS é par (ver config)
PASSO_X_MM        = 120.0                      # distância entre slots vizinhos
AFASTAMENTO_Y_MM  = 150.0                      # meia-largura do corredor
# HOME fica no centro do corredor, um passo ANTES do primeiro par: fora da
# faixa de qualquer dispenser, e equidistante de D1 e D5.
HOME: tuple[float, float] = (-PASSO_X_MM, 0.0)


def _gerar_posicoes() -> dict[int, tuple[float, float]]:
    """Mapa slot → (x, y). Ids 1..N/2 na fileira esquerda, N/2+1..N na direita.

    A numeração deixa os pares frente a frente com distância fixa de
    `SLOTS_POR_FILEIRA`: D1↔D5, D2↔D6, D3↔D7, D4↔D8 para N=8.
    """
    posicoes: dict[int, tuple[float, float]] = {}
    for slot_id in range(1, NUM_SLOTS + 1):
        coluna = (slot_id - 1) % SLOTS_POR_FILEIRA
        lado   = -1.0 if slot_id <= SLOTS_POR_FILEIRA else 1.0
        posicoes[slot_id] = (coluna * PASSO_X_MM, lado * AFASTAMENTO_Y_MM)
    return posicoes


POSICOES: dict[int, tuple[float, float]] = _gerar_posicoes()

# Peso usado quando o catálogo não responde. É o mesmo default de
# `database.get_peso_medicamento` para medicamento sem peso cadastrado.
PESO_UNITARIO_PADRAO_G = 50.0

# ── Estado compartilhado (referência injetada pelo main.py) ───────────────────
_estado: dict = {}
_lock: object = None          # threading.Lock injetado pelo main
_broadcast_fn = None          # função de broadcast injetada pelo main
_loop: Optional[asyncio.AbstractEventLoop] = None  # event loop (para call_soon_threadsafe)

# ── Fila e eventos de orquestração ────────────────────────────────────────────
# Fila LIMITADA: o gerador posta a cada 90s e uma OS leva mais que isso — de
# 90 a 140s quando a célula tinha 6 slots, e a de 8 acrescenta dois ciclos de
# CNC + dispensa + câmera + pesagem. Com a trava do Triple Check ativa o loop
# para até um humano liberar.
# Sem teto, a fila cresce indefinidamente em memória e no banco. O limite é o
# próprio `maxsize` — a estrutura recusa, em vez de cada chamador ter que
# lembrar de conferir (ver `enfileirar_os` e `ha_vaga_na_fila`).
_os_queue: asyncio.Queue = asyncio.Queue(maxsize=settings.MAX_FILA_OS)
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
    if _client:
        await _client.aclose()
        logger.info("[ORCH] httpx.AsyncClient encerrado")


# ── API de trava ───────────────────────────────────────────────────────────────

def get_trava_estado() -> dict:
    """Retorna o estado atual da trava para o dashboard e o app de manutenção."""
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
    global _trava_ativa, _trava_motivo, _trava_slot_id, _trava_os_id
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
    # As telas dos slots voltam ao normal. Agendado, não esperado: esta função
    # roda numa thread (`to_thread`, a partir do endpoint) e a resposta ao
    # supervisor não pode depender da placa das telas.
    _agendar_aviso_telas(False, None, None, "")
    return True


async def _ativar_trava(os_id: str, slot_id: Optional[int], motivo: str,
                        resumo: str = "") -> asyncio.Event:
    """
    Ativa a trava de erro: suspende a OS até intervenção de supervisor.
    Retorna o Event que será aguardado pelo orquestrador.
    Deve ser chamado DENTRO do event loop (é async).

    `resumo` é a categoria da divergência que vai às telas TFT dos slots
    (teto de TRAVA_RESUMO_MAX) — nunca o `motivo` formatado, que é do display
    de 7" e da web, onde o supervisor decide.
    """
    global _trava_ativa, _trava_evento, _trava_motivo, _trava_slot_id, _trava_os_id
    # PRIMEIRO o estado interno, DEPOIS a publicação. `liberar_trava` responde
    # a partir de `_trava_ativa`; se o dashboard mostrasse a trava antes dessa
    # linha, um clique em "Liberar" nesse intervalo cairia em
    # `if not _trava_ativa: return False` e a API responderia 409 "nenhuma
    # trava ativa" com a trava bem visível na tela do supervisor.
    _trava_ativa   = True
    _trava_motivo  = motivo
    _trava_slot_id = slot_id
    _trava_os_id   = os_id
    _trava_evento  = asyncio.Event()
    logger.error(
        "[TRAVA] ⛔ TRAVA ATIVADA — OS=%s slot=%s motivo=%s",
        os_id, slot_id, motivo,
    )
    # A publicação em `_estado["trava"]` mora aqui, e não em cada chamador,
    # justamente para que a ordem acima não dependa de ninguém lembrar dela.
    if _lock:
        with _lock:
            _estado["trava"] = {
                "ativa":   True,
                "os_id":   os_id,
                "slot_id": slot_id,
                "motivo":  motivo,
            }
    # As telas dos slots — agendado ANTES do broadcast e sem esperar: o aviso
    # corre em paralelo (uma placa de telas fora do ar não segura nada aqui),
    # e ficar na frente do broadcast garante a ORDEM: se a liberação vier no
    # instante do broadcast, o aviso de "liberada" sai depois do de "ativa".
    _agendar_aviso_telas(True, slot_id, os_id, resumo)
    # Avisa todos os clientes conectados (dashboard, manut_web) via WebSocket
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


async def _nulo() -> None:
    """Corrotina que resolve em `None` imediatamente.

    Serve para preencher a posição de um `gather` cujo evento já se sabe que
    nunca chegará — comando que o adapter recusou. Sem ela a alternativa seria
    `aguardar_evento`, que gastaria o timeout inteiro para produzir o mesmo
    `None`; com ela o slot cai no ramo "sem medição" na hora.
    """
    return None


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
    Prioridade 3: slot ocupado por OUTRO medicamento — marcado com
                  `precisa_limpeza=True`, para o chamador descartar o resíduo
                  antes de carregar.
    Retorna lista de atribuições ou None se não há slots suficientes.

    Função PURA: não envia comando nenhum. Quem consome a lista é que dispara a
    limpeza dos slots marcados (ver `_processar_os`). Com 96 medicamentos no
    catálogo, sem o passo 3 qualquer resíduo órfão tirava o slot de circulação
    até que uma OS pedisse exatamente aquele item.
    """
    atribuicoes: list[dict] = []
    slots_reservados: set[int] = set()

    for item in medicamentos:
        med = item["medicamento"]
        sku = item.get("sku", "")
        cat = item.get("categoria", "")
        qtd = item["quantidade"]
        slot_escolhido: Optional[int] = None
        precisa_limpeza = False

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

        # Passo 3: nenhum slot livre — sacrifica o de menor residual (menos
        # estoque descartado), desempatando pelo menor id.
        if slot_escolhido is None:
            candidatos = [
                slot_id for slot_id in range(1, NUM_SLOTS + 1)
                if slot_id not in slots_reservados
            ]
            if candidatos:
                slot_escolhido = min(
                    candidatos,
                    key=lambda s: (
                        (estado_dispensers.get(str(s), {}).get("quantidade", 0) or 0), s
                    ),
                )
                precisa_limpeza = True
                disp = estado_dispensers.get(str(slot_escolhido), {})
                logger.warning(
                    "[IA] %s → D%d (limpeza necessária: descarta %s × %d)",
                    med, slot_escolhido, disp.get("medicamento"),
                    disp.get("quantidade", 0) or 0,
                )

        if slot_escolhido is None:
            logger.error("[IA] Sem slot disponível para '%s'! Reservados: %s", med, slots_reservados)
            return None

        slots_reservados.add(slot_escolhido)
        atribuicoes.append({
            "dispenser_id":    slot_escolhido,
            "medicamento":     med,
            "sku":             sku,
            "categoria":       cat,
            "quantidade":      qtd,
            "precisa_limpeza": precisa_limpeza,
        })

    return atribuicoes


# ── Triple Check: as 3 fontes de contagem de um slot ───────────────────────────

class ResultadoTripleCheck(NamedTuple):
    """Veredito do Triple Check para uma dispensa.

    `divergencias` são fontes que CONTRADIZEM o alvo da OS; `fontes_indisponiveis`
    são fontes que não responderam ou não conseguiram medir. A distinção é o que
    permite o limiar 1 — ver `avaliar_triple_check`.
    """
    travar: bool
    divergencias: list[str]
    fontes_indisponiveis: list[str]
    limiar: int
    # A CATEGORIA de cada divergência, na mesma ordem de `divergencias`
    # ("dispenser divergente", "contagem divergente", "divergência de peso").
    # É daqui — e não do texto — que sai o `trava_resumo` das telas TFT.
    categorias: tuple = ()


def avaliar_triple_check(
    quantidade_esperada: int,
    quantidade_dispensada: int,
    resultado_mesa: Optional[dict],
    resultado_peso: Optional[dict],
    min_divergencias: Optional[int] = None,
) -> ResultadoTripleCheck:
    """
    Confronta as 3 fontes independentes de contagem e decide se a OS trava.

    Fonte 1 — dispenser: quantidade contada mecanicamente (`quantidade_dispensada`).
    Fonte 2 — câmera da mesa: contagem por visão (`leitura_mesa_divergencia`).
    Fonte 3 — balança HX711: delta de peso vs esperado (`peso_divergencia`).

    **Uma divergência basta para travar** (`TRIPLE_CHECK_MIN_DIVERGENCIAS`,
    default 1). O limiar antigo de 2 fazia do Triple Check um double check: a
    fonte solitária que acusasse erro virava alarme e a OS seguia para o
    paciente. Em contagem farmacêutica o custo dos dois erros não é simétrico —
    parar uma OS boa custa uma liberação de supervisor, deixar passar uma OS
    ruim custa medicamento errado no leito.

    **Fonte que não mediu não é fonte que divergiu.** Timeout da câmera e
    `leitura_mesa_falha` (a câmera não conseguiu ler) entram em
    `fontes_indisponiveis`, não em `divergencias`: elas não contradizem nada,
    apenas deixam de confirmar. Contá-las como divergência com limiar 1
    transformaria os ~2% de falha de leitura da câmera em trava por ruído —
    exatamente o que a regra conservadora não pode custar, sob pena de ser
    desligada em campo. O mesmo critério já vale para a câmera do dispenser,
    onde falha de leitura é não-bloqueante e SKU errado é bloqueante.

    Função PURA: não envia comando, não toca no estado. `min_divergencias`
    sobrepõe o limiar de configuração (usado pelos testes).
    """
    limiar = (
        settings.TRIPLE_CHECK_MIN_DIVERGENCIAS if min_divergencias is None
        else min_divergencias
    )

    divergencias: list[str] = []
    categorias: list[str] = []
    indisponiveis: list[str] = []

    # Fonte 1 — dispenser (sempre presente: sem o evento a OS já teria abortado)
    if quantidade_dispensada != quantidade_esperada:
        divergencias.append(
            f"dispenser: dispensou {quantidade_dispensada} de {quantidade_esperada} esperados"
        )
        categorias.append("dispenser divergente")

    # Fonte 2 — câmera da mesa
    if resultado_mesa is None:
        indisponiveis.append("câmera_mesa: sem resposta (timeout)")
    elif resultado_mesa.get("tipo") == "leitura_mesa_divergencia":
        det = resultado_mesa.get("quantidade_detectada", "?")
        divergencias.append(f"câmera_mesa: detectou {det} de {quantidade_esperada}")
        categorias.append("contagem divergente")
    elif resultado_mesa.get("tipo") == "leitura_mesa_falha":
        indisponiveis.append("câmera_mesa: falha de leitura")

    # Fonte 3 — balança HX711
    if resultado_peso is None:
        indisponiveis.append("balança: sem resposta (timeout)")
    elif resultado_peso.get("tipo") == "peso_divergencia":
        desvio = resultado_peso.get("desvio_pct") or 0
        divergencias.append(f"balança: desvio={desvio:.1f}%")
        categorias.append("divergência de peso")
    elif resultado_peso.get("tipo") == "erro_sensor":
        indisponiveis.append("balança: sensor indisponível")

    return ResultadoTripleCheck(
        travar=len(divergencias) >= limiar,
        divergencias=divergencias,
        fontes_indisponiveis=indisponiveis,
        limiar=limiar,
        categorias=tuple(categorias),
    )


# ── Planejamento de rota CNC (serpentina) ──────────────────────────────────────

def distancia_rota(ordem: list[int], pos_inicial: tuple[float, float],
                   voltar_para_home: bool = True) -> float:
    """Comprimento em mm do trajeto `pos_inicial` → slots na ordem dada → HOME.

    A volta entra por padrão porque ela SEMPRE acontece: o passo 5 de
    `_processar_os` faz homing ao fim de toda OS. Uma rota avaliada sem a
    perna de retorno é avaliada por um custo que a máquina não paga.
    """
    total = 0.0
    pos = pos_inicial
    for disp_id in ordem:
        alvo = POSICOES[disp_id]
        total += math.hypot(alvo[0] - pos[0], alvo[1] - pos[1])
        pos = alvo
    if voltar_para_home:
        total += math.hypot(HOME[0] - pos[0], HOME[1] - pos[1])
    return total


def planejar_rota(dispenser_ids: list[int], pos_inicial: tuple[float, float]) -> list[int]:
    """Ordena os dispensers para minimizar a distância percorrida pela CNC.

    SERPENTINA: sobe a fileira esquerda em x crescente e volta pela direita em
    x decrescente, pulando quem não está na OS. Substituiu o nearest-neighbor
    que servia enquanto a célula era uma fileira só — com o Y sempre em 0,
    qualquer heurística devolvia a mesma linha, e a escolha não custava nada.

    Com duas fileiras ela passa a custar. O ciclo real é FECHADO
    (HOME → slots → HOME, ver `distancia_rota`), e HOME mais as duas fileiras
    estão todos na borda de um mesmo polígono convexo — caso em que o tour
    ótimo é a ordem do contorno, que é o que a serpentina produz. Medido por
    força bruta nos 255 subconjuntos não-vazios de 8 slots, partindo de HOME e
    contra o ótimo de cada um: serpentina = ótimo em 255/255; nearest-neighbor
    4,1% pior em média e 40,4% pior no pior caso (slots {1,3,5,6,7,8}, onde ele
    cruza o corredor cedo e precisa cruzar de novo para fechar). A vantagem não
    vem dos números escolhidos: vale para todo passo e afastamento testados.

    `pos_inicial` decide por qual fileira começar: entrar pelo lado oposto ao
    que a CNC ocupa custa uma travessia de corredor a mais na ida e outra na
    volta. Toda OS parte de HOME (y=0, o `else` do teste), onde o argumento
    empata e o contorno é percorrido pela esquerda; o espelhamento serve a
    quem chame a função com a CNC parada do lado direito — aí ele economiza de
    10% a 17%, mas sem a garantia de otimalidade, que é do trajeto por HOME.
    """
    esquerda = sorted(d for d in dispenser_ids if d <= SLOTS_POR_FILEIRA)
    direita  = sorted((d for d in dispenser_ids if d > SLOTS_POR_FILEIRA), reverse=True)

    if esquerda and direita:
        # Começa pela fileira do lado em que a CNC já está: entrar pelo lado
        # errado custa uma travessia de corredor a mais, na ida e na volta.
        if pos_inicial[1] > 0:
            esquerda, direita = list(reversed(direita)), list(reversed(esquerda))

    return esquerda + direita


# ── Comandos HTTP aos adapters ─────────────────────────────────────────────────

_TENTATIVAS_POST = 3
_ESPERA_ENTRE_POSTS_S = 1.0

# Status que valem uma segunda chance: o lado de lá falhou por conta própria
# (5xx) ou pediu para esperar (408, 429). Todo o resto do 3xx/4xx é uma RECUSA
# DETERMINÍSTICA — a mesma requisição vai receber a mesma resposta, e insistir
# só gasta os 2s de `sleep` entre as tentativas.
#
# O caso concreto é o 409 `limpeza_em_operacao` do dispenser-simulator: o slot
# está limpando, e ele vai continuar limpando pelos próximos dois segundos. O
# 422 do FastAPI é pior ainda de retentar — payload que não passa na validação
# não passa na terceira tentativa, e o log fica com três linhas idênticas para
# um erro de contrato que aconteceu uma vez.
#
# Isso importa porque o `_post` é o relógio de quem chama: a etapa 3 do
# `_processar_os` dispara os comandos de todos os slots em `gather`, e cada
# recusa determinística segurava um slot por 2s a mais enquanto o
# `TIMEOUT_CARREGAMENTO` do primeiro já corria.
def _vale_retentar(status: int) -> bool:
    return status >= 500 or status in (408, 429)


async def _post(url: str, payload: dict, timeout: float = 10.0) -> bool:
    """POST HTTP. Retorna True se 2xx.

    Retenta em falha de rede, timeout e 5xx — as três em que tentar de novo
    pode dar outro resultado. Recusa determinística (409, 422, 404...) devolve
    False na hora: ver `_vale_retentar`.
    """
    for tentativa in range(_TENTATIVAS_POST):
        try:
            r = await _client.post(url, json=payload, timeout=timeout)
            if r.status_code < 300:
                return True
            if not _vale_retentar(r.status_code):
                logger.warning("[HTTP] %s → status %d — recusa definitiva, sem retry.",
                               url, r.status_code)
                return False
            logger.warning("[HTTP] %s → status %d (tentativa %d)", url, r.status_code, tentativa + 1)
        except Exception as exc:
            logger.warning("[HTTP] %s falhou (tentativa %d): %s", url, tentativa + 1, exc)
        if tentativa < _TENTATIVAS_POST - 1:
            await asyncio.sleep(_ESPERA_ENTRE_POSTS_S)
    return False


# ── Aviso à célula: as 8 telas TFT, pelo dispenser-adapter ────────────────────
#
# UMA tentativa, timeout curto, sem retry — e NUNCA por `_post`: ele retenta
# 3× com sleep(1) e timeout de 10 s, e uma placa de telas fora do ar
# acrescentaria até ~32 s ao caminho da trava, que é justamente o momento em
# que a tela tem que mudar na hora. Falha aqui NUNCA muda o fluxo da OS: não
# aborta, não propaga exceção, não entra no veredito do Triple Check e não
# atrasa o `_broadcast_fn` que o dashboard espera — loga e segue. Tela é
# cosmética; a trava, não.
TIMEOUT_AVISO_TELAS_S = 3.0

# Teto do `trava_resumo` que vai às telas (docs/PROTOCOLO_SERIAL.md §6). Ele
# sai da CATEGORIA da divergência, nunca da string `motivo` formatada — o texto
# é para humano e vai mudar; a categoria é dado do veredito.
TRAVA_RESUMO_MAX = 48


def resumo_da_trava(veredito: "ResultadoTripleCheck") -> str:
    """A categoria da PRIMEIRA divergência do veredito, no teto das telas.

    Derivado de `ResultadoTripleCheck.categorias`, não de regex sobre o
    `motivo`: o motivo é montado para gente ler e vai mudar de formato; a
    categoria é o dado que já estava no veredito.
    """
    categoria = veredito.categorias[0] if veredito.categorias else "divergência"
    return categoria[:TRAVA_RESUMO_MAX]


async def _avisar_telas(trava_ativa: bool, slot_id: Optional[int],
                        os_id: Optional[str], resumo: str) -> bool:
    """POST único em `/comandos/estado-celula`. Nunca levanta; devolve se chegou."""
    if _client is None:
        return False
    payload = {
        "trava_ativa":   bool(trava_ativa),
        "trava_slot_id": slot_id,
        "os_id":         os_id or "",
        "trava_resumo":  (resumo or "")[:TRAVA_RESUMO_MAX],
    }
    url = settings.DISPENSER_ADAPTER_URL + "/comandos/estado-celula"
    try:
        r = await _client.post(url, json=payload, timeout=TIMEOUT_AVISO_TELAS_S)
    except Exception as exc:  # noqa: BLE001 — cosmético: loga e segue
        logger.warning("[TELAS] estado-celula não entregue (%s) — as telas ficam "
                       "desatualizadas até o próximo aviso.", exc)
        return False
    if r.status_code >= 300:
        logger.warning("[TELAS] estado-celula → HTTP %d — as telas ficam "
                       "desatualizadas até o próximo aviso.", r.status_code)
        return False
    try:
        telas = (r.json() or {}).get("telas")
    except Exception:  # noqa: BLE001 — corpo não é JSON: o status já disse que chegou
        telas = None
    if telas and telas != "ok":
        logger.warning("[TELAS] estado-celula aceito pelo adapter, telas=%s", telas)
    return True


def _agendar_aviso_telas(trava_ativa: bool, slot_id: Optional[int],
                         os_id: Optional[str], resumo: str) -> None:
    """Dispara `_avisar_telas` SEM esperar por ele.

    Serve de dentro do event loop (`_ativar_trava`) e de fora dele
    (`liberar_trava`, que o main chama por `to_thread`). Não esperar é o que
    mantém a ativação da trava na casa de milissegundos com o adapter fora do
    ar — o aviso corre em paralelo, e a trava já está armada e publicada.
    """
    coro = _avisar_telas(trava_ativa, slot_id, os_id, resumo)
    if _loop is not None and not _loop.is_closed():
        asyncio.run_coroutine_threadsafe(coro, _loop)
        return
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        logger.debug("[TELAS] sem event loop — aviso de trava descartado")


def _injecao_para(comando: str, slot_id: int) -> dict:
    """Campo `injetar_falha` a acrescentar no corpo do comando, se houver.

    Devolve `{}` no caso normal — o comando sai byte a byte como saía antes de
    esta feature existir, e o simulador nem vê o campo. Devolve
    `{"injetar_falha": tipo}` quando o gatilho armado no console casa com ESTE
    slot e ESTE comando; `injecao.consumir` é check-and-pop, então o gatilho já
    se desarmou quando esta função retorna.

    A publicação em `_estado` vem junto e nesta ordem — gatilho consumido
    primeiro, tela depois —, a mesma de `_ativar_trava` e pelo mesmo motivo:
    publicar antes abriria a janela em que o console mostra "desarmado" com a
    falha ainda por sair.
    """
    tipo = injecao.consumir(slot_id, comando)
    if not tipo:
        return {}
    _publicar_injecao()
    return {"injetar_falha": tipo}


def _publicar_injecao() -> None:
    """Espelha o gatilho em `_estado["falha_armada"]` e avisa quem está olhando.

    Transição de operador: não passa pelo throttle do broadcast. Quem armou
    precisa ver o gatilho aparecer, e quem está explicando a planta precisa ver
    o gatilho sumir no instante em que a falha saiu.
    """
    with _lock:
        _estado["falha_armada"] = injecao.armada()
    if _broadcast_fn:
        _broadcast_fn()


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
        {"dispenser_id": disp_id, "os_id": os_id,
         **_injecao_para("/comandos/dispensar", disp_id)},
    )


async def cmd_limpar(disp_id: int, solicitado_por: str) -> bool:
    return await _post(
        settings.DISPENSER_ADAPTER_URL + "/comandos/limpar",
        {"dispenser_id": disp_id, "solicitado_por": solicitado_por},
    )


async def _liberar_slot(disp_id: int, solicitado_por: str) -> bool:
    """
    Comanda a limpeza física de um slot e AGUARDA a confirmação do dispenser.

    A chave do evento não leva os_id: o payload de `limpeza_ok` emitido pelo
    simulador não carrega OS nenhuma (a limpeza é uma operação de slot, não de
    OS). Por isso `_limpar_eventos_os` — que varre pelo prefixo `{os_id}:` —
    não colide com essas chaves.

    Nunca levanta: devolve False em recusa, falha de HTTP ou timeout, para que
    o chamador decida o que fazer sem perder o erro que o trouxe até aqui.
    """
    chave = f"limpeza:{disp_id}"
    registrar_evento(chave)

    try:
        enviado = await cmd_limpar(disp_id, solicitado_por)
    except Exception as exc:
        _pending_events.pop(chave, None)
        logger.error("[ORCH] Exceção ao comandar limpeza de D%d: %s", disp_id, exc)
        return False

    if not enviado:
        _pending_events.pop(chave, None)
        logger.error("[ORCH] Dispenser-adapter não aceitou a limpeza de D%d.", disp_id)
        return False

    resultado = await aguardar_evento(chave, settings.TIMEOUT_LIMPEZA)
    if resultado is None:
        logger.error("[ORCH] Timeout aguardando limpeza de D%d.", disp_id)
        return False
    if resultado.get("tipo") == "erro":
        logger.error(
            "[ORCH] Limpeza de D%d recusada: %s",
            disp_id, resultado.get("descricao", resultado.get("codigo_erro", "?")),
        )
        return False

    logger.info("[ORCH] D%d limpo (resíduo descartado: %s).",
                disp_id, resultado.get("medicamento_limpo") or "vazio")
    return True


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
    """Manda a CNC voltar para HOME, com as coordenadas no próprio comando.

    Mesmo princípio de `cmd_mover`: quem conhece a geometria é o central. Sem
    isso o simulador precisaria de uma cópia do HOME, e a cópia divergiria no
    dia em que o corredor mudasse de lugar.
    """
    return await _post(
        settings.CNC_ADAPTER_URL + "/comandos/homing",
        {"os_id": os_id, "posicao_x": HOME[0], "posicao_y": HOME[1]},
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
            **_injecao_para("/comandos/capturar/dispenser", slot_id),
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
            **_injecao_para("/comandos/capturar/mesa", slot_id),
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


async def cmd_pesar(slot_id: int, os_id: str, quantidade: int, peso_unitario_g: float,
                    quantidade_real: Optional[int] = None) -> bool:
    """Solicita pesagem após dispensa de um slot.

    Os dois campos de quantidade têm papéis distintos e ambos precisam viajar:

    - `quantidade_esperada` é o ALVO da OS — é sobre ele que a balança calcula
      o peso esperado e, portanto, o desvio;
    - `quantidade_real` é o que o dispenser reportou ter efetivamente soltado
      (`quantidade_dispensada`) — é o que de fato caiu na mesa.

    Mandar só o alvo tornava a balança cega: o simulador incrementava a mesa
    pelo peso esperado e comparava com ele mesmo, então a fonte 3 só divergia
    por ruído gaussiano (σ=2 g contra ≥100 g esperados — praticamente nunca).
    A divergência tem que emergir da diferença entre depositado e esperado, que
    é o que uma célula de carga mede na vida real.

    `None` mantém o contrato antigo (real = esperada) para chamadores que não
    têm a contagem do dispenser em mãos.
    """
    return await _post(
        settings.WEIGHT_ADAPTER_URL + "/comandos/pesar",
        {
            "os_id":               os_id,
            "slot_id":             slot_id,
            "quantidade_esperada": quantidade,
            "quantidade_real":     quantidade if quantidade_real is None else quantidade_real,
            "peso_unitario_g":     peso_unitario_g,
            **_injecao_para("/comandos/pesar", slot_id),
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

    # O banco também precisa saber que esta OS saiu da fila: `get_ordem_ativa`
    # — e o GET /os/ativa que o app de manutenção consome — separa a OS em
    # execução das que esperam pelo STATUS. Enquanto "em_andamento" só existia neste dicionário
    # em memória, toda OS do banco continuava "aguardando" e o endpoint
    # devolvia a última enfileirada, não a que estava rodando.
    try:
        await asyncio.to_thread(atualizar_status_ordem, os_id, "em_andamento")
    except Exception as e:
        logger.warning("[DB] atualizar_status_ordem em_andamento: %s", e)

    # ── 1. Atribuição de slots (IA) ──────────────────────────────────────────
    atribuicoes = atribuir_slots(medicamentos, disp_snapshot)
    if atribuicoes is None:
        logger.error("[ORCH] OS %s REJEITADA — sem slots disponíveis.", os_id)
        # Rejeição também é uma saída de `_processar_os`, e agora a OS já está
        # "em_andamento" no banco: sem o abort ela ficaria eternamente em
        # execução para o GET /os/ativa. Sem atribuições, nenhum slot foi
        # reservado — não há estoque órfão a descartar.
        await _abortar_os(os_id, "sem_slot")
        return

    log_atrib = " | ".join(f"D{a['dispenser_id']}←{a['medicamento']}×{a['quantidade']}"
                           for a in atribuicoes)
    logger.info("[ORCH] Atribuição: %s", log_atrib)

    # ── 1b. Limpeza prévia dos slots reaproveitados (passo 3 da IA) ──────────
    # `atribuir_slots` é pura: ela só MARCA o slot que está ocupado por outro
    # medicamento. Descartar o resíduo é responsabilidade daqui, e tem que
    # terminar antes de qualquer carregamento — senão o slot recusa a carga.
    slots_sujos = [a for a in atribuicoes if a.get("precisa_limpeza")]
    if slots_sujos:
        logger.warning(
            "[ORCH] %d slot(s) ocupado(s) por outro medicamento — limpando antes da carga: %s",
            len(slots_sujos), ", ".join(f"D{a['dispenser_id']}" for a in slots_sujos),
        )
        for a in slots_sujos:
            if not await _liberar_slot(a["dispenser_id"], f"pre_carga:{os_id}"):
                logger.error(
                    "[ORCH] Limpeza prévia de D%d falhou. Abortando OS %s.",
                    a["dispenser_id"], os_id,
                )
                # Sem atribuições: nada foi carregado ainda, não há o que descartar.
                await _abortar_os(os_id, "erro_limpeza_previa")
                return

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

    # Enriquece atribuições com peso unitário (lookup paralelo no DB).
    # `return_exceptions=True`: sem ele, uma falha de banco em UM medicamento
    # propaga pelo gather e derruba a OS inteira — sendo que o peso unitário já
    # tem fallback (`get_peso_medicamento` devolve 50 g para medicamento sem
    # peso cadastrado). Perder a precisão da balança em um item é aceitável;
    # perder a OS por causa disso, não. O erro é tratado item a item.
    pesos = await asyncio.gather(*[
        asyncio.to_thread(get_peso_medicamento, a["medicamento"])
        for a in atribuicoes
    ], return_exceptions=True)
    for a, peso in zip(atribuicoes, pesos):
        if isinstance(peso, BaseException):
            logger.warning(
                "[DB] get_peso_medicamento(%s): %s — usando %.0f g.",
                a["medicamento"], peso, PESO_UNITARIO_PADRAO_G,
            )
            peso = PESO_UNITARIO_PADRAO_G
        a["peso_unitario_g"] = peso

    # ── 2. Planejamento de rota ──────────────────────────────────────────────
    rota = planejar_rota([a["dispenser_id"] for a in atribuicoes], HOME)
    logger.info("[ORCH] Rota CNC: %s", " → ".join(f"D{d}" for d in rota))

    # ── 3. Carregar dispensers em paralelo ───────────────────────────────────
    # Registrar eventos ANTES de enviar comandos (evita race condition)
    chaves_carga = [f"{os_id}:carregado:{a['dispenser_id']}" for a in atribuicoes]
    for chave in chaves_carga:
        registrar_evento(chave)

    # O `gather` aqui não é otimização: é o que torna o TIMEOUT_CARREGAMENTO
    # comparável entre os slots. `_post` retenta 3x com sleep(1) e timeout de
    # 10s, ou seja, até ~32s por comando — em laço sequencial, com a célula
    # cheia, o comando do último slot só sairia ~4 min depois do primeiro,
    # enquanto o relógio dos 180s do PRIMEIRO já corre desde o começo. A OS
    # abortava por "timeout" de um dispenser que ainda nem tinha sido chamado.
    envios = await asyncio.gather(*[
        cmd_carregar(
            a["dispenser_id"], a["medicamento"], a.get("sku", ""),
            a.get("categoria", ""), a["quantidade"], os_id,
        )
        for a in atribuicoes
    ])

    # Envio recusado é informação que já temos: esperar os 180s de um comando
    # que sabidamente não chegou ao adapter só atrasa o abort — e mantém a fila
    # inteira parada nesse intervalo, porque o orquestrador é um loop único.
    nao_enviados = [a["dispenser_id"] for a, ok in zip(atribuicoes, envios) if not ok]
    if nao_enviados:
        logger.error(
            "[ORCH] Falha ao enviar comando carregar para %s. Abortando OS %s.",
            ", ".join(f"D{d}" for d in nao_enviados), os_id,
        )
        await _abortar_os(os_id, "erro_envio_carregamento", atribuicoes)
        return

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
            await _abortar_os(os_id, "erro_carregamento", atribuicoes)
            return
        if resultado.get("tipo") == "erro":
            logger.error("[ORCH] ERRO carregamento D%d: %s. Abortando OS %s.",
                         a["dispenser_id"], resultado.get("descricao", ""), os_id)
            await _abortar_os(os_id, "erro_carregamento", atribuicoes)
            return

    logger.info("[ORCH] Todos os dispensers prontos. Iniciando validação de visão.")

    # ── 3b. Scan da câmera dos dispensers (paralelo, não-bloqueante) ─────────
    # Registrar eventos ANTES de solicitar scans
    chaves_visao_disp = [f"{os_id}:visao_dispenser:{a['dispenser_id']}" for a in atribuicoes]
    for chave in chaves_visao_disp:
        registrar_evento(chave)

    # Dispara scans em paralelo — mesmo motivo do carregamento: em laço
    # sequencial o relógio do primeiro slot corre enquanto o comando do último
    # ainda está sendo retentado.
    envios_scan = await asyncio.gather(*[
        cmd_visao_dispenser(
            a["dispenser_id"], a.get("sku", ""), a["medicamento"],
            a["quantidade"], os_id,
        )
        for a in atribuicoes
    ])

    # Aqui o envio recusado NÃO aborta a OS, ao contrário do carregamento:
    # câmera que não leu é fonte que deixou de confirmar, não fonte que
    # contradisse — é a mesma regra que já vale para `leitura_dispenser_falha`
    # e para o timeout logo abaixo. O que muda é só a espera: sem o comando na
    # planta o evento nunca vem, então o `aguardar_evento` seria TIMEOUT_VISAO_
    # DISPENSER de relógio queimado por um resultado que já sabemos que não
    # existe. Resolvemos o slot como `None` na hora, que é onde ele cairia.
    for a, ok in zip(atribuicoes, envios_scan):
        if not ok:
            logger.warning(
                "[ORCH] Não foi possível solicitar scan câmera dispenser D%d — "
                "slot segue sem validação de SKU.", a["dispenser_id"],
            )
            _pending_events.pop(f"{os_id}:visao_dispenser:{a['dispenser_id']}", None)

    # Aguarda resultados dos scans (com timeout próprio)
    resultados_visao_disp = await asyncio.gather(*[
        aguardar_evento(chave, settings.TIMEOUT_VISAO_DISPENSER)
        if chave in _pending_events else _nulo()
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

        # `_ativar_trava` arma o estado interno e só então publica em
        # `_estado["trava"]` — publicar aqui antes reabriria a janela em que a
        # tela mostra trava que `liberar_trava` ainda não reconhece.
        evento_lib = await _ativar_trava(os_id, a_err["dispenser_id"], motivo,
                                         resumo="SKU errado")
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
            await _abortar_os(os_id, "erro_cnc", atribuicoes)
            return

        resultado_pos = await aguardar_evento(chave_pos, settings.TIMEOUT_POSICIONAMENTO)
        if resultado_pos is None:
            logger.error("[ORCH] Timeout CNC posicionando em D%d.", disp_id)
            await _abortar_os(os_id, "erro_cnc", atribuicoes)
            return
        if resultado_pos.get("tipo") == "erro":
            logger.error("[ORCH] ERRO CNC ao mover para D%d: %s.",
                         disp_id, resultado_pos.get("descricao", ""))
            await _abortar_os(os_id, "erro_cnc", atribuicoes)
            return

        logger.info("[ORCH] CNC posicionada em D%d. Disparando dispensa.", disp_id)

        # 4b. Dispensar
        chave_disp = f"{os_id}:dispensado:{disp_id}"
        registrar_evento(chave_disp)

        ok = await cmd_dispensar(disp_id, os_id)
        if not ok:
            logger.error("[ORCH] Falha ao enviar cmd dispensar para D%d.", disp_id)
            await _abortar_os(os_id, "erro_dispenser", atribuicoes)
            return

        resultado_disp = await aguardar_evento(chave_disp, settings.TIMEOUT_DISPENSA)
        if resultado_disp is None:
            logger.error("[ORCH] Timeout dispensando D%d.", disp_id)
            await _abortar_os(os_id, "erro_dispenser", atribuicoes)
            return
        if resultado_disp.get("tipo") == "erro":
            logger.error("[ORCH] ERRO dispensa D%d: %s.",
                         disp_id, resultado_disp.get("descricao", ""))
            await _abortar_os(os_id, "erro_dispenser", atribuicoes)
            return

        # Contagem da fonte 1, necessária tanto para a pesagem (4d) quanto para
        # o Triple Check (4e). Ausente do payload, assume-se o alvo — o que
        # deixa a decisão nas outras duas fontes.
        qtd_esperada   = a["quantidade"]
        qtd_dispensada = resultado_disp.get("quantidade_dispensada", qtd_esperada)

        logger.info("[ORCH] D%d dispensou %d/%d × %s.",
                    disp_id, qtd_dispensada, qtd_esperada, a["medicamento"])

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
        peso_unit = a.get("peso_unitario_g") or PESO_UNITARIO_PADRAO_G
        resultado_peso: Optional[dict] = None
        ok_p = await cmd_pesar(disp_id, os_id, qtd_esperada, peso_unit, qtd_dispensada)
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
        # Regra em `avaliar_triple_check`: por default 1 divergência já trava.
        veredito = avaliar_triple_check(
            quantidade_esperada=qtd_esperada,
            quantidade_dispensada=qtd_dispensada,
            resultado_mesa=resultado_mesa,
            resultado_peso=resultado_peso,
        )
        n_div  = len(veredito.divergencias)
        causas = veredito.divergencias

        if veredito.fontes_indisponiveis:
            logger.warning(
                "[ORCH] Triple Check D%d com %d fonte(s) sem medição: %s",
                disp_id, len(veredito.fontes_indisponiveis),
                "; ".join(veredito.fontes_indisponiveis),
            )

        if veredito.travar:
            motivo_trava = (
                f"Triple Check FALHOU ({n_div}/3 fontes divergentes, "
                f"limiar={veredito.limiar}) — D{disp_id}: " + "; ".join(causas)
            )
            logger.error("[ORCH] ⛔ %s", motivo_trava)
            # Mesma ordem do bloco de SKU errado: `_ativar_trava` arma o estado
            # interno antes de publicar. Aguarda liberação manual por
            # supervisor/admin — bloqueia aqui.
            evento_liberacao = await _ativar_trava(os_id, disp_id, motivo_trava,
                                                   resumo=resumo_da_trava(veredito))
            logger.warning("[ORCH] Aguardando liberação da trava (OS %s, D%d)…", os_id, disp_id)
            await evento_liberacao.wait()
            logger.info("[ORCH] Trava liberada. Retomando OS %s a partir de D%d.", os_id, disp_id)
            with _lock:
                _estado["trava"] = {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""}
            if _broadcast_fn:
                _broadcast_fn()
        elif causas:
            # Só alcançável com TRIPLE_CHECK_MIN_DIVERGENCIAS > 1: divergência
            # abaixo do limiar não trava, mas fica registrada no banco — quem
            # subiu o limiar precisa poder auditar o que passou por baixo dele.
            descricao = (
                f"Triple Check D{disp_id} OS {os_id}: {n_div}/3 fontes divergentes "
                f"(limiar={veredito.limiar}, OS prossegue) — " + "; ".join(causas)
            )
            logger.warning("[ORCH] %s", descricao)
            try:
                await asyncio.to_thread(
                    salvar_alarme, "triple_check", "divergencia_abaixo_do_limiar", descricao
                )
            except Exception as e:
                logger.warning("[DB] salvar_alarme triple_check: %s", e)

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


# ── Reset da planta (console) ─────────────────────────────────────────────────

class ResetRecusado(RuntimeError):
    """Reset pedido num momento em que ele estragaria mais do que arruma."""


def _drenar_fila() -> list:
    """Tira todas as OS que ESPERAM e devolve os ids, na ordem em que estavam.

    `get_nowait` em laço, e não `_os_queue = Queue()`: trocar o objeto deixaria
    o `loop_orquestrador` pendurado no `await _os_queue.get()` da fila ANTIGA —
    o consumidor pararia de consumir e a planta inteira ficaria muda, sem erro
    em lugar nenhum. O `maxsize` também se perderia junto.
    """
    ids: list = []
    while True:
        try:
            payload = _os_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        ids.append(payload.get("os_id", "?"))
        # O `task_done` casa com o `get_nowait`: sem ele, um `join()` futuro na
        # fila nunca retornaria.
        _os_queue.task_done()

    with _lock:
        _estado["fila_os"] = []
        _estado["fila_tamanho"] = 0
    return ids


async def resetar_planta(limpar_historico_tambem: bool = False) -> dict:
    """Devolve a bancada ao estado de boot. DESTRUTIVO.

    Existe porque repetir a demonstração exigia `docker compose down -v`, que
    apaga o banco e leva minutos.

    **Recusa enquanto houver OS em execução**, e essa é a decisão central desta
    função. O orquestrador é um loop ÚNICO parado dentro de `_processar_os`, a
    meio caminho de um ciclo de CNC; não há como interrompê-lo daqui sem
    cancelar a própria task do loop — o que pararia o consumidor da fila e
    calaria a planta inteira. E resetar POR CIMA de uma OS viva seria pior que
    não resetar: `cmd_limpar` num slot que está dispensando toma 409
    `limpeza_em_operacao`, a tara zera a balança no meio de uma pesagem e a OS
    aborta por divergência de peso que ninguém provocou.

    Tudo ou nada, e a recusa é explícita: um reset pela metade é o estado mais
    difícil de diagnosticar que esta planta consegue produzir. Com a trava
    ativa, a saída é liberá-la primeiro (o console tem o botão ao lado) e
    esperar a OS fechar.

    Devolve um relatório do que foi feito — inclusive os slots que NÃO
    confirmaram a limpeza, que é o que manda o operador olhar a bancada em vez
    de confiar na tela.
    """
    # `global` no topo: a mensagem de recusa logo abaixo já LÊ `_trava_ativa`, e
    # Python recusa a declaração depois do primeiro uso no corpo da função.
    global _trava_ativa, _trava_evento, _trava_motivo, _trava_slot_id, _trava_os_id

    with _lock:
        os_ativa = _estado.get("os_ativa")
    if os_ativa:
        raise ResetRecusado(
            f"OS {os_ativa.get('os_id', '?')} em execução. "
            + ("Libere a trava do Triple Check e espere a OS fechar antes de resetar."
               if _trava_ativa else
               "Espere a OS terminar (ou pause o gerador para não entrar outra).")
        )

    logger.warning("[ORCH] RESET DA PLANTA pedido pelo console "
                   "(limpar_historico=%s).", limpar_historico_tambem)

    relatorio: dict = {
        "os_canceladas":   [],
        "slots_limpos":    [],
        "slots_com_falha": [],
        "tara":            False,
        "homing":          False,
        "historico":       None,
    }

    # 1. Fila — memória e banco juntos. A linha "aguardando" que sobrasse viraria
    #    a OS que `get_ordem_ativa` anuncia para sempre.
    canceladas = _drenar_fila()
    relatorio["os_canceladas"] = canceladas
    if canceladas:
        try:
            await asyncio.to_thread(cancelar_ordens_pendentes, canceladas)
        except Exception as exc:
            logger.warning("[DB] cancelar_ordens_pendentes: %s", exc)

    # 2. Trava e gatilho de injeção: dois estados que sobrevivem ao fim de uma
    #    OS e que ninguém lembra de limpar à mão antes da próxima demonstração.
    if _trava_ativa and _trava_evento is not None:
        _trava_evento.set()
    _trava_ativa = False
    _trava_evento = None
    _trava_motivo = ""
    _trava_slot_id = None
    _trava_os_id = None
    injecao.desarmar()
    # As telas dos slots ficam sabendo que a trava saiu. Aqui dá para esperar:
    # o reset não é caminho de OS, e o teto é TIMEOUT_AVISO_TELAS_S.
    await _avisar_telas(False, None, "", "")

    # 3. Estoque FÍSICO dos slots. Comandar a limpeza de verdade é o ponto: só
    #    zerar `_estado["dispensers"]` deixaria o medicamento dentro do
    #    dispenser, e a OS seguinte tomaria 409 na etapa 1b — o mesmo bug que a
    #    seção "Ciclo de vida de um slot" do CLAUDE.md registra.
    #    Em paralelo, pelo mesmo motivo do `gather` das etapas 3 e 3b: são
    #    NUM_SLOTS comandos e cada um pode retentar por até ~32s.
    resultados = await asyncio.gather(*[
        _liberar_slot(slot, "reset_console") for slot in range(1, NUM_SLOTS + 1)
    ])
    for slot, ok in zip(range(1, NUM_SLOTS + 1), resultados):
        (relatorio["slots_limpos"] if ok else relatorio["slots_com_falha"]).append(slot)
        try:
            await asyncio.to_thread(limpar_dispenser_estado, slot)
        except Exception as exc:
            logger.warning("[DB] limpar_dispenser_estado D%d: %s", slot, exc)

    with _lock:
        for key, slot in _estado["dispensers"].items():
            slot.update({
                "status": "idle", "medicamento": None, "sku": None,
                "categoria": None, "quantidade": 0, "quantidade_alvo": 0,
                "quantidade_dispensada": 0, "quantidade_residual": 0,
                "os_id": None,
            })
        _estado["os_ativa"] = None
        _estado["atribuicao_ia"] = []
        _estado["trava"] = {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""}
        _estado["falha_armada"] = None

    # 4. Balança e CNC. A tara vem DEPOIS da limpeza dos slots: zerar antes e
    #    depois mexer na bancada deixaria o offset da tara contando o peso do
    #    que ainda estava lá.
    relatorio["tara"] = await cmd_tara("reset")
    relatorio["homing"] = await cmd_homing("reset")

    # 5. Histórico — opcional e à parte, porque "repetir a demo" e "apagar o que
    #    já rodou" são decisões diferentes: quase sempre se quer a bancada limpa
    #    COM o histórico de pé, que é o que dá forma ao dashboard.
    if limpar_historico_tambem:
        try:
            relatorio["historico"] = await asyncio.to_thread(limpar_historico)
        except Exception as exc:
            logger.error("[DB] limpar_historico: %s", exc)
            relatorio["historico"] = {"erro": str(exc)}

    # Os eventos pendentes de qualquer OS morta saem junto: chave órfã em
    # `_pending_events` é vazamento de memória e, pior, uma notificação futura
    # que casaria com a espera errada.
    _pending_events.clear()
    _pending_data.clear()

    if _broadcast_fn:
        _broadcast_fn()

    logger.warning("[ORCH] RESET concluído: %d OS canceladas, %d slot(s) limpo(s), "
                   "%d com falha.", len(relatorio["os_canceladas"]),
                   len(relatorio["slots_limpos"]), len(relatorio["slots_com_falha"]))
    return relatorio


def _limpar_eventos_os(os_id: str):
    """Remove todos os eventos pendentes associados a uma OS (evita memory leak e notificações cruzadas)."""
    prefixo = f"{os_id}:"
    chaves_remover = [k for k in list(_pending_events.keys()) if k.startswith(prefixo)]
    for chave in chaves_remover:
        _pending_events.pop(chave, None)
        _pending_data.pop(chave, None)
    if chaves_remover:
        logger.debug("[ORCH] Limpou %d evento(s) pendente(s) da OS %s.", len(chaves_remover), os_id)


async def _abortar_os(os_id: str, motivo: str, atribuicoes: Optional[list] = None):
    """
    Encerra a OS em erro e devolve os slots ao pool.

    O estoque já carregado continua FISICAMENTE no dispenser depois do abort —
    resetar só a memória do central deixava o slot ocupado por um medicamento
    órfão que nenhuma OS futura reclamaria. Por isso cada slot atribuído leva um
    comando de limpeza aqui. A falha da limpeza vira alarme próprio, sem
    sobrescrever `motivo`, que é o que explica o abort.
    """
    logger.error("[ORCH] Abortando OS %s — motivo: %s", os_id, motivo)
    _limpar_eventos_os(os_id)
    try:
        await asyncio.to_thread(atualizar_status_ordem, os_id, "erro")
        await asyncio.to_thread(
            salvar_alarme, "orchestrator", motivo, f"OS {os_id} abortada: {motivo}"
        )
    except Exception as e:
        logger.warning("[DB] _abortar_os: %s", e)

    # ── Descarta o estoque órfão dos slots que a OS chegou a reservar ────────
    nao_liberados: list[int] = []
    for a in atribuicoes or []:
        disp_id = a["dispenser_id"]
        if not await _liberar_slot(disp_id, f"abort_os:{os_id}"):
            nao_liberados.append(disp_id)

    if nao_liberados:
        slots_txt = ", ".join(f"D{d}" for d in nao_liberados)
        logger.error(
            "[ORCH] Limpeza NÃO confirmada em %s após abort da OS %s — "
            "estoque órfão pode ter ficado no slot.", slots_txt, os_id,
        )
        try:
            await asyncio.to_thread(
                salvar_alarme, "orchestrator", "limpeza_pos_abort_falhou",
                f"OS {os_id} abortada ({motivo}): limpeza não confirmada em {slots_txt}",
            )
        except Exception as e:
            logger.warning("[DB] salvar_alarme limpeza_pos_abort_falhou: %s", e)

    with _lock:
        _estado["os_ativa"] = None
        _estado["atribuicao_ia"] = []
        # Só os slots QUE ESTA OS RESERVOU, como no caminho de sucesso. Varrer
        # `_estado["dispensers"]` inteiro apagava o `os_id` de slot que guarda
        # resíduo de outra OS — e `os_id` é justamente o que diz de quem é o
        # medicamento parado ali. Zerado, o resíduo vira órfão sem dono
        # aparente: o painel mostra o slot como idle, e o próximo
        # `atribuir_slots` o toma por livre.
        for a in atribuicoes or []:
            key = str(a["dispenser_id"])
            if key in _estado["dispensers"]:
                _estado["dispensers"][key]["status"] = "idle"
                _estado["dispensers"][key]["os_id"] = None

    if _broadcast_fn:
        _broadcast_fn()


# ── Loop principal do orquestrador ────────────────────────────────────────────

def fila_status() -> dict:
    """Ocupação da fila — o que o erp-simulator consulta antes de gerar OS.

    `tamanho` conta apenas quem ESPERA: a OS em execução já saiu da fila. Por
    isso `os_ativa` e `trava_ativa` vão junto — fila vazia com trava ativa não
    significa planta ociosa, significa planta parada esperando um humano.
    """
    tamanho = _os_queue.qsize()
    capacidade = _os_queue.maxsize or 0
    if _lock:                      # None até o `inicializar` do main
        with _lock:
            os_ativa = bool(_estado.get("os_ativa"))
    else:
        os_ativa = False
    return {
        "tamanho":     tamanho,
        "capacidade":  capacidade,
        "disponivel":  max(capacidade - tamanho, 0),
        "cheia":       tamanho >= capacidade,
        "os_ativa":    os_ativa,
        "trava_ativa": _trava_ativa,
    }


def ha_vaga_na_fila() -> bool:
    """Checagem barata para o endpoint recusar ANTES de persistir a OS."""
    return _os_queue.qsize() < _os_queue.maxsize


async def enfileirar_os(os_payload: dict) -> bool:
    """Chamado pelo endpoint POST /api/v1/ordens. False = fila cheia.

    `put_nowait` em vez de `await put`: com a fila cheia, o `await` deixaria o
    request do gerador pendurado até abrir vaga — que, sob trava, pode ser
    "quando alguém aparecer". Recusar na hora é o que dá ao gerador a chance de
    esperar do lado dele.
    """
    try:
        _os_queue.put_nowait(os_payload)
    except asyncio.QueueFull:
        logger.warning(
            "[ORCH] Fila cheia (%d/%d) — OS %s recusada.",
            _os_queue.qsize(), _os_queue.maxsize, os_payload.get("os_id", "?"),
        )
        return False
    pos = _os_queue.qsize()
    with _lock:
        _estado["fila_tamanho"] = pos
        _estado["fila_capacidade"] = _os_queue.maxsize
        if os_payload["os_id"] not in _estado["fila_os"]:
            _estado["fila_os"].append(os_payload["os_id"])
    logger.info("[ORCH] OS %s enfileirada (posição %d/%d).",
                os_payload["os_id"], pos, _os_queue.maxsize)
    return True


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
            _estado["fila_capacidade"] = _os_queue.maxsize
        try:
            await _processar_os(os_payload)
        except Exception as exc:
            logger.error("[ORCH] Exceção não tratada em _processar_os: %s", exc, exc_info=True)
            # Uma exceção inesperada encerra a OS no MEIO do ciclo, e é
            # justamente o encerramento que precisa fazer o mesmo que um abort
            # explícito: gravar "erro", DESCARTAR o estoque que ficou nos slots
            # e apagar os eventos pendentes da OS morta.
            #
            # Fechar só o status (o que esta cláusula fazia) deixava três
            # rastros: o medicamento fisicamente no dispenser, o `os_id` da OS
            # morta na memória dos slots, e as chaves `{os_id}:...` em
            # `_pending_events`. O sintoma aparecia na OS SEGUINTE, longe daqui
            # — a limpeza prévia batia em 409 no slot que ninguém liberou.
            #
            # As atribuições vêm de `_estado["atribuicao_ia"]`, que `_processar_os`
            # publica na etapa 1: é o registro de quais slots esta OS chegou a
            # reservar. Lista vazia (exceção antes da reserva) faz `_abortar_os`
            # não mandar limpeza nenhuma, que é o correto.
            with _lock:
                atribuicoes = list(_estado.get("atribuicao_ia") or [])
            try:
                await _abortar_os(os_payload.get("os_id", "?"),
                                  "excecao_nao_tratada", atribuicoes)
            except Exception as exc_abort:
                # O abort é a limpeza do caminho de erro; se ELE falhar, o loop
                # ainda precisa seguir para a próxima OS — parar aqui deixaria
                # a planta inteira parada por uma falha de limpeza.
                logger.error("[ORCH] Falha ao abortar OS após exceção: %s",
                             exc_abort, exc_info=True)
                with _lock:
                    _estado["os_ativa"] = None
        finally:
            _os_queue.task_done()
