"""Aplicacao principal: le a camera, valida a posicao dos medicamentos e alerta.

Uso:
    python src/main.py                       # camera padrao, janela ao vivo
    python src/main.py --camera 1
    python src/main.py --sem-janela          # modo headless (so log/webhook/GPIO)
    python src/main.py --video teste.mp4     # roda sobre um video gravado
    python src/main.py --gravar saida.mp4    # grava o video com o overlay

Teclas na janela:
    Q ou ESC ... sair
    P ......... pausar / continuar
    E ......... salvar um print do frame atual em logs/
    F ......... tela cheia
    Z ......... painel classico (overlay simples) x painel Apsen
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alertas import Notificador  # noqa: E402
from camera import abrir_camera, indice_para_usar, listar_cameras  # noqa: E402
from configuracao import (  # noqa: E402
    RAIZ,
    Catalogo,
    MapaZonas,
    carregar_parametros,
)
from desenho import renderizar  # noqa: E402
from detector import DetectorDispensers  # noqa: E402
from fusao import MotorDeFusao  # noqa: E402
from interface import (  # noqa: E402
    Medidas,
    PainelApsen,
    desenhar_zonas_no_video,
    reduzir_frame,
)
from leitor_qr import LeitorCodigos, backend_disponivel  # noqa: E402

JANELA = "Visao dos dispensers"


def _dica_camera() -> str:
    """Mensagem util quando a camera nao abre — inclui o caso de camera virtual."""
    cameras = listar_cameras()
    if not cameras:
        return ("Nenhuma camera respondeu. Se voce usa o celular como webcam "
                "(Camo, Iriun, DroidCam), abra o app no PC e no celular antes.\n"
                "Rode: python src/camera.py")
    linhas = "\n".join(f"  {c}" for c in cameras)
    return f"Cameras disponiveis:\n{linhas}\nEscolha com: python src/camera.py"


def abrir_fonte(args, cam_cfg: dict):
    if args.video:
        cap = cv2.VideoCapture(args.video)
        if not cap.isOpened():
            raise SystemExit(f"Nao consegui abrir o video {args.video}")
        return cap, False
    indice = indice_para_usar(args.camera, cam_cfg)
    cap = abrir_camera(
        indice,
        int(cam_cfg.get("largura", 1280)),
        int(cam_cfg.get("altura", 720)),
        int(cam_cfg.get("fps", 30)),
        ajustes=cam_cfg,
    )
    if cap is None:
        raise SystemExit(f"Nao consegui abrir a camera {indice}.\n{_dica_camera()}")
    return cap, True


def main() -> int:
    p = argparse.ArgumentParser(description="Vigilancia de posicionamento nos dispensers.")
    p.add_argument("--camera", type=str, default=None, help="numero da camera ou parte do nome (ex: camo)")
    p.add_argument("--video", type=str, default=None)
    p.add_argument("--gravar", type=str, default=None, help="salva o video com overlay")
    p.add_argument("--sem-janela", action="store_true", help="modo headless")
    p.add_argument("--config-zonas", type=str, default=None)
    p.add_argument("--classico", action="store_true",
                   help="usa o overlay antigo em vez do painel Apsen")
    p.add_argument("--largura", type=int, default=None,
                   help="largura de processamento (px). Menor = mais FPS. 0 desliga")
    args = p.parse_args()

    parametros = carregar_parametros()
    cam_cfg = parametros.get("camera", {})
    det_cfg = parametros.get("deteccao", {})
    ale_cfg = parametros.get("alertas", {})
    ui_cfg = parametros.get("interface", {})

    catalogo = Catalogo.carregar()
    zonas = MapaZonas.carregar(args.config_zonas) if args.config_zonas else MapaZonas.carregar()

    for aviso in zonas.validar(catalogo):
        print(f"[aviso] {aviso}")

    leitor = LeitorCodigos(
        backend=det_cfg.get("backend", "auto"),
        escala=float(det_cfg.get("escala_processamento", 1.0)),
        melhorar_contraste=bool(det_cfg.get("melhorar_contraste", True)),
        cascata=bool(det_cfg.get("cascata", True)),
        cascata_completa=bool(det_cfg.get("cascata_completa", True)),
        usar_aruco=bool(det_cfg.get("usar_aruco", True)),
        dicionario_aruco=det_cfg.get("dicionario_aruco", "DICT_4X4_50"),
        ler_por_zona=bool(det_cfg.get("ler_por_zona", True)),
        margem_zona_px=int(det_cfg.get("margem_zona_px", 12)),
        lado_alvo_zona=int(det_cfg.get("lado_alvo_zona", 320)),
        lado_minimo_aruco=int(det_cfg.get("lado_minimo_aruco", 16)),
        limiar_mudanca=float(det_cfg.get("limiar_mudanca", 2.0)),
        intervalo_reavaliacao=float(det_cfg.get("intervalo_reavaliacao", 1.0)),
        intervalo_cascata_completa=float(det_cfg.get("intervalo_cascata_completa", 0.5)),
        intervalo_varredura=float(det_cfg.get("intervalo_varredura", 1.0)),
        orcamento_ms=float(det_cfg.get("orcamento_ms", 250.0)),
    )
    detector = DetectorDispensers(
        catalogo=catalogo,
        zonas=zonas,
        leitor=leitor,
        frames_para_confirmar=int(ale_cfg.get("frames_para_confirmar", 3)),
        cooldown_segundos=float(ale_cfg.get("cooldown_segundos", 10.0)),
        memoria_segundos=float(ale_cfg.get("memoria_segundos", 2.0)),
        margem_zona=float(det_cfg.get("margem_zona", 0.0)),
        alertar_qr_desconhecido=bool(ale_cfg.get("alertar_qr_desconhecido", True)),
        alertar_fora_de_zona=bool(ale_cfg.get("alertar_fora_de_zona", False)),
    )
    notificador = Notificador.a_partir_dos_parametros(parametros)
    fusao = MotorDeFusao(catalogo)

    largura_proc = (args.largura if args.largura is not None
                    else int(det_cfg.get("largura_processamento", 0)))
    painel = PainelApsen(largura=int(ui_cfg.get("largura", 1280)),
                         altura=int(ui_cfg.get("altura", 720)))
    medidas = Medidas()
    tela_cheia = bool(ui_cfg.get("tela_cheia", False))

    cap, e_camera = abrir_fonte(args, cam_cfg)
    espelhar = bool(cam_cfg.get("espelhar", False)) and e_camera
    gravador = None

    print("=" * 68)
    print("Sistema de visao - conferencia de medicamentos nos dispensers")
    print(f"  leitor de QR ....... {leitor.backend} (disponivel: {backend_disponivel()})")
    print(f"  dispensers ......... {', '.join(str(n) for n in catalogo.dispensers)}")
    print(f"  zonas calibradas ... {len(zonas.zonas)}")
    for med in catalogo.medicamentos:
        extra = f" + ArUco {med.aruco}" if med.aruco is not None else ""
        print(f"     dispenser {med.dispenser}: {med.nome}  [{med.qr}]{extra}")
    print("=" * 68)

    pausado = False
    classico = bool(args.classico)
    tempos: list[float] = []
    fps = 0.0
    frame_render = None
    ultimo_pesadas = 0
    janela_criada = False

    try:
        while True:
            if not pausado:
                inicio = time.monotonic()
                ok, bruto = cap.read()
                if not ok:
                    if args.video:
                        print("Fim do video.")
                    else:
                        print("Falha ao ler da camera.")
                    break
                frame = reduzir_frame(bruto, largura_proc)
                if espelhar:
                    frame = cv2.flip(frame, 1)
                t_captura = time.monotonic()

                resultado = detector.processar_frame(frame)
                notificador.notificar(resultado.alertas_novos)
                t_deteccao = time.monotonic()

                tempos.append(t_deteccao - inicio)
                if len(tempos) > 30:
                    tempos.pop(0)
                media = sum(tempos) / len(tempos)
                fps = 1.0 / media if media > 0 else 0.0

                medidas.captura_ms = (t_captura - inicio) * 1000
                medidas.deteccao_ms = (t_deteccao - t_captura) * 1000
                medidas.fps = fps
                medidas.frames += 1
                pesadas = getattr(leitor, "execucoes_pesadas", 0)
                if pesadas > ultimo_pesadas:
                    medidas.reavaliacoes += 1
                    ultimo_pesadas = pesadas

                if not args.sem_janela or args.gravar:
                    if classico:
                        frame_render = renderizar(frame, resultado, catalogo, True, fps)
                    else:
                        t_ui = time.monotonic()
                        zonas_vivas = resultado.zonas or zonas.para_resolucao(
                            frame.shape[1], frame.shape[0])
                        por_zona: dict[int, list] = {}
                        for oc in resultado.ocorrencias:
                            if oc.dispenser_detectado is not None:
                                por_zona.setdefault(oc.dispenser_detectado, []).append(oc)
                        vereditos = fusao.avaliar_frame(zonas_vivas, por_zona, {})
                        for r in vereditos:
                            if getattr(r, "critico", False):
                                painel.registrar_alerta(r.mensagem())
                                break
                        video = desenhar_zonas_no_video(frame.copy(), vereditos,
                                                        zonas_vivas)
                        frame_render = painel.compor(video, vereditos, catalogo, medidas)
                        medidas.interface_ms = (time.monotonic() - t_ui) * 1000

                if args.gravar and frame_render is not None:
                    if gravador is None:
                        h, w = frame_render.shape[:2]
                        gravador = cv2.VideoWriter(
                            args.gravar,
                            cv2.VideoWriter_fourcc(*"mp4v"),
                            float(cam_cfg.get("fps", 30)),
                            (w, h),
                        )
                    gravador.write(frame_render)

            if args.sem_janela:
                continue

            if frame_render is not None:
                try:
                    if not janela_criada:
                        cv2.namedWindow(JANELA, cv2.WINDOW_NORMAL)
                        cv2.resizeWindow(JANELA, painel.largura, painel.altura)
                        if tela_cheia:
                            cv2.setWindowProperty(JANELA, cv2.WND_PROP_FULLSCREEN,
                                                  cv2.WINDOW_FULLSCREEN)
                        janela_criada = True
                    cv2.imshow(JANELA, frame_render)
                except cv2.error:
                    # build do OpenCV sem suporte a janela: segue headless
                    args.sem_janela = True
                    continue

            k = cv2.waitKey(1) & 0xFF
            if k in (27, ord("q"), ord("Q")):
                break
            elif k in (ord("p"), ord("P")):
                pausado = not pausado
            elif k in (ord("z"), ord("Z")):
                classico = not classico
            elif k in (ord("f"), ord("F")):
                tela_cheia = not tela_cheia
                cv2.setWindowProperty(
                    JANELA, cv2.WND_PROP_FULLSCREEN,
                    cv2.WINDOW_FULLSCREEN if tela_cheia else cv2.WINDOW_NORMAL)
            elif k in (ord("e"), ord("E")) and frame_render is not None:
                destino = RAIZ / "logs" / f"print_{datetime.now():%Y%m%d_%H%M%S}.png"
                destino.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(destino), frame_render)
                print(f"Print salvo em {destino}")

    except KeyboardInterrupt:
        print("\nInterrompido pelo usuario.")
    finally:
        cap.release()
        if gravador is not None:
            gravador.release()
        notificador.fechar()
        if not args.sem_janela:
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass  # build do OpenCV sem suporte a janela (servidor/headless)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
