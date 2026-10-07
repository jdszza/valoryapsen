"""Gera uma cena sintetica com 4 dispensers em linha, para testar sem hardware.

Uso direto:
    python tests/cena_sintetica.py                 # cena correta -> cena_ok.png
    python tests/cena_sintetica.py --trocar 1 3    # troca o conteudo dos dispensers 1 e 3
    python tests/cena_sintetica.py --video cena.mp4 --frames 90
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
import qrcode
from qrcode.constants import ERROR_CORRECT_H

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from configuracao import Catalogo, MapaZonas, Zona  # noqa: E402
from gerar_qrcodes import gerar_etiqueta  # noqa: E402

LARGURA, ALTURA = 1280, 720


def imagem_qr(texto: str, lado: int) -> np.ndarray:
    """QR puro, sem legenda (usado quando nao se quer a etiqueta completa)."""
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(texto)
    qr.make(fit=True)
    pil = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    arr = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    return cv2.resize(arr, (lado, lado), interpolation=cv2.INTER_NEAREST)


_cache_etiquetas: dict = {}


def imagem_etiqueta(med, lado_qr: int, com_aruco: bool = True) -> np.ndarray:
    """Etiqueta real (QR + ArUco + legenda), como sai da impressora."""
    chave = (med.qr, lado_qr, com_aruco)
    if chave not in _cache_etiquetas:
        pil = gerar_etiqueta(
            med.qr, med.nome, med.dispenser, 30, med.aruco if com_aruco else None
        )
        arr = cv2.cvtColor(np.array(pil.convert("RGB")), cv2.COLOR_RGB2BGR)
        # a etiqueta e redimensionada pela LARGURA DO QR, preservando proporcao
        fator = lado_qr / (arr.shape[0] * 0.72 if com_aruco else arr.shape[1])
        novo = (max(8, int(arr.shape[1] * fator)), max(8, int(arr.shape[0] * fator)))
        _cache_etiquetas[chave] = cv2.resize(arr, novo, interpolation=cv2.INTER_AREA)
    return _cache_etiquetas[chave]


def zonas_padrao(quantidade: int = 4) -> MapaZonas:
    margem_x, margem_y = 60, 130
    vao = 24
    w = (LARGURA - 2 * margem_x - vao * (quantidade - 1)) // quantidade
    h = ALTURA - margem_y - 90
    zonas = [
        Zona(dispenser=i + 1, x=margem_x + i * (w + vao), y=margem_y, largura=w, altura=h)
        for i in range(quantidade)
    ]
    return MapaZonas(zonas=zonas, resolucao=(LARGURA, ALTURA))


def montar_cena(
    catalogo: Catalogo,
    mapa: MapaZonas,
    conteudo_por_dispenser: dict[int, str] | None = None,
    deslocamento: tuple[int, int] = (0, 0),
    ruido: float = 0.0,
    lado_qr: int = 150,
    etiqueta_completa: bool = False,
) -> np.ndarray:
    """Desenha a bancada: 4 aberturas com um QR visivel em cada uma."""
    frame = np.full((ALTURA, LARGURA, 3), 38, dtype=np.uint8)
    cv2.rectangle(frame, (0, 0), (LARGURA, 90), (58, 58, 58), -1)
    cv2.putText(frame, "BANCADA DE DISPENSERS (cena sintetica)", (40, 56),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (210, 210, 210), 2, cv2.LINE_AA)

    if conteudo_por_dispenser is None:
        conteudo_por_dispenser = {
            m.dispenser: m.qr for m in catalogo.por_qr.values()
        }

    dx, dy = deslocamento
    for zona in mapa.zonas:
        # corpo do dispenser
        cv2.rectangle(frame, (zona.x, zona.y), (zona.x2, zona.y2), (95, 95, 95), -1)
        cv2.rectangle(frame, (zona.x, zona.y), (zona.x2, zona.y2), (140, 140, 140), 2)
        # brecha (janela) voltada para a camera
        bx = zona.x + zona.largura // 2 - lado_qr // 2
        by = zona.y + zona.altura // 2 - lado_qr // 2
        cv2.rectangle(frame, (bx - 18, by - 18), (bx + lado_qr + 18, by + lado_qr + 18),
                      (25, 25, 25), -1)

        conteudo = conteudo_por_dispenser.get(zona.dispenser)
        if not conteudo:
            continue

        if etiqueta_completa:
            med = catalogo.buscar(conteudo)
            arte = imagem_etiqueta(med, lado_qr) if med else imagem_qr(conteudo, lado_qr)
        else:
            arte = imagem_qr(conteudo, lado_qr)

        ah, aw = arte.shape[:2]
        x = bx + dx - (aw - lado_qr) // 2
        y = by + dy - (ah - lado_qr) // 2
        x = max(0, min(x, LARGURA - aw))
        y = max(0, min(y, ALTURA - ah))
        frame[y : y + ah, x : x + aw] = arte

    if ruido > 0:
        barulho = np.random.normal(0, ruido * 255, frame.shape).astype(np.int16)
        frame = np.clip(frame.astype(np.int16) + barulho, 0, 255).astype(np.uint8)

    return frame


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--trocar", nargs=2, type=int, default=None,
                   help="troca o conteudo de dois dispensers (ex: --trocar 1 3)")
    p.add_argument("--saida", type=str, default=str(RAIZ / "tests" / "cena.png"))
    p.add_argument("--video", type=str, default=None)
    p.add_argument("--frames", type=int, default=60)
    p.add_argument("--salvar-zonas", action="store_true",
                   help="grava as zonas desta cena em config/zonas.json")
    args = p.parse_args()

    catalogo = Catalogo.carregar()
    mapa = zonas_padrao(4)
    if args.salvar_zonas:
        mapa.salvar()
        print("Zonas da cena sintetica salvas em config/zonas.json")

    conteudo = {m.dispenser: m.qr for m in catalogo.por_qr.values()}
    if args.trocar:
        a, b = args.trocar
        conteudo[a], conteudo[b] = conteudo[b], conteudo[a]
        print(f"Conteudo dos dispensers {a} e {b} trocado (deve gerar ERRO_POSICAO).")

    if args.video:
        vw = cv2.VideoWriter(args.video, cv2.VideoWriter_fourcc(*"mp4v"), 20.0,
                             (LARGURA, ALTURA))
        for i in range(args.frames):
            atual = conteudo if i > args.frames // 3 else {
                m.dispenser: m.qr for m in catalogo.por_qr.values()
            }
            vw.write(montar_cena(catalogo, mapa, atual,
                                 deslocamento=(int(3 * np.sin(i / 5)), 0), ruido=0.01))
        vw.release()
        print(f"Video de teste: {args.video}")
    else:
        cv2.imwrite(args.saida, montar_cena(catalogo, mapa, conteudo))
        print(f"Cena salva em {args.saida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
