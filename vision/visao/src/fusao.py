"""Fusao das duas evidencias independentes: o codigo lido e a aparencia da caixa.

O ganho nao e so redundancia. Sao dois canais que erram de formas diferentes, e
cruzar os dois cria uma capacidade que nenhum deles tem sozinho:

    codigo diz "Dipirona"  +  embalagem parece Paracetamol  =  DIVERGENCIA

Esse caso e invisivel para qualquer sistema que so le codigo. Ele aparece
quando alguem cola a etiqueta errada na caixa, quando o produto foi
reembalado, ou quando o lote e falsificado. Em cadeia farmaceutica isso vale
mais que a deteccao de posicao original — e sai de graca, porque as duas
evidencias ja estao sendo calculadas.

Regra de ouro adotada: divergencia entre canais NUNCA vira silencio. Se os dois
discordam, o sistema para e chama gente, em vez de escolher um deles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from configuracao import Catalogo, Medicamento, Zona
from detector import Estado, Ocorrencia
from reconhecimento import Identificacao


class Veredito(str, Enum):
    OK = "OK"                          # tudo confere
    ERRO_POSICAO = "ERRO_POSICAO"      # medicamento no dispenser errado
    DIVERGENCIA = "DIVERGENCIA"        # codigo e embalagem discordam entre si
    NAO_CADASTRADO = "NAO_CADASTRADO"  # codigo lido nao existe no catalogo
    VAZIO = "VAZIO"                    # nada identificado na zona
    INDETERMINADO = "INDETERMINADO"    # evidencia insuficiente para concluir


CRITICOS = {Veredito.ERRO_POSICAO, Veredito.DIVERGENCIA, Veredito.NAO_CADASTRADO}


@dataclass
class ResultadoFusao:
    dispenser: int
    veredito: Veredito
    confianca: float                    # 0..1
    medicamento: Medicamento | None
    esperado: Medicamento | None
    fonte: str                          # 'codigo+visual' | 'codigo' | 'visual' | '-'
    detalhe: str
    ocorrencia: Ocorrencia | None = None
    identificacao: Identificacao | None = None
    quantidade_certa: int = 0           # unidades do medicamento correto
    quantidade_errada: int = 0          # unidades que nao pertencem a este dispenser
    itens: dict = field(default_factory=dict)  # {nome: quantidade}

    @property
    def total_unidades(self) -> int:
        return self.quantidade_certa + self.quantidade_errada

    @property
    def critico(self) -> bool:
        return self.veredito in CRITICOS

    def resumo_itens(self) -> str:
        """Ex.: '3x Dipirona 500mg, 1x Ibuprofeno 400mg'"""
        return ", ".join(f"{q}x {nome}" for nome, q in
                         sorted(self.itens.items(), key=lambda x: -x[1]))

    def mensagem(self) -> str:
        d = self.dispenser
        if self.veredito is Veredito.OK:
            nome = self.medicamento.nome if self.medicamento else "?"
            if self.quantidade_certa > 1:
                return f"Dispenser {d}: {self.quantidade_certa}x {nome} conferido(s)"
            return f"Dispenser {d}: {nome} conferido"
        if self.veredito is Veredito.ERRO_POSICAO:
            certo = self.esperado.nome if self.esperado else "-"
            achado = self.medicamento.nome if self.medicamento else "-"
            quantos = f"{self.quantidade_errada}x " if self.quantidade_errada > 1 else ""
            extra = (f" (ha tambem {self.quantidade_certa}x '{certo}' corretos)"
                     if self.quantidade_certa else "")
            return (f"Dispenser {d}: {quantos}'{achado}' fora de lugar — este "
                    f"dispenser e de '{certo}'{extra}")
        if self.veredito is Veredito.DIVERGENCIA:
            return f"Dispenser {d}: {self.detalhe}"
        if self.veredito is Veredito.NAO_CADASTRADO:
            return f"Dispenser {d}: {self.detalhe}"
        if self.veredito is Veredito.VAZIO:
            return f"Dispenser {d}: sem medicamento identificado"
        return f"Dispenser {d}: {self.detalhe}"

    def para_dict(self) -> dict:
        return {
            "dispenser": self.dispenser,
            "quantidade_certa": self.quantidade_certa,
            "quantidade_errada": self.quantidade_errada,
            "total_unidades": self.total_unidades,
            "itens": self.itens,
            "veredito": self.veredito.value,
            "confianca": round(self.confianca, 3),
            "medicamento": self.medicamento.nome if self.medicamento else None,
            "sku": self.medicamento.qr if self.medicamento else None,
            "esperado": self.esperado.nome if self.esperado else None,
            "fonte": self.fonte,
            "detalhe": self.detalhe,
            "mensagem": self.mensagem(),
        }


# --------------------------------------------------------------------------- #
class MotorDeFusao:
    """Combina codigo e aparencia em um veredito por dispenser."""

    def __init__(
        self,
        catalogo: Catalogo,
        exigir_visual: bool = False,
        peso_codigo: float = 0.7,
        peso_visual: float = 0.3,
    ) -> None:
        self.catalogo = catalogo
        self.exigir_visual = exigir_visual
        self.peso_codigo = peso_codigo
        self.peso_visual = peso_visual

    # ------------------------------------------------------------------ #
    def avaliar(
        self,
        zona: Zona,
        ocorrencias: Ocorrencia | list | None,
        identificacao: Identificacao | None,
    ) -> ResultadoFusao:
        """Avalia a zona inteira. Aceita uma ocorrencia ou a lista de unidades.

        Com varias caixas no mesmo dispenser, o veredito da zona e o do PIOR
        caso: uma unidade errada no meio de dez certas mantem o dispenser em
        erro. As quantidades de cada lado vao junto, para o operador saber
        quantas precisa tirar.
        """
        lista = ([] if ocorrencias is None else
                 (list(ocorrencias) if isinstance(ocorrencias, (list, tuple))
                  else [ocorrencias]))

        # Cada ocorrencia e UMA unidade, mas todas carregam o total daquele
        # medicamento na zona. Entao consolida por medicamento com max(), nao
        # com soma — somar contaria 3 caixas tres vezes.
        itens: dict[str, int] = {}
        pertence: dict[str, bool] = {}
        for oc in lista:
            nome = oc.nome_medicamento
            itens[nome] = max(itens.get(nome, 0), max(1, oc.quantidade_na_zona))
            pertence[nome] = (oc.medicamento is not None
                              and oc.medicamento.dispenser == zona.dispenser)

        certas = sum(q for nome, q in itens.items() if pertence.get(nome))
        erradas = sum(q for nome, q in itens.items() if not pertence.get(nome))

        # representante: o erro manda no veredito da zona
        ocorrencia = None
        if lista:
            ocorrencia = max(lista, key=lambda o: (o.e_erro, o.quantidade_na_zona))

        resultado = self._avaliar_unidade(zona, ocorrencia, identificacao)
        resultado.quantidade_certa = certas
        resultado.quantidade_errada = erradas
        resultado.itens = itens
        return resultado

    # ------------------------------------------------------------------ #
    def _avaliar_unidade(
        self,
        zona: Zona,
        ocorrencia: Ocorrencia | None,
        identificacao: Identificacao | None,
    ) -> ResultadoFusao:
        dispenser = zona.dispenser
        esperado = self.catalogo.esperado_em(dispenser)

        med_codigo = ocorrencia.medicamento if ocorrencia else None
        codigo_desconhecido = (
            ocorrencia is not None and ocorrencia.estado is Estado.QR_DESCONHECIDO
        )
        med_visual = None
        if identificacao and identificacao.nivel in ("confirmado", "indicio"):
            med_visual = self.catalogo.buscar(identificacao.sku or "")

        def resultado(v, conf, med, fonte, detalhe):
            return ResultadoFusao(dispenser, v, conf, med, esperado, fonte, detalhe,
                                  ocorrencia, identificacao)

        # ---------- nada visto ---------- #
        if med_codigo is None and med_visual is None and not codigo_desconhecido:
            return resultado(Veredito.VAZIO, 1.0 if ocorrencia is None else 0.5,
                             None, "-", "nenhuma evidencia na zona")

        # ---------- codigo lido mas fora do catalogo ---------- #
        if codigo_desconhecido and med_visual is None:
            return resultado(Veredito.NAO_CADASTRADO, 0.9, None, "codigo",
                             f"codigo '{ocorrencia.conteudo}' nao esta no catalogo")

        # ---------- os dois canais responderam ---------- #
        if med_codigo is not None and med_visual is not None:
            if med_codigo.qr != med_visual.qr:
                # O caso que so a fusao enxerga. Confianca alta de que HA um
                # problema, ainda que nao se saiba qual dos dois esta certo.
                conf = 0.75 if identificacao.nivel == "indicio" else 0.95
                return resultado(
                    Veredito.DIVERGENCIA, conf, med_codigo, "codigo+visual",
                    f"o codigo diz '{med_codigo.nome}' mas a embalagem parece "
                    f"'{med_visual.nome}' — possivel etiqueta trocada, "
                    f"reembalagem ou produto irregular",
                )

            conf = min(1.0, self.peso_codigo + self.peso_visual *
                       (1.0 if identificacao.nivel == "confirmado" else 0.6))
            if esperado and med_codigo.dispenser != zona.dispenser:
                return resultado(Veredito.ERRO_POSICAO, conf, med_codigo,
                                 "codigo+visual", "codigo e embalagem concordam")
            return resultado(Veredito.OK, conf, med_codigo, "codigo+visual",
                             "codigo e embalagem concordam")

        # ---------- so o codigo ---------- #
        if med_codigo is not None:
            if self.exigir_visual:
                return resultado(Veredito.INDETERMINADO, 0.4, med_codigo, "codigo",
                                 "codigo lido, mas a embalagem nao foi reconhecida "
                                 "e a politica exige as duas evidencias")
            conf = self.peso_codigo
            if med_codigo.dispenser != zona.dispenser:
                return resultado(Veredito.ERRO_POSICAO, conf, med_codigo, "codigo",
                                 "confirmado apenas pelo codigo")
            return resultado(Veredito.OK, conf, med_codigo, "codigo",
                             "confirmado apenas pelo codigo")

        # ---------- so a aparencia ---------- #
        conf = self.peso_visual * (1.0 if identificacao.nivel == "confirmado" else 0.6)
        detalhe = "codigo ilegivel; identificado pela embalagem"
        if med_visual.dispenser != zona.dispenser:
            return resultado(Veredito.ERRO_POSICAO, conf, med_visual, "visual", detalhe)
        return resultado(Veredito.OK, conf, med_visual, "visual", detalhe)

    # ------------------------------------------------------------------ #
    def avaliar_frame(
        self,
        zonas,
        ocorrencias_por_zona: dict[int, Ocorrencia],
        identificacoes_por_zona: dict[int, Identificacao],
    ) -> list[ResultadoFusao]:
        return [
            self.avaliar(
                zona,
                ocorrencias_por_zona.get(zona.dispenser),
                identificacoes_por_zona.get(zona.dispenser),
            )
            for zona in zonas.zonas
        ]
