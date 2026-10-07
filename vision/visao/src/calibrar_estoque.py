"""Calibracao da contagem de estoque.

Duas coisas precisam ser ensinadas uma unica vez por dispenser:

  1. como a prateleira se parece VAZIA  (referencia para achar o topo da pilha)
  2. a altura de uma caixa em pixels    (deduzida de uma pilha de quantidade
                                         conhecida — voce conta uma vez, o
                                         sistema conta para sempre)

Uso:
    python src/calibrar_estoque.py                 # assistente ao vivo
    python src/calibrar_estoque.py --conferir      # so mostra a leitura atual

Teclas:
    V           grava a zona selecionada como VAZIA
    1..9        informa quantas caixas ha na zona selecionada e aprende a altura
    TAB         muda a zona selecionada
    R           limpa a reposicao (limiar) da zona selecionada
    ENTER       salva
    ESC         sai sem salvar
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camera import abrir_camera  # noqa: E402
from configuracao import Catalogo, MapaZonas, carregar_parametros  # noqa: E402
from contagem import ContadorDeEstoque  # noqa: E402

JANELA = "Calibracao de estoque"
FONTE = cv2.FONT_HERSHEY_SIMPLEX


def main() -> int:
    p = argparse.ArgumentParser(description="Calibra a contagem de estoque.")
    p.add_argument("--camera", type=int, default=None)
    p.add_argument("--video", type=str, default=None)
    p.add_argument("--conferir", action="store_true", help="so mostra a leitura atual")
    args = p.parse_args()

    parametros = carregar_parametros()
    cam_cfg = parametros.get("camera", {})
    catalogo = Catalogo.carregar()

    try:
        zonas = MapaZonas.carregar()
    except FileNotFoundError:
        print("Calibre as zonas primeiro: python src/calibrar.py")
        return 1

    contador = ContadorDeEstoque()
    contador.carregar()

    if args.video:
        cap = cv2.VideoCapture(args.video)
    else:
        indice = args.camera if args.camera is not None else int(cam_cfg.get("indice", 0))
        cap = abrir_camera(indice, int(cam_cfg.get("largura", 1280)),
                           int(cam_cfg.get("altura", 720)),
                           int(cam_cfg.get("fps", 30)), ajustes=cam_cfg)
    if cap is None or not cap.isOpened():
        print("Camera indisponivel.")
        return 1

    numeros = [z.dispenser for z in zonas.zonas]
    selecionada = numeros[0]
    mensagem = "V grava a prateleira vazia | 1..9 informa quantas caixas ha agora"

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        vivas = zonas.para_resolucao(frame.shape[1], frame.shape[0])
        tela = frame.copy()

        for zona in vivas.zonas:
            nivel = contador.medir(frame, zona)
            calib = contador.pilhas.get(zona.dispenser)
            ativa = zona.dispenser == selecionada
            cor = (90, 220, 120) if ativa else (150, 150, 150)
            cv2.rectangle(tela, (zona.x, zona.y), (zona.x2, zona.y2), cor,
                          3 if ativa else 1, cv2.LINE_AA)

            # marca a altura ocupada estimada
            topo = int(zona.y2 - nivel.altura_px)
            if nivel.altura_px > 2:
                cv2.line(tela, (zona.x, topo), (zona.x2, topo), (247, 169, 90), 2)

            med = catalogo.esperado_em(zona.dispenser)
            linhas = [
                f"D{zona.dispenser} {med.nome[:18] if med else ''}",
                (f"{nivel.caixas} cx" if nivel.caixas is not None
                 else f"{nivel.fracao * 100:.0f}%"),
                ("vazio: OK" if zona.dispenser in contador.vazios else "vazio: FALTA"),
                (f"altura: {calib.altura_caixa_px:.0f}px" if calib and calib.calibrada
                 else "altura: FALTA"),
                f"conf: {nivel.confianca:.2f}",
            ]
            y = zona.y + 20
            cv2.rectangle(tela, (zona.x, zona.y), (zona.x + 190, zona.y + 5 + 19 * len(linhas)),
                          (18, 18, 18), -1)
            for i, texto in enumerate(linhas):
                cv2.putText(tela, texto, (zona.x + 6, y + i * 19), FONTE, 0.45,
                            (235, 235, 235) if i else cor, 1, cv2.LINE_AA)

        cv2.rectangle(tela, (0, tela.shape[0] - 54), (tela.shape[1], tela.shape[0]),
                      (18, 18, 18), -1)
        cv2.putText(tela, f"zona {selecionada} selecionada (TAB muda)",
                    (14, tela.shape[0] - 32), FONTE, 0.5, (90, 220, 120), 1, cv2.LINE_AA)
        cv2.putText(tela, mensagem, (14, tela.shape[0] - 12), FONTE, 0.45,
                    (200, 200, 200), 1, cv2.LINE_AA)
        cv2.imshow(JANELA, tela)

        k = cv2.waitKey(20) & 0xFF
        zona = vivas.por_dispenser(selecionada)
        if k in (27, ord("q")):
            print("saindo sem salvar")
            break
        if k == 9:
            selecionada = numeros[(numeros.index(selecionada) + 1) % len(numeros)]
        elif k in (ord("v"), ord("V")) and zona:
            contador.registrar_vazio(frame, zona)
            mensagem = f"prateleira vazia do dispenser {selecionada} registrada"
            print(f"  {mensagem}")
        elif ord("1") <= k <= ord("9") and zona:
            quantidade = k - ord("0")
            altura = contador.aprender_altura(frame, zona, quantidade)
            if altura > 1:
                capacidade = contador.pilhas[selecionada].capacidade
                mensagem = (f"dispenser {selecionada}: {quantidade} caixas -> "
                            f"{altura:.0f} px por caixa (cabem ~{capacidade})")
            else:
                mensagem = "nao consegui medir a pilha; registre a prateleira vazia antes (V)"
            print(f"  {mensagem}")
        elif k in (ord("r"), ord("R")) and zona:
            calib = contador.pilhas.get(selecionada)
            if calib:
                calib.limiar_reposicao = (calib.limiar_reposicao + 1) % 6
                mensagem = f"avisar reposicao com {calib.limiar_reposicao} caixa(s) ou menos"
        elif k in (13, 10):
            contador.salvar()
            faltando = [z.dispenser for z in vivas.zonas
                        if z.dispenser not in contador.vazios]
            if faltando:
                print(f"[aviso] sem foto da prateleira vazia em: {faltando} "
                      "— a contagem sai com confianca baixa nesses")
            print("Calibracao de estoque salva em config/pilhas.json")
            break

    cap.release()
    try:
        cv2.destroyAllWindows()
    except cv2.error:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
