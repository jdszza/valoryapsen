"""Motor de decisao: cruza os codigos lidos com as zonas calibradas.

Duas ideias importantes para funcionar com camera ruim:

1. **Confirmacao por leituras reais, nao por frames consecutivos.**
   Se a camera so consegue decodificar 1 frame em cada 8, exigir 5 frames
   *seguidos* nunca dispara alerta nenhum. Aqui o contador sobe a cada leitura
   bem-sucedida, independentemente de quantos frames falharam no meio.

2. **Memoria temporal por dispenser.**
   Os dispensers sao estaticos: a caixa que estava ali ha 300 ms continua ali.
   Depois de ler um codigo, a zona guarda esse estado por alguns segundos.
   Isso elimina o piscar do status e do alerta quando a leitura e intermitente.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

import numpy as np

from configuracao import Catalogo, MapaZonas, Medicamento, Zona
from leitor_qr import LeitorCodigos, LeituraQR, agrupar_em_unidades


class Estado(str, Enum):
    OK = "OK"
    ERRO_POSICAO = "ERRO_POSICAO"        # medicamento no dispenser errado
    QR_DESCONHECIDO = "QR_DESCONHECIDO"  # codigo nao cadastrado
    FORA_DE_ZONA = "FORA_DE_ZONA"        # codigo visivel fora de qualquer dispenser


ESTADOS_DE_ERRO = {Estado.ERRO_POSICAO, Estado.QR_DESCONHECIDO, Estado.FORA_DE_ZONA}


@dataclass
class Ocorrencia:
    """Resultado da analise de um unico codigo."""

    estado: Estado
    conteudo: str
    leitura: LeituraQR
    zona: Zona | None = None
    medicamento: Medicamento | None = None
    esperado_na_zona: Medicamento | None = None
    leituras_reais: int = 0
    confirmada: bool = False
    alertar_agora: bool = False
    lembrada: bool = False        # veio da memoria, nao foi lido neste frame
    idade_segundos: float = 0.0   # ha quanto tempo foi lido de fato
    codigos: list = field(default_factory=list)  # codigos que formaram esta unidade
    quantidade_na_zona: int = 1   # quantas unidades deste medicamento ha na zona

    @property
    def e_erro(self) -> bool:
        return self.estado in ESTADOS_DE_ERRO

    @property
    def dispenser_detectado(self) -> int | None:
        return self.zona.dispenser if self.zona else None

    @property
    def dispenser_esperado(self) -> int | None:
        return self.medicamento.dispenser if self.medicamento else None

    @property
    def nome_medicamento(self) -> str:
        return self.medicamento.nome if self.medicamento else "DESCONHECIDO"

    @property
    def tipo_codigo(self) -> str:
        return self.leitura.tipo

    def mensagem(self) -> str:
        quantos = (f"{self.quantidade_na_zona} unidades de "
                   if self.quantidade_na_zona > 1 else "")
        if self.estado is Estado.OK:
            return (f"OK - {quantos}{self.nome_medicamento} no dispenser "
                    f"{self.dispenser_detectado}")
        if self.estado is Estado.ERRO_POSICAO:
            esperado = self.esperado_na_zona.nome if self.esperado_na_zona else "-"
            return (
                f"ERRO - {quantos}'{self.nome_medicamento}' no dispenser "
                f"{self.dispenser_detectado}, mas pertence ao dispenser "
                f"{self.dispenser_esperado} (o dispenser {self.dispenser_detectado} "
                f"deveria conter '{esperado}')"
            )
        if self.estado is Estado.QR_DESCONHECIDO:
            disp = self.dispenser_detectado
            onde = f"no dispenser {disp}" if disp else "fora das zonas"
            return f"ERRO - codigo nao cadastrado '{self.conteudo}' {onde}"
        return f"AVISO - '{self.nome_medicamento}' visivel fora de qualquer dispenser"

    def para_dict(self) -> dict:
        cx, cy = self.leitura.centro
        return {
            "estado": self.estado.value,
            "qr": self.conteudo,
            "medicamento": self.nome_medicamento,
            "dispenser_detectado": self.dispenser_detectado,
            "dispenser_esperado": self.dispenser_esperado,
            "esperado_na_zona": self.esperado_na_zona.nome if self.esperado_na_zona else None,
            "tipo_codigo": self.tipo_codigo,
            "quantidade_na_zona": self.quantidade_na_zona,
            "centro_x": round(cx, 1),
            "centro_y": round(cy, 1),
            "mensagem": self.mensagem(),
        }


def _prioridade(leitura: LeituraQR) -> tuple[int, float]:
    """QR ganha do ArUco na exibicao (e o codigo 'oficial'); depois, o maior."""
    return (1 if leitura.tipo == "qr" else 0, leitura.lado)


@dataclass
class _Rastro:
    """Estado de um codigo observado numa zona ao longo do tempo."""

    leituras_reais: int = 0
    quantidade: int = 0
    ultimo_visto: float = 0.0
    ultima_leitura: LeituraQR | None = None
    ultimo_alerta: float = 0.0
    ja_alertou: bool = False


@dataclass
class Estatisticas:
    """Taxa de leitura por dispenser — base do diagnostico."""

    frames: int = 0
    lidos: dict[int, int] = field(default_factory=dict)
    tipos: dict[str, int] = field(default_factory=dict)

    def taxa(self, dispenser: int) -> float:
        return (self.lidos.get(dispenser, 0) / self.frames * 100.0) if self.frames else 0.0


@dataclass
class ResultadoFrame:
    ocorrencias: list[Ocorrencia] = field(default_factory=list)
    zonas: MapaZonas | None = None

    @property
    def erros(self) -> list[Ocorrencia]:
        return [o for o in self.ocorrencias if o.e_erro]

    @property
    def erros_confirmados(self) -> list[Ocorrencia]:
        return [o for o in self.ocorrencias if o.e_erro and o.confirmada]

    @property
    def alertas_novos(self) -> list[Ocorrencia]:
        return [o for o in self.ocorrencias if o.alertar_agora]

    @property
    def ha_erro(self) -> bool:
        return bool(self.erros_confirmados)

    @property
    def lidos_agora(self) -> list[Ocorrencia]:
        return [o for o in self.ocorrencias if not o.lembrada]

    def contagem_por_dispenser(self) -> dict[int, dict[str, int]]:
        """{dispenser: {nome do medicamento: quantidade de unidades}}"""
        # usa `quantidade_na_zona` em vez de contar ocorrencias: quando o estado
        # vem da memoria (leitura falhou neste frame) existe uma ocorrencia so
        # representando as N unidades, e contar ocorrencias diria 1
        mapa: dict[int, dict[str, int]] = {}
        for o in self.ocorrencias:
            if o.dispenser_detectado is None:
                continue
            zona = mapa.setdefault(o.dispenser_detectado, {})
            nome = o.nome_medicamento
            zona[nome] = max(zona.get(nome, 0), o.quantidade_na_zona)
        return mapa

    def por_dispenser(self) -> dict[int, list[Ocorrencia]]:
        mapa: dict[int, list[Ocorrencia]] = {}
        for o in self.ocorrencias:
            if o.dispenser_detectado is not None:
                mapa.setdefault(o.dispenser_detectado, []).append(o)
        return mapa


class DetectorDispensers:
    """Aplica as regras de posicionamento sobre os codigos lidos."""

    def __init__(
        self,
        catalogo: Catalogo,
        zonas: MapaZonas,
        leitor: LeitorCodigos | None = None,
        frames_para_confirmar: int = 3,
        cooldown_segundos: float = 10.0,
        memoria_segundos: float = 2.0,
        margem_zona: float = 0.0,
        alertar_qr_desconhecido: bool = True,
        alertar_fora_de_zona: bool = False,
        relogio=time.monotonic,
        **compat,
    ) -> None:
        self.catalogo = catalogo
        self.zonas = zonas
        self.zonas_ativas = zonas
        self.leitor = leitor or LeitorCodigos()
        self.leituras_para_confirmar = max(1, int(frames_para_confirmar))
        self.cooldown_segundos = float(cooldown_segundos)
        self.memoria_segundos = float(memoria_segundos)
        self.margem_zona = float(margem_zona)
        self.alertar_qr_desconhecido = alertar_qr_desconhecido
        self.alertar_fora_de_zona = alertar_fora_de_zona
        self._relogio = relogio
        self._rastros: dict[tuple[str, object], _Rastro] = {}
        self._resolucao_atual: tuple[int, int] | None = None
        self.estatisticas = Estatisticas()

    # ------------------------------------------------------------------ #
    def processar_frame(self, frame: np.ndarray) -> ResultadoFrame:
        altura, largura = frame.shape[:2]
        if self._resolucao_atual != (largura, altura):
            self._resolucao_atual = (largura, altura)
            self.zonas_ativas = self.zonas.para_resolucao(largura, altura)
        leituras = self.leitor.ler(frame, zonas=self.zonas_ativas, agora=self._relogio())
        return self.analisar(leituras)

    # ------------------------------------------------------------------ #
    def analisar(self, leituras: Iterable[LeituraQR]) -> ResultadoFrame:
        zonas = self.zonas_ativas
        agora = self._relogio()
        ocorrencias: list[Ocorrencia] = []
        zonas_com_leitura: set[object] = set()

        self.estatisticas.frames += 1

        # ---------------- leituras reais deste frame ---------------- #
        for leitura, cluster, quantidade in self._consolidar(leituras, zonas):
            oc, chave = self._avaliar(leitura, zonas)
            oc.codigos = cluster
            oc.quantidade_na_zona = quantidade
            rastro = self._rastros.setdefault(chave, _Rastro())
            rastro.leituras_reais += 1
            rastro.ultimo_visto = agora
            rastro.ultima_leitura = leitura
            # mudanca de quantidade e um evento novo: reabre o alerta mesmo
            # dentro do cooldown, senao acrescentar uma caixa errada passa batido
            if rastro.quantidade != quantidade:
                rastro.quantidade = quantidade
                rastro.ja_alertou = False

            oc.leituras_reais = rastro.leituras_reais
            oc.confirmada = rastro.leituras_reais >= self.leituras_para_confirmar
            self._talvez_alertar(oc, rastro, agora)

            zonas_com_leitura.add(chave[1])
            ocorrencias.append(oc)

            if oc.dispenser_detectado is not None:
                d = oc.dispenser_detectado
                self.estatisticas.lidos[d] = self.estatisticas.lidos.get(d, 0) + 1
            t = leitura.tipo
            self.estatisticas.tipos[t] = self.estatisticas.tipos.get(t, 0) + 1

        # ---------------- memoria: zonas sem leitura agora ---------------- #
        for chave, rastro in list(self._rastros.items()):
            idade = agora - rastro.ultimo_visto
            if idade > self.memoria_segundos:
                del self._rastros[chave]
                continue
            if chave[1] in zonas_com_leitura or rastro.ultima_leitura is None:
                continue

            oc, _ = self._avaliar(rastro.ultima_leitura, zonas)
            oc.quantidade_na_zona = max(1, rastro.quantidade)
            oc.leituras_reais = rastro.leituras_reais
            oc.confirmada = rastro.leituras_reais >= self.leituras_para_confirmar
            oc.lembrada = True
            oc.idade_segundos = idade
            # nao dispara alerta novo a partir da memoria: so mantem o estado
            ocorrencias.append(oc)

        return ResultadoFrame(ocorrencias=ocorrencias, zonas=zonas)

    # ------------------------------------------------------------------ #
    def _consolidar(self, leituras: Iterable[LeituraQR], zonas: MapaZonas):
        """Agrupa os codigos em UNIDADES FISICAS por medicamento e por zona.

        Uma etiqueta traz tres codigos (1 QR + 2 ArUco), entao contar codigos
        contaria tres caixas onde ha uma. Aqui os codigos de um mesmo
        medicamento na mesma zona sao agrupados por proximidade: cada grupo e
        uma caixa. Assim o sistema informa "3 unidades de Dipirona no dispenser
        1" e continua distinguindo o que esta certo do que esta errado.

        Tambem descarta id ArUco que nao existe no catalogo: como o ArUco e
        detectado de forma permissiva, um id desconhecido tem chance real de ser
        falso positivo, e um alerta falso e pior que uma leitura a menos. QR
        desconhecido continua virando alerta, porque QR nao gera falso positivo.
        """
        por_chave: dict[tuple, list[LeituraQR]] = {}
        soltas: list[LeituraQR] = []

        for leitura in leituras:
            conhecido = self.catalogo.buscar(leitura.conteudo)
            if conhecido is None:
                if leitura.tipo == "aruco":
                    continue  # id ArUco fora do catalogo: provavel falso positivo
                soltas.append(leitura)
                continue

            cx, cy = leitura.centro
            zona = zonas.zona_do_ponto(cx, cy, self.margem_zona * leitura.lado)
            if zona is None and leitura.zona is not None:
                zona = zonas.por_dispenser(leitura.zona)
            chave = (conhecido.qr, zona.dispenser if zona else "fora")
            por_chave.setdefault(chave, []).append(leitura)

        unidades: list[tuple[LeituraQR, list[LeituraQR], int]] = []
        for grupo in por_chave.values():
            clusters = agrupar_em_unidades(grupo)
            for cluster in clusters:
                representante = max(cluster, key=_prioridade)
                unidades.append((representante, cluster, len(clusters)))

        for leitura in soltas:
            unidades.append((leitura, [leitura], 1))

        return unidades

    # ------------------------------------------------------------------ #
    def _avaliar(self, leitura: LeituraQR, zonas: MapaZonas):
        cx, cy = leitura.centro
        margem = self.margem_zona * leitura.lado
        zona = zonas.zona_do_ponto(cx, cy, margem)
        if zona is None and leitura.zona is not None:
            zona = zonas.por_dispenser(leitura.zona)  # veio do recorte daquela zona

        medicamento = self.catalogo.buscar(leitura.conteudo)
        esperado = self.catalogo.esperado_em(zona.dispenser) if zona else None

        if medicamento is None:
            estado = Estado.QR_DESCONHECIDO
        elif zona is None:
            estado = Estado.FORA_DE_ZONA
        elif zona.dispenser == medicamento.dispenser:
            estado = Estado.OK
        else:
            estado = Estado.ERRO_POSICAO

        oc = Ocorrencia(
            estado=estado,
            conteudo=leitura.conteudo,
            leitura=leitura,
            zona=zona,
            medicamento=medicamento,
            esperado_na_zona=esperado,
        )
        # a chave identifica "este medicamento nesta zona"; codigos QR e ArUco do
        # mesmo medicamento compartilham a chave, entao as leituras se somam.
        identidade = medicamento.qr if medicamento else leitura.conteudo
        chave = (identidade, zona.dispenser if zona else "fora")
        return oc, chave

    def _talvez_alertar(self, oc: Ocorrencia, rastro: _Rastro, agora: float) -> None:
        if not (oc.e_erro and oc.confirmada and self._deve_alertar(oc.estado)):
            return
        dentro_cooldown = (
            rastro.ja_alertou and (agora - rastro.ultimo_alerta) < self.cooldown_segundos
        )
        if dentro_cooldown:
            return
        oc.alertar_agora = True
        rastro.ultimo_alerta = agora
        rastro.ja_alertou = True

    def _deve_alertar(self, estado: Estado) -> bool:
        if estado is Estado.ERRO_POSICAO:
            return True
        if estado is Estado.QR_DESCONHECIDO:
            return self.alertar_qr_desconhecido
        if estado is Estado.FORA_DE_ZONA:
            return self.alertar_fora_de_zona
        return False

    def resetar(self) -> None:
        self._rastros.clear()
        self.estatisticas = Estatisticas()
