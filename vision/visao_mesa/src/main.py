"""Execucao ao vivo da visao da mesa.

    python src/main.py                 # janela com bounding box e contagem
    python src/main.py --sem-janela    # so terminal (para rodar como servico)
    python src/main.py --uma-vez       # uma leitura, imprime JSON e sai
    python src/main.py --json          # uma linha JSON por leitura estavel

Arquitetura em tres estagios independentes (ver src/fluxo.py): a camera e lida
numa thread, a deteccao roda em outra, e o laco principal so desenha. E o que
mantem o video liso mesmo quando uma busca de limiar leva 200 ms — ela acontece
fora do caminho do desenho.

A contagem so e publicada depois de ficar ESTAVEL por alguns frames. Sem essa
histerese, a mao do operador atravessando o quadro vira uma leitura a menos e,
logo em seguida, uma a mais — dois eventos falsos por gesto.

Uma leitura recusada sai com "quantidade": null. Isso NAO e zero: zero e caixa
vazia, null e "a imagem nao permite afirmar". Quem consome precisa tratar os
dois casos diferente — inventar um numero aqui viraria divergencia com o peso e
com o ciclo da CNC, ou pior, passaria batido.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from pathlib import Path

import cv2

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from camera import Camera  # noqa: E402
from desenho import (  # noqa: E402
    desenhar_conteudo, desenhar_fundo, empilhar, tamanho_tela,
)
from fluxo import FluxoVisao  # noqa: E402
from visao_mesa import ConfigMesa, VisaoMesa, retificar  # noqa: E402

JANELA = "Visao da mesa"


class Estabilizador:
    """Publica a contagem so quando ela para de mudar.

    Guarda as ultimas N leituras e exige que a maioria concorde. Uma leitura
    recusada (None) tambem precisa de maioria para virar 'indeterminado' — caso
    contrario um unico frame com reflexo derrubaria uma leitura boa.
    """

    def __init__(self, janela: int = 8, minimo_iguais: int = 6):
        self.historico: deque = deque(maxlen=janela)
        self.minimo = minimo_iguais
        self.publicado = None
        self.mudou = False

    def atualizar(self, valor):
        self.historico.append(valor)
        self.mudou = False
        if len(self.historico) < self.minimo:
            return self.publicado
        candidato = self.historico[-1]
        iguais = sum(1 for v in self.historico if v == candidato)
        if iguais >= self.minimo and candidato != self.publicado:
            self.publicado = candidato
            self.mudou = True
        return self.publicado


def _uma_leitura(cfg: ConfigMesa) -> None:
    """Caminho pontual: sem threads, com mediana de frames para baixar ruido."""
    cam = Camera(cfg.camera).abrir()
    try:
        resultado = VisaoMesa(cfg).processar(cam.ler_estavel(3))
        print(json.dumps(resultado.para_dict(), ensure_ascii=False, indent=2))
    finally:
        cam.fechar()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--sem-janela", action="store_true")
    p.add_argument("--uma-vez", action="store_true")
    p.add_argument("--json", action="store_true", help="uma linha JSON por leitura")
    p.add_argument("--indice", type=int)
    args = p.parse_args()

    cfg = ConfigMesa.carregar()
    if args.indice is not None:
        cfg.camera.indice = args.indice

    if args.uma_vez:
        _uma_leitura(cfg)
        return

    cam = Camera(cfg.camera).abrir()
    fluxo = FluxoVisao(cam, VisaoMesa(cfg))
    estabilizador = Estabilizador()
    mostrar = not (args.sem_janela or args.json)

    tela = tamanho_tela()
    tamanho_janela: tuple[int, int] | None = None
    if mostrar:
        print("Visao da mesa em execucao. 'q' fecha a janela, Ctrl+C encerra.")
        # NORMAL para poder redimensionar; KEEPRATIO para que arrastar a borda
        # nao ACHATE a imagem — a vista esta em escala mm, e distorcer faria uma
        # embalagem de 77x34mm parecer outra proporcao.
        cv2.namedWindow(JANELA, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
        cv2.moveWindow(JANELA, 0, 0)

    def mostrar_janela(img) -> None:
        """Mostra e acerta o tamanho da janela quando a composicao muda.

        So na mudanca: chamar resizeWindow a cada frame faz a janela piscar e
        ignora o operador que arrastou a borda para outro tamanho.
        """
        nonlocal tamanho_janela
        alvo = (img.shape[1], img.shape[0])
        if alvo != tamanho_janela:
            tamanho_janela = alvo
            cv2.resizeWindow(JANELA, alvo[0], alvo[1])
        cv2.imshow(JANELA, img)

    try:
        fluxo.iniciar()
        fluxo.processador.esperar_primeiro()
        ultimo_desenhado = -1

        while True:
            frame, contador, _momento = fluxo.leitor.ultimo()
            resultado, _frame_usado, idade = fluxo.processador.ultimo()

            if resultado is not None:
                estavel = estabilizador.atualizar(resultado.contagem)
                if args.json and estabilizador.mudou:
                    registro = resultado.para_dict()
                    registro["quantidade"] = estavel
                    registro["momento"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    print(json.dumps(registro, ensure_ascii=False), flush=True)

            if not mostrar:
                time.sleep(0.02)
                continue

            # So redesenha quando ha frame novo: sem isto o laco gira a centenas
            # de FPS queimando o nucleo que a deteccao precisa.
            if frame is None or contador == ultimo_desenhado or resultado is None:
                if cv2.waitKey(5) & 0xFF in (ord("q"), 27):
                    break
                continue
            ultimo_desenhado = contador

            painel_fundo = desenhar_fundo(frame, resultado)
            if resultado.fundo is None:
                mostrar_janela(empilhar(painel_fundo, tamanho=tela))
            else:
                # O frame desenhado e o AO VIVO, retificado pela homografia da
                # ultima deteccao. Como o fundo da caixa e estatico, a imagem
                # fica correta e o video nao espera o processamento.
                vista = retificar(frame, resultado.fundo)
                valor = "--" if estabilizador.publicado is None else str(estabilizador.publicado)
                painel = desenhar_conteudo(
                    vista, resultado,
                    f"estavel={valor}  cam {fluxo.leitor.fps:.0f} FPS  "
                    f"visao {fluxo.processador.fps:.1f} Hz  atraso {idade * 1000:.0f} ms",
                )
                # Em cima o quadro da camera com o quadrilatero; embaixo a vista
                # retificada com as bounding box. Empilhado e nao lado a lado:
                # as duas vistas sao mais largas que altas, entao o que falta
                # numa tela de notebook e largura, nao altura.
                mostrar_janela(empilhar(painel_fundo, painel, tamanho=tela))

            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break
    except KeyboardInterrupt:
        pass
    finally:
        fluxo.parar()
        cam.fechar()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
