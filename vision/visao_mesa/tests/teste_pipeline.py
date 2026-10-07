"""Valida a visao da mesa em cenas sinteticas com ground truth.

Cobre os tres casos que quebram um detector de limiar fixo numa celula real:
altura de camera diferente, reflexo de LED na embalagem, e luz fraca.
O modo lock precisa acertar a contagem nos tres SEM ninguem tocar no slider.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from visao_mesa import ConfigMesa, VisaoMesa, medir_qualidade  # noqa: E402

# (x_mm, y_mm, comprimento_mm, largura_mm, angulo)
CONTEUDO = [
    (55.0, 45.0, 80.0, 45.0, 0.0),
    (150.0, 42.0, 75.0, 42.0, 8.0),
    (240.0, 48.0, 70.0, 40.0, -12.0),
    (60.0, 120.0, 85.0, 48.0, 3.0),
    (160.0, 125.0, 78.0, 44.0, 25.0),
    (245.0, 118.0, 60.0, 38.0, 0.0),   # encostadas
    (245.0, 160.0, 60.0, 38.0, 0.0),
]


def gerar_cena(
    cfg: ConfigMesa,
    altura_camera_mm: float = 700.0,
    reflexo: float = 0.0,
    luz: float = 1.0,
    semente: int = 7,
    conteudo: list | None = None,
) -> np.ndarray:
    rng = np.random.default_rng(semente)
    s = 3.0
    comp, larg = cfg.fundo.comprimento_mm, cfg.fundo.largura_mm
    margem = 70.0
    W, H = int((comp + 2 * margem) * s), int((larg + 2 * margem) * s)

    # Bancada escura.
    quadro = np.full((H, W, 3), 55, dtype=np.uint8)

    # Fundo da caixa: retangulo claro, com parede um pouco mais escura em volta.
    x0, y0 = int(margem * s), int(margem * s)
    x1, y1 = int((margem + comp) * s), int((margem + larg) * s)
    parede = int(9 * s)
    cv2.rectangle(quadro, (x0 - parede, y0 - parede), (x1 + parede, y1 + parede),
                  (105, 105, 105), -1)
    cv2.rectangle(quadro, (x0, y0), (x1, y1), (168, 168, 168), -1)

    # Medicamentos. Recebido por parametro para o teste poder montar uma cena
    # com uma caixa a menos sem mexer em estado global do modulo.
    for (x, y, c, l, ang) in (CONTEUDO if conteudo is None else conteudo):
        rect = ((x0 + x * s, y0 + y * s), (c * s, l * s), ang)
        pts = cv2.boxPoints(rect).astype(np.int32)
        cor = tuple(int(v) for v in rng.integers(215, 250, 3))
        cv2.fillConvexPoly(quadro, pts, cor)
        cv2.polylines(quadro, [pts], True, (120, 120, 120), 1, cv2.LINE_AA)
        cx, cy = int(rect[0][0]), int(rect[0][1])
        cv2.putText(quadro, "APSEN", (cx - int(c * s * 0.28), cy),
                    cv2.FONT_HERSHEY_SIMPLEX, c * s / 300, (70, 70, 70), 2)

    # Perspectiva (camera nunca perfeitamente perpendicular).
    k = 0.035
    origem = np.float32([[0, 0], [W, 0], [W, H], [0, H]])
    destino = np.float32([[W * k, H * k * 0.6], [W * (1 - k * 0.5), 0],
                          [W, H * (1 - k * 0.4)], [W * k * 0.4, H]])
    quadro = cv2.warpPerspective(quadro, cv2.getPerspectiveTransform(origem, destino),
                                 (W, H), borderValue=(45, 45, 45))

    if reflexo > 0:
        # Mancha especular de LED sobre as embalagens.
        brilho = np.zeros((H, W), dtype=np.float32)
        cv2.circle(brilho, (int(W * 0.62), int(H * 0.42)), int(H * 0.13), 1.0, -1)
        cv2.circle(brilho, (int(W * 0.30), int(H * 0.60)), int(H * 0.08), 1.0, -1)
        brilho = cv2.GaussianBlur(brilho, (0, 0), H * 0.045)
        quadro = np.clip(quadro.astype(np.float32) + brilho[..., None] * reflexo,
                         0, 255).astype(np.uint8)

    if luz != 1.0:
        quadro = np.clip(quadro.astype(np.float32) * luz, 0, 255).astype(np.uint8)

    fator = 700.0 / altura_camera_mm
    quadro = cv2.resize(quadro, None, fx=fator, fy=fator)
    quadro = np.clip(quadro.astype(np.int16) + rng.normal(0, 5, quadro.shape),
                     0, 255).astype(np.uint8)
    return quadro


def anotar(vista, resultado):
    img = vista.copy()
    for item in resultado.itens:
        cv2.polylines(img, [item.poligono.astype(np.int32)], True, (35, 190, 232), 2)
        p = item.poligono.mean(axis=0).astype(int)
        cv2.putText(img, str(item.indice), (p[0] - 8, p[1] + 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (20, 20, 20), 2)
    cv2.rectangle(img, (0, 0), (img.shape[1], 30), (18, 18, 18), -1)
    cv2.putText(img, f"ITENS: {resultado.quantidade}   limiar_conteudo="
                     f"{resultado.limiar_conteudo}   {resultado.qualidade.resumo()}",
                (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (35, 190, 232), 1, cv2.LINE_AA)
    return img


# esperado: numero de itens, ou None quando a leitura CORRETA e recusar.
CASOS = [
    ("altura 550mm", dict(altura_camera_mm=550), 7),
    ("altura 700mm", dict(altura_camera_mm=700), 7),
    ("altura 900mm", dict(altura_camera_mm=900), 7),
    ("luz fraca (60%)", dict(luz=0.6), 7),
    ("luz forte (135%)", dict(luz=1.35), 7),
    ("LED lateral", dict(reflexo=70), 7),
    ("reflexo forte", dict(reflexo=120), None),   # deve RECUSAR
    ("reflexo estourado", dict(reflexo=210), None),   # deve RECUSAR, nao chutar
]


def rodar(lock: bool) -> int:
    falhas = 0
    saida = Path(__file__).resolve().parent.parent / "dados" / "saida_testes"
    saida.parent.mkdir(exist_ok=True)
    saida.mkdir(exist_ok=True)
    print(f"\n=== modo {'LOCK (automatico)' if lock else 'MANUAL (slider fixo)'} ===")

    for nome, kwargs, esperado in CASOS:
        cfg = ConfigMesa()
        cfg.fundo.lock = lock
        cfg.conteudo.lock = lock
        visao = VisaoMesa(cfg)
        frame = gerar_cena(cfg, **kwargs)
        r = visao.processar(frame)

        if r.fundo is None:
            print(f"  {nome:20} FALHA: fundo da caixa nao encontrado")
            falhas += 1
            continue

        ok = r.contagem == esperado
        falhas += 0 if ok else 1
        alvo = "recusar" if esperado is None else str(esperado)
        lido = "recusou" if r.contagem is None else str(r.contagem)
        print(f"  {nome:20} contagem={lido:>7} (esperado {alvo:>7})  "
              f"lf={r.fundo.limiar_usado:3d} lc={r.limiar_conteudo:3d} "
              f"{r.metodo_conteudo:12} patamar={r.patamar:2d} "
              f"reflexo={r.qualidade.reflexo * 100:4.1f}%  "
              f"-> {'OK' if ok else 'FALHA'}")
        if not r.confiavel:
            print(f"  {'':20} motivo: {r.motivo}")

        if lock:
            from visao_mesa import retificar
            vista = retificar(frame, r.fundo)
            cv2.imwrite(str(saida / f"{nome.replace(' ', '_')}.png"), anotar(vista, r))
    return falhas


def testar_encostadas() -> int:
    """Embalagens coladas, sem vao nenhum, em varios angulos.

    E o caso que o watershed por distancia NAO resolve: quatro retangulos
    iguais encostados formam um retangulo perfeito, a transformada de distancia
    vira uma crista continua e nao ha pico por caixa. Quem separa aqui e o
    corte por juncao, que procura o vinco entre as embalagens.
    """
    import math

    def fila(n, angulo, vao=0.0, x0=90.0, y0=100.0, comp=70.0, larg=42.0):
        rad = math.radians(angulo)
        dx, dy = -math.sin(rad) * (larg + vao), math.cos(rad) * (larg + vao)
        return [(x0 + i * dx, y0 + i * dy, comp, larg, angulo) for i in range(n)]

    print("\n=== embalagens encostadas ===")
    falhas = 0

    print("  duas coladas, por angulo:")
    for angulo in (0, 15, 30, 45, 60, 75, 90):
        cfg = ConfigMesa()
        cfg.fundo.lock = True
        cfg.conteudo.lock = True
        frame = gerar_cena(cfg, altura_camera_mm=700, conteudo=fila(2, angulo))
        lido = VisaoMesa(cfg).processar(frame).contagem
        ok = lido == 2
        falhas += 0 if ok else 1
        print(f"    {angulo:3d} graus  contagem={lido} (esperado 2)  "
              f"-> {'OK' if ok else 'FALHA'}")

    print("  quatro em fila, variando o vao:")
    for vao in (3.0, 1.5, 1.0, 0.5, 0.0):
        cfg = ConfigMesa()
        cfg.fundo.lock = True
        cfg.conteudo.lock = True
        conteudo = [(60.0 + i * (42.0 + vao), 60.0, 70.0, 42.0, 90.0) for i in range(4)]
        frame = gerar_cena(cfg, altura_camera_mm=700, conteudo=conteudo)
        lido = VisaoMesa(cfg).processar(frame).contagem
        ok = lido == 4
        falhas += 0 if ok else 1
        print(f"    vao {vao:4.1f} mm  contagem={lido} (esperado 4)  "
              f"-> {'OK' if ok else 'FALHA'}")
    return falhas


def cena_mesa(cfg: ConfigMesa, n: int = 3, semente: int = 3) -> np.ndarray:
    """Embalagens soltas na bancada, SEM bandeja nenhuma.

    E o caso da celula hoje: o maior retangulo do quadro e uma das proprias
    embalagens, nao um recipiente. Com modo 'caixa' o sistema elege uma delas
    como fundo e vai procurar conteudo dentro — encontra zero.
    """
    rng = np.random.default_rng(semente)
    s = 3.0
    W, H = int(440 * s), int(300 * s)
    quadro = np.full((H, W, 3), 150, dtype=np.uint8)          # bancada clara
    yy, xx = np.mgrid[0:H, 0:W]
    quadro = np.clip(quadro.astype(np.int16)
                     + (18 * (1 - xx / W * 0.7))[..., None], 0, 255).astype(np.uint8)
    for i in range(n):
        x, y = 70.0 + i * 120.0, 120.0
        rect = ((x * s, y * s), (75 * s, 150 * s), rng.uniform(-8, 8))
        pts = cv2.boxPoints(rect).astype(np.int32)
        cv2.fillConvexPoly(quadro, pts, tuple(int(v) for v in rng.integers(235, 252, 3)))
        cv2.polylines(quadro, [pts], True, (170, 170, 170), 1, cv2.LINE_AA)
    return np.clip(quadro.astype(np.int16) + rng.normal(0, 4, quadro.shape),
                   0, 255).astype(np.uint8)


def testar_modo_mesa() -> int:
    """Modo 'mesa' conta as embalagens soltas; modo 'caixa' nao tem como."""
    print("\n=== modo mesa (sem bandeja) ===")
    esperado = 3
    falhas = 0

    cfg_caixa = ConfigMesa()
    cfg_caixa.fundo.lock = True
    cfg_caixa.conteudo.lock = True
    frame = cena_mesa(cfg_caixa, esperado)
    lido_caixa = VisaoMesa(cfg_caixa).processar(frame).contagem
    print(f"  modo caixa  contagem={lido_caixa}  "
          f"(esperado errar: nao existe bandeja no quadro)")

    cfg = ConfigMesa()
    cfg.fundo.modo = "mesa"
    cfg.fundo.escala_px_por_mm = 3.0
    cfg.conteudo.lock = True
    lido = VisaoMesa(cfg).processar(frame).contagem
    ok = lido == esperado
    falhas += 0 if ok else 1
    print(f"  modo mesa   contagem={lido} (esperado {esperado})  "
          f"-> {'OK' if ok else 'FALHA'}")
    return falhas


def testar_gate_mudanca() -> int:
    """O gate de mudanca NAO pode deixar passar uma caixa retirada.

    E o teste mais importante do modulo de desempenho: um gate bom demais
    economiza CPU deixando o sistema preso num numero velho — e num sistema de
    contagem isso e pior que gastar CPU a toa. Um gate por diferenca MEDIA
    falhava aqui em silencio.
    """
    print("\n=== gate de mudanca ===")
    cfg = ConfigMesa()
    original = CONTEUDO
    cheia = gerar_cena(cfg, altura_camera_mm=700, conteudo=original)
    faltando = gerar_cena(cfg, altura_camera_mm=700, conteudo=original[:-1])

    falhas = 0
    for lock in (False, True):
        c = ConfigMesa()
        c.fundo.lock = lock
        c.conteudo.lock = lock
        visao = VisaoMesa(c)
        antes = visao.processar(cheia).contagem
        depois = visao.processar(faltando).contagem
        ok = antes == len(original) and depois == len(original) - 1
        falhas += 0 if ok else 1
        print(f"  {'LOCK' if lock else 'MANUAL':7} {antes} -> {depois} "
              f"(esperado {len(original)} -> {len(original) - 1})  "
              f"-> {'OK' if ok else 'FALHA: a retirada passou despercebida'}")
    return falhas


def testar_cantos_manuais() -> int:
    """Modo 'manual': os quatro cantos marcados com o mouse contam igual.

    Dois riscos, os dois testados aqui. O primeiro e a ORDEM: se o operador
    arrastar um canto por cima do outro, a lista sai fora de TL/TR/BR/BL e a
    homografia espelha a vista — o erro nao aparece na marcacao, aparece na
    contagem, depois. O segundo e a NORMALIZACAO: os cantos sao gravados em
    fracao do quadro, entao a mesma marcacao tem de valer em outra resolucao.
    """
    print("\n=== cantos manuais (modo manual) ===")
    from editor_fundo import _ordenar  # noqa: PLC0415

    falhas = 0
    base = np.array([[10, 20], [110, 18], [115, 90], [8, 95]], np.float32)
    for ordem in ([0, 1, 2, 3], [2, 3, 0, 1], [1, 0, 3, 2], [3, 2, 1, 0], [0, 3, 2, 1]):
        ok = np.allclose(_ordenar(base[ordem]), base)
        falhas += 0 if ok else 1
        print(f"  ordem {ordem} -> {'OK' if ok else 'FALHA: quadrilatero girou'}")

    cfg = ConfigMesa()
    esperado = 3
    frame = cena_mesa(cfg, esperado)
    altura, largura = frame.shape[:2]
    # Cantos folgados de proposito: e como um operador marca na mao, com alguns
    # pixels de erro em cada quina. A margem interna tem de absorver isso.
    cfg.fundo.modo = "manual"
    cfg.fundo.cantos_manuais = ((0.04, 0.05), (0.96, 0.04), (0.97, 0.95), (0.03, 0.96))
    cfg.fundo.comprimento_mm = 320.0
    cfg.fundo.largura_mm = 210.0
    cfg.fundo.px_por_mm = 3.0
    cfg.conteudo.lock = True
    lido = VisaoMesa(cfg).processar(frame).contagem
    ok = lido == esperado
    falhas += 0 if ok else 1
    print(f"  contagem={lido} (esperado {esperado})  -> {'OK' if ok else 'FALHA'}")

    # Mesma marcacao, quadro com metade da resolucao: tem de continuar valendo.
    menor = cv2.resize(frame, (largura // 2, altura // 2))
    lido_menor = VisaoMesa(cfg).processar(menor).contagem
    ok = lido_menor == esperado
    falhas += 0 if ok else 1
    print(f"  metade da resolucao contagem={lido_menor} -> "
          f"{'OK' if ok else 'FALHA: a marcacao depende de pixel, nao de fracao'}")
    return falhas


def testar_autoluz() -> int:
    """Auto-ajuste: varre exposicao e foco e para no melhor ponto MEDIDO.

    Camera falsa porque o que se testa aqui e a maquina de estados, nao a
    webcam: ela tem de terminar (nao girar para sempre), tem de fugir da
    exposicao que estoura a embalagem — e nao tem como fazer isso se alguem
    inverter a ordem e varrer o foco antes.
    """
    print("\n=== auto-ajuste de luz e foco ===")
    from autoluz import AutoLuz  # noqa: PLC0415

    cfg = ConfigMesa()
    base = gerar_cena(cfg, altura_camera_mm=700)
    falhas = 0

    class CamFalsa:
        def pedir_aplicacao(self):
            return None

    class LeitorFalso:
        """Brilho segue a exposicao; nitidez cai conforme o foco se afasta."""

        def __init__(self, cfg, foco_certo=140, ganho_base=8, passo=0.55):
            self.cfg, self.n = cfg, 0
            self.foco_certo, self.ganho_base, self.passo = foco_certo, ganho_base, passo

        def ultimo(self):
            self.n += 1
            ganho = 2.0 ** ((self.cfg.camera.exposicao + self.ganho_base) * self.passo)
            img = np.clip(base.astype(np.float32) * ganho, 0, 255).astype(np.uint8)
            sigma = abs(self.cfg.camera.foco - self.foco_certo) / 28.0
            if sigma > 0.3:
                img = cv2.GaussianBlur(img, (0, 0), sigma)
            return img, self.n, 0.0

    leitor = LeitorFalso(cfg)
    auto = AutoLuz(cfg, CamFalsa(), leitor, 1, 1)
    auto.iniciar()
    voltas = 0
    while auto.ativo and voltas < 2000:
        auto.passo()
        voltas += 1
    terminou = auto.fase == "pronto"
    perto = abs(cfg.camera.foco - leitor.foco_certo) <= 12
    falhas += 0 if (terminou and perto) else 1
    print(f"  {voltas} passos, exposicao={cfg.camera.exposicao} foco={cfg.camera.foco} "
          f"(certo ~{leitor.foco_certo})  -> {'OK' if terminou and perto else 'FALHA'}")

    # Bancada com luz sobrando: o vencedor NAO pode ser uma exposicao chapada.
    cfg2 = ConfigMesa()
    l2 = LeitorFalso(cfg2, ganho_base=11, passo=0.7)
    a2 = AutoLuz(cfg2, CamFalsa(), l2, 1, 1)
    a2.iniciar(com_foco=False)
    voltas = 0
    while a2.ativo and voltas < 500:
        a2.passo()
        voltas += 1
    estourado = medir_qualidade(l2.ultimo()[0]).reflexo * 100
    ok = estourado <= 5.0
    falhas += 0 if ok else 1
    print(f"  cena com luz sobrando: exposicao={cfg2.camera.exposicao} "
          f"estourado={estourado:.1f}%  -> "
          f"{'OK' if ok else 'FALHA: escolheu imagem chapada'}")

    a2.iniciar()
    a2.passo()
    a2.cancelar()
    ok = not a2.ativo and a2.fase == "ocioso"
    falhas += 0 if ok else 1
    print(f"  cancelamento no meio -> {'OK' if ok else 'FALHA: ficou preso ativo'}")
    return falhas


def testar_layout_tela() -> int:
    """A janela tem de CABER na tela, com os painéis em cima e embaixo.

    O sintoma que gerou este teste: lado a lado, os dois painéis somavam quase
    1920 px de largura e o Windows cortava o painel da direita — a contagem
    aparecia, as ultimas bounding box nao. Aqui se verifica o tamanho exato, a
    divisao em metades iguais e, principalmente, que nada foi ESTICADO: a vista
    esta em escala mm, e distorcer faz uma embalagem de 77x34mm parecer outra
    proporcao na tela.
    """
    print("\n=== layout da tela ===")
    from desenho import _encaixar, empilhar, tamanho_tela  # noqa: PLC0415

    falhas = 0
    largura, altura = tamanho_tela()
    print(f"  tela medida: {largura}x{altura}")

    cfg = ConfigMesa()
    frame = gerar_cena(cfg, altura_camera_mm=700)          # 4:3, quadro da camera
    resultado = VisaoMesa(cfg).processar(frame)
    from desenho import desenhar_conteudo, desenhar_fundo  # noqa: PLC0415
    from visao_mesa import retificar  # noqa: PLC0415

    alto = desenhar_fundo(frame, resultado)
    baixo = desenhar_conteudo(retificar(frame, resultado.fundo), resultado)

    for rotulo, imagens in [("1 painel", [alto]),
                            ("2 painéis", [alto, baixo]),
                            ("3 painéis", [alto, baixo, resultado.mascara])]:
        medidas = []
        for alvo in [(1366, 691), (1920, 972), (1280, 720)]:
            saida = empilhar(*imagens, tamanho=alvo)
            # Altura exata (as metades tem de fechar a tela) e largura DENTRO do
            # limite — a largura sai a que o conteudo precisa, para nao encher a
            # janela de barra preta.
            ok = saida.shape[0] == alvo[1] and 0 < saida.shape[1] <= alvo[0]
            falhas += 0 if ok else 1
            medidas.append(f"{saida.shape[1]}x{saida.shape[0]}")
            if not ok:
                print(f"  {rotulo} em {alvo}: saiu {saida.shape[1]}x{saida.shape[0]}"
                      f"  -> FALHA")
        print(f"  {rotulo}: {'  '.join(medidas)}  -> OK")

    # Metades iguais, e a de cima e mesmo o primeiro painel.
    alvo = (1366, 692)
    saida = empilhar(alto, baixo, tamanho=alvo)
    meio = alvo[1] // 2
    ok = saida[:meio].shape == saida[meio:meio * 2].shape
    falhas += 0 if ok else 1
    print(f"  metades de mesma altura ({meio} px cada) -> {'OK' if ok else 'FALHA'}")

    # A imagem NAO pode ter faixa preta inutil dos dois lados: a largura tem de
    # ser a que a linha mais larga precisa. Duas imagens 16:9 empilhadas numa
    # tela 1366x691 usam ~615 px, nao 1366.
    esperado = max(int(round(img.shape[1] * meio / img.shape[0]))
                   for img in (alto, baixo))
    ok = abs(saida.shape[1] - esperado) <= 2
    falhas += 0 if ok else 1
    print(f"  largura colada no conteudo: {saida.shape[1]} px "
          f"(painel mais largo pede {esperado}) -> {'OK' if ok else 'FALHA'}")

    # Proporcao preservada: um retangulo 2:1 encaixado numa celula quadrada tem
    # de sair 2:1, com barra escura em cima e embaixo — nunca esticado.
    prova = np.zeros((100, 200, 3), np.uint8)
    prova[:] = (0, 0, 255)
    celula = _encaixar(prova, 300, 300)
    vermelhas = np.where((celula == (0, 0, 255)).all(axis=2))
    alt_util = vermelhas[0].max() - vermelhas[0].min() + 1
    larg_util = vermelhas[1].max() - vermelhas[1].min() + 1
    razao = larg_util / alt_util
    ok = abs(razao - 2.0) < 0.05
    falhas += 0 if ok else 1
    print(f"  proporcao preservada no encaixe: {larg_util}x{alt_util} "
          f"(razao {razao:.2f}, esperado 2.00) -> {'OK' if ok else 'FALHA'}")
    return falhas


def testar_teclas_e_botoes() -> int:
    """Toda tecla tem de passar pelo MESMO tratador, e toda acao tem botao.

    Este teste existe por causa de um bug que chegou na bancada: o laco tinha
    tres `cv2.waitKey` e dois deles tratavam so 'q'/ESC, jogando o resto fora.
    Como o laco gira muito mais rapido que os 30 FPS da camera, a maioria das
    teclas caia justamente nesses dois — e '1', '2' e 'e' pareciam nao
    funcionar, funcionando de vez em quando.

    Nao da para clicar num slider dentro de um teste, entao o que se verifica e
    a ESTRUTURA do arquivo: nenhum `waitKey` solto, e cada acao tambem exposta
    como controle visivel (quando o foco esta numa aba de sliders, o HighGUI do
    Windows nao entrega tecla nenhuma; sem botao, a acao fica inalcancavel).
    """
    print("\n=== teclas e botoes do calibrar ===")
    import ast  # noqa: PLC0415

    fonte = (Path(__file__).resolve().parent.parent / "src" / "calibrar.py").read_text(
        encoding="utf-8")
    arvore = ast.parse(fonte)
    falhas = 0

    def eh_waitkey(no) -> bool:
        return (isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)
                and no.func.attr == "waitKey")

    # Todo waitKey do calibrar tem de estar DENTRO de uma chamada a
    # tratar_tecla — e o que garante que nenhum atalho do laco trate um
    # subconjunto das teclas e descarte o resto.
    protegidos = {
        id(filho)
        for no in ast.walk(arvore)
        if isinstance(no, ast.Call) and isinstance(no.func, ast.Name)
        and no.func.id == "tratar_tecla"
        for filho in ast.walk(no) if eh_waitkey(filho)
    }
    todos = [n for n in ast.walk(arvore) if eh_waitkey(n)]
    soltos = [n.lineno for n in todos if id(n) not in protegidos]
    ok = bool(todos) and not soltos
    falhas += 0 if ok else 1
    print(f"  {len(todos)} waitKey, {len(soltos)} fora do tratador -> "
          f"{'OK' if ok else f'FALHA nas linhas {soltos}'}")

    # Cada acao tambem tem de existir como trackbar, com o painel certo.
    for rotulo, chamadas in [
        ("salvar (nas tres abas)", ["createTrackbar(SALVAR, P1",
                                    "createTrackbar(SALVAR, P2",
                                    "createTrackbar(SALVAR, P3"]),
        ("marcar o fundo com o mouse", ["createTrackbar(MARCAR, P1"]),
        ("auto-ajuste de luz e foco", ["createTrackbar(AUTO, P3"]),
        ("lock do fundo e do conteudo", ['createTrackbar("LOCK", P1',
                                         'createTrackbar("LOCK", P2']),
        ("vista e congelar", ["createTrackbar(VISTA, P1",
                              "createTrackbar(CONGELAR, P1"]),
        ("recarregar do disco", ["createTrackbar(RECARREGAR, P1"]),
    ]:
        faltando = [c for c in chamadas if c not in fonte]
        falhas += 0 if not faltando else 1
        print(f"  controle visivel para {rotulo} -> "
              f"{'OK' if not faltando else f'FALHA: {faltando}'}")
    return falhas


if __name__ == "__main__":
    falhas_botoes = testar_teclas_e_botoes()
    falhas_layout = testar_layout_tela()
    falhas_manual = rodar(lock=False)
    falhas_lock = rodar(lock=True)
    falhas_encostadas = testar_encostadas()
    falhas_mesa = testar_modo_mesa()
    falhas_gate = testar_gate_mudanca()
    falhas_cantos = testar_cantos_manuais()
    falhas_auto = testar_autoluz()
    print(f"\nMANUAL: {falhas_manual} falha(s)   LOCK: {falhas_lock} falha(s)   "
          f"ENCOSTADAS: {falhas_encostadas} falha(s)   MESA: {falhas_mesa} falha(s)   "
          f"GATE: {falhas_gate} falha(s)   CANTOS: {falhas_cantos} falha(s)   "
          f"AUTOLUZ: {falhas_auto} falha(s)   BOTOES: {falhas_botoes} falha(s)   "
          f"LAYOUT: {falhas_layout} falha(s)")
    print("O modo LOCK e o que precisa passar em todos: e ele que roda sem operador.")
    raise SystemExit(1 if (falhas_lock or falhas_encostadas or falhas_mesa
                           or falhas_gate or falhas_cantos or falhas_auto
                           or falhas_botoes or falhas_layout) else 0)
