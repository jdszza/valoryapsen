"""Cascata de pre-processamento para camera ruim.

A ideia: em vez de tentar decodificar uma unica versao da imagem, gerar algumas
versoes tratadas e parar na primeira que funcionar. Cada variante ataca um
defeito diferente:

    direto           imagem boa, custo zero
    x2 + adaptativo  desfoque e codigo pequeno  (a variante que mais recupera)
    unsharp          desfoque leve
    x2 + clahe       luz baixa / contraste ruim
    x3 + adaptativo  codigo muito pequeno na imagem
    bilateral + ...  ruido de sensor barato

Medido no simulador de degradacao (tests/benchmark_robustez.py), a cascata leva
cenarios como "desfoque forte" e "codigo com 55 px" de 0% para 100% de leitura.
"""

from __future__ import annotations

from typing import Callable, Iterator

import cv2
import numpy as np

_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


# --------------------------------------------------------------------------- #
# blocos basicos
# --------------------------------------------------------------------------- #
def cinza(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def ampliar(g: np.ndarray, fator: float) -> np.ndarray:
    if fator == 1.0:
        return g
    return cv2.resize(g, None, fx=fator, fy=fator, interpolation=cv2.INTER_CUBIC)


def realcar(g: np.ndarray, sigma: float = 1.5, peso: float = 1.6) -> np.ndarray:
    """Unsharp mask: devolve nitidez a bordas borradas."""
    return cv2.addWeighted(g, 1 + peso, cv2.GaussianBlur(g, (0, 0), sigma), -peso, 0)


def equalizar(g: np.ndarray) -> np.ndarray:
    """CLAHE: equaliza contraste por regiao, sem estourar o resto da imagem."""
    return _clahe.apply(g)


def binarizar_adaptativo(g: np.ndarray, bloco: int = 51, c: int = 7) -> np.ndarray:
    bloco = max(3, bloco | 1)
    return cv2.adaptiveThreshold(
        g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, bloco, c
    )


def binarizar_otsu(g: np.ndarray) -> np.ndarray:
    return cv2.threshold(
        cv2.GaussianBlur(g, (3, 3), 0), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )[1]


def suavizar_preservando_bordas(g: np.ndarray) -> np.ndarray:
    return cv2.bilateralFilter(g, 7, 60, 60)


def corrigir_gama(g: np.ndarray, gama: float = 0.5) -> np.ndarray:
    tabela = np.array([((i / 255.0) ** gama) * 255 for i in range(256)], dtype=np.uint8)
    return cv2.LUT(g, tabela)


# --------------------------------------------------------------------------- #
# cascata
# --------------------------------------------------------------------------- #
Variante = tuple[str, Callable[[np.ndarray], np.ndarray]]

# Ordem importa: da mais barata/mais provavel para a mais cara.
CASCATA_RAPIDA: list[Variante] = [
    ("direto", lambda g: g),
    ("x2+adaptativo", lambda g: binarizar_adaptativo(ampliar(g, 2))),
    ("realce", lambda g: realcar(g)),
]

CASCATA_COMPLETA: list[Variante] = CASCATA_RAPIDA + [
    ("x2+clahe", lambda g: equalizar(ampliar(g, 2))),
    ("x3+adaptativo", lambda g: binarizar_adaptativo(ampliar(g, 3), 81, 9)),
    ("x2+realce+adaptativo", lambda g: binarizar_adaptativo(realcar(ampliar(g, 2)))),
    ("bilateral+x2+adaptativo",
     lambda g: binarizar_adaptativo(ampliar(suavizar_preservando_bordas(g), 2), 51, 5)),
    ("gama+x2+adaptativo", lambda g: binarizar_adaptativo(ampliar(corrigir_gama(g), 2))),
    ("clahe+realce+x2", lambda g: ampliar(realcar(equalizar(g)), 2)),
    ("otsu", lambda g: binarizar_otsu(g)),
]


def variantes(
    g: np.ndarray,
    completa: bool = True,
    preferida: str | None = None,
) -> Iterator[tuple[str, np.ndarray, float]]:
    """Gera (nome, imagem, fator_de_escala) na ordem de tentativa.

    `preferida` e a variante que funcionou da ultima vez naquela zona: ela vai
    para a frente da fila, o que corta o custo medio drasticamente quando as
    condicoes de iluminacao sao estaveis.
    """
    lista = CASCATA_COMPLETA if completa else CASCATA_RAPIDA
    if preferida:
        lista = sorted(lista, key=lambda v: v[0] != preferida)

    for nome, funcao in lista:
        try:
            saida = funcao(g)
        except cv2.error:
            continue
        escala = saida.shape[1] / g.shape[1] if g.shape[1] else 1.0
        yield nome, saida, escala


# --------------------------------------------------------------------------- #
# metricas de qualidade (usadas pelo diagnostico)
# --------------------------------------------------------------------------- #
def nitidez(g: np.ndarray) -> float:
    """Variancia do Laplaciano. Abaixo de ~60 a imagem esta fora de foco."""
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def brilho(g: np.ndarray) -> float:
    return float(g.mean())


def contraste(g: np.ndarray) -> float:
    return float(g.std())


def percentual_estourado(g: np.ndarray) -> float:
    """% de pixels saturados em branco — indica reflexo especular."""
    return float((g >= 250).mean() * 100.0)
