"""
APSEN — OS personalizada: a ordem montada à mão no console.

O operador escolhe, por linha, **medicamento, dispenser e quantidade**. A OS
entra na mesma fila e no mesmo caminho das dez ordens padrão; o que muda é que
o slot é do operador (`orchestrator.atribuir_slots_fixos` o honra) e que a mesa
não recebe letra de receita — o `mover` vai com `receita` vazia e a placa acha
a parada pelo `dispenser_alvo`, como sempre achou.

Este módulo é puro
──────────────────
Sem FastAPI, sem `database`, sem HTTP: recebe os itens e o catálogo e devolve
a lista de problemas ou o corpo do `POST /api/v1/ordens`. Mesma separação de
`necessidades.py` e `prevoo.py`, e pelo mesmo motivo — dá para testar a regra
inteira sem subir nada.

Quem chama `validar` é `receber_ordem`, e não só o console: é a porta única de
entrada de OS (CLAUDE.md, "O disparo manual É o `POST /api/v1/ordens`"). Uma
validação só no console deixaria um POST à mão escolher dispenser repetido.
"""
from datetime import datetime, timezone

import os_templates

# `template_id` que marca a OS como personalizada. Nenhuma ordem padrão pode
# usá-lo — `tests/test_os_personalizada.py` cobra.
TEMPLATE_ID = "PERS"
DESCRICAO   = "Ordem personalizada"
CATEGORIA   = "personalizada"

# No máximo 8 medicamentos diferentes (e nunca mais linhas que dispensers).
MAX_ITENS_PERS = 8
# 1 é o default desta feature; as ordens padrão usam 2 (`os_templates.QTD_MIN`).
QTD_MIN_PERS   = 1
QTD_MAX_PERS   = 10


def _inteiro(valor) -> bool:
    """`True` não é 1 aqui: um checkbox serializado não pode virar quantidade."""
    return isinstance(valor, int) and not isinstance(valor, bool)


def teto_de_itens(num_slots: int) -> int:
    return min(MAX_ITENS_PERS, num_slots)


def validar(itens, catalogo, num_slots: int) -> list[str]:
    """Problemas da OS personalizada, em pt-BR e por linha. Vazia = válida.

    `catalogo` é qualquer coisa que responda `in` por nome do medicamento (o
    `{nome: medicamento}` do central). `None` é catálogo indisponível — quem
    chama recusa antes com 503; aqui ele vale como vazio, e todo medicamento
    sai "fora do catálogo", que é a verdade do que dá para afirmar.
    """
    if not isinstance(itens, list):
        return ["a OS personalizada precisa de uma lista de itens."]

    teto = teto_de_itens(num_slots)
    problemas: list[str] = []
    if not 1 <= len(itens) <= teto:
        problemas.append(f"{len(itens)} linha(s) — a faixa é 1..{teto}.")

    catalogo = catalogo or {}
    linha_do_dispenser: dict[int, int] = {}
    linha_do_medicamento: dict[str, int] = {}

    for n, item in enumerate(itens, start=1):
        if not isinstance(item, dict):
            problemas.append(f"linha {n}: item malformado.")
            continue

        disp = item.get("dispenser_id")
        if not _inteiro(disp) or not 1 <= disp <= num_slots:
            problemas.append(f"linha {n}: dispenser {disp!r} fora da faixa D1..D{num_slots}.")
        elif disp in linha_do_dispenser:
            problemas.append(f"linha {n}: D{disp} já usado na linha "
                             f"{linha_do_dispenser[disp]}.")
        else:
            linha_do_dispenser[disp] = n

        qtd = item.get("quantidade")
        if not _inteiro(qtd) or not QTD_MIN_PERS <= qtd <= QTD_MAX_PERS:
            problemas.append(f"linha {n}: quantidade {qtd!r} fora da faixa "
                             f"{QTD_MIN_PERS}..{QTD_MAX_PERS}.")

        med = item.get("medicamento")
        if not isinstance(med, str) or not med.strip():
            problemas.append(f"linha {n}: sem medicamento.")
            continue
        if med in linha_do_medicamento:
            problemas.append(f"linha {n}: '{med}' já está na linha "
                             f"{linha_do_medicamento[med]}.")
        else:
            linha_do_medicamento[med] = n
        if med not in catalogo:
            problemas.append(f"linha {n}: '{med}' não existe no catálogo de medicamentos.")

    return problemas


def montar_corpo(itens, catalogo, agora: datetime | None = None,
                 sufixo: str | None = None) -> dict:
    """Corpo de `POST /api/v1/ordens` para a OS personalizada.

    `sku` e `categoria` saem do catálogo, nunca do cliente — o mesmo motivo de
    `os_templates.instanciar`: é o SKU que a câmera do dispenser compara. O
    `os_id` sai de `os_templates.novo_os_id`, e não de uma terceira grafia do
    formato.
    """
    catalogo = catalogo or {}
    momento = agora or datetime.now(timezone.utc)
    return {
        "os_id":       os_templates.novo_os_id(TEMPLATE_ID, momento, sufixo),
        "template_id": TEMPLATE_ID,
        "descricao":   DESCRICAO,
        "categoria":   CATEGORIA,
        "medicamentos": [
            {
                "medicamento":  item["medicamento"],
                "sku":          (catalogo.get(item["medicamento"]) or {}).get("sku", ""),
                "categoria":    (catalogo.get(item["medicamento"]) or {}).get("categoria", ""),
                "quantidade":   item["quantidade"],
                "dispenser_id": item["dispenser_id"],
            }
            for item in itens
        ],
        "criado_em":   momento.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "origem":      "OS_PERSONALIZADA_v1.0",
    }
