"""Cena sintetica de uma pilha de caixas dentro de um dispenser."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(RAIZ / "tests"))

from configuracao import Zona  # noqa: E402
from embalagens import face_embalagem  # noqa: E402

ALTURA, LARGURA = 720, 320


def cena_pilha(
    quantidade: int,
    altura_caixa: int = 90,
    ruido: float = 3.0,
    semente: int = 0,
    sku: str = "MED-001",
    nome: str = "Dipirona 500mg",
):
    """Coluna do dispenser com N caixas empilhadas a partir do fundo."""
    frame = np.full((ALTURA, LARGURA, 3), 70, np.uint8)
    cv2.rectangle(frame, (30, 60), (LARGURA - 30, ALTURA - 40), (105, 105, 105), -1)
    zona = Zona(1, 30, 60, LARGURA - 60, ALTURA - 100)

    face = face_embalagem(nome, sku, indice=0)
    caixa = cv2.resize(face, (zona.largura - 16, altura_caixa), interpolation=cv2.INTER_AREA)

    y = zona.y2 - 8
    for _ in range(quantidade):
        y -= altura_caixa
        if y < zona.y:
            break
        frame[y:y + altura_caixa, zona.x + 8:zona.x + 8 + caixa.shape[1]] = caixa

    rng = np.random.RandomState(semente)
    frame = np.clip(frame.astype(np.float32) + rng.normal(0, ruido, frame.shape),
                    0, 255).astype(np.uint8)
    return frame, zona
