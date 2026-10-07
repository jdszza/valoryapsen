"""Calibragem interativa em tres abas, com sliders e lock.

A ordem importa e nao e negociavel:

    ETAPA 1  ajuste o slider do FUNDO ate o contorno dourado abracar exatamente
             o fundo da caixa. Enquanto o nivel 1 estiver errado, o nivel 2 esta
             medindo dentro do lugar errado — nao adianta mexer nele.

    ETAPA 2  com o fundo travado, ajuste o slider do CONTEUDO ate a contagem
             bater com o numero real de medicamentos na caixa.

Depois de acertar os dois, ligue o LOCK de cada um. Lock suspende o valor
manual e deixa o sistema escolher o limiar a cada leitura, pela estabilidade da
contagem — e o que absorve reflexo de LED, sombra do braco e mudanca de
luminaria sem ninguem voltar aqui.

    ETAPA 3  a aba 3 (luz e foco) nao muda o algoritmo: ela escreve direto nos
             controles da webcam. Use quando a imagem estiver estourada, escura
             ou fora de foco. O painel na tela mostra as quatro medidas com o
             alvo de cada uma e diz o que fazer, em ordem.

Ordem ao resolver imagem ruim: PRIMEIRO a exposicao, DEPOIS o foco. Area branca
estourada nao tem textura, entao a medida de nitidez cai junto — e o sintoma de
"desfocado" costuma sumir sozinho quando o estouro sai.

TODA acao e um SLIDER VISIVEL, e nao apenas uma tecla. Isso nao e conforto: o
HighGUI do Windows nao entrega tecla nenhuma ao waitKey quando o foco esta numa
janela de sliders — que e exatamente onde a mao do operador esta durante a
calibragem. Quem depende de tecla descobre isso na bancada, do pior jeito.

    SALVAR TUDO   primeiro slider das TRES abas: arraste de 0 para 1 e a config
                  inteira (mesa + medicamentos + luz) vai para config/mesa.json,
                  que e o arquivo que o main.py le. Nao importa de qual aba voce
                  aperta — e sempre tudo, de uma vez.

    MARCAR FUNDO  na aba 1: abre o editor de mouse e voce arrasta os quatro
                  cantos do fundo da caixa. Ao aplicar, o modo vira 'manual' e a
                  marcacao e gravada. E o caminho certo quando a caixa e de
                  papelao e o detector automatico nao acha contorno nenhum.

    AUTO AJUSTE   na aba 3, varre exposicao e foco sozinho e para no melhor
                  ponto medido. Leva alguns segundos e a janela continua
                  respondendo; ESC cancela no meio. Ao terminar, os sliders
                  mostram o que ele achou — confira e salve.

    LOCK          um em cada aba (1 e 2). O slider E o estado: ligar por tecla
                  ou arrastando da no mesmo.

    VISTA / CONGELAR / RECARREGAR  tambem na aba 1.

    python src/calibrar.py                    # ao vivo pela webcam
    python src/calibrar.py --imagem foto.png  # sobre uma imagem parada

As mesmas acoes por tecla, para quem preferir — funcionam com o foco na janela
de VIDEO:
    s  salvar em config/mesa.json     1  liga/desliga lock do FUNDO
    v  alternar vista                 2  liga/desliga lock do CONTEUDO
    espaco  congelar quadro           r  recarregar config do disco
    l  mostra/esconde o painel de luz e  marcar o fundo com o mouse
    a  liga o auto-ajuste de luz/foco
    q / ESC  sair  (durante o auto-ajuste, cancela em vez de sair)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from autoluz import AutoLuz  # noqa: E402
from camera import Camera  # noqa: E402
from desenho import (  # noqa: E402
    VERDE, desenhar_conteudo, desenhar_fundo, empilhar, painel_luz,
    tamanho_tela, texto_com_fundo,
)
from editor_fundo import editar_cantos  # noqa: E402
from fluxo import FluxoVisao  # noqa: E402
from visao_mesa import ConfigMesa, VisaoMesa, retificar  # noqa: E402

JANELA = "Visao da mesa — calibragem"
P1 = "1_FUNDO_da_caixa"
P2 = "2_CONTEUDO_medicamentos"
P3 = "3_CAMERA_luz_e_foco"

METODOS_FUNDO = ["borda", "limiar"]
MODOS_FUNDO = ["caixa", "mesa", "manual"]
METODOS_CONTEUDO = ["otsu_desloc", "adaptativo", "fixo"]

SALVAR = "SALVAR TUDO (0->1)"
AUTO = "AUTO AJUSTE (0->1)"
MARCAR = "MARCAR FUNDO c MOUSE (0->1)"
MODO = "modo 0=caixa 1=mesa 2=manual"
VISTA = "VISTA 0=fundo+itens 1=mascara 2=tudo"
CONGELAR = "CONGELAR quadro"
RECARREGAR = "RECARREGAR do disco (0->1)"


def _nada(_v):
    return None


def _botao(painel: str, nome: str) -> bool:
    """Trackbar 0/1 usado como botao: devolve True uma vez e volta sozinho a 0.

    O OpenCV nao tem botao. O trackbar de dois passos e o mais proximo disso —
    e diferente de uma tecla, ele fica VISIVEL na aba, que era o pedido: ver
    onde salvar sem precisar decorar atalho. Voltar a zero na hora e o que
    impede o clique de virar acao repetida a cada frame.
    """
    try:
        if cv2.getTrackbarPos(nome, painel) == 1:
            cv2.setTrackbarPos(nome, painel, 0)
            return True
    except cv2.error:
        return False    # aba fechada (ex.: P3 nao existe no modo --imagem)
    return False


def criar_paineis(cfg: ConfigMesa) -> None:
    f, c = cfg.fundo, cfg.conteudo

    # Altura generosa de proposito: com a janela curta o HighGUI corta os
    # trackbars de baixo e eles simplesmente nao existem para quem esta
    # olhando — foi assim que o LOCK "parou de funcionar" sem nunca ter
    # deixado de funcionar. Todas as abas sao redimensionaveis.
    cv2.namedWindow(P1, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(P1, 520, 760)
    # Toda acao existe como CONTROLE VISIVEL, nao so como tecla: quando o foco
    # esta numa aba de sliders (que e onde a mao do operador esta), o HighGUI do
    # Windows nao entrega tecla nenhuma ao waitKey. Botao continua funcionando.
    cv2.createTrackbar(SALVAR, P1, 0, 1, _nada)
    cv2.createTrackbar(MARCAR, P1, 0, 1, _nada)
    cv2.createTrackbar(VISTA, P1, 0, 2, _nada)
    cv2.createTrackbar(CONGELAR, P1, 0, 1, _nada)
    cv2.createTrackbar(RECARREGAR, P1, 0, 1, _nada)
    # modo 'mesa' = nao ha bandeja no quadro; a area util e a ROI abaixo.
    # modo 'manual' = os quatro cantos marcados com o mouse (botao acima).
    cv2.createTrackbar(MODO, P1, MODOS_FUNDO.index(f.modo), 2, _nada)
    cv2.createTrackbar("limiar", P1, f.limiar, 255, _nada)
    cv2.createTrackbar("LOCK", P1, int(f.lock), 1, _nada)
    cv2.createTrackbar("metodo 0=borda 1=limiar", P1, METODOS_FUNDO.index(f.metodo), 1, _nada)
    cv2.createTrackbar("dilatacao", P1, f.dilatacao, 15, _nada)
    cv2.createTrackbar("area min %", P1, int(f.area_minima_frac * 100), 90, _nada)
    cv2.createTrackbar("comprimento mm", P1, int(f.comprimento_mm), 900, _nada)
    cv2.createTrackbar("largura mm", P1, int(f.largura_mm), 900, _nada)
    # Abaixo: so valem no modo 'mesa'.
    cv2.createTrackbar("MESA escala px/mm x10", P1, int(f.escala_px_por_mm * 10), 200, _nada)
    cv2.createTrackbar("MESA roi x %", P1, int(f.roi[0] * 100), 90, _nada)
    cv2.createTrackbar("MESA roi y %", P1, int(f.roi[1] * 100), 90, _nada)
    cv2.createTrackbar("MESA roi larg %", P1, int(f.roi[2] * 100), 100, _nada)
    cv2.createTrackbar("MESA roi alt %", P1, int(f.roi[3] * 100), 100, _nada)

    cv2.namedWindow(P2, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(P2, 520, 700)
    cv2.createTrackbar(SALVAR, P2, 0, 1, _nada)
    cv2.createTrackbar("limiar", P2, c.limiar, 255, _nada)
    cv2.createTrackbar("LOCK", P2, int(c.lock), 1, _nada)
    cv2.createTrackbar("metodo 0=otsu 1=adapt 2=fixo", P2,
                       METODOS_CONTEUDO.index(c.metodo), 2, _nada)
    cv2.createTrackbar("inverter", P2, int(c.inverter), 1, _nada)
    cv2.createTrackbar("normalizar luz", P2, int(c.normalizar_iluminacao), 1, _nada)
    cv2.createTrackbar("margem interna mm", P2, int(c.margem_interna_mm), 60, _nada)
    cv2.createTrackbar("area min mm2 /10", P2, int(c.area_min_mm2 / 10), 500, _nada)
    cv2.createTrackbar("area max mm2 /100", P2, int(c.area_max_mm2 / 100), 600, _nada)
    cv2.createTrackbar("lado min mm", P2, int(c.lado_min_mm), 200, _nada)
    cv2.createTrackbar("lado max mm", P2, int(c.lado_max_mm), 400, _nada)
    cv2.createTrackbar("aspecto max x10", P2, int(c.razao_aspecto_max * 10), 200, _nada)
    cv2.createTrackbar("preenchimento %", P2, int(c.preenchimento_min * 100), 100, _nada)
    cv2.createTrackbar("separar encostadas", P2, int(c.separar_encostadas), 1, _nada)
    cv2.createTrackbar("limiar distancia %", P2, int(c.limiar_distancia * 100), 95, _nada)


def criar_painel_camera(cfg: ConfigMesa) -> None:
    """Aba 3: escreve nos controles da webcam, nao no algoritmo."""
    k = cfg.camera
    cv2.namedWindow(P3, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(P3, 520, 520)
    cv2.createTrackbar(SALVAR, P3, 0, 1, _nada)
    # Um slider que mexe em TODOS os de baixo: varre exposicao e depois foco e
    # para no melhor ponto medido, em vez de deixar o operador tentando no olho.
    cv2.createTrackbar(AUTO, P3, 0, 1, _nada)
    cv2.createTrackbar("autofoco", P3, int(k.autofoco), 1, _nada)
    cv2.createTrackbar("foco", P3, k.foco, 255, _nada)
    cv2.createTrackbar("autoexposicao", P3, int(k.autoexposicao), 1, _nada)
    # No Windows/DSHOW a exposicao vai de -13 a -1 e e logaritmica: cada passo
    # dobra ou divide o tempo. O slider mapeia 0..12 nessa faixa.
    cv2.createTrackbar("exposicao (-13..-1)", P3, int(k.exposicao) + 13, 12, _nada)
    cv2.createTrackbar("auto white balance", P3, int(k.autobalanco_branco), 1, _nada)
    cv2.createTrackbar("temperatura K /100", P3, k.temperatura_cor // 100, 100, _nada)
    # 0 = nao mexer; 1..256 vira 0..255 no driver.
    cv2.createTrackbar("ganho (0=auto)", P3, max(0, k.ganho + 1), 256, _nada)
    cv2.createTrackbar("brilho (0=auto)", P3, max(0, k.brilho + 1), 256, _nada)
    cv2.createTrackbar("contraste (0=auto)", P3, max(0, k.contraste + 1), 256, _nada)


def ler_painel_camera(cfg: ConfigMesa) -> tuple:
    """Le a aba 3 e devolve a assinatura, para so escrever no driver na mudanca.

    Cada cap.set() custa milissegundos e conversa com o firmware da webcam.
    Escrever os nove controles a cada frame derrubaria o FPS e faria a imagem
    piscar — por isso a escrita so acontece quando algum slider muda de fato.
    """
    k = cfg.camera
    g = lambda n: cv2.getTrackbarPos(n, P3)  # noqa: E731
    k.autofoco = bool(g("autofoco"))
    k.foco = g("foco")
    k.autoexposicao = bool(g("autoexposicao"))
    k.exposicao = g("exposicao (-13..-1)") - 13
    k.autobalanco_branco = bool(g("auto white balance"))
    k.temperatura_cor = max(2000, g("temperatura K /100") * 100)
    k.ganho = g("ganho (0=auto)") - 1
    k.brilho = g("brilho (0=auto)") - 1
    k.contraste = g("contraste (0=auto)") - 1
    return (k.autofoco, k.foco, k.autoexposicao, k.exposicao,
            k.autobalanco_branco, k.temperatura_cor, k.ganho, k.brilho, k.contraste)


def sincronizar_painel_camera(cfg: ConfigMesa) -> None:
    """Joga os valores da config PARA os sliders da aba 3.

    Caminho inverso do ler_painel_camera, necessario depois do auto-ajuste: sem
    isso o slider continuaria mostrando o valor antigo e, na volta seguinte do
    laco, sobrescreveria o que o auto-ajuste acabou de achar.
    """
    k = cfg.camera
    try:
        cv2.setTrackbarPos("autofoco", P3, int(k.autofoco))
        cv2.setTrackbarPos("foco", P3, int(max(0, min(255, k.foco))))
        cv2.setTrackbarPos("autoexposicao", P3, int(k.autoexposicao))
        cv2.setTrackbarPos("exposicao (-13..-1)", P3,
                           int(max(0, min(12, int(k.exposicao) + 13))))
    except cv2.error:
        pass


def ler_paineis(cfg: ConfigMesa) -> None:
    f, c = cfg.fundo, cfg.conteudo
    g1 = lambda n: cv2.getTrackbarPos(n, P1)  # noqa: E731
    g2 = lambda n: cv2.getTrackbarPos(n, P2)  # noqa: E731

    f.modo = MODOS_FUNDO[g1(MODO)]
    f.limiar = g1("limiar")
    f.lock = bool(g1("LOCK"))
    f.metodo = METODOS_FUNDO[g1("metodo 0=borda 1=limiar")]
    f.dilatacao = g1("dilatacao")
    f.area_minima_frac = max(0.01, g1("area min %") / 100)
    f.comprimento_mm = max(10.0, float(g1("comprimento mm")))
    f.largura_mm = max(10.0, float(g1("largura mm")))
    f.escala_px_por_mm = max(0.2, g1("MESA escala px/mm x10") / 10)
    f.roi = (g1("MESA roi x %") / 100, g1("MESA roi y %") / 100,
             max(0.05, g1("MESA roi larg %") / 100), max(0.05, g1("MESA roi alt %") / 100))

    c.limiar = g2("limiar")
    c.lock = bool(g2("LOCK"))
    c.metodo = METODOS_CONTEUDO[g2("metodo 0=otsu 1=adapt 2=fixo")]
    c.inverter = bool(g2("inverter"))
    c.normalizar_iluminacao = bool(g2("normalizar luz"))
    c.margem_interna_mm = float(g2("margem interna mm"))
    c.area_min_mm2 = max(10.0, g2("area min mm2 /10") * 10.0)
    c.area_max_mm2 = max(c.area_min_mm2 + 10, g2("area max mm2 /100") * 100.0)
    c.lado_min_mm = max(1.0, float(g2("lado min mm")))
    c.lado_max_mm = max(c.lado_min_mm + 1, float(g2("lado max mm")))
    c.razao_aspecto_max = max(1.0, g2("aspecto max x10") / 10)
    c.preenchimento_min = g2("preenchimento %") / 100
    c.separar_encostadas = bool(g2("separar encostadas"))
    c.limiar_distancia = max(0.05, g2("limiar distancia %") / 100)


def alternar_lock(painel: str) -> None:
    atual = cv2.getTrackbarPos("LOCK", painel)
    cv2.setTrackbarPos("LOCK", painel, 0 if atual else 1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--imagem", help="calibrar sobre uma imagem em vez da webcam")
    p.add_argument("--indice", type=int, help="sobrepoe o indice da camera")
    args = p.parse_args()

    cfg = ConfigMesa.carregar()
    if args.indice is not None:
        cfg.camera.indice = args.indice

    print(__doc__)
    criar_paineis(cfg)

    imagem_fixa = cv2.imread(args.imagem) if args.imagem else None
    if args.imagem and imagem_fixa is None:
        raise SystemExit(f"Nao consegui abrir {args.imagem}")

    cam = None if imagem_fixa is not None else Camera(cfg.camera).abrir()
    if cam is not None:
        criar_painel_camera(cfg)
        efetivos = cam.conferir()
        print(f"\nCamera {cfg.camera.indice}: "
              f"{efetivos['largura']:.0f}x{efetivos['altura']:.0f} @ {efetivos['fps']:.0f} FPS")
        if efetivos["auto_exposicao"] not in (0, 0.25, 1, 1.0, 0.0):
            print("  AVISO: a autoexposicao pode nao ter desligado — "
                  "a calibragem pode nao valer amanha.")

    # Uma unica instancia para toda a sessao. Recriar a VisaoMesa a cada frame
    # jogava fora o cache do limiar automatico e fazia a busca inteira rodar
    # sempre. Os detectores guardam referencia aos mesmos objetos de config que
    # os sliders alteram, entao mexer no slider continua valendo na hora.
    visao = VisaoMesa(cfg)

    # Ao vivo: camera numa thread, deteccao em outra, desenho aqui. E o que
    # mantem a janela respondendo enquanto uma busca de limiar roda.
    fluxo = FluxoVisao(cam, visao).iniciar() if cam is not None else None
    if fluxo is not None:
        fluxo.processador.esperar_primeiro()

    modo_vista, congelado = 0, False
    mostrar_luz = True
    assinatura_camera = None
    frame = imagem_fixa
    ultimo_desenhado = -1

    # Na calibragem o video divide a tela com as tres abas de slider e com o
    # painel de luz, entao ele fica com ~62% da largura. No main.py, onde nao ha
    # slider nenhum, a janela usa a tela inteira.
    tela = tamanho_tela(0.62, 0.90)
    cv2.namedWindow(JANELA, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
    cv2.moveWindow(JANELA, 0, 0)
    tamanho_janela: tuple[int, int] | None = None

    # Auto-ajuste: so existe com camera real. Sobre imagem parada nao ha o que
    # varrer — os controles de exposicao e foco nao afetam um PNG.
    autoluz = AutoLuz(cfg, cam, fluxo.leitor) if fluxo is not None else None
    aviso, aviso_em = "", 0.0

    def anunciar(texto: str) -> None:
        """Mensagem grande na tela + no terminal.

        O terminal sozinho nao serve: o operador esta olhando a janela de video
        com a mao no slider, e nao ve o print. E a tela sozinha tambem nao, que
        e onde fica o registro do que foi salvo.
        """
        nonlocal aviso, aviso_em
        aviso, aviso_em = texto, time.monotonic()
        print(texto)

    def obter_frame():
        return fluxo.leitor.ultimo()[0] if fluxo is not None else imagem_fixa

    def marcar_fundo() -> None:
        if editar_cantos(cfg, obter_frame):
            cv2.setTrackbarPos(MODO, P1, MODOS_FUNDO.index("manual"))
            anunciar(f"cantos marcados (modo manual) -> {cfg.salvar()}")
        else:
            anunciar("marcacao cancelada")

    def recarregar() -> None:
        nonlocal cfg, visao, autoluz, assinatura_camera
        cfg = ConfigMesa.carregar()
        visao = VisaoMesa(cfg)
        if fluxo is not None:
            fluxo.processador.visao = visao
        cv2.destroyWindow(P1)
        cv2.destroyWindow(P2)
        criar_paineis(cfg)
        if cam is not None:
            cv2.destroyWindow(P3)
            criar_painel_camera(cfg)
            assinatura_camera = None
        # Recarregar troca o OBJETO de config. Sem refazer o auto-ajuste ele
        # continuaria escrevendo na config antiga, que ninguem mais le — e o
        # ajuste "funcionaria" sem sair nada na imagem.
        if fluxo is not None:
            autoluz = AutoLuz(cfg, cam, fluxo.leitor)
        anunciar("config recarregada do disco")

    def tratar_tecla(tecla: int) -> bool:
        """Trata TODA tecla, e devolve True quando e para sair.

        Precisa ser um lugar so. Antes, cada `waitKey` do laco tratava um
        subconjunto: os dois atalhos que existem para quando nao ha frame novo
        so olhavam 'q'/ESC e jogavam o resto fora. Como o laco gira muito mais
        rapido que os 30 FPS da camera, a MAIORIA das teclas caia justamente
        ali — e 'e', '1' e '2' pareciam nao funcionar, funcionando de vez em
        quando, que e o pior dos dois mundos para quem esta calibrando.
        """
        nonlocal mostrar_luz
        if tecla in (255, -1):          # nenhuma tecla no periodo do waitKey
            return False
        if tecla in (ord("q"), 27):
            # 'q'/ESC fecha o programa — mas cancela o auto-ajuste primeiro,
            # senao abortar uma varredura de dez segundos custa a sessao toda.
            if autoluz is not None and autoluz.ativo:
                autoluz.cancelar()
                anunciar("auto-ajuste cancelado")
                return False
            return True
        if tecla == ord("s"):
            anunciar(f"SALVO -> {cfg.salvar()}")
        elif tecla == ord("e"):
            marcar_fundo()
        elif tecla == ord("a") and autoluz is not None and not autoluz.ativo:
            autoluz.iniciar()
            anunciar("auto-ajuste iniciado (ESC cancela)")
        elif tecla == ord("1"):
            alternar_lock(P1)
        elif tecla == ord("2"):
            alternar_lock(P2)
        elif tecla == ord("v"):
            # Escreve no trackbar, nao numa variavel: a aba e a fonte unica da
            # verdade, entao tecla e slider nunca discordam do que esta na tela.
            cv2.setTrackbarPos(VISTA, P1, (cv2.getTrackbarPos(VISTA, P1) + 1) % 3)
        elif tecla == ord(" "):
            cv2.setTrackbarPos(CONGELAR, P1, 0 if cv2.getTrackbarPos(CONGELAR, P1) else 1)
        elif tecla == ord("r"):
            recarregar()
        elif tecla == ord("l"):
            mostrar_luz = not mostrar_luz
            if not mostrar_luz:
                cv2.destroyWindow("Luz e foco")
        return False

    try:
        while True:
            ler_paineis(cfg)

            if _botao(P1, SALVAR) or _botao(P2, SALVAR) or _botao(P3, SALVAR):
                # O botao de qualquer aba salva TUDO: o arquivo e um so e o
                # main.py le ele inteiro. Salvar "so a aba atual" daria a falsa
                # sensacao de que as outras ficaram como estavam no disco.
                anunciar(f"SALVO -> {cfg.salvar()}")
            if _botao(P1, MARCAR):
                marcar_fundo()
            if _botao(P1, RECARREGAR):
                recarregar()
                continue        # os paineis foram recriados; releia do zero

            modo_vista = cv2.getTrackbarPos(VISTA, P1)
            quer_congelar = bool(cv2.getTrackbarPos(CONGELAR, P1))
            if quer_congelar != congelado:
                congelado = quer_congelar
                # Congelado, a deteccao para de consumir frames novos e passa a
                # reprocessar o quadro travado — assim o slider continua dando
                # retorno visual sobre exatamente a imagem que esta na tela.
                if fluxo is not None:
                    fluxo.processador.fixar(frame if congelado else None)

            if cam is not None:
                if autoluz is not None and autoluz.ativo:
                    # Enquanto a varredura roda, os sliders da aba 3 NAO sao
                    # lidos: eles ainda mostram o valor antigo e sobrescreveriam
                    # cada valor que o auto-ajuste acabou de escrever.
                    aviso, aviso_em = autoluz.passo(), time.monotonic()
                    if not autoluz.ativo:
                        sincronizar_painel_camera(cfg)
                        assinatura_camera = None
                        print(autoluz.resumo)
                else:
                    if _botao(P3, AUTO):
                        autoluz.iniciar()
                        anunciar("auto-ajuste iniciado (ESC cancela)")
                    nova = ler_painel_camera(cfg)
                    if nova != assinatura_camera:
                        assinatura_camera = nova
                        # A escrita acontece na thread que le a camera:
                        # VideoCapture nao e seguro entre threads.
                        cam.pedir_aplicacao()

            if fluxo is None:
                # Imagem parada: sem threads, processa direto a cada volta.
                resultado = visao.processar(frame)
                contador = ultimo_desenhado + 1
                idade = 0.0
            else:
                if not congelado:
                    frame, contador, _m = fluxo.leitor.ultimo()
                else:
                    contador = ultimo_desenhado + 1
                resultado, _usado, idade = fluxo.processador.ultimo()
                if frame is None or resultado is None:
                    if tratar_tecla(cv2.waitKey(5) & 0xFF):
                        break
                    continue
                # So redesenha com frame novo — a nao ser congelado, onde o
                # slider ainda precisa dar retorno visual a cada volta.
                if contador == ultimo_desenhado and not congelado:
                    if tratar_tecla(cv2.waitKey(5) & 0xFF):
                        break
                    continue
            ultimo_desenhado = contador

            painel_fundo = desenhar_fundo(frame, resultado)
            if resultado.fundo is None:
                saida = empilhar(painel_fundo, tamanho=tela)
            else:
                vista = retificar(frame, resultado.fundo)
                extra = "[CONGELADO]" if congelado else ""
                if fluxo is not None:
                    extra += (f"  cam {fluxo.leitor.fps:.0f} FPS"
                              f"  visao {fluxo.processador.fps:.1f} Hz"
                              f"  atraso {idade * 1000:.0f} ms")
                painel_conteudo = desenhar_conteudo(vista, resultado, extra)
                # Empilhado: em cima o que era o painel da esquerda, embaixo o
                # da direita. Na vista 2 o quadro da camera fica na linha de
                # cima e os dois derivados dividem a de baixo.
                if modo_vista == 0:
                    saida = empilhar(painel_fundo, painel_conteudo, tamanho=tela)
                elif modo_vista == 1:
                    saida = empilhar(painel_conteudo, resultado.mascara, tamanho=tela)
                else:
                    saida = empilhar(painel_fundo, vista, resultado.mascara,
                                     tamanho=tela)

            # Aviso de "salvei" / progresso do auto-ajuste por cima do video, por
            # alguns segundos. Fica aqui, depois da composicao, para valer em
            # qualquer modo de vista e tambem quando o fundo nao foi encontrado.
            if aviso and time.monotonic() - aviso_em < 4.0:
                texto_com_fundo(saida, aviso, (14, saida.shape[0] - 16),
                                VERDE, 0.6, 2)
            # Acerta o tamanho da janela so quando a composicao muda (trocar de
            # vista muda): a cada frame faria piscar e ignoraria o operador que
            # arrastou a borda.
            if (saida.shape[1], saida.shape[0]) != tamanho_janela:
                tamanho_janela = (saida.shape[1], saida.shape[0])
                cv2.resizeWindow(JANELA, tamanho_janela[0], tamanho_janela[1])
            cv2.imshow(JANELA, saida)
            if mostrar_luz and resultado.qualidade is not None:
                cv2.imshow("Luz e foco", painel_luz(resultado))

            if tratar_tecla(cv2.waitKey(1) & 0xFF):
                break
    finally:
        if fluxo is not None:
            fluxo.parar()
        if cam is not None:
            cam.fechar()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
