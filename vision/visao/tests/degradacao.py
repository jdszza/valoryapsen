"""Simulador de camera ruim: angulo, desfoque, luz baixa, reflexo, ruido e compressao.

Serve para medir objetivamente o ganho de cada melhoria de leitura, sem depender
de refazer o teste fisico toda vez.
"""

from __future__ import annotations

import cv2
import numpy as np


def warp_angulo(
    img: np.ndarray,
    graus_x: float = 0.0,
    graus_y: float = 0.0,
    distancia_relativa: float = 3.0,
) -> np.ndarray:
    """Projecao pinhole de um plano girado — perspectiva realista, nao um 'chute'.

    graus_y  : camera olhando o codigo de lado (giro em torno do eixo vertical)
    graus_x  : camera olhando de cima/baixo
    distancia_relativa : distancia da camera em multiplos do lado do codigo.
                         3.0 e uma camera relativamente proxima (perspectiva forte);
                         valores maiores achatam a distorcao.
    """
    h, w = img.shape[:2]
    if not graus_x and not graus_y:
        return img

    ax, ay = np.deg2rad(graus_x), np.deg2rad(graus_y)
    lado = float(max(w, h))
    d = distancia_relativa * lado
    f = d  # foco tal que o codigo ocupe aproximadamente o mesmo tamanho

    Rx = np.array([[1, 0, 0],
                   [0, np.cos(ax), -np.sin(ax)],
                   [0, np.sin(ax), np.cos(ax)]], dtype=np.float64)
    Ry = np.array([[np.cos(ay), 0, np.sin(ay)],
                   [0, 1, 0],
                   [-np.sin(ay), 0, np.cos(ay)]], dtype=np.float64)
    R = Ry @ Rx

    cantos = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    proj = []
    for x, y in cantos:
        p = R @ np.array([x - w / 2.0, y - h / 2.0, 0.0])
        p[2] += d
        proj.append([f * p[0] / p[2] + w / 2.0, f * p[1] / p[2] + h / 2.0])

    M = cv2.getPerspectiveTransform(cantos, np.float32(proj))
    return cv2.warpPerspective(
        img, M, (w, h), flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=(120, 120, 120),
    )


def desfoque(img: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return img
    k = int(sigma * 6) | 1
    return cv2.GaussianBlur(img, (k, k), sigma)


def luz(img: np.ndarray, ganho: float = 1.0, offset: int = 0) -> np.ndarray:
    """ganho<1 escurece, offset>0 lava a imagem (contraste baixo)."""
    return np.clip(img.astype(np.float32) * ganho + offset, 0, 255).astype(np.uint8)


def reflexo(img: np.ndarray, intensidade: float = 0.6, raio: float = 0.35) -> np.ndarray:
    """Mancha especular clara sobre parte do codigo — o defeito mais comum em blister."""
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w * 0.62, h * 0.38
    r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / (min(h, w) * raio)
    mancha = np.clip(1.0 - r, 0, 1) ** 2 * intensidade
    if img.ndim == 3:
        mancha = mancha[:, :, None]
    return np.clip(img.astype(np.float32) + mancha * 255, 0, 255).astype(np.uint8)


def ruido(img: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return img
    return np.clip(img.astype(np.float32) + np.random.normal(0, sigma, img.shape),
                   0, 255).astype(np.uint8)


def compressao(img: np.ndarray, qualidade: int = 40) -> np.ndarray:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, qualidade])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR) if ok else img


def cena_degradada(
    codigo: np.ndarray,
    lado_px: int = 90,
    graus: float = 0.0,
    sigma_desfoque: float = 0.0,
    ganho_luz: float = 1.0,
    offset_luz: int = 0,
    forca_reflexo: float = 0.0,
    sigma_ruido: float = 0.0,
    qualidade_jpeg: int = 100,
    fundo: int = 110,
    margem: int = 40,
    largura_referencia: int | None = None,
) -> np.ndarray:
    """Coloca o codigo num fundo cinza e aplica a cadeia de degradacoes.

    `lado_px` e o tamanho, em pixels do frame, da parte de referencia do codigo
    (por padrao a imagem inteira; numa etiqueta hibrida, a largura do QR).
    A proporcao original e preservada — nada de esticar a etiqueta.
    """
    ref = largura_referencia or codigo.shape[1]
    fator = lado_px / float(ref)
    novo = (max(8, int(round(codigo.shape[1] * fator))),
            max(8, int(round(codigo.shape[0] * fator))))
    alvo = cv2.resize(codigo, novo, interpolation=cv2.INTER_AREA)
    if alvo.ndim == 2:
        alvo = cv2.cvtColor(alvo, cv2.COLOR_GRAY2BGR)

    ah, aw = alvo.shape[:2]
    cena = np.full((ah + 2 * margem, aw + 2 * margem, 3), fundo, np.uint8)
    cena[margem : margem + ah, margem : margem + aw] = alvo

    if graus:
        cena = warp_angulo(cena, graus_y=graus)
    if forca_reflexo:
        cena = reflexo(cena, forca_reflexo)
    cena = desfoque(cena, sigma_desfoque)
    cena = luz(cena, ganho_luz, offset_luz)
    cena = ruido(cena, sigma_ruido)
    if qualidade_jpeg < 100:
        cena = compressao(cena, qualidade_jpeg)
    return cena


CENARIOS = [
    # nome,                     lado, graus, blur, ganho, offset, reflexo, ruido, jpeg
    ("ideal",                    120,   0,   0.0,  1.00,   0,     0.0,     0,   100),
    ("pequeno 70px",              70,   0,   0.6,  1.00,   0,     0.0,     3,    92),
    ("pequeno 55px",              55,   0,   0.6,  1.00,   0,     0.0,     3,    92),
    ("angulo 30",                120,  30,   0.8,  1.00,   0,     0.0,     4,    90),
    ("angulo 45",                120,  45,   0.8,  1.00,   0,     0.0,     4,    90),
    ("angulo 55",                120,  55,   0.8,  1.00,   0,     0.0,     4,    90),
    ("luz baixa",                110,   0,   1.0,  0.35,   0,     0.0,     8,    85),
    ("luz baixa + angulo 30",    100,  30,   1.2,  0.35,   0,     0.0,     8,    85),
    ("lavado (contraste baixo)", 110,   0,   1.0,  0.45,  95,     0.0,     6,    85),
    ("reflexo forte",            120,   0,   0.8,  1.00,   0,     0.75,    5,    88),
    ("desfoque forte",           120,   0,   2.2,  1.00,   0,     0.0,     5,    88),
    ("webcam ruim (tudo junto)",  80,  35,   1.6,  0.50,  40,     0.45,   10,    70),
]


def gerar_cenario(
    codigo: np.ndarray, cenario: tuple, largura_referencia: int | None = None
) -> np.ndarray:
    _, lado, graus, blur, ganho, off, refl, ruid, jpg = cenario
    return cena_degradada(
        codigo, lado_px=lado, graus=graus, sigma_desfoque=blur,
        ganho_luz=ganho, offset_luz=off, forca_reflexo=refl,
        sigma_ruido=ruid, qualidade_jpeg=jpg,
        largura_referencia=largura_referencia,
    )
