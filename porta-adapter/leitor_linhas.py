"""
Leitor só-leitura de UMA porta serial — a USB do ESP receptor da porta NR-12.

Uma thread por porta, que abre a COM, lê linha a linha e entrega cada linha a
um callback. **Não tem método de escrita nenhum**, e isso é o contrato: em
campo a porta decide a tag sozinha e o PC só escuta (`docs/PROTOCOLO_SERIAL.md`
§10). Um `R` que descesse por engano poria a porta em modo registro; um `T`
mandaria lixo ao Serial2 dela. `tests/test_porta_adapter.py` varre o
porta-adapter por AST atrás de `write`/`flush`/`enviar_comando`/`send`.

Quatro regras, as mesmas do `serial_link.py` dos outros adapters:

  * **DTR e RTS desligados ANTES do `open`.** Em ESP32 esses dois sinais são o
    circuito de reset: abrir a COM do jeito padrão do pyserial reinicia o
    receptor, e cada reconexão viraria um boot;
  * **teto de 1024 bytes por linha.** Linha maior é descartada INTEIRA e
    contada — nunca partida em duas, porque meia linha `EVT:` seria lida como
    um código que a porta não mandou;
  * **reconexão para sempre.** Cabo solto e receptor reiniciado são rotina; só
    a TRANSIÇÃO vira linha de log;
  * **não conhece asyncio.** É de threads; o salto para o event loop
    (`run_coroutine_threadsafe`) é do `main.py`.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

try:
    import serial
except ImportError:  # pragma: no cover — o requirements.txt traz o pyserial
    serial = None

logger = logging.getLogger(__name__)

MAX_LINHA_BYTES = 1024
LEITURA_TIMEOUT_S = 0.1
RECONEXAO_S = 2.0


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LeitorLinhas:
    """Lê a COM em `url` e chama `ao_receber_linha(texto)` por linha completa.

    O callback roda NA THREAD LEITORA. Exceção nele é logada e a leitura segue:
    a thread é única, e uma linha estranha não pode deixá-la morta com a porta
    aberta.
    """

    def __init__(self, url: str, ao_receber_linha: Callable[[str], None],
                 baud: int = 115200, reconexao_s: float = RECONEXAO_S,
                 nome: str = "porta"):
        self.url = url
        self.baud = int(baud)
        self.nome = nome
        self._ao_receber = ao_receber_linha
        self._reconexao_s = float(reconexao_s)
        self._conn = None
        self._thread: Optional[threading.Thread] = None
        self._parar = threading.Event()
        self._buffer = bytearray()
        self._descartando = False
        self._conectado_desde: Optional[float] = None
        self._ultima_linha_em: Optional[float] = None
        self._linhas = 0
        self._descartadas = 0
        self._aviso_fechada = False
        # Quantas vezes a COM abriu. Uma queda e volta mais rápida que a vigia
        # do `main.py` não pode passar despercebida: o que se sabia da porta
        # é da conexão ANTERIOR, e é este número que denuncia a troca.
        self.conexoes = 0

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    def iniciar(self) -> None:
        """Sobe a thread. Não bloqueia e não levanta por porta ausente: o
        adapter tem de subir assim mesmo para o `/ping` responder."""
        if serial is None:
            raise RuntimeError("pyserial não está instalado (porta-adapter/requirements.txt)")
        if self._thread is not None:
            return
        self._parar.clear()
        self._thread = threading.Thread(target=self._laco, name=f"leitor-{self.nome}",
                                        daemon=True)
        self._thread.start()

    def parar(self, timeout: float = 3.0) -> None:
        self._parar.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
        self._fechar()

    @property
    def conectado(self) -> bool:
        return self._conn is not None

    def estado(self) -> dict:
        return {
            "url": self.url,
            "baud": self.baud,
            "conectado": self.conectado,
            "conexoes": self.conexoes,
            "desde": _iso(self._conectado_desde),
            "ultima_linha": _iso(self._ultima_linha_em),
            "linhas": self._linhas,
            "descartadas": self._descartadas,
        }

    # ── Laço ──────────────────────────────────────────────────────────────────

    def _laco(self) -> None:
        while not self._parar.is_set():
            if self._conn is None:
                conn = self._abrir()
                if conn is None:
                    self._parar.wait(self._reconexao_s)
                    continue
                self._conn = conn
                self.conexoes += 1
                self._buffer.clear()
                self._descartando = False
                self._conectado_desde = time.time()
                self._aviso_fechada = False
                logger.info("[%s] USB %s aberta (só leitura).", self.nome, self.url)

            conn = self._conn
            if conn is None:
                continue
            try:
                dados = conn.read(max(1, getattr(conn, "in_waiting", 0) or 1))
            except Exception as exc:  # noqa: BLE001
                self._desconectar(f"erro de leitura: {exc}")
                continue
            if not dados:
                continue
            for linha in self._consumir(dados):
                if not linha:
                    continue
                self._linhas += 1
                self._ultima_linha_em = time.time()
                try:
                    self._ao_receber(linha)
                except Exception:  # noqa: BLE001
                    logger.exception("[%s] falha tratando a linha %r", self.nome, linha)

    def _consumir(self, dados: bytes) -> list[str]:
        """Acumula bytes e devolve as linhas COMPLETAS — o `read()` devolve o
        que estava no buffer do SO, não uma linha."""
        linhas: list[str] = []
        self._buffer.extend(dados)
        while True:
            corte = self._buffer.find(b"\n")
            if corte < 0:
                if len(self._buffer) > MAX_LINHA_BYTES:
                    self._descartar()
                    del self._buffer[:]
                    self._descartando = True
                return linhas
            crua = bytes(self._buffer[:corte])
            del self._buffer[: corte + 1]
            if self._descartando:
                self._descartando = False      # o rabo da linha já descartada
                continue
            if len(crua) > MAX_LINHA_BYTES:
                self._descartar()
                continue
            linhas.append(crua.decode("ascii", errors="replace").rstrip("\r"))

    def _descartar(self) -> None:
        self._descartadas += 1
        logger.warning("[%s] linha acima de %d bytes descartada inteira.",
                       self.nome, MAX_LINHA_BYTES)

    # ── Conexão ───────────────────────────────────────────────────────────────

    def _abrir(self):
        try:
            conn = serial.serial_for_url(self.url, do_not_open=True)
            conn.baudrate = self.baud
            conn.timeout = LEITURA_TIMEOUT_S
            # DTR/RTS desligados ANTES de abrir: no ESP32 eles são o reset.
            conn.dtr = False
            conn.rts = False
            conn.open()
            return conn
        except Exception as exc:  # noqa: BLE001
            if not self._aviso_fechada:
                self._aviso_fechada = True
                logger.warning(
                    "[%s] a COM %s NÃO abriu (%s). Confira o número da COM do "
                    "RECEPTOR no Gerenciador de Dispositivos, o cabo de DADOS e "
                    "se nenhum outro processo a segura (o Monitor Serial é o "
                    "suspeito nº 1). Tentando de novo a cada %.0fs.",
                    self.nome, self.url, exc, self._reconexao_s)
            return None

    def _desconectar(self, motivo: str) -> None:
        logger.warning("[%s] USB %s caiu — %s.", self.nome, self.url, motivo)
        self._fechar()

    def _fechar(self) -> None:
        conn, self._conn = self._conn, None
        self._conectado_desde = None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
