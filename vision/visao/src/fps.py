"""Diagnostico de FPS: mede onde o tempo esta indo e diz o que fazer.

FPS baixo quase nunca tem uma causa unica obvia. Sao quatro suspeitos, e cada
um pede uma correcao diferente:

    1. captura   - a camera nao ENTREGA mais frames que isso (o loop espera ela)
    2. reducao   - o frame chega grande demais e reduzir custa
    3. deteccao  - a leitura pesada esta rodando frame a frame
    4. interface - o desenho da tela

Este script separa os quatro. Rode-o e siga a recomendacao do fim.

Uso:
    python src/fps.py                 # camera configurada, 10 s
    python src/fps.py --segundos 20
    python src/fps.py --camera 1
    python src/fps.py --larguras 0,1280,960,640   # compara resolucoes
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camera import abrir_camera, indice_para_usar, listar_cameras  # noqa: E402
from configuracao import Catalogo, MapaZonas, carregar_parametros  # noqa: E402
from detector import DetectorDispensers  # noqa: E402
from fusao import MotorDeFusao  # noqa: E402
from interface import (  # noqa: E402
    Medidas,
    PainelApsen,
    desenhar_zonas_no_video,
    reduzir_frame,
)
from leitor_qr import LeitorCodigos  # noqa: E402


def _percentil(valores, p):
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    i = min(len(ordenados) - 1, int(len(ordenados) * p))
    return ordenados[i]


def medir_captura(cap, segundos: float) -> tuple[float, tuple[int, int]]:
    """Quanto tempo a camera leva para entregar um frame, sem processar nada."""
    tempos = []
    forma = (0, 0)
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        t0 = time.perf_counter()
        ok, frame = cap.read()
        t1 = time.perf_counter()
        if not ok:
            break
        forma = (frame.shape[1], frame.shape[0])
        tempos.append((t1 - t0) * 1000)
    tempos = tempos[2:] or tempos      # descarta o aquecimento
    media = sum(tempos) / len(tempos) if tempos else 0.0
    return media, forma


def medir_pipeline(cap, catalogo, zonas, det_cfg, largura, segundos):
    leitor = LeitorCodigos(
        backend=det_cfg.get("backend", "auto"),
        usar_aruco=bool(det_cfg.get("usar_aruco", True)),
        ler_por_zona=bool(det_cfg.get("ler_por_zona", True)),
        lado_alvo_zona=int(det_cfg.get("lado_alvo_zona", 320)),
        limiar_mudanca=float(det_cfg.get("limiar_mudanca", 2.0)),
        intervalo_reavaliacao=float(det_cfg.get("intervalo_reavaliacao", 1.0)),
        orcamento_ms=float(det_cfg.get("orcamento_ms", 250.0)),
    )
    detector = DetectorDispensers(catalogo=catalogo, zonas=zonas, leitor=leitor)
    fus = MotorDeFusao(catalogo)
    painel = PainelApsen(1280, 720)

    cap_ms, red_ms, det_ms, ui_ms, total = [], [], [], [], []
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        t0 = time.perf_counter()
        ok, bruto = cap.read()
        if not ok:
            break
        t1 = time.perf_counter()
        frame = reduzir_frame(bruto, largura)
        t2 = time.perf_counter()
        resultado = detector.processar_frame(frame)
        t3 = time.perf_counter()

        zonas_vivas = resultado.zonas or zonas.para_resolucao(frame.shape[1], frame.shape[0])
        por_zona: dict[int, list] = {}
        for oc in resultado.ocorrencias:
            if oc.dispenser_detectado is not None:
                por_zona.setdefault(oc.dispenser_detectado, []).append(oc)
        vereditos = fus.avaliar_frame(zonas_vivas, por_zona, {})
        video = desenhar_zonas_no_video(frame.copy(), vereditos, zonas_vivas)
        painel.compor(video, vereditos, catalogo, Medidas())
        t4 = time.perf_counter()

        cap_ms.append((t1 - t0) * 1000)
        red_ms.append((t2 - t1) * 1000)
        det_ms.append((t3 - t2) * 1000)
        ui_ms.append((t4 - t3) * 1000)
        total.append((t4 - t0) * 1000)

    n = max(1, len(total))
    return {
        "frames": len(total),
        "captura": sum(cap_ms) / n,
        "reducao": sum(red_ms) / n,
        "deteccao": sum(det_ms) / n,
        "deteccao_p95": _percentil(det_ms, 0.95),
        "interface": sum(ui_ms) / n,
        "total": sum(total) / n,
        "fps": 1000 / (sum(total) / n) if total else 0.0,
        "pesadas": leitor.execucoes_pesadas,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Descobre por que o FPS esta baixo.")
    p.add_argument("--camera", type=str, default=None)
    p.add_argument("--video", type=str, default=None)
    p.add_argument("--segundos", type=float, default=8.0)
    p.add_argument("--larguras", type=str, default=None,
                   help="lista de larguras a comparar, ex: 0,1280,960,640")
    args = p.parse_args()

    parametros = carregar_parametros()
    cam_cfg = parametros.get("camera", {})
    det_cfg = parametros.get("deteccao", {})
    catalogo = Catalogo.carregar()
    try:
        zonas = MapaZonas.carregar()
    except FileNotFoundError:
        print("Sem zonas calibradas. Rode antes: python src/calibrar.py")
        return 1

    def abrir():
        if args.video:
            return cv2.VideoCapture(args.video)
        indice = indice_para_usar(args.camera, cam_cfg)
        return abrir_camera(indice, int(cam_cfg.get("largura", 1280)),
                            int(cam_cfg.get("altura", 720)),
                            int(cam_cfg.get("fps", 30)), ajustes=cam_cfg)

    cap = abrir()
    if cap is None or not cap.isOpened():
        print("Camera indisponivel. Disponiveis:")
        for c in listar_cameras():
            print(f"  {c}")
        return 1

    print("=" * 66)
    print("DIAGNOSTICO DE FPS")
    print("=" * 66)

    captura_ms, forma = medir_captura(cap, min(4.0, args.segundos))
    teto = 1000 / captura_ms if captura_ms else 0.0
    print(f"\n1) A camera sozinha (sem processar nada)")
    print(f"   resolucao entregue ..... {forma[0]}x{forma[1]}")
    print(f"   tempo por frame ........ {captura_ms:.1f} ms")
    print(f"   TETO de FPS da camera .. {teto:.1f}")
    print("   Nenhum ajuste no codigo passa desse teto: e o que a camera entrega.")

    larguras = ([int(x) for x in args.larguras.split(",")] if args.larguras
                else [0, int(det_cfg.get("largura_processamento", 960) or 960)])
    print(f"\n2) O sistema completo, por largura de processamento")
    print(f"   {'largura':>9} | {'captura':>8} {'reducao':>8} {'deteccao':>9} "
          f"{'p95':>7} {'ui':>6} | {'total':>7} {'FPS':>6}")
    melhor = None
    for largura in larguras:
        if args.video:
            cap.release()
            cap = abrir()
        m = medir_pipeline(cap, catalogo, zonas, det_cfg, largura, args.segundos)
        rotulo = "original" if largura <= 0 else str(largura)
        print(f"   {rotulo:>9} | {m['captura']:7.1f}  {m['reducao']:7.1f}  "
              f"{m['deteccao']:8.1f}  {m['deteccao_p95']:6.1f}  {m['interface']:5.1f} | "
              f"{m['total']:6.1f}  {m['fps']:5.1f}")
        if melhor is None or m["fps"] > melhor[1]["fps"]:
            melhor = (largura, m)

    cap.release()

    largura, m = melhor
    print("\n" + "=" * 66)
    print("O QUE FAZER")
    print("=" * 66)

    partes = {"captura": m["captura"], "reducao": m["reducao"],
              "deteccao": m["deteccao"], "interface": m["interface"]}
    gargalo = max(partes, key=partes.get)
    print(f"   gargalo: {gargalo}  ({partes[gargalo]:.1f} ms de {m['total']:.1f} ms)")

    if gargalo == "captura":
        print("""
   O loop esta ESPERANDO a camera. Processar mais rapido nao ajuda.
     - baixe a resolucao NO APP da camera (no Camo: Settings > Resolution
       para 720p). Pedir 1280x720 no parametros.json nao adianta se o app
       insiste em 1080p.
     - no Camo, use conexao USB em vez de Wi-Fi.
     - feche Teams/Meet/navegador: qualquer um deles disputando a camera
       derruba a taxa de entrega.""")
    elif gargalo == "deteccao":
        print(f"""
   A leitura pesada esta rodando demais ({m['pesadas']} vezes em
   {m['frames']} frames). Em cena estatica ela deveria ser rara.
     - se a camera treme (celular na mao/tripe fraco), o portao de mudanca
       dispara toda hora: fixe a camera.
     - suba 'limiar_mudanca' em config/parametros.json (2.0 -> 4.0) para
       tolerar ruido de sensor.
     - baixe 'orcamento_ms' (250 -> 100) para limitar o pior frame.""")
    elif gargalo == "reducao":
        print("""
   Reduzir esta custando mais que processar. Isso quer dizer que a camera
   entrega muito mais pixels do que precisamos: baixe a resolucao no app
   da camera em vez de reduzir por software.""")
    else:
        print("""
   O desenho da tela e o maior custo — o que significa que a leitura ja
   esta barata. Diminua a janela em config/parametros.json > interface
   (1280x720 -> 1024x576) se precisar de mais FPS.""")

    atual = int(det_cfg.get("largura_processamento", 0))
    if largura != atual:
        print(f"\n   Melhor largura medida: {largura or 'original'} "
              f"(configurada hoje: {atual or 'original'})")
        print(f"   Grave em config/parametros.json > deteccao > "
              f"largura_processamento: {largura}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
