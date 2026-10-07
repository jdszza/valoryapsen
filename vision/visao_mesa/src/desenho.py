"""Overlay: quadrilatero do fundo, bounding box de cada item e barra de estado.

Identidade Valory: preto com dourado.
"""

from __future__ import annotations

import cv2
import numpy as np

FONTE = cv2.FONT_HERSHEY_SIMPLEX
DOURADO = (35, 190, 232)     # BGR
PRETO = (18, 18, 18)
BRANCO = (240, 240, 240)
VERMELHO = (60, 60, 235)
VERDE = (80, 200, 120)
CINZA = (150, 150, 150)


def texto_com_fundo(img, texto, org, cor=BRANCO, escala=0.45, espessura=1):
    x, y = int(org[0]), max(14, int(org[1]))
    (w, h), base = cv2.getTextSize(texto, FONTE, escala, espessura)
    x = max(0, min(x, img.shape[1] - w - 8))
    cv2.rectangle(img, (x - 3, y - h - 4), (x + w + 4, y + base + 1), PRETO, -1)
    cv2.putText(img, texto, (x, y), FONTE, escala, cor, espessura, cv2.LINE_AA)


def barra(img, texto, cor=DOURADO, altura=30, escala=0.5):
    cv2.rectangle(img, (0, 0), (img.shape[1], altura), PRETO, -1)
    cv2.putText(img, texto, (8, int(altura * 0.7)), FONTE, escala, cor, 1, cv2.LINE_AA)


def desenhar_fundo(frame: np.ndarray, resultado) -> np.ndarray:
    """Nivel 1 sobre o frame original: mostra QUAL retangulo virou a caixa."""
    img = frame.copy()
    if resultado.fundo is None:
        barra(img, "FUNDO DA CAIXA NAO ENCONTRADO — ajuste o slider 1", VERMELHO)
        return img

    quad = resultado.fundo.quadrilatero.astype(np.int32)
    cor = DOURADO if resultado.fundo.fresco else CINZA
    cv2.polylines(img, [quad], True, cor, 3, cv2.LINE_AA)
    for indice, (px, py) in enumerate(quad):
        cv2.circle(img, (int(px), int(py)), 5, cor, -1)
        texto_com_fundo(img, "TL TR BR BL".split()[indice], (int(px) + 8, int(py) - 6), cor)

    c, l = resultado.fundo.dimensao_mm
    estado = "" if resultado.fundo.fresco else "  [reaproveitado]"
    if resultado.fundo.limiar_usado:
        rotulo = (f"FUNDO (caixa)  limiar={resultado.fundo.limiar_usado}  "
                  f"{c:.0f}x{l:.0f}mm  area={resultado.fundo.area_frac * 100:.0f}%")
    else:
        # Modo mesa: nao ha limiar de fundo, a area util e a ROI configurada.
        rotulo = (f"AREA UTIL (mesa)  {c:.0f}x{l:.0f}mm  "
                  f"{resultado.fundo.px_por_mm:.2f} px/mm  "
                  f"{resultado.fundo.area_frac * 100:.0f}% do quadro")
    barra(img, rotulo + estado, cor)
    return img


def desenhar_conteudo(vista: np.ndarray, resultado, extra: str = "") -> np.ndarray:
    """Nivel 2 sobre a vista retificada: cada item com sua bounding box."""
    img = vista.copy()
    cor = DOURADO if resultado.confiavel else VERMELHO

    for item in resultado.itens:
        pontos = item.poligono.astype(np.int32)
        cv2.polylines(img, [pontos], True, cor, 2, cv2.LINE_AA)
        centro = item.poligono.mean(axis=0).astype(int)
        cv2.drawMarker(img, tuple(centro), cor, cv2.MARKER_CROSS, 9, 1)
        texto_com_fundo(
            img,
            f"{item.indice}  {item.comprimento_mm:.0f}x{item.largura_mm:.0f}mm",
            (pontos[:, 0].min(), pontos[:, 1].min() - 5),
            cor,
        )

    contagem = "--" if resultado.contagem is None else str(resultado.contagem)
    linha = (f"ITENS: {contagem}   limiar={resultado.limiar_conteudo} "
             f"({resultado.metodo_conteudo})   patamar={resultado.patamar}")
    if extra:
        linha += f"   {extra}"
    barra(img, linha, cor)

    if not resultado.confiavel:
        # O motivo fica na tela porque um numero recusado sem explicacao vira
        # "o sistema nao funciona" na boca do operador.
        cv2.rectangle(img, (0, img.shape[0] - 26), (img.shape[1], img.shape[0]), PRETO, -1)
        cv2.putText(img, f"RECUSADO: {resultado.motivo}", (8, img.shape[0] - 8),
                    FONTE, 0.42, VERMELHO, 1, cv2.LINE_AA)
    return img


def lado_a_lado(*imagens: np.ndarray, altura: int = 540) -> np.ndarray:
    partes = []
    for img in imagens:
        if img is None:
            continue
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        escala = altura / img.shape[0]
        partes.append(cv2.resize(img, (max(1, int(img.shape[1] * escala)), altura)))
    return np.hstack(partes) if partes else np.zeros((altura, altura, 3), np.uint8)


# --------------------------------------------------------------------------- #
# Layout de tela
# --------------------------------------------------------------------------- #
_TELA_BRUTA: tuple[int, int] | None = None


def _medir_tela() -> tuple[int, int]:
    """Resolucao do monitor, medida uma vez e guardada.

    No Windows sai do GetSystemMetrics, que nao cria janela nenhuma. De
    proposito NAO se chama SetProcessDPIAware: com a escala do Windows em 125%
    (padrao em notebook), o processo sem DPI awareness enxerga a area logica e
    e nela que a janela e desenhada — pedir o tamanho fisico faria a janela
    nascer um quarto maior que a tela, que e o problema que se quer evitar.
    """
    global _TELA_BRUTA
    if _TELA_BRUTA is not None:
        return _TELA_BRUTA
    medida = (1366, 768)     # notebook mais comum; so vale se tudo falhar
    try:
        import ctypes
        usuario = ctypes.windll.user32                     # type: ignore[attr-defined]
        largura, altura = usuario.GetSystemMetrics(0), usuario.GetSystemMetrics(1)
        if largura > 0 and altura > 0:
            medida = (int(largura), int(altura))
    except Exception:
        try:
            import tkinter
            raiz = tkinter.Tk()
            raiz.withdraw()
            medida = (raiz.winfo_screenwidth(), raiz.winfo_screenheight())
            raiz.destroy()
        except Exception:
            pass
    _TELA_BRUTA = medida
    return medida


def tamanho_tela(frac_largura: float = 0.99,
                 frac_altura: float = 0.90) -> tuple[int, int]:
    """Area util para a janela de video, em pixel.

    As fracoes descontam barra de titulo e barra de tarefas. Sem elas a janela
    nasce do tamanho da tela inteira e a barra de tarefas come a ultima faixa —
    justo onde fica o rodape de RECUSADO.
    """
    largura, altura = _medir_tela()
    return max(640, int(largura * frac_largura)), max(400, int(altura * frac_altura))


def _encaixar(img: np.ndarray, largura: int, altura: int) -> np.ndarray:
    """Imagem centrada numa celula fixa, sem distorcer.

    Letterbox e nao esticar: a vista retificada esta em escala mm conhecida, e
    esticar para preencher a celula faria uma embalagem de 77x34mm PARECER
    outra proporcao na tela — confere errado a olho.
    """
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    escala = min(largura / img.shape[1], altura / img.shape[0])
    nova = (max(1, int(img.shape[1] * escala)), max(1, int(img.shape[0] * escala)))
    interpolacao = cv2.INTER_AREA if escala < 1 else cv2.INTER_LINEAR
    reduzida = cv2.resize(img, nova, interpolation=interpolacao)
    celula = np.full((altura, largura, 3), 24, dtype=np.uint8)
    x = (largura - nova[0]) // 2
    y = (altura - nova[1]) // 2
    celula[y:y + nova[1], x:x + nova[0]] = reduzida
    return celula


def empilhar(*imagens: np.ndarray, tamanho: tuple[int, int] | None = None) -> np.ndarray:
    """Painéis EM CIMA E EMBAIXO, em metades iguais, dentro da tela.

    Lado a lado, os dois painéis somavam quase 1920 px de largura: em tela de
    notebook o Windows cortava o painel da direita — a contagem aparecia, as
    ultimas bounding box nao. Empilhado, nada e cortado.

    Ordem: o que era o painel da ESQUERDA vai para cima, o da DIREITA para
    baixo. Com tres painéis, o primeiro fica na linha de cima e os outros dois
    dividem a de baixo.

    `tamanho` e o MAXIMO, nao o tamanho final. A altura e usada por inteiro
    (metades exatamente iguais), mas a largura sai a que o conteudo precisa —
    duas imagens 16:9 empilhadas em 1366x691 usam so ~615 px de largura, e
    forcar os 1366 encheria a janela de barra preta dos dois lados sem
    aumentar nada. Quem chama redimensiona a janela pelo que volta.
    """
    partes = [img for img in imagens if img is not None]
    largura_max, altura = tamanho or tamanho_tela()
    if not partes:
        return np.full((altura, largura_max, 3), 24, dtype=np.uint8)
    if len(partes) <= 1:
        linhas = [partes]
    elif len(partes) == 2:
        linhas = [[partes[0]], [partes[1]]]
    else:
        linhas = [[partes[0]], partes[1:]]

    alt_linha = altura // len(linhas)
    # Largura que cada linha ocuparia sem sobra, ja com a altura da metade.
    precisa = max(
        sum(int(round(img.shape[1] * alt_linha / img.shape[0])) for img in linha)
        for linha in linhas
    )
    largura = max(320, min(largura_max, precisa))

    montadas = []
    for linha in linhas:
        larg_celula = largura // len(linha)
        celulas = [_encaixar(img, larg_celula, alt_linha) for img in linha]
        faixa = np.hstack(celulas)
        if faixa.shape[1] != largura:   # sobra da divisao inteira
            faixa = np.hstack([faixa, np.full(
                (alt_linha, largura - faixa.shape[1], 3), 24, dtype=np.uint8)])
        montadas.append(faixa)
    tela = np.vstack(montadas)
    if tela.shape[0] != altura:
        tela = np.vstack([tela, np.full(
            (altura - tela.shape[0], largura, 3), 24, dtype=np.uint8)])
    # Risco separando as metades: sem ele, com os dois painéis escuros, o
    # operador nao enxerga onde um termina e o outro comeca.
    for i in range(1, len(linhas)):
        cv2.line(tela, (0, i * alt_linha), (largura, i * alt_linha), DOURADO, 1)
    return tela


# --------------------------------------------------------------------------- #
# Aba 3 — luz e foco
# --------------------------------------------------------------------------- #
# Faixas medidas em duas fotos da bancada real: uma que o sistema contou certo
# e uma que ele recusou. Servem de alvo para o operador mirar, em vez de mexer
# no slider no escuro.
ALVOS = {
    "brilho": (85.0, 155.0),      # media do quadro (0-255)
    "estourado": (0.0, 5.0),      # % de pixels >= 250
    "hotspot": (0.0, 0.24),       # mancha localizada de luz
    "nitidez": (1.5, 99.0),       # laplaciano relativo ao contraste
}


def _situacao(valor: float, faixa: tuple[float, float]) -> tuple[str, tuple]:
    minimo, maximo = faixa
    if valor < minimo:
        return "BAIXO", AMARELO if valor > minimo * 0.6 else VERMELHO
    if valor > maximo:
        return "ALTO", AMARELO if valor < maximo * 1.4 else VERMELHO
    return "ok", VERDE


AMARELO = (60, 190, 240)


def diagnostico_luz(qualidade) -> list[tuple[str, tuple]]:
    """O que fazer, em ordem de prioridade.

    A ordem importa: nao adianta mexer no foco enquanto a imagem esta estourada,
    porque area branca chapada nao tem textura e a medida de nitidez cai junto —
    o sintoma de 'desfocado' some sozinho quando a exposicao baixa.
    """
    acoes = []
    if qualidade.reflexo * 100 > ALVOS["estourado"][1]:
        acoes.append(("1. BAIXE a exposicao (mais negativa) ou o ganho", VERMELHO))
        acoes.append(("   a embalagem branca esta chapada, sem borda", CINZA))
    elif qualidade.brilho < ALVOS["brilho"][0]:
        acoes.append(("1. SUBA a exposicao (menos negativa)", AMARELO))
    if qualidade.hotspot > ALVOS["hotspot"][1]:
        acoes.append(("2. Mude o angulo da luz ou use luz difusa", AMARELO))
        acoes.append(("   ha uma mancha de brilho concentrada", CINZA))
    if qualidade.nitidez < ALVOS["nitidez"][0]:
        if qualidade.reflexo * 100 > ALVOS["estourado"][1]:
            acoes.append(("3. Foco: ajuste DEPOIS de resolver o estouro", CINZA))
        else:
            acoes.append(("3. Ajuste o FOCO ate o texto ficar legivel", AMARELO))
    if not acoes:
        acoes.append(("Luz e foco dentro do alvo.", VERDE))
    return acoes


def painel_luz(resultado, largura: int = 460) -> np.ndarray:
    """Leitura ao vivo das metricas de luz, com alvo e o que fazer."""
    q = resultado.qualidade
    linhas = [
        ("brilho medio", q.brilho, "%.0f", ALVOS["brilho"]),
        ("estourado %", q.reflexo * 100, "%.1f", ALVOS["estourado"]),
        ("hotspot", q.hotspot, "%.2f", ALVOS["hotspot"]),
        ("nitidez", q.nitidez, "%.2f", ALVOS["nitidez"]),
    ]
    acoes = diagnostico_luz(q) if q is not None else []
    altura = 46 + len(linhas) * 30 + 16 + len(acoes) * 22 + 14
    img = np.full((altura, largura, 3), 24, dtype=np.uint8)
    barra(img, "3  LUZ E FOCO", DOURADO, altura=32, escala=0.55)

    y = 62
    for nome, valor, formato, faixa in linhas:
        estado, cor = _situacao(valor, faixa)
        cv2.putText(img, nome, (12, y), FONTE, 0.46, BRANCO, 1, cv2.LINE_AA)
        cv2.putText(img, formato % valor, (185, y), FONTE, 0.52, cor, 1, cv2.LINE_AA)
        # 99 e o sentinela de "sem teto" (nitidez). Sem ele, a faixa 85-155 do
        # brilho aparecia como "alvo >85" e escondia o limite de cima, que era
        # justamente o que estava estourado.
        if faixa[1] >= 99:
            alvo = f"alvo >{faixa[0]:g}"
        elif faixa[0] == 0:
            alvo = f"alvo <{faixa[1]:g}"
        else:
            alvo = f"alvo {faixa[0]:g}-{faixa[1]:g}"
        cv2.putText(img, alvo, (255, y), FONTE, 0.42, CINZA, 1, cv2.LINE_AA)
        cv2.putText(img, estado, (385, y), FONTE, 0.46, cor, 1, cv2.LINE_AA)
        y += 30

    cv2.line(img, (10, y - 12), (largura - 10, y - 12), (70, 70, 70), 1)
    y += 8
    for texto, cor in acoes:
        cv2.putText(img, texto, (12, y), FONTE, 0.44, cor, 1, cv2.LINE_AA)
        y += 22
    return img
