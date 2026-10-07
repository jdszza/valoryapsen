"""Carregamento e validacao dos arquivos de configuracao."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parent.parent
DIR_CONFIG = RAIZ / "config"

ARQ_MEDICAMENTOS = DIR_CONFIG / "medicamentos.json"
ARQ_ZONAS = DIR_CONFIG / "zonas.json"
ARQ_PARAMETROS = DIR_CONFIG / "parametros.json"


# --------------------------------------------------------------------------- #
# Medicamentos
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Medicamento:
    qr: str
    nome: str
    dispenser: int
    cor: tuple[int, int, int] = (200, 200, 200)
    aruco: int | None = None  # id do marcador ArUco redundante na mesma etiqueta

    @property
    def chave_aruco(self) -> str | None:
        return f"ARUCO:{self.aruco}" if self.aruco is not None else None


@dataclass
class Catalogo:
    """Mapa conteudo-do-QR -> medicamento esperado."""

    por_qr: dict[str, Medicamento] = field(default_factory=dict)

    @classmethod
    def carregar(cls, caminho: Path | str = ARQ_MEDICAMENTOS) -> "Catalogo":
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
        itens = dados.get("medicamentos", [])
        if not itens:
            raise ValueError(f"Nenhum medicamento definido em {caminho}")
        return cls.de_itens(itens)

    @classmethod
    def de_itens(cls, itens: list[dict]) -> "Catalogo":
        """Monta o catalogo a partir de uma lista de dicionarios.

        Separado de `carregar` para que a mesma validacao valha tanto para o
        arquivo local quanto para o catalogo que vem do backend Apsen. Regra
        que nao pode ser relaxada em nenhum dos dois caminhos: um dispenser
        guarda um unico tipo de medicamento, e cada codigo aponta para um so.
        """
        if not itens:
            raise ValueError("Nenhum medicamento no catalogo")

        por_qr: dict[str, Medicamento] = {}
        vistos_por_dispenser: dict[int, str] = {}

        for item in itens:
            qr = str(item["qr"]).strip()
            if not qr:
                raise ValueError("Medicamento com campo 'qr' vazio.")
            if qr in por_qr:
                raise ValueError(f"QR duplicado no catalogo: {qr!r}")

            dispenser = int(item["dispenser"])
            if dispenser < 1:
                raise ValueError(f"Dispenser invalido para {qr!r}: {dispenser}")

            # Regra do projeto: um dispenser guarda um unico tipo de medicamento.
            if dispenser in vistos_por_dispenser:
                raise ValueError(
                    f"Dispenser {dispenser} foi atribuido a mais de um medicamento "
                    f"({vistos_por_dispenser[dispenser]!r} e {qr!r}). "
                    "Cada dispenser deve conter apenas um tipo."
                )
            vistos_por_dispenser[dispenser] = qr

            cor = tuple(int(c) for c in item.get("cor", (200, 200, 200)))[:3]
            aruco = item.get("aruco")
            aruco = int(aruco) if aruco is not None else None

            med = Medicamento(
                qr=qr,
                nome=str(item.get("nome", qr)),
                dispenser=dispenser,
                cor=cor,  # type: ignore[arg-type]
                aruco=aruco,
            )
            por_qr[qr] = med
            if med.chave_aruco:
                if med.chave_aruco in por_qr:
                    raise ValueError(f"id ArUco duplicado no catalogo: {aruco}")
                por_qr[med.chave_aruco] = med  # mesmo medicamento, outro codigo

        return cls(por_qr=por_qr)

    def buscar(self, conteudo_qr: str) -> Medicamento | None:
        return self.por_qr.get(conteudo_qr.strip())

    def esperado_em(self, dispenser: int) -> Medicamento | None:
        for med in self.medicamentos:
            if med.dispenser == dispenser:
                return med
        return None

    @property
    def medicamentos(self) -> list[Medicamento]:
        """Lista sem duplicar (cada medicamento aparece uma vez, nao uma por codigo)."""
        vistos: dict[str, Medicamento] = {}
        for med in self.por_qr.values():
            vistos.setdefault(med.qr, med)
        return sorted(vistos.values(), key=lambda m: m.dispenser)

    @property
    def dispensers(self) -> list[int]:
        return sorted({m.dispenser for m in self.medicamentos})


# --------------------------------------------------------------------------- #
# Zonas (retangulos calibraveis)
# --------------------------------------------------------------------------- #
@dataclass
class Zona:
    """Retangulo de um dispenser, em pixels do frame de referencia."""

    dispenser: int
    x: int
    y: int
    largura: int
    altura: int
    rotulo: str = ""

    # -- geometria ---------------------------------------------------------- #
    @property
    def x2(self) -> int:
        return self.x + self.largura

    @property
    def y2(self) -> int:
        return self.y + self.altura

    @property
    def centro(self) -> tuple[float, float]:
        return (self.x + self.largura / 2.0, self.y + self.altura / 2.0)

    def contem(self, px: float, py: float, margem: float = 0.0) -> bool:
        return (
            self.x - margem <= px <= self.x2 + margem
            and self.y - margem <= py <= self.y2 + margem
        )

    def normalizar(self) -> None:
        """Garante largura/altura positivas (o usuario pode arrastar ao contrario)."""
        if self.largura < 0:
            self.x += self.largura
            self.largura = -self.largura
        if self.altura < 0:
            self.y += self.altura
            self.altura = -self.altura

    def escalar(self, fx: float, fy: float) -> "Zona":
        return Zona(
            dispenser=self.dispenser,
            x=int(round(self.x * fx)),
            y=int(round(self.y * fy)),
            largura=int(round(self.largura * fx)),
            altura=int(round(self.altura * fy)),
            rotulo=self.rotulo,
        )

    def para_dict(self) -> dict[str, Any]:
        return {
            "dispenser": self.dispenser,
            "x": int(self.x),
            "y": int(self.y),
            "largura": int(self.largura),
            "altura": int(self.altura),
            "rotulo": self.rotulo,
        }


@dataclass
class MapaZonas:
    zonas: list[Zona] = field(default_factory=list)
    resolucao: tuple[int, int] = (1280, 720)  # (largura, altura) da calibracao

    # -- io ----------------------------------------------------------------- #
    @classmethod
    def carregar(cls, caminho: Path | str = ARQ_ZONAS) -> "MapaZonas":
        caminho = Path(caminho)
        if not caminho.exists():
            raise FileNotFoundError(
                f"{caminho} nao existe. Rode primeiro:  python src/calibrar.py"
            )
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        zonas = [
            Zona(
                dispenser=int(z["dispenser"]),
                x=int(z["x"]),
                y=int(z["y"]),
                largura=int(z["largura"]),
                altura=int(z["altura"]),
                rotulo=str(z.get("rotulo", "")),
            )
            for z in dados.get("zonas", [])
        ]
        res = dados.get("resolucao", [1280, 720])
        return cls(zonas=zonas, resolucao=(int(res[0]), int(res[1])))

    def salvar(self, caminho: Path | str = ARQ_ZONAS) -> None:
        caminho = Path(caminho)
        caminho.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_comentario": (
                "Zonas dos dispensers em pixels. Geradas por src/calibrar.py. "
                "Se a resolucao da camera mudar, o sistema reescala automaticamente."
            ),
            "resolucao": [int(self.resolucao[0]), int(self.resolucao[1])],
            "zonas": [z.para_dict() for z in sorted(self.zonas, key=lambda z: z.dispenser)],
        }
        caminho.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # -- consulta ----------------------------------------------------------- #
    def para_resolucao(self, largura: int, altura: int) -> "MapaZonas":
        """Reescala as zonas caso o frame atual tenha resolucao diferente da calibracao."""
        lc, ac = self.resolucao
        if (lc, ac) == (largura, altura) or lc == 0 or ac == 0:
            return self
        fx, fy = largura / lc, altura / ac
        return MapaZonas(
            zonas=[z.escalar(fx, fy) for z in self.zonas],
            resolucao=(largura, altura),
        )

    def zona_do_ponto(self, px: float, py: float, margem: float = 0.0) -> Zona | None:
        """Zona que contem o ponto. Se houver sobreposicao, vence a menor area."""
        candidatas = [z for z in self.zonas if z.contem(px, py, margem)]
        if not candidatas:
            return None
        return min(candidatas, key=lambda z: z.largura * z.altura)

    def por_dispenser(self, numero: int) -> Zona | None:
        for z in self.zonas:
            if z.dispenser == numero:
                return z
        return None

    def validar(self, catalogo: Catalogo | None = None) -> list[str]:
        """Retorna lista de avisos (vazia = tudo certo)."""
        avisos: list[str] = []
        numeros = [z.dispenser for z in self.zonas]
        if len(numeros) != len(set(numeros)):
            avisos.append("Ha mais de uma zona com o mesmo numero de dispenser.")
        for z in self.zonas:
            if z.largura < 20 or z.altura < 20:
                avisos.append(f"Zona do dispenser {z.dispenser} esta muito pequena.")
        # sobreposicao
        for i, a in enumerate(self.zonas):
            for b in self.zonas[i + 1 :]:
                if a.x < b.x2 and b.x < a.x2 and a.y < b.y2 and b.y < a.y2:
                    avisos.append(
                        f"Zonas dos dispensers {a.dispenser} e {b.dispenser} se sobrepoem."
                    )
        if catalogo is not None:
            faltando = set(catalogo.dispensers) - set(numeros)
            if faltando:
                avisos.append(
                    "Sem zona calibrada para o(s) dispenser(s): "
                    + ", ".join(str(n) for n in sorted(faltando))
                )
        return avisos


# --------------------------------------------------------------------------- #
# Parametros
# --------------------------------------------------------------------------- #
def carregar_parametros(caminho: Path | str = ARQ_PARAMETROS) -> dict[str, Any]:
    caminho = Path(caminho)
    if not caminho.exists():
        return {}
    return json.loads(caminho.read_text(encoding="utf-8"))
