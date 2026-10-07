"""Ponte entre o comando do PC central e o algoritmo de contagem que ja existe.

NAO ha algoritmo de visao aqui. Este modulo so:

  * pega frames (da webcam, pelo `Camera` do projeto);
  * chama `VisaoMesa.processar`, que e a contagem de hoje, intocada;
  * repete isso N vezes e tira o voto de maioria;
  * transforma o que a visao devolve (confiavel, cobertura, patamar) num numero
    de `confianca` de 0 a 1, que e o que o contrato do central pede;
  * guarda o total por OS para converter total-da-caixa em quanto-caiu-agora.

A quarta tarefa merece explicacao, porque e onde se mente sem querer. A visao
nao produz probabilidade: ela produz um veredito (`confiavel`) e as medidas que
o sustentam. Inventar `confianca: 0.95` fixo passaria no contrato e seria
falso — o central usaria esse numero para decidir se trava uma OS. O que se faz
aqui e derivar a confianca das medidas que existem de fato, e deixar o veredito
mandar: imagem recusada pela visao sai com confianca 0.0 e vira falha, nunca
divergencia.
"""

from __future__ import annotations

import logging
import sys
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

import cv2

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
if str(RAIZ_PROJETO / "src") not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO / "src"))

from camera import Camera  # noqa: E402
from visao_mesa import ConfigMesa, VisaoMesa  # noqa: E402

registro = logging.getLogger("integracao.contagem")

# Tradução do motivo da visao para o vocabulario de `motivo` do contrato. O
# texto original vai junto, no campo extra `motivo_visao`: o vocabulario do
# central e curto de proposito, e perder a frase explicativa transformaria o
# diagnostico em adivinhacao.
MAPA_MOTIVOS = (
    ("fundo da caixa nao encontrado", "camera_desalinhada"),
    ("reflexo especular", "obstrucao_visual"),
    ("fora de foco", "imagem_fora_de_foco"),
    ("contagem instavel", "baixa_confianca"),
    ("da area detectada virou item", "obstrucao_visual"),
)


def traduzir_motivo(motivo_visao: str) -> str:
    texto = (motivo_visao or "").lower()
    for marca, traducao in MAPA_MOTIVOS:
        if marca in texto:
            return traducao
    return "erro_interno"


# Quadro chapado: preto, branco ou cinza uniforme. Lente tampada, luz apagada,
# cabo solto. A visao ate responde nesses casos, mas responde "fora de foco" —
# area sem textura nenhuma derruba a medida de nitidez. O diagnostico sairia
# errado e mandaria o operador mexer no foco de uma camera que esta e tampada.
BRILHO_MINIMO = 20.0
BRILHO_MAXIMO = 245.0
CONTRASTE_MINIMO = 8.0


def quadro_degenerado(frame) -> str:
    """Devolve o motivo se o quadro nao tem informacao nenhuma, ou "" se tem.

    Medido numa copia reduzida: media e desvio nao mudam com a reducao, e custa
    microssegundos em vez de varrer dois milhoes de pixels.
    """
    cinza = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    pequeno = cv2.resize(cinza, (64, 64), interpolation=cv2.INTER_AREA)
    media, desvio = float(pequeno.mean()), float(pequeno.std())
    if desvio < CONTRASTE_MINIMO and (media < BRILHO_MINIMO or media > BRILHO_MAXIMO):
        return "obstrucao_visual"
    if desvio < CONTRASTE_MINIMO:
        return "obstrucao_visual"
    return ""


# --------------------------------------------------------------------------- #
# Fontes de frame
# --------------------------------------------------------------------------- #
class FonteCamera:
    """A webcam da mesa, aberta uma vez e mantida aberta.

    Abrir custa os frames de aquecimento (20 na config atual, quase um
    segundo): abrir e fechar a cada captura gastaria isso oito vezes por OS,
    dentro do orcamento de 30 s do central. Em compensacao, se uma leitura
    falhar a camera e FECHADA, para que a proxima captura reabra — webcam USB
    que cai costuma voltar so com o handle novo.
    """

    def __init__(self, cfg_camera):
        self.cfg_camera = cfg_camera
        self.cam: Camera | None = None
        self.pronta = False
        self.ultimo_erro = ""

    def frame(self):
        if self.cam is None:
            self.cam = Camera(self.cfg_camera).abrir()
            registro.info("camera %s aberta", self.cfg_camera.indice)
        quadro = self.cam.ler()
        self.pronta = True
        self.ultimo_erro = ""
        return quadro

    def descartar(self) -> None:
        if self.cam is not None:
            try:
                self.cam.fechar()
            except Exception:
                pass
        self.cam = None
        self.pronta = False

    def fechar(self) -> None:
        self.descartar()


class FonteImagem:
    """Uma foto parada no lugar da webcam, para ensaiar a integracao.

    Existe para testar o caminho HTTP com a CNC desligada (ver README). NAO e
    modo de operacao: a estacao avisa no log, em WARNING, a cada captura — uma
    estacao que parece funcionando mas esta olhando um PNG e pior que uma
    estacao parada.
    """

    def __init__(self, caminho: str):
        self.caminho = caminho
        self.imagem = cv2.imread(caminho)
        if self.imagem is None:
            raise RuntimeError(f"nao consegui abrir a imagem fixa {caminho}")
        self.pronta = True
        self.ultimo_erro = ""

    def frame(self):
        registro.warning("IMAGEM FIXA em uso (%s) — isto nao e a camera",
                         self.caminho)
        return self.imagem.copy()

    def descartar(self) -> None:
        return None

    def fechar(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Medicao
# --------------------------------------------------------------------------- #
@dataclass
class Medicao:
    """O que uma captura produziu. `total` None = nao deu para afirmar."""

    total: int | None = None
    confianca: float = 0.0
    motivo: str = ""
    detalhes: dict = field(default_factory=dict)

    @property
    def valida(self) -> bool:
        return self.total is not None


class ContadorMesa:
    """Executa uma captura: N frames, voto de maioria, confianca derivada."""

    def __init__(self, cfg_integracao, cfg_mesa: ConfigMesa | None = None,
                 fonte=None):
        self.cfg = cfg_integracao
        self.cfg_mesa = cfg_mesa or ConfigMesa.carregar()
        # Gate de mudanca DESLIGADO aqui. Ele existe para o video ao vivo: numa
        # cena parada, devolve o resultado em cache e poupa CPU. Nesta estacao
        # isso estragaria a medida — os frames 2..N voltariam o resultado do
        # frame 1 e o "voto de maioria" seria o mesmo voto contado cinco vezes,
        # dando uma estabilidade de 100% que nao foi medida.
        self.cfg_mesa.desempenho.gate_mudanca = False
        self.visao = VisaoMesa(self.cfg_mesa)
        self.fonte = fonte or (FonteImagem(self.cfg.imagem_fixa)
                               if self.cfg.imagem_fixa else
                               FonteCamera(self.cfg_mesa.camera))

    # ------------------------------------------------------------------
    def _confianca(self, estabilidade: float, cobertura: float, patamar: int,
                   com_lock: bool) -> float:
        """Confianca 0..1 a partir das tres medidas que a visao ja produz.

        Pesos e normalizacoes, com o porque de cada um:

        ESTABILIDADE (0,50) — quantos dos N frames concordaram com o vencedor.
        E a evidencia mais forte que existe aqui: um numero que se repete em
        cinco fotos independentes nao e coincidencia de limiar. Normalizada a
        partir de 60%, nao de zero, e a escolha e calibrada: com 5 frames, 3
        concordando (60%) zera a parcela e a leitura cai abaixo do minimo; com
        3 frames, 2 concordando tambem cai. Maioria apertada e empate
        disfarcado, e tem de virar falha, nao um numero com nota media.

        COBERTURA (0,30) — fracao da mascara que virou item. Foi medida na
        bancada: leitura correta fica entre 0,80 e 1,00; leitura errada por
        reflexo cai para ~0,39. E o unico sinal que separa "contei" de "contei
        errado" dentro de um mesmo frame.

        PATAMAR (0,20) — largura da faixa de limiar em que a contagem nao muda.
        Peso menor porque so e informativo no modo lock; com o lock desligado o
        valor nao e medido de verdade, entao o peso dele e redistribuido para os
        outros dois em vez de virar uma penalidade injusta.

        `com_lock` vem de fora, e nao de `self.cfg_mesa.conteudo.lock`, porque
        durante a captura o lock e desligado de proposito depois do primeiro
        frame (ver `_medir_frames`). Ler o flag aqui dentro faria a formula
        trocar de pesos no meio da propria medicao.
        """
        auto = self.cfg_mesa.auto
        est = max(0.0, min(1.0, (estabilidade - 0.6) / 0.4))
        teto_cob = 0.95
        piso_cob = auto.cobertura_minima
        cob = max(0.0, min(1.0, (cobertura - piso_cob) / max(0.05, teto_cob - piso_cob)))

        if com_lock:
            pat = max(0.0, min(1.0, (patamar - auto.patamar_minimo)
                               / max(1.0, 3.0 * auto.patamar_minimo)))
            return 0.50 * est + 0.30 * cob + 0.20 * pat
        return 0.60 * est + 0.40 * cob

    # ------------------------------------------------------------------
    def medir(self, prazo: float | None = None) -> Medicao:
        """Fotografa N vezes, conta cada uma e devolve o veredito do conjunto.

        `prazo` e o instante (time.monotonic) em que a captura vira abobora.
        Checado ENTRE frames: o processamento de um frame nao da para abortar
        no meio, mas se o tempo acabou nao se comeca mais um.
        """
        inicio = time.monotonic()
        total_frames = max(1, self.cfg.frames_por_captura)
        votos: list[int | None] = []
        motivos: list[str] = []
        coberturas: list[float] = []
        patamares: list[int] = []
        detalhe_ultimo: dict = {}
        conteudo = self.cfg_mesa.conteudo
        estado_lock = (conteudo.lock, conteudo.limiar, conteudo.metodo)
        try:
            return self._medir_frames(total_frames, prazo, inicio, votos, motivos,
                                      coberturas, patamares, detalhe_ultimo)
        finally:
            # Restaura SEMPRE. O proximo comando tem de comecar com o lock como
            # o operador deixou na calibragem; herdar o limiar da captura
            # anterior faria a estacao ficar presa na luz de dez minutos atras.
            conteudo.lock, conteudo.limiar, conteudo.metodo = estado_lock

    def _medir_frames(self, total_frames, prazo, inicio, votos, motivos,
                      coberturas, patamares, detalhe_ultimo) -> Medicao:
        conteudo = self.cfg_mesa.conteudo
        lock_original = conteudo.lock
        for indice in range(total_frames):
            if prazo is not None and time.monotonic() > prazo:
                registro.warning("prazo estourado depois de %d frame(s)", indice)
                if not votos:
                    return Medicao(motivo="timeout_processamento",
                                   detalhes={"frames_lidos": indice})
                break
            try:
                quadro = self.fonte.frame()
            except Exception as erro:
                # Camera caiu: descarta o handle para a proxima captura reabrir.
                self.fonte.descartar()
                registro.error("falha ao ler da camera: %s", erro)
                return Medicao(motivo="camera_indisponivel",
                               detalhes={"erro": str(erro), "frames_lidos": indice})
            if quadro is None:
                self.fonte.descartar()
                return Medicao(motivo="camera_indisponivel",
                               detalhes={"erro": "frame vazio", "frames_lidos": indice})

            degenerado = quadro_degenerado(quadro)
            if degenerado:
                return Medicao(motivo=degenerado,
                               detalhes={"frames_lidos": indice + 1,
                                         "motivo_visao": "quadro sem contraste "
                                                         "(lente tampada, luz apagada "
                                                         "ou cabo solto)"})

            try:
                resultado = self.visao.processar(quadro)
            except Exception as erro:
                registro.exception("erro dentro da visao")
                return Medicao(motivo="erro_interno",
                               detalhes={"erro": str(erro), "frames_lidos": indice + 1})

            votos.append(resultado.contagem)      # None quando a visao recusou
            if resultado.contagem is None:
                motivos.append(resultado.motivo)
            elif indice == 0 and conteudo.lock:
                # O caro na leitura e a VARREDURA de limiar do modo lock: 1,5 s
                # por frame, contra 0,3 s com o limiar ja escolhido. Dentro de
                # uma mesma captura a cena e a mesma, entao varrer de novo em
                # cada frame paga cinco vezes pela mesma resposta — e numa
                # maquina lenta isso encosta no teto de 20 s.
                #
                # O que se perde: os frames 2..N nao testam mais a fragilidade
                # do limiar. O que se mantem, que e o que importa aqui: eles
                # continuam sendo FOTOS NOVAS, entao ruido do sensor, variacao
                # de luz e uma embalagem que se mexeu ainda aparecem no voto. A
                # fragilidade do limiar continua medida uma vez, pelo patamar.
                conteudo.lock = False
                conteudo.limiar = int(resultado.limiar_conteudo)
                conteudo.metodo = resultado.metodo_conteudo
            coberturas.append(float(resultado.cobertura))
            patamares.append(int(resultado.patamar))
            detalhe_ultimo = {
                "limiar_conteudo": int(resultado.limiar_conteudo),
                "metodo_conteudo": resultado.metodo_conteudo,
                "nitidez": round(float(resultado.qualidade.nitidez), 2)
                if resultado.qualidade else None,
                "estourado_pct": round(float(resultado.qualidade.reflexo) * 100, 2)
                if resultado.qualidade else None,
            }

        tempo_ms = int((time.monotonic() - inicio) * 1000)
        contagem_votos = Counter(votos)
        vencedor, apoios = contagem_votos.most_common(1)[0]
        estabilidade = apoios / len(votos)

        base = {
            "frames_lidos": len(votos),
            "frames_concordantes": apoios,
            "estabilidade": round(estabilidade, 3),
            "cobertura": round(sum(coberturas) / len(coberturas), 3),
            "patamar": max(patamares),
            "tempo_processamento_ms": tempo_ms,
            **detalhe_ultimo,
        }

        if vencedor is None:
            # A maioria dos frames foi recusada pela propria visao. O motivo
            # mais frequente e o que vai para o evento — se metade recusou por
            # reflexo e metade por foco, o que mais apareceu e o que o operador
            # tem de atacar primeiro.
            motivo_visao = Counter(motivos).most_common(1)[0][0] if motivos else ""
            base["motivo_visao"] = motivo_visao
            return Medicao(motivo=traduzir_motivo(motivo_visao), detalhes=base)

        confianca = self._confianca(estabilidade, base["cobertura"],
                                    base["patamar"], com_lock=lock_original)
        if confianca < self.cfg.confianca_minima:
            return Medicao(confianca=confianca, motivo="baixa_confianca", detalhes=base)
        return Medicao(total=int(vencedor), confianca=confianca, detalhes=base)

    def fechar(self) -> None:
        self.fonte.fechar()

    @property
    def camera_pronta(self) -> bool:
        return bool(getattr(self.fonte, "pronta", False))


# --------------------------------------------------------------------------- #
# Total da caixa -> quanto caiu agora
# --------------------------------------------------------------------------- #
class AcumuladoPorOS:
    """Converte o total visto na caixa no incremento daquele slot.

    A camera ve a caixa INTEIRA; o central pergunta quantas unidades aquele
    dispenser acabou de soltar. Numa OS de varios slots os medicamentos se
    acumulam, entao o que o central espera e sempre `total_agora -
    total_anterior`.

    O preco disso e um estado com memoria, e e o estado que estraga primeiro:
    se um comando reenviado pelo central fosse processado duas vezes, o
    `total_anterior` seria atualizado duas vezes e o SLOT SEGUINTE daria
    divergencia — um erro que aparece longe de onde nasceu. A protecao contra
    isso nao esta aqui, esta na idempotencia do servidor; aqui so se garante
    que a atualizacao e atomica e explicita.
    """

    def __init__(self, maximo: int = 50, validade_h: float = 2.0):
        self.maximo = maximo
        self.validade_s = validade_h * 3600.0
        self._totais: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._trava = Lock()

    def anterior(self, os_id: str) -> int:
        with self._trava:
            self._limpar()
            registro_os = self._totais.get(str(os_id))
            # OS nova comeca em zero: a suposicao e que cada OS comeca com a
            # caixa de coleta VAZIA. Se um dia nao for verdade, isto aqui e o
            # unico ponto a mudar (ver relatorio, secao de suposicoes).
            return registro_os[0] if registro_os else 0

    def registrar(self, os_id: str, total: int) -> None:
        with self._trava:
            chave = str(os_id)
            self._totais[chave] = (int(total), time.time())
            self._totais.move_to_end(chave)
            while len(self._totais) > self.maximo:
                antiga, _ = self._totais.popitem(last=False)
                registro.info("acumulado da OS %s descartado (memoria cheia)", antiga)

    def esquecer(self, os_id: str) -> None:
        with self._trava:
            self._totais.pop(str(os_id), None)

    def _limpar(self) -> None:
        agora = time.time()
        vencidas = [k for k, (_, t) in self._totais.items()
                    if agora - t > self.validade_s]
        for chave in vencidas:
            self._totais.pop(chave, None)
            registro.info("acumulado da OS %s expirou (%.0f h sem captura)",
                          chave, self.validade_s / 3600)

    def instantaneo(self) -> dict:
        with self._trava:
            return {k: v[0] for k, v in self._totais.items()}
