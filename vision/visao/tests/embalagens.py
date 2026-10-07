"""Gerador de faces de embalagem sinteticas, para validar o reconhecimento visual
sem precisar fotografar caixa de verdade.

Cada "medicamento" recebe uma arte distinta e deterministica: cor de fundo,
faixa, bloco de texto, marca e um padrao grafico. Nao substitui foto real, mas
exercita exatamente o que o reconhecedor usa — pontos-chave, textura e cor.
"""

from __future__ import annotations

import cv2
import numpy as np

LARGURA_CAIXA, ALTURA_CAIXA = 420, 300

PALETAS = [
    ((235, 240, 245), (40, 90, 210), (25, 40, 70)),
    ((240, 248, 238), (60, 165, 75), (20, 60, 30)),
    ((245, 238, 248), (150, 60, 190), (60, 20, 70)),
    ((238, 244, 250), (230, 145, 40), (70, 45, 15)),
    ((250, 240, 238), (60, 60, 215), (70, 20, 20)),
    ((238, 250, 250), (190, 160, 40), (60, 55, 15)),
]


def face_embalagem(
    nome: str,
    codigo: str,
    marca: str = "APSEN",
    indice: int = 0,
    largura: int = LARGURA_CAIXA,
    altura: int = ALTURA_CAIXA,
) -> np.ndarray:
    """Desenha a face frontal de uma caixa de medicamento."""
    fundo, destaque, tinta = PALETAS[indice % len(PALETAS)]
    img = np.full((altura, largura, 3), fundo, np.uint8)
    rng = np.random.RandomState(abs(hash(codigo)) % (2**31))

    # faixa superior da marca
    cv2.rectangle(img, (0, 0), (largura, int(altura * 0.17)), destaque, -1)
    cv2.putText(img, marca, (16, int(altura * 0.125)), cv2.FONT_HERSHEY_DUPLEX,
                0.95, (255, 255, 255), 2, cv2.LINE_AA)

    # nome do produto
    cv2.putText(img, nome.split()[0].upper(), (16, int(altura * 0.36)),
                cv2.FONT_HERSHEY_DUPLEX, 1.15, tinta, 2, cv2.LINE_AA)
    resto = " ".join(nome.split()[1:])
    if resto:
        cv2.putText(img, resto, (18, int(altura * 0.47)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, tinta, 2, cv2.LINE_AA)

    # linhas de texto miudo (bula/composicao) — fonte de pontos-chave, como no real
    y = int(altura * 0.58)
    for i in range(5):
        comprimento = int(largura * (0.45 + 0.4 * rng.rand()))
        cv2.line(img, (18, y), (18 + comprimento, y), tinta, 2)
        y += 11

    # padrao grafico distinto por produto
    modo = indice % 3
    cx, cy = int(largura * 0.80), int(altura * 0.62)
    if modo == 0:
        for r in range(12, 60, 12):
            cv2.circle(img, (cx, cy), r, destaque, 3)
    elif modo == 1:
        for k in range(4):
            deslocamento = k * 15
            cv2.rectangle(img, (cx - 55 + deslocamento, cy - 55 + deslocamento),
                          (cx + 55 - deslocamento, cy + 55 - deslocamento), destaque, 3)
    else:
        pts = np.array([[cx, cy - 58], [cx + 58, cy + 40], [cx - 58, cy + 40]], np.int32)
        cv2.polylines(img, [pts], True, destaque, 4)
        cv2.line(img, (cx - 30, cy), (cx + 30, cy), destaque, 3)

    # tarja e codigo impresso
    cv2.rectangle(img, (0, altura - int(altura * 0.14)), (largura, altura), destaque, -1)
    cv2.putText(img, codigo, (16, altura - int(altura * 0.045)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2, cv2.LINE_AA)

    cv2.rectangle(img, (0, 0), (largura - 1, altura - 1), (120, 120, 120), 2)
    return img


def variacao(
    face: np.ndarray,
    graus: float = 0.0,
    escala: float = 1.0,
    brilho: float = 1.0,
    desfoque: float = 0.0,
    ruido: float = 0.0,
    semente: int | None = None,
) -> np.ndarray:
    """Simula a mesma caixa vista de outro jeito (angulo, distancia, luz)."""
    from degradacao import warp_angulo  # reaproveita a projecao pinhole

    img = face
    if escala != 1.0:
        img = cv2.resize(img, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
    if graus:
        img = warp_angulo(img, graus_y=graus)
    if desfoque > 0:
        img = cv2.GaussianBlur(img, (0, 0), desfoque)
    if brilho != 1.0:
        img = np.clip(img.astype(np.float32) * brilho, 0, 255).astype(np.uint8)
    if ruido > 0:
        rng = np.random.RandomState(semente)
        img = np.clip(img.astype(np.float32) + rng.normal(0, ruido, img.shape),
                      0, 255).astype(np.uint8)
    return img


def catalogo_sintetico(medicamentos) -> dict[str, np.ndarray]:
    """Uma face por medicamento do catalogo real do projeto."""
    return {
        med.qr: face_embalagem(med.nome, med.qr, indice=i)
        for i, med in enumerate(medicamentos)
    }
