# -*- coding: utf-8 -*-
"""Os quatro números do relógio do ciclo, lidos como a planta os lê.

O ciclo `mover` → `dispensar` não espera confirmação: o central calcula QUANDO
cada peça acontece e dispara no relógio. Estes quatro números são esse relógio,
e eram os únicos de `config.py` lidos com `float(os.getenv(...))` cru — todos os
outros já passavam por um leitor com faixa, warning e queda no default. O
comentário ao lado deles diz, com todas as letras, que um cronograma errado é a
única forma de esta feature derrubar comprimido no chão.

Dois modos de falhar, os dois silenciosos do jeito errado:

  * **texto não numérico** — `2,5` com vírgula, que é como se digita em pt-BR,
    ou a variável vazia. O `float()` levantava `ValueError` na avaliação do
    `dataclass`, ou seja, no import: o central não subia e o traceback apontava
    para `config.py`, não para o `.env` de quem digitou;
  * **zero ou negativo** — `cronograma_do_ciclo` devolvia prazo ≤ 0 e
    `_dormir_ou_cancelar` retornava NA HORA dizendo que o prazo foi cumprido.
    O `dispensar` saía com a mesa ainda andando.

O fixture do orquestrador zera os quatro escrevendo direto no `settings`, e não
pelo ambiente, justamente porque o ambiente agora recusa zero (ver o comentário
de `CRONOGRAMA_INSTANTANEO` em `conftest.py`). Aqui é o contrário: lê-se pelo
ambiente, como a planta lê.
"""
from __future__ import annotations

import pytest

CAMPOS = (
    "CNC_TETO_TRAJETO_S",
    "CNC_MARGEM_CHEGADA_S",
    "DISPENSA_S_POR_UNIDADE",
    "DISPENSA_FOLGA_S",
)

# O que o `.env` de alguém pode conter e que antes não era tratado.
VALORES_RUINS = ["0", "-1", "2,5", "", "  ", "muito", "1e400"]


def _com_ambiente(carregar_orquestrador, valor: str):
    """Carrega o orquestrador com os QUATRO números vindos do ambiente."""
    return carregar_orquestrador(env={campo: valor for campo in CAMPOS})


@pytest.mark.parametrize("valor", VALORES_RUINS)
def test_valor_ruim_nao_derruba_o_import(carregar_orquestrador, valor):
    """`ValueError` na avaliação do dataclass é o central que não sobe, com o
    traceback apontando para `config.py` em vez de para o `.env`."""
    orq = _com_ambiente(carregar_orquestrador, valor)

    for campo in CAMPOS:
        assert getattr(orq.modulo.settings, campo) > 0


@pytest.mark.parametrize("valor", VALORES_RUINS)
@pytest.mark.parametrize("quantidade", [0, 1, 15])
def test_o_cronograma_nunca_devolve_prazo_nao_positivo(carregar_orquestrador,
                                                       valor, quantidade):
    """A invariante que importa, e ela é sobre o RESULTADO, não sobre a leitura.

    Prazo ≤ 0 faz `_dormir_ou_cancelar` acordar na hora dizendo que o prazo foi
    cumprido — `asyncio.wait(timeout=-1)` volta com `feitos` vazio, que é
    exatamente o sinal de "venceu" — e o `dispensar` sai com a mesa andando.
    """
    orq = _com_ambiente(carregar_orquestrador, valor)

    espera, dispensa = orq.modulo.cronograma_do_ciclo(quantidade)

    assert espera > 0
    assert dispensa > 0


def test_o_default_e_o_da_bancada(carregar_orquestrador):
    """Controle: cair no default não pode ser confundido com ler o valor.

    Sem isto, "o cronograma é positivo" passaria também numa implementação que
    ignorasse o ambiente por inteiro.
    """
    orq = carregar_orquestrador(env={campo: "" for campo in CAMPOS})

    assert orq.modulo.cronograma_do_ciclo(0) == (3.25, 1.0)


def test_valor_valido_do_ambiente_e_respeitado(carregar_orquestrador):
    """O outro controle: dentro da faixa, quem manda é o `.env`."""
    orq = carregar_orquestrador(env={
        "CNC_TETO_TRAJETO_S": "4", "CNC_MARGEM_CHEGADA_S": "1",
        "DISPENSA_S_POR_UNIDADE": "0.5", "DISPENSA_FOLGA_S": "2",
    })

    assert orq.modulo.cronograma_do_ciclo(10) == (5.0, 7.0)


@pytest.mark.parametrize("campo, acima_do_teto", [
    ("CNC_TETO_TRAJETO_S", "61"),
    ("CNC_MARGEM_CHEGADA_S", "61"),
    ("DISPENSA_S_POR_UNIDADE", "31"),
    ("DISPENSA_FOLGA_S", "31"),
])
def test_valor_absurdamente_alto_cai_no_default(carregar_orquestrador, campo,
                                                acima_do_teto):
    """O teto existe pelo motivo oposto ao piso: um número alto não derruba
    nada, só faz cada ciclo esperar minutos — e aí a demonstração parece
    travada sem que ninguém saiba por quê."""
    orq = carregar_orquestrador(env={campo: acima_do_teto})

    padroes = {"CNC_TETO_TRAJETO_S": 2.5, "CNC_MARGEM_CHEGADA_S": 0.75,
               "DISPENSA_S_POR_UNIDADE": 1.0, "DISPENSA_FOLGA_S": 1.0}
    assert getattr(orq.modulo.settings, campo) == padroes[campo]
