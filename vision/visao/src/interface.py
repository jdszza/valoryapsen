"""Interface de operacao, com a identidade visual da Apsen.

Paleta oficial da marca (fonte: brandfetch.com/apsen.com.br):
    azul Apsen   #2577BE
    cinza Apsen  #54595F
    branco       #FFFFFF

As cores de status (verde/ambar/vermelho) NAO sao cores de marca — sao
semanticas, escolhidas para conviver com o azul institucional sem competir com
ele e para manter contraste legivel a distancia, que e como o operador vai
olhar para esta tela: de pe, a um metro, de relance.

A tela e composta num tamanho fixo (padrao 1280x720) independentemente da
resolucao da camera. Isso resolve dois problemas de uma vez: a janela para de
mudar de tamanho conforme a camera, e o custo de desenhar deixa de crescer com
a resolucao da fonte.
"""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass, field

import cv2
import numpy as np

FONTE = cv2.FONT_HERSHEY_SIMPLEX
FONTE_TITULO = cv2.FONT_HERSHEY_DUPLEX

# --- marca ---------------------------------------------------------------- #
AZUL = (190, 119, 37)          # #2577BE em BGR
AZUL_ESCURO = (140, 88, 27)
AZUL_CLARO = (240, 226, 208)
CINZA = (95, 89, 84)           # #54595F em BGR
CINZA_MEDIO = (150, 146, 142)
CINZA_CLARO = (238, 238, 238)
BRANCO = (255, 255, 255)
FUNDO = (250, 249, 248)

# --- status (semantico, nao e cor de marca) -------------------------------- #
VERDE = (108, 176, 46)
AMBAR = (40, 170, 235)
VERMELHO = (58, 58, 214)
ROXO = (190, 70, 150)          # divergencia: nem erro comum, nem alerta comum

CORES_STATUS = {
    "OK": VERDE,
    "ERRO_POSICAO": VERMELHO,
    "DIVERGENCIA": ROXO,
    "NAO_CADASTRADO": VERMELHO,
    "VAZIO": CINZA_MEDIO,
    "INDETERMINADO": AMBAR,
}

ROTULO_STATUS = {
    "OK": "CONFERIDO",
    "ERRO_POSICAO": "FORA DE LUGAR",
    "DIVERGENCIA": "DIVERGENCIA",
    "NAO_CADASTRADO": "NAO CADASTRADO",
    "VAZIO": "VAZIO",
    "INDETERMINADO": "VERIFICAR",
}


@dataclass
class Medidas:
    """Quanto tempo cada etapa levou — o que responde 'por que o FPS caiu'."""

    captura_ms: float = 0.0
    deteccao_ms: float = 0.0
    interface_ms: float = 0.0
    fps: float = 0.0
    reavaliacoes: int = 0     # quantas vezes a leitura pesada rodou
    frames: int = 0

    @property
    def total_ms(self) -> float:
        return self.captura_ms + self.deteccao_ms + self.interface_ms

    @property
    def gargalo(self) -> str:
        partes = {"captura": self.captura_ms, "deteccao": self.deteccao_ms,
                  "interface": self.interface_ms}
        return max(partes, key=partes.get)


# --------------------------------------------------------------------------- #
_SUBSTITUICOES = {"→": "->", "←": "<-", "–": "-", "—": "-",
                  "‘": "'", "’": "'", "“": '"', "”": '"',
                  "…": "...", "°": "o", "º": "o", "ª": "a"}


def limpar(texto: str) -> str:
    """Deixa o texto no ASCII que o Hershey do OpenCV sabe desenhar.

    Sem isto, acento e seta viram '?' na tela — e o texto do alerta vem do
    pipeline, onde nao ha como garantir que so haja ASCII.
    """
    texto = str(texto)
    for de, para in _SUBSTITUICOES.items():
        texto = texto.replace(de, para)
    texto = unicodedata.normalize("NFKD", texto)
    return texto.encode("ascii", "ignore").decode("ascii")


def largura_texto(texto, escala=0.5, espessura=1, fonte=FONTE) -> int:
    return int(cv2.getTextSize(limpar(texto), fonte, escala, espessura)[0][0])


def encaixar(texto, largura_px, escala=0.5, espessura=1, fonte=FONTE) -> str:
    """Corta o texto para caber em largura_px, terminando em reticencias."""
    texto = limpar(texto)
    if largura_texto(texto, escala, espessura, fonte) <= largura_px:
        return texto
    corte = texto
    while corte and largura_texto(corte + "...", escala, espessura, fonte) > largura_px:
        corte = corte[:-1]
    return (corte.rstrip() + "...") if corte else ""


def _texto(img, texto, pos, escala=0.5, cor=CINZA, espessura=1, fonte=FONTE):
    cv2.putText(img, limpar(texto), pos, fonte, escala, cor, espessura, cv2.LINE_AA)


def reduzir_frame(frame: np.ndarray, largura_alvo: int) -> np.ndarray:
    """Leva o frame a resolucao de trabalho.

    E a alavanca mais direta de FPS: o custo de quase tudo depois daqui e
    proporcional ao numero de pixels. Reduzir de 1080p para 960 px de largura
    corta ~68% dos pixels antes de qualquer processamento.
    """
    if largura_alvo <= 0 or frame.shape[1] <= largura_alvo:
        return frame
    escala = largura_alvo / frame.shape[1]
    return cv2.resize(frame, (largura_alvo, max(1, int(frame.shape[0] * escala))),
                      interpolation=cv2.INTER_AREA)


def _retangulo_arredondado(img, p1, p2, cor, raio=8, espessura=-1):
    x1, y1 = p1
    x2, y2 = p2
    raio = max(0, min(raio, (x2 - x1) // 2, (y2 - y1) // 2))
    if espessura < 0:
        cv2.rectangle(img, (x1 + raio, y1), (x2 - raio, y2), cor, -1)
        cv2.rectangle(img, (x1, y1 + raio), (x2, y2 - raio), cor, -1)
        for cx, cy in ((x1 + raio, y1 + raio), (x2 - raio, y1 + raio),
                       (x1 + raio, y2 - raio), (x2 - raio, y2 - raio)):
            cv2.circle(img, (cx, cy), raio, cor, -1, cv2.LINE_AA)
    else:
        cv2.rectangle(img, p1, p2, cor, espessura, cv2.LINE_AA)


def _pilula(img, texto, pos, cor_fundo, cor_texto=BRANCO, escala=0.42, alt=22):
    (w, _), _ = cv2.getTextSize(texto, FONTE, escala, 1)
    x, y = pos
    _retangulo_arredondado(img, (x, y), (x + w + 20, y + alt), cor_fundo, alt // 2)
    _texto(img, texto, (x + 10, y + alt - 7), escala, cor_texto, 1)
    return w + 20


def desenhar_logo(img, x, y, altura=26):
    """Assinatura da marca desenhada em vetor — sem depender de arquivo externo.

    Nao e o logotipo oficial: e uma marca-texto no padrao da identidade, para o
    piloto nao carregar um asset que nao temos licenca para redistribuir. Trocar
    pelo SVG oficial e so substituir esta funcao.
    """
    escala = altura / 26.0
    _texto(img, "APSEN", (x, y + int(20 * escala)), 0.85 * escala, BRANCO, 2, FONTE_TITULO)
    largura_marca = int(cv2.getTextSize("APSEN", FONTE_TITULO, 0.85 * escala, 2)[0][0])

    # o sol do logo novo: circulo com raios, simbolo de renovacao
    cx = x + largura_marca + int(16 * escala)
    cy = y + int(12 * escala)
    r = int(7 * escala)
    cv2.circle(img, (cx, cy), r, BRANCO, -1, cv2.LINE_AA)
    for k in range(8):
        ang = k * np.pi / 4
        p1 = (int(cx + np.cos(ang) * (r + 3 * escala)),
              int(cy + np.sin(ang) * (r + 3 * escala)))
        p2 = (int(cx + np.cos(ang) * (r + 7 * escala)),
              int(cy + np.sin(ang) * (r + 7 * escala)))
        cv2.line(img, p1, p2, BRANCO, max(1, int(2 * escala)), cv2.LINE_AA)
    return cx + r + int(10 * escala)


# --------------------------------------------------------------------------- #
class PainelApsen:
    """Compoe a tela de operacao num tamanho fixo."""

    ALTURA_CABECALHO = 62
    ALTURA_RODAPE = 34
    LARGURA_LATERAL = 380

    def __init__(self, largura: int = 1280, altura: int = 720,
                 estacao: str = "bancada", titulo: str = "Conferencia de Dispensers"):
        self.largura = int(largura)
        self.altura = int(altura)
        self.estacao = estacao
        self.titulo = titulo
        self._ultimo_alerta: str | None = None
        self._momento_alerta = 0.0
        self._tela: np.ndarray | None = None

    # ------------------------------------------------------------------ #
    @property
    def area_video(self) -> tuple[int, int]:
        return (self.largura - self.LARGURA_LATERAL,
                self.altura - self.ALTURA_CABECALHO - self.ALTURA_RODAPE)

    def registrar_alerta(self, texto: str) -> None:
        """Guarda a ultima ocorrencia para a faixa da lateral.

        Repetir a MESMA mensagem nao reinicia o relogio: enquanto o erro
        persiste, o operador precisa ver ha quanto tempo ele comecou, nao um
        contador travado em zero.
        """
        if texto == self._ultimo_alerta:
            return
        self._ultimo_alerta = texto
        self._momento_alerta = time.monotonic()

    # ------------------------------------------------------------------ #
    def compor(self, video: np.ndarray, vereditos, catalogo, medidas: Medidas,
               niveis: dict | None = None, camera_cega: bool = False) -> np.ndarray:
        """Desenha a tela inteira e devolve o buffer.

        O buffer e reaproveitado entre frames de proposito: alocar 1280x720x3
        com np.full custava 3,7 ms por frame — mais caro que TODO o resto do
        desenho. Quem precisar guardar o frame (gravacao, print) deve copiar.
        """
        if self._tela is None:
            self._tela = np.empty((self.altura, self.largura, 3), np.uint8)
        tela = self._tela
        cv2.rectangle(tela, (0, 0), (self.largura, self.altura), FUNDO, -1)

        self._cabecalho(tela, vereditos, camera_cega)
        self._video(tela, video, camera_cega)
        self._lateral(tela, vereditos, catalogo, niveis or {})
        self._rodape(tela, medidas)
        return tela

    # ------------------------------------------------------------------ #
    def _cabecalho(self, tela, vereditos, camera_cega: bool) -> None:
        cv2.rectangle(tela, (0, 0), (self.largura, self.ALTURA_CABECALHO), AZUL, -1)
        fim_logo = desenhar_logo(tela, 22, 18, 26)

        cv2.line(tela, (fim_logo + 8, 16), (fim_logo + 8, self.ALTURA_CABECALHO - 16),
                 (220, 200, 170), 1)
        _texto(tela, self.titulo, (fim_logo + 22, 30), 0.56, BRANCO, 1)
        _texto(tela, "Inspirados pela saude", (fim_logo + 22, 48), 0.4,
               (235, 220, 200), 1)

        criticos = [r for r in vereditos if getattr(r, "critico", False)]
        if camera_cega:
            texto, cor = "SEM IMAGEM", AMBAR
        elif criticos:
            texto, cor = f"{len(criticos)} OCORRENCIA(S)", VERMELHO
        elif vereditos:
            texto, cor = "TUDO CONFERIDO", VERDE
        else:
            texto, cor = "AGUARDANDO", CINZA_MEDIO

        largura_pilula = cv2.getTextSize(texto, FONTE, 0.46, 1)[0][0] + 20
        _pilula(tela, texto, (self.largura - largura_pilula - 22, 12), cor, BRANCO, 0.46, 24)
        _texto(tela, f"{self.estacao}   {time.strftime('%H:%M:%S')}",
               (self.largura - largura_pilula - 22, 52), 0.4, (235, 220, 200), 1)

    # ------------------------------------------------------------------ #
    def _video(self, tela, video, camera_cega: bool) -> None:
        larg, alt = self.area_video
        x0, y0 = 0, self.ALTURA_CABECALHO

        if video is None or video.size == 0:
            cv2.rectangle(tela, (x0, y0), (x0 + larg, y0 + alt), (235, 233, 231), -1)
            return

        # encaixa preservando a proporcao (letterbox), sem distorcer a cena
        vh, vw = video.shape[:2]
        escala = min(larg / vw, alt / vh)
        nova = (max(1, int(vw * escala)), max(1, int(vh * escala)))
        # INTER_AREA custa 2,4 ms aqui e a diferenca nao aparece numa previa
        # desse tamanho; so vale a pena quando a reducao e grande (>2x).
        interp = cv2.INTER_AREA if escala < 0.5 else cv2.INTER_LINEAR
        redimensionado = cv2.resize(video, nova, interpolation=interp)

        cv2.rectangle(tela, (x0, y0), (x0 + larg, y0 + alt), (28, 28, 30), -1)
        dx = x0 + (larg - nova[0]) // 2
        dy = y0 + (alt - nova[1]) // 2
        tela[dy:dy + nova[1], dx:dx + nova[0]] = redimensionado

        if camera_cega:
            faixa = tela[y0:y0 + alt, x0:x0 + larg]
            cv2.addWeighted(np.full_like(faixa, (30, 30, 40)), 0.6, faixa, 0.4, 0, faixa)
            _texto(tela, "SEM IMAGEM UTILIZAVEL", (x0 + 40, y0 + alt // 2),
                   0.9, BRANCO, 2, FONTE_TITULO)
            _texto(tela, "lente tapada, fora de foco ou luz apagada",
                   (x0 + 40, y0 + alt // 2 + 28), 0.5, (200, 200, 210), 1)

    # ------------------------------------------------------------------ #
    def _lateral(self, tela, vereditos, catalogo, niveis) -> None:
        x0 = self.largura - self.LARGURA_LATERAL
        y = self.ALTURA_CABECALHO + 14
        cv2.rectangle(tela, (x0, self.ALTURA_CABECALHO),
                      (self.largura, self.altura - self.ALTURA_RODAPE), FUNDO, -1)
        cv2.line(tela, (x0, self.ALTURA_CABECALHO),
                 (x0, self.altura - self.ALTURA_RODAPE), CINZA_CLARO, 1)

        total = sum(getattr(r, "total_unidades", 0) for r in vereditos)
        certas = sum(getattr(r, "quantidade_certa", 0) for r in vereditos)
        erradas = sum(getattr(r, "quantidade_errada", 0) for r in vereditos)

        y = self._resumo(tela, x0 + 16, y, total, certas, erradas)

        _texto(tela, "DISPENSERS", (x0 + 16, y + 4), 0.42, CINZA_MEDIO, 1)
        y += 16

        reservado = 44 if self._ultimo_alerta else 0
        disponivel = self.altura - self.ALTURA_RODAPE - y - 12 - reservado
        quantos = max(1, len(vereditos))
        cartao = min(116, max(58, disponivel // quantos - 8))
        for r in vereditos:
            if y + cartao > self.altura - self.ALTURA_RODAPE - 6 - reservado:
                break
            self._cartao_dispenser(tela, x0 + 16, y, self.LARGURA_LATERAL - 32,
                                   cartao, r, niveis.get(r.dispenser))
            y += cartao + 8

        if self._ultimo_alerta:
            self._faixa_alerta(tela, x0 + 16, self.altura - self.ALTURA_RODAPE - 48,
                               self.LARGURA_LATERAL - 32)

    def _faixa_alerta(self, tela, x, y, largura) -> None:
        """Ultima ocorrencia, com a idade — some sozinha depois de um tempo."""
        idade = time.monotonic() - self._momento_alerta
        if idade > 60:
            self._ultimo_alerta = None
            return
        _retangulo_arredondado(tela, (x, y), (x + largura, y + 38), (232, 238, 252), 8)
        cv2.rectangle(tela, (x, y + 6), (x + 4, y + 32), VERMELHO, -1)
        _texto(tela, "ULTIMA OCORRENCIA", (x + 14, y + 16), 0.34, VERMELHO, 1)
        texto = encaixar(self._ultimo_alerta, largura - 28, 0.38, 1)
        _texto(tela, texto, (x + 14, y + 31), 0.38, CINZA, 1)
        idade_txt = f"ha {int(idade)}s" if idade < 60 else ""
        w = largura_texto(idade_txt, 0.34, 1)
        _texto(tela, idade_txt, (x + largura - w - 12, y + 16), 0.34, CINZA_MEDIO, 1)

    def _resumo(self, tela, x, y, total, certas, erradas) -> int:
        largura = self.LARGURA_LATERAL - 32
        altura = 76
        _retangulo_arredondado(tela, (x, y), (x + largura, y + altura), BRANCO, 10)
        _retangulo_arredondado(tela, (x, y), (x + largura, y + altura), CINZA_CLARO, 10, 1)

        colunas = [("UNIDADES", total, AZUL), ("CONFORMES", certas, VERDE),
                   ("IRREGULARES", erradas, VERMELHO if erradas else CINZA_MEDIO)]
        passo = largura // 3
        for i, (rotulo, valor, cor) in enumerate(colunas):
            cx = x + i * passo + 16
            _texto(tela, str(valor), (cx, y + 42), 1.15, cor, 2, FONTE_TITULO)
            _texto(tela, rotulo, (cx, y + 62), 0.36, CINZA_MEDIO, 1)
            if i:
                cv2.line(tela, (x + i * passo, y + 14), (x + i * passo, y + altura - 14),
                         CINZA_CLARO, 1)
        return y + altura + 16

    def _cartao_dispenser(self, tela, x, y, largura, altura, r, nivel) -> None:
        veredito = r.veredito.value if hasattr(r.veredito, "value") else str(r.veredito)
        cor = CORES_STATUS.get(veredito, CINZA_MEDIO)

        _retangulo_arredondado(tela, (x, y), (x + largura, y + altura), BRANCO, 8)
        _retangulo_arredondado(tela, (x, y), (x + largura, y + altura), CINZA_CLARO, 8, 1)
        cv2.rectangle(tela, (x, y + 8), (x + 4, y + altura - 8), cor, -1)

        _texto(tela, f"DISPENSER {r.dispenser}", (x + 16, y + 22), 0.46, CINZA, 1)
        rotulo = ROTULO_STATUS.get(veredito, veredito)
        w = largura_texto(rotulo, 0.36, 1) + 18
        _pilula(tela, rotulo, (x + largura - w - 12, y + 8), cor, BRANCO, 0.36, 19)

        esperado = r.esperado.nome if getattr(r, "esperado", None) else "nao cadastrado"
        _texto(tela, encaixar(esperado, largura - 32, 0.42, 1),
               (x + 16, y + 40), 0.42, CINZA_MEDIO, 1)

        # o que foi realmente encontrado, com as quantidades
        itens = getattr(r, "itens", {}) or {}
        linha = y + 58
        if itens:
            cursor = x + 16
            limite = x + largura - 12
            for nome, quantidade in sorted(itens.items(), key=lambda i: -i[1])[:2]:
                if cursor >= limite - 40:
                    _texto(tela, "...", (cursor, linha), 0.44, CINZA_MEDIO, 1)
                    break
                certo = getattr(r, "esperado", None) is not None and nome == r.esperado.nome
                texto = encaixar(f"{quantidade}x {nome}", limite - cursor, 0.44, 1)
                _texto(tela, texto, (cursor, linha), 0.44,
                       VERDE if certo else VERMELHO, 1)
                cursor += largura_texto(texto, 0.44, 1) + 14
        elif altura > 62:
            _texto(tela, "nenhuma unidade lida", (x + 16, linha), 0.4, CINZA_MEDIO, 1)

        if nivel is not None and getattr(nivel, "caixas", None) is not None and altura >= 84:
            repor = bool(getattr(nivel, "precisa_repor", False))
            base = y + altura - 16
            _texto(tela, f"estoque {nivel.caixas}", (x + 16, base), 0.4,
                   AMBAR if repor else CINZA_MEDIO, 1)
            # barra de nivel
            bx = x + 96
            bw = largura - 112 - (58 if repor else 0)
            fracao = float(min(1.0, max(0.0, getattr(nivel, "fracao", 0.0))))
            _retangulo_arredondado(tela, (bx, base - 9), (bx + bw, base - 1),
                                   (232, 231, 230), 4)
            if fracao > 0:
                _retangulo_arredondado(tela, (bx, base - 9),
                                       (bx + max(6, int(bw * fracao)), base - 1),
                                       AMBAR if repor else AZUL, 4)
            if repor:
                _pilula(tela, "REPOR", (x + largura - 66, base - 14), AMBAR, BRANCO,
                        0.32, 17)

    # ------------------------------------------------------------------ #
    def _rodape(self, tela, medidas: Medidas) -> None:
        y = self.altura - self.ALTURA_RODAPE
        cv2.rectangle(tela, (0, y), (self.largura, self.altura), (243, 242, 241), -1)
        cv2.line(tela, (0, y), (self.largura, y), CINZA_CLARO, 1)

        cor_fps = VERDE if medidas.fps >= 20 else (AMBAR if medidas.fps >= 12 else VERMELHO)
        _texto(tela, f"{medidas.fps:.0f} FPS", (18, y + 22), 0.52, cor_fps, 2)

        detalhe = (f"captura {medidas.captura_ms:.0f} ms  |  "
                   f"deteccao {medidas.deteccao_ms:.0f} ms  |  "
                   f"interface {medidas.interface_ms:.0f} ms")
        atalhos = "Q sair    P pausar    E print    F tela cheia"
        w = largura_texto(atalhos, 0.42, 1)
        inicio_atalhos = self.largura - w - 18
        _texto(tela, atalhos, (inicio_atalhos, y + 22), 0.42, CINZA_MEDIO, 1)

        cursor = 104
        _texto(tela, encaixar(detalhe, inicio_atalhos - cursor - 24, 0.42, 1),
               (cursor, y + 22), 0.42, CINZA_MEDIO, 1)
        cursor += largura_texto(detalhe, 0.42, 1) + 24

        if medidas.frames:
            taxa = medidas.reavaliacoes / max(1, medidas.frames) * 100
            pesada = f"leitura pesada: {taxa:.0f}% dos frames"
            if cursor + largura_texto(pesada, 0.42, 1) < inicio_atalhos - 12:
                _texto(tela, pesada, (cursor, y + 22), 0.42, CINZA_MEDIO, 1)


# --------------------------------------------------------------------------- #
def desenhar_zonas_no_video(video, vereditos, zonas, contagens=None):
    """Marca as zonas sobre a imagem da camera, no estilo da interface."""
    img = video
    contagens = contagens or {}
    for r in vereditos:
        zona = zonas.por_dispenser(r.dispenser)
        if zona is None:
            continue
        veredito = r.veredito.value if hasattr(r.veredito, "value") else str(r.veredito)
        cor = CORES_STATUS.get(veredito, CINZA_MEDIO)
        cv2.rectangle(img, (zona.x, zona.y), (zona.x2, zona.y2), cor, 2, cv2.LINE_AA)

        total = getattr(r, "total_unidades", 0)
        etiqueta = f"D{r.dispenser}" + (f"  {total} un" if total else "")
        (w, h), _ = cv2.getTextSize(etiqueta, FONTE, 0.5, 2)
        cv2.rectangle(img, (zona.x, zona.y), (zona.x + w + 16, zona.y + h + 14), cor, -1)
        _texto(img, etiqueta, (zona.x + 8, zona.y + h + 6), 0.5, BRANCO, 2)
    return img
