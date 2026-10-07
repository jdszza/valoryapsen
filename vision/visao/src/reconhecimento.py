"""Reconhecimento do medicamento pela propria embalagem, sem depender de codigo.

Por que casamento de caracteristicas e nao uma rede treinada:

  - Cadastrar um SKU novo custa 3 a 5 fotos e 2 segundos. Nao ha treino, nao ha
    GPU, nao ha ciclo de retreino a cada lancamento. Numa farmaceutica isso
    importa: o catalogo muda, e a embalagem tambem muda (troca de arte, novo
    dosador) sem aviso.
  - Roda em CPU comum dentro do orcamento de tempo do loop de video.
  - E auditavel: da para mostrar EXATAMENTE quais pontos casaram entre a foto
    de referencia e a imagem ao vivo. Um classificador neural responde
    "97% Dipirona" e ninguem sabe por que. Em ambiente regulado, poder mostrar
    a evidencia vale mais que alguns pontos de acuracia.

Como funciona:
  1. cadastro     -> extrai pontos-chave (ORB/SIFT) de cada foto de referencia
  2. identificacao-> casa os pontos do recorte ao vivo contra cada SKU
  3. verificacao  -> exige que os pontos casados sejam geometricamente
                     coerentes (uma homografia unica explica todos eles).
                     Sem isso, textura parecida vira falso positivo.
  4. rejeicao     -> se o melhor SKU nao for suficientemente melhor que o
                     segundo, responde DESCONHECIDO em vez de chutar.

O passo 4 e o que torna o modulo utilizavel em producao: reconhecedor que
sempre responde alguma coisa e um gerador de erro silencioso.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

RAIZ = Path(__file__).resolve().parent.parent
DIR_REFERENCIAS = RAIZ / "referencias"

ALTURA_MINIATURA = 120  # miniatura de conferencia: barata de guardar e comparar


# --------------------------------------------------------------------------- #
@dataclass
class Amostra:
    """Uma foto de referencia ja processada."""

    pontos: np.ndarray          # (N, 2) coordenadas dos pontos-chave
    descritores: np.ndarray     # (N, D) assinatura de cada ponto
    histograma: np.ndarray      # assinatura de cor, para um pre-filtro barato
    tamanho: tuple[int, int]    # (largura, altura) da foto de referencia
    miniatura: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), np.uint8))
    # miniatura em tons de cinza: usada na conferencia final pixel a pixel


@dataclass
class Referencia:
    """Todas as amostras de um medicamento."""

    sku: str
    nome: str
    amostras: list[Amostra] = field(default_factory=list)

    @property
    def total_pontos(self) -> int:
        return sum(len(a.pontos) for a in self.amostras)


@dataclass
class Identificacao:
    """Resposta do reconhecedor para um recorte."""

    sku: str | None
    nome: str
    inliers: int                 # pontos casados geometricamente coerentes
    score: float                 # 0..1, quao bem a melhor referencia explicou
    margem: float                # quanto o 1o lugar superou o 2o
    aceito: bool
    correlacao: float = 0.0      # conferencia final: o quanto os pixels batem
    nivel: str = "desconhecido"  # confirmado | indicio | desconhecido
    motivo: str = ""
    segundo_sku: str | None = None
    homografia: np.ndarray | None = None

    @property
    def desconhecido(self) -> bool:
        return not self.aceito

    def para_dict(self) -> dict:
        return {
            "sku": self.sku if self.aceito else None,
            "nome": self.nome,
            "inliers": self.inliers,
            "score": round(self.score, 3),
            "margem": round(self.margem, 3),
            "correlacao": round(self.correlacao, 3),
            "aceito": self.aceito,
            "nivel": self.nivel,
            "motivo": self.motivo,
            "segundo_sku": self.segundo_sku,
        }


# --------------------------------------------------------------------------- #
class ReconhecedorVisual:
    """Identifica a embalagem por casamento de caracteristicas verificado."""

    def __init__(
        self,
        metodo: str = "orb",
        max_pontos: int = 900,
        razao_lowe: float = 0.75,
        min_inliers: int = 14,
        min_score: float = 0.06,          # abaixo disso: nao reconhecido
        limiar_confirmado: float = 0.15,  # acima disso: reconhecimento firme
        min_margem: float = 1.6,
        vizinhos: int = 8,
        min_correlacao: float = 0.30,
        tamanho_normalizado: int = 340,
        escalas_cadastro: tuple = (1.0, 0.6, 0.4),
        escalas_consulta: tuple = (1.0, 0.7, 1.45),
        usar_histograma: bool = True,
        limiar_histograma: float = 0.25,
    ) -> None:
        self.metodo = metodo.lower()
        self.max_pontos = max_pontos
        self.razao_lowe = razao_lowe
        self.min_inliers = min_inliers
        self.min_score = min_score
        self.limiar_confirmado = float(limiar_confirmado)
        self.min_margem = min_margem
        self.vizinhos = int(vizinhos)
        self.min_correlacao = float(min_correlacao)
        self.tamanho_normalizado = int(tamanho_normalizado)
        self.escalas_cadastro = tuple(escalas_cadastro)
        self.escalas_consulta = tuple(escalas_consulta)
        self.usar_histograma = usar_histograma
        self.limiar_histograma = limiar_histograma

        self._extrator = self._criar_extrator()
        self._matcher = self._criar_matcher()
        self.referencias: dict[str, Referencia] = {}
        self._cache_indice = None

    # ------------------------------------------------------------------ #
    def _criar_extrator(self):
        if self.metodo == "sift" and hasattr(cv2, "SIFT_create"):
            return cv2.SIFT_create(nfeatures=self.max_pontos)
        if self.metodo == "akaze" and hasattr(cv2, "AKAZE_create"):
            return cv2.AKAZE_create()
        self.metodo = "orb"
        return cv2.ORB_create(
            nfeatures=self.max_pontos,
            scaleFactor=1.2,
            nlevels=8,
            edgeThreshold=15,
            fastThreshold=12,
        )

    def _criar_matcher(self):
        binario = self.metodo in ("orb", "akaze")
        norma = cv2.NORM_HAMMING if binario else cv2.NORM_L2
        return cv2.BFMatcher(norma, crossCheck=False)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _histograma(img: np.ndarray) -> np.ndarray:
        """Assinatura de cor (matiz x saturacao). Pre-filtro barato: caixa azul
        nao precisa nem ser comparada ponto a ponto com caixa vermelha."""
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [24, 8], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
        return hist.flatten().astype(np.float32)

    def _extrair(self, img: np.ndarray, tamanho: int | None = None) -> Amostra | None:
        # Normaliza pela RAIZ DA AREA, nao pela altura. Normalizar por altura
        # parece equivalente, mas quebra com a caixa girada 90 graus: a imagem
        # vira retrato, a altura passa a ser o lado maior e o conteudo acaba
        # numa escala diferente da que foi cadastrada. Area e invariante a
        # rotacao, entao caixa em pe e caixa deitada chegam do mesmo tamanho.
        img = _normalizar_area(img, tamanho or self.tamanho_normalizado)

        cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        cinza = cv2.createCLAHE(2.0, (8, 8)).apply(cinza)
        pontos, desc = self._extrator.detectAndCompute(cinza, None)
        if desc is None or len(pontos) < 8:
            return None
        escala = ALTURA_MINIATURA / max(1, cinza.shape[0])
        miniatura = cv2.resize(
            cinza, (max(8, int(cinza.shape[1] * escala)), ALTURA_MINIATURA),
            interpolation=cv2.INTER_AREA,
        )
        return Amostra(
            pontos=np.float32([p.pt for p in pontos]),
            descritores=desc,
            histograma=self._histograma(img),
            tamanho=(img.shape[1], img.shape[0]),
            miniatura=miniatura,
        )

    # ------------------------------------------------------------------ #
    def cadastrar(self, sku: str, nome: str, imagens: list[np.ndarray]) -> int:
        """Cadastra (ou reforca) um medicamento a partir de fotos. Retorna quantas
        fotos foram aproveitadas."""
        ref = self.referencias.setdefault(sku, Referencia(sku=sku, nome=nome))
        ref.nome = nome
        aproveitadas = 0
        for img in imagens:
            usou = False
            # Cada foto entra em varias escalas. Motivo: uma caixa vista de
            # longe nao e so "menor" — ela chega reamostrada, com bordas mais
            # moles, e os descritores mudam o suficiente para o casamento
            # falhar mesmo com a escala ja normalizada. Guardar a referencia
            # tambem na versao reduzida faz a consulta distante encontrar um
            # par com a mesma assinatura de borragem.
            for escala in self.escalas_cadastro:
                fonte = img
                if escala != 1.0:
                    fonte = cv2.resize(img, None, fx=escala, fy=escala,
                                       interpolation=cv2.INTER_AREA)
                amostra = self._extrair(fonte)
                if amostra is not None:
                    ref.amostras.append(amostra)
                    usou = True
            aproveitadas += 1 if usou else 0
        if not ref.amostras:
            self.referencias.pop(sku, None)
        self._cache_indice = None
        return aproveitadas

    def remover(self, sku: str) -> None:
        self.referencias.pop(sku, None)
        self._cache_indice = None

    # ------------------------------------------------------------------ #
    def identificar(self, recorte: np.ndarray) -> Identificacao:
        """Diz qual medicamento e — ou assume que nao sabe."""
        def vazia(nome: str, motivo: str) -> Identificacao:
            return Identificacao(None, nome, 0, 0.0, 0.0, False, 0.0,
                                 "desconhecido", motivo)

        if not self.referencias:
            return vazia("SEM CADASTRO", "nenhum medicamento cadastrado")

        # Tenta algumas escalas de trabalho e fica com a melhor. Isso remove
        # duas dependencias incomodas de uma vez: a distancia da caixa ate a
        # camera e quanta sobra de fundo veio dentro do recorte da zona. Sem
        # isso, mudar a zona alguns pixels ja alterava a escala normalizada o
        # bastante para o casamento falhar.
        melhor = None
        for fator in self.escalas_consulta:
            consulta = self._extrair(recorte, int(self.tamanho_normalizado * fator))
            if consulta is None:
                continue
            pontuacoes = self._pontuar_discriminativo(consulta, recorte, fator)
            if pontuacoes and (melhor is None or pontuacoes[0][0] > melhor[0][0]):
                melhor = pontuacoes
                # para na primeira escala que ja produz um resultado utilizavel;
                # as outras so existem para o caso de a primeira nao servir
                if (pontuacoes[0][0] >= self.min_score
                        and pontuacoes[0][1] >= self.min_inliers):
                    break

        if melhor is None:
            return vazia("DESCONHECIDO",
                         "nenhum ponto exclusivo de algum medicamento cadastrado")
        pontuacoes = melhor

        score, inliers, sku, H, correlacao = pontuacoes[0]
        score2 = pontuacoes[1][0] if len(pontuacoes) > 1 else 0.0
        sku2 = pontuacoes[1][2] if len(pontuacoes) > 1 else None
        margem = score / score2 if score2 > 1e-6 else float("inf")

        nome = self.referencias[sku].nome

        def resposta(nome_exibido: str, aceito: bool, nivel: str, motivo: str):
            return Identificacao(sku, nome_exibido, inliers, score, margem, aceito,
                                 correlacao, nivel, motivo, sku2, H)

        if inliers < self.min_inliers:
            return resposta("DESCONHECIDO", False, "desconhecido",
                            f"poucos pontos coerentes ({inliers} < {self.min_inliers})")
        if score < self.min_score:
            return resposta("DESCONHECIDO", False, "desconhecido",
                            f"semelhanca fraca ({score:.3f} < {self.min_score:.3f})")
        if margem < self.min_margem:
            return resposta("AMBIGUO", False, "desconhecido",
                            f"parecido demais com {sku2} (margem {margem:.2f})")

        # Tres niveis em vez de sim/nao. O caso intermediario existe porque a
        # aparencia raramente e prova sozinha: como 'indicio' ela reforca ou
        # contesta o codigo lido, sem precisar decidir por conta propria.
        if score >= self.limiar_confirmado:
            return resposta(nome, True, "confirmado", "reconhecimento firme")
        return resposta(nome, False, "indicio",
                        f"semelhanca moderada ({score:.3f}); serve como indicio")

    # ------------------------------------------------------------------ #
    def _indice_global(self):
        """Indice unico com os descritores de TODOS os medicamentos.

        Comparar a consulta contra um SKU de cada vez tem um defeito grave neste
        dominio: caixas da mesma fabrica compartilham marca, fonte, tarja e
        layout. Esses pontos casam bem com qualquer produto e inflam a
        semelhanca de todo mundo — foi o que gerou 1 falso positivo a cada 4
        embalagens nao cadastradas na primeira versao.

        Com o indice global da para exigir que o ponto seja EXCLUSIVO: ele so
        conta se estiver claramente mais perto do medicamento vencedor do que de
        qualquer outro. A tarja "APSEN" casa igual com todos, entao e descartada
        sozinha, e sobra so o que realmente distingue um produto do outro.
        """
        if getattr(self, "_cache_indice", None) is not None:
            return self._cache_indice

        blocos, origens = [], []
        for sku, ref in self.referencias.items():
            for i_amostra, amostra in enumerate(ref.amostras):
                blocos.append(amostra.descritores)
                origens += [(sku, i_amostra, i) for i in range(len(amostra.pontos))]

        if not blocos:
            self._cache_indice = (None, [])
            return self._cache_indice

        self._cache_indice = (np.vstack(blocos), origens)
        return self._cache_indice

    def _pontuar_discriminativo(self, consulta: Amostra, imagem: np.ndarray,
                                fator: float = 1.0):
        """Pontua cada medicamento usando so os pontos exclusivos dele."""
        descritores, origens = self._indice_global()
        if descritores is None or len(consulta.descritores) < 8:
            return []

        vizinhos = min(self.vizinhos, len(descritores))
        try:
            pares = self._matcher.knnMatch(consulta.descritores, descritores, k=vizinhos)
        except cv2.error:
            return []

        # candidatos por (sku, amostra): so pontos que passam no teste da razao
        # de Lowe CONTRA OUTRO MEDICAMENTO
        candidatos: dict[tuple[str, int], list[tuple[int, int]]] = {}
        for lista in pares:
            if not lista:
                continue
            melhor = lista[0]
            sku_melhor, i_amostra, i_ponto = origens[melhor.trainIdx]

            rival = next(
                (m for m in lista[1:] if origens[m.trainIdx][0] != sku_melhor), None
            )
            if rival is None:
                continue  # todos os vizinhos sao do mesmo SKU: sem discriminacao
            if melhor.distance >= self.razao_lowe * rival.distance:
                continue  # ponto generico (marca, fonte, tarja): nao distingue nada

            candidatos.setdefault((sku_melhor, i_amostra), []).append(
                (melhor.queryIdx, i_ponto)
            )

        pontuacoes: list[tuple[float, int, str, np.ndarray | None]] = []
        melhor_por_sku: dict[str, tuple[float, int, np.ndarray | None]] = {}

        for (sku, i_amostra), casados in candidatos.items():
            if len(casados) < 6:
                continue
            amostra = self.referencias[sku].amostras[i_amostra]
            origem = np.float32([consulta.pontos[q] for q, _ in casados]).reshape(-1, 1, 2)
            destino = np.float32([amostra.pontos[t] for _, t in casados]).reshape(-1, 1, 2)

            H, mascara = cv2.findHomography(origem, destino, cv2.RANSAC, 5.0, maxIters=2000)
            if H is None or mascara is None or not _homografia_plausivel(H):
                continue

            inliers = int(mascara.sum())

            # Conferencia final: alinhar a imagem ao vivo com a foto de
            # referencia usando a homografia e comparar os pixels. Geometria
            # coerente sozinha ainda deixa passar coincidencia; exigir que a
            # imagem realmente se pareca com a referencia depois de alinhada e
            # o que derruba o falso positivo entre caixas da mesma linha.
            correlacao = self._conferir_aparencia(imagem, amostra, H, fator)
            if correlacao < self.min_correlacao:
                continue

            score = inliers / max(25.0, min(len(amostra.pontos), len(consulta.pontos)))
            score = float(min(1.0, score)) * (0.5 + 0.5 * correlacao)

            atual = melhor_por_sku.get(sku)
            if atual is None or score > atual[0]:
                melhor_por_sku[sku] = (score, inliers, H, correlacao)

        for sku, (score, inliers, H, correlacao) in melhor_por_sku.items():
            if self.usar_histograma:
                similar = max(
                    cv2.compareHist(consulta.histograma, a.histograma, cv2.HISTCMP_CORREL)
                    for a in self.referencias[sku].amostras
                )
                if similar < self.limiar_histograma:
                    continue  # geometria bate mas a cor nao: desconfia
            pontuacoes.append((score, inliers, sku, H, correlacao))

        pontuacoes.sort(key=lambda p: -p[0])
        return pontuacoes

    # ------------------------------------------------------------------ #
    def _conferir_aparencia(self, imagem: np.ndarray, amostra: Amostra, H,
                            fator: float = 1.0) -> float:
        """Alinha a imagem ao vivo com a referencia e mede o quanto batem.

        Compara o gradiente (bordas), nao o brilho: assim luz mais fraca ou mais
        forte nao derruba a conferencia, mas arte diferente derruba.
        """
        alvo = amostra.miniatura
        if alvo is None or alvo.size < 64:
            return 1.0  # referencia antiga sem miniatura: nao bloqueia

        # A homografia foi calculada no espaco NORMALIZADO (tanto a consulta
        # quanto a referencia passam por _normalizar_area antes da extracao).
        # Comparar aqui a imagem crua so funcionaria por coincidencia, quando o
        # recorte ja chegasse no tamanho normalizado — e foi exatamente o que
        # fazia o reconhecimento falhar assim que a caixa mudava de distancia.
        imagem = _normalizar_area(imagem, int(self.tamanho_normalizado * fator))

        cinza = cv2.cvtColor(imagem, cv2.COLOR_BGR2GRAY) if imagem.ndim == 3 else imagem
        larg_ref, alt_ref = amostra.tamanho
        if alt_ref < 2:
            return 1.0

        escala = alvo.shape[0] / float(alt_ref)
        S = np.array([[escala, 0, 0], [0, escala, 0], [0, 0, 1]], dtype=np.float64)
        try:
            alinhada = cv2.warpPerspective(
                cinza, S @ H, (alvo.shape[1], alvo.shape[0]), flags=cv2.INTER_AREA
            )
        except cv2.error:
            return 0.0

        valido = alinhada > 0
        if valido.mean() < 0.35:      # sobreposicao pequena demais para julgar
            return 0.0

        a = _bordas(alinhada)[valido]
        b = _bordas(alvo)[valido]
        if a.size < 200 or a.std() < 1e-6 or b.std() < 1e-6:
            return 0.0
        return float(np.clip(np.corrcoef(a, b)[0, 1], 0.0, 1.0))

    # ------------------------------------------------------------------ #
    def salvar(self, pasta: Path | str = DIR_REFERENCIAS) -> Path:
        pasta = Path(pasta)
        pasta.mkdir(parents=True, exist_ok=True)

        indice = {"metodo": self.metodo, "medicamentos": []}
        dados: dict[str, np.ndarray] = {}
        for sku, ref in self.referencias.items():
            indice["medicamentos"].append(
                {"sku": sku, "nome": ref.nome, "amostras": len(ref.amostras)}
            )
            for i, a in enumerate(ref.amostras):
                dados[f"{sku}|{i}|pontos"] = a.pontos
                dados[f"{sku}|{i}|desc"] = a.descritores
                dados[f"{sku}|{i}|hist"] = a.histograma
                dados[f"{sku}|{i}|tam"] = np.array(a.tamanho)
                dados[f"{sku}|{i}|mini"] = a.miniatura

        np.savez_compressed(pasta / "referencias.npz", **dados)
        (pasta / "referencias.json").write_text(
            json.dumps(indice, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return pasta

    def carregar(self, pasta: Path | str = DIR_REFERENCIAS) -> int:
        pasta = Path(pasta)
        arq_json = pasta / "referencias.json"
        arq_npz = pasta / "referencias.npz"
        if not arq_json.exists() or not arq_npz.exists():
            return 0

        indice = json.loads(arq_json.read_text(encoding="utf-8"))
        if indice.get("metodo") and indice["metodo"] != self.metodo:
            self.metodo = indice["metodo"]
            self._extrator = self._criar_extrator()
            self._matcher = self._criar_matcher()

        dados = np.load(arq_npz)
        self.referencias.clear()
        for item in indice.get("medicamentos", []):
            sku, nome = item["sku"], item["nome"]
            ref = Referencia(sku=sku, nome=nome)
            for i in range(int(item["amostras"])):
                chave = f"{sku}|{i}|"
                if chave + "desc" not in dados:
                    continue
                ref.amostras.append(
                    Amostra(
                        pontos=dados[chave + "pontos"],
                        descritores=dados[chave + "desc"],
                        histograma=dados[chave + "hist"],
                        tamanho=tuple(int(v) for v in dados[chave + "tam"]),
                        miniatura=dados[chave + "mini"] if chave + "mini" in dados
                        else np.zeros((1, 1), np.uint8),
                    )
                )
            if ref.amostras:
                self.referencias[sku] = ref
        self._cache_indice = None
        return len(self.referencias)


# --------------------------------------------------------------------------- #
def _normalizar_area(img: np.ndarray, alvo: int) -> np.ndarray:
    """Leva a imagem a uma area padrao (raiz da area = `alvo`), sem distorcer.

    Invariante a rotacao: girar a caixa 90 graus nao muda a area, entao a
    escala de trabalho continua a mesma.
    """
    if alvo <= 0:
        return img
    h, w = img.shape[:2]
    atual = (h * w) ** 0.5
    if atual < 1:
        return img
    fator = alvo / atual
    if abs(fator - 1.0) < 0.02:
        return img
    novo = (max(8, int(round(w * fator))), max(8, int(round(h * fator))))
    interp = cv2.INTER_AREA if fator < 1 else cv2.INTER_CUBIC
    return cv2.resize(img, novo, interpolation=interp)


def _bordas(g: np.ndarray) -> np.ndarray:
    """Magnitude do gradiente, suavizada — insensivel a nivel de luz."""
    g = cv2.GaussianBlur(g, (3, 3), 0)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def _homografia_plausivel(H: np.ndarray) -> bool:
    """Rejeita transformacoes fisicamente impossiveis para uma caixa plana.

    Sem esta trava, o RANSAC as vezes encontra uma homografia degenerada que
    'explica' pontos espalhados esmagando tudo numa linha — e um falso positivo
    com aparencia de evidencia solida.
    """
    if H is None or not np.all(np.isfinite(H)):
        return False
    det = float(np.linalg.det(H[:2, :2]))
    if abs(det) < 1e-3 or abs(det) > 40.0:
        return False
    # razao de aspecto entre os eixos transformados
    ex = np.linalg.norm(H[:2, 0])
    ey = np.linalg.norm(H[:2, 1])
    if ex < 1e-3 or ey < 1e-3:
        return False
    razao = max(ex / ey, ey / ex)
    return razao < 6.0
