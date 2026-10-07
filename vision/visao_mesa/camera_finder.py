"""Descobre o indice da webcam externa e o backend que aceita travar os ajustes.

Rode ISTO primeiro. O indice da webcam externa nao e fixo: com notebook, a
camera integrada costuma ficar no 0 e a externa no 1, mas basta desconectar e
reconectar o USB para trocar. E o backend importa tanto quanto o indice —
no Windows, so o DSHOW expoe foco e exposicao da maioria das webcams; o MSMF
aceita o comando e ignora em silencio, que e o pior dos mundos.

    python camera_finder.py              # varre e mostra o relatorio
    python camera_finder.py --ver 1      # abre uma janela com o indice 1
    python camera_finder.py --salvar     # grava um PNG de cada camera achada

O que procurar no relatorio: a camera EXTERNA e quase sempre a de maior
resolucao nativa, e a integrada costuma ser 640x480 ou 1280x720. Se houver
duvida, use --ver e olhe a imagem.
"""

from __future__ import annotations

import argparse
import platform
from pathlib import Path

import cv2

MAX_INDICE = 10


def nomes_das_cameras() -> dict[int, str]:
    """Nome de cada camera por indice — so no Windows, via pygrabber.

    E a diferenca entre "provavelmente a de maior resolucao" e "a que se chama
    Logitech C925e". Sem isso o operador tem que abrir uma janela e olhar.
    pygrabber e opcional: se nao estiver instalado, o script continua e cai no
    palpite por resolucao.

    Ressalva honesta: a ordem que o pygrabber devolve costuma bater com o
    indice do OpenCV, mas isso nao e garantido pelo Windows. Confirme com
    --ver antes de confiar no nome.
    """
    if platform.system() != "Windows":
        return {}
    try:
        from pygrabber.dshow_graph import FilterGraph
    except Exception:
        return {}
    try:
        return dict(enumerate(FilterGraph().get_input_devices()))
    except Exception:
        return {}


def backends_do_sistema() -> list[tuple[str, int]]:
    sistema = platform.system()
    if sistema == "Windows":
        return [("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF), ("ANY", cv2.CAP_ANY)]
    if sistema == "Linux":
        return [("V4L2", cv2.CAP_V4L2), ("ANY", cv2.CAP_ANY)]
    return [("AVFOUNDATION", cv2.CAP_AVFOUNDATION), ("ANY", cv2.CAP_ANY)]


def testar(indice: int, nome_backend: str, backend: int) -> dict | None:
    cap = cv2.VideoCapture(indice, backend)
    if not cap.isOpened():
        cap.release()
        return None
    ok, frame = cap.read()
    if not ok or frame is None:
        cap.release()
        return None

    info = {
        "indice": indice,
        "backend": nome_backend,
        "resolucao_nativa": (frame.shape[1], frame.shape[0]),
        "fps": round(cap.get(cv2.CAP_PROP_FPS), 1),
    }

    # Tenta 1280x720 em MJPG: e o modo que a maioria das webcams entrega a 30
    # FPS. Sem MJPG o driver manda YUYV cru e o FPS despenca em 720p.
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    ok, frame = cap.read()
    if ok and frame is not None:
        info["resolucao_720p"] = (frame.shape[1], frame.shape[0])

    # O teste que importa: o driver aceita DESLIGAR os automatismos?
    manual = 1 if platform.system() == "Linux" else 0.25
    cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, manual)
    cap.set(cv2.CAP_PROP_AUTO_WB, 0)
    info["autofoco_desligou"] = cap.get(cv2.CAP_PROP_AUTOFOCUS) in (0, 0.0)
    info["autoexposicao_desligou"] = abs(cap.get(cv2.CAP_PROP_AUTO_EXPOSURE) - manual) < 0.1
    info["autowb_desligou"] = cap.get(cv2.CAP_PROP_AUTO_WB) in (0, 0.0)
    info["frame"] = frame if ok else None
    cap.release()
    return info


def varrer() -> list[dict]:
    achadas = []
    for indice in range(MAX_INDICE):
        for nome, backend in backends_do_sistema():
            info = testar(indice, nome, backend)
            if info:
                achadas.append(info)
                break  # primeiro backend que funciona neste indice
    return achadas


def relatorio(achadas: list[dict]) -> None:
    if not achadas:
        print("Nenhuma camera encontrada.\n"
              "  - confira o cabo USB e troque de porta (evite hub sem fonte)\n"
              "  - feche Teams, Meet, OBS e qualquer app que prenda a camera\n"
              "  - no Windows: Configuracoes > Privacidade > Camera, permitir apps")
        return

    nomes = nomes_das_cameras()
    largura_nome = max([len(n) for n in nomes.values()] + [6]) if nomes else 0
    coluna_nome = f"  {'camera':{largura_nome}}" if nomes else ""

    print(f"\n{len(achadas)} camera(s) encontrada(s):\n")
    print(f"{'idx':>3}  {'backend':8}{coluna_nome}  {'nativa':>11}  "
          f"{'720p':>11}  {'fps':>5}  travas")
    print("-" * (68 + largura_nome + (2 if nomes else 0)))
    for info in achadas:
        n = "x".join(map(str, info["resolucao_nativa"]))
        s = "x".join(map(str, info.get("resolucao_720p", ("-", "-"))))
        travas = "".join([
            "F" if info["autofoco_desligou"] else "-",
            "E" if info["autoexposicao_desligou"] else "-",
            "W" if info["autowb_desligou"] else "-",
        ])
        nome = f"  {nomes.get(info['indice'], '?'):{largura_nome}}" if nomes else ""
        print(f"{info['indice']:>3}  {info['backend']:8}{nome}  {n:>11}  {s:>11}  "
              f"{info['fps']:>5}  {travas}")
    print("\ntravas: F = autofoco desligou, E = autoexposicao, W = white balance.")
    print("        '-' significa que o driver IGNOROU o comando nesse backend.")

    # Palpite: a webcam externa e a de maior resolucao nativa. A integrada de
    # notebook costuma ser 640x480 ou 1280x720.
    externa = max(achadas, key=lambda i: i["resolucao_nativa"][0] * i["resolucao_nativa"][1])
    etiqueta = nomes.get(externa["indice"])
    print(f"\nProvavel webcam externa: indice {externa['indice']} "
          f"({externa['backend']}, {'x'.join(map(str, externa['resolucao_nativa']))}"
          + (f", \"{etiqueta}\"" if etiqueta else "") + ").")
    if not nomes and platform.system() == "Windows":
        print("  Dica: 'pip install pygrabber' faz esta lista mostrar o NOME de")
        print("  cada camera, em vez de adivinhar pela resolucao.")
    print("  Confirme com:  python camera_finder.py --ver "
          f"{externa['indice']}")
    print(f"Coloque em config/mesa.json:  \"camera\": {{ \"indice\": {externa['indice']} }}")
    if not externa["autoexposicao_desligou"]:
        print("\nATENCAO: a autoexposicao nao desligou nesta camera. Numa celula com\n"
              "  braco robotico entrando no quadro, a imagem vai 'respirar' e a\n"
              "  calibragem de limiar perde validade. Tente outro backend ou outra\n"
              "  camera antes de calibrar.")


def visualizar(indice: int) -> None:
    for nome, backend in backends_do_sistema():
        cap = cv2.VideoCapture(indice, backend)
        if cap.isOpened():
            print(f"Indice {indice} aberto com {nome}. 'q' para sair.")
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                cv2.putText(frame, f"indice {indice} / {nome}", (10, 28),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (35, 190, 232), 2)
                cv2.imshow("camera_finder", frame)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
            cap.release()
            cv2.destroyAllWindows()
            return
    print(f"Nao consegui abrir o indice {indice}.")


def main() -> None:
    p = argparse.ArgumentParser(description="Encontra o indice da webcam externa.")
    p.add_argument("--ver", type=int, help="abre uma janela com este indice")
    p.add_argument("--salvar", action="store_true", help="grava um PNG de cada camera")
    args = p.parse_args()

    if args.ver is not None:
        visualizar(args.ver)
        return

    achadas = varrer()
    relatorio(achadas)

    if args.salvar:
        destino = Path(__file__).resolve().parent / "dados"
        destino.mkdir(exist_ok=True)
        for info in achadas:
            if info.get("frame") is not None:
                caminho = destino / f"camera_{info['indice']}_{info['backend']}.png"
                cv2.imwrite(str(caminho), info["frame"])
                print(f"  salvo {caminho}")


if __name__ == "__main__":
    main()
