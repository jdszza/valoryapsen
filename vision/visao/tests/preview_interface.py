"""Renderiza a interface com dados fabricados, sem camera.

Serve para conferir o layout (corte de texto, sobreposicao, cartoes) sem
precisar de hardware. Gera um PNG e sai.
"""

import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from configuracao import Catalogo, Medicamento, MapaZonas, Zona  # noqa: E402
from contagem import NivelEstoque  # noqa: E402
from fusao import ResultadoFusao, Veredito  # noqa: E402
from interface import Medidas, PainelApsen, desenhar_zonas_no_video  # noqa: E402

LARGURA, ALTURA = 1280, 720


def cena(largura=960, altura=540):
    img = np.full((altura, largura, 3), 58, np.uint8)
    cv2.rectangle(img, (0, 60), (largura, 110), (72, 72, 72), -1)
    cv2.putText(img, "BANCADA DE DISPENSERS (cena sintetica)", (24, 96),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (210, 210, 210), 2, cv2.LINE_AA)
    for i in range(4):
        x = 30 + i * 230
        cv2.rectangle(img, (x, 150), (x + 200, 500), (110, 110, 110), -1)
    return img


def dados():
    meds = [
        Medicamento("MED-001", "Dipirona Sodica 500mg", 1, aruco=1),
        Medicamento("MED-002", "Paracetamol 750mg", 2, aruco=2),
        Medicamento("MED-003", "Ibuprofeno 400mg", 3, aruco=3),
        Medicamento("MED-004", "Amoxicilina 500mg comprimido revestido", 4, aruco=4),
    ]
    catalogo = Catalogo({m.qr: m for m in meds})
    zonas = MapaZonas(zonas=[Zona(1, 30, 150, 200, 350), Zona(2, 260, 150, 200, 350),
                             Zona(3, 490, 150, 200, 350), Zona(4, 720, 150, 200, 350)],
                      resolucao=(960, 540))

    vereditos = [
        ResultadoFusao(1, Veredito.OK, 0.97, meds[0], meds[0], "codigo+visual",
                       "conferido", quantidade_certa=3,
                       itens={"Dipirona Sodica 500mg": 3}),
        ResultadoFusao(2, Veredito.OK, 0.91, meds[1], meds[1], "codigo",
                       "conferido", quantidade_certa=1,
                       itens={"Paracetamol 750mg": 1}),
        # caso duro de layout: nome comprido + dois itens + acento na mensagem
        ResultadoFusao(3, Veredito.ERRO_POSICAO, 0.93, meds[0], meds[2], "codigo",
                       "fora de lugar", quantidade_certa=2, quantidade_errada=2,
                       itens={"Dipirona Sodica 500mg": 2, "Ibuprofeno 400mg": 2}),
        ResultadoFusao(4, Veredito.DIVERGENCIA, 0.72, meds[0], meds[3],
                       "codigo+visual", "divergencia", quantidade_errada=1,
                       itens={"Amoxicilina 500mg comprimido revestido": 1}),
    ]
    niveis = {
        1: NivelEstoque(1, 5, 0.82, 300.0, 0.9),
        2: NivelEstoque(2, 2, 0.31, 110.0, 0.8, precisa_repor=True),
        3: NivelEstoque(3, 4, 0.66, 240.0, 0.9),
        4: NivelEstoque(4, 0, 0.0, 0.0, 0.7, precisa_repor=True, vazio=True),
    }
    return catalogo, zonas, vereditos, niveis


def main():
    catalogo, zonas, vereditos, niveis = dados()
    painel = PainelApsen(LARGURA, ALTURA, estacao="BANCADA-01")
    # mensagem real do pipeline: tem acento e seta, que o Hershey nao desenha
    painel.registrar_alerta(vereditos[2].mensagem())

    medidas = Medidas(captura_ms=4.1, deteccao_ms=6.3, interface_ms=2.2,
                      fps=28.4, reavaliacoes=3, frames=150)
    video = desenhar_zonas_no_video(cena(), vereditos, zonas)
    tela = painel.compor(video, vereditos, catalogo, medidas, niveis)

    destino = sys.argv[1] if len(sys.argv) > 1 else "ui_apsen.png"
    cv2.imwrite(destino, tela)
    print(f"gravado: {destino}  ({tela.shape[1]}x{tela.shape[0]})")


if __name__ == "__main__":
    main()
