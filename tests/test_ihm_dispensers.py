"""O botão 🧹 Limpar da IHM: os ids do render têm que casar com o Input do callback.

O botão é a única forma de o operador descartar estoque encalhado num slot, e
já esteve inoperante por três motivos independentes (ver AUDITORIA.md 2, 3 e 4):
o simulador recusava limpar slot `concluido`, a telemetria reescrevia o status
de volta e o central bloqueava por `os_id` residual. Esses três já têm teste em
`test_dispenser_simulator.py` e `test_central_eventos.py`.

Falta o quarto modo de falha, que nenhum deles pega: o clique não chegar ao
callback porque o id do botão renderizado não casa com o `Input` registrado.
Divergência de id não levanta erro em lugar nenhum — o Dash simplesmente não
dispara o callback, e o botão fica mudo. Por isso o teste lê o padrão que o
callback REALMENTE registrou no Dash, em vez de repetir o literal aqui: um
teste que repete o id não detecta os dois lados mudarem juntos para o valor
errado.
"""
import json

import pytest

from conftest import NUM_SLOTS

SAIDA_LIMPAR = "msg-limpar-disp.children"


@pytest.fixture
def ihm(carregar_ihm):
    return carregar_ihm(env={"BACKEND_URL": "http://central-computer:8000"})


def _percorrer(no):
    """Componentes da árvore, em profundidade."""
    yield no
    filhos = getattr(no, "children", None)
    if isinstance(filhos, (list, tuple)):
        for filho in filhos:
            yield from _percorrer(filho)
    elif filhos is not None:
        yield from _percorrer(filhos)


def _tipo_registrado_no_callback():
    """O `type` que o Input do callback de limpeza espera, lido do Dash."""
    from dash._callback import GLOBAL_CALLBACK_MAP

    entrada = GLOBAL_CALLBACK_MAP[SAIDA_LIMPAR]["inputs"][0]["id"]
    return json.loads(entrada)["type"]


def _dispensers_falsos(n=NUM_SLOTS):
    return [
        {"dispenser_id": i, "medicamento": f"MED {i}", "quantidade_atual": 4,
         "categoria": "analgesicos", "capacidade": 100, "ultima_os_id": "OS-1"}
        for i in range(1, n + 1)
    ]


def test_todo_card_renderiza_o_botao_que_o_callback_escuta(ihm):
    """Id divergente = clique que não chega ao callback, sem erro nenhum."""
    ihm.requests.payload = _dispensers_falsos()
    tipo = _tipo_registrado_no_callback()

    pagina = ihm.modulo._render_dispensers("jwt")
    ids = [getattr(c, "id", None) for c in _percorrer(pagina)]

    for d_id in range(1, NUM_SLOTS + 1):
        assert {"type": tipo, "index": d_id} in ids, (
            f"D{d_id} não renderizou botão com o id que o callback escuta ({tipo})"
        )


def test_o_destino_da_mensagem_existe_na_pagina(ihm):
    """O Output do callback precisa de alvo, senão o retorno some."""
    ihm.requests.payload = _dispensers_falsos(1)

    pagina = ihm.modulo._render_dispensers("jwt")
    ids = [getattr(c, "id", None) for c in _percorrer(pagina)]

    assert SAIDA_LIMPAR.split(".")[0] in ids


def test_backend_sem_resposta_nao_quebra_a_aba(ihm):
    """Central fora do ar: a aba renderiza vazia em vez de levantar."""
    ihm.requests.payload = {"detail": "indisponível"}

    pagina = ihm.modulo._render_dispensers("jwt")

    ids = [getattr(c, "id", None) for c in _percorrer(pagina)]
    assert not any(isinstance(i, dict) and i.get("type") == "btn-limpar-disp" for i in ids)
