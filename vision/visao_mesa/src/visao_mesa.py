"""Visao da mesa: fundo da caixa como referencial e contagem do conteudo.

A camera fica acima da mesa, olhando para dentro de uma caixa. A deteccao e
HIERARQUICA, em dois niveis independentes:

    nivel 1  o maior retangulo do quadro e a estrutura do FUNDO DA CAIXA
    nivel 2  todo retangulo circunscrito DENTRO dele e um medicamento

Por que o fundo da caixa vira o referencial, e nao a mesa inteira:

1. **Resolve a altura variavel de graca.** O fundo tem dimensao conhecida em
   mm. Retificar o quadro para esse retangulo fixa a escala px/mm sozinho, a
   qualquer altura de camera, sem marcador auxiliar nenhum.
2. **Elimina o falso positivo de fora da caixa.** O que esta fora do fundo nao
   entra na contagem por construcao, e nao por filtro — reflexo na bancada, mao
   do operador e borda da mesa deixam de existir para o nivel 2.
3. **Da o enquadramento certo para o limiar do conteudo.** O limiar do nivel 2
   e calculado so sobre os pixels de dentro da caixa, onde a iluminacao e mais
   uniforme que no quadro inteiro.

Os dois niveis tem limiar SEPARADO e ajustavel (slider), porque sao problemas
opticos diferentes: o fundo e um contorno grande e de alto contraste contra a
bancada; o conteudo sao retangulos pequenos, encostados, e com o mesmo material
e cor do fundo. Um unico limiar nunca serve para os dois.

Cada limiar tem modo LOCK: o valor manual e suspenso e o sistema escolhe o
melhor limiar do momento pela qualidade da imagem — e o que absorve reflexo de
LED, sombra do braco e mudanca de cor da luminaria sem ninguem tocar no slider.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import cv2
import numpy as np

RAIZ = Path(__file__).resolve().parent.parent
ARQ_MESA = RAIZ / "config" / "mesa.json"


# --------------------------------------------------------------------------- #
# Configuracao
# --------------------------------------------------------------------------- #
@dataclass
class ConfigFundo:
    """Nivel 1 — o referencial onde a contagem acontece."""

    # 'manual' — VOCE marca os quatro cantos com o mouse, uma vez, e eles
    #            ficam salvos. E o melhor dos dois mundos quando o recipiente
    #            existe mas nao tem contorno detectavel (papelao contra
    #            papelao): da correcao de perspectiva e escala real em mm, que
    #            o modo 'mesa' nao tem, sem depender de contraste, que o modo
    #            'caixa' exige. Tecla 'e' no calibrar.py abre o editor.
    # 'caixa' — o maior retangulo do quadro e o fundo de um recipiente, e os
    #           medicamentos estao DENTRO dele. So use quando existir mesmo uma
    #           bandeja ou caixa no quadro: sem ela, o maior retangulo passa a
    #           ser uma das proprias embalagens, o sistema vai procurar conteudo
    #           dentro dela e encontra zero.
    # 'mesa'  — nao ha recipiente. A area util e uma regiao fixa do quadro
    #           (`roi`) e os medicamentos ficam soltos sobre a bancada. Como nao
    #           existe objeto de dimensao conhecida para dar a escala, ela vem
    #           de `escala_px_por_mm`, medida uma vez com regua.
    modo: str = "caixa"

    # Regiao util no modo 'mesa', em fracao do quadro: [x, y, largura, altura].
    # O padrao e o quadro inteiro. Reduzir e a forma mais barata de tirar da
    # conta o que esta na borda da bancada e nao interessa.
    roi: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)

    # Escala do modo 'mesa'. Meca uma regua no quadro: quantos pixels tem
    # 100 mm, divida por 100. Depende da altura da camera — se ela mudar, este
    # numero muda junto, e e por isso que o modo 'caixa' e preferivel quando
    # existe um recipiente de dimensao conhecida.
    escala_px_por_mm: float = 2.0

    # Quatro cantos do modo 'manual', em FRACAO do quadro (0..1), na ordem
    # TL, TR, BR, BL. Guardar normalizado e nao em pixel: assim trocar a
    # resolucao da camera nao invalida a marcacao feita na bancada.
    cantos_manuais: tuple = (
        (0.10, 0.10), (0.90, 0.10), (0.90, 0.90), (0.10, 0.90),
    )

    # 'borda' (Canny) aguenta fundo de cor parecida com a bancada;
    # 'limiar' (binario) e mais barato e basta com bancada contrastante.
    metodo: str = "borda"
    limiar: int = 90                 # slider: 0..255
    limiar_minimo: int = 20          # faixa da busca automatica (modo lock)
    limiar_maximo: int = 220
    lock: bool = False               # True = limiar escolhido pelo sistema

    desfoque: int = 5
    dilatacao: int = 3               # fecha o contorno partido do fundo
    epsilon_poligono: float = 0.02   # tolerancia do approxPolyDP
    area_minima_frac: float = 0.06   # fracao minima do quadro
    area_maxima_frac: float = 0.98

    # Dimensoes reais do fundo da caixa, medidas com trena/paquimetro.
    # Sao elas que dao a escala mm da vista retificada.
    comprimento_mm: float = 300.0
    largura_mm: float = 200.0
    px_por_mm: float = 2.0

    # Estabilidade: o fundo e estatico, entao vale suavizar os cantos entre
    # frames. Sem isso a vista retificada treme e o conteudo "anda" 1-2 mm.
    suavizacao: float = 0.7          # 0 = sem suavizacao, 0.9 = muito lenta
    frames_validade: int = 60        # segura o ultimo fundo valido por N frames


@dataclass
class ConfigConteudo:
    """Nivel 2 — os retangulos circunscritos dentro do fundo."""

    metodo: str = "otsu_desloc"      # 'otsu_desloc' | 'fixo' | 'adaptativo'
    limiar: int = 128                # slider: 0..255
    limiar_minimo: int = 40
    limiar_maximo: int = 220
    lock: bool = False
    inverter: bool = False

    # No modo lock o sistema tambem troca de METODO, nao so de valor: sob luz
    # irregular o adaptativo ganha do global, e o contrario acontece com luz
    # uniforme. Deixar isso no automatico e o que dispensa recalibrar quando a
    # luminaria da celula muda.
    # O sufixo '+norm' liga a normalizacao de iluminacao naquele candidato:
    # dividir a imagem por uma versao muito borrada dela mesma remove o
    # gradiente suave de luz (LED lateral, sombra do braco). Medido nos testes:
    # com luz uniforme a versao SEM normalizacao da um patamar 3x mais largo;
    # com LED lateral so a versao COM normalizacao acerta a contagem. Nenhuma
    # das duas serve sempre — por isso quem escolhe e o lock, a cada leitura.
    metodos_auto: tuple[str, ...] = (
        "otsu_desloc", "otsu_desloc+norm", "adaptativo", "adaptativo+norm",
    )

    # Usada apenas no modo manual; no lock o sufixo do metodo decide.
    normalizar_iluminacao: bool = False
    sigma_iluminacao_mm: float = 25.0

    desfoque: int = 3
    adaptativo_bloco: int = 51
    abertura_mm: float = 1.0
    fechamento_mm: float = 1.0
    preencher_buracos: bool = True

    # Margem interna: descarta a parede e a sombra da caixa, que formam um
    # retangulo perfeito colado na borda e seriam contados como medicamento.
    margem_interna_mm: float = 6.0

    # Filtros do medicamento, em mm (invariantes a altura da camera).
    area_min_mm2: float = 400.0
    area_max_mm2: float = 20000.0
    lado_min_mm: float = 12.0
    lado_max_mm: float = 200.0
    razao_aspecto_max: float = 6.0
    preenchimento_min: float = 0.72

    separar_encostadas: bool = True
    limiar_distancia: float = 0.45

    # --- corte por juncao (embalagens encostadas SEM vao) ---
    # O watershed por distancia so separa o que ja tem um estrangulamento.
    # Quatro caixas iguais coladas formam um retangulo perfeito: a transformada
    # de distancia vira uma crista continua, sem pico por caixa, e nada e
    # separado. O que sobra de sinal nesse caso e o VINCO — a linha reta entre
    # duas embalagens, que existe mesmo sem vao, porque a borda do papelao faz
    # sombra. Este estagio procura linhas retas longas dentro da mancha e corta
    # a mascara ao longo delas.
    cortar_juncoes: bool = True
    juncao_comprimento_min_mm: float = 15.0   # linha menor que isso e texto
    juncao_espessura_mm: float = 0.8          # largura do corte
    juncao_margem_mm: float = 1.5             # ignora bordas do contorno externo
    juncao_sensibilidade: int = 40            # limiar do Canny (alto = 3x)
    # Os pedacos que sobram de um corte bom sao PARECIDOS entre si: uma bandeja
    # guarda o mesmo SKU, e separar duas embalagens iguais devolve duas metades
    # iguais. Um corte ruim, feito sobre uma tarja impressa, devolve um pedaco
    # grande e um caco. Esta e a fracao minima da mediana que o menor pedaco
    # pode ter para o corte ser aceito.
    juncao_uniformidade: float = 0.6


@dataclass
class ConfigAuto:
    """Modo lock: busca do melhor limiar pela qualidade da imagem."""

    # Largura minima do patamar, em UNIDADES de limiar (nao em passos, para o
    # criterio nao mudar quando alguem mexer no passo). Patamar estreito
    # significa que a contagem depende do valor exato do limiar — ou seja, nao
    # ha medida, ha coincidencia.
    patamar_minimo: int = 12

    # Reflexo especular = mancha localizada MUITO mais clara que o resto E com
    # pixels estourados. As duas condicoes juntas, porque cada uma sozinha
    # acusa errado: imagem globalmente clara estoura pixel sem ter reflexo
    # nenhum, e sombra forte cria contraste local sem estourar nada.
    hotspot_critico: float = 0.24
    saturacao_critica: float = 0.10

    # Fracao da mascara que os itens aceitos precisam explicar. Sobrou muito
    # branco dentro da caixa que nao virou medicamento? Entao ha algo ali que o
    # detector nao entendeu — duas embalagens fundidas pelo brilho, um objeto
    # estranho, a parede da caixa entrando. Medido: leitura correta fica acima
    # de 0,80; leitura errada por reflexo cai para ~0,39.
    cobertura_minima: float = 0.60
    # Nitidez RELATIVA: variancia do laplaciano dividida pela variancia da
    # propria imagem. A variancia crua sobe e desce com o contraste, entao uma
    # bancada mais escura derrubava a medida e o sistema recusava por "fora de
    # foco" uma imagem perfeitamente nitida. Medido na bancada real: a crua vai
    # de 65 para 12 so escurecendo a imagem para 40%, enquanto a relativa fica
    # entre 3,2 e 3,6. Desfoque de verdade leva a relativa para 0,06–0,16.
    nitidez_minima: float = 1.0


@dataclass
class ConfigCamera:
    """Webcam externa, com todos os automatismos travados.

    Camera em modo automatico e a causa numero 1 de contagem instavel numa
    celula robotica: quando o braco entra no quadro, o autofoco caca foco e a
    autoexposicao reescalona o brilho — e todo limiar calibrado quebra no mesmo
    instante. Rode camera_finder.py para descobrir indice e backend.
    """

    indice: int = 0
    largura: int = 1280
    altura: int = 720
    fps: int = 30
    fourcc: str = "MJPG"     # sem MJPG o driver manda YUYV e o FPS despenca

    autofoco: bool = False
    foco: int = 30           # 0 = infinito, 255 = macro
    autoexposicao: bool = False
    exposicao: int = -6      # Windows/DSHOW: -13..-1 | Linux/V4L2: 3..2047
    autobalanco_branco: bool = False
    temperatura_cor: int = 4000

    # -1 = nao mexer, deixa o driver decidir. Nem toda webcam expoe os tres.
    ganho: int = -1
    brilho: int = -1
    contraste: int = -1

    espelhar: bool = False
    frames_aquecimento: int = 20


@dataclass
class ConfigDesempenho:
    """Custo de CPU. Nada aqui muda o resultado — so quanto se paga por ele."""

    # A varredura do modo lock roda numa copia reduzida da vista. Como os
    # filtros sao em mm e a escala px/mm e reduzida junto, o resultado nao muda;
    # so custa 4x menos pixels. O limiar escolhido e depois aplicado na vista
    # cheia, entao a medida final continua em resolucao plena.
    escala_varredura: float = 0.5

    # Busca em dois estagios: passada grossa na faixa inteira, depois refino so
    # em volta do melhor. Sai de ~46 deteccoes por metodo para ~20.
    # 8, e nao 16: um patamar estreito (o caso do LED lateral, com 16 unidades)
    # cai entre duas amostras de uma passada grossa demais e o metodo vencedor
    # passa despercebido. Medido: 16 perde esse caso, 8 acerta e custa 90 ms a
    # mais numa busca que so roda quando a cena muda.
    passo_grosso: int = 8
    passo_fino: int = 4
    raio_refino: int = 20

    # Limiar ruim gera centenas de manchas de ruido, e cada uma custaria um
    # contorno. Acima disto a leitura ja esta perdida — melhor abandonar cedo do
    # que gastar 300 ms provando que era lixo.
    max_componentes: int = 250

    # Gate de mudanca: a mesa fica parada quase o tempo todo. Comparar uma
    # miniatura custa microssegundos e evita reprocessar uma cena identica. O
    # trabalho pesado so acontece quando alguem mexe na caixa — que e
    # exatamente quando ele importa.
    gate_mudanca: bool = True
    lado_miniatura: int = 32

    # Limiar sobre a MAIOR diferenca de celula, nao sobre a media. Medido: tirar
    # uma caixa de sete move a media da miniatura de 0,37 para 0,91 — perto
    # demais do ruido do sensor para servir de criterio, e um gate por media
    # deixava a retirada passar despercebida. Na mesma troca, o MAXIMO vai de 1
    # para 57. Uma mudanca real e local e intensa; ruido e difuso e fraco.
    limiar_mudanca: float = 12.0

    # Rede de seguranca: mesmo sem mudanca detectada, reprocessa de tempos em
    # tempos. Um gate que erra deixa o sistema preso num numero velho, e num
    # sistema de contagem isso e pior que gastar CPU a toa.
    intervalo_forcado_s: float = 3.0

    # Teto de reavaliacao do limiar automatico, em segundos.
    intervalo_auto_s: float = 0.7


@dataclass
class ConfigMesa:
    camera: ConfigCamera = field(default_factory=ConfigCamera)
    fundo: ConfigFundo = field(default_factory=ConfigFundo)
    conteudo: ConfigConteudo = field(default_factory=ConfigConteudo)
    auto: ConfigAuto = field(default_factory=ConfigAuto)
    desempenho: ConfigDesempenho = field(default_factory=ConfigDesempenho)

    def salvar(self, caminho: Path | str = ARQ_MESA) -> Path:
        caminho = Path(caminho)
        caminho.parent.mkdir(parents=True, exist_ok=True)
        dados = asdict(self)
        dados["_comentario"] = (
            "Visao da mesa. Gerado por src/calibrar.py. "
            "fundo.limiar e conteudo.limiar sao os dois sliders; "
            "lock=true entrega o limiar ao ajuste automatico."
        )
        caminho.write_text(json.dumps(dados, indent=2, ensure_ascii=False), "utf-8")
        return caminho

    @classmethod
    def carregar(cls, caminho: Path | str = ARQ_MESA) -> "ConfigMesa":
        """Le o arquivo tolerando versoes diferentes do programa.

        Campo que falta vira o padrao; campo que sobra e ignorado com aviso.
        Sem isso, qualquer campo renomeado ou removido numa atualizacao faria o
        programa morrer no arranque com um TypeError — e a config do operador,
        que custou uma bancada inteira para ajustar, e justamente o arquivo que
        nao pode virar refem de uma mudanca interna do codigo.
        """
        caminho = Path(caminho)
        if not caminho.exists():
            return cls()
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
        secoes = {
            "camera": ConfigCamera, "fundo": ConfigFundo, "conteudo": ConfigConteudo,
            "auto": ConfigAuto, "desempenho": ConfigDesempenho,
        }
        partes, ignorados = {}, []
        for nome, classe in secoes.items():
            validos = {f.name for f in fields(classe)}
            dados = {k: v for k, v in bruto.get(nome, {}).items() if not k.startswith("_")}
            ignorados += [f"{nome}.{k}" for k in dados if k not in validos]
            partes[nome] = classe(**{k: v for k, v in dados.items() if k in validos})
        if ignorados:
            print(f"[config] campos ignorados (do programa antigo): {', '.join(ignorados)}")
        return cls(**partes)

# --------------------------------------------------------------------------- #
# Qualidade da imagem
# --------------------------------------------------------------------------- #
@dataclass
class Qualidade:
    nitidez: float          # variancia do laplaciano RELATIVA ao contraste
    reflexo: float          # fracao de pixels saturados (LED, brilho especular)
    contraste: float        # desvio padrao
    brilho: float           # media
    sombra: float           # fracao de pixels quase pretos
    hotspot: float          # o quanto o ponto mais claro se destaca da mediana

    @property
    def reflexo_especular(self) -> bool:
        return self.hotspot >= 0.24 and self.reflexo >= 0.10

    def resumo(self) -> str:
        return (f"nitidez {self.nitidez:.2f} | estourado {self.reflexo * 100:.1f}% "
                f"| hotspot {self.hotspot:.2f} | contraste {self.contraste:.0f}")


def _recortar_quadrilatero(frame: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """Recorte do frame na caixa envolvente do quadrilatero, em pixel de captura.

    Usado para medir foco: interessa a area util, nao o quadro inteiro (a
    bancada em volta pode estar em outro plano e puxar a medida para baixo).
    """
    altura, largura = frame.shape[:2]
    pontos = np.asarray(quad, dtype=np.float32).reshape(-1, 2)
    x0 = int(max(0, np.floor(pontos[:, 0].min())))
    y0 = int(max(0, np.floor(pontos[:, 1].min())))
    x1 = int(min(largura, np.ceil(pontos[:, 0].max())))
    y1 = int(min(altura, np.ceil(pontos[:, 1].max())))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return frame
    return frame[y0:y1, x0:x1]


def medir_nitidez(img: np.ndarray) -> float:
    """Foco, em numero: variancia do laplaciano dividida pela variancia da imagem.

    Dividir pela variancia da propria imagem torna a medida independente do
    quanto a cena esta clara ou escura — o que se quer medir e foco, nao luz.
    Sem essa divisao, baixar a exposicao derrubava a "nitidez" e o sistema
    acusava desfoque onde havia apenas penumbra.
    """
    cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    variancia = float(cinza.std()) ** 2
    return float(cv2.Laplacian(cinza, cv2.CV_64F).var()) / max(variancia, 1e-6) * 100


def medir_qualidade(img: np.ndarray) -> Qualidade:
    """Metricas usadas pelo modo lock para escolher o limiar.

    Reflexo e o que mais atrapalha aqui: um LED batendo na embalagem satura uma
    regiao e cria uma borda que nao existe no objeto. Medir a saturacao permite
    o ajuste automatico fugir da faixa de limiar onde esse brilho vira contorno.
    """
    cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    total = cinza.size or 1
    # Campo de iluminacao: a imagem borrada o suficiente para apagar os objetos
    # e sobrar so a luz. O quanto o ponto mais claro dele se afasta da mediana e
    # a medida de reflexo LOCALIZADO — que e o que apaga a borda da embalagem.
    sigma = max(1.0, cinza.shape[0] * 0.06)
    iluminacao = cv2.GaussianBlur(cinza, (0, 0), sigma)
    hotspot = (float(iluminacao.max()) - float(np.median(iluminacao))) / 255.0
    return Qualidade(
        nitidez=medir_nitidez(cinza),
        reflexo=float((cinza >= 250).sum()) / total,
        contraste=float(cinza.std()),
        brilho=float(cinza.mean()),
        sombra=float((cinza <= 12).sum()) / total,
        hotspot=hotspot,
    )


# --------------------------------------------------------------------------- #
# Nivel 1 — fundo da caixa
# --------------------------------------------------------------------------- #
@dataclass
class Fundo:
    quadrilatero: np.ndarray = field(repr=False)   # 4x2 no frame original
    homografia: np.ndarray = field(repr=False)     # frame -> vista retificada
    tamanho_vista: tuple[int, int]
    px_por_mm: float
    area_frac: float
    limiar_usado: int
    fresco: bool = True

    @property
    def dimensao_mm(self) -> tuple[float, float]:
        w, h = self.tamanho_vista
        return (w / self.px_por_mm, h / self.px_por_mm)


def _ordenar_cantos(pts: np.ndarray) -> np.ndarray:
    """Ordena em TL, TR, BR, BL — sem isso o warp sai espelhado ou girado."""
    pts = pts.reshape(4, 2).astype(np.float32)
    soma = pts.sum(axis=1)
    dif = pts[:, 0] - pts[:, 1]
    return np.array(
        [pts[np.argmin(soma)], pts[np.argmax(dif)],
         pts[np.argmax(soma)], pts[np.argmin(dif)]],
        dtype=np.float32,
    )


def _mascara_fundo(cinza: np.ndarray, cfg: ConfigFundo, limiar: int) -> np.ndarray:
    k = max(1, cfg.desfoque | 1)
    suave = cv2.GaussianBlur(cinza, (k, k), 0)
    if cfg.metodo == "limiar":
        _, m = cv2.threshold(suave, limiar, 255, cv2.THRESH_BINARY)
    else:
        # Canny com razao 1:2 entre os limiares (recomendacao do proprio Canny).
        m = cv2.Canny(suave, max(1, limiar // 2), max(2, limiar))
    if cfg.dilatacao > 0:
        lado = max(1, cfg.dilatacao | 1)
        m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_RECT, (lado, lado)))
        m = cv2.morphologyEx(
            m, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (lado * 3, lado * 3)),
        )
    return m


def _maior_quadrilatero(mascara: np.ndarray, cfg: ConfigFundo, area_quadro: float):
    contornos, _ = cv2.findContours(mascara, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    melhor, melhor_area = None, 0.0
    for contorno in contornos:
        area = cv2.contourArea(contorno)
        frac = area / area_quadro
        if not (cfg.area_minima_frac <= frac <= cfg.area_maxima_frac):
            continue
        if area <= melhor_area:
            continue
        perimetro = cv2.arcLength(contorno, True)
        aprox = cv2.approxPolyDP(contorno, cfg.epsilon_poligono * perimetro, True)
        if len(aprox) == 4 and cv2.isContourConvex(aprox):
            quad = aprox
        else:
            # Caixa com canto arredondado ou parcialmente ocluida nao fecha em 4
            # vertices. O retangulo rotacionado minimo e a melhor aproximacao —
            # e mantem o requisito de "maior retangulo".
            quad = cv2.boxPoints(cv2.minAreaRect(contorno)).astype(np.int32)
            if cv2.contourArea(quad) / area_quadro > cfg.area_maxima_frac:
                continue
        melhor, melhor_area = quad, area
    return melhor, melhor_area


class DetectorFundo:
    """Encontra o fundo da caixa e devolve a homografia para a vista retificada."""

    def __init__(self, cfg: ConfigFundo, cfg_auto: ConfigAuto,
                 desempenho: ConfigDesempenho | None = None):
        self.cfg = cfg
        self.auto = cfg_auto
        self.desempenho = desempenho or ConfigDesempenho()
        self._ultimo: Fundo | None = None
        self._idade = 0
        self._cantos_suaves: np.ndarray | None = None
        self._limiar_auto: int | None = None
        self._assinatura: tuple | None = None
        self._t_auto = 0.0

    # ------------------------------------------------------------------
    def _area_manual(self, frame: np.ndarray) -> Fundo:
        """Modo 'manual': os quatro cantos marcados com o mouse.

        Entrega o que nenhum dos outros dois entrega junto: correcao de
        perspectiva (porque sao quatro pontos, nao um retangulo) e escala real
        em mm (porque as dimensoes do recipiente sao conhecidas), sem depender
        de contraste nenhum. Numa caixa de papelao, onde o fundo e as paredes
        tem a mesma cor e o detector automatico nao acha contorno, e o unico
        modo que fecha a conta.
        """
        altura, largura = frame.shape[:2]
        cantos = np.array(
            [[float(x) * largura, float(y) * altura]
             for x, y in list(self.cfg.cantos_manuais)[:4]],
            dtype=np.float32,
        )
        if len(cantos) != 4:
            return self._area_fixa(frame)

        s = self.cfg.px_por_mm
        larg_vista = max(2, int(round(self.cfg.comprimento_mm * s)))
        alt_vista = max(2, int(round(self.cfg.largura_mm * s)))
        destino = np.array(
            [[0, 0], [larg_vista - 1, 0],
             [larg_vista - 1, alt_vista - 1], [0, alt_vista - 1]],
            dtype=np.float32,
        )
        H = cv2.getPerspectiveTransform(cantos, destino)
        area = cv2.contourArea(cantos.astype(np.float32))
        return Fundo(
            quadrilatero=cantos,
            homografia=H,
            tamanho_vista=(larg_vista, alt_vista),
            px_por_mm=s,
            area_frac=float(area) / float(largura * altura),
            limiar_usado=0,
            fresco=True,
        )

    # ------------------------------------------------------------------
    def _area_fixa(self, frame: np.ndarray) -> Fundo:
        """Modo 'mesa': nao ha recipiente, a area util e uma regiao do quadro.

        Sem recipiente nao ha objeto de dimensao conhecida, e com ele se perde
        a correcao de perspectiva e a escala automatica que o modo 'caixa'
        dava de graca. Em troca, a contagem funciona com as embalagens soltas
        sobre a bancada — que e como a celula opera enquanto nao existe
        bandeja. A escala vem de `escala_px_por_mm`, medida com regua.
        """
        altura, largura = frame.shape[:2]
        fx, fy, fw, fh = self.cfg.roi
        x0 = int(round(np.clip(fx, 0.0, 1.0) * largura))
        y0 = int(round(np.clip(fy, 0.0, 1.0) * altura))
        x1 = int(round(np.clip(fx + fw, 0.0, 1.0) * largura))
        y1 = int(round(np.clip(fy + fh, 0.0, 1.0) * altura))
        x1 = max(x0 + 2, x1)
        y1 = max(y0 + 2, y1)

        # Recorte puro: translada a ROI para a origem, sem escalar. Assim a
        # escala em px/mm e exatamente a que foi medida com a regua.
        H = np.array([[1.0, 0.0, -x0], [0.0, 1.0, -y0], [0.0, 0.0, 1.0]])
        cantos = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float32)
        return Fundo(
            quadrilatero=cantos,
            homografia=H,
            tamanho_vista=(x1 - x0, y1 - y0),
            px_por_mm=self.cfg.escala_px_por_mm,
            area_frac=((x1 - x0) * (y1 - y0)) / float(largura * altura),
            limiar_usado=0,
            fresco=True,
        )

    # ------------------------------------------------------------------
    def limiar_efetivo(self, cinza: np.ndarray, area_quadro: float) -> int:
        if not self.cfg.lock:
            return self.cfg.limiar
        c = self.cfg
        assinatura = (c.metodo, c.limiar_minimo, c.limiar_maximo, c.desfoque,
                      c.dilatacao, c.area_minima_frac, c.area_maxima_frac,
                      c.epsilon_poligono)
        agora = time.monotonic()
        if (self._limiar_auto is not None and self._assinatura == assinatura
                and agora - self._t_auto < self.desempenho.intervalo_auto_s):
            return self._limiar_auto
        # A busca roda numa copia reduzida: o fundo e o maior objeto do quadro,
        # e achar um retangulo grande nao precisa de resolucao cheia. As fracoes
        # de area sao relativas, entao os filtros continuam valendo.
        e = self.desempenho.escala_varredura
        pequena = (cinza if e >= 0.99 else
                   cv2.resize(cinza, None, fx=e, fy=e, interpolation=cv2.INTER_AREA))
        self._limiar_auto = self._buscar_limiar(
            pequena, float(pequena.shape[0] * pequena.shape[1])
        )
        self._assinatura = assinatura
        self._t_auto = agora
        return self._limiar_auto

    def _buscar_limiar(self, cinza: np.ndarray, area_quadro: float) -> int:
        """Escolhe o limiar cujo resultado e o mais ESTAVEL, nao o mais bonito.

        A ideia: varrer a faixa e medir a area do fundo encontrado em cada
        limiar. O limiar correto nao e um ponto isolado — ele fica no meio de um
        patamar largo onde a area praticamente nao muda. Reflexo de LED e sombra
        produzem deteccoes que aparecem e somem a cada poucos passos; escolher o
        centro do patamar mais largo descarta esses automaticamente.
        """
        candidatos = list(range(self.cfg.limiar_minimo,
                                self.cfg.limiar_maximo + 1,
                                max(1, self.desempenho.passo_grosso)))
        areas: list[float] = []
        for limiar in candidatos:
            _, area = _maior_quadrilatero(
                _mascara_fundo(cinza, self.cfg, limiar), self.cfg, area_quadro
            )
            areas.append(area / area_quadro)

        melhor_ini, melhor_fim = 0, 0
        ini = None
        for i, area in enumerate(areas):
            if area <= 0:
                ini = None
                continue
            if ini is None:
                ini = i
                continue
            # Mesmo patamar enquanto a area variar menos de 3%.
            if abs(area - areas[ini]) / max(areas[ini], 1e-6) > 0.03:
                if i - 1 - ini > melhor_fim - melhor_ini:
                    melhor_ini, melhor_fim = ini, i - 1
                ini = i
        if ini is not None and len(areas) - 1 - ini > melhor_fim - melhor_ini:
            melhor_ini, melhor_fim = ini, len(areas) - 1

        if melhor_fim == melhor_ini == 0 and (not areas or areas[0] <= 0):
            return self.cfg.limiar
        return candidatos[(melhor_ini + melhor_fim) // 2]

    # ------------------------------------------------------------------
    def detectar(self, frame: np.ndarray) -> Fundo | None:
        if self.cfg.modo == "manual":
            return self._area_manual(frame)
        if self.cfg.modo == "mesa":
            return self._area_fixa(frame)
        cinza = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        area_quadro = float(frame.shape[0] * frame.shape[1])
        limiar = self.limiar_efetivo(cinza, area_quadro)

        quad, area = _maior_quadrilatero(
            _mascara_fundo(cinza, self.cfg, limiar), self.cfg, area_quadro
        )

        if quad is None:
            if self._ultimo is not None and self._idade < self.cfg.frames_validade:
                self._idade += 1
                velho = self._ultimo
                return Fundo(velho.quadrilatero, velho.homografia, velho.tamanho_vista,
                             velho.px_por_mm, velho.area_frac, velho.limiar_usado,
                             fresco=False)
            return None

        cantos = _ordenar_cantos(quad)
        if self._cantos_suaves is not None and self.cfg.suavizacao > 0:
            a = self.cfg.suavizacao
            cantos = (a * self._cantos_suaves + (1 - a) * cantos).astype(np.float32)
        self._cantos_suaves = cantos

        s = self.cfg.px_por_mm
        largura = max(2, int(round(self.cfg.comprimento_mm * s)))
        altura = max(2, int(round(self.cfg.largura_mm * s)))
        destino = np.array(
            [[0, 0], [largura - 1, 0], [largura - 1, altura - 1], [0, altura - 1]],
            dtype=np.float32,
        )
        H = cv2.getPerspectiveTransform(cantos, destino)

        fundo = Fundo(
            quadrilatero=cantos,
            homografia=H,
            tamanho_vista=(largura, altura),
            px_por_mm=s,
            area_frac=area / area_quadro,
            limiar_usado=limiar,
        )
        self._ultimo = fundo
        self._idade = 0
        return fundo


def retificar(frame: np.ndarray, fundo: Fundo) -> np.ndarray:
    return cv2.warpPerspective(frame, fundo.homografia, fundo.tamanho_vista)


# --------------------------------------------------------------------------- #
# Nivel 2 — conteudo circunscrito
# --------------------------------------------------------------------------- #
def _caixas_por_rotulo(rotulos: np.ndarray) -> dict[int, tuple[int, int, int, int, int]]:
    """(x0, y0, x1, y1, n_pixels) de cada rotulo, numa unica passada.

    A versao ingenua compara `rotulos == r` para cada rotulo, o que percorre a
    imagem INTEIRA uma vez por componente. Com um limiar ruim gerando centenas
    de manchas de ruido, isso vira o gargalo do sistema todo. Aqui os pixels sao
    ordenados por rotulo uma vez so, e cada componente sai por fatiamento.
    """
    planos = rotulos.ravel()
    posicoes = np.flatnonzero(planos)
    if posicoes.size == 0:
        return {}
    marcas = planos[posicoes]
    ys, xs = np.divmod(posicoes, rotulos.shape[1])
    ordem = np.argsort(marcas, kind="stable")
    marcas, ys, xs = marcas[ordem], ys[ordem], xs[ordem]
    unicos, inicios = np.unique(marcas, return_index=True)
    fins = np.append(inicios[1:], marcas.size)

    caixas: dict[int, tuple[int, int, int, int, int]] = {}
    for rotulo, i, f in zip(unicos, inicios, fins):
        fx, fy = xs[i:f], ys[i:f]
        caixas[int(rotulo)] = (int(fx.min()), int(fy.min()),
                               int(fx.max()), int(fy.max()), int(f - i))
    return caixas


@dataclass
class Item:
    """Um medicamento detectado dentro do fundo da caixa."""

    poligono: np.ndarray = field(repr=False)   # 4x2 na vista retificada
    centro_mm: tuple[float, float]
    comprimento_mm: float
    largura_mm: float
    angulo_graus: float
    area_mm2: float
    preenchimento: float
    indice: int = 0

    def para_dict(self) -> dict:
        return {
            "indice": self.indice,
            "centro_x_mm": round(self.centro_mm[0], 2),
            "centro_y_mm": round(self.centro_mm[1], 2),
            "comprimento_mm": round(self.comprimento_mm, 2),
            "largura_mm": round(self.largura_mm, 2),
            "angulo_graus": round(self.angulo_graus, 1),
            "area_mm2": round(self.area_mm2, 1),
        }


@dataclass
class ResultadoMesa:
    fundo: Fundo | None
    itens: list[Item] = field(default_factory=list)
    mascara: np.ndarray | None = field(default=None, repr=False)
    qualidade: Qualidade | None = None
    limiar_conteudo: int = 0
    metodo_conteudo: str = ""
    patamar: int = 0                 # largura da faixa de limiar estavel
    cobertura: float = 0.0           # fracao da mascara explicada pelos itens
    confiavel: bool = False
    motivo: str = ""

    @property
    def quantidade(self) -> int:
        return len(self.itens)

    @property
    def contagem(self) -> int | None:
        """A contagem so existe quando a leitura e confiavel.

        `None` nao e o mesmo que zero, e o resto do sistema precisa dessa
        distincao: zero significa caixa vazia, `None` significa que a imagem
        nao permite afirmar nada. Devolver um numero inventado aqui viraria
        divergencia com o peso e com o ciclo da CNC, ou pior, passaria batido.
        """
        return self.quantidade if self.confiavel else None

    def para_dict(self) -> dict:
        return {
            "quantidade": self.contagem,
            "quantidade_bruta": self.quantidade,
            "confiavel": self.confiavel,
            "motivo": self.motivo,
            "patamar": self.patamar,
            "cobertura": round(self.cobertura, 3),
            "fundo_mm": [round(v, 1) for v in self.fundo.dimensao_mm] if self.fundo else None,
            "fundo_fresco": self.fundo.fresco if self.fundo else False,
            "limiar_fundo": self.fundo.limiar_usado if self.fundo else None,
            "limiar_conteudo": self.limiar_conteudo,
            "metodo_conteudo": self.metodo_conteudo,
            "qualidade": {
                "nitidez": round(self.qualidade.nitidez, 1),
                "reflexo": round(self.qualidade.reflexo, 4),
                "contraste": round(self.qualidade.contraste, 1),
            } if self.qualidade else None,
            "itens": [i.para_dict() for i in self.itens],
        }


class DetectorConteudo:
    def __init__(self, cfg: ConfigConteudo, cfg_auto: ConfigAuto,
                 desempenho: ConfigDesempenho | None = None):
        self.cfg = cfg
        self.auto = cfg_auto
        self.desempenho = desempenho or ConfigDesempenho()
        # Escrito por _binarizar e lido logo em seguida por quem chamou, sempre
        # na mesma thread. Existe para nao ter de mudar a assinatura de _mascara,
        # que e usada em varios lugares que nao se importam com isso.
        self._cortou_juncao = False
        self._cache: tuple[str, int, int] | None = None
        self._cache_assinatura: tuple | None = None
        self._t_auto = 0.0

    # ------------------------------------------------------------------
    def detectar(self, vista: np.ndarray, px_por_mm: float,
                 qualidade: Qualidade | None = None):
        """Devolve (itens, mascara, limiar, metodo, patamar, cobertura).

        `patamar` e a largura, em passos de limiar, da faixa onde a contagem
        nao muda. E a medida de confianca da leitura: patamar largo significa
        que a resposta nao depende do valor exato do slider.
        """
        metodo, limiar, patamar = self._escolher(vista, px_por_mm, qualidade)
        mascara = self._mascara(vista, px_por_mm, limiar, metodo)
        itens = self._itens(mascara, px_por_mm, ja_separado=self._cortou_juncao)
        return itens, mascara, limiar, metodo, patamar, self._cobertura(mascara, itens)

    # ------------------------------------------------------------------
    def _escolher(self, vista, px_por_mm, qualidade) -> tuple[str, int, int]:
        """Metodo e limiar do frame, com cache.

        A varredura e cara e a cena muda devagar. Refazer a escolha a cada frame
        e o que travava o sistema: no modo manual o patamar e so informativo, e
        no lock o limiar bom de 700 ms atras continua bom agora. O cache cai
        sozinho quando o operador mexe em qualquer slider.
        """
        assinatura = self._assinatura()
        agora = time.monotonic()
        if (self._cache is not None
                and self._cache_assinatura == assinatura
                and agora - self._t_auto < self.desempenho.intervalo_auto_s):
            return self._cache

        vista_r, px_r = self._reduzir(vista, px_por_mm)
        if self.cfg.lock:
            escolha = self._buscar(vista_r, px_r)
        else:
            # Manual: o slider manda. O patamar e medido so para mostrar ao
            # operador se o ponto escolhido e estavel ou foi sorte.
            escolha = (self.cfg.metodo, self.cfg.limiar,
                       self._largura_patamar(vista_r, px_r, self.cfg.metodo,
                                             self.cfg.limiar))
        self._cache = escolha
        self._cache_assinatura = assinatura
        self._t_auto = agora
        return escolha

    def _assinatura(self) -> tuple:
        """Os campos que, se mudarem, invalidam a escolha em cache.

        Com o LOCK ligado, o limiar e o metodo do slider sao ignorados pela
        busca — entao arrastar esses dois controles nao pode invalidar o cache.
        Incluir-los custava uma busca completa por frame arrastado e fazia a
        janela parecer travada justamente no momento em que o operador mexe.
        """
        c = self.cfg
        manual = () if c.lock else (c.metodo, c.limiar, c.normalizar_iluminacao)
        return manual + (c.lock, c.inverter,
                         c.limiar_minimo, c.limiar_maximo, c.margem_interna_mm,
                         c.area_min_mm2, c.area_max_mm2, c.lado_min_mm, c.lado_max_mm,
                         c.razao_aspecto_max, c.preenchimento_min, c.separar_encostadas,
                         c.limiar_distancia, c.abertura_mm, c.fechamento_mm, c.desfoque)

    def _reduzir(self, vista, px_por_mm):
        """Copia reduzida para a varredura.

        Reduzir a imagem E a escala px/mm na mesma proporcao mantem todos os
        filtros (que sao em mm) valendo igual — o que muda e so o custo. O
        limiar encontrado aqui e aplicado depois na vista cheia.
        """
        e = self.desempenho.escala_varredura
        if e >= 0.99:
            return vista, px_por_mm
        return (cv2.resize(vista, None, fx=e, fy=e, interpolation=cv2.INTER_AREA),
                px_por_mm * e)

    def _varrer(self, vista, px_por_mm, metodo, candidatos,
                rapido: bool = True) -> list[tuple[int, int, float]]:
        """Varre a faixa de limiares medindo quantos itens cada um produz.

        `rapido` desliga o watershed durante a varredura. Ele custa ~4 ms por
        limiar e serve para separar caixas encostadas — mas a POSICAO do patamar
        nao muda por causa dele: duas caixas coladas continuam coladas em todos
        os limiares. Ele volta a valer na medicao final, em resolucao cheia.
        """
        base, normalizar = self._partir(metodo)
        cinza = self._preparar(vista, px_por_mm, normalizar)
        leituras = []
        for limiar in candidatos:
            mascara = self._binarizar(cinza, px_por_mm, limiar, base, rapido=rapido)
            itens = self._itens(mascara, px_por_mm, rapido=rapido)
            preench = float(np.mean([i.preenchimento for i in itens])) if itens else 0.0
            leituras.append((limiar, len(itens), preench))
        return leituras

    @staticmethod
    def _cobertura(mascara: np.ndarray, itens: list) -> float:
        """Quanto da mascara os itens aceitos explicam.

        E o que distingue uma leitura certa de uma errada quando as duas sao
        estaveis. Quando o brilho funde duas embalagens, o blob resultante e
        grande demais e os filtros o rejeitam — a area continua na mascara, mas
        deixa de ser explicada. Largura de patamar sozinha nao ve isso: um
        metodo saturado pelo reflexo tem patamar LARGO justamente porque parou
        de responder ao limiar.
        """
        total = float(np.count_nonzero(mascara))
        if total <= 0:
            return 0.0
        aceito = np.zeros_like(mascara)
        for item in itens:
            cv2.fillConvexPoly(aceito, item.poligono.astype(np.int32), 255)
        return float(np.count_nonzero((aceito > 0) & (mascara > 0))) / total

    @staticmethod
    def _patamares(leituras):
        """Agrupa a varredura em faixas contiguas de contagem constante."""
        blocos, i = [], 0
        while i < len(leituras):
            j = i
            while j + 1 < len(leituras) and leituras[j + 1][1] == leituras[i][1]:
                j += 1
            blocos.append((i, j, leituras[i][1]))
            i = j + 1
        return blocos

    def _melhor_patamar(self, leituras, passo):
        """(pontuacao, metodo_limiar, largura) do patamar mais largo e mais cheio."""
        melhor, melhor_pontuacao = None, -1.0
        for ini, fim, quantidade in self._patamares(leituras):
            if quantidade <= 0:
                continue
            # Largura em unidades de limiar: comparavel entre metodos e
            # independente do passo da varredura.
            largura = leituras[fim][0] - leituras[ini][0] + passo
            preench = float(np.mean([l[2] for l in leituras[ini:fim + 1]]))
            pontuacao = largura + preench
            if pontuacao > melhor_pontuacao:
                melhor_pontuacao = pontuacao
                melhor = (leituras[(ini + fim) // 2][0], largura)
        return melhor_pontuacao, melhor

    def _largura_patamar(self, vista, px_por_mm, metodo, limiar) -> int:
        passo = max(1, self.desempenho.passo_fino)
        raio = self.desempenho.raio_refino
        candidatos = range(max(0, limiar - raio), min(255, limiar + raio) + 1, passo)
        leituras = self._varrer(vista, px_por_mm, metodo, candidatos)
        for ini, fim, quantidade in self._patamares(leituras):
            if quantidade > 0 and leituras[ini][0] <= limiar <= leituras[fim][0]:
                return leituras[fim][0] - leituras[ini][0] + passo
        return 0

    def _buscar(self, vista, px_por_mm) -> tuple[str, int, int]:
        """Escolhe metodo e limiar em dois criterios, um dentro e outro entre.

        DENTRO de cada metodo, o limiar certo e o centro do patamar mais largo:
        a contagem correta sobrevive a dezenas de niveis de limiar, enquanto um
        reflexo so vira item numa faixa estreita e some no passo seguinte.

        ENTRE metodos, largura de patamar nao serve de criterio — e apples com
        oranges. Um metodo cego pelo reflexo tem patamar LARGO exatamente porque
        parou de responder ao limiar, e ganharia do metodo que enxerga. Quem
        decide aqui e a COBERTURA: quanto da mascara os itens aceitos explicam.
        Medido no caso de LED lateral: o metodo errado cobre 0,39 e o certo
        0,82, enquanto os patamares eram 20 e 16 — a largura apontava para o
        lado errado.

        A varredura roda em dois estagios (grosso na faixa inteira, fino em
        volta do vencedor) porque varrer tudo no passo fino em todos os metodos
        custava mais de 10 s por frame.
        """
        d = self.desempenho
        faixa = (self.cfg.limiar_minimo, self.cfg.limiar_maximo)
        melhor = (self.cfg.metodo, self.cfg.limiar, 0)
        melhor_cobertura, melhor_largura = -1.0, -1

        for metodo in self.cfg.metodos_auto:
            leituras = self._varrer(
                vista, px_por_mm, metodo,
                range(faixa[0], faixa[1] + 1, max(1, d.passo_grosso)),
            )
            _p, achado = self._melhor_patamar(leituras, d.passo_grosso)
            if achado is None:
                continue

            centro = achado[0]
            leituras = self._varrer(
                vista, px_por_mm, metodo,
                range(max(faixa[0], centro - d.raio_refino),
                      min(faixa[1], centro + d.raio_refino) + 1,
                      max(1, d.passo_fino)),
            )
            _p, refinado = self._melhor_patamar(leituras, d.passo_fino)
            limiar, largura = refinado if refinado else (centro, d.passo_grosso)

            # Cobertura no ponto escolhido, com o watershed ligado: e a medida
            # final deste metodo, nao mais uma amostra da varredura.
            mascara = self._mascara(vista, px_por_mm, limiar, metodo)
            itens = self._itens(mascara, px_por_mm, ja_separado=self._cortou_juncao)
            if not itens:
                continue
            cobertura = self._cobertura(mascara, itens)

            # Empate em cobertura (diferenca < 5 pontos) vai para o patamar mais
            # largo: entre duas leituras igualmente bem explicadas, prefere a
            # que depende menos do valor exato do limiar.
            if (cobertura > melhor_cobertura + 0.05
                    or (abs(cobertura - melhor_cobertura) <= 0.05
                        and largura > melhor_largura)):
                melhor_cobertura, melhor_largura = cobertura, largura
                melhor = (metodo, limiar, largura)

        return melhor

    # ------------------------------------------------------------------
    def _mascara(self, vista: np.ndarray, px_por_mm: float, limiar: int,
                 metodo: str | None = None) -> np.ndarray:
        metodo = metodo or self.cfg.metodo
        base, normalizar = self._partir(metodo)
        return self._binarizar(
            self._preparar(vista, px_por_mm, normalizar), px_por_mm, limiar, base
        )

    def _partir(self, metodo: str) -> tuple[str, bool]:
        """Separa 'otsu_desloc+norm' em ('otsu_desloc', True)."""
        if metodo.endswith("+norm"):
            return metodo[:-5], True
        return metodo, self.cfg.normalizar_iluminacao

    def _preparar(self, vista: np.ndarray, px_por_mm: float, normalizar: bool) -> np.ndarray:
        """Pre-processamento que NAO depende do limiar.

        Separar isto do limiar e o que torna a varredura viavel: a normalizacao
        de iluminacao e um desfoque gaussiano de raio grande, o passo mais caro
        do pipeline (16 ms). Recalcula-lo a cada limiar custava isso vezes
        dezenas de limiares; calculado uma vez por metodo, some da conta.
        """
        cinza = cv2.cvtColor(vista, cv2.COLOR_BGR2GRAY) if vista.ndim == 3 else vista
        k = max(1, self.cfg.desfoque | 1)
        cinza = cv2.GaussianBlur(cinza, (k, k), 0)
        if normalizar:
            # Divide pela propria imagem muito borrada: sobra o detalhe local,
            # sem o gradiente de iluminacao. Um LED lateral deixa de deslocar o
            # limiar de um lado da caixa para o outro.
            sigma = max(1.0, self.cfg.sigma_iluminacao_mm * px_por_mm)
            cinza = cv2.divide(cinza, cv2.GaussianBlur(cinza, (0, 0), sigma), scale=128)
        return cinza

    def _binarizar(self, cinza: np.ndarray, px_por_mm: float, limiar: int,
                   metodo: str, rapido: bool = False) -> np.ndarray:
        cfg = self.cfg
        if metodo == "adaptativo":
            bloco = max(3, cfg.adaptativo_bloco | 1)
            # O slider vira o C do adaptativo, centrado em 128.
            mascara = cv2.adaptiveThreshold(
                cinza, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,
                bloco, (limiar - 128) / 8.0,
            )
        elif metodo == "otsu_desloc":
            # Otsu acha o ponto de equilibrio da cena; o slider desloca em torno
            # dele. Assim o mesmo valor de slider continua valendo quando a luz
            # da bancada muda — que e o caso real de uma celula com LED.
            base, _ = cv2.threshold(cinza, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            alvo = float(np.clip(base + (limiar - 128), 0, 255))
            _, mascara = cv2.threshold(cinza, alvo, 255, cv2.THRESH_BINARY)
        else:
            _, mascara = cv2.threshold(cinza, limiar, 255, cv2.THRESH_BINARY)

        if cfg.inverter:
            mascara = cv2.bitwise_not(mascara)

        def kernel_mm(mm: float):
            lado = max(1, int(round(mm * px_por_mm)) | 1)
            return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (lado, lado))

        if cfg.abertura_mm > 0:
            mascara = cv2.morphologyEx(mascara, cv2.MORPH_OPEN, kernel_mm(cfg.abertura_mm))
        if cfg.fechamento_mm > 0:
            mascara = cv2.morphologyEx(mascara, cv2.MORPH_CLOSE, kernel_mm(cfg.fechamento_mm))
        if cfg.preencher_buracos:
            # Preenche vazios internos (texto e logo impressos) pelo contorno
            # externo. Um fechamento grande o bastante para tapa-los tambem
            # soldaria duas caixas vizinhas; assim o tamanho do buraco some.
            contornos, _h = cv2.findContours(mascara, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cheia = np.zeros_like(mascara)
            cv2.drawContours(cheia, contornos, -1, 255, cv2.FILLED)
            mascara = cheia

        # Corte por juncao vem DEPOIS do preenchimento: preencher buracos usa o
        # contorno externo e refecharia qualquer corte feito antes.
        # Durante a varredura o corte fica de fora, pela mesma razao que o
        # watershed: ele nao muda ONDE fica o patamar de limiar, so o que se
        # extrai depois. Rodando na copia reduzida, a razao entre o comprimento
        # minimo da linha e o tamanho da embalagem muda, e o corte passava a
        # disparar em lugares que nao dispara na resolucao cheia — levando o
        # lock a escolher um metodo pelo motivo errado.
        cortou = False
        if cfg.cortar_juncoes and not rapido:
            mascara, cortou = self._cortar_juncoes(mascara, cinza, px_por_mm)

        # Margem interna: apaga parede e sombra da caixa, que formam um anel
        # retangular perfeito colado na borda e virariam "medicamento".
        margem = int(round(cfg.margem_interna_mm * px_por_mm))
        if margem > 0:
            valida = np.zeros_like(mascara)
            cv2.rectangle(
                valida, (margem, margem),
                (mascara.shape[1] - margem - 1, mascara.shape[0] - margem - 1),
                255, -1,
            )
            mascara = cv2.bitwise_and(mascara, valida)
        self._cortou_juncao = cortou
        return mascara

    # ------------------------------------------------------------------
    def _cortar_juncoes(self, mascara: np.ndarray, cinza: np.ndarray,
                        px_por_mm: float) -> tuple[np.ndarray, bool]:
        """Corta a mascara ao longo do vinco entre embalagens encostadas.

        Procura linhas RETAS e LONGAS dentro da mancha. O comprimento minimo e o
        que separa vinco de texto impresso: a divisa entre duas caixas atravessa
        a embalagem inteira, enquanto uma letra tem alguns milimetros.

        O corte so e aceito se aumentar o numero de manchas com tamanho de
        medicamento. Se ele picar uma caixa em fragmentos pequenos demais — o
        que acontece quando a arte da embalagem tem uma tarja reta impressa —
        a contagem de manchas validas CAI, e a versao sem corte e mantida.
        Sem essa verificacao, o estagio trocaria um erro por outro.
        """
        cfg = self.cfg
        margem = max(1, int(round(cfg.juncao_margem_mm * px_por_mm)) | 1)
        interior = cv2.erode(
            mascara, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (margem, margem))
        )
        if not interior.any():
            return mascara, False

        bordas = cv2.bitwise_and(
            cv2.Canny(cinza, cfg.juncao_sensibilidade, cfg.juncao_sensibilidade * 3),
            interior,
        )
        if not bordas.any():
            return mascara, False

        comprimento = max(5, int(round(cfg.juncao_comprimento_min_mm * px_por_mm)))
        linhas = cv2.HoughLinesP(
            bordas, 1, np.pi / 180,
            threshold=max(10, comprimento // 2),
            minLineLength=comprimento,
            maxLineGap=max(1, int(round(2 * px_por_mm))),
        )
        if linhas is None:
            return mascara, False

        espessura = max(1, int(round(cfg.juncao_espessura_mm * px_por_mm)))
        corte = np.zeros_like(mascara)
        # O formato varia entre versoes do OpenCV ((N,1,4) ou (N,4)); o reshape
        # cobre as duas sem depender da versao instalada na maquina.
        for x1, y1, x2, y2 in np.asarray(linhas).reshape(-1, 4):
            cv2.line(corte, (int(x1), int(y1)), (int(x2), int(y2)), 255, espessura,
                     lineType=cv2.LINE_4)
        cortada = cv2.subtract(mascara, corte)

        if self._corte_util(mascara, cortada, px_por_mm):
            return cortada, True
        return mascara, False

    def _corte_util(self, antes: np.ndarray, depois: np.ndarray,
                    px_por_mm: float) -> bool:
        """O corte separou embalagens, ou picou uma delas?

        Duas condicoes. A primeira e obvia: tem de sobrar MAIS manchas com
        tamanho de medicamento do que antes. A segunda e a que importa de
        verdade: os pedacos precisam ser PARECIDOS entre si. Uma bandeja guarda
        o mesmo SKU, entao separar duas embalagens iguais devolve duas metades
        iguais; cortar em cima de uma tarja impressa devolve um pedaco grande e
        um caco.

        Sem a segunda condicao, um caco de 30x18 mm passava como se fosse uma
        caixa: area acima do minimo, contagem subindo de 4 para 5. Era trocar
        um erro por outro.
        """
        areas_antes = self._areas_validas(antes, px_por_mm)
        areas_depois = self._areas_validas(depois, px_por_mm)
        if len(areas_depois) <= len(areas_antes):
            return False
        if len(areas_depois) >= 2:
            mediana = float(np.median(areas_depois))
            if mediana <= 0:
                return False
            if areas_depois.min() < self.cfg.juncao_uniformidade * mediana:
                return False
        return True

    def _areas_validas(self, mascara: np.ndarray, px_por_mm: float) -> np.ndarray:
        """Areas das manchas com tamanho compativel com um medicamento.

        Criterio barato (so area, sem contorno) para julgar um corte.
        """
        # Conectividade 4: um corte na diagonal deixa os dois lados tocando
        # pelos cantos, e com conectividade 8 eles continuam sendo UMA mancha —
        # o corte parecia nao ter separado nada. Medido: com conectividade 8,
        # duas caixas encostadas a 45 graus continuavam contando como uma, e so
        # engrossando o corte para 1,5 mm elas se separavam; mas engrossar come
        # 1,5 mm da medida de cada embalagem, o que nao e aceitavel num numero
        # que vai para a CNC.
        n, _rotulos, stats, _c = cv2.connectedComponentsWithStats(mascara, connectivity=4)
        if n <= 1:
            return np.empty(0)
        areas = stats[1:, cv2.CC_STAT_AREA]
        minimo = self.cfg.area_min_mm2 * px_por_mm ** 2
        maximo = self.cfg.area_max_mm2 * px_por_mm ** 2
        return areas[(areas >= minimo) & (areas <= maximo)]

    # ------------------------------------------------------------------
    def _rotular(self, mascara: np.ndarray, rapido: bool = False,
                 ja_separado: bool = False):
        # Ver a nota em _areas_validas: depois de um corte diagonal, so a
        # conectividade 4 enxerga os dois lados como manchas distintas.
        conectividade = 4 if ja_separado else 8
        n_comp, componentes = cv2.connectedComponents(mascara, connectivity=conectividade)
        # `ja_separado`: o corte por juncao acabou de dividir as embalagens.
        # Rodar o watershed por cima disso so acrescenta risco — ele passa a
        # procurar divisa DENTRO de cada embalagem ja isolada e, com a
        # transformada de distancia de um retangulo, acha uma. Medido: de 4
        # caixas separadas corretamente pelo corte, o watershed fazia 5.
        if rapido or ja_separado or not self.cfg.separar_encostadas:
            return componentes, n_comp

        # Abandono cedo: limiar ruim gera centenas de manchas. Continuar so
        # gastaria CPU para produzir uma leitura que os filtros descartariam.
        if n_comp - 1 > self.desempenho.max_componentes:
            return componentes, n_comp

        dist = cv2.distanceTransform(mascara, cv2.DIST_L2, 5)
        if dist.max() <= 0:
            return np.zeros_like(mascara, dtype=np.int32), 1

        # Normalizacao POR COMPONENTE: com o maximo global, um medicamento
        # pequeno ao lado de um grande nunca alcanca o limiar e some da
        # contagem — e a contagem aqui precisa ser exata. O recorte por caixa
        # evita varrer a imagem inteira uma vez por componente.
        normalizada = np.zeros_like(dist)
        for rotulo, (x0, y0, x1, y1, _n) in _caixas_por_rotulo(componentes).items():
            janela = (slice(y0, y1 + 1), slice(x0, x1 + 1))
            seletor = componentes[janela] == rotulo
            recorte = dist[janela]
            pico = recorte[seletor].max()
            if pico > 0:
                alvo = normalizada[janela]
                alvo[seletor] = recorte[seletor] / pico

        picos = (normalizada >= self.cfg.limiar_distancia).astype(np.uint8) * 255
        n_sementes, sementes = cv2.connectedComponents(picos)
        if n_sementes <= 1:
            return componentes.astype(np.int32), n_comp

        fundo_dilatado = cv2.dilate(
            mascara, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)), iterations=2
        )
        desconhecido = cv2.subtract(fundo_dilatado, picos)
        marcadores = sementes.astype(np.int32) + 1
        marcadores[desconhecido == 255] = 0
        cv2.watershed(cv2.cvtColor(mascara, cv2.COLOR_GRAY2BGR), marcadores)
        rotulos = np.where(marcadores > 1, marcadores, 0).astype(np.int32)
        return rotulos, int(marcadores.max()) + 1

    # ------------------------------------------------------------------
    def _itens(self, mascara: np.ndarray, px_por_mm: float,
               rapido: bool = False, ja_separado: bool = False) -> list[Item]:
        cfg = self.cfg
        rotulos, _n = self._rotular(mascara, rapido=rapido, ja_separado=ja_separado)
        caixas = _caixas_por_rotulo(rotulos)
        if len(caixas) > self.desempenho.max_componentes:
            return []

        # Pre-filtros baratos, antes de qualquer contorno: contagem de pixels e
        # tamanho da caixa envolvente ja eliminam ruido e mancha gigante. O
        # contorno, que e a parte cara, so roda no que tem chance de passar.
        area_min_px = cfg.area_min_mm2 * px_por_mm ** 2
        area_max_px = cfg.area_max_mm2 * px_por_mm ** 2
        lado_min_px = cfg.lado_min_mm * px_por_mm
        lado_max_px = cfg.lado_max_mm * px_por_mm

        itens: list[Item] = []
        for rotulo, (x0, y0, x1, y1, n_pixels) in caixas.items():
            if not (area_min_px <= n_pixels <= area_max_px):
                continue
            largura_px, altura_px = x1 - x0 + 1, y1 - y0 + 1
            if max(largura_px, altura_px) < lado_min_px:
                continue
            if min(largura_px, altura_px) > lado_max_px:
                continue

            janela = (slice(y0, y1 + 1), slice(x0, x1 + 1))
            recorte = (rotulos[janela] == rotulo).astype(np.uint8) * 255
            contornos, _h = cv2.findContours(
                recorte, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE, offset=(x0, y0)
            )
            if not contornos:
                continue
            contorno = max(contornos, key=cv2.contourArea)

            area_px = cv2.contourArea(contorno)
            area_mm2 = area_px / (px_por_mm ** 2)
            if not (cfg.area_min_mm2 <= area_mm2 <= cfg.area_max_mm2):
                continue

            (cx, cy), (w_px, h_px), angulo = cv2.minAreaRect(contorno)
            comprimento = max(w_px, h_px) / px_por_mm
            largura = min(w_px, h_px) / px_por_mm
            if largura <= 0 or not (cfg.lado_min_mm <= largura and comprimento <= cfg.lado_max_mm):
                continue
            if comprimento / largura > cfg.razao_aspecto_max:
                continue

            # Preenchimento separa uma embalagem (retangulo cheio) de um reflexo
            # ou sombra em L, que tem a mesma area mas nao preenche o retangulo.
            preenchimento = area_px / ((w_px * h_px) or 1.0)
            if preenchimento < cfg.preenchimento_min:
                continue

            itens.append(
                Item(
                    poligono=cv2.boxPoints(((cx, cy), (w_px, h_px), angulo)),
                    centro_mm=(cx / px_por_mm, cy / px_por_mm),
                    comprimento_mm=comprimento,
                    largura_mm=largura,
                    angulo_graus=float(angulo),
                    area_mm2=area_mm2,
                    preenchimento=preenchimento,
                )
            )

        # Ordem estavel (varredura por linha): a CNC recebe sempre na mesma
        # sequencia, e o indice de cada item nao pula entre frames.
        itens.sort(key=lambda i: (round(i.centro_mm[1], 1), i.centro_mm[0]))
        for indice, item in enumerate(itens, start=1):
            item.indice = indice
        return itens

# --------------------------------------------------------------------------- #
# Pipeline completo
# --------------------------------------------------------------------------- #
class VisaoMesa:
    """Junta os dois niveis. E a classe que a estacao usa."""

    def __init__(self, cfg: ConfigMesa | None = None):
        self.cfg = cfg or ConfigMesa.carregar()
        d = self.cfg.desempenho
        self.detector_fundo = DetectorFundo(self.cfg.fundo, self.cfg.auto, d)
        self.detector_conteudo = DetectorConteudo(self.cfg.conteudo, self.cfg.auto, d)
        self._miniatura: np.ndarray | None = None
        self._ultimo_resultado: ResultadoMesa | None = None
        self._assinatura_cfg: tuple | None = None
        self._t_forcado = 0.0

    # ------------------------------------------------------------------
    def _miniatura_de(self, frame: np.ndarray) -> np.ndarray:
        lado = max(8, self.cfg.desempenho.lado_miniatura)
        cinza = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        return cv2.resize(cinza, (lado, lado), interpolation=cv2.INTER_AREA).astype(np.int16)

    def _cena_parada(self, frame: np.ndarray) -> bool:
        """A mesa fica parada quase o tempo todo.

        Comparar uma miniatura 16x16 custa microssegundos e evita reprocessar
        uma cena identica. O trabalho pesado so acontece quando alguem mexe na
        caixa — que e exatamente quando ele importa. Mesma ideia do gate de
        mudanca da visao dos dispensers.
        """
        if not self.cfg.desempenho.gate_mudanca or self._ultimo_resultado is None:
            return False

        # Mexer num slider tem de reprocessar mesmo com a cena parada — senao a
        # tela da calibragem congela e o operador acha que o controle quebrou.
        assinatura = (self.detector_conteudo._assinatura(),
                      self.cfg.fundo.limiar, self.cfg.fundo.lock,
                      self.cfg.fundo.metodo, self.cfg.fundo.dilatacao,
                      self.cfg.fundo.area_minima_frac,
                      self.cfg.fundo.comprimento_mm, self.cfg.fundo.largura_mm)
        if assinatura != self._assinatura_cfg:
            self._assinatura_cfg = assinatura
            return False

        agora = time.monotonic()
        if agora - self._t_forcado >= self.cfg.desempenho.intervalo_forcado_s:
            self._t_forcado = agora
            self._miniatura = self._miniatura_de(frame)
            return False

        atual = self._miniatura_de(frame)
        if self._miniatura is None:
            self._miniatura = atual
            return False

        # MAIOR diferenca de celula, nao a media: uma caixa retirada muda pouco
        # da imagem inteira, mas muda MUITO onde ela estava.
        diferenca = float(np.abs(atual - self._miniatura).max())
        if diferenca <= self.cfg.desempenho.limiar_mudanca:
            return True
        self._miniatura = atual
        return False

    def processar(self, frame: np.ndarray) -> ResultadoMesa:
        if self._cena_parada(frame):
            return self._ultimo_resultado  # type: ignore[return-value]
        fundo = self.detector_fundo.detectar(frame)
        if fundo is None:
            self._ultimo_resultado = ResultadoMesa(
                fundo=None, qualidade=medir_qualidade(frame),
                confiavel=False, motivo="fundo da caixa nao encontrado",
            )
            return self._ultimo_resultado

        vista = retificar(frame, fundo)
        qualidade = medir_qualidade(vista)
        # Foco e propriedade da CAPTURA, nao da vista retificada. A vista e uma
        # reamostragem: se a homografia amplia (quadrilatero pequeno no quadro,
        # px_por_mm alto), a interpolacao borra e a nitidez cai mesmo com a
        # lente perfeita; se reduz, sobe e uma bancada realmente desfocada
        # passaria. Medir no recorte do quadro tira `px_por_mm` da conta —
        # senao o veredito de foco dependia de um numero de escrituracao.
        qualidade.nitidez = medir_nitidez(_recortar_quadrilatero(frame, fundo.quadrilatero))
        itens, mascara, limiar, metodo, patamar, cobertura = (
            self.detector_conteudo.detectar(vista, fundo.px_por_mm, qualidade)
        )

        confiavel, motivo = self._avaliar(qualidade, patamar, cobertura, itens)
        self._ultimo_resultado = ResultadoMesa(
            fundo=fundo, itens=itens, mascara=mascara, qualidade=qualidade,
            limiar_conteudo=limiar, metodo_conteudo=metodo, patamar=patamar,
            cobertura=cobertura, confiavel=confiavel, motivo=motivo,
        )
        return self._ultimo_resultado

    def _avaliar(self, qualidade: Qualidade, patamar: int,
                 cobertura: float, itens: list) -> tuple[bool, str]:
        """Decide se a leitura pode virar numero — ou se o sistema deve calar.

        Os tres motivos de recusa sao os tres jeitos de a imagem mentir:
        reflexo apaga a borda entre embalagem e fundo; desfoque funde caixas
        vizinhas; e patamar estreito significa que a contagem so existe naquele
        valor exato de limiar, ou seja, e coincidencia e nao medida.
        """
        auto = self.cfg.auto
        if (qualidade.hotspot >= auto.hotspot_critico
                and qualidade.reflexo >= auto.saturacao_critica):
            return False, (f"reflexo especular (hotspot {qualidade.hotspot:.2f}, "
                           f"{qualidade.reflexo * 100:.0f}% estourado) — a borda da "
                           "embalagem desaparece na mancha de luz")
        if qualidade.nitidez < auto.nitidez_minima:
            return False, f"imagem fora de foco (nitidez {qualidade.nitidez:.2f})"
        if patamar < auto.patamar_minimo:
            return False, (f"contagem instavel — muda dentro de {patamar} unidades "
                           "de limiar; recalibrar ou melhorar a luz")
        if itens and cobertura < auto.cobertura_minima:
            return False, (f"so {cobertura * 100:.0f}% da area detectada virou item — "
                           "ha algo dentro da caixa que o detector nao explicou "
                           "(embalagens fundidas pelo brilho ou objeto estranho)")
        return True, "ok"
