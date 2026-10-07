"""Contagem e nivel de estoque por dispenser.

O metodo nao tenta "detectar caixas", que e fragil quando elas estao encostadas
umas nas outras e sem separacao visivel. Em vez disso mede a ALTURA DA COLUNA
ocupada e divide pela altura de uma caixa:

    n_caixas = altura_ocupada_px / altura_da_caixa_px

Isso e mais robusto porque:
  - nao depende de enxergar a divisa entre caixas (que pode nem existir);
  - degrada com elegancia: se a estimativa erra, erra em 1 caixa, nao vira lixo;
  - a mesma calibracao serve para qualquer produto, bastando a altura da caixa.

A altura da caixa e aprendida sozinha: no momento em que a pilha esta cheia com
uma quantidade conhecida, o sistema divide e guarda. Depois disso e automatico.

Para achar o topo da pilha, compara-se cada linha da coluna com a imagem da
prateleira VAZIA. A linha onde a diferenca passa a ser consistente e o topo.
Usar o vazio como referencia mata o problema de fundo texturizado.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

RAIZ = Path(__file__).resolve().parent.parent
ARQ_PILHAS = RAIZ / "config" / "pilhas.json"
DIR_VAZIOS = RAIZ / "config" / "vazios"


@dataclass
class CalibracaoPilha:
    """O que o sistema precisa saber para contar uma pilha."""

    dispenser: int
    altura_caixa_px: float = 0.0        # 0 = ainda nao aprendida
    capacidade: int = 0                 # quantas caixas cabem (0 = desconhecida)
    limiar_reposicao: int = 2           # abaixo disso, avisa
    limiar_diferenca: float = 18.0      # sensibilidade contra a imagem vazia

    @property
    def calibrada(self) -> bool:
        return self.altura_caixa_px > 1.0

    def para_dict(self) -> dict:
        return {
            "dispenser": self.dispenser,
            "altura_caixa_px": round(self.altura_caixa_px, 2),
            "capacidade": self.capacidade,
            "limiar_reposicao": self.limiar_reposicao,
            "limiar_diferenca": self.limiar_diferenca,
        }


@dataclass
class NivelEstoque:
    dispenser: int
    caixas: int | None                  # None = nao calibrado
    fracao: float                       # 0..1 da altura util ocupada
    altura_px: float
    confianca: float
    precisa_repor: bool = False
    vazio: bool = False

    def mensagem(self) -> str:
        if self.caixas is None:
            return f"Dispenser {self.dispenser}: {self.fracao * 100:.0f}% cheio (sem calibracao de contagem)"
        if self.vazio:
            return f"Dispenser {self.dispenser}: VAZIO"
        aviso = "  <- repor" if self.precisa_repor else ""
        return f"Dispenser {self.dispenser}: ~{self.caixas} caixa(s), {self.fracao * 100:.0f}% cheio{aviso}"

    def para_dict(self) -> dict:
        return {
            "dispenser": self.dispenser,
            "caixas": self.caixas,
            "fracao": round(self.fracao, 3),
            "altura_px": round(self.altura_px, 1),
            "confianca": round(self.confianca, 3),
            "precisa_repor": self.precisa_repor,
            "vazio": self.vazio,
        }


# --------------------------------------------------------------------------- #
class ContadorDeEstoque:
    """Estima o nivel de cada pilha comparando com a imagem da prateleira vazia."""

    def __init__(self, suavizacao: float = 0.35) -> None:
        self.pilhas: dict[int, CalibracaoPilha] = {}
        self.vazios: dict[int, np.ndarray] = {}
        self.suavizacao = float(suavizacao)   # media movel: nivel nao pode tremer
        self._ultimo: dict[int, float] = {}

    # ------------------------------------------------------------------ #
    def registrar_vazio(self, frame: np.ndarray, zona) -> None:
        """Guarda como a zona se parece sem nenhuma caixa."""
        recorte = self._recortar(frame, zona)
        if recorte is not None:
            self.vazios[zona.dispenser] = recorte
            self.pilhas.setdefault(zona.dispenser, CalibracaoPilha(zona.dispenser))

    def aprender_altura(self, frame: np.ndarray, zona, quantidade: int) -> float:
        """Com a pilha contendo `quantidade` caixas, deduz a altura de uma caixa."""
        if quantidade < 1:
            return 0.0
        perfil = self._perfil_ocupacao(frame, zona)
        if perfil is None:
            return 0.0
        altura = self._altura_ocupada(perfil, zona)
        calib = self.pilhas.setdefault(zona.dispenser, CalibracaoPilha(zona.dispenser))
        calib.altura_caixa_px = altura / quantidade
        if calib.altura_caixa_px > 1:
            calib.capacidade = int(zona.altura / calib.altura_caixa_px)
        return calib.altura_caixa_px

    # ------------------------------------------------------------------ #
    def medir(self, frame: np.ndarray, zona) -> NivelEstoque:
        calib = self.pilhas.setdefault(zona.dispenser, CalibracaoPilha(zona.dispenser))
        perfil = self._perfil_ocupacao(frame, zona)
        if perfil is None:
            return NivelEstoque(zona.dispenser, None, 0.0, 0.0, 0.0, vazio=True)

        altura = self._altura_ocupada(perfil, zona)
        tem_referencia = zona.dispenser in self.vazios

        # media movel: a leitura de nivel nao pode oscilar a cada frame, senao
        # o alerta de reposicao fica piscando
        anterior = self._ultimo.get(zona.dispenser)
        if anterior is not None:
            altura = self.suavizacao * altura + (1 - self.suavizacao) * anterior
        self._ultimo[zona.dispenser] = altura

        fracao = float(np.clip(altura / max(1.0, zona.altura), 0.0, 1.0))
        confianca = self._confianca(perfil)
        if not tem_referencia:
            # Sem foto da prateleira vazia o metodo cai para densidade de borda,
            # que confunde textura da caixa com ocupacao. Medido no simulador:
            # com referencia o erro e 0 caixa; sem ela, chega a 3. Entao a
            # leitura ate sai, mas marcada como pouco confiavel.
            confianca = min(confianca, 0.35)

        if not calib.calibrada:
            return NivelEstoque(zona.dispenser, None, fracao, altura, confianca,
                                vazio=fracao < 0.03)

        caixas = int(round(altura / calib.altura_caixa_px))
        caixas = max(0, caixas)
        return NivelEstoque(
            dispenser=zona.dispenser,
            caixas=caixas,
            fracao=fracao,
            altura_px=altura,
            confianca=confianca,
            precisa_repor=caixas <= calib.limiar_reposicao,
            vazio=caixas == 0,
        )

    # ------------------------------------------------------------------ #
    def _recortar(self, frame: np.ndarray, zona) -> np.ndarray | None:
        h, w = frame.shape[:2]
        x1, y1 = max(0, zona.x), max(0, zona.y)
        x2, y2 = min(w, zona.x2), min(h, zona.y2)
        if x2 - x1 < 10 or y2 - y1 < 20:
            return None
        cinza = frame[y1:y2, x1:x2]
        if cinza.ndim == 3:
            cinza = cv2.cvtColor(cinza, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(cinza, (5, 5), 0)

    def _perfil_ocupacao(self, frame: np.ndarray, zona) -> np.ndarray | None:
        """Para cada linha da zona, o quanto ela difere da prateleira vazia."""
        atual = self._recortar(frame, zona)
        if atual is None:
            return None

        vazio = self.vazios.get(zona.dispenser)
        if vazio is not None and vazio.shape == atual.shape:
            diferenca = cv2.absdiff(atual, vazio)
        else:
            # Sem referencia de vazio: usa a densidade de bordas. Caixa tem
            # textura (texto, arte); fundo liso do dispenser nao tem.
            gx = cv2.Sobel(atual, cv2.CV_32F, 1, 0, ksize=3)
            gy = cv2.Sobel(atual, cv2.CV_32F, 0, 1, ksize=3)
            diferenca = np.clip(cv2.magnitude(gx, gy) / 4.0, 0, 255).astype(np.uint8)

        # mediana por linha resiste a reflexo pontual e a um dedo na frente
        return np.median(diferenca, axis=1).astype(np.float32)

    def _altura_ocupada(self, perfil: np.ndarray, zona) -> float:
        """Altura, em pixels, da parte de baixo da coluna que esta ocupada.

        Percorre de baixo para cima: a pilha cresce do fundo. O topo e a ultima
        linha antes de uma sequencia de linhas 'vazias' — exigir a sequencia
        evita que uma etiqueta clara no meio da caixa corte a contagem.
        """
        calib = self.pilhas.get(zona.dispenser)
        limiar = calib.limiar_diferenca if calib else 18.0

        suave = cv2.GaussianBlur(perfil.reshape(-1, 1), (1, 9), 0).ravel()
        ocupado = suave > limiar

        consecutivas_vazias = 0
        exigidas = max(3, int(len(suave) * 0.04))
        topo = len(suave)

        for i in range(len(suave) - 1, -1, -1):
            if ocupado[i]:
                consecutivas_vazias = 0
                topo = i
            else:
                consecutivas_vazias += 1
                if consecutivas_vazias >= exigidas and topo < len(suave):
                    break

        if topo >= len(suave):
            return 0.0
        return float(len(suave) - topo)

    @staticmethod
    def _confianca(perfil: np.ndarray) -> float:
        """Alta quando ha contraste claro entre ocupado e vazio."""
        if perfil.size < 8:
            return 0.0
        alto = float(np.percentile(perfil, 85))
        baixo = float(np.percentile(perfil, 15))
        return float(np.clip((alto - baixo) / 60.0, 0.0, 1.0))

    # ------------------------------------------------------------------ #
    def salvar(self, caminho: Path | str = ARQ_PILHAS) -> None:
        caminho = Path(caminho)
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(
            json.dumps(
                {
                    "_comentario": "Calibracao de contagem por dispenser. "
                                   "Gerada por src/calibrar_estoque.py",
                    "pilhas": [p.para_dict() for p in self.pilhas.values()],
                },
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        DIR_VAZIOS.mkdir(parents=True, exist_ok=True)
        for dispenser, img in self.vazios.items():
            cv2.imwrite(str(DIR_VAZIOS / f"vazio_{dispenser}.png"), img)

    def carregar(self, caminho: Path | str = ARQ_PILHAS) -> int:
        caminho = Path(caminho)
        if not caminho.exists():
            return 0
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        for item in dados.get("pilhas", []):
            self.pilhas[int(item["dispenser"])] = CalibracaoPilha(
                dispenser=int(item["dispenser"]),
                altura_caixa_px=float(item.get("altura_caixa_px", 0.0)),
                capacidade=int(item.get("capacidade", 0)),
                limiar_reposicao=int(item.get("limiar_reposicao", 2)),
                limiar_diferenca=float(item.get("limiar_diferenca", 18.0)),
            )
        for arq in DIR_VAZIOS.glob("vazio_*.png"):
            try:
                numero = int(arq.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            img = cv2.imread(str(arq), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                self.vazios[numero] = img
        return len(self.pilhas)
