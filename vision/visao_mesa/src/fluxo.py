"""Captura, processamento e desenho em threads separadas.

O problema que isto resolve nao e velocidade de algoritmo — e ACOPLAMENTO. Num
laco unico, o desenho so acontece depois que a deteccao termina, entao qualquer
pico de processamento vira engasgo visivel no video. Pior: `cap.read()` bloqueia
esperando o proximo frame da camera, e o laco inteiro fica refem do relogio do
sensor.

Com tres estagios independentes:

    LeitorCamera      la um frame atras do outro e guarda SEMPRE o mais novo,
                      descartando os atrasados. O laco principal nunca espera
                      a camera.

    ProcessadorVisao  pega o frame mais novo disponivel, processa, publica o
                      resultado. Se a deteccao demora 240 ms, ela demora 240 ms
                      aqui dentro — sem prender o video.

    laco principal    desenha o frame ao VIVO com o ultimo resultado conhecido.
                      O video fica liso na taxa da camera; a contagem atualiza
                      na taxa que a maquina aguentar.

O preco e que a caixa desenhada pode estar alguns milissegundos atrasada em
relacao ao frame exibido. Numa mesa onde as caixas ficam paradas isso e
invisivel — e o modulo reporta a idade do resultado para quem quiser conferir.
"""

from __future__ import annotations

import threading
import time

import numpy as np


class LeitorCamera(threading.Thread):
    """Le a camera continuamente e mantem so o frame mais recente.

    Descartar frame atrasado e proposital: numa celula robotica, agir sobre uma
    imagem de 200 ms atras e pior do que nao agir. A fila aqui tem tamanho um.
    """

    def __init__(self, camera, nome: str = "leitor-camera"):
        super().__init__(name=nome, daemon=True)
        self.camera = camera
        self._trava = threading.Lock()
        self._frame: np.ndarray | None = None
        self._contador = 0
        self._momento = 0.0
        self._parar = threading.Event()
        self._erro: Exception | None = None
        self._intervalos: list[float] = []

    def run(self) -> None:
        anterior = time.monotonic()
        while not self._parar.is_set():
            try:
                frame = self.camera.ler_direto()
            except Exception as erro:  # camera desconectada no meio da operacao
                self._erro = erro
                time.sleep(0.05)
                continue
            agora = time.monotonic()
            with self._trava:
                self._frame = frame
                self._contador += 1
                self._momento = agora
                self._intervalos.append(agora - anterior)
                if len(self._intervalos) > 60:
                    self._intervalos.pop(0)
            anterior = agora

    def ultimo(self) -> tuple[np.ndarray | None, int, float]:
        with self._trava:
            return self._frame, self._contador, self._momento

    def esperar_primeiro(self, timeout: float = 5.0) -> bool:
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            if self.ultimo()[0] is not None:
                return True
            if self._erro is not None:
                raise self._erro
            time.sleep(0.01)
        return False

    @property
    def fps(self) -> float:
        with self._trava:
            if not self._intervalos:
                return 0.0
            media = sum(self._intervalos) / len(self._intervalos)
        return 1.0 / media if media > 0 else 0.0

    def parar(self) -> None:
        self._parar.set()
        self.join(timeout=2.0)


class ProcessadorVisao(threading.Thread):
    """Roda a deteccao sobre o frame mais novo, fora do laco de desenho."""

    def __init__(self, visao, leitor: LeitorCamera, nome: str = "visao"):
        super().__init__(name=nome, daemon=True)
        self.visao = visao
        self.leitor = leitor
        self._trava = threading.Lock()
        self._resultado = None
        self._frame_usado: np.ndarray | None = None
        self._momento = 0.0
        self._ultimo_contador = -1
        self._frame_fixo: np.ndarray | None = None
        self._parar = threading.Event()
        self._pausado = threading.Event()
        self._intervalos: list[float] = []

    def run(self) -> None:
        while not self._parar.is_set():
            if self._pausado.is_set():
                time.sleep(0.01)
                continue
            with self._trava:
                fixo = self._frame_fixo
            if fixo is not None:
                # Quadro congelado: reprocessa a cada volta para o slider dar
                # retorno na hora. Sai barato porque o gate de mudanca da
                # VisaoMesa devolve o resultado em cache quando nada mudou.
                inicio = time.monotonic()
                try:
                    resultado = self.visao.processar(fixo)
                except Exception:
                    time.sleep(0.01)
                    continue
                with self._trava:
                    self._resultado = resultado
                    self._frame_usado = fixo
                    self._momento = time.monotonic()
                    self._intervalos.append(time.monotonic() - inicio)
                    if len(self._intervalos) > 30:
                        self._intervalos.pop(0)
                time.sleep(0.005)
                continue

            frame, contador, _momento = self.leitor.ultimo()
            if frame is None or contador == self._ultimo_contador:
                # Nada novo: dorme um tiquinho em vez de girar em vazio
                # queimando um nucleo que a maquina fraca nao tem de sobra.
                time.sleep(0.003)
                continue
            inicio = time.monotonic()
            try:
                resultado = self.visao.processar(frame)
            except Exception:
                self._ultimo_contador = contador
                continue
            duracao = time.monotonic() - inicio
            with self._trava:
                self._resultado = resultado
                self._frame_usado = frame
                self._momento = time.monotonic()
                self._ultimo_contador = contador
                self._intervalos.append(duracao)
                if len(self._intervalos) > 30:
                    self._intervalos.pop(0)

    def ultimo(self):
        """(resultado, frame_usado, idade_em_segundos)."""
        with self._trava:
            if self._resultado is None:
                return None, None, 0.0
            return self._resultado, self._frame_usado, time.monotonic() - self._momento

    def esperar_primeiro(self, timeout: float = 10.0) -> bool:
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            if self.ultimo()[0] is not None:
                return True
            time.sleep(0.01)
        return False

    @property
    def fps(self) -> float:
        with self._trava:
            if not self._intervalos:
                return 0.0
            media = sum(self._intervalos) / len(self._intervalos)
        return 1.0 / media if media > 0 else 0.0

    def fixar(self, frame: np.ndarray | None) -> None:
        """Trava a deteccao num quadro (congelar) ou solta (None)."""
        with self._trava:
            self._frame_fixo = frame
            self._ultimo_contador = -1

    def pausar(self, pausado: bool = True) -> None:
        self._pausado.set() if pausado else self._pausado.clear()

    def parar(self) -> None:
        self._parar.set()
        self.join(timeout=3.0)


class FluxoVisao:
    """Conveniencia: sobe os dois estagios e derruba os dois."""

    def __init__(self, camera, visao):
        self.leitor = LeitorCamera(camera)
        self.processador = ProcessadorVisao(visao, self.leitor)

    def iniciar(self, timeout: float = 5.0) -> "FluxoVisao":
        self.leitor.start()
        if not self.leitor.esperar_primeiro(timeout):
            self.leitor.parar()
            raise RuntimeError(
                "A camera abriu mas nao entregou nenhum frame. Feche outros "
                "programas que possam estar usando a webcam e tente de novo."
            )
        self.processador.start()
        return self

    def parar(self) -> None:
        self.processador.parar()
        self.leitor.parar()

    def __enter__(self) -> "FluxoVisao":
        return self.iniciar()

    def __exit__(self, *_exc) -> None:
        self.parar()
