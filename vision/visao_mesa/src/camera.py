"""Captura da webcam com foco, exposicao e white balance travados.

Regra que vale mais que qualquer ajuste de algoritmo: uma camera em modo
automatico invalida a calibragem sozinha. O braco robotico entra no quadro, a
camera reage, e o limiar que voce ajustou ontem nao vale mais. Este modulo
desliga os tres automatismos e CONFERE se o driver obedeceu.
"""

from __future__ import annotations

import platform
import threading
import time
from contextlib import contextmanager

import cv2
import numpy as np

from visao_mesa import ConfigCamera


def backend_preferido() -> int:
    """DSHOW no Windows: e o unico que expoe foco e exposicao na maioria das
    webcams. O MSMF aceita o comando e ignora em silencio."""
    sistema = platform.system()
    if sistema == "Windows":
        return cv2.CAP_DSHOW
    if sistema == "Linux":
        return cv2.CAP_V4L2
    return cv2.CAP_ANY


class Camera:
    def __init__(self, cfg: ConfigCamera):
        self.cfg = cfg
        self.cap: cv2.VideoCapture | None = None
        self._sistema = platform.system()
        # Pedido de reaplicacao vindo de outra thread (os sliders da aba 3).
        # VideoCapture nao e seguro entre threads: chamar cap.set() na thread da
        # interface enquanto a thread de captura esta em cap.read() trava ou
        # corrompe o driver. Por isso o pedido so levanta uma bandeira, e quem
        # aplica e a propria thread que le.
        self._reaplicar = threading.Event()

    # ------------------------------------------------------------------
    def abrir(self) -> "Camera":
        cap = cv2.VideoCapture(self.cfg.indice, backend_preferido())
        if not cap.isOpened():
            raise RuntimeError(
                f"Nao consegui abrir a camera no indice {self.cfg.indice}.\n"
                "Rode 'python camera_finder.py' para descobrir o indice correto."
            )
        self.cap = cap

        # FOURCC ANTES da resolucao: se a ordem inverter, o driver fixa a
        # resolucao em YUYV e depois ignora o pedido de MJPG.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.cfg.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.largura)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.altura)
        cap.set(cv2.CAP_PROP_FPS, self.cfg.fps)
        # Buffer de 1: sem isso a maquina fraca entrega frames atrasados, e a
        # visao responde sobre uma cena que a celula ja mudou.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self.aplicar_controles()
        for _ in range(self.cfg.frames_aquecimento):
            cap.read()
            time.sleep(0.01)
        return self

    def pedir_aplicacao(self) -> None:
        """Marca que os controles mudaram. Aplicado na proxima leitura."""
        self._reaplicar.set()

    def aplicar_controles(self) -> None:
        """Escreve no driver todos os ajustes de luz e foco da config."""
        cap = self.cap
        if cap is None:
            return
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 1 if self.cfg.autofoco else 0)
        if not self.cfg.autofoco:
            cap.set(cv2.CAP_PROP_FOCUS, self.cfg.foco)

        # A semantica de AUTO_EXPOSURE muda com o backend:
        #   V4L2  -> 3 automatico, 1 manual
        #   DSHOW -> 0.75 automatico, 0.25 manual
        if self.cfg.autoexposicao:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3 if self._sistema == "Linux" else 0.75)
        else:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1 if self._sistema == "Linux" else 0.25)
            cap.set(cv2.CAP_PROP_EXPOSURE, self.cfg.exposicao)

        cap.set(cv2.CAP_PROP_AUTO_WB, 1 if self.cfg.autobalanco_branco else 0)
        if not self.cfg.autobalanco_branco:
            cap.set(cv2.CAP_PROP_WB_TEMPERATURE, self.cfg.temperatura_cor)

        # -1 = nao mexer. Escrever um valor onde a webcam nao expoe o controle
        # e inofensivo, mas escrever 0 sem querer apaga a imagem.
        if self.cfg.ganho >= 0:
            cap.set(cv2.CAP_PROP_GAIN, self.cfg.ganho)
        if self.cfg.brilho >= 0:
            cap.set(cv2.CAP_PROP_BRIGHTNESS, self.cfg.brilho)
        if self.cfg.contraste >= 0:
            cap.set(cv2.CAP_PROP_CONTRAST, self.cfg.contraste)

    # ------------------------------------------------------------------
    def ler_direto(self) -> np.ndarray:
        """Um unico frame do driver, sem descarte.

        E o que a thread de captura usa. Como ela le sem parar, o buffer nunca
        represa nada e o descarte extra so custaria uma espera de frame.
        """
        cap = self.cap
        if cap is None:
            raise RuntimeError("Camera nao aberta. Chame abrir() antes.")
        if self._reaplicar.is_set():
            self._reaplicar.clear()
            self.aplicar_controles()
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError("Falha ao ler frame da camera.")
        return cv2.flip(frame, 1) if self.cfg.espelhar else frame

    def ler(self) -> np.ndarray:
        """Frame atual, descartando o que estiver represado no buffer.

        Para uso SEM thread de captura (script pontual, --uma-vez). Custa uma
        espera de frame a mais, de proposito: sem o descarte, um laco lento vai
        consumindo o buffer e acaba respondendo sobre uma cena que a celula ja
        mudou. Com a thread de captura ligada, use ler_direto().
        """
        cap = self.cap
        if cap is None:
            raise RuntimeError("Camera nao aberta. Chame abrir() antes.")
        cap.grab()  # descarta o frame represado sem decodificar
        return self.ler_direto()

    def ler_estavel(self, n: int = 3) -> np.ndarray:
        """Mediana de N frames: reduz ruido sem borrar borda.

        Usada na captura de comando (evento pontual), nunca no video continuo.
        A mediana, e nao a media, porque uma faisca de ruido nao move a mediana.
        """
        frames = [self.ler().astype(np.float32) for _ in range(max(1, n))]
        return np.median(np.stack(frames), axis=0).astype(np.uint8)

    def conferir(self) -> dict:
        """O que o driver REALMENTE aplicou. Nem todo set() e obedecido."""
        cap = self.cap
        assert cap is not None
        return {
            "largura": cap.get(cv2.CAP_PROP_FRAME_WIDTH),
            "altura": cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
            "fps": cap.get(cv2.CAP_PROP_FPS),
            "autofoco": cap.get(cv2.CAP_PROP_AUTOFOCUS),
            "foco": cap.get(cv2.CAP_PROP_FOCUS),
            "auto_exposicao": cap.get(cv2.CAP_PROP_AUTO_EXPOSURE),
            "exposicao": cap.get(cv2.CAP_PROP_EXPOSURE),
            "auto_wb": cap.get(cv2.CAP_PROP_AUTO_WB),
            "ganho": cap.get(cv2.CAP_PROP_GAIN),
            "brilho": cap.get(cv2.CAP_PROP_BRIGHTNESS),
            "contraste": cap.get(cv2.CAP_PROP_CONTRAST),
        }

    def fechar(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


@contextmanager
def abrir_camera(cfg: ConfigCamera):
    cam = Camera(cfg)
    try:
        yield cam.abrir()
    finally:
        cam.fechar()
