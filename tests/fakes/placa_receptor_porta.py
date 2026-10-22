# -*- coding: utf-8 -*-
"""Receptor falso da porta NR-12 em campo — a USB que o `porta-adapter` lê (§10).

Em campo são duas ESPs: a da porta (`porta/nr12_oficial/nr12_oficial.ino`), que
decide a tag sozinha e escreve linhas de TEXTO no Serial2, e o receptor
(`porta/receptor_esp32/receptor_esp32.ino`), que as repassa IDÊNTICAS na USB.
Esta classe é as duas de uma vez, vistas pelo lado da USB: um servidor TCP em
localhost atendendo `socket://`, como as outras placas falsas — o caminho de
abertura exercitado é o de verdade (`serial_for_url`), o que muda é o que está
do outro lado do fio.

Ela ENCENA a máquina de estados do `nr12_oficial.ino` e escreve as linhas no
formato dele: `EVT:<CODIGO>[;UID=<HEX>] | <texto>` e, depois de cada alerta, a
linha dos fins de curso `   ES1(D4)=… ES2(D18)=…`. E as diferenças do firmware
importam tanto quanto as regras:

  * a tag cadastrada autoriza SOZINHA (`PAUSA`) — ninguém manda `porta_liberar`;
  * a segunda porta que abre com a primeira já aberta não gera linha nenhuma;
  * fechar não religa (`PORTA_FECHADA` → rearme pendente).

Encenação:

    placa.ligar_receptor()        -> o banner do receptor
    placa.ligar_porta()           -> BOOT_OK (ou ALERTA_SEM_AUTORIZACAO) + linha ES
    placa.passar_tag(uid)         -> PAUSA, REARME_OK ou TAG_NEGADA
    placa.abrir("frente")         -> ALERTA_* + linha ES (só na primeira porta)
    placa.fechar("frente")        -> PORTA_FECHADA (quando as duas fecham)
    placa.expirar()               -> LIBERACAO_EXPIRADA (a janela de 10 s venceu)
    placa.linha_crua(texto)       -> qualquer texto na USB
    placa.derrubar_cabo()         -> a USB cai

Sem ninguém conectado, as linhas se perdem — que é o que acontece com a USB do
receptor fechada: a porta segue a máquina de estados dela, e o PC não ouve.

**Conta todo byte que RECEBER** (`bytes_recebidos`): o PC só lê, e a suíte
exige zero.
"""
from __future__ import annotations

import socket
import threading
import time

ESTADOS = ("FECHADA", "LIBERADA", "ABERTA_AUTORIZADA", "ABERTA_SEM_AUTORIZACAO")

BANNER_RECEPTOR = (
    "",
    "=== RECEPTOR Serial2 | segundo ESP32 ===",
    "Serial2: RX=GPIO16 (pull-up ON)  TX=GPIO17  baud=115200",
    "Aguardando mensagens do ESP32 da porta...",
    "",
    "Comandos: X (hex on/off) | F (filtro de ruido on/off) | T (enviar teste p/ a "
    "porta) | S (estatisticas) | H (ajuda)",
)


class PlacaReceptorPorta:
    def __init__(self, tags=("04A1B2C3",), teto_fechada: bool = True,
                 frente_fechada: bool = True, host: str = "127.0.0.1"):
        self.tags = set(tags)
        self.teto_fechada = teto_fechada
        self.frente_fechada = frente_fechada
        self.estado_atual = "FECHADA"
        self.aguardando_rearme = False
        self.led_pode_abrir = False
        self.bytes_recebidos = 0
        self.linhas_enviadas: list[str] = []

        self._servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._servidor.bind((host, 0))
        self._servidor.listen(1)
        self._servidor.settimeout(0.2)
        self.host, self.porta = self._servidor.getsockname()
        self._cliente: socket.socket | None = None
        self._lock = threading.Lock()
        self._parar = threading.Event()
        self._threads: list[threading.Thread] = []

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return f"socket://{self.host}:{self.porta}"

    def iniciar(self) -> "PlacaReceptorPorta":
        thread = threading.Thread(target=self._aceitar, name="receptor-accept", daemon=True)
        thread.start()
        self._threads.append(thread)
        return self

    def parar(self) -> None:
        self._parar.set()
        self.derrubar_cabo()
        try:
            self._servidor.close()
        except OSError:
            pass
        for thread in list(self._threads):
            thread.join(2.0)

    def derrubar_cabo(self) -> None:
        """A USB do receptor cai. As duas ESPs seguem ligadas."""
        with self._lock:
            cliente, self._cliente = self._cliente, None
        if cliente is not None:
            for fechar in (lambda: cliente.shutdown(socket.SHUT_RDWR), cliente.close):
                try:
                    fechar()
                except OSError:
                    pass

    @property
    def conectada(self) -> bool:
        return self._cliente is not None

    def esperar_conexao(self, timeout: float = 5.0) -> bool:
        limite = time.time() + timeout
        while time.time() < limite:
            if self._cliente is not None:
                return True
            time.sleep(0.01)
        return False

    def _aceitar(self) -> None:
        while not self._parar.is_set():
            try:
                conexao, _ = self._servidor.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            conexao.settimeout(0.2)
            with self._lock:
                self._cliente = conexao
            self._ouvir(conexao)

    def _ouvir(self, conexao: socket.socket) -> None:
        """O PC não deveria escrever nada. Se escrever, fica contado."""
        while not self._parar.is_set():
            try:
                dados = conexao.recv(4096)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            if not dados:
                break
            self.bytes_recebidos += len(dados)
        with self._lock:
            if self._cliente is conexao:
                self._cliente = None

    # ── O fio ─────────────────────────────────────────────────────────────────

    def linha_crua(self, texto: str) -> None:
        """Uma linha na USB, como o `Serial.println` do receptor a escreve."""
        self._escrever([texto])

    def _escrever(self, linhas: list[str]) -> list[str]:
        bruto = "".join(linha + "\r\n" for linha in linhas).encode("ascii", "replace")
        with self._lock:
            cliente = self._cliente
            if cliente is not None:
                try:
                    cliente.sendall(bruto)
                    self.linhas_enviadas.extend(linhas)
                except OSError:
                    pass
        return linhas

    @staticmethod
    def _evt(codigo: str, texto: str, uid: str = "") -> str:
        """`evento()` do nr12_oficial, byte a byte."""
        return "EVT:" + codigo + (";UID=" + uid if uid else "") + " | " + texto

    def _sensores(self) -> str:
        """`imprimirSensores()` do nr12_oficial."""
        return ("   ES1(D4)=" + ("FECHADO  " if self.teto_fechada else "ABERTO  ")
                + "ES2(D18)=" + ("FECHADO  " if self.frente_fechada else "ABERTO  "))

    # ── O que a bancada faz ───────────────────────────────────────────────────

    @property
    def fechada(self) -> bool:
        return self.teto_fechada and self.frente_fechada

    def ligar_receptor(self) -> list[str]:
        return self._escrever(list(BANNER_RECEPTOR))

    def ligar_porta(self) -> list[str]:
        self.aguardando_rearme = False
        self.led_pode_abrir = False
        if self.fechada:
            self.estado_atual = "FECHADA"
            linhas = [self._evt("BOOT_OK", "Porta fechada. Sistema armado.")]
        else:
            self.estado_atual = "ABERTA_SEM_AUTORIZACAO"
            linhas = [self._evt("ALERTA_SEM_AUTORIZACAO",
                                "Porta aberta no boot! Maquina bloqueada.")]
        return self._escrever(linhas + [self._sensores()])

    def passar_tag(self, uid: str) -> list[str]:
        if uid not in self.tags:
            return self._escrever([self._evt(
                "TAG_NEGADA", "Tag nao cadastrada. Envie 'R' para registrar.", uid)])
        if self.estado_atual != "FECHADA" or not self.fechada:
            return []
        if self.aguardando_rearme:
            self.aguardando_rearme = False
            return self._escrever([self._evt(
                "REARME_OK", "Rearme confirmado. Maquina pode retomar.", uid)])
        self.estado_atual = "LIBERADA"
        self.led_pode_abrir = True
        return self._escrever([self._evt(
            "PAUSA", "Acesso autorizado. Maquina em pausa. Pode abrir.", uid)])

    def abrir(self, porta: str) -> list[str]:
        assert porta in ("teto", "frente"), porta
        if porta == "teto":
            self.teto_fechada = False
        else:
            self.frente_fechada = False
        if self.estado_atual == "FECHADA":
            self.estado_atual = "ABERTA_SEM_AUTORIZACAO"
            return self._escrever([self._evt(
                "ALERTA_SEM_AUTORIZACAO",
                "PORTA ABERTA SEM AUTORIZACAO! Parada de emergencia."), self._sensores()])
        if self.estado_atual == "LIBERADA":
            self.estado_atual = "ABERTA_AUTORIZADA"
            return self._escrever([self._evt(
                "ALERTA_PORTA_ABERTA", "ALERTA: MAQUINA PAUSADA - PORTA ABERTA."),
                self._sensores()])
        return []           # a segunda porta, com a primeira já aberta: silêncio

    def fechar(self, porta: str) -> list[str]:
        if porta == "teto":
            self.teto_fechada = True
        else:
            self.frente_fechada = True
        if self.fechada and self.estado_atual in ("ABERTA_AUTORIZADA",
                                                  "ABERTA_SEM_AUTORIZACAO"):
            self.estado_atual = "FECHADA"
            self.led_pode_abrir = False
            self.aguardando_rearme = True        # NR-12: fechar NÃO religa
            return self._escrever([self._evt(
                "PORTA_FECHADA", "Porta fechada. Aguardando rearme (passe a tag).")])
        return []

    def expirar(self) -> list[str]:
        """A janela de 10 s da liberação venceu sem ninguém abrir."""
        if self.estado_atual != "LIBERADA" or not self.fechada:
            return []
        self.estado_atual = "FECHADA"
        self.led_pode_abrir = False
        self.aguardando_rearme = True
        return self._escrever([self._evt(
            "LIBERACAO_EXPIRADA", "Porta nao foi aberta. Passe a tag para rearmar.")])
