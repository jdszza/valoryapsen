"""Mede objetivamente quanto cada melhoria ajuda em condicoes de camera ruim.

Roda a etiqueta real (a mesma que sai de gerar_qrcodes.py) por uma bateria de
degradacoes — angulo, desfoque, luz baixa, reflexo, ruido, compressao — e
compara tres configuracoes de leitura.

Uso:
    python tests/benchmark_robustez.py
    python tests/benchmark_robustez.py --repeticoes 20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(RAIZ / "tests"))

from configuracao import Catalogo, MapaZonas, Zona  # noqa: E402
from degradacao import CENARIOS, gerar_cenario  # noqa: E402
from gerar_qrcodes import _mm_para_px, gerar_etiqueta  # noqa: E402
from leitor_qr import LeitorCodigos  # noqa: E402


def _pil_para_cv(img) -> np.ndarray:
    return cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)


CONFIGS = {
    "1. QR simples": dict(cascata=False, ler_por_zona=False, usar_aruco=False),
    "2. + cascata": dict(cascata=True, ler_por_zona=True, usar_aruco=False),
    "3. + ArUco": dict(cascata=True, ler_por_zona=True, usar_aruco=True),
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repeticoes", type=int, default=12)
    p.add_argument("--semente", type=int, default=5)
    args = p.parse_args()

    catalogo = Catalogo.carregar()
    med = catalogo.medicamentos[0]

    LADO_MM = 30
    lado_qr_px = _mm_para_px(LADO_MM)  # largura do QR dentro da etiqueta
    etiqueta_qr = _pil_para_cv(
        gerar_etiqueta(med.qr, med.nome, med.dispenser, LADO_MM, None)
    )
    etiqueta_hibrida = _pil_para_cv(
        gerar_etiqueta(med.qr, med.nome, med.dispenser, LADO_MM, med.aruco)
    )

    leitores = {
        nome: LeitorCodigos(**cfg, cascata_completa=True) for nome, cfg in CONFIGS.items()
    }

    print(f"Etiqueta: {med.nome} [{med.qr}] / ArUco {med.aruco}")
    print(f"Repeticoes por cenario: {args.repeticoes}\n")
    cab = f"{'cenario':30s}" + "".join(f"{n:>16s}" for n in CONFIGS)
    print(cab)
    print("-" * len(cab))

    totais = {n: 0 for n in CONFIGS}
    quantidade = 0

    for cenario in CENARIOS:
        linha = f"{cenario[0]:30s}"
        for nome, leitor in leitores.items():
            usa_aruco = CONFIGS[nome]["usar_aruco"]
            base = etiqueta_hibrida if usa_aruco else etiqueta_qr

            np.random.seed(args.semente)  # mesmas degradacoes para todos
            ok = 0
            for _ in range(args.repeticoes):
                cena = gerar_cenario(base, cenario, largura_referencia=lado_qr_px)
                # zona = a imagem inteira (equivale a um dispenser enquadrado)
                zonas = MapaZonas(
                    zonas=[Zona(1, 0, 0, cena.shape[1], cena.shape[0])],
                    resolucao=(cena.shape[1], cena.shape[0]),
                )
                leituras = leitor.ler(cena, zonas=zonas if CONFIGS[nome]["ler_por_zona"] else None)
                alvos = {med.qr, f"ARUCO:{med.aruco}"}
                ok += 1 if any(l.conteudo in alvos for l in leituras) else 0
            totais[nome] += ok
            linha += f"{ok}/{args.repeticoes}".rjust(16)
        quantidade += args.repeticoes
        print(linha)

    print("-" * len(cab))
    linha = f"{'TOTAL':30s}"
    for nome in CONFIGS:
        pct = totais[nome] * 100 // quantidade
        linha += f"{pct}%".rjust(16)
    print(linha)

    print("\nLegenda:")
    print("  1. QR simples ... como era antes: um QR, uma tentativa de leitura")
    print("  2. + cascata .... recorte por zona, ampliacao e pre-processamento em cascata")
    print("  3. + ArUco ...... etiqueta hibrida com marcador redundante")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
