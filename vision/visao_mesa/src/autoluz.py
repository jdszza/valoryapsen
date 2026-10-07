"""Auto-ajuste de luz e foco: varre os controles da camera e escolhe o melhor.

E o "melhor caminho" que um operador acharia na mao, so que medido em vez de
tentado no olho. A rotina mexe em DOIS controles, em ordem, e a ordem importa:

    1. EXPOSICAO  ate o brilho medio cair no alvo sem estourar a embalagem.
    2. FOCO       ate a nitidez chegar no maximo.

Exposicao primeiro porque area branca estourada nao tem textura: com a imagem
chapada, a medida de nitidez cai junto e a varredura de foco perseguiria um
numero que nao depende do foco. Resolvido o estouro, o foco passa a ser a unica
coisa que move a nitidez.

Por que e uma MAQUINA DE ESTADOS e nao um laco fechado: cada escrita no driver
leva alguns frames para aparecer na imagem, e um laco bloqueante congelaria a
janela por quase dez segundos. Aqui cada volta do laco principal avanca um
passo, o video continua correndo e o operador ve o progresso.
"""

from __future__ import annotations

from visao_mesa import medir_qualidade

# Alvos, iguais aos do painel de luz.
BRILHO_ALVO = 120.0          # centro da faixa util (85-155)
ESTOURADO_MAXIMO = 5.0       # % de pixels chapados que ainda se tolera


def pontuar_exposicao(qualidade) -> float:
    """Quao boa esta a luz. Maior e melhor.

    O estouro pesa muito mais que o brilho porque e irreversivel: pixel chapado
    perdeu a informacao, e nenhum ajuste posterior traz a borda da embalagem de
    volta. Brilho baixo demais ainda tem detalhe, so com menos contraste.
    """
    estourado = qualidade.reflexo * 100
    nota = -abs(qualidade.brilho - BRILHO_ALVO)
    if estourado > ESTOURADO_MAXIMO:
        nota -= 40 * (estourado - ESTOURADO_MAXIMO)
    return nota + qualidade.nitidez


class AutoLuz:
    """Varredura passo a passo dos controles da camera."""

    def __init__(self, cfg, camera, leitor,
                 frames_espera_exposicao: int = 6,
                 frames_espera_foco: int = 8):
        self.cfg = cfg
        self.camera = camera
        self.leitor = leitor
        # O foco e mecanico e demora mais que a exposicao para assentar.
        self.espera = {"exposicao": frames_espera_exposicao, "foco": frames_espera_foco}
        self.fase = "ocioso"
        self._candidatos: list[int] = []
        self._indice = 0
        self._medidas: list[tuple[int, float]] = []
        self._contador_alvo = 0
        self._melhor_exposicao: int | None = None
        self._melhor_foco: int | None = None
        self.resumo = ""

    # ------------------------------------------------------------------
    @property
    def ativo(self) -> bool:
        return self.fase not in ("ocioso", "pronto")

    def iniciar(self, com_foco: bool = True) -> None:
        """Comeca a varredura. Desliga os automatismos antes de medir."""
        self.com_foco = com_foco
        # Varrer com autoexposicao ligada nao mede nada: a camera desfaz cada
        # valor escrito no passo seguinte.
        self.cfg.camera.autoexposicao = False
        self.cfg.camera.autofoco = False
        self.fase = "exposicao"
        self._candidatos = list(range(-13, 0))
        self._indice = 0
        self._medidas = []
        self.resumo = ""
        self._aplicar_candidato()

    def cancelar(self) -> None:
        self.fase = "ocioso"
        self.resumo = "auto-ajuste cancelado"

    # ------------------------------------------------------------------
    def _aplicar_candidato(self) -> None:
        valor = self._candidatos[self._indice]
        if self.fase == "exposicao":
            self.cfg.camera.exposicao = valor
        else:
            self.cfg.camera.foco = valor
        self.camera.pedir_aplicacao()
        _f, contador, _m = self.leitor.ultimo()
        self._contador_alvo = contador + self.espera[self.fase]

    def passo(self) -> str:
        """Avanca um passo. Chame uma vez por volta do laco principal."""
        if not self.ativo:
            return self.resumo

        frame, contador, _m = self.leitor.ultimo()
        if frame is None or contador < self._contador_alvo:
            return self._texto_progresso()

        qualidade = medir_qualidade(frame)
        valor = self._candidatos[self._indice]
        nota = (pontuar_exposicao(qualidade) if self.fase == "exposicao"
                else qualidade.nitidez)
        self._medidas.append((valor, nota))

        self._indice += 1
        if self._indice < len(self._candidatos):
            self._aplicar_candidato()
            return self._texto_progresso()
        return self._fechar_fase()

    # ------------------------------------------------------------------
    def _fechar_fase(self) -> str:
        melhor = max(self._medidas, key=lambda m: m[1])[0]

        if self.fase == "exposicao":
            self._melhor_exposicao = melhor
            self.cfg.camera.exposicao = melhor
            self.camera.pedir_aplicacao()
            if not self.com_foco:
                return self._concluir()
            # Foco em duas passadas: grossa na faixa inteira, fina em volta do
            # vencedor. Varrer os 256 valores um a um levaria mais de um minuto.
            self.fase = "foco"
            self._candidatos = list(range(0, 256, 24))
            self._indice = 0
            self._medidas = []
            self._aplicar_candidato()
            return self._texto_progresso()

        # fase foco
        if self._candidatos[1] - self._candidatos[0] > 6:
            centro = melhor
            self._candidatos = [v for v in range(max(0, centro - 24),
                                                 min(255, centro + 24) + 1, 6)]
            self._indice = 0
            self._medidas = []
            self._aplicar_candidato()
            return self._texto_progresso()

        self._melhor_foco = melhor
        self.cfg.camera.foco = melhor
        self.camera.pedir_aplicacao()
        return self._concluir()

    def _concluir(self) -> str:
        self.fase = "pronto"
        partes = [f"exposicao={self._melhor_exposicao}"]
        if self._melhor_foco is not None:
            partes.append(f"foco={self._melhor_foco}")
        self.resumo = "AUTO-AJUSTE PRONTO: " + "  ".join(partes) + "   ('s' para salvar)"
        return self.resumo

    def _texto_progresso(self) -> str:
        total = len(self._candidatos)
        atual = min(self._indice + 1, total)
        valor = self._candidatos[min(self._indice, total - 1)]
        return (f"AUTO-AJUSTE  {self.fase}  {atual}/{total}  "
                f"testando {valor}  (ESC cancela)")

    # ------------------------------------------------------------------
    def curva(self) -> list[tuple[int, float]]:
        """Medidas da fase atual, para quem quiser desenhar o caminho."""
        return list(self._medidas)
