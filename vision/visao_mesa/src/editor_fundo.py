"""Editor de mouse: marcar os quatro cantos do fundo da caixa na mao.

Existe por um motivo pratico. O detector automatico (modo 'caixa') precisa de
contraste entre o fundo do recipiente e a borda dele; numa caixa de papelao com
embalagem de papelao dentro, esse contraste nao existe e o contorno nunca fecha.
O modo 'mesa' contorna isso, mas cobra o preco de perder a correcao de
perspectiva e a escala automatica.

Marcar os quatro cantos com o mouse resolve os dois de uma vez: quatro pontos
dao a homografia (perspectiva corrigida) e, junto com as dimensoes reais da
caixa, dao a escala em mm — sem depender de contraste nenhum. E trabalho de uma
vez so: enquanto a camera e a caixa nao se mexerem, a marcacao continua valendo.

Detalhes que parecem menores e nao sao:

  * os cantos sao guardados em FRACAO do quadro (0..1), nao em pixel. Trocar a
    resolucao da camera de 1280x720 para 1920x1080 nao invalida a marcacao.
  * as coordenadas do mouse sao convertidas pela escala que ESTE modulo aplicou
    na imagem exibida. Confiar no OpenCV para fazer essa conversao em janela
    WINDOW_NORMAL da resultado diferente por versao e por sistema.
  * na hora de salvar, os pontos sao REORDENADOS para TL, TR, BR, BL. Se o
    operador arrastar um canto por cima do outro e a ordem inverter, a vista
    retificada sai espelhada ou de cabeca para baixo — e o erro aparece so
    depois, na contagem.
"""

from __future__ import annotations

import cv2
import numpy as np

from desenho import DOURADO, FONTE, PRETO, VERDE, barra, texto_com_fundo

JANELA_EDITOR = "Marcar o fundo da caixa — mouse"

RAIO_ALCA = 9          # raio do circulo desenhado em cada canto
RAIO_PEGADA = 26       # distancia maxima, em pixel de tela, para pegar um canto

AJUDA = [
    "Arraste os 4 cantos ate as quinas do fundo da caixa",
    "ENTER ou 's' aplica e salva     'r' volta ao retangulo padrao",
    "ESC cancela e mantem o que estava antes",
]


def _ordenar(pontos: np.ndarray) -> np.ndarray:
    """Devolve os quatro pontos na ordem TL, TR, BR, BL.

    Ordena pelo angulo em volta do centro (com y para baixo, angulo crescente e
    o sentido horario na tela) e depois gira a lista para comecar pelo canto
    mais proximo da origem. Assim a ordem fica certa mesmo que o operador tenha
    arrastado os cantos fora de sequencia.
    """
    centro = pontos.mean(axis=0)
    angulo = np.arctan2(pontos[:, 1] - centro[1], pontos[:, 0] - centro[0])
    p = pontos[np.argsort(angulo)]
    return np.roll(p, -int(np.argmin(p.sum(axis=1))), axis=0)


class _Arraste:
    """Estado do mouse. Separado para o callback nao virar closure gigante."""

    def __init__(self, pontos: np.ndarray):
        self.pontos = pontos          # em pixel do FRAME, nao da tela
        self.escala = 1.0             # tela = frame * escala
        self.indice = -1              # canto sendo arrastado, -1 = nenhum

    def callback(self, evento, x, y, _flags, _param) -> None:
        alvo = np.array([x / self.escala, y / self.escala], dtype=np.float32)
        if evento == cv2.EVENT_LBUTTONDOWN:
            distancias = np.linalg.norm(self.pontos - alvo, axis=1) * self.escala
            mais_perto = int(np.argmin(distancias))
            # Longe de qualquer canto o clique e ignorado, em vez de teleportar
            # o canto mais proximo: um clique acidental no meio da imagem nao
            # deve desfazer uma marcacao pronta.
            if distancias[mais_perto] <= RAIO_PEGADA:
                self.indice = mais_perto
        elif evento == cv2.EVENT_MOUSEMOVE and self.indice >= 0:
            self.pontos[self.indice] = alvo
        elif evento in (cv2.EVENT_LBUTTONUP, cv2.EVENT_RBUTTONUP):
            self.indice = -1


def _previa(frame: np.ndarray, pontos: np.ndarray, cfg_fundo,
            altura: int) -> np.ndarray | None:
    """Vista retificada pelos cantos atuais — o retorno visual que importa.

    Sem ela o operador so ve se o quadrilatero "parece" alinhado. Com ela ve o
    resultado: se as embalagens sairem tortas ou esticadas aqui, a marcacao
    ainda esta errada, mesmo que na imagem original pareca certa.
    """
    try:
        escala = float(cfg_fundo.px_por_mm)
        largura = max(2, int(round(cfg_fundo.comprimento_mm * escala)))
        alta = max(2, int(round(cfg_fundo.largura_mm * escala)))
        destino = np.array([[0, 0], [largura - 1, 0],
                            [largura - 1, alta - 1], [0, alta - 1]],
                           dtype=np.float32)
        H = cv2.getPerspectiveTransform(_ordenar(pontos.astype(np.float32)), destino)
        vista = cv2.warpPerspective(frame, H, (largura, alta))
    except cv2.error:
        return None
    fator = altura / vista.shape[0]
    vista = cv2.resize(vista, (max(1, int(vista.shape[1] * fator)), altura))
    barra(vista, f"PREVIA retificada  {cfg_fundo.comprimento_mm:.0f}x"
                 f"{cfg_fundo.largura_mm:.0f}mm", VERDE, altura=26, escala=0.45)
    return vista


def _montar(frame: np.ndarray, arraste: _Arraste, cfg_fundo,
            altura_tela: int) -> np.ndarray:
    fator = altura_tela / frame.shape[0]
    arraste.escala = fator
    tela = cv2.resize(frame, (max(1, int(frame.shape[1] * fator)), altura_tela))

    pontos_tela = (arraste.pontos * fator).astype(np.int32)
    cv2.polylines(tela, [pontos_tela], True, DOURADO, 2, cv2.LINE_AA)
    for indice, (px, py) in enumerate(pontos_tela):
        pego = indice == arraste.indice
        cv2.circle(tela, (int(px), int(py)), RAIO_ALCA + (3 if pego else 0),
                   VERDE if pego else DOURADO, -1, cv2.LINE_AA)
        cv2.circle(tela, (int(px), int(py)), RAIO_ALCA + 6, PRETO, 1, cv2.LINE_AA)
        texto_com_fundo(tela, "TL TR BR BL".split()[indice],
                        (int(px) + 12, int(py) - 8), VERDE if pego else DOURADO)

    barra(tela, "MARCAR O FUNDO DA CAIXA  (modo manual)", DOURADO, altura=30)
    y = tela.shape[0] - 8 - 20 * len(AJUDA)
    cv2.rectangle(tela, (0, y - 14), (tela.shape[1], tela.shape[0]), PRETO, -1)
    for linha in AJUDA:
        cv2.putText(tela, linha, (10, y), FONTE, 0.45, (235, 235, 235), 1, cv2.LINE_AA)
        y += 20

    previa = _previa(frame, arraste.pontos, cfg_fundo, altura_tela)
    return tela if previa is None else np.hstack([tela, previa])


def editar_cantos(cfg, obter_frame, altura_tela: int = 620) -> bool:
    """Abre o editor. Devolve True se o operador aplicou a marcacao.

    `obter_frame` e uma funcao sem argumento que devolve o frame mais novo (ou
    None). Passando a funcao, e nao a imagem, o editor mostra video ao vivo e o
    operador pode ajustar a caixa na bancada enquanto marca.

    Quem chama e responsavel por sincronizar o slider de modo e gravar o
    arquivo — o editor so mexe no objeto de config.
    """
    frame = obter_frame()
    if frame is None:
        print("[editor] sem frame da camera; nada a marcar.")
        return False

    altura, largura = frame.shape[:2]
    pontos = np.array([[float(x) * largura, float(y) * altura]
                       for x, y in list(cfg.fundo.cantos_manuais)[:4]],
                      dtype=np.float32)
    if len(pontos) != 4:
        pontos = np.array([[0.1 * largura, 0.1 * altura], [0.9 * largura, 0.1 * altura],
                           [0.9 * largura, 0.9 * altura], [0.1 * largura, 0.9 * altura]],
                          dtype=np.float32)

    arraste = _Arraste(pontos)
    cv2.namedWindow(JANELA_EDITOR, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(JANELA_EDITOR, arraste.callback)

    aplicado = False
    try:
        while True:
            novo = obter_frame()
            if novo is not None:
                frame = novo
            # Limita ao quadro: um canto arrastado para fora viraria homografia
            # com ponto negativo, e a previa quebraria sem dizer por que.
            np.clip(arraste.pontos[:, 0], 0, largura - 1, out=arraste.pontos[:, 0])
            np.clip(arraste.pontos[:, 1], 0, altura - 1, out=arraste.pontos[:, 1])

            cv2.imshow(JANELA_EDITOR, _montar(frame, arraste, cfg.fundo, altura_tela))

            tecla = cv2.waitKey(15) & 0xFF
            if tecla in (27, ord("q")):
                break
            if tecla in (13, 10, ord("s")):
                ordenados = _ordenar(arraste.pontos.astype(np.float32))
                cfg.fundo.cantos_manuais = tuple(
                    (float(x) / largura, float(y) / altura) for x, y in ordenados
                )
                cfg.fundo.modo = "manual"
                aplicado = True
                break
            if tecla == ord("r"):
                arraste.pontos[:] = np.array(
                    [[0.1 * largura, 0.1 * altura], [0.9 * largura, 0.1 * altura],
                     [0.9 * largura, 0.9 * altura], [0.1 * largura, 0.9 * altura]],
                    dtype=np.float32,
                )
    finally:
        cv2.destroyWindow(JANELA_EDITOR)
    return aplicado
