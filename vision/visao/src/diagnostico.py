"""Diagnostico de qualidade de leitura, dispenser por dispenser.

Em vez de adivinhar por que a leitura falha, esta ferramenta mede — para cada
zona calibrada — taxa de leitura, nitidez, brilho, contraste, saturacao
(reflexo) e tamanho aparente do codigo, e diz o que corrigir.

Uso:
    python src/diagnostico.py                  # ao vivo, 20 segundos
    python src/diagnostico.py --segundos 60
    python src/diagnostico.py --imagem foto.jpg
    python src/diagnostico.py --video teste.mp4
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import preprocessamento as pre  # noqa: E402
from camera import abrir_camera, indice_para_usar, listar_cameras  # noqa: E402
from configuracao import Catalogo, MapaZonas, carregar_parametros  # noqa: E402
from detector import DetectorDispensers  # noqa: E402
from leitor_qr import LeitorCodigos, backend_disponivel  # noqa: E402

# limiares usados nos diagnosticos
NITIDEZ_MINIMA = 60.0
BRILHO_MINIMO = 55.0
BRILHO_MAXIMO = 205.0
CONTRASTE_MINIMO = 28.0
SATURACAO_MAXIMA = 3.0   # % de pixels estourados dentro da zona
LADO_MINIMO_PX = 70.0    # lado do codigo na imagem


class MedidasZona:
    def __init__(self, dispenser: int) -> None:
        self.dispenser = dispenser
        self.frames = 0
        self.lidos = 0
        self.lidos_qr = 0
        self.lidos_aruco = 0
        self.nitidez: list[float] = []
        self.brilho: list[float] = []
        self.contraste: list[float] = []
        self.saturacao: list[float] = []
        self.lado: list[float] = []
        self.variantes: dict[str, int] = {}

    @property
    def taxa(self) -> float:
        return self.lidos / self.frames * 100.0 if self.frames else 0.0

    @staticmethod
    def _media(v: list[float]) -> float:
        return float(np.mean(v)) if v else 0.0

    def diagnosticar(self) -> list[str]:
        """Traduz as medidas em acoes concretas."""
        problemas: list[str] = []

        if self.taxa >= 95:
            return ["leitura estavel, nada a corrigir"]

        nit = self._media(self.nitidez)
        bri = self._media(self.brilho)
        con = self._media(self.contraste)
        sat = self._media(self.saturacao)
        lado = self._media(self.lado)

        if self.lidos == 0:
            problemas.append(
                "NUNCA leu nada nesta zona. Confira se a zona esta no lugar certo "
                "(src/calibrar.py) e se o codigo esta cadastrado em medicamentos.json"
            )

        if lado and lado < LADO_MINIMO_PX:
            problemas.append(
                f"codigo pequeno demais ({lado:.0f} px de lado; o minimo confiavel e "
                f"{LADO_MINIMO_PX:.0f}). Imprima maior (--tamanho-mm 45), aproxime a "
                "camera ou aumente a resolucao"
            )
        if nit < NITIDEZ_MINIMA:
            problemas.append(
                f"imagem fora de foco (nitidez {nit:.0f}, minimo {NITIDEZ_MINIMA:.0f}). "
                "Fixe o foco em parametros.json (autofoco: false, foco: <valor>) — "
                "o autofoco costuma ficar cacando foco"
            )
        if bri < BRILHO_MINIMO:
            problemas.append(
                f"escuro demais (brilho medio {bri:.0f}). Adicione luz difusa frontal "
                "ou aumente 'exposicao'/'ganho' em parametros.json"
            )
        elif bri > BRILHO_MAXIMO:
            problemas.append(
                f"claro demais (brilho medio {bri:.0f}), a imagem esta estourando. "
                "Reduza a exposicao ou afaste a luz"
            )
        if con < CONTRASTE_MINIMO:
            problemas.append(
                f"contraste baixo ({con:.0f}). Etiqueta em papel fosco branco resolve; "
                "papel brilhante e plastico transparente pioram muito"
            )
        if sat > SATURACAO_MAXIMA:
            problemas.append(
                f"reflexo forte ({sat:.1f}% da zona saturada em branco). Mude o angulo "
                "da luz para nao bater de frente no codigo, use luz lateral difusa ou "
                "cole a etiqueta em material fosco"
            )

        if not problemas:
            problemas.append(
                "leitura intermitente sem causa unica clara. Aumente 'memoria_segundos' "
                "e reduza 'frames_para_confirmar' em parametros.json"
            )
        return problemas


def _medir_zona(frame: np.ndarray, zona) -> tuple[float, float, float, float]:
    h, w = frame.shape[:2]
    x1, y1 = max(0, zona.x), max(0, zona.y)
    x2, y2 = min(w, zona.x2), min(h, zona.y2)
    if x2 - x1 < 5 or y2 - y1 < 5:
        return 0.0, 0.0, 0.0, 0.0
    g = pre.cinza(frame[y1:y2, x1:x2])
    return pre.nitidez(g), pre.brilho(g), pre.contraste(g), pre.percentual_estourado(g)


def main() -> int:
    p = argparse.ArgumentParser(description="Diagnostico da qualidade de leitura.")
    p.add_argument("--camera", type=str, default=None, help="numero da camera ou parte do nome (ex: camo)")
    p.add_argument("--video", type=str, default=None)
    p.add_argument("--imagem", type=str, default=None)
    p.add_argument("--segundos", type=float, default=20.0)
    p.add_argument("--sem-janela", action="store_true")
    args = p.parse_args()

    parametros = carregar_parametros()
    cam_cfg = parametros.get("camera", {})
    det_cfg = parametros.get("deteccao", {})

    catalogo = Catalogo.carregar()
    zonas = MapaZonas.carregar()

    leitor = LeitorCodigos(
        backend=det_cfg.get("backend", "auto"),
        cascata=bool(det_cfg.get("cascata", True)),
        cascata_completa=bool(det_cfg.get("cascata_completa", True)),
        usar_aruco=bool(det_cfg.get("usar_aruco", True)),
        dicionario_aruco=det_cfg.get("dicionario_aruco", "DICT_4X4_50"),
        ler_por_zona=bool(det_cfg.get("ler_por_zona", True)),
        lado_alvo_zona=int(det_cfg.get("lado_alvo_zona", 320)),
    )
    detector = DetectorDispensers(
        catalogo=catalogo, zonas=zonas, leitor=leitor,
        frames_para_confirmar=1, cooldown_segundos=1e9, memoria_segundos=0.0,
    )

    # fonte
    quadro_unico = None
    cap = None
    if args.imagem:
        quadro_unico = cv2.imread(args.imagem)
        if quadro_unico is None:
            print(f"Nao consegui abrir {args.imagem}")
            return 1
    elif args.video:
        cap = cv2.VideoCapture(args.video)
    else:
        indice = indice_para_usar(args.camera, cam_cfg)
        cap = abrir_camera(
            indice, int(cam_cfg.get("largura", 1280)), int(cam_cfg.get("altura", 720)),
            int(cam_cfg.get("fps", 30)), ajustes=cam_cfg,
        )
        if cap is None:
            print("Nao consegui abrir a camera. Disponiveis:")
            for camera in listar_cameras():
                print(f"  {camera}")
            print("Escolha com: python src/camera.py")
            return 1

    medidas = {z.dispenser: MedidasZona(z.dispenser) for z in zonas.zonas}

    print("=" * 72)
    print("DIAGNOSTICO DE LEITURA")
    print(f"  leitor ....... {backend_disponivel()}")
    print(f"  zonas ........ {len(zonas.zonas)}")
    if quadro_unico is None:
        print(f"  coletando .... {args.segundos:.0f} s (Q na janela encerra antes)")
    print("=" * 72)

    inicio = time.monotonic()
    total_frames = 0

    while True:
        if quadro_unico is not None:
            frame = quadro_unico.copy()
        else:
            ok, frame = cap.read()  # type: ignore[union-attr]
            if not ok:
                break

        resultado = detector.processar_frame(frame)
        zonas_ativas = resultado.zonas or zonas
        total_frames += 1

        lidos_por_zona: dict[int, list] = {}
        for oc in resultado.lidos_agora:
            if oc.dispenser_detectado is not None:
                lidos_por_zona.setdefault(oc.dispenser_detectado, []).append(oc)

        for zona in zonas_ativas.zonas:
            m = medidas.get(zona.dispenser)
            if m is None:
                continue
            m.frames += 1
            nit, bri, con, sat = _medir_zona(frame, zona)
            m.nitidez.append(nit)
            m.brilho.append(bri)
            m.contraste.append(con)
            m.saturacao.append(sat)

            ocs = lidos_por_zona.get(zona.dispenser, [])
            if ocs:
                m.lidos += 1
                for oc in ocs:
                    if oc.leitura.tipo == "aruco":
                        m.lidos_aruco += 1
                    else:
                        m.lidos_qr += 1
                    m.variantes[oc.leitura.variante] = m.variantes.get(oc.leitura.variante, 0) + 1
                    m.lado.append(oc.leitura.lado)

        if quadro_unico is not None:
            break
        if time.monotonic() - inicio >= args.segundos:
            break
        if not args.sem_janela:
            try:
                cv2.imshow("Diagnostico (Q encerra)", frame)
                if (cv2.waitKey(1) & 0xFF) in (27, ord("q"), ord("Q")):
                    break
            except cv2.error:
                args.sem_janela = True

    if cap is not None:
        cap.release()
    if not args.sem_janela:
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass

    # ------------------------------ relatorio ------------------------------ #
    print(f"\nFrames analisados: {total_frames}\n")
    for numero in sorted(medidas):
        m = medidas[numero]
        esperado = catalogo.esperado_em(numero)
        nome = esperado.nome if esperado else "(sem medicamento cadastrado)"
        print(f"DISPENSER {numero} — {nome}")
        print(f"  taxa de leitura .. {m.taxa:5.1f}%   "
              f"(QR {m.lidos_qr}, ArUco {m.lidos_aruco})")
        print(f"  nitidez .......... {m._media(m.nitidez):6.0f}   (min {NITIDEZ_MINIMA:.0f})")
        print(f"  brilho ........... {m._media(m.brilho):6.0f}   (faixa boa "
              f"{BRILHO_MINIMO:.0f}-{BRILHO_MAXIMO:.0f})")
        print(f"  contraste ........ {m._media(m.contraste):6.0f}   (min {CONTRASTE_MINIMO:.0f})")
        print(f"  reflexo .......... {m._media(m.saturacao):6.1f}%  (max {SATURACAO_MAXIMA:.0f}%)")
        if m.lado:
            print(f"  lado do codigo ... {m._media(m.lado):6.0f} px (min {LADO_MINIMO_PX:.0f})")
        if m.variantes:
            top = sorted(m.variantes.items(), key=lambda x: -x[1])[:3]
            print("  como conseguiu ... " + ", ".join(f"{k} ({v}x)" for k, v in top))
        for linha in m.diagnosticar():
            print(f"  -> {linha}")
        print()

    fracas = [m for m in medidas.values() if m.taxa < 80]
    if fracas:
        print("RESUMO: " + ", ".join(f"dispenser {m.dispenser} em {m.taxa:.0f}%" for m in fracas))
        print("Comece pelo item marcado com '->' de cada dispenser fraco: "
              "quase sempre e foco travado, luz ou tamanho do codigo.")
    else:
        print("RESUMO: todos os dispensers com leitura confiavel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
