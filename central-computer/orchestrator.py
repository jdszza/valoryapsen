"""
APSEN - Orquestrador Central v1.0
Toda a inteligência de negócio: fila de OS, IA de atribuição de slots,
planejamento de rota CNC, orquestração de carregamento e dispensa.

Fluxo de uma OS:
  1. OS chega via POST /api/v1/ordens → entra na fila
  1a. CNC: homing (handshake) e foto da caixa VAZIA no HOME — com caixinha na
      caixa a OS espera, não começa
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
  6. Comanda CNC: homing (handshake) e foto da caixa CHEIA no HOME — contagem
     absoluta contra o total confirmado; divergência trava
  7. Comanda CNC: centro — só com a mesa lá a OS é concluída
"""
import asyncio
import logging
import math
import re
from collections import OrderedDict
from datetime import datetime, timezone
from typing import NamedTuple, Optional

import httpx

import chave_visao_tc
import injecao
import os_templates
from config import settings
from database import (
    atribuir_dispenser_item,
    atualizar_status_ordem,
    cancelar_ordens_pendentes,
    get_peso_medicamento,
    limpar_dispenser_estado,
    limpar_historico,
    registrar_visao_ignorada,
    resolver_alarme,
    salvar_alarme,
    salvar_porta_evento,
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
# `POSICOES` é o MODELO LÓGICO da célula, e deixou de ser o mapa que a mesa
# obedece. Ela serve para exatamente duas coisas:
#
#   1. ORDENAR A ROTA (`planejar_rota`, `distancia_rota`). Para isso basta a
#      geometria RELATIVA — quem está de que lado, quem é vizinho de quem —, e
#      essa continua correta: a serpentina é ótima pelo formato do arranjo, não
#      pelos milímetros.
#   2. PREENCHER O HISTÓRICO quando a máquina não informa a posição, e só aí
#      (ver o passo 4c de `_processar_os`).
#
# O que mudou: a mesa agora executa um roteiro MEDIDO nela, gravado waypoint a
# waypoint na bancada e indexado pelo dispenser. Mandar coordenadas para ela
# seria mandar um número que ela ignora — e número ignorado que continua sendo
# gravado no banco é pior que nenhum, porque ninguém descobre que ele não
# descreve a máquina. Em troca, a placa REPORTA a posição medida no evento
# `posicionado`, e é dela que saem a câmera da mesa e o `cnc_eventos`.
#
# O desenho acima fica: é ele que explica por que a serpentina é a rota certa.
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

# ── Cancelamento do cronograma ────────────────────────────────────────────────
# Um Event por OS, criado no início de `_processar_os`. Enquanto o ciclo da mesa
# corre por RELÓGIO, este é o único jeito de interromper um prazo EM CURSO: sem
# ele, um cancelamento só teria efeito no ciclo seguinte, e "o ciclo seguinte"
# pode ser um `dispensar` que já saiu.
#
# Ele é ARMADO por quem precisa parar a OS agora (a trava do Triple Check, o
# reset da planta) e LIMPO por quem a retoma. Esquecer de limpar é o modo de
# falhar desta peça: todo prazo posterior venceria na hora, a OS terminaria em
# silêncio no meio da rota, e nada no log diria por quê — por isso a limpeza
# mora no mesmo bloco que espera a liberação, e não num chamador distante.
_cancelar_cronograma: Optional[asyncio.Event] = None

# ── A janela entre a checagem do reset e o reset ──────────────────────────────
#
# `resetar_planta` recusa com OS em execução, e a decisão de recusar é a peça
# central dela. Mas entre a LEITURA de `_estado["os_ativa"]` e o primeiro
# `cmd_limpar` há `await`s — e o `loop_orquestrador` roda no mesmo event loop.
# Uma OS que estivesse esperando na fila podia começar exatamente aí, e o reset
# então mandaria `cmd_limpar` para slots que ela acabou de carregar: 409
# `limpeza_em_operacao` e um reset pela metade, que é o estado mais difícil de
# diagnosticar que esta planta produz.
#
# O flag fecha a janela pelo outro lado: enquanto ele está de pé, o loop NÃO
# começa OS nenhuma — ele fecha em `cancelada` o que tirou da fila e volta a
# esperar. Uma OS cancelada por um reset é exatamente o que o reset já faz com
# a fila inteira (`cancelar_ordens_pendentes`), então o desfecho é o mesmo,
# venha ela de antes ou de durante.
_reset_em_curso: bool = False


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
    # Com a porta NR-12 habilitada o central nasce PARADO: uma porta que nunca
    # falou é tratada como link perdido, e o primeiro `estado` fechado e
    # travado é o que libera a célula (com homing, como toda retomada).
    if settings.PORTA_NR12_HABILITADA:
        _entrar_em_pausa("link_perdido")
    _publicar_porta()
    _publicar_visao_tc()


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
    global _trava_ativa, _trava_motivo, _trava_slot_id, _trava_os_id, _trava_resumo
    if not _trava_ativa or _trava_evento is None:
        return False
    logger.warning("[TRAVA] Trava liberada por '%s'.", liberado_por)
    _trava_ativa   = False
    _trava_motivo  = ""
    _trava_slot_id = None
    _trava_os_id   = None
    _trava_resumo  = ""
    if _loop and not _loop.is_closed():
        _loop.call_soon_threadsafe(_trava_evento.set)
    else:
        _trava_evento.set()
    # As telas dos slots voltam ao normal. Agendado, não esperado: esta função
    # roda numa thread (`to_thread`, a partir do endpoint) e a resposta ao
    # supervisor não pode depender da placa das telas.
    _agendar_aviso_telas(False, None, None, "")
    # Porta já rearmada esperando só a trava: as duas resolvidas, a máquina
    # retoma — em qualquer ordem.
    if _pausa_porta is not None and not _pausa_porta.get("aguardando_rearme"):
        _agendar_porta(_tentar_retomar("trava_liberada"))
    return True


# ── Switch da visão no Triple Check ─────────────────────────────────────────────
#
# Desligado pelo console, a visão (câmera da mesa por slot, câmeras dos
# dispensers, fotos VAZIA/FINAL no HOME) continua MEDINDO — as fotos saem, a
# estação registra o total, `pendentes_mesa`/`deficit_mesa`/dessincronia andam
# como sempre — e só o VEREDITO a deixa de fora: o que ela disser vira o alarme
# `visao_ignorada`. É por continuar medindo que dá para religar no meio da OS
# sem "a mais" falso na foto seguinte.
#
# O estado mora em `chave_visao_tc` e é lido em CADA decisão (`visao_conta()`),
# nunca copiado no começo da OS. Ele não libera trava já ativa: liberar continua
# sendo o botão de sempre.

# Quantas leituras foram deixadas de fora na OS em execução (o console mostra).
_visao_ignoradas_os: int = 0


def visao_conta() -> bool:
    return chave_visao_tc.visao_conta()


def get_visao_tc() -> dict:
    return {**chave_visao_tc.chave.estado(), "ignoradas_na_os_atual": _visao_ignoradas_os}


def _publicar_visao_tc() -> None:
    """Estado do switch em `_estado` (o `/ws` do console). Estado real primeiro,
    tela depois — a ordem de `_ativar_trava`."""
    if _lock:
        with _lock:
            _estado["visao_triple_check"] = get_visao_tc()
    if _broadcast_fn:
        _broadcast_fn()


def _zerar_visao_ignoradas() -> None:
    global _visao_ignoradas_os
    _visao_ignoradas_os = 0
    _publicar_visao_tc()


MSG_VISAO_IGNORADA = "visão ignorada no Triple Check (switch desligado)"


async def _registrar_visao_ignorada(os_id: str, slot_id: Optional[int], texto: str) -> None:
    """UM alarme `visao_ignorada` por leitura deixada de fora, a contagem da OS
    e o marcador do slot. Nunca levanta: é o caminho em que a OS SEGUE."""
    global _visao_ignoradas_os
    _visao_ignoradas_os += 1
    alvo = f"OS {os_id}" + (f" D{slot_id}" if slot_id is not None else "")
    descricao = f"{MSG_VISAO_IGNORADA}: {alvo} — {texto}"
    logger.warning("[ORCH] %s", descricao)
    _historico("visao_ignorada", descricao)
    try:
        await asyncio.to_thread(salvar_alarme, "visao_ignorada", "visao_ignorada", descricao)
    except Exception as e:
        logger.warning("[DB] salvar_alarme visao_ignorada: %s", e)
    try:
        await asyncio.to_thread(registrar_visao_ignorada, os_id, slot_id)
    except Exception as e:
        logger.warning("[DB] registrar_visao_ignorada: %s", e)
    _publicar_visao_tc()


class VereditoSku(NamedTuple):
    """O que a leitura da câmera de um dispenser decide (passo 3b e re-scan).

    `travar` = SKU comprovadamente errado com a visão no Triple Check (ou a
    câmera muda no re-scan de um slot que já divergiu). `ignorada` = o texto
    do alarme `visao_ignorada` quando o switch está desligado e a leitura
    teria travado ou alarmado.
    """
    travar: bool
    ignorada: Optional[str] = None


def avaliar_sku_dispenser(resultado: Optional[dict], visao_conta: bool = True,
                          rescan: bool = False) -> VereditoSku:
    """PURA. A regra do SKU, com o switch.

    Com a visão no Triple Check é a de sempre: divergência trava; falha de
    leitura e timeout não (fonte que não mediu), EXCETO no re-scan, onde o
    silêncio mantém a trava (`_VISAO_INDISPONIVEL`). Desligado, nada daqui
    trava — a divergência, a falha e o timeout viram `ignorada`. A exceção é
    `sem_etiqueta_cadastrada`: é cobertura, não leitura, e não alarma em
    posição nenhuma do switch.
    """
    tipo = (resultado or {}).get("tipo")
    if tipo == "leitura_dispenser_divergencia" or (rescan and tipo == _VISAO_INDISPONIVEL):
        if visao_conta:
            return VereditoSku(True)
        if tipo == _VISAO_INDISPONIVEL:
            return VereditoSku(False, "câmera do dispenser sem resposta no re-scan de "
                                      "um SKU divergente")
        return VereditoSku(False, f"câmera do dispenser: SKU lido="
                                  f"{resultado.get('sku_lido', '?')} esperado="
                                  f"{resultado.get('sku_esperado', '?')}")
    if visao_conta:
        return VereditoSku(False)
    if resultado is None:
        return VereditoSku(False, "câmera do dispenser sem resposta (timeout)")
    if tipo == "leitura_dispenser_falha":
        if resultado.get("motivo") == "sem_etiqueta_cadastrada":
            return VereditoSku(False)
        return VereditoSku(False, f"câmera do dispenser: falha de leitura "
                                  f"({resultado.get('motivo') or 'desconhecido'})")
    return VereditoSku(False)


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
    global _trava_resumo
    _trava_resumo = resumo
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

    # E o cronograma morre AQUI, antes de qualquer aviso a qualquer placa.
    #
    # A ordem é a feature. Avisar a mesa custa uma requisição com
    # TIMEOUT_AVISO_TELAS_S (3 s) de teto, e 3 s é tempo de sobra para o relógio
    # disparar mais um `dispensar` — o comando sairia DEPOIS de a trava existir,
    # com a mesa já indo para o HOME, e o medicamento cairia no caminho.
    # Cancelar primeiro custa uma linha e fecha essa janela inteira.
    cancelar_cronograma()

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


def espiar_evento(chave: str) -> Optional[dict]:
    """Lê o evento que já chegou, SEM esperar e SEM cancelar o registro.

    Por que não `aguardar_evento(chave, 0)`, que seria o caminho óbvio: ele
    desregistra a chave no `finally`. Uma espiada que não achasse nada apagaria
    o registro, e o evento que chegasse 200 ms depois seria DESCARTADO por
    `notificar_evento` — que ignora chave sem ninguém esperando. No modelo por
    relógio isso é o modo de falhar mais provável, porque a espiada acontece
    justamente enquanto o evento ainda está a caminho: o `posicionado` que
    atravessa o adapter enquanto o `dispensar` já saiu tem de continuar
    chegando, e é ele que carrega a posição MEDIDA para a câmera da mesa.

    O segundo motivo é menor e vale registrar: `wait_for(..., timeout=0)` é um
    `await` que cede ao loop para produzir um `None` que já se sabe.
    """
    return _pending_data.get(chave)


def colher_evento(chave: str) -> Optional[dict]:
    """Lê e ENCERRA o registro — no fim do ciclo, quando não se espera mais nada.

    O par de `espiar_evento`: espiar durante o ciclo não desregistra, colher no
    fim desregistra. Sem a colheita, `_pending_events` cresceria uma entrada por
    slot por OS e `_limpar_eventos_os` teria de varrer o que este ciclo deixou.
    """
    dados = _pending_data.pop(chave, None)
    _pending_events.pop(chave, None)
    return dados


def cronograma_do_ciclo(quantidade: int) -> tuple[float, float]:
    """(espera até o `dispensar`, duração da dispensa), em segundos.

    O ciclo da mesa não espera confirmação: o central calcula quando cada peça
    acontece e dispara no relógio. Esta função é esse cálculo, e é PURA de
    propósito — dá para conferir o cronograma de uma OS inteira sem subir nada.

        espera   = CNC_TETO_TRAJETO_S + CNC_MARGEM_CHEGADA_S
        dispensa = quantidade × DISPENSA_S_POR_UNIDADE + DISPENSA_FOLGA_S

    A segunda conta é a MESMA que o `WP()` do firmware usa para gravar o dwell
    das receitas — `(quantidade + 1) × 1000 ms`, com 1 s por unidade. As duas
    descrevem o mesmo mecanismo físico (um ciclo de servo por comprimido) visto
    de dois lugares, e **têm de ser mudadas juntas**: se o servo real for mais
    lento e só o firmware for ajustado, o central segue cortando a dispensa no
    meio — a OS termina "completa" com menos comprimido do que o leito precisa,
    que é o pior desfecho possível desta feature.

    `quantidade` menor que zero é tratada como zero: a folga sozinha ainda é um
    prazo válido, e um cronograma negativo faria o `dispensar` e a colheita
    saírem no mesmo instante.
    """
    espera = settings.CNC_TETO_TRAJETO_S + settings.CNC_MARGEM_CHEGADA_S
    dispensa = max(0, quantidade) * settings.DISPENSA_S_POR_UNIDADE + settings.DISPENSA_FOLGA_S
    return espera, dispensa


async def _dormir_ou_cancelar(segundos: float, cancelar: asyncio.Event) -> bool:
    """Cumpre o prazo, ou acorda antes se `cancelar` for disparado.

    Devolve True quando o prazo foi cumprido (o caminho normal) e False quando
    foi cancelado.

    **`asyncio.wait` e não `wait_for`, e o motivo é o sinal invertido.** Com
    `wait_for(cancelar.wait(), timeout=segundos)`, o caminho NORMAL — o prazo
    vencendo sem cancelamento nenhum — é o que levanta `TimeoutError`. Além de
    pagar a construção de uma exceção a cada perna de cada ciclo, isso põe o
    caso esperado no ramo `except`: qualquer `try/except asyncio.TimeoutError`
    acrescentado em volta um dia engoliria o caminho feliz sem que nada
    parecesse errado. `asyncio.wait` diz o que aconteceu pelo RETORNO — o
    conjunto `feitos` vem vazio quando o prazo venceu —, e o cancelamento, que é
    a exceção de verdade, fica sendo a exceção também na leitura do código.
    """
    espera = asyncio.ensure_future(cancelar.wait())
    try:
        feitos, _ = await asyncio.wait({espera}, timeout=segundos)
    finally:
        if not espera.done():
            espera.cancel()
            # Aguardar a task cancelada evita o "Task was destroyed but it is
            # pending" que apareceria no log do central a cada ciclo.
            try:
                await espera
            except asyncio.CancelledError:
                pass
    return not feitos


# Os quatro desfechos de um ciclo. Fechado, e cada um com nome: um ciclo que
# termina sem nome no log é um ciclo que ninguém consegue auditar depois, e no
# modelo por relógio a maior parte dos desfechos não é mais "abortou".
DESFECHO_COMPLETO        = "completo"          # chegou tudo e a quantidade bate
DESFECHO_CURTO           = "curto"             # dispensou menos que o alvo
DESFECHO_SEM_CONFIRMACAO = "sem_confirmacao"   # o prazo venceu sem o `dispensado`
DESFECHO_ERRO            = "erro"              # evento `erro` de uma das pontas


async def _aguardar_liberacao(evento_liberacao: asyncio.Event,
                              cancelar: asyncio.Event) -> None:
    """Espera o supervisor liberar a trava e RETOMA o cronograma.

    A limpeza do cancelamento mora aqui, colada na espera, e não em cada bloco
    que trava. `_ativar_trava` arma o cancelamento para matar qualquer prazo em
    curso; quem esperou a liberação é quem sabe que a pausa acabou.

    Espalhada pelos chamadores, a limpeza vira coisa de lembrar — e o que
    acontece é o que já aconteceu: o bloco de SKU errado (etapa 3b) trava pela
    MESMA função e tem o próprio ponto de retomada. Sem passar por aqui, ele
    voltava com o cancelamento ainda armado, o primeiro prazo do ciclo da mesa
    vencia na hora, e a OS terminava no meio da rota sem uma linha de log
    dizendo por quê — com a bancada inteira íntegra.
    """
    await evento_liberacao.wait()
    # A trava liberada não basta com a porta NR-12 em pausa: retomar exige as
    # duas resolvidas. Esperar aqui, e não em cada chamador, pelo mesmo motivo
    # da limpeza do cancelamento logo abaixo.
    await _aguardar_porta_livre()
    cancelar.clear()


def cancelar_cronograma() -> None:
    """Arma o cancelamento do cronograma da OS em curso, se houver uma.

    Seguro de chamar sem OS ativa (vira no-op) e de chamar duas vezes. Quem
    retoma a OS é quem limpa — ver `_cancelar_cronograma`.
    """
    if _cancelar_cronograma is not None and not _cancelar_cronograma.is_set():
        _cancelar_cronograma.set()


def _desfecho_do_ciclo(disp_id: int, qtd_esperada: int,
                       resultado_pos: Optional[dict],
                       resultado_disp: Optional[dict]) -> tuple[str, Optional[int]]:
    """Classifica o ciclo e devolve (desfecho, quantidade medida pelo dispenser).

    A quantidade volta como `None` quando o dispenser NÃO confirmou — e é esse
    `None` que segue para `avaliar_triple_check`. Devolver o alvo no lugar dele,
    que é o que a versão de handshake fazia com `.get(..., qtd_esperada)`, faria
    a fonte 1 CONFIRMAR uma contagem que ninguém fez: o Triple Check viraria um
    double check sem que nada no log dissesse isso.

    Nenhum desfecho aborta a OS aqui. "curto" e "sem_confirmacao" são
    PENDÊNCIAS, e quem decide sobre elas é o Triple Check no fim do ciclo, que
    já compara três fontes — é para lá que elas vão, e não para uma lista
    paralela que alguém teria de lembrar de consultar.
    """
    if resultado_disp is not None and resultado_disp.get("tipo") == "erro":
        return DESFECHO_ERRO, None
    if resultado_pos is not None and resultado_pos.get("tipo") == "erro":
        return DESFECHO_ERRO, None
    if resultado_disp is None:
        return DESFECHO_SEM_CONFIRMACAO, None

    qtd = resultado_disp.get("quantidade_dispensada")
    if qtd is None:
        # O evento veio, mas sem a contagem (contrato antigo). É o mesmo
        # "não confirmou" — a fonte 1 não tem o que dizer.
        return DESFECHO_SEM_CONFIRMACAO, None
    if qtd < qtd_esperada:
        return DESFECHO_CURTO, int(qtd)
    return DESFECHO_COMPLETO, int(qtd)


def _registrar_desfecho(disp_id: int, os_id: str, desfecho: str,
                        qtd_dispensada: Optional[int], qtd_esperada: int,
                        medicamento: str) -> None:
    """Log + estado publicado. O banco fica com o handler do evento, como sempre.

    O desfecho entra em `_estado["dispensers"][slot]` para aparecer no painel
    pelo WebSocket que já existe: um ciclo "sem_confirmacao" precisa ser visível
    ENQUANTO a OS corre, não só no relatório depois — é ele que explica por que
    o Triple Check travou dois slots adiante.
    """
    medido = "—" if qtd_dispensada is None else str(qtd_dispensada)
    if desfecho == DESFECHO_COMPLETO:
        logger.info("[ORCH] D%d ciclo %s — dispensou %s/%d × %s.",
                    disp_id, desfecho, medido, qtd_esperada, medicamento)
    else:
        logger.warning("[ORCH] D%d ciclo %s — dispensou %s/%d × %s. "
                       "A OS SEGUE; quem decide é o Triple Check.",
                       disp_id, desfecho, medido, qtd_esperada, medicamento)

    with _lock:
        slot = _estado["dispensers"].get(str(disp_id))
        if slot is not None:
            slot["ultimo_ciclo"] = {
                "desfecho": desfecho,
                "os_id": os_id,
                "quantidade_dispensada": qtd_dispensada,
                "quantidade_esperada": qtd_esperada,
            }


# Marcador interno: a câmera não respondeu ao re-scan da trava de SKU. NÃO é um
# `tipo` de evento do contrato — nenhum adapter o emite, e nenhum teste de
# protocolo o conhece. Ele existe para o laço de trava distinguir "a câmera leu
# e está OK" de "a câmera não disse nada", que é a diferença entre soltar e não
# soltar um slot com SKU comprovadamente errado.
_VISAO_INDISPONIVEL = "visao_indisponivel"


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
    # O que não trava mas precisa de alguém olhando: hoje, a câmera da mesa
    # contando A MENOS sem nenhuma outra fonte acusar (possível oclusão). Vira
    # o alarme `contagem_camera_abaixo` no passo 4e.
    alertas: tuple = ()
    # Unidades que a câmera da mesa contou A MENOS, sozinha, neste veredito (o
    # caso de `alertas`). A estação REGISTRA o total dessa foto, então a
    # caixinha escondida aqui reaparece como "a mais" numa foto seguinte — o
    # passo 4 guarda a soma em `deficit_mesa` para não travar o slot vizinho.
    oclusao: int = 0
    # Unidades "a mais" absorvidas por `tolerancia_camera_mais`: as que se
    # esconderam antes e reapareceram. Não trava e não vira alarme — é log.
    reconciliado: int = 0
    # Mensagens só de log, sem alarme (hoje: a reconciliação acima).
    informativos: tuple = ()
    # Com o switch "Visão no Triple Check" DESLIGADO: o que a câmera da mesa
    # disse e que, com ele ligado, teria virado divergência, alerta de oclusão
    # ou falha de leitura. Não trava; cada item vira um alarme `visao_ignorada`.
    visao_ignorada: tuple = ()
    # A leitura da câmera da mesa como chegou — o veredito não a descarta só
    # porque ela não conta (o relatório mostra o que a câmera viu).
    leitura_mesa: Optional[dict] = None


def _inteiro(valor, padrao: int = 0) -> int:
    """O número do payload como int, nunca uma exceção — a mesma conversão de
    `main._inteiro` (o payload do adapter atravessa sem interpretação)."""
    try:
        return int(valor)
    except (TypeError, ValueError):
        return padrao


def avaliar_triple_check(
    quantidade_esperada: int,
    quantidade_dispensada: Optional[int],
    resultado_mesa: Optional[dict],
    resultado_peso: Optional[dict],
    min_divergencias: Optional[int] = None,
    esperado_camera: Optional[int] = None,
    slots_camera: tuple = (),
    motivo_mesa_indisponivel: Optional[str] = None,
    tolerancia_camera_mais: int = 0,
    visao_conta: bool = True,
) -> ResultadoTripleCheck:
    """
    Confronta as 3 fontes independentes de contagem e decide se a OS trava.

    Fonte 1 — dispenser: quantidade contada mecanicamente (`quantidade_dispensada`);
               `None` quando o `dispensado` não chegou no prazo do cronograma.
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

    **A câmera da mesa contando A MENOS, sozinha, não trava — vira alerta.** A
    câmera é fixa e inclinada, e a visão não distingue caixinha escondida atrás
    da parede da caixa de caixinha que não caiu: não há critério de oclusão no
    `_avaliar` da estação. Contar a menos é divergência só se o dispenser ou a
    balança também acusarem neste mesmo veredito. Contar A MAIS continua
    travando: caixinha a mais não se esconde — é unidade extra ou objeto
    estranho. A diferença é calculada AQUI, dos dois inteiros, e nunca lida do
    `delta` do payload.

    A câmera pode ter conferido mais de um slot de uma vez (posições em que ela
    não vê a caixa inteira, ou captura anterior que falhou): `esperado_camera`
    é o total esperado nessa foto e `slots_camera` os slots que ela cobre.
    None/() é uma foto de um slot só, contra `quantidade_esperada`.
    `motivo_mesa_indisponivel` dá o porquê quando `resultado_mesa` é None e não
    foi timeout (posição sem visão, câmera ressincronizando).

    **O que se escondeu num slot reaparece no outro.** A estação registra o
    total da foto que contou a menos, e a caixa anda com a CNC: a caixinha que
    estava atrás da parede volta a aparecer numa foto seguinte, como "a mais".
    `tolerancia_camera_mais` é quantas unidades a câmera ainda deve de fotos
    anteriores (o `deficit_mesa` do passo 4): "a mais" até esse número é
    reconciliação (`reconciliado`, log), e só o EXCEDENTE é divergência. O
    default 0 é o comportamento de antes.

    **`visao_conta=False` é o switch do console desligado.** A câmera é avaliada
    EXATAMENTE como sempre — inclusive `oclusao` e `reconciliado`, que mantêm o
    `deficit_mesa` do ciclo correto para quando o switch voltar —, mas o que ela
    produziria (divergência, alerta de oclusão, falha de leitura ou timeout)
    vai para `visao_ignorada` em vez de pesar no veredito. Dispenser e balança
    decidem sozinhos, com o mesmo limiar. Sem nenhuma das três medindo, o
    veredito é o de hoje: nada contradisse, a OS segue. Default True = o
    comportamento de antes do switch.

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
    alertas: list[str] = []
    informativos: list[str] = []
    oclusao = reconciliado = 0

    # Fonte 1 — dispenser. `None` significa que o `dispensado` NÃO chegou dentro
    # do prazo do cronograma, e isso deixou de abortar a OS quando o ciclo
    # passou a ser por relógio. É a mesma distinção que já valia para as outras
    # duas fontes: o dispenser não contradisse nada, apenas não confirmou.
    #
    # Contá-lo como divergência seria transformar todo evento perdido no
    # encaminhamento (o `_post_central` do adapter desiste depois de 3
    # tentativas) numa trava — trava por ruído é trava desligada em campo. E
    # assumir o alvo, que é o que a versão anterior fazia com o `.get(...,
    # qtd_esperada)`, seria pior: a fonte 1 passaria a CONFIRMAR uma contagem
    # que ninguém fez, e o Triple Check viraria um double check sem avisar.
    # As três fontes são decididas primeiro e montadas depois, na ordem de
    # sempre (dispenser, câmera, balança): a câmera a menos depende de saber se
    # ALGUMA das outras duas acusou, inclusive a balança, que vem depois.
    div_disp = div_cam = div_peso = None
    indisponivel_peso = None
    # O que a câmera disse e que, com o switch desligado, só vira informação.
    falha_cam: Optional[str] = None
    visao_ignorada: list[str] = []

    if quantidade_dispensada is None:
        indisponiveis.append("dispenser: sem confirmação de dispensa (prazo vencido)")
    elif quantidade_dispensada != quantidade_esperada:
        div_disp = f"dispenser: dispensou {quantidade_dispensada} de {quantidade_esperada} esperados"

    # Fonte 3 — balança HX711
    if resultado_peso is None:
        indisponivel_peso = "balança: sem resposta (timeout)"
    elif resultado_peso.get("tipo") == "peso_divergencia":
        desvio = resultado_peso.get("desvio_pct") or 0
        div_peso = f"balança: desvio={desvio:.1f}%"
    elif resultado_peso.get("tipo") == "erro_sensor":
        indisponivel_peso = "balança: sensor indisponível"

    # Fonte 2 — câmera da mesa
    if resultado_mesa is None:
        indisponiveis.append(
            f"câmera_mesa: {motivo_mesa_indisponivel or 'sem resposta (timeout)'}")
        # Só o timeout é falha de leitura. Os motivos que o ciclo dá (posição
        # sem visão, ressincronizando, comando recusado) já têm o rastro deles.
        if motivo_mesa_indisponivel is None:
            falha_cam = "câmera_mesa: sem resposta (timeout)"
    elif resultado_mesa.get("tipo") == "leitura_mesa_divergencia":
        esperado = (quantidade_esperada if esperado_camera is None
                    else _inteiro(esperado_camera, quantidade_esperada))
        det = _inteiro(resultado_mesa.get("quantidade_detectada"))
        delta = det - esperado
        cobertura = (f" ({'+'.join(f'D{s}' for s in slots_camera)})"
                     if len(slots_camera) > 1 else "")
        tolerancia = max(0, tolerancia_camera_mais)
        if 0 < delta <= tolerancia:
            reconciliado = delta
            informativos.append(f"câmera_mesa: {delta} unidade(s) antes escondida(s) "
                                f"reapareceram{cobertura}")
        elif delta > 0:
            reconciliado = tolerancia
            excedente = (f" — {delta - tolerancia} a mais além de {tolerancia} antes "
                         f"escondida(s)" if tolerancia else "")
            div_cam = f"câmera_mesa: detectou {det} de {esperado}{cobertura}{excedente}"
        elif delta < 0 and (div_disp or div_peso):
            div_cam = f"câmera_mesa: detectou {det} de {esperado}{cobertura}"
        elif delta < 0:
            oclusao = -delta
            alertas.append(f"câmera_mesa: contou {-delta} a menos{cobertura} — possível "
                           f"oclusão; balança e dispenser não acusaram")
        else:
            logger.warning("[ORCH] leitura_mesa_divergencia com contagem igual à esperada "
                           "(%d de %d) — payload incoerente, câmera tratada como "
                           "sem medição.", det, esperado)
            indisponiveis.append("câmera_mesa: divergência sem diferença de contagem "
                                 "(payload incoerente)")
    elif resultado_mesa.get("tipo") == "leitura_mesa_falha":
        indisponiveis.append("câmera_mesa: falha de leitura")
        falha_cam = (f"câmera_mesa: falha de leitura "
                     f"({resultado_mesa.get('motivo') or 'desconhecido'})")

    if not visao_conta:
        # A conta da câmera continua feita (oclusão e reconciliação seguem
        # valendo para o `deficit_mesa`); só o efeito no veredito sai.
        if div_cam:
            visao_ignorada.append(div_cam)
            div_cam = None
        visao_ignorada.extend(alertas)
        alertas = []
        if falha_cam:
            visao_ignorada.append(falha_cam)

    if indisponivel_peso:
        indisponiveis.append(indisponivel_peso)

    for texto, categoria in ((div_disp, "dispenser divergente"),
                             (div_cam, "contagem divergente"),
                             (div_peso, "divergência de peso")):
        if texto:
            divergencias.append(texto)
            categorias.append(categoria)

    return ResultadoTripleCheck(
        travar=len(divergencias) >= limiar,
        divergencias=divergencias,
        fontes_indisponiveis=indisponiveis,
        limiar=limiar,
        categorias=tuple(categorias),
        alertas=tuple(alertas),
        oclusao=oclusao,
        reconciliado=reconciliado,
        informativos=tuple(informativos),
        visao_ignorada=tuple(visao_ignorada),
        leitura_mesa=resultado_mesa,
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


async def _avisar_uma_placa(alvo: str, url: str, payload: dict) -> bool:
    """POST único em `/comandos/estado-celula`. Nunca levanta; devolve se chegou."""
    if _client is None:
        return False
    try:
        r = await _client.post(url, json=payload, timeout=TIMEOUT_AVISO_TELAS_S)
    except Exception as exc:  # noqa: BLE001 — cosmético: loga e segue
        logger.warning("[TELAS] estado-celula não entregue a %s (%s) — a placa "
                       "fica desatualizada até o próximo aviso.", alvo, exc)
        return False
    if r.status_code >= 300:
        logger.warning("[TELAS] estado-celula → %s HTTP %d — a placa fica "
                       "desatualizada até o próximo aviso.", alvo, r.status_code)
        return False
    try:
        telas = (r.json() or {}).get("telas")
    except Exception:  # noqa: BLE001 — corpo não é JSON: o status já disse que chegou
        telas = None
    if telas and telas != "ok":
        logger.warning("[TELAS] estado-celula aceito por %s, telas=%s", alvo, telas)
    return True


async def _avisar_telas(trava_ativa: bool, slot_id: Optional[int],
                        os_id: Optional[str], resumo: str, so_telas: bool = False) -> bool:
    """Avisa as DUAS placas que precisam saber da trava, em paralelo.

    As telas TFT, porque elas mostram ao operador de qual slot é a divergência.
    E a MESA, que é a peça fisicamente sobre a bancada onde o supervisor vai
    mexer — e que, sob o ciclo por relógio, é também a peça que continua andando
    sozinha se ninguém a avisar.

    **Em `gather`, não em sequência.** Em série, uma placa fora do ar somaria o
    seu `TIMEOUT_AVISO_TELAS_S` ao prazo da outra, e o aviso à mesa (a que
    importa, porque ela se move) chegaria depois de um timeout inteiro gasto
    esperando uma tela. `return_exceptions=True` pelo mesmo motivo de sempre:
    isto é cosmético para as telas e defensivo para a mesa, e nenhum dos dois
    pode derrubar o caminho da trava.

    O cancelamento do cronograma NÃO está aqui — ele acontece em
    `_ativar_trava`, antes desta função ser sequer agendada. Ver o comentário
    de lá: 3 s de teto é tempo de sobra para o relógio disparar mais um
    `dispensar`.
    """
    payload = {
        "trava_ativa":   bool(trava_ativa),
        "trava_slot_id": slot_id,
        "os_id":         os_id or "",
        "trava_resumo":  (resumo or "")[:TRAVA_RESUMO_MAX],
    }
    # A pausa da porta NR-12 vai SÓ às telas: a mesa recebe `estado_celula`
    # como trava, e uma trava a manda ao HOME — exatamente o que a porta aberta
    # não pode fazer. Quem para a mesa na pausa é o `parar`.
    #
    # Os dois campos da pausa só vão quando há pausa, ou no próprio aviso dela
    # (`so_telas`, que também é o da RETOMADA, com `pausa_porta: false`). Sem
    # porta NR-12 o aviso da trava sai byte a byte como antes — e a placa trata
    # o campo ausente como "sem pausa".
    payload_telas = dict(payload)
    if so_telas or _pausa_porta is not None:
        payload_telas["pausa_porta"] = _pausa_porta is not None
        payload_telas["pausa_resumo"] = resumo_da_pausa_para_telas(faixa_da_porta())
    alvos = [("telas", settings.DISPENSER_ADAPTER_URL + "/comandos/estado-celula",
              payload_telas)]
    if not so_telas:
        alvos.append(("mesa", settings.CNC_ADAPTER_URL + "/comandos/estado-celula", payload))
    resultados = await asyncio.gather(
        *(_avisar_uma_placa(alvo, url, corpo) for alvo, url, corpo in alvos),
        return_exceptions=True,
    )
    return all(r is True for r in resultados)


def _agendar_aviso_telas(trava_ativa: bool, slot_id: Optional[int],
                         os_id: Optional[str], resumo: str, so_telas: bool = False) -> None:
    """Dispara `_avisar_telas` SEM esperar por ele.

    Serve de dentro do event loop (`_ativar_trava`) e de fora dele
    (`liberar_trava`, que o main chama por `to_thread`). Não esperar é o que
    mantém a ativação da trava na casa de milissegundos com o adapter fora do
    ar — o aviso corre em paralelo, e a trava já está armada e publicada.
    """
    coro = _avisar_telas(trava_ativa, slot_id, os_id, resumo, so_telas)
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


async def cmd_dispensar(disp_id: int, os_id: str,
                        quantidade: Optional[int] = None) -> bool:
    """`quantidade` só na RETOMADA depois de uma pausa da porta: o que falta.

    Ausente, o corpo sai byte a byte como antes.
    """
    corpo = {"dispenser_id": disp_id, "os_id": os_id,
             **_injecao_para("/comandos/dispensar", disp_id)}
    if quantidade is not None:
        corpo["quantidade"] = quantidade
    return await _post(settings.DISPENSER_ADAPTER_URL + "/comandos/dispensar", corpo)


async def _reposicionar(disp_id: int, os_id: str, receita: str, seq: int,
                        total: int, cancelar: asyncio.Event) -> Optional[dict]:
    """A mesa de volta ao slot, só para a foto, depois de uma pausa ou trava.

    A dispensa do slot já terminou; o que se perdeu foi a POSIÇÃO (a retomada
    faz homing). Sem voltar, a câmera da mesa fotografaria a caixa no HOME e
    chamaria aquilo de slot.
    """
    while True:
        await _aguardar_retomada_porta(cancelar)
        geracao = _porta_geracao
        chave = f"{os_id}:posicionado:{disp_id}"
        registrar_evento(chave)
        ok = await cmd_mover(disp_id, os_id, receita, seq, total)
        espera, _ = cronograma_do_ciclo(0)
        cumpriu = ok and await _dormir_ou_cancelar(espera, cancelar)
        if _houve_pausa(geracao):
            colher_evento(chave)
            continue
        resultado = colher_evento(chave)
        if not ok or not cumpriu or (resultado and resultado.get("tipo") == "erro"):
            logger.warning("[ORCH] D%d: a mesa não voltou ao slot para a foto.", disp_id)
            return None
        return resultado


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


def _posicao_do_evento(evento: Optional[dict], disp_id: int) -> tuple[float, float]:
    """Posição para a câmera da mesa: a MEDIDA, ou o modelo com aviso.

    Um lugar só, porque o fallback é a parte que se esquece: o evento pode não
    ter vindo (timeout) ou vir sem os campos (contrato antigo), e as duas
    coisas precisam da mesma resposta.
    """
    if evento:
        px = evento.get("posicao_x")
        py = evento.get("posicao_y")
        if px is not None and py is not None:
            return float(px), float(py)

    modelo = POSICOES[disp_id]
    logger.warning(
        "[ORCH] D%d sem posição medida no evento — usando o MODELO (%.1f, %.1f). "
        "A câmera da mesa vai olhar para onde o modelo diz que o slot está, não "
        "para onde a máquina parou.",
        disp_id, modelo[0], modelo[1],
    )
    return modelo


async def cmd_mover(disp_id: int, os_id: str, receita: str,
                    ciclo: int, total: int) -> bool:
    """Manda a mesa ao dispenser — pela RECEITA, não por coordenada.

    A mesa segue o roteiro que ela mesma gravou: `receita` diz QUAL ordem está
    em execução (a letra A–J do slot na NVS da placa) e `dispenser_alvo` diz
    qual parada daquele roteiro. O waypoint é indexado pelo DISPENSER e não
    pela posição na rota, porque a rota é decidida aqui, em tempo de execução
    (serpentina), e muda quando um slot sai da OS: "vá ao ponto 3" seria outro
    dispenser no dia seguinte, e a mesa iria ao lugar errado sem erro nenhum.
    """
    return await _post(
        settings.CNC_ADAPTER_URL + "/comandos/mover",
        {
            "dispenser_alvo": disp_id,
            "os_id":          os_id,
            "receita":        receita,
            "ciclo_atual":    ciclo,
            "total_ciclos":   total,
        },
    )


async def cmd_homing(os_id: str) -> bool:
    """Manda a mesa voltar para HOME — sem coordenadas, como o `mover`.

    O HOME da máquina é o zero que o homing dela estabelece contra os fins de
    curso: um par de coordenadas vindo daqui seria um segundo HOME, e os dois
    concordariam só enquanto ninguém mexesse na mesa. `HOME` continua existindo
    neste módulo como origem do modelo lógico (a serpentina parte dele).
    """
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
            **_injecao_para("/comandos/capturar/dispenser", slot_id),
        },
        timeout=5.0,   # comando é rápido — o resultado chega via evento async
    )


async def cmd_visao_mesa(slot_id: int, os_id: str, quantidade: int,
                          pos_x: float, pos_y: float,
                          slots_cobertos: Optional[list] = None,
                          quantidade_slot: Optional[int] = None) -> bool:
    """Solicita captura da câmera da mesa para validar produtos dispensados.

    `quantidade` é o que a câmera deve ver de NOVO desde a última captura que
    ela registrou — o slot `slot_id` mais os que ficaram sem conferência antes
    dele. `slots_cobertos` e `quantidade_slot` vão de informação (a estação
    aceita campos extras e os ignora).
    """
    return await _post(
        settings.VISION_ADAPTER_URL + "/comandos/capturar/mesa",
        {
            "slot_id":           slot_id,
            "os_id":             os_id,
            "quantidade_esperada": quantidade,
            "posicao_x":         pos_x,
            "posicao_y":         pos_y,
            "slots_cobertos":    slots_cobertos if slots_cobertos is not None else [slot_id],
            "quantidade_slot":   quantidade if quantidade_slot is None else quantidade_slot,
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


# ── A caixa de coleta, conferida no HOME ──────────────────────────────────────
#
# CLAUDE.md, "A caixa é conferida duas vezes no HOME, e a mesa só vai ao centro
# com a visão ok". Vazia antes da OS, cheia depois — as duas com um `os_id`
# próprio, que a estação da mesa nunca viu. Ela conta por INCREMENTO dentro do
# `os_id`, e o acumulado de uma OS nova é 0: o "incremento" que volta é a
# contagem ABSOLUTA da caixa. É o que livra a conferência final de tudo o que
# as fotos por slot carregam (pendentes, déficit, dessincronia, estação
# reiniciada).

ALIAS_VAZIA = "VAZIA-"
ALIAS_FINAL = "FINAL-"
ETAPA_VAZIA = "caixa_vazia"
ETAPA_FINAL = "caixa_final"
_PREFIXO_DA_ETAPA = {ETAPA_VAZIA: ALIAS_VAZIA, ETAPA_FINAL: ALIAS_FINAL}
_ETAPA_DO_PREFIXO = {ALIAS_VAZIA: ETAPA_VAZIA, ALIAS_FINAL: ETAPA_FINAL}

# A estação e o vision-simulator só aceitam `slot_id` em 1..NUM_SLOTS. Com um
# `os_id` nunca visto, o par (alias, 1) é novo por construção, e o número não
# significa nada para a estação — quem diz o que a foto é são o prefixo e o
# campo `etapa`.
SLOT_FOTO_HOME = 1

# alias → (os_id real, etapa). Quem pede a foto registra; o handler do central
# traduz o evento de volta à OS real por aqui. Limitado: um alias por foto, e
# eles não servem para nada depois que a OS fecha.
_ALIASES_MAX = 200
_aliases_visao: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
# `-<n>` das repetições: até TRÊS dígitos, nunca mais. O `os_id` real termina em
# seis hexadecimais (os_templates.novo_os_id), que podem ser todos dígitos — um
# `-\d+$` guloso comeria o fim da OS real.
_SUFIXO_REPETICAO = re.compile(r"-\d{1,3}$")


def alias_foto_home(etapa: str, os_id: str, tentativa: int = 1) -> str:
    """`VAZIA-<os_id>` / `FINAL-<os_id>` na primeira foto; `-<n>` nas seguintes.

    Repetição é `os_id` NOVO porque a estação nunca refotografa um par
    (`os_id`, `slot_id`) que já recebeu: a segunda `VAZIA-<os_id>` voltaria
    "captura já em andamento" e evento nenhum.
    """
    base = f"{_PREFIXO_DA_ETAPA[etapa]}{os_id}"
    return base if tentativa <= 1 else f"{base}-{tentativa}"


def _registrar_alias(alias: str, os_id: str, etapa: str) -> None:
    _aliases_visao[alias] = (os_id, etapa)
    _aliases_visao.move_to_end(alias)
    while len(_aliases_visao) > _ALIASES_MAX:
        _aliases_visao.popitem(last=False)


def resolver_alias_visao(os_id: Optional[str]) -> tuple[Optional[str], str]:
    """(os_id real, etapa) de um `os_id` que veio num evento da câmera da mesa.

    `etapa` é `caixa_vazia`, `caixa_final` ou `slot`. O registro de quem pediu a
    foto manda; o texto é o fallback (central reiniciado entre a foto e o
    evento): tira o prefixo e um `-<n>` de até três dígitos. `os_id` sem prefixo
    — uma OS de verdade, ou o `MAPA-` da bancada — volta como veio.
    """
    if not os_id:
        return os_id, "slot"
    if os_id in _aliases_visao:
        return _aliases_visao[os_id]
    for prefixo, etapa in _ETAPA_DO_PREFIXO.items():
        if os_id.startswith(prefixo):
            real = os_id[len(prefixo):]
            return _SUFIXO_REPETICAO.sub("", real) or real, etapa
    return os_id, "slot"


# O que a faixa do operador diz em cada etapa da caixa (web do painel, display
# de 7", dashboard). Pura, e a mesma frase nas três telas — o motivo de
# `faixa_da_porta`.
def faixa_da_caixa(caixa: Optional[dict]) -> str:
    if not caixa:
        return ""
    etapa = caixa.get("etapa")
    if etapa == "aguardando_caixa_vazia":
        if caixa.get("problema") == "camera":
            return "CÂMERA DA MESA SEM RESPOSTA — CONFIRA A CAIXA DE COLETA"
        n = caixa.get("contagem") or 0
        return f"ESVAZIE A CAIXA DE COLETA ({n} caixinha{'s' if n != 1 else ''})"
    if etapa == "conferindo_caixa":
        return "CONFERINDO A CAIXA…"
    if etapa == "indo_ao_centro":
        if caixa.get("problema") == "centro":
            return "MESA NÃO CHEGOU AO CENTRO — REENVIE PELO CONSOLE"
        return "CAIXA CONFERIDA — LEVANDO AO CENTRO"
    return ""


# Linha do histórico de eventos do central (`/log/eventos`), injetada pelo
# main depois do `inicializar` (é o `main._log`). None nos testes e no ensaio:
# o histórico é tela, não decisão.
_historico_fn = None


def _historico(tipo: str, msg: str) -> None:
    if _historico_fn is not None:
        try:
            _historico_fn(tipo, msg)
        except Exception:  # noqa: BLE001 — histórico nunca derruba a OS
            logger.debug("[ORCH] histórico indisponível", exc_info=True)


def _publicar_caixa(os_id: Optional[str], etapa: Optional[str] = None,
                    **extra) -> None:
    """Estado da caixa para as telas (`/estado`, `/ws`, `GET /api/v1/caixa`)."""
    caixa = None if etapa is None else {"os_id": os_id, "etapa": etapa, **extra}
    if caixa is not None:
        caixa["faixa"] = faixa_da_caixa(caixa)
    if _lock:
        with _lock:
            _estado["caixa"] = caixa
    if _broadcast_fn:
        _broadcast_fn()


async def _status_da_os(os_id: str, status: str) -> None:
    """O status nos DOIS lados (memória e banco), como toda transição de OS."""
    if _lock:
        with _lock:
            ativa = _estado.get("os_ativa")
            if ativa and ativa.get("os_id") == os_id:
                ativa["status"] = status
    try:
        await asyncio.to_thread(atualizar_status_ordem, os_id, status)
    except Exception as e:
        logger.warning("[DB] atualizar_status_ordem %s: %s", status, e)
    if _broadcast_fn:
        _broadcast_fn()


async def _aguardar_sem_pausa(chave: str, timeout: float, geracao: int) -> Optional[dict]:
    """`aguardar_evento` que desiste NA HORA se a porta NR-12 pausar a célula.

    As esperas do HOME (homing, foto, centro) são handshake, e a pausa as torna
    sem sentido: a retomada faz homing e a etapa se refaz. Sem isto, a espera
    seguiria até o timeout inteiro — e, pior, o homing da retomada registra a
    MESMA chave `{os_id}:homing`, e o evento iria para ele, não para cá.

    O `finally` só desregistra a chave se ela ainda for a desta espera: quem a
    registrou de novo (a retomada) continua esperando.
    """
    evt = _pending_events.get(chave)
    if evt is None:
        evt = registrar_evento(chave)
    loop = asyncio.get_running_loop()
    fim = loop.time() + timeout
    try:
        while True:
            if _houve_pausa(geracao):
                return None
            resto = fim - loop.time()
            if resto <= 0:
                logger.warning("[ORCH] Timeout aguardando evento '%s' (%.0fs)", chave, timeout)
                return None
            try:
                await asyncio.wait_for(evt.wait(), min(0.25, resto))
            except asyncio.TimeoutError:
                continue
            return _pending_data.pop(chave, {})
    finally:
        if _pending_events.get(chave) is evt:
            _pending_events.pop(chave, None)
            _pending_data.pop(chave, None)


async def _portao_do_home(cancelar: asyncio.Event, geracao_anterior: Optional[int]) -> bool:
    """Espera a pausa da porta acabar e desarma o cancelamento que ela armou.

    `_aguardar_retomada_porta` só desarma quando ESPERA. Nas etapas do HOME a
    pausa costuma ser notada depois de acabar (a espera da foto confere a
    geração a cada fatia), e o cancelamento armado por ela ficaria de pé: o
    primeiro prazo do ciclo seguinte venceria na hora e a OS sairia de
    `_processar_os` sem status terminal. Aqui o critério é a GERAÇÃO — mudou
    desde a última olhada, houve pausa, e quem a armou já se foi.

    Seguro porque nada mais arma o cancelamento nestas etapas: a trava desarma
    o próprio em `_aguardar_liberacao`, e o reset recusa com OS em execução.
    Devolve True se houve pausa (a retomada fez homing).
    """
    esperou = await _aguardar_retomada_porta(cancelar)
    houve = esperou or (geracao_anterior is not None and _porta_geracao != geracao_anterior)
    if houve and _pausa_porta is None:
        cancelar.clear()
    return houve


async def _homing_confirmado(os_id: str, geracao: int) -> Optional[str]:
    """Homing que ESPERA o `concluido`. None = ok; senão, o motivo da falha."""
    chave = f"{os_id}:homing"
    registrar_evento(chave)
    if not await cmd_homing(os_id):
        _pending_events.pop(chave, None)
        return "comando de homing não aceito pelo cnc-adapter"
    resultado = await _aguardar_sem_pausa(chave, settings.TIMEOUT_POSICIONAMENTO, geracao)
    if resultado is None:
        return None if _houve_pausa(geracao) else "homing sem confirmação no prazo"
    if resultado.get("tipo") == "erro":
        return resultado.get("codigo_erro") or "erro no homing"
    return None


async def cmd_visao_mesa_home(alias: str, etapa: str, esperado: int) -> bool:
    """A foto da caixa inteira no HOME. NUNCA leva injeção (D5): o gatilho de
    demonstração não é consumido aqui, e fica armado para o slot dele."""
    return await _post(
        settings.VISION_ADAPTER_URL + "/comandos/capturar/mesa",
        {
            "slot_id":             SLOT_FOTO_HOME,
            "os_id":               alias,
            "quantidade_esperada": esperado,
            "posicao_x":           HOME[0],
            "posicao_y":           HOME[1],
            "slots_cobertos":      [],
            "quantidade_slot":     0,
            "etapa":               etapa,
        },
        timeout=5.0,
    )


async def _foto_home(os_id: str, etapa: str, tentativa: int, esperado: int,
                     geracao: int) -> tuple[Optional[int], str, Optional[dict]]:
    """Uma foto da caixa no HOME: (contagem absoluta | None, motivo, evento).

    Envio recusado decide NA HORA (a regra das etapas 3 e 3b): o evento não vai
    vir. Falha de leitura, timeout e pausa voltam com contagem None — a câmera
    não mediu, e quem decide o que isso significa é a etapa.
    """
    alias = alias_foto_home(etapa, os_id, tentativa)
    _registrar_alias(alias, os_id, etapa)
    chave = f"{alias}:visao_mesa:{SLOT_FOTO_HOME}"
    registrar_evento(chave)
    logger.info("[ORCH] Câmera mesa no HOME (%s): foto %s | esperado=%d", etapa, alias, esperado)
    if not await cmd_visao_mesa_home(alias, etapa, esperado):
        _pending_events.pop(chave, None)
        return None, "comando não aceito pelo vision-adapter", None
    resultado = await _aguardar_sem_pausa(chave, settings.TIMEOUT_VISAO_MESA, geracao)
    if resultado is None:
        return None, ("pausa da porta" if _houve_pausa(geracao) else "sem resposta"), None
    if resultado.get("tipo") in ("leitura_mesa_ok", "leitura_mesa_divergencia"):
        return _inteiro(resultado.get("quantidade_detectada")), "", resultado
    return None, resultado.get("motivo") or resultado.get("tipo") or "falha de leitura", resultado


async def _alarme_mesa(tipo: str, descricao: str, fonte: str = "camera_mesa") -> Optional[int]:
    logger.warning("[ORCH] %s", descricao)
    try:
        return await asyncio.to_thread(salvar_alarme, fonte, tipo, descricao)
    except Exception as e:
        logger.warning("[DB] salvar_alarme %s: %s", tipo, e)
        return None


async def _resolver_alarme(alarme_id: Optional[int]) -> None:
    if alarme_id is None:
        return
    try:
        await asyncio.to_thread(resolver_alarme, alarme_id)
    except Exception as e:
        logger.warning("[DB] resolver_alarme %s: %s", alarme_id, e)


def _mesa_nao_comparavel(resultado: Optional[dict], dessincronizada: bool) -> Optional[str]:
    """Por que a divergência desta foto não pode ser comparada — ou None.

    Dois motivos, e o primeiro vence porque é mais específico: o vision-adapter
    marcou `ressincronizar` (a estação perdeu o acumulado desta OS — reiniciou,
    ou a OS passou de `VALIDADE_OS_H` parada numa trava — e acabou de comparar
    a caixa INTEIRA com o esperado de um slot), ou o próprio central já não
    sabe se a estação registrou o total anterior.
    """
    if resultado and resultado.get("ressincronizar"):
        return "estação da mesa sem o acumulado desta OS (reiniciada ou expirada)"
    if dessincronizada:
        return "câmera ressincronizando (captura anterior sem desfecho conhecido)"
    return None


# ── Caixa VAZIA: a OS espera, não aborta ──────────────────────────────────────

# Pedido de encerrar a espera da caixa vazia, vindo do console. Um Event por OS
# (criado na espera) — os testes rodam cada corrotina num loop próprio.
_cancelar_espera: Optional[asyncio.Event] = None
_cancelar_espera_os: Optional[str] = None


def cancelar_espera_caixa() -> Optional[str]:
    """Console: encerra a OS que espera a caixa esvaziar. Devolve o `os_id`, ou
    None se não há OS nessa espera — e só nessa: depois dela há medicamento
    reservado, e a saída é outra (reset, trava)."""
    if _cancelar_espera is None or _cancelar_espera_os is None:
        return None
    if _loop is not None and not _loop.is_closed():
        _loop.call_soon_threadsafe(_cancelar_espera.set)
    else:
        _cancelar_espera.set()
    return _cancelar_espera_os


async def _conferir_caixa_vazia(os_id: str, cancelar: asyncio.Event) -> bool:
    """A foto VAZIA antes da OS. True = caixa vazia, a OS começa; False = a OS
    foi cancelada pelo console durante a espera.

    Caixa com caixinha ou câmera muda NÃO abortam: é a operação esperando
    alguém. UM alarme por espera (e por motivo), resolvido quando a caixa
    esvazia; re-foto a cada `VISAO_CAIXA_VAZIA_REPETIR_S` com alias novo.
    """
    global _cancelar_espera, _cancelar_espera_os
    if not settings.VISAO_CAIXA_VAZIA:
        logger.warning("[ORCH] OS %s: conferência da caixa no HOME desligada "
                       "(VISAO_CAIXA_VAZIA=0) — a OS começa sem a foto da caixa vazia.",
                       os_id)
        return True

    _cancelar_espera = asyncio.Event()
    _cancelar_espera_os = os_id
    alarme_id: Optional[int] = None
    alarme_tipo: Optional[str] = None
    em_espera = False
    tentativa = 0
    geracao: Optional[int] = _porta_geracao
    try:
        while True:
            if _cancelar_espera.is_set():
                return False
            # A retomada fez homing: a mesa está no HOME. A foto que estava no
            # fio na hora da pausa foi descartada.
            await _portao_do_home(cancelar, geracao)
            geracao = _porta_geracao
            tentativa += 1
            contagem, motivo, _res = await _foto_home(os_id, ETAPA_VAZIA, tentativa, 0, geracao)
            if _houve_pausa(geracao):
                continue
            if contagem == 0:
                await _resolver_alarme(alarme_id)
                _historico("caixa_vazia_ok", f"OS {os_id}: caixa de coleta vazia no HOME")
                if em_espera:
                    logger.info("[ORCH] OS %s: caixa de coleta esvaziada — a OS começa.", os_id)
                return True

            if not visao_conta():
                # Switch desligado: a foto foi tirada e fica registrada, mas não
                # segura a OS.
                await _resolver_alarme(alarme_id)
                await _registrar_visao_ignorada(
                    os_id, None,
                    "caixa de coleta no HOME antes da OS: "
                    + (f"câmera sem resposta ({motivo})" if contagem is None
                       else f"{contagem} caixinha(s) na caixa") + " — a OS começa")
                return True

            if contagem is None:
                tipo = "camera_mesa_indisponivel_inicio"
                descricao = (f"OS {os_id} não começa: a câmera da mesa não conferiu a "
                             f"caixa de coleta no HOME ({motivo}). Confira a caixa e a "
                             f"estação; a foto se repete a cada "
                             f"{settings.VISAO_CAIXA_VAZIA_REPETIR_S:.0f}s.")
                _publicar_caixa(os_id, "aguardando_caixa_vazia", problema="camera")
            else:
                tipo = "caixa_nao_vazia"
                descricao = (f"OS {os_id} não começa: a caixa de coleta tem {contagem} "
                             f"caixinha(s) no HOME. Esvazie a caixa; a foto se repete a "
                             f"cada {settings.VISAO_CAIXA_VAZIA_REPETIR_S:.0f}s.")
                _publicar_caixa(os_id, "aguardando_caixa_vazia", contagem=contagem)
            if not em_espera:
                em_espera = True
                await _status_da_os(os_id, "aguardando_caixa_vazia")
            _historico(tipo, descricao)
            if tipo != alarme_tipo:
                await _resolver_alarme(alarme_id)
                alarme_id = await _alarme_mesa(tipo, descricao)
                alarme_tipo = tipo
            await _dormir_ou_cancelar(settings.VISAO_CAIXA_VAZIA_REPETIR_S, _cancelar_espera)
    finally:
        _cancelar_espera = None
        _cancelar_espera_os = None


# ── Caixa CHEIA: a contagem absoluta contra o que o ciclo confirmou ───────────

class ResultadoFinal(NamedTuple):
    """Veredito da foto FINAL. `estado` ∈ ok | a_mais | a_menos | indisponivel."""
    estado: str
    travar: bool
    texto: str
    alarme: Optional[str] = None     # tipo do alarme quando NÃO trava


def avaliar_caixa_final(contada: Optional[int], esperada: int,
                        peso_ok: Optional[bool], visao_conta: bool = True) -> ResultadoFinal:
    """A conferência da caixa cheia. PURA — a tabela inteira cabe num teste.

    - igual → ok;
    - a mais → trava: caixinha a mais não se esconde, é unidade extra ou objeto
      estranho;
    - a menos → D3: se a balança fechou o total da OS (`peso_ok` True), alarme
      `conferencia_final_abaixo` e a OS segue; se ela também não fecha, ou não
      há leitura (None), trava;
    - sem leitura → D2: trava, e o supervisor confere a olho.

    `peso_ok` é a conjunção das pesagens por slot (ver `_processar_os`): não há
    leitura absoluta da balança no HOME.

    `visao_conta=False` (switch do console desligado): o veredito é o mesmo,
    mas nunca trava — o que não for "ok" sai com `alarme="visao_ignorada"` e a
    mesa vai ao centro. A balança já decidiu slot a slot no ciclo.
    """
    resultado = _avaliar_caixa_final(contada, esperada, peso_ok)
    if visao_conta or resultado.estado == "ok":
        return resultado
    return resultado._replace(travar=False, alarme="visao_ignorada")


def _avaliar_caixa_final(contada: Optional[int], esperada: int,
                         peso_ok: Optional[bool]) -> ResultadoFinal:
    if contada is None:
        return ResultadoFinal(
            "indisponivel", True,
            f"conferencia_final_sem_camera — a câmera da mesa não conferiu a caixa no "
            f"HOME (esperado {esperada}). Confira a caixa a olho e libere a trava.")
    if contada == esperada:
        return ResultadoFinal("ok", False,
                              f"caixa conferida no HOME: {contada} de {esperada}.")
    if contada > esperada:
        return ResultadoFinal(
            "a_mais", True,
            f"conferencia_final_a_mais — a câmera contou {contada} na caixa e a OS "
            f"pôs {esperada} ({contada - esperada} a mais). Confira a caixa e libere "
            f"a trava.")
    falta = esperada - contada
    if peso_ok is True:
        return ResultadoFinal(
            "a_menos", False,
            f"conferencia_final_abaixo — a câmera contou {contada} de {esperada} no HOME "
            f"({falta} a menos), mas a balança fechou o total da OS: possível oclusão.",
            alarme="conferencia_final_abaixo")
    balanca = ("a balança também divergiu" if peso_ok is False
               else "a balança não tem leitura de todos os slots")
    return ResultadoFinal(
        "a_menos", True,
        f"conferencia_final_a_menos — a câmera contou {contada} de {esperada} no HOME "
        f"({falta} a menos) e {balanca}. Confira a caixa e libere a trava.")


async def _travar_no_home(os_id: str, motivo: str, resumo: str,
                          cancelar: asyncio.Event) -> None:
    """Trava sem slot (é a caixa inteira) e espera a liberação."""
    logger.error("[ORCH] ⛔ %s", motivo)
    evento_lib = await _ativar_trava(os_id, None, motivo, resumo=resumo)
    await _aguardar_liberacao(evento_lib, cancelar)
    with _lock:
        _estado["trava"] = {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""}
    if _broadcast_fn:
        _broadcast_fn()


async def _conferir_caixa_final(os_id: str, esperado: int, peso_ok: Optional[bool],
                                cancelar: asyncio.Event,
                                tentativas: Optional[list] = None) -> None:
    """homing → FINAL → veredito. Volta quando a caixa está conferida (ou o
    supervisor liberou a trava, que conta como conferida: ele viu a caixa).

    Pausa da porta no meio refaz a etapa com alias novo. Homing que não
    confirma: a FINAL não é tirada (a posição não é confiável e a câmera pode
    nem ver a caixa) e a OS entra na trava com `homing_final_falhou`; liberada,
    a etapa se refaz desde o homing — a trava era sobre a POSIÇÃO.
    """
    await _status_da_os(os_id, "conferindo_caixa")
    _publicar_caixa(os_id, "conferindo_caixa", esperado=esperado)
    if not settings.VISAO_CAIXA_VAZIA:
        logger.warning("[ORCH] OS %s: conferência da caixa no HOME desligada "
                       "(VISAO_CAIXA_VAZIA=0) — %d unidade(s) seguem ao centro sem a "
                       "foto da caixa cheia.", os_id, esperado)
    # O contador de fotos FINAL vive fora desta chamada: a ida ao centro
    # interrompida refaz a etapa, e a estação nunca refotografa o par
    # (FINAL-<os_id>, 1) — o alias seguinte tem de ser NOVO.
    tentativas = tentativas if tentativas is not None else [0]
    geracao: Optional[int] = _porta_geracao
    while True:
        retomou = await _portao_do_home(cancelar, geracao)
        geracao = _porta_geracao
        if not retomou:
            # Depois de uma retomada a mesa JÁ fez homing — é a condição de a
            # pausa acabar. Um segundo seria segundos a mais à toa.
            motivo_homing = await _homing_confirmado(os_id, geracao)
            if _houve_pausa(geracao):
                continue
            if motivo_homing:
                await _alarme_mesa("homing_final_falhou",
                                   f"OS {os_id}: homing do fim da OS não confirmou "
                                   f"({motivo_homing}) — a caixa não foi conferida.",
                                   fonte="cnc")
                await _travar_no_home(
                    os_id,
                    f"homing_final_falhou — OS {os_id}: a mesa não confirmou o HOME "
                    f"({motivo_homing}); a caixa de coleta não pôde ser fotografada. "
                    f"Confira a mesa e libere a trava para tentar de novo.",
                    "homing do fim", cancelar)
                continue
        if not settings.VISAO_CAIXA_VAZIA:
            return
        tentativas[0] += 1
        contada, motivo, _res = await _foto_home(os_id, ETAPA_FINAL, tentativas[0],
                                                 esperado, geracao)
        if _houve_pausa(geracao):
            continue
        veredito = avaliar_caixa_final(contada, esperado, peso_ok, visao_conta())
        _historico(f"conferencia_final_{veredito.estado}",
                   f"OS {os_id}: caixa no HOME contada="
                   f"{'—' if contada is None else contada} esperada={esperado} → "
                   f"{veredito.estado}{' (trava)' if veredito.travar else ''}")
        if contada is None:
            logger.warning("[ORCH] OS %s: FINAL sem leitura (%s).", os_id, motivo)
        if veredito.estado == "ok":
            logger.info("[ORCH] OS %s: %s", os_id, veredito.texto)
            return
        if veredito.alarme == "visao_ignorada":
            await _registrar_visao_ignorada(os_id, None,
                                            f"conferência final no HOME: {veredito.texto}")
            return
        if not veredito.travar:
            await _alarme_mesa(veredito.alarme or "conferencia_final_abaixo",
                               f"OS {os_id}: {veredito.texto}")
            return
        await _travar_no_home(os_id, f"OS {os_id}: {veredito.texto}",
                              "conferência final", cancelar)
        return


# ── O centro: a OS só fecha com a mesa lá ─────────────────────────────────────

_reenviar_centro: Optional[asyncio.Event] = None
_reenviar_centro_os: Optional[str] = None


def reenviar_centro() -> Optional[str]:
    """Console: tenta de novo a ida ao centro que não chegou. `os_id` ou None."""
    if _reenviar_centro is None or _reenviar_centro_os is None:
        return None
    if _loop is not None and not _loop.is_closed():
        _loop.call_soon_threadsafe(_reenviar_centro.set)
    else:
        _reenviar_centro.set()
    return _reenviar_centro_os


async def cmd_centro(os_id: str) -> bool:
    """A caixa conferida vai ao centro operacional gravado NA MESA."""
    return await _post(settings.CNC_ADAPTER_URL + "/comandos/centro", {"os_id": os_id})


async def _ir_ao_centro(os_id: str, cancelar: asyncio.Event) -> bool:
    """`centro` até a mesa chegar. True = chegou; False = uma pausa da porta
    interrompeu a ida, e a etapa inteira (homing, FINAL, centro) se refaz.

    Não chegou, `erro` ou `sem_homing`: alarme `cnc_nao_chegou_ao_centro`, SEM
    trava — a contagem já foi validada. A OS espera o "reenviar centro" do
    console, e a fila espera junto: a mesa precisa estar fora do caminho.
    """
    global _reenviar_centro, _reenviar_centro_os
    await _status_da_os(os_id, "indo_ao_centro")
    _publicar_caixa(os_id, "indo_ao_centro")
    geracao: Optional[int] = _porta_geracao
    while True:
        if await _portao_do_home(cancelar, geracao):
            return False
        geracao = _porta_geracao
        chave = f"{os_id}:centro"
        registrar_evento(chave)
        _historico("centro_pedido", f"OS {os_id}: mesa enviada ao centro")
        if await cmd_centro(os_id):
            resultado = await _aguardar_sem_pausa(chave, settings.TIMEOUT_CNC_CENTRO, geracao)
        else:
            _pending_events.pop(chave, None)
            resultado = {"tipo": "erro", "codigo_erro": "comando_nao_aceito"}
        if _houve_pausa(geracao):
            return False
        if resultado is not None and resultado.get("tipo") == "posicionado":
            logger.info("[ORCH] OS %s: mesa no centro.", os_id)
            _historico("centro_chegou", f"OS {os_id}: mesa chegou ao centro")
            return True
        motivo = ("sem confirmação no prazo" if resultado is None
                  else resultado.get("codigo_erro") or "erro")
        _historico("centro_falhou", f"OS {os_id}: mesa não chegou ao centro ({motivo})")
        await _alarme_mesa("cnc_nao_chegou_ao_centro",
                           f"OS {os_id}: a mesa não chegou ao centro ({motivo}). A "
                           f"contagem já foi conferida — use \"reenviar centro\" no "
                           f"console.", fonte="cnc")
        _publicar_caixa(os_id, "indo_ao_centro", problema="centro", motivo=motivo)
        _reenviar_centro = asyncio.Event()
        _reenviar_centro_os = os_id
        try:
            await _reenviar_centro.wait()
        finally:
            _reenviar_centro = None
            _reenviar_centro_os = None
        logger.warning("[ORCH] OS %s: reenviando a mesa ao centro (console).", os_id)
        _publicar_caixa(os_id, "indo_ao_centro")


async def _fechar_no_home(os_id: str, esperado: int, peso_ok: Optional[bool],
                          cancelar: asyncio.Event) -> None:
    """homing → FINAL → centro, refeito inteiro se uma pausa interromper a ida."""
    tentativas = [0]
    while True:
        await _conferir_caixa_final(os_id, esperado, peso_ok, cancelar, tentativas)
        if await _ir_ao_centro(os_id, cancelar):
            return
        logger.warning("[ORCH] OS %s: ida ao centro interrompida pela porta — "
                       "refazendo homing, conferência da caixa e centro.", os_id)


# ── Portas NR-12: a porta pede, o central para e autoriza ─────────────────────
#
# Um estado da célula SEPARADO da trava do Triple Check. As duas podem coexistir,
# e retomar exige as duas resolvidas. Ver CLAUDE.md, "Portas NR-12".
#
#   _pausa_porta = None | {motivo, desde, portas, aguardando_rearme, os_id, ...}
#   motivo ∈ pedido_acesso | porta_aberta_sem_autorizacao | link_perdido
#
# Entrar em pausa é SÍNCRONO e acontece na hora em que o evento chega: o estado
# arma, o cronograma da OS é interrompido (`cancelar_cronograma`), e só depois
# saem os comandos de parada — a mesma ordem de `_ativar_trava`, pelo mesmo
# motivo: cada `await` antes de armar é uma janela para mais um `dispensar`.
#
# O ciclo da OS distingue a pausa do reset pela GERAÇÃO: toda entrada em pausa
# incrementa `_porta_geracao`, e um prazo que acordou cedo com a geração mudada
# foi uma pausa (o slot se refaz depois da retomada), não um reset (a OS para).

MOTIVOS_PAUSA = ("pedido_acesso", "porta_aberta_sem_autorizacao", "link_perdido")
# Quem pesa mais quando dois chegam juntos: o link perdido é o pior caso (não se
# sabe nada), a abertura sem autorização vem depois, o pedido é o mais brando.
_GRAVIDADE_MOTIVO = {"pedido_acesso": 0, "porta_aberta_sem_autorizacao": 1,
                     "link_perdido": 2}
# Os portas da célula e a fonte do alarme de cada uma.
PORTAS = ("teto", "frente")
# Tentativas de tag negada que viram alarme: 3 em 60 s.
TAG_NEGADA_LIMITE = 3
TAG_NEGADA_JANELA_S = 60.0
# A OS da retomada quando nenhuma está em andamento: o homing precisa de um
# `os_id`, e a chave de espera também.
OS_RETOMADA = "PORTA-RETOMADA"

_pausa_porta: Optional[dict] = None
_porta_geracao: int = 0
# O que se sabe da porta, a partir dos eventos dela. `link_ok` None = ainda não
# se ouviu nada desde o boot do central.
_porta: dict = {
    "link_ok": None, "teto": None, "frente": None, "estado": None,
    "aguardando_rearme": False,
    "visto_em": None, "ultimo_evento": None, "ultimo_evento_ts": None,
}
# Quem espera a pausa acabar. Um Event por espera, e não um Event global: os
# testes rodam cada corrotina num `asyncio.run` próprio, e um Event que
# atravessasse loops diferentes levantaria "bound to a different event loop".
_porta_esperas: set = set()
_retomada_em_curso: bool = False
# Paradas (`_parar_maquina`) com comando no fio. A retomada espera que cheguem a
# zero antes do homing: um `parar` que alcançasse a mesa DURANTE o homing a
# pararia sem origem, e o `mover` seguinte seria `sem_homing`.
_paradas_em_curso: int = 0
# A dispensa que está saindo AGORA — de antes do `dispensar` até a colheita. É
# o que a parada da porta confere para saber se precisa esperar o
# `dispensado_parcial` antes de autorizar a porta.
_dispensa_em_voo: Optional[dict] = None
_tags_negadas: list = []
_tarefas_porta: set = set()
_trava_resumo: str = ""
_faixa_publicada: str = ""


def _porta_monotonic() -> float:
    import time as _time
    return _time.monotonic()


def get_pausa_porta() -> Optional[dict]:
    """A pausa em vigor, ou None. Cópia: quem lê não escreve."""
    return dict(_pausa_porta) if _pausa_porta is not None else None


def idade_porta_s() -> Optional[float]:
    """Segundos desde a última mensagem da porta (None = nunca falou)."""
    visto = _porta.get("visto_em")
    return None if visto is None else _porta_monotonic() - visto


def get_estado_porta() -> dict:
    return {k: v for k, v in _porta.items() if k != "visto_em"}


def porta_suspende_conclusao() -> bool:
    """O `concluido` da mesa agora é o homing da PAUSA, não o fim de uma OS.

    O homing da retomada é endereçado à OS em andamento — é ela que retoma — e
    o handler da CNC no central publicaria a OS como concluída no meio dela. É
    a mesma guarda que a trava do Triple Check já tem.
    """
    return _pausa_porta is not None or _retomada_em_curso


def _houve_pausa(geracao: int) -> bool:
    return _pausa_porta is not None or _porta_geracao != geracao


def _os_em_andamento() -> Optional[str]:
    ativa = _estado.get("os_ativa") if _estado else None
    return ativa.get("os_id") if ativa else None


def _agendar_porta(coro) -> None:
    """Background no loop, com referência forte (ver `_agendar` dos adapters)."""
    try:
        tarefa = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        if _loop is not None and not _loop.is_closed():
            asyncio.run_coroutine_threadsafe(coro, _loop)
            return
        coro.close()
        return
    _tarefas_porta.add(tarefa)
    tarefa.add_done_callback(_tarefas_porta.discard)


async def aguardar_tarefas_porta() -> None:
    """Para a suíte: espera o que a porta agendou (parada, liberação, retomada)."""
    while _tarefas_porta:
        await asyncio.gather(*list(_tarefas_porta), return_exceptions=True)


def _acordar_esperas_porta() -> None:
    for evt in list(_porta_esperas):
        evt.set()


async def _aguardar_porta_livre() -> bool:
    """Bloqueia enquanto houver pausa de porta. True se esperou."""
    esperou = False
    while _pausa_porta is not None:
        esperou = True
        evt = asyncio.Event()
        _porta_esperas.add(evt)
        try:
            if _pausa_porta is not None:
                await evt.wait()
        finally:
            _porta_esperas.discard(evt)
    return esperou


async def _aguardar_retomada_porta(cancelar: asyncio.Event) -> bool:
    """O portão do ciclo: espera a retomada e desarma o cancelamento que a
    pausa armou. True se houve pausa — a mesa fez homing, a posição se perdeu."""
    esperou = await _aguardar_porta_livre()
    if esperou:
        cancelar.clear()
    return esperou


def faixa_da_porta(pausa: Optional[dict] = None, porta: Optional[dict] = None) -> str:
    """O texto da faixa que o operador vê (display de 7", web, telas TFT).

    Pura e de módulo, para que as três telas digam a MESMA frase — e para que o
    teste a confira sem montar nada.
    """
    if pausa is None and porta is None:
        pausa, porta = _pausa_porta, _porta
    porta = porta or {}
    if not pausa:
        return ""
    if porta.get("link_ok") is False or pausa.get("motivo") == "link_perdido" and \
            porta.get("link_ok") is not True:
        return "LINK DA PORTA PERDIDO"
    abertas = [p for p in PORTAS if porta.get(p) == "aberta"]
    if abertas:
        if len(abertas) == 2:
            return "MÁQUINA PARADA — PORTAS TETO E FRENTE ABERTAS"
        return f"MÁQUINA PARADA — PORTA {abertas[0].upper()} ABERTA"
    if pausa.get("aguardando_rearme"):
        return "PORTAS FECHADAS — PASSE A TAG PARA RETOMAR"
    if porta.get("estado") == "LIBERADA":
        return "MÁQUINA PARADA — PORTA LIBERADA, PODE ABRIR"
    return "MÁQUINA PARADA — PEDIDO DE ACESSO"


def resumo_da_pausa_para_telas(texto: str) -> str:
    """A faixa em ASCII, no teto das telas TFT (a fonte delas não tem acento)."""
    import unicodedata
    limpo = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return limpo.replace("  ", " ")[:TRAVA_RESUMO_MAX]


def _publicar_porta() -> None:
    """Estado real primeiro, tela depois — a ordem de `_ativar_trava`."""
    faixa = faixa_da_porta()
    pausa = get_pausa_porta()
    if _lock:
        with _lock:
            _estado["porta"] = {**get_estado_porta(),
                                "habilitada": settings.PORTA_NR12_HABILITADA,
                                "faixa": faixa}
            _estado["pausa_porta"] = pausa
    global _faixa_publicada
    anterior, _faixa_publicada = _faixa_publicada, faixa
    if faixa != anterior:
        _agendar_aviso_telas(_trava_ativa, _trava_slot_id, _trava_os_id,
                             _trava_resumo, so_telas=True)
    if _broadcast_fn:
        _broadcast_fn()


def _entrar_em_pausa(motivo: str, aguardando_rearme: bool = False) -> bool:
    """Arma a pausa (ou a agrava). Devolve True se ela NASCEU agora."""
    global _pausa_porta, _porta_geracao
    assert motivo in MOTIVOS_PAUSA, motivo
    if _pausa_porta is None:
        _porta_geracao += 1
        _pausa_porta = {
            "motivo": motivo,
            "desde": _ts(),
            "portas": {p: _porta.get(p) for p in PORTAS},
            "aguardando_rearme": bool(aguardando_rearme),
            "os_id": _os_em_andamento(),
            "rearmada_com_trava": False,
        }
        # O cronograma morre AQUI, antes de qualquer comando sair — ver
        # `_ativar_trava` sobre por que a ordem é a feature.
        cancelar_cronograma()
        logger.error("[PORTA] ⛔ MÁQUINA PARADA — %s (OS %s)", motivo,
                     _pausa_porta["os_id"] or "nenhuma")
        _publicar_porta()
        return True
    if _GRAVIDADE_MOTIVO[motivo] > _GRAVIDADE_MOTIVO[_pausa_porta["motivo"]]:
        _pausa_porta["motivo"] = motivo
    if aguardando_rearme:
        _pausa_porta["aguardando_rearme"] = True
    _pausa_porta["portas"] = {p: _porta.get(p) for p in PORTAS}
    _publicar_porta()
    return False


def _maquina_ativa() -> bool:
    return _os_em_andamento() is not None or _retomada_em_curso


async def _parar_maquina() -> bool:
    """CNC e dispensers, em `gather`. True só com a parada CONFIRMADA no prazo.

    Confirmada = os dois `parar` aceitos (ACK) e, se havia dispensa saindo, o
    `dispensado`/`dispensado_parcial` dela colhido pelo ciclo. Tudo dentro de
    `PORTA_PARADA_MAX_S`: é o que a porta espera do outro lado (ESPERA_CENTRAL_MS
    gravado na placa), e `_post` sozinho retentaria por até ~32 s.
    """
    global _paradas_em_curso
    prazo = settings.PORTA_PARADA_MAX_S
    inicio = _porta_monotonic()

    async def _um(url: str) -> bool:
        # Conferido de novo NA HORA de sair: parada agendada que encontra a
        # retomada em curso (ou a pausa já encerrada) não vai mais ao fio.
        if _retomada_em_curso or _pausa_porta is None:
            return True
        try:
            return await asyncio.wait_for(_post(url, {"motivo": "porta_nr12"}), prazo)
        except asyncio.TimeoutError:
            return False

    _paradas_em_curso += 1
    try:
        respostas = await asyncio.gather(
            _um(settings.CNC_ADAPTER_URL + "/comandos/parar"),
            _um(settings.DISPENSER_ADAPTER_URL + "/comandos/parar"),
            return_exceptions=True,
        )
    finally:
        _paradas_em_curso -= 1
    confirmada = all(r is True for r in respostas)
    voo = _dispensa_em_voo
    if voo is not None and not voo["parou"].is_set():
        restante = max(0.0, prazo - (_porta_monotonic() - inicio))
        try:
            await asyncio.wait_for(voo["parou"].wait(), restante)
        except asyncio.TimeoutError:
            confirmada = False
    if not confirmada:
        logger.error("[PORTA] parada NÃO confirmada em %.1fs (cnc, dispensers) = %s",
                     prazo, respostas)
    return confirmada


async def _alarme_porta(fonte: str, tipo: str, descricao: str) -> None:
    logger.warning("[PORTA] ALARME %s/%s: %s", fonte, tipo, descricao)
    try:
        await asyncio.to_thread(salvar_alarme, fonte, tipo, descricao)
    except Exception as e:
        logger.warning("[DB] salvar_alarme %s: %s", tipo, e)


async def _atender_pedido_acesso(uid: str, geracao: int) -> None:
    """Para tudo, e SÓ com a parada confirmada autoriza a abertura.

    A porta não tem fechadura (é presa por ímã): quem puxa, abre. O que o
    `porta_liberar` faz é acender o "pode abrir" — abertura antes dele é
    abertura SEM autorização, e para a célula do mesmo jeito.

    A pausa já nasceu em `evento_porta`, na hora em que o pedido chegou; aqui
    fica só o que demora. `geracao` é a da pausa que este pedido abriu: se ela
    já acabou quando a tarefa roda, não há mais o que parar nem o que liberar.
    """
    if _porta_geracao != geracao or _pausa_porta is None:
        return
    confirmada = True
    if _maquina_ativa():
        confirmada = await _parar_maquina()
    if _porta_geracao != geracao or _pausa_porta is None:
        return
    if confirmada:
        ok = await _post(settings.PORTA_ADAPTER_URL + "/comandos/porta/liberar",
                         {"janela_ms": settings.PORTA_JANELA_LIBERACAO_MS})
        if not ok:
            # A porta desiste sozinha em ESPERA_CENTRAL_MS e volta a FECHADA
            # (`liberacao_expirada` central_nao_respondeu) — é por esse evento
            # que a máquina retoma.
            logger.error("[PORTA] porta_liberar não foi aceito.")
        return
    await _alarme_porta(
        "porta", "maquina_nao_confirmou_parada",
        f"Pedido de acesso (tag {uid}) NEGADO: a CNC ou os dispensers não "
        f"confirmaram a parada em {settings.PORTA_PARADA_MAX_S:.1f}s. A porta "
        f"continua travada.")
    await _post(settings.PORTA_ADAPTER_URL + "/comandos/porta/negar",
                {"motivo": "maquina_nao_confirmou_parada"})


async def _parar_se_ativa(motivo: str, geracao: Optional[int] = None) -> None:
    """A parada de uma pausa que JÁ está armada — e só dela.

    Agendada, ela pode rodar depois de a máquina já ter retomado; um `parar`
    que chegasse então à mesa a pararia em movimento e tiraria a origem dela,
    e o `mover` seguinte seria `sem_homing`. Por isso a geração: parada de uma
    pausa que acabou não sai.
    """
    geracao = _porta_geracao if geracao is None else geracao
    if _pausa_porta is None or _porta_geracao != geracao:
        return
    if _maquina_ativa() and not await _parar_maquina():
        logger.error("[PORTA] pausa por %s: a máquina não confirmou a parada.", motivo)


def _bloqueio_da_retomada(exigir_rearme: bool = True) -> Optional[str]:
    """Por que a máquina NÃO pode retomar agora — ou None."""
    if _porta.get("link_ok") is not True:
        return "link da porta não confirmado"
    abertas = [p for p in PORTAS if _porta.get(p) != "fechada"]
    if abertas:
        return "porta " + " e ".join(abertas) + " não está fechada"
    if _porta.get("estado") not in (None, "FECHADA"):
        return f"porta em {_porta.get('estado')}, não travada"
    if exigir_rearme and _pausa_porta and _pausa_porta.get("aguardando_rearme"):
        return "aguardando rearme por tag"
    if _trava_ativa:
        return "trava do Triple Check pendente"
    return None


def _agendar_retomada(origem: str) -> None:
    _agendar_porta(_tentar_retomar(origem))


async def _tentar_retomar(origem: str) -> bool:
    """Homing e só então a pausa acaba. Homing que falha: a OS segue pausada."""
    global _pausa_porta, _retomada_em_curso
    if _pausa_porta is None or _retomada_em_curso:
        return False
    bloqueio = _bloqueio_da_retomada()
    if bloqueio:
        logger.warning("[PORTA] retomada (%s) recusada: %s.", origem, bloqueio)
        return False
    _retomada_em_curso = True
    try:
        limite = _porta_monotonic() + settings.PORTA_PARADA_MAX_S + 1.0
        while _paradas_em_curso > 0 and _porta_monotonic() < limite:
            await asyncio.sleep(0.01)
        os_id = _os_em_andamento() or OS_RETOMADA
        chave = f"{os_id}:homing"
        registrar_evento(chave)
        logger.info("[PORTA] retomando (%s): homing da mesa antes de continuar.", origem)
        aceito = await cmd_homing(os_id)
        resultado = await aguardar_evento(chave, settings.PORTA_TIMEOUT_HOMING_S) \
            if aceito else None
        if not aceito:
            _pending_events.pop(chave, None)
        if resultado is None or resultado.get("tipo") == "erro":
            motivo = ("comando de homing não aceito" if not aceito else
                      "homing sem confirmação" if resultado is None else
                      resultado.get("codigo_erro") or "erro no homing")
            # A máquina continua PARADA: a posição não é confiável, e a OS não
            # anda. Rearme pendente de novo, para a próxima tag ser deliberada.
            if _pausa_porta is not None:
                _pausa_porta["aguardando_rearme"] = True
            await _alarme_porta("cnc", "homing_retomada_falhou",
                                f"Retomada da porta NR-12 ({origem}) sem homing — "
                                f"{motivo}. A OS {os_id} segue pausada.")
            _publicar_porta()
            return False
        if _bloqueio_da_retomada(exigir_rearme=False):
            logger.warning("[PORTA] a porta mudou durante o homing — continua pausada.")
            return False
        _pausa_porta = None
        logger.warning("[PORTA] ✓ MÁQUINA RETOMADA (%s).", origem)
        _acordar_esperas_porta()
        _publicar_porta()
        return True
    finally:
        _retomada_em_curso = False


def _registrar_tag_negada(agora: float) -> bool:
    """True quando esta negada fecha o ciclo de 3 em 60 s (e zera a contagem)."""
    _tags_negadas[:] = [t for t in _tags_negadas if agora - t <= TAG_NEGADA_JANELA_S]
    _tags_negadas.append(agora)
    if len(_tags_negadas) >= TAG_NEGADA_LIMITE:
        _tags_negadas.clear()
        return True
    return False


async def evento_porta(payload: dict) -> dict:
    """Um evento da porta NR-12 (via porta-adapter). Decide e devolve o fato.

    Rápido: o que demora (parar, liberar, homing) vai para background — o
    handler HTTP é o único caminho de volta do adapter, e segurá-lo faria o
    adapter retentar um evento já processado.

    Devolve {"linha": bool, "alarmes": [...]} para o histórico do main, e grava
    os alarmes que dependem do estado DAQUI (transição de link, tag negada
    repetida, abertura sem autorização).
    """
    tipo = payload.get("tipo")
    _porta["visto_em"] = _porta_monotonic()
    _porta["ultimo_evento"] = tipo
    _porta["ultimo_evento_ts"] = payload.get("ts")
    alarmes: list[tuple[str, str, str]] = []
    linha = True     # toda mensagem que muda estado vira linha; o heartbeat não

    if tipo in ("estado", "boot"):
        for p in PORTAS:
            if payload.get(p) in ("aberta", "fechada"):
                _porta[p] = payload[p]
    if tipo == "estado":
        linha = False
        mudou_link = _porta["link_ok"] is not True
        _porta["link_ok"] = True
        _porta["estado"] = payload.get("estado")
        _porta["aguardando_rearme"] = bool(payload.get("aguardando_rearme"))
        if mudou_link:
            logger.info("[PORTA] link da porta confirmado pelo `estado`.")
        abertas = [p for p in PORTAS if _porta.get(p) == "aberta"]
        if abertas and _pausa_porta is None:
            # Porta aberta que chegou só pelo heartbeat (o `porta_aberta` se
            # perdeu): para do mesmo jeito.
            autorizada = _porta["estado"] == "ABERTA_AUTORIZADA"
            _entrar_em_pausa("pedido_acesso" if autorizada
                             else "porta_aberta_sem_autorizacao", aguardando_rearme=True)
            _agendar_porta(_parar_se_ativa("estado_com_porta_aberta", _porta_geracao))
        elif _pausa_porta is not None:
            if _porta["aguardando_rearme"]:
                _pausa_porta["aguardando_rearme"] = True
            _pausa_porta["portas"] = {p: _porta.get(p) for p in PORTAS}
            # Link perdido SEM porta aberta no meio: o `estado` fechado, com a
            # porta travada e sem rearme pendente, basta para retomar.
            if (_pausa_porta["motivo"] == "link_perdido"
                    and not _pausa_porta["aguardando_rearme"]
                    and _bloqueio_da_retomada() is None):
                _agendar_retomada("link_ok")
        _publicar_porta()

    elif tipo == "porta_link_perdido":
        transicao = _porta["link_ok"] is not False
        _porta["link_ok"] = False
        _entrar_em_pausa("link_perdido")
        _agendar_porta(_parar_se_ativa("link_perdido", _porta_geracao))
        if transicao:
            alarmes.append(("porta", "porta_link_perdido",
                            "Link com a porta NR-12 perdido — tratado como porta "
                            "aberta: a célula parou."))
        else:
            linha = False

    elif tipo == "porta_link_ok":
        # Sozinho não retoma: quem prova o link é o `estado` que vem depois.
        _publicar_porta()

    elif tipo == "pedido_acesso":
        # A pausa nasce AQUI, e não na tarefa: o ciclo que está mandando um
        # `dispensar` neste instante precisa vê-la quando o comando voltar.
        _entrar_em_pausa("pedido_acesso")
        _agendar_porta(_atender_pedido_acesso(payload.get("uid") or "?", _porta_geracao))

    elif tipo == "liberada":
        _porta["estado"] = "LIBERADA"
        _publicar_porta()

    elif tipo == "porta_aberta":
        porta = payload.get("porta")
        if porta in PORTAS:
            _porta[porta] = "aberta"
        autorizada = bool(payload.get("autorizada"))
        _entrar_em_pausa("pedido_acesso" if autorizada else "porta_aberta_sem_autorizacao",
                         aguardando_rearme=True)
        _agendar_porta(_parar_se_ativa("porta_aberta", _porta_geracao))
        if not autorizada:
            alarmes.append((f"porta_{porta}", "porta_aberta_sem_autorizacao",
                            f"Porta {porta} aberta SEM autorização (OS "
                            f"{_os_em_andamento() or 'nenhuma'}) — a célula parou."))

    elif tipo == "porta_fechada":
        for p in PORTAS:
            _porta[p] = "fechada"
        if _pausa_porta is not None:
            _pausa_porta["aguardando_rearme"] = True
        _publicar_porta()

    elif tipo == "rearme":
        if _pausa_porta is None:
            logger.info("[PORTA] rearme sem pausa em vigor — nada a retomar.")
        else:
            bloqueio = _bloqueio_da_retomada(exigir_rearme=False)
            if bloqueio == "trava do Triple Check pendente":
                # Rearme registrado; a máquina retoma quando a trava for
                # liberada — as duas resolvidas, em qualquer ordem.
                _pausa_porta["aguardando_rearme"] = False
                _pausa_porta["rearmada_com_trava"] = True
                logger.warning("[PORTA] rearme registrado; a OS retoma quando a trava "
                               "do Triple Check for liberada.")
                _publicar_porta()
            elif bloqueio:
                logger.warning("[PORTA] rearme recusado: %s. Máquina segue parada.",
                               bloqueio)
                _agendar_porta(_post(settings.PORTA_ADAPTER_URL
                                     + "/comandos/porta/estado", {}))
            else:
                _pausa_porta["aguardando_rearme"] = False
                _agendar_retomada("rearme")

    elif tipo == "liberacao_expirada":
        motivo = payload.get("motivo") or ""
        if _pausa_porta is not None:
            if motivo == "janela_expirou":
                # Liberada e ninguém abriu: a máquina já parou, só retoma com tag.
                _pausa_porta["aguardando_rearme"] = True
                _publicar_porta()
            else:
                # Negado, ou a porta desistiu de esperar: o acesso NÃO aconteceu,
                # a porta está travada, e não há o que rearmar — a célula volta
                # ao que fazia, pelo homing como toda retomada.
                _pausa_porta["aguardando_rearme"] = False
                _agendar_retomada(f"acesso_nao_aconteceu:{motivo}")

    elif tipo == "boot":
        # A porta reiniciou: com OS em andamento, é link perdido + o estado
        # atual — não se sabe o que aconteceu enquanto ela estava fora.
        if _os_em_andamento() is not None or _pausa_porta is not None:
            _entrar_em_pausa("link_perdido")
            _agendar_porta(_parar_se_ativa("boot_da_porta", _porta_geracao))
        _publicar_porta()

    elif tipo == "tag_negada":
        if _registrar_tag_negada(_porta_monotonic()):
            alarmes.append(("porta", "tentativa_acesso_negada",
                            f"{TAG_NEGADA_LIMITE} tags negadas em "
                            f"{TAG_NEGADA_JANELA_S:.0f}s na porta NR-12 (última: "
                            f"{payload.get('uid') or '?'})."))

    for fonte, tipo_alarme, descricao in alarmes:
        await _alarme_porta(fonte, tipo_alarme, descricao)
    return {"linha": linha, "alarmes": [a[1] for a in alarmes],
            "os_id": _os_em_andamento(),
            "slot_id": (_dispensa_em_voo or {}).get("slot") or _slot_em_curso}


# O slot do ciclo em andamento, para o histórico da porta dizer o que ela
# interrompeu. Escrito pelo passo 4, lido aqui.
_slot_em_curso: Optional[int] = None


def porta_silenciosa(agora: float, visto_em: Optional[float], habilitada: bool,
                     link_ok: Optional[bool], silencio_max_s: float,
                     desde: float) -> bool:
    """A vigia de cima: nada da porta há tempo demais = link perdido. Pura.

    O adapter reenvia o `estado` a cada 5 s mesmo sem mudança; um silêncio de
    `PORTA_SILENCIO_MAX_S` é, portanto, o ADAPTER morto — que, sem esta vigia,
    deixaria a célula rodando com a porta sem ninguém vigiando.
    """
    if link_ok is False:
        return False                      # já está perdido: nada a fazer
    if visto_em is None:
        return habilitada and agora - desde > silencio_max_s
    return agora - visto_em > silencio_max_s


async def vigiar_porta() -> None:
    """Laço da `lifespan`: o silêncio da porta também para a célula."""
    desde = _porta_monotonic()
    while True:
        await asyncio.sleep(1.0)
        try:
            if porta_silenciosa(_porta_monotonic(), _porta["visto_em"],
                                settings.PORTA_NR12_HABILITADA, _porta["link_ok"],
                                settings.PORTA_SILENCIO_MAX_S, desde):
                logger.error("[PORTA] nenhuma mensagem da porta há mais de %.0fs.",
                             settings.PORTA_SILENCIO_MAX_S)
                await evento_porta({"origem": "porta", "tipo": "porta_link_perdido",
                                    "ts": _ts(), "detalhe": "silencio"})
                try:
                    await asyncio.to_thread(salvar_porta_evento, "porta_link_perdido",
                                            None, None, None, "silencio no central",
                                            _os_em_andamento(), None)
                except Exception as e:
                    logger.warning("[DB] salvar_porta_evento: %s", e)
        except Exception:
            logger.exception("[PORTA] falha na vigia do central")


# ── Processamento de uma OS ────────────────────────────────────────────────────

async def _processar_os(os_payload: dict):
    global _cancelar_cronograma, _slot_em_curso, _dispensa_em_voo
    os_id        = os_payload["os_id"]
    medicamentos = os_payload.get("medicamentos", [])
    descricao    = os_payload.get("descricao", "")

    logger.info("\n%s\n[ORCH] OS INICIADA: %s | %d medicamentos\n%s",
                "=" * 60, os_id, len(medicamentos), "=" * 60)

    # Um Event por OS, e novo a cada OS: reaproveitar o da anterior traria junto
    # um cancelamento já armado, e a OS nova morreria no primeiro prazo sem que
    # nada no log explicasse.
    cancelar = asyncio.Event()
    _cancelar_cronograma = cancelar
    _zerar_visao_ignoradas()

    # ── Estado: fora da fila, conferindo a caixa vazia ───────────────────────
    # A OS atravessa aguardando_caixa_vazia → em_andamento → conferindo_caixa →
    # indo_ao_centro → concluida (CLAUDE.md, "A caixa é conferida duas vezes no
    # HOME"). Os quatro primeiros são "em execução" para `get_ordem_ativa`.
    with _lock:
        _estado["os_ativa"] = {
            "os_id":    os_id,
            "descricao": descricao,
            "status":   "aguardando_caixa_vazia",
            "itens":    medicamentos,
        }

    # O banco também precisa saber que esta OS saiu da fila: `get_ordem_ativa`
    # — e o GET /os/ativa que o app de manutenção consome — separa a OS em
    # execução das que esperam pelo STATUS. Enquanto o status de execução só
    # existia neste dicionário em memória, toda OS do banco continuava
    # "aguardando" e o endpoint devolvia a última enfileirada, não a que estava
    # rodando.
    try:
        await asyncio.to_thread(atualizar_status_ordem, os_id, "aguardando_caixa_vazia")
    except Exception as e:
        logger.warning("[DB] atualizar_status_ordem aguardando_caixa_vazia: %s", e)

    # ── 0. A receita gravada na mesa ─────────────────────────────────────────
    # A mesa não recebe coordenadas: ela executa o roteiro que foi gravado nela,
    # e o comando `mover` leva a LETRA do slot de receita. Uma OS que não seja
    # uma das dez padrão não tem roteiro gravado — e descobrir isso pelo
    # `receita_desconhecida` que a placa devolve no PRIMEIRO ciclo significaria
    # ter carregado os dispensers, limpado resíduo e gasto o ciclo inteiro para
    # nada. A checagem vem antes de reservar slot.
    receita = os_templates.receita_de(os_payload.get("template_id") or "")
    if receita is None:
        logger.error(
            "[ORCH] OS %s sem receita mapeada (template_id=%r) — a mesa não tem "
            "roteiro gravado para esta ordem. Abortada antes do primeiro mover.",
            os_id, os_payload.get("template_id"),
        )
        await _abortar_os(os_id, "receita_nao_mapeada")
        return
    logger.info("[ORCH] OS %s usa a receita %s da mesa.", os_id, receita)

    # ── 0b. HOME e caixa VAZIA, antes de reservar qualquer coisa ─────────────
    # A OS anterior terminou com a mesa no CENTRO: o homing é obrigatório, e é
    # handshake — a foto da caixa só vale com a mesa no HOME. A conferência vem
    # antes da reserva de slots: uma caixa esquecida é a operação esperando
    # alguém, e esperar com medicamento carregado seria estoque preso.
    geracao_inicio: Optional[int] = _porta_geracao
    while True:
        retomou = await _portao_do_home(cancelar, geracao_inicio)
        geracao_inicio = _porta_geracao
        # Depois de uma retomada a mesa já fez homing (é a condição de a pausa
        # acabar).
        motivo_homing = None if retomou else await _homing_confirmado(os_id, geracao_inicio)
        if not _houve_pausa(geracao_inicio):
            break
    if motivo_homing:
        logger.error("[ORCH] OS %s: homing do início não confirmou (%s) — a OS não "
                     "começa.", os_id, motivo_homing)
        await _abortar_os(os_id, "homing_inicial_falhou")
        return

    if not await _conferir_caixa_vazia(os_id, cancelar):
        await _encerrar_cancelada(os_id, "cancelada pelo console durante a espera da "
                                         "caixa vazia")
        return
    _publicar_caixa(None)
    # A caixa está vazia e nenhuma pausa está em vigor: o que uma pausa
    # anterior tenha armado no cronograma não vale para o ciclo que começa.
    await _portao_do_home(cancelar, geracao_inicio)
    await _status_da_os(os_id, "em_andamento")
    with _lock:
        disp_snapshot = {k: dict(v) for k, v in _estado["dispensers"].items()}

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
    for a, lido in zip(atribuicoes, pesos, strict=True):
        # `lido` e não `peso`: reatribuir a variável do laço confunde o que veio
        # do banco com o que foi decidido aqui, e é o fallback que interessa
        # entender depois.
        peso = lido
        if isinstance(lido, BaseException):
            logger.warning(
                "[DB] get_peso_medicamento(%s): %s — usando %.0f g.",
                a["medicamento"], lido, PESO_UNITARIO_PADRAO_G,
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
    nao_enviados = [a["dispenser_id"]
                    for a, ok in zip(atribuicoes, envios, strict=True)
                    if not ok]
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
    # Com a conferência de SKU desligada, nenhum slot é escaneado: o desfecho é
    # o mesmo de hoje quando o envio falha (slot segue sem validar SKU), sem
    # as três tentativas por slot contra uma câmera que não está lá.
    if settings.VISAO_SKU_HABILITADA:
        atribuicoes_scan = atribuicoes
    else:
        atribuicoes_scan = []
        logger.warning(
            "[ORCH] OS %s: validação de SKU desligada (VISAO_SKU_HABILITADA=0) — "
            "slots seguem sem conferência de produto.", os_id,
        )

    # Registrar eventos ANTES de solicitar scans
    chaves_visao_disp = [f"{os_id}:visao_dispenser:{a['dispenser_id']}" for a in atribuicoes_scan]
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
        for a in atribuicoes_scan
    ])

    # Aqui o envio recusado NÃO aborta a OS, ao contrário do carregamento:
    # câmera que não leu é fonte que deixou de confirmar, não fonte que
    # contradisse — é a mesma regra que já vale para `leitura_dispenser_falha`
    # e para o timeout logo abaixo. O que muda é só a espera: sem o comando na
    # planta o evento nunca vem, então o `aguardar_evento` seria TIMEOUT_VISAO_
    # DISPENSER de relógio queimado por um resultado que já sabemos que não
    # existe. Resolvemos o slot como `None` na hora, que é onde ele cairia.
    for a, ok in zip(atribuicoes_scan, envios_scan, strict=True):
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
        a = atribuicoes_scan[i]
        # O switch é lido AQUI, slot a slot: é o momento do veredito do SKU.
        veredito_sku = avaliar_sku_dispenser(res, visao_conta())
        if veredito_sku.ignorada:
            # Switch desligado: a leitura fica registrada (o handler já gravou)
            # e o slot segue sem conferência — um alarme, nunca trava.
            await _registrar_visao_ignorada(os_id, a["dispenser_id"], veredito_sku.ignorada)
        elif res is None:
            logger.warning(
                "[ORCH] Timeout visão dispenser D%d — sem validação de SKU.", a["dispenser_id"]
            )
        elif veredito_sku.travar:
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

    # A câmera da mesa só sabe o que caiu num slot pela DIFERENÇA entre o total
    # que ela vê na caixa agora e o último total que ela mesma registrou nesta
    # OS. Duas coisas quebram essa conta, e o laço do passo 4 as carrega:
    #
    # - `pendentes_mesa`: slots cuja contagem ainda não foi conferida — posição
    #   em que a câmera não vê a caixa inteira (VISAO_MESA_POSICOES), ou foto
    #   que falhou sem a estação registrar o total. A próxima foto vê todos eles
    #   juntos, e é contra a soma que ela tem de ser comparada; comparar com a
    #   quantidade de um slot só seria divergência falsa. Cada item é
    #   (slot, quantidade, já_pedido): a estação nunca refotografa um par
    #   (os_id, slot) que já recebeu, e é isso que decide quem pode ser usado na
    #   conferência final no HOME.
    # - `mesa_dessincronizada`: não se sabe se a estação registrou o total da
    #   última foto (timeout, envio recusado, timeout_processamento — que pode
    #   terminar depois e atualizar em silêncio), ou alguém pode ter mexido na
    #   caixa durante uma trava. A próxima divergência não é comparável: vira
    #   fonte indisponível, e a estação, ao registrar o total, ressincroniza.
    # - `deficit_mesa`: unidades que a câmera contou A MENOS, sozinha (possível
    #   oclusão — não trava), e que ainda não reapareceram. A estação registrou
    #   o total daquela foto, então a caixinha escondida volta como "a mais"
    #   numa foto seguinte; até esse número, "a mais" é reconciliação e não
    #   trava (`tolerancia_camera_mais`). Toda dessincronia o zera: a conta
    #   deixou de ser comparável.
    pendentes_mesa: list[tuple[int, int, bool]] = []
    mesa_dessincronizada = False
    deficit_mesa = 0
    # Para a foto FINAL: o que o ciclo CONFIRMOU ter posto na caixa (o
    # `quantidade_dispensada` do dispenser, ou o alvo quando ele não confirmou)
    # e o desfecho da balança em cada slot (True/False; None sem leitura).
    total_caixa = 0
    pesos_slots: list[Optional[bool]] = []

    # Loop de bloqueio: mantém trava até todos os slots com SKU errado serem corrigidos
    while slots_sku_errado:
        a_err, res_err = slots_sku_errado[0]
        if res_err.get("tipo") == _VISAO_INDISPONIVEL:
            motivo = (
                f"Câmera não respondeu ao re-scan do dispenser D{a_err['dispenser_id']}: "
                f"a divergência de SKU (lido={res_err.get('sku_lido', '?')} | "
                f"esperado={res_err.get('sku_esperado', '?')}) segue SEM confirmação. "
                f"Verifique a estação de visão e libere a trava para tentar de novo."
            )
            resumo = "visão indisponível"
        else:
            motivo = (
                f"SKU errado no dispenser D{a_err['dispenser_id']}: "
                f"lido={res_err.get('sku_lido', '?')} | esperado={res_err.get('sku_esperado', '?')} — "
                f"remova o medicamento incorreto e libere a trava para re-escanear."
            )
            resumo = "SKU errado"
        logger.error("[ORCH] ⛔ %s", motivo)

        # `_ativar_trava` arma o estado interno e só então publica em
        # `_estado["trava"]` — publicar aqui antes reabriria a janela em que a
        # tela mostra trava que `liberar_trava` ainda não reconhece.
        evento_lib = await _ativar_trava(os_id, a_err["dispenser_id"], motivo,
                                         resumo=resumo)
        logger.warning(
            "[ORCH] Aguardando operador corrigir dispenser D%d (OS %s)…",
            a_err["dispenser_id"], os_id,
        )
        await _aguardar_liberacao(evento_lib, cancelar)
        # Decisão F: com a trava aberta, alguém pode ter mexido na caixa de
        # coleta, e o central não tem como saber.
        mesa_dessincronizada = True

        with _lock:
            _estado["trava"] = {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""}
        if _broadcast_fn:
            _broadcast_fn()

        # Re-escaneia todos os slots que ainda têm divergência
        logger.info("[ORCH] Trava liberada. Re-escaneando %d slot(s) com SKU errado…", len(slots_sku_errado))
        atribuicoes_retry = [a for a, _ in slots_sku_errado]
        # A divergência ANTERIOR de cada slot, guardada antes de a lista ser
        # refeita: é ela que o motivo da trava cita quando a câmera não
        # responde ao re-scan — a pendência é a de sempre, o que faltou foi a
        # confirmação.
        res_anterior = {a["dispenser_id"]: res for a, res in slots_sku_errado}
        chaves_retry = [f"{os_id}:visao_dispenser:{a['dispenser_id']}" for a in atribuicoes_retry]
        for chave in chaves_retry:
            registrar_evento(chave)
        # Envio recusado é resolvido NA HORA como "sem medição", em vez de
        # queimar TIMEOUT_VISAO_DISPENSER esperando um evento que já se sabe
        # inexistente — a mesma regra da etapa 3b.
        envios_retry = await asyncio.gather(*[
            cmd_visao_dispenser(
                a["dispenser_id"], a.get("sku", ""), a["medicamento"], a["quantidade"], os_id,
            )
            for a in atribuicoes_retry
        ])
        for a, chave, ok in zip(atribuicoes_retry, chaves_retry, envios_retry,
                                strict=True):
            if not ok:
                logger.error(
                    "[ORCH] ⛔ Re-scan D%d não foi aceito pelo vision-adapter.",
                    a["dispenser_id"],
                )
                _pending_events.pop(chave, None)

        resultados_retry = await asyncio.gather(*[
            aguardar_evento(chave, settings.TIMEOUT_VISAO_DISPENSER)
            if chave in _pending_events else _nulo()
            for chave in chaves_retry
        ])

        # Verifica se ainda há divergência após a correção
        slots_sku_errado = []
        for a, res in zip(atribuicoes_retry, resultados_retry, strict=True):
            # O switch pode ter sido desligado com a trava aberta — é o roteiro
            # de emergência: a trava atual se libera pelo botão de sempre, e o
            # re-scan já não trava mais pela câmera.
            veredito_sku = avaliar_sku_dispenser(
                res if res is not None else {"tipo": _VISAO_INDISPONIVEL},
                visao_conta(), rescan=True)
            if veredito_sku.ignorada:
                await _registrar_visao_ignorada(os_id, a["dispenser_id"],
                                                veredito_sku.ignorada)
            elif res is None:
                # Fonte que não RESPONDEU não é fonte que CONFIRMOU, e aqui a
                # diferença decide o que sai do dispenser. Este slot tem SKU
                # comprovadamente errado — foi essa medição que armou a trava.
                # Soltá-lo por falta de resposta transformava o vision-adapter
                # fora do ar num caminho para dispensar o medicamento errado:
                # bastava liberar a trava e esperar o timeout.
                #
                # É a mesma linha que `avaliar_triple_check` já traça entre
                # `divergencias` e `fontes_indisponiveis`, com o sinal
                # invertido de propósito: lá a fonte muda faz a OS seguir
                # porque nada a contradisse; aqui já há contradição registrada,
                # e o silêncio não a apaga.
                logger.error(
                    "[ORCH] ⛔ Re-scan D%d sem resposta da câmera — a trava CONTINUA.",
                    a["dispenser_id"],
                )
                slots_sku_errado.append((
                    a,
                    {**res_anterior.get(a["dispenser_id"], {}),
                     "tipo": _VISAO_INDISPONIVEL},
                ))
            elif res.get("tipo") == "leitura_dispenser_divergencia":
                logger.error(
                    "[ORCH] ⛔ Ainda SKU errado em D%d após re-scan. Nova trava.", a["dispenser_id"]
                )
                slots_sku_errado.append((a, res))
            else:
                logger.info("[ORCH] Re-scan D%d OK — SKU=%s.", a["dispenser_id"], res.get("sku_lido", ""))

    logger.info("[ORCH] Validação de visão concluída. Realizando tara da balança.")

    # ── 3c. Tara da balança HX711 (antes de qualquer dispensa) ───────────────
    # Nunca com a porta aberta: uma mão na caixa entraria no zero da balança.
    await _aguardar_retomada_porta(cancelar)
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

        # ── O CICLO É POR RELÓGIO, e o evento da placa não é portão ──────────
        #
        # A mesa avisa que chegou, mas ninguém espera o aviso para seguir. O
        # central calcula QUANDO cada peça acontece (`cronograma_do_ciclo`) e
        # dispara na hora marcada, tenha o evento chegado ou não.
        #
        # O que o evento passou a valer: ele REGISTRA (a posição medida, a
        # contagem do dispenser) e, no máximo, CANCELA. Um evento que fizesse o
        # central esperar recriaria o handshake por acidente — e é exatamente
        # isso que quem mexer aqui vai querer fazer quando vir uma OS seguir sem
        # confirmação. O custo de esperar está escrito em
        # `docs/PROTOCOLO_SERIAL.md`, seção "Por que o relógio e não a
        # confirmação".
        qtd_esperada = a["quantidade"]
        _slot_em_curso = disp_id

        # ── A PORTA NR-12 pode parar este ciclo a qualquer momento ───────────
        #
        # A pausa interrompe os prazos do relógio (`cancelar_cronograma`), e o
        # ciclo a reconhece pela GERAÇÃO: prazo que acordou cedo com a geração
        # mudada foi pausa, e o slot se REFAZ depois da retomada — a retomada
        # faz homing, então toda pausa custa a posição. O que se preserva é a
        # CONTAGEM: unidades que já caíram (`dispensado_parcial`) não saem de
        # novo, e o `dispensar` da retomada pede só o que falta. Sem contagem
        # confirmada, nada se reenvia: o slot cai na trava do Triple Check.
        confirmado_antes = 0
        sem_contagem_parcial = False
        dispensa_feita = False
        resultado_pos: Optional[dict] = None
        resultado_disp: Optional[dict] = None

        while True:
            await _aguardar_retomada_porta(cancelar)
            geracao = _porta_geracao
            restante = qtd_esperada - confirmado_antes
            espera_trajeto, duracao_dispensa = cronograma_do_ciclo(restante)

            # 4a. As DUAS chaves são registradas antes de qualquer comando sair.
            # Registrar a do `dispensado` aqui, e não depois do trajeto, é o
            # que faz um `dispensado` rápido não chegar antes de haver quem o
            # guarde.
            chave_pos  = f"{os_id}:posicionado:{disp_id}"
            chave_disp = f"{os_id}:dispensado:{disp_id}"
            registrar_evento(chave_pos)
            registrar_evento(chave_disp)

            # 4b. Mover. Falha de POST continua abortando: é falha de
            # TRANSPORTE (ou ACK negativo da placa, que o adapter devolve como
            # 502), não falta de confirmação. No modelo por relógio esta é a
            # ÚNICA defesa que age antes do `dispensar` — ver o veto em 4c.
            ok = await cmd_mover(disp_id, os_id, receita, seq, total)
            if _houve_pausa(geracao):
                colher_evento(chave_pos)
                colher_evento(chave_disp)
                continue
            if not ok:
                logger.error("[ORCH] Mesa recusou ou não recebeu o mover para D%d.", disp_id)
                colher_evento(chave_pos)
                colher_evento(chave_disp)
                await _abortar_os(os_id, "erro_cnc", atribuicoes)
                return

            # 4c. O prazo do trajeto. Não é `aguardar_evento`: é o relógio.
            # `_dormir_ou_cancelar` acorda antes se a OS for cancelada no meio
            # — a trava, o reset ou a porta disparam esse Event.
            cumpriu = await _dormir_ou_cancelar(espera_trajeto, cancelar)
            if _houve_pausa(geracao):
                colher_evento(chave_pos)
                colher_evento(chave_disp)
                continue
            if not cumpriu:
                logger.warning("[ORCH] Cronograma cancelado durante o trajeto até D%d.", disp_id)
                colher_evento(chave_pos)
                colher_evento(chave_disp)
                return

            # ── O VETO ───────────────────────────────────────────────────────
            # A mesa pode NÃO ter chegado: limit disparado, origem perdida,
            # placa muda. Disparar o `dispensar` assim mesmo despeja medicamento
            # fora da célula. O cronograma não pode ESPERAR (seria o handshake de
            # volta), mas pode ser cancelado por um evento que já chegou.
            #
            # ⚠ A AUSÊNCIA DO `posicionado` NÃO CANCELA. Um evento perdido no
            # encaminhamento NÃO é a mesa parada, e tratar os dois como iguais
            # aborta OS com o hardware intacto. Só um `erro` EXPLÍCITO veta.
            erro_cnc = espiar_evento(chave_pos)
            if erro_cnc is not None and erro_cnc.get("tipo") == "erro":
                logger.error("[ORCH] ERRO da mesa em D%d (%s): %s — dispensa NÃO disparada.",
                             disp_id, erro_cnc.get("codigo_erro", "?"),
                             erro_cnc.get("descricao", ""))
                colher_evento(chave_pos)
                colher_evento(chave_disp)
                await _abortar_os(os_id, "erro_cnc", atribuicoes)
                return

            if dispensa_feita:
                # A dispensa deste slot terminou antes da pausa; a volta foi só
                # para a câmera da mesa ver a caixa no lugar dela.
                resultado_pos = colher_evento(chave_pos)
                colher_evento(chave_disp)
                break

            # 4d. Dispensar, na hora marcada. A dispensa em voo é publicada
            # ANTES do comando: a parada da porta precisa saber que há uma.
            _dispensa_em_voo = {"os_id": os_id, "slot": disp_id, "parou": asyncio.Event()}
            ok = await cmd_dispensar(disp_id, os_id,
                                     quantidade=restante if confirmado_antes else None)
            if ok and _houve_pausa(geracao):
                # A pausa chegou com o `dispensar` no fio: o `parar` pode ter
                # chegado ANTES dele à placa, que o descarta ao começar a
                # dispensa. Um segundo `parar`, agora, pega a dispensa.
                await _post(settings.DISPENSER_ADAPTER_URL + "/comandos/parar",
                            {"motivo": "porta_nr12"})
            if not ok:
                _dispensa_em_voo["parou"].set()
                _dispensa_em_voo = None
                if _houve_pausa(geracao):
                    colher_evento(chave_pos)
                    colher_evento(chave_disp)
                    continue
                logger.error("[ORCH] Falha ao enviar cmd dispensar para D%d.", disp_id)
                colher_evento(chave_pos)
                colher_evento(chave_disp)
                await _abortar_os(os_id, "erro_dispenser", atribuicoes)
                return

            # 4e. O prazo da dispensa — a mesma conta do dwell gravado na receita.
            cumpriu = await _dormir_ou_cancelar(duracao_dispensa, cancelar)
            if _houve_pausa(geracao):
                # O dispenser termina a unidade e para. Quantas caíram é o que
                # decide a retomada — e esperar por isso cabe no mesmo prazo
                # que a porta espera para autorizar a abertura.
                parcial = await aguardar_evento(chave_disp, settings.PORTA_PARADA_MAX_S)
                _dispensa_em_voo["parou"].set()
                _dispensa_em_voo = None
                colher_evento(chave_pos)
                tipo_parcial = (parcial or {}).get("tipo")
                if tipo_parcial == "dispensado_parcial":
                    caidas = _inteiro(parcial.get("quantidade_confirmada"))
                    confirmado_antes += caidas
                    logger.warning("[ORCH] D%d parado pela porta: %d de %d caíram — a "
                                   "retomada pede só o que falta.", disp_id,
                                   confirmado_antes, qtd_esperada)
                    if confirmado_antes >= qtd_esperada:
                        resultado_disp = {"tipo": "dispensado", "quantidade_dispensada": 0}
                        dispensa_feita = True
                elif tipo_parcial in ("dispensado", "erro"):
                    # A dispensa terminou antes de o `parar` alcançá-la.
                    resultado_disp = parcial
                    dispensa_feita = True
                else:
                    # Sem contagem: não se adivinha. Nada se reenvia.
                    logger.error("[ORCH] D%d parado pela porta SEM contagem parcial — "
                                 "o slot vai para a trava do Triple Check.", disp_id)
                    sem_contagem_parcial = True
                    dispensa_feita = True
                continue
            _dispensa_em_voo["parou"].set()
            _dispensa_em_voo = None
            if not cumpriu:
                logger.warning("[ORCH] Cronograma cancelado durante a dispensa em D%d.", disp_id)
                colher_evento(chave_pos)
                colher_evento(chave_disp)
                return

            # 4f. Colhe o que chegou, SEM esperar. Daqui em diante ninguém mais
            # espera por estas chaves, então é colheita e não espiada.
            resultado_pos  = colher_evento(chave_pos)
            resultado_disp = colher_evento(chave_disp)
            dispensa_feita = True
            break

        if sem_contagem_parcial:
            # A porta parou a dispensa e o dispenser não disse quantas caíram.
            # Adivinhar seria dose dobrada (reenviar tudo) ou dose curta (seguir
            # como se tivesse caído): quem decide é o supervisor, com a caixa
            # na frente — a mesma liberação de toda trava.
            motivo_trava = (
                f"pausa_porta_sem_contagem_parcial — D{disp_id}: a porta NR-12 "
                f"parou a dispensa e o dispenser não informou quantas unidades "
                f"caíram. Confira a caixa de coleta e libere a trava; o slot não "
                f"será redispensado."
            )
            evento_lib = await _ativar_trava(os_id, disp_id, motivo_trava,
                                             resumo="pausa sem contagem")
            await _aguardar_liberacao(evento_lib, cancelar)
            mesa_dessincronizada = True
            with _lock:
                _estado["trava"] = {"ativa": False, "os_id": None, "slot_id": None,
                                    "motivo": ""}
            if _broadcast_fn:
                _broadcast_fn()
            resultado_pos = (await _reposicionar(disp_id, os_id, receita, seq, total,
                                                 cancelar)) or resultado_pos
            resultado_disp = None
        elif (confirmado_antes and resultado_disp is not None
              and resultado_disp.get("tipo") == "dispensado"):
            # O TOTAL do slot, as duas partes: é ele que o Triple Check compara.
            resultado_disp = {
                **resultado_disp,
                "quantidade_dispensada": _inteiro(resultado_disp.get("quantidade_dispensada"))
                + confirmado_antes,
            }

        desfecho, qtd_dispensada = _desfecho_do_ciclo(
            disp_id, qtd_esperada, resultado_pos, resultado_disp)
        _registrar_desfecho(disp_id, os_id, desfecho, qtd_dispensada, qtd_esperada,
                            a["medicamento"])
        total_caixa += qtd_esperada if qtd_dispensada is None else qtd_dispensada

        # 4c. Scan da câmera da mesa (não-bloqueante)
        chave_visao_mesa = f"{os_id}:visao_mesa:{disp_id}"

        # A posição vem da MÁQUINA (o evento `posicionado` carrega a medida do
        # tracker da placa). Só quando ela não vem é que se cai no modelo — e
        # o log diz isso, porque um número do modelo e um medido valem coisas
        # diferentes para quem depois lê o histórico.
        #
        # O default 0.0 que estava aqui era a pior saída possível: 0.0 é a
        # ORIGEM da mesa, então a câmera seria mandada olhar para o HOME e
        # chamar aquilo de D5 — divergência de contagem num slot só, que é
        # indistinguível de medicamento faltando.
        # Porta aberta entre a dispensa e a foto: espera a retomada e leva a
        # mesa de volta ao slot — a retomada fez homing.
        if await _aguardar_retomada_porta(cancelar):
            resultado_pos = (await _reposicionar(disp_id, os_id, receita, seq, total,
                                                 cancelar)) or resultado_pos
        pos_x, pos_y = _posicao_do_evento(resultado_pos, disp_id)
        resultado_mesa: Optional[dict] = None
        motivo_mesa: Optional[str] = None
        esperado_camera: Optional[int] = None
        slots_camera: tuple = ()

        if disp_id not in settings.VISAO_MESA_POSICOES:
            pendentes_mesa.append((disp_id, qtd_esperada, False))
            motivo_mesa = ("câmera sem visão completa nesta posição — conferido junto "
                           "com o próximo slot visível")
            logger.info("[ORCH] Câmera mesa D%d não fotografada (fora de "
                        "VISAO_MESA_POSICOES) — pendentes: %s",
                        disp_id, [p[0] for p in pendentes_mesa])
        else:
            slots_camera = tuple(p[0] for p in pendentes_mesa) + (disp_id,)
            esperado_camera = qtd_esperada + sum(p[1] for p in pendentes_mesa)
            registrar_evento(chave_visao_mesa)
            logger.info("[ORCH] Câmera mesa D%d: pedindo captura | OS=%s esperado=%d "
                        "slots_cobertos=%s", disp_id, os_id, esperado_camera,
                        list(slots_camera))
            ok = await cmd_visao_mesa(disp_id, os_id, esperado_camera, pos_x, pos_y,
                                      slots_cobertos=list(slots_camera),
                                      quantidade_slot=qtd_esperada)
            if ok:
                resultado_mesa = await aguardar_evento(chave_visao_mesa,
                                                       settings.TIMEOUT_VISAO_MESA)
            else:
                # Envio recusado decide NA HORA, como nas etapas 3 e 3b: o
                # evento não vai vir, e esperar TIMEOUT_VISAO_MESA por ele só
                # queimaria relógio — 30 s por slot com a estação caída. O
                # desfecho continua o de um timeout (não se sabe se a estação
                # registrou o total: o `_post` pode ter desistido depois de ela
                # processar), só o motivo diz a verdade.
                _pending_events.pop(chave_visao_mesa, None)
                motivo_mesa = "comando não aceito"
                logger.warning("[ORCH] Câmera mesa D%d: comando não aceito pelo "
                               "vision-adapter — sem foto neste slot.", disp_id)
            tipo_mesa = resultado_mesa.get("tipo") if resultado_mesa else None
            nao_comparavel = _mesa_nao_comparavel(resultado_mesa, mesa_dessincronizada)

            if tipo_mesa in ("leitura_mesa_ok", "leitura_mesa_divergencia"):
                # A estação registrou o total que viu: a conta volta a fechar.
                pendentes_mesa = []
                mesa_dessincronizada = False
                if nao_comparavel:
                    deficit_mesa = 0
            elif (tipo_mesa == "leitura_mesa_falha"
                    and resultado_mesa.get("motivo") != "timeout_processamento"):
                # Falha que a estação NÃO registrou: o total dela continua o de
                # antes, e o que caiu aqui entra na próxima foto.
                pendentes_mesa.append((disp_id, qtd_esperada, True))
            else:
                # Timeout, envio recusado ou timeout_processamento: não se sabe
                # se a estação registrou o total.
                pendentes_mesa = []
                mesa_dessincronizada = True

            if (tipo_mesa == "leitura_mesa_divergencia" and not nao_comparavel
                    and _inteiro(resultado_mesa.get("quantidade_detectada"))
                    == esperado_camera):
                # Divergência com a contagem igual à esperada: payload
                # incoerente, que o Triple Check trata como fonte indisponível.
                # Como o handler do central deixou de gravar alarme para a
                # divergência da mesa, quem grava é o orquestrador — todo
                # caminho de uma divergência da mesa termina em um alarme ou
                # numa trava.
                await _alarme_mesa("leitura_mesa_incoerente",
                                   f"Câmera mesa D{disp_id} OS {os_id}: divergência com "
                                   f"a contagem igual à esperada ({esperado_camera}) — "
                                   f"payload incoerente, câmera sem medição.")

            if tipo_mesa == "leitura_mesa_divergencia" and nao_comparavel:
                motivo_mesa = nao_comparavel
                descricao = (f"Câmera mesa D{disp_id} OS {os_id}: divergência não "
                             f"conferida — {motivo_mesa}; detectou "
                             f"{resultado_mesa.get('quantidade_detectada')} de "
                             f"{esperado_camera}.")
                logger.warning("[ORCH] %s", descricao)
                try:
                    await asyncio.to_thread(salvar_alarme, f"camera_mesa_{disp_id}",
                                            "camera_mesa_ressincronizando", descricao)
                except Exception as e:
                    logger.warning("[DB] salvar_alarme ressincronizando: %s", e)
                resultado_mesa = None

            # O ciclo é por relógio: sem o `dispensado`, o central não sabe se o
            # mecanismo terminou — servo mais lento que o cronograma, pulso
            # atrasado. A estação acabou de registrar um total que pode não
            # incluir unidades ainda caindo, e a que cair depois apareceria
            # como "a mais" na foto do slot SEGUINTE: trava no slot errado, com
            # este marcado OK. A foto seguinte deixa de ser comparável. Não se
            # espera o `dispensado` — o relógio não ganha portão novo.
            if (desfecho == DESFECHO_SEM_CONFIRMACAO
                    and tipo_mesa in ("leitura_mesa_ok", "leitura_mesa_divergencia")):
                mesa_dessincronizada = True
                logger.warning("[ORCH] Câmera mesa D%d: foto tirada sem a confirmação da "
                               "dispensa — o total registrado pela estação pode não "
                               "incluir unidades ainda caindo; a próxima foto não será "
                               "comparada.", disp_id)

            if resultado_mesa is None and motivo_mesa is None:
                logger.warning(
                    "[ORCH] Timeout câmera mesa D%d — continuando sem validação de contagem.", disp_id
                )
            elif tipo_mesa in ("leitura_mesa_falha", "leitura_mesa_divergencia"):
                logger.warning(
                    "[ORCH] ALARME câmera mesa D%d: %s | esp=%s det=%s | slots=%s | "
                    "pendentes=%s",
                    disp_id, tipo_mesa, esperado_camera,
                    resultado_mesa.get("quantidade_detectada") if resultado_mesa else "-",
                    list(slots_camera), [p[0] for p in pendentes_mesa],
                )
            elif resultado_mesa is not None:
                logger.info(
                    "[ORCH] Câmera mesa D%d OK — detectado=%s de %d %s conf=%.0f%%",
                    disp_id, resultado_mesa.get("quantidade_detectada", 0),
                    esperado_camera, list(slots_camera),
                    (resultado_mesa.get("confianca", 0) or 0) * 100,
                )

        # 4d. Pesagem HX711 (bloqueante se Triple Check divergir). Com a porta
        # aberta, não: a da frente é a da caixa sobre a balança.
        await _aguardar_retomada_porta(cancelar)
        chave_peso = f"{os_id}:peso:{disp_id}"
        registrar_evento(chave_peso)
        peso_unit = a.get("peso_unitario_g") or PESO_UNITARIO_PADRAO_G
        resultado_peso: Optional[dict] = None
        # `quantidade_real` é o melhor palpite sobre o que caiu na mesa; sem a
        # confirmação do dispenser, o melhor palpite é o alvo. São perguntas
        # diferentes e é por isso que os dois valores se separam aqui: a balança
        # precisa de um número para simular a massa depositada, e o Triple Check
        # precisa saber que a fonte 1 NÃO mediu. Passar o alvo para os dois faria
        # a fonte 1 confirmar uma contagem que ninguém fez.
        qtd_para_balanca = qtd_esperada if qtd_dispensada is None else qtd_dispensada
        ok_p = await cmd_pesar(disp_id, os_id, qtd_esperada, peso_unit, qtd_para_balanca)
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
        tipo_peso = (resultado_peso or {}).get("tipo")
        pesos_slots.append(None if tipo_peso not in ("peso_ok", "peso_divergencia")
                           else tipo_peso == "peso_ok")

        # ── 4e. TRIPLE CHECK — valida as 3 fontes (dispenser, câmera mesa, balança) ──
        # Regra em `avaliar_triple_check`: por default 1 divergência já trava.
        veredito = avaliar_triple_check(
            quantidade_esperada=qtd_esperada,
            quantidade_dispensada=qtd_dispensada,
            resultado_mesa=resultado_mesa,
            resultado_peso=resultado_peso,
            esperado_camera=esperado_camera,
            slots_camera=slots_camera,
            motivo_mesa_indisponivel=motivo_mesa,
            tolerancia_camera_mais=deficit_mesa,
            # Lido NESTE slot, não no começo da OS: desligar no meio vale já aqui.
            visao_conta=visao_conta(),
        )
        # O déficit anda nas duas posições do switch — é o que permite religar
        # no meio da OS sem a caixinha escondida reaparecer como "a mais".
        deficit_mesa += veredito.oclusao - veredito.reconciliado
        for texto in veredito.visao_ignorada:
            await _registrar_visao_ignorada(os_id, disp_id, texto)
        for texto in veredito.informativos:
            logger.info("[ORCH] Triple Check D%d OS %s: %s", disp_id, os_id, texto)
        n_div  = len(veredito.divergencias)
        causas = veredito.divergencias

        if veredito.alertas:
            # Não trava: a câmera sozinha contando a menos pode ser oclusão.
            # Mas fica no banco — é a balança que segura esse caso, e alguém
            # precisa poder ver quantas vezes ele aconteceu.
            descricao = (f"Triple Check D{disp_id} OS {os_id} (OS prossegue): "
                         + "; ".join(veredito.alertas))
            logger.warning("[ORCH] %s", descricao)
            try:
                await asyncio.to_thread(
                    salvar_alarme, "triple_check", "contagem_camera_abaixo", descricao
                )
            except Exception as e:
                logger.warning("[DB] salvar_alarme contagem_camera_abaixo: %s", e)

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
            await _aguardar_liberacao(evento_liberacao, cancelar)
            # Decisão F: com a trava aberta, alguém pode ter mexido na caixa.
            mesa_dessincronizada = True
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

        if mesa_dessincronizada:
            deficit_mesa = 0

    # ── 5. HOME, caixa CHEIA e centro ────────────────────────────────────────
    #
    # O homing é CONFERIDO (handshake): a foto FINAL só vale com a mesa no HOME,
    # e a OS seguinte planeja a serpentina a partir dele. A FINAL não herda os
    # pendentes nem a dessincronia das fotos por slot — ela conta a caixa
    # INTEIRA, sob um `os_id` que a estação nunca viu. Os pendentes que sobraram
    # (slots sem foto) estão cobertos por ela.
    _slot_em_curso = None
    if pendentes_mesa:
        logger.info("[ORCH] OS %s: %s sem foto de slot — cobertos pela conferência "
                    "final da caixa no HOME.", os_id,
                    ", ".join(f"D{p[0]}" for p in pendentes_mesa))
    peso_ok: Optional[bool] = (None if not pesos_slots or None in pesos_slots
                               else all(pesos_slots))
    logger.info("[ORCH] Ciclo completo. Conferindo a caixa no HOME (esperado %d).",
                total_caixa)
    await _fechar_no_home(os_id, total_caixa, peso_ok, cancelar)

    # ── 6. OS concluída — o ÚNICO lugar que conclui com sucesso ──────────────
    await _concluir_os(os_id, atribuicoes)


async def _concluir_os(os_id: str, atribuicoes: list) -> None:
    """A mesa chegou ao centro: a OS acabou. Ninguém mais marca `concluida` —
    nem o `concluido` da mesa, que é fim de homing (CLAUDE.md, "A caixa é
    conferida duas vezes no HOME")."""
    try:
        await asyncio.to_thread(atualizar_status_ordem, os_id, "concluida")
    except Exception as e:
        logger.warning("[DB] atualizar_status_ordem: %s", e)

    with _lock:
        _estado["os_ativa"] = None
        _estado["atribuicao_ia"] = []
        _estado["caixa"] = None
        for a in atribuicoes:
            key = str(a["dispenser_id"])
            if key in _estado["dispensers"]:
                _estado["dispensers"][key]["os_id"] = None
                _estado["dispensers"][key]["status"] = "idle"

    logger.info("\n%s\n[ORCH] OS CONCLUÍDA: %s\n%s", "=" * 60, os_id, "=" * 60)
    if _visao_ignoradas_os:
        _historico("os_concluida_visao_fora", marca_visao_fora(os_id, _visao_ignoradas_os))
    _zerar_visao_ignoradas()
    if _broadcast_fn:
        _broadcast_fn()


def marca_visao_fora(os_id: str, n: int) -> str:
    """A frase do histórico e do detalhe da OS — uma só, para as duas telas."""
    plural = "s" if n != 1 else ""
    return (f"OS {os_id} concluída com a visão fora do Triple Check "
            f"({n} leitura{plural} ignorada{plural})")


async def _encerrar_cancelada(os_id: str, motivo: str) -> None:
    """A OS sai sem ter reservado nada (espera da caixa vazia cancelada)."""
    logger.warning("[ORCH] OS %s cancelada: %s.", os_id, motivo)
    _limpar_eventos_os(os_id)
    try:
        await asyncio.to_thread(atualizar_status_ordem, os_id, "cancelada")
    except Exception as e:
        logger.warning("[DB] atualizar_status_ordem cancelada: %s", e)
    with _lock:
        _estado["os_ativa"] = None
        _estado["atribuicao_ia"] = []
        _estado["caixa"] = None
    if _broadcast_fn:
        _broadcast_fn()


async def _retomar_etapa_final(os_payload: dict) -> None:
    """Reinício do central com a OS em `conferindo_caixa`/`indo_ao_centro`.

    A dispensa já tinha terminado: a OS volta para "homing → FINAL → centro".
    O esperado vem do banco (`os_itens.quantidade_real`); a balança não tem o
    que dizer depois de um reinício (`peso_ok=None`), então uma caixa com menos
    do que o banco registrou trava — o supervisor confere.
    """
    global _cancelar_cronograma
    os_id = os_payload["os_id"]
    esperado = int(os_payload.get("esperado") or 0)
    cancelar = asyncio.Event()
    _cancelar_cronograma = cancelar
    _zerar_visao_ignoradas()
    logger.warning("[ORCH] OS %s retomada depois de um reinício do central: homing, "
                   "conferência da caixa (esperado %d) e centro.", os_id, esperado)
    with _lock:
        _estado["os_ativa"] = {"os_id": os_id, "descricao": os_payload.get("descricao", ""),
                               "status": "conferindo_caixa", "itens": [],
                               "retomada": True}
    await _fechar_no_home(os_id, esperado, None, cancelar)
    await _concluir_os(os_id, [])


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
    # Só `_reset_em_curso` é ATRIBUÍDO aqui: os cinco campos da trava passaram
    # a ser escritos em `_resetar_planta`, e uma declaração `global` que só lê
    # não faz nada além de sugerir uma escrita que não existe.
    global _reset_em_curso

    # O flag sobe ANTES da leitura de `os_ativa`, e a ordem é a feature: do
    # outro lado, o `loop_orquestrador` o consulta logo depois de tirar a OS da
    # fila. Levantado primeiro, não existe instante em que a checagem veja "não
    # há OS" e uma OS comece mesmo assim — é a mesma disciplina de
    # `_ativar_trava` (estado real primeiro, publicação depois).
    _reset_em_curso = True
    try:
        # E o cronograma morre junto. Ele é ARMADO por quem precisa parar a OS
        # agora, e o reset é um desses — o docstring de `_cancelar_cronograma`
        # já dizia isso, e era a única parte dele que não era verdade. Sem OS em
        # curso vira no-op; com uma OS que tenha escapado pela janela acima, é o
        # que impede o `dispensar` agendado de sair com a mesa já sendo limpa.
        cancelar_cronograma()

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
        return await _resetar_planta(limpar_historico_tambem)
    finally:
        # `finally` e não uma linha no fim: a recusa levanta `ResetRecusado`, e
        # um flag que ficasse de pé depois dela calaria o orquestrador para
        # sempre — toda OS seguinte seria cancelada por um reset que não
        # aconteceu.
        _reset_em_curso = False


async def _resetar_planta(limpar_historico_tambem: bool) -> dict:
    """O reset em si, já com a recusa decidida e o flag de pé.

    Separada de `resetar_planta` para que o `try/finally` do flag envolva TODAS
    as saídas sem embrulhar noventa linhas num nível a mais de indentação.
    """
    global _trava_ativa, _trava_evento, _trava_motivo, _trava_slot_id, _trava_os_id

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
    for slot, ok in zip(range(1, NUM_SLOTS + 1), resultados, strict=True):
        (relatorio["slots_limpos"] if ok else relatorio["slots_com_falha"]).append(slot)
        try:
            await asyncio.to_thread(limpar_dispenser_estado, slot)
        except Exception as exc:
            logger.warning("[DB] limpar_dispenser_estado D%d: %s", slot, exc)

    with _lock:
        for slot in _estado["dispensers"].values():
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

        # A checagem vem DEPOIS do `get`, e não antes, porque é aqui que o loop
        # passa a maior parte do tempo: bloqueado em `_os_queue.get()`. O
        # `get_nowait` do reset não vê nada nesse estado — a fila está vazia —,
        # e um `put` que chegasse durante o reset seria entregue direto a este
        # `get` que já estava esperando. Conferir só no topo do laço deixaria
        # justamente essa OS passar.
        if _reset_em_curso:
            logger.warning("[ORCH] OS %s cancelada: reset da planta em curso.", os_id)
            try:
                await asyncio.to_thread(cancelar_ordens_pendentes, [os_id])
            except Exception as exc:
                logger.warning("[DB] cancelar_ordens_pendentes (reset): %s", exc)
            _os_queue.task_done()
            continue

        # Nenhuma OS nova começa com a porta NR-12 em pausa. A espera é SEM
        # teto: quem a encerra é a tag de rearme, que é uma pessoa dizendo
        # "pode voltar".
        if _pausa_porta is not None:
            logger.warning("[ORCH] OS %s aguarda: célula parada pela porta NR-12.", os_id)
            await _aguardar_porta_livre()

        try:
            if os_payload.get("retomar_final"):
                await _retomar_etapa_final(os_payload)
            else:
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
