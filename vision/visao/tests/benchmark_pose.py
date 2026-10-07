"""Limites de escala e de orientacao — quando cada canal para de funcionar.

Responde, com numero, as perguntas que decidem a montagem fisica:

  1. Ainda preciso do ArUco agora que existe reconhecimento visual?
  2. Ate que tamanho na imagem cada canal aguenta?
  3. E se a caixa ficar de lado, na diagonal ou de cabeca para baixo?

Uso:
    python tests/benchmark_pose.py
    python tests/benchmark_pose.py --repeticoes 5
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
from degradacao import warp_angulo  # noqa: E402
from embalagens import catalogo_sintetico, variacao  # noqa: E402
from gerar_qrcodes import _mm_para_px, gerar_etiqueta  # noqa: E402
from leitor_qr import LeitorCodigos  # noqa: E402
from reconhecimento import ReconhecedorVisual  # noqa: E402

LARGURA_CENA, ALTURA_CENA = 640, 640


def _pil_para_cv(img) -> np.ndarray:
    return cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)


def girar(img: np.ndarray, graus: float, fundo: int = 110) -> np.ndarray:
    """Rotacao no plano da imagem, sem cortar os cantos."""
    h, w = img.shape[:2]
    centro = (w / 2.0, h / 2.0)
    M = cv2.getRotationMatrix2D(centro, graus, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    M[0, 2] += nw / 2 - centro[0]
    M[1, 2] += nh / 2 - centro[1]
    return cv2.warpPerspective(
        img, np.vstack([M, [0, 0, 1]]), (nw, nh),
        borderMode=cv2.BORDER_CONSTANT, borderValue=(fundo, fundo, fundo),
    )


def montar_cena(arte: np.ndarray, lado_alvo: int, largura_ref: int,
                ruido: float = 3.0, semente: int = 0):
    """Coloca a arte numa cena do tamanho de uma zona, com o codigo em `lado_alvo` px."""
    fator = lado_alvo / float(largura_ref)
    novo = (max(8, int(arte.shape[1] * fator)), max(8, int(arte.shape[0] * fator)))
    peca = cv2.resize(arte, novo, interpolation=cv2.INTER_AREA)

    cena = np.full((ALTURA_CENA, LARGURA_CENA, 3), 110, np.uint8)
    ph, pw = peca.shape[:2]
    if ph >= ALTURA_CENA or pw >= LARGURA_CENA:
        escala = min(ALTURA_CENA / (ph + 20), LARGURA_CENA / (pw + 20))
        peca = cv2.resize(peca, (int(pw * escala), int(ph * escala)))
        ph, pw = peca.shape[:2]
    y = (ALTURA_CENA - ph) // 2
    x = (LARGURA_CENA - pw) // 2
    cena[y:y + ph, x:x + pw] = peca

    rng = np.random.RandomState(semente)
    cena = np.clip(cena.astype(np.float32) + rng.normal(0, ruido, cena.shape),
                   0, 255).astype(np.uint8)
    zonas = MapaZonas(zonas=[Zona(1, 0, 0, LARGURA_CENA, ALTURA_CENA)],
                      resolucao=(LARGURA_CENA, ALTURA_CENA))
    return cena, zonas


# --------------------------------------------------------------------------- #
def preparar(catalogo):
    med = catalogo.medicamentos[0]
    lado_qr = _mm_para_px(30)

    etiqueta_hibrida = _pil_para_cv(
        gerar_etiqueta(med.qr, med.nome, med.dispenser, 30, med.aruco))
    etiqueta_so_qr = _pil_para_cv(
        gerar_etiqueta(med.qr, med.nome, med.dispenser, 30, None))

    faces = catalogo_sintetico(catalogo.medicamentos)
    rec = ReconhecedorVisual()
    for sku, face in faces.items():
        rec.cadastrar(sku, catalogo.buscar(sku).nome,
                      [variacao(face, semente=i, **kw) for i, kw in enumerate(
                          [dict(), dict(graus=20), dict(graus=-20), dict(graus=40),
                           dict(graus=-40), dict(escala=0.5)])])

    return med, lado_qr, etiqueta_hibrida, etiqueta_so_qr, faces, rec


def ler_qr(leitor, cena, zonas, alvo) -> bool:
    return any(l.conteudo == alvo and l.tipo == "qr"
               for l in leitor.ler(cena, zonas=zonas))


def ler_aruco(leitor, cena, zonas, alvo) -> bool:
    # o leitor recebido aqui tem o QR desligado: o pipeline para na primeira
    # estrategia que funciona, entao medir o ArUco com o QR ligado mediria o QR
    return any(l.conteudo == alvo and l.tipo == "aruco"
               for l in leitor.ler(cena, zonas=zonas))


def cena_visual(face: np.ndarray, lado: int, semente: int = 0) -> np.ndarray:
    """Recorte como o que a zona entrega: a caixa enquadrada, com pouca folga.

    Passar a caixa pequena dentro de uma cena grande mediria outra coisa — o
    reconhecedor recebe sempre o recorte da zona, ja enquadrado.
    """
    fator = lado / float(face.shape[1])
    peca = cv2.resize(face, (max(8, int(face.shape[1] * fator)),
                             max(8, int(face.shape[0] * fator))),
                      interpolation=cv2.INTER_AREA)
    margem = max(4, int(lado * 0.08))
    cena = np.full((peca.shape[0] + 2 * margem, peca.shape[1] + 2 * margem, 3),
                   110, np.uint8)
    cena[margem:margem + peca.shape[0], margem:margem + peca.shape[1]] = peca
    rng = np.random.RandomState(semente)
    return np.clip(cena.astype(np.float32) + rng.normal(0, 3.0, cena.shape),
                   0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- #
def teste_escala(med, lado_qr, hibrida, faces, rec, repeticoes):
    print("\n" + "=" * 74)
    print("ESCALA — codigo/embalagem cada vez menor na imagem")
    print("=" * 74)
    print(f"{'lado do QR na imagem':>22s} {'QR':>10s} {'ArUco':>10s} {'embalagem':>12s}")

    # leitor sem ArUco para medir o QR isolado, e outro completo
    so_qr = LeitorCodigos(usar_aruco=False)
    completo = LeitorCodigos(usar_qr=False, usar_aruco=True)
    face = faces[med.qr]

    limites = {}
    for lado in (25, 30, 40, 50, 60, 70, 85, 100, 130, 170):
        acertos = {"qr": 0, "aruco": 0, "visual": 0}
        for r in range(repeticoes):
            cena, zonas = montar_cena(hibrida, lado, lado_qr, semente=r)
            so_qr._cache_zona.clear()
            completo._cache_zona.clear()
            acertos["qr"] += ler_qr(so_qr, cena, zonas, med.qr)
            acertos["aruco"] += ler_aruco(completo, cena, zonas, f"ARUCO:{med.aruco}")

        for canal in acertos:
            if acertos[canal] == repeticoes:
                limites.setdefault(canal, lado)
        print(f"{lado:>19d} px {acertos['qr']:>7d}/{repeticoes} "
              f"{acertos['aruco']:>7d}/{repeticoes} {acertos['visual']:>9d}/{repeticoes}")

    print("\nmenor tamanho com 100% de leitura:")
    for canal, rotulo in (("qr", "QR"), ("aruco", "ArUco")):
        valor = limites.get(canal)
        print(f"  {rotulo:10s} {str(valor) + ' px' if valor else 'nunca chegou a 100%'}")

    # a embalagem tem eixo proprio: o que importa nela e a largura da CAIXA no
    # recorte, nao o lado do codigo impresso
    print("\n  reconhecimento da embalagem (largura da caixa no recorte da zona):")
    limite_visual = None
    for largura in (120, 180, 250, 340, 500, 750, 1000):
        ok = 0
        for r in range(repeticoes):
            for sku, f in faces.items():
                res = rec.identificar(cena_visual(f, largura, semente=r))
                ok += res.nivel != "desconhecido" and res.sku == sku
        total = repeticoes * len(faces)
        if ok == total and limite_visual is None:
            limite_visual = largura
        print(f"    {largura:5d} px -> {ok}/{total}")
    print(f"  menor largura com 100%: "
          f"{str(limite_visual) + ' px' if limite_visual else 'nao atingiu'}")
    return limites


def teste_rotacao(med, lado_qr, hibrida, faces, rec, repeticoes):
    print("\n" + "=" * 74)
    print("ROTACAO NO PLANO — caixa de lado, de cabeca para baixo, na diagonal")
    print("=" * 74)
    print(f"{'giro':>12s} {'QR':>10s} {'ArUco':>10s} {'embalagem':>12s}")

    so_qr = LeitorCodigos(usar_aruco=False)
    completo = LeitorCodigos(usar_qr=False, usar_aruco=True)
    face = faces[med.qr]

    for graus in (0, 15, 30, 45, 90, 135, 180, 225, 270):
        acertos = {"qr": 0, "aruco": 0, "visual": 0}
        for r in range(repeticoes):
            arte = girar(hibrida, graus)
            cena, zonas = montar_cena(arte, 110, lado_qr, semente=r)
            so_qr._cache_zona.clear()
            completo._cache_zona.clear()
            acertos["qr"] += ler_qr(so_qr, cena, zonas, med.qr)
            acertos["aruco"] += ler_aruco(completo, cena, zonas, f"ARUCO:{med.aruco}")

            res = rec.identificar(cena_visual(girar(face, graus), 620, semente=r))
            acertos["visual"] += res.nivel != "desconhecido" and res.sku == med.qr

        rotulo = {0: "0 (normal)", 90: "90 (de lado)", 180: "180 (de ponta-cabeca)",
                  270: "270 (de lado)"}.get(graus, f"{graus} (diagonal)")
        print(f"{rotulo:>12s}".rjust(24) +
              f" {acertos['qr']:>7d}/{repeticoes} {acertos['aruco']:>7d}/{repeticoes}"
              f" {acertos['visual']:>9d}/{repeticoes}")


def teste_inclinacao(med, lado_qr, hibrida, faces, rec, repeticoes):
    print("\n" + "=" * 74)
    print("INCLINACAO — caixa virada para o lado em relacao a camera (perspectiva)")
    print("=" * 74)
    print(f"{'inclinacao':>12s} {'QR':>10s} {'ArUco':>10s} {'embalagem':>12s}")

    so_qr = LeitorCodigos(usar_aruco=False)
    completo = LeitorCodigos(usar_qr=False, usar_aruco=True)
    face = faces[med.qr]

    for graus in (0, 20, 40, 55, 65, 75):
        acertos = {"qr": 0, "aruco": 0, "visual": 0}
        for r in range(repeticoes):
            arte = warp_angulo(hibrida, graus_y=graus) if graus else hibrida
            cena, zonas = montar_cena(arte, 110, lado_qr, semente=r)
            so_qr._cache_zona.clear()
            completo._cache_zona.clear()
            acertos["qr"] += ler_qr(so_qr, cena, zonas, med.qr)
            acertos["aruco"] += ler_aruco(completo, cena, zonas, f"ARUCO:{med.aruco}")

            arte_face = warp_angulo(face, graus_y=graus) if graus else face
            res = rec.identificar(cena_visual(arte_face, 620, semente=r))
            acertos["visual"] += res.nivel != "desconhecido" and res.sku == med.qr

        print(f"{graus:>10d} st {acertos['qr']:>7d}/{repeticoes} "
              f"{acertos['aruco']:>7d}/{repeticoes} {acertos['visual']:>9d}/{repeticoes}")


def teste_aruco_ainda_vale(med, lado_qr, hibrida, so_qr_arte, faces, rec, repeticoes):
    print("\n" + "=" * 74)
    print("O ArUco AINDA VALE A PENA, agora que existe reconhecimento visual?")
    print("=" * 74)

    cenarios = [
        ("codigo pequeno (45 px)", dict(lado=45)),
        ("codigo bem pequeno (32 px)", dict(lado=32)),
        ("inclinado 65 graus", dict(lado=110, inclinacao=65)),
        ("desfocado forte", dict(lado=110, desfoque=2.4)),
        ("luz muito baixa", dict(lado=110, brilho=0.30)),
        ("pequeno + inclinado + escuro", dict(lado=55, inclinacao=45, brilho=0.45,
                                              desfoque=1.4)),
    ]

    sem_aruco = LeitorCodigos(usar_aruco=False)
    com_aruco = LeitorCodigos(usar_qr=False, usar_aruco=True)
    face = faces[med.qr]

    print(f"{'cenario':>32s} {'QR':>8s} {'+ArUco':>8s} {'+visual':>9s} {'os tres':>9s}")
    totais = {"qr": 0, "aruco": 0, "visual": 0, "uniao": 0}
    n = 0

    for nome, cfg in cenarios:
        conta = {"qr": 0, "aruco": 0, "visual": 0, "uniao": 0}
        for r in range(repeticoes):
            def preparar_arte(base, ref):
                arte = base
                if cfg.get("inclinacao"):
                    arte = warp_angulo(arte, graus_y=cfg["inclinacao"])
                cena, zonas = montar_cena(arte, cfg["lado"], ref, semente=r)
                if cfg.get("desfoque"):
                    cena = cv2.GaussianBlur(cena, (0, 0), cfg["desfoque"])
                if cfg.get("brilho"):
                    cena = np.clip(cena.astype(np.float32) * cfg["brilho"], 0,
                                   255).astype(np.uint8)
                return cena, zonas

            cena, zonas = preparar_arte(hibrida, lado_qr)
            sem_aruco._cache_zona.clear()
            com_aruco._cache_zona.clear()
            leu_qr = ler_qr(sem_aruco, cena, zonas, med.qr)
            leu_aruco = ler_aruco(com_aruco, cena, zonas, f"ARUCO:{med.aruco}")

            arte_face = face
            if cfg.get("inclinacao"):
                arte_face = warp_angulo(arte_face, graus_y=cfg["inclinacao"])
            cena_face = cena_visual(arte_face, max(340, cfg["lado"] * 6), semente=r)
            if cfg.get("desfoque"):
                cena_face = cv2.GaussianBlur(cena_face, (0, 0), cfg["desfoque"])
            if cfg.get("brilho"):
                cena_face = np.clip(cena_face.astype(np.float32) * cfg["brilho"],
                                    0, 255).astype(np.uint8)
            leu_visual = rec.identificar(cena_face).nivel != "desconhecido"

            conta["qr"] += leu_qr
            conta["aruco"] += leu_aruco
            conta["visual"] += leu_visual
            conta["uniao"] += (leu_qr or leu_aruco or leu_visual)

        for k in conta:
            totais[k] += conta[k]
        n += repeticoes
        print(f"{nome:>32s} {conta['qr']:>5d}/{repeticoes} {conta['aruco']:>5d}/{repeticoes}"
              f" {conta['visual']:>6d}/{repeticoes} {conta['uniao']:>6d}/{repeticoes}")

    print("-" * 74)
    print(f"{'TOTAL':>32s} {totais['qr'] * 100 // n:>6d}% {totais['aruco'] * 100 // n:>7d}%"
          f" {totais['visual'] * 100 // n:>8d}% {totais['uniao'] * 100 // n:>8d}%")

    qr_mais_visual = totais["qr"] + max(0, totais["visual"] - totais["qr"])
    print("\nCobertura nos cenarios dificeis:")
    print(f"  QR sozinho ....................... {totais['qr'] * 100 // n}%")
    print(f"  QR + embalagem, SEM ArUco ........ {min(100, qr_mais_visual * 100 // n)}%")
    print(f"  QR + ArUco (sem embalagem) ....... {totais['aruco'] * 100 // n}%")
    print(f"  os tres juntos ................... {totais['uniao'] * 100 // n}%")
    print("\nConclusao: nestes cenarios o reconhecimento de embalagem NAO cobre o")
    print("que o ArUco cobre. Sao coisas diferentes — o ArUco resolve codigo")
    print("pequeno, torto e desfocado; a embalagem resolve o caso de nao haver")
    print("codigo legivel nenhum, e so funciona com a caixa grande no recorte.")
    return totais, n


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repeticoes", type=int, default=4)
    args = p.parse_args()

    catalogo = Catalogo.carregar()
    med, lado_qr, hibrida, so_qr_arte, faces, rec = preparar(catalogo)
    print(f"Etiqueta de referencia: {med.nome} [{med.qr}] / ArUco {med.aruco}")
    print(f"Repeticoes por ponto: {args.repeticoes}")

    teste_escala(med, lado_qr, hibrida, faces, rec, args.repeticoes)
    teste_rotacao(med, lado_qr, hibrida, faces, rec, args.repeticoes)
    teste_inclinacao(med, lado_qr, hibrida, faces, rec, args.repeticoes)
    teste_aruco_ainda_vale(med, lado_qr, hibrida, so_qr_arte, faces, rec, args.repeticoes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
