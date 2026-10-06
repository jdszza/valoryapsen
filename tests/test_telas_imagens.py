# -*- coding: utf-8 -*-
"""As imagens das telas TFT: nome do medicamento -> número da imagem.

A tela entende NÚMERO (o `imgN` de `dispenser/telas_tft/imagens.h`) e o comando
`slot` traz o NOME do catálogo do central. A tradução mora em
`catalogo_imagens.h`, ao lado das imagens, e são três coisas que precisam
concordar sem que nenhuma delas dê erro quando discorda:

  * a lista de nomes e as imagens — uma posição a mais ou a menos desloca
    todas as caixas seguintes, e a tela do slot mostra o medicamento VIZINHO
    na lista: o quadro exato de um medicamento trocado;
  * a lista e a tabela de ponteiros do sketch — o `static_assert` cobra só a
    CONTAGEM, na compilação, e a compilação não roda aqui;
  * a lista e o catálogo do central — nome grafado diferente nunca casa, e a
    tela cai no modo texto calada.

Lê C++ por texto, como `test_dispenser_firmware.py`, e pelo mesmo motivo cada
extrator tem piso: um regex que para de casar deixaria tudo verde para sempre.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PASTA = RAIZ / "dispenser" / "telas_tft"

PISO_IMAGENS = 30          # hoje são 39; menos que isto é extrator quebrado


def _normalizar(nome: str) -> str:
    """A MESMA regra de `normalizarNome` no sketch: maiúsculas, sem espaço nas
    pontas, espaços repetidos viram um. Nada além disso."""
    return " ".join(nome.split()).upper()


def _nomes_do_catalogo_de_imagens() -> list[str]:
    texto = (PASTA / "catalogo_imagens.h").read_text(encoding="utf-8")
    bloco = texto[texto.index("NOMES_IMAGEM[] = {"):]
    bloco = bloco[: bloco.index("};")]
    nomes = re.findall(r'^\s*"([^"]+)",', bloco, re.M)
    assert len(nomes) >= PISO_IMAGENS, f"extrator de nomes achou só {len(nomes)}"
    return nomes


def _imagens_declaradas() -> list[tuple[int, int]]:
    """(número, tamanho) de cada `const uint16_t imgN[tamanho]`, na ordem do arquivo.

    O arquivo tem ~6 MB de pixels: lê linha a linha e só olha as declarações.
    """
    achadas = []
    padrao = re.compile(r"^const uint16_t img(\d+)\[(\d+)\]")
    with open(PASTA / "imagens.h", encoding="utf-8") as f:
        for linha in f:
            if linha.startswith("const"):
                m = padrao.match(linha)
                if m:
                    achadas.append((int(m.group(1)), int(m.group(2))))
    assert len(achadas) >= PISO_IMAGENS, f"extrator de imagens achou só {len(achadas)}"
    return achadas


def _ponteiros_do_sketch() -> list[str]:
    texto = (PASTA / "telas_tft.ino").read_text(encoding="utf-8")
    bloco = texto[texto.index("IMAGENS[] = {"):]
    bloco = bloco[: bloco.index("};")]
    ponteiros = re.findall(r"\bimg\d+\b", bloco)
    assert len(ponteiros) >= PISO_IMAGENS, f"extrator de ponteiros achou só {len(ponteiros)}"
    return ponteiros


def _nomes_do_central() -> set[str]:
    arvore = ast.parse((RAIZ / "central-computer" / "database.py").read_text(encoding="utf-8"))
    for no in ast.walk(arvore):
        if (isinstance(no, ast.Assign)
                and any(getattr(t, "id", None) == "_MEDICAMENTOS_SEED" for t in no.targets)):
            return {linha[0] for linha in ast.literal_eval(no.value)}
    raise AssertionError("_MEDICAMENTOS_SEED sumiu de central-computer/database.py")


def test_toda_imagem_tem_nome_e_todo_nome_tem_imagem():
    nomes = _nomes_do_catalogo_de_imagens()
    imagens = _imagens_declaradas()
    assert [n for n, _ in imagens] == list(range(len(imagens))), (
        "imagens.h precisa declarar img0..imgN-1 em ordem e sem buraco — a "
        "posição É o número que a tela desenha")
    assert len(nomes) == len(imagens), (
        f"{len(nomes)} nomes em catalogo_imagens.h para {len(imagens)} imagens em "
        f"imagens.h: daqui em diante toda tela mostraria a caixa do vizinho")


def test_toda_imagem_e_uma_tela_inteira():
    """128 x 160 em RGB565, retrato. Imagem de outro tamanho seria desenhada
    lendo memória além do array."""
    tamanhos = {tam for _, tam in _imagens_declaradas()}
    assert tamanhos == {128 * 160}, tamanhos


def test_o_sketch_aponta_para_as_imagens_na_mesma_ordem():
    imagens = _imagens_declaradas()
    assert _ponteiros_do_sketch() == [f"img{n}" for n, _ in imagens]


def test_todo_nome_existe_no_catalogo_do_central():
    """Grafado igual ao `medicamentos.nome`: é ele que chega no `slot`."""
    central = {_normalizar(n) for n in _nomes_do_central()}
    fora = [n for n in _nomes_do_catalogo_de_imagens() if _normalizar(n) not in central]
    assert not fora, f"nomes sem par no catálogo do central (nunca acharão imagem): {fora}"


def test_nenhum_nome_repetido_depois_de_normalizar():
    """Dois nomes que normalizam igual: o segundo nunca seria desenhado, e a
    tela mostraria a primeira caixa para os dois."""
    nomes = [_normalizar(n) for n in _nomes_do_catalogo_de_imagens()]
    repetidos = sorted({n for n in nomes if nomes.count(n) > 1})
    assert not repetidos, repetidos


def test_a_ordem_e_a_da_bancada():
    """Âncoras da lista entregue com as imagens (numeração da lista começa em
    1, a do código em 0). Se alguém reordenar só um dos dois arquivos, os
    testes acima pegam; este pega a reordenação dos DOIS juntos."""
    nomes = _nomes_do_catalogo_de_imagens()
    assert nomes[0] == "ALOIS 10MG"
    assert nomes[5] == "DESOL"
    assert nomes[16] == "LACTOSIL 10000 COMP"
    assert nomes[38] == "ZANIDIP 10MG"
