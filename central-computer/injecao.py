"""
APSEN — Injeção de falha sob demanda: um gatilho armado, consumido uma vez.

Para que isto existe
────────────────────
O Triple Check e a trava de emergência são o diferencial técnico do sistema, e
hoje eles só aparecem quando o sorteio resolve falhar. Numa apresentação isso
dá duas situações ruins e nenhuma boa: ou a falha não acontece e o recurso não
é mostrado, ou acontece no meio de outra explicação. O console arma
"próxima falha: divergência de peso no D3", a falha ocorre na próxima ocorrência
aplicável, e o gatilho se desarma sozinho.

Uma fonte, e ela é o CENTRAL
────────────────────────────
O gatilho mora aqui, em memória do central, e viaja no comando que o
orquestrador já envia — `injetar_falha` é um campo a mais no corpo de
`/comandos/dispensar`, `/comandos/capturar/*` e `/comandos/pesar`. Não há
"armar o simulador".

A alternativa era guardar o armado DENTRO de cada simulador, por uma rota nova
de armar/desarmar em três adapters e três simuladores. Ela cria o problema que
este repositório já resolveu duas vezes (o mapa de posições da CNC, a lista de
ordens padrão): dois lugares guardando o mesmo fato, que divergem. E aqui a
divergência seria visível na pior hora — o console mostrando "armado" depois de
um restart do simulador que esqueceu o gatilho, ou mostrando "desarmado" com
uma falha ainda a caminho. Com o gatilho só no central, "armado" é sempre o que
o central vai injetar no próximo comando aplicável.

O one-shot mora no `consumir()`, que é check-and-pop sob lock: a decisão de
"esta é a ocorrência aplicável" acontece no momento em que o comando é montado,
que é exatamente onde o orquestrador sabe o slot, a etapa e a OS.

O que este módulo NÃO faz
─────────────────────────
Não altera a lógica normal de simulador nenhum. Ele só decide se um campo extra
vai no comando; o efeito é um ramo EXPLÍCITO e isolado do lado do simulador,
marcado como caminho de demonstração e anterior a qualquer sorteio. Por isso a
injeção funciona com `MODO_APRESENTACAO` ligado: o modo zera o acaso, a injeção
liga o que foi escolhido. São controles independentes, e essa combinação — sem
surpresa, com a falha que eu quero, na hora que eu quero — é a da banca.

Nada persiste. Restart do central desarma, como a pausa do gerador: o estado
default do sistema é não injetar nada, e um gatilho gravado em banco
sobreviveria à apresentação que o motivou.
"""
import logging
import threading
import time

from config import settings

logger = logging.getLogger(__name__)


class InjecaoInvalida(ValueError):
    """Pedido de arme que o console mandou errado — vira 422, não 500."""


# ── Catálogo dos tipos ────────────────────────────────────────────────────────
#
# Fonte ÚNICA do que pode ser armado. O console monta o seletor a partir daqui
# (`GET /console/api/injecao`), o orquestrador decide por aqui quais tipos cada
# comando aceita, e o teste cobra que os três concordem. Uma lista escrita à mão
# no HTML ofereceria um dia um tipo que nenhum simulador entende, e o sintoma
# seria o gatilho armado que nunca dispara — sem erro em lugar nenhum.

TIPO_SKU_DISPENSER        = "sku_dispenser"
TIPO_FALHA_LEITURA_DISP   = "falha_leitura_dispenser"
TIPO_DIVERGENCIA_MESA     = "divergencia_mesa"
TIPO_DIVERGENCIA_PESO     = "divergencia_peso"
TIPO_FALHA_MECANICA       = "falha_mecanica_dispenser"

TIPOS: dict[str, dict] = {
    TIPO_SKU_DISPENSER: {
        "rotulo":     "SKU errado na câmera do dispenser",
        "efeito":     ("A câmera da fileira do slot lê um SKU que não bate com o "
                       "esperado. Ativa a TRAVA bloqueante e o laço de re-scan: "
                       "a OS só anda quando um supervisor liberar."),
        "comando":    "/comandos/capturar/dispenser",
        "simulador":  "vision",
        "evento":     "leitura_dispenser_divergencia",
        "bloqueante": True,
    },
    TIPO_FALHA_LEITURA_DISP: {
        "rotulo":     "Falha de leitura da câmera do dispenser",
        "efeito":     ("A câmera não consegue ler o código. Fonte que deixou de "
                       "confirmar, não que contradisse: gera alarme e a OS "
                       "continua, sem validação de SKU naquele slot."),
        "comando":    "/comandos/capturar/dispenser",
        "simulador":  "vision",
        "evento":     "leitura_dispenser_falha",
        "bloqueante": False,
    },
    TIPO_DIVERGENCIA_MESA: {
        "rotulo":     "Divergência de contagem na câmera da balança",
        "efeito":     ("A câmera da mesa conta um a menos do que o esperado. "
                       "É UMA fonte do Triple Check divergindo — com o limiar "
                       "padrão de 1, já trava a OS."),
        "comando":    "/comandos/capturar/mesa",
        "simulador":  "vision",
        "evento":     "leitura_mesa_divergencia",
        "bloqueante": True,
    },
    TIPO_DIVERGENCIA_PESO: {
        "rotulo":     "Divergência de peso na balança",
        "efeito":     ("O HX711 mede fora da tolerância. Também é uma fonte do "
                       "Triple Check divergindo e, com o limiar padrão, trava."),
        "comando":    "/comandos/pesar",
        "simulador":  "weight",
        "evento":     "peso_divergencia",
        "bloqueante": True,
    },
    TIPO_FALHA_MECANICA: {
        "rotulo":     "Falha mecânica no dispenser (uma unidade a menos)",
        "efeito":     ("O dispenser solta UMA unidade a menos que o alvo. É o "
                       "caso mais completo de demonstrar: a balança mede o "
                       "déficit sozinha (1 em 15 já passa da tolerância de 5%) "
                       "e a trava nasce de uma falha física de verdade, não de "
                       "um sensor mentindo."),
        "comando":    "/comandos/dispensar",
        "simulador":  "dispenser",
        "evento":     "dispensado (quantidade_dispensada < quantidade_alvo)",
        "bloqueante": True,
    },
}

# Quais tipos cada comando pode carregar. Deriva de `TIPOS` — escrever à mão
# seria a segunda lista a manter em sincronia.
TIPOS_POR_COMANDO: dict[str, tuple[str, ...]] = {
    comando: tuple(t for t, meta in TIPOS.items() if meta["comando"] == comando)
    for comando in {meta["comando"] for meta in TIPOS.values()}
}


# ── Gatilho armado ────────────────────────────────────────────────────────────

_armada: dict | None = None
_lock = threading.Lock()


def catalogo() -> list[dict]:
    """Os tipos que dá para armar, para o console montar o seletor."""
    return [{"tipo": tipo, **meta} for tipo, meta in TIPOS.items()]


def armada() -> dict | None:
    """O gatilho em vigor, ou `None`. Cópia — o chamador publica no `_estado`."""
    with _lock:
        return dict(_armada) if _armada else None


def armar(tipo: str, slot_id: int) -> dict:
    """Arma o gatilho. Substitui o anterior, se houver.

    Substituir, e não recusar: o operador que erra o slot quer corrigir, não
    ler "já existe uma falha armada" e ter que desarmar antes. Só há UM gatilho
    de cada vez de propósito — dois armados ao mesmo tempo tornariam a próxima
    trava ambígua justamente na hora de explicar o que a causou.
    """
    global _armada

    if tipo not in TIPOS:
        raise InjecaoInvalida(
            f"Tipo de falha desconhecido: {tipo!r}. "
            f"Válidos: {', '.join(sorted(TIPOS))}."
        )
    try:
        slot = int(slot_id)
    except (TypeError, ValueError):
        raise InjecaoInvalida(f"slot_id inválido: {slot_id!r}.") from None
    if not 1 <= slot <= settings.NUM_SLOTS:
        raise InjecaoInvalida(
            f"slot_id deve ser 1-{settings.NUM_SLOTS} (recebido {slot})."
        )

    gatilho = {
        "tipo":       tipo,
        "slot_id":    slot,
        "rotulo":     TIPOS[tipo]["rotulo"],
        "bloqueante": TIPOS[tipo]["bloqueante"],
        "armada_em":  time.time(),
    }
    with _lock:
        _armada = gatilho

    logger.warning(
        "[INJECAO] ARMADA — %s no slot D%d. Será injetada na próxima "
        "ocorrência aplicável e se desarma sozinha.", tipo, slot,
    )
    return dict(gatilho)


def desarmar() -> dict | None:
    """Desarma sem consumir. Devolve o que estava armado, ou `None`."""
    global _armada
    with _lock:
        anterior, _armada = _armada, None
    if anterior:
        logger.warning("[INJECAO] DESARMADA — %s no slot D%d não será injetada.",
                       anterior["tipo"], anterior["slot_id"])
    return anterior


def consumir(slot_id: int, comando: str) -> str | None:
    """Check-and-pop: devolve o tipo a injetar NESTE comando, ou `None`.

    Sob lock, e num passo só: é isso que faz o one-shot valer mesmo com os
    comandos dos oito slots saindo em `gather`. Duas verificações separadas
    (ler, decidir, apagar) deixariam dois slots lerem o mesmo gatilho armado e
    a falha sairia em dois lugares — justamente o tipo de surpresa que esta
    feature existe para eliminar.

    `comando` é a rota do adapter, e não o tipo: quem chama é `cmd_pesar`, que
    sabe qual comando está montando e não deveria precisar saber quais tipos de
    falha cabem nele. A tabela está em `TIPOS_POR_COMANDO`, derivada de `TIPOS`.
    """
    global _armada
    aceitos = TIPOS_POR_COMANDO.get(comando, ())
    if not aceitos:
        return None

    with _lock:
        if not _armada:
            return None
        if _armada["slot_id"] != slot_id or _armada["tipo"] not in aceitos:
            return None
        tipo = _armada["tipo"]
        _armada = None

    logger.warning(
        "[INJECAO] ⚡ INJETADA — %s no slot D%d, via %s. Gatilho desarmado. "
        "ESTA FALHA FOI PROVOCADA, não é falha real do simulador.",
        tipo, slot_id, comando,
    )
    return tipo


def resetar() -> None:
    """Só para teste: devolve o gatilho ao estado de boot."""
    global _armada
    with _lock:
        _armada = None
