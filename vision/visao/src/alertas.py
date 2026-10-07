"""Canais de saida dos alertas: console, CSV, som, webhook e GPIO."""

from __future__ import annotations

import csv
import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from detector import Ocorrencia

RAIZ = Path(__file__).resolve().parent.parent

CABECALHO_CSV = [
    "timestamp",
    "estado",
    "qr",
    "medicamento",
    "dispenser_detectado",
    "dispenser_esperado",
    "esperado_na_zona",
    "centro_x",
    "centro_y",
    "mensagem",
]


class CanalAlerta:
    def enviar(self, oc: Ocorrencia) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def fechar(self) -> None:
        pass


# --------------------------------------------------------------------------- #
class CanalConsole(CanalAlerta):
    VERMELHO = "\033[91m"
    AMARELO = "\033[93m"
    RESET = "\033[0m"

    def __init__(self, colorido: bool = True) -> None:
        self.colorido = colorido and sys.stdout.isatty()

    def enviar(self, oc: Ocorrencia) -> None:
        hora = datetime.now().strftime("%H:%M:%S")
        texto = f"[{hora}] {oc.mensagem()}"
        if self.colorido:
            cor = self.VERMELHO if oc.estado.value.startswith("ERRO") else self.AMARELO
            texto = f"{cor}{texto}{self.RESET}"
        print(texto, flush=True)


# --------------------------------------------------------------------------- #
class CanalCSV(CanalAlerta):
    def __init__(self, caminho: str | Path = "logs/alertas.csv") -> None:
        caminho = Path(caminho)
        if not caminho.is_absolute():
            caminho = RAIZ / caminho
        caminho.parent.mkdir(parents=True, exist_ok=True)
        novo = not caminho.exists() or caminho.stat().st_size == 0
        self._arquivo = caminho.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._arquivo, fieldnames=CABECALHO_CSV)
        if novo:
            self._writer.writeheader()
            self._arquivo.flush()
        self.caminho = caminho

    def enviar(self, oc: Ocorrencia) -> None:
        linha: dict[str, Any] = {"timestamp": datetime.now().isoformat(timespec="seconds")}
        linha.update(oc.para_dict())
        self._writer.writerow({k: linha.get(k, "") for k in CABECALHO_CSV})
        self._arquivo.flush()

    def fechar(self) -> None:
        try:
            self._arquivo.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
class CanalSom(CanalAlerta):
    """Beep simples e nao bloqueante."""

    def __init__(self, intervalo_minimo: float = 1.0) -> None:
        self.intervalo_minimo = intervalo_minimo
        self._ultimo = 0.0

    def enviar(self, oc: Ocorrencia) -> None:
        import time

        agora = time.monotonic()
        if agora - self._ultimo < self.intervalo_minimo:
            return
        self._ultimo = agora
        threading.Thread(target=self._beep, daemon=True).start()

    @staticmethod
    def _beep() -> None:
        try:
            if sys.platform.startswith("win"):
                import winsound

                winsound.Beep(880, 250)
            else:
                sys.stdout.write("\a")
                sys.stdout.flush()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
class CanalWebhook(CanalAlerta):
    def __init__(self, url: str, timeout: float = 3.0) -> None:
        self.url = url
        self.timeout = timeout

    def enviar(self, oc: Ocorrencia) -> None:
        payload = {"timestamp": datetime.now().isoformat(timespec="seconds")}
        payload.update(oc.para_dict())
        threading.Thread(target=self._post, args=(payload,), daemon=True).start()

    def _post(self, payload: dict) -> None:
        try:
            import requests

            requests.post(self.url, json=payload, timeout=self.timeout)
        except Exception as exc:  # nao derruba a visao por causa de rede
            print(f"[webhook] falhou: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
class CanalGPIO(CanalAlerta):
    """Aciona um pino no Raspberry Pi (buzzer/LED) a cada alerta."""

    def __init__(self, pino: int = 18, duracao_ms: int = 500) -> None:
        self.pino = pino
        self.duracao_ms = duracao_ms
        self._gpio = None
        try:
            import RPi.GPIO as GPIO  # type: ignore

            GPIO.setmode(GPIO.BCM)
            GPIO.setup(pino, GPIO.OUT, initial=GPIO.LOW)
            self._gpio = GPIO
        except Exception as exc:  # pragma: no cover
            print(f"[gpio] indisponivel ({exc}); canal desativado", file=sys.stderr)

    def enviar(self, oc: Ocorrencia) -> None:
        if self._gpio is None:
            return
        threading.Thread(target=self._pulso, daemon=True).start()

    def _pulso(self) -> None:  # pragma: no cover
        import time

        try:
            self._gpio.output(self.pino, self._gpio.HIGH)
            time.sleep(self.duracao_ms / 1000.0)
            self._gpio.output(self.pino, self._gpio.LOW)
        except Exception:
            pass

    def fechar(self) -> None:  # pragma: no cover
        if self._gpio is not None:
            try:
                self._gpio.cleanup(self.pino)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
class Notificador:
    """Agrega varios canais."""

    def __init__(self, canais: list[CanalAlerta] | None = None) -> None:
        self.canais = canais or []

    def notificar(self, ocorrencias: list[Ocorrencia]) -> None:
        for oc in ocorrencias:
            for canal in self.canais:
                try:
                    canal.enviar(oc)
                except Exception as exc:
                    print(f"[alerta] canal {type(canal).__name__} falhou: {exc}",
                          file=sys.stderr)

    def fechar(self) -> None:
        for canal in self.canais:
            canal.fechar()

    # ------------------------------------------------------------------ #
    @classmethod
    def a_partir_dos_parametros(cls, parametros: dict) -> "Notificador":
        cfg = parametros.get("alertas", {})
        canais: list[CanalAlerta] = [CanalConsole()]

        if cfg.get("log_csv"):
            canais.append(CanalCSV(cfg["log_csv"]))
        if cfg.get("som", True):
            canais.append(CanalSom())

        web = parametros.get("webhook", {})
        if web.get("ativo") and web.get("url"):
            canais.append(CanalWebhook(web["url"], float(web.get("timeout_segundos", 3.0))))

        gpio = parametros.get("gpio", {})
        if gpio.get("ativo"):
            canais.append(CanalGPIO(int(gpio.get("pino", 18)), int(gpio.get("duracao_ms", 500))))

        return cls(canais)
