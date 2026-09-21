# -*- coding: utf-8 -*-
"""O transporte das placas falsas: uma linha JSON por mensagem, por `socket://`.

Por que socket e não `loop://` nem pseudo-terminal:

  * `loop://` é loopback — o que o adapter escreve volta para ele mesmo, então
    não há DUAS partes e o contrato não é exercitado (serve só para provar que
    abrir e escrever não estoura);
  * pseudo-terminal (`os.openpty`) não existe no Windows, e o desenvolvimento
    deste projeto é em Windows;
  * `socket://` é um handler de URL do próprio pyserial, aceito pelo MESMO
    `serial_for_url()` que abre `/dev/ttyUSB0` e `COM4`. Exercita o caminho de
    abertura de verdade, roda nos dois sistemas operacionais, e é a cara do
    plano B de execução (a ponte RFC2217 do CLAUDE.md).

O que esta classe implementa é só o que o firmware terá de implementar: ping,
ACK, `cmd_id` idempotente e eventos assíncronos. A regra de negócio de cada
subsistema fica na subclasse, em `eventos_para`.
"""
from __future__ import annotations

import json
import socket
import threading
import time


class PlacaFalsa:
    """Duplo de um firmware. Serve UM cliente por vez, como uma porta USB.

    `atraso_evento` é o intervalo entre o ACK e o evento de resultado: é o que
    separa "aceitei" de "terminei". Zero nos testes; qualquer coisa acima disso
    encena um `dispensar` demorado sem prender a suíte.
    """

    # Preenchidos pela subclasse.
    SUBSISTEMA: str = ""
    COMANDOS: dict[str, tuple[str, ...]] = {}

    # Comandos que existem SÓ no serial — a placa os atende, mas nenhum
    # simulador HTTP os implementa, então eles não entram em `_ROTAS_SIM` nem
    # na tabela de comandos do documento. Hoje é só a balança de bancada
    # (`docs/PROTOCOLO_SERIAL.md` §5, "Comandos de bancada"): com o transporte
    # serial ligado, o adapter é o dono da porta e ninguém mais abre o Monitor
    # Serial para configurar o peso unitário.
    COMANDOS_BANCADA: dict[str, tuple[str, ...]] = {}

    def __init__(self, atraso_evento: float = 0.0, intervalo_ping: float = 0.5,
                 host: str = "127.0.0.1"):
        self.atraso_evento = float(atraso_evento)
        self.intervalo_ping = float(intervalo_ping)

        # Gatilhos de encenação — cada um é um modo de falhar que o adapter
        # precisa distinguir do outro.
        self.mudo = False        # não responde ACK nenhum (placa travada)
        self.recusar = False     # responde ACK negativo (comando inválido)

        # O que a placa REALMENTE executou. É contra esta lista que se prova
        # que um `cmd_id` repetido não dispensou duas vezes.
        self.executados: list[dict] = []
        self.linhas_recebidas: list[str] = []
        self.pongs_recebidos = 0

        self._ultimo_cmd_id = 0
        self.sessao_adapter = None
        self._servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._servidor.bind((host, 0))
        self._servidor.listen(1)
        self._servidor.settimeout(0.2)
        self.host, self.porta = self._servidor.getsockname()

        self._cliente: socket.socket | None = None
        self._lock_escrita = threading.Lock()
        self._parar = threading.Event()
        self._threads: list[threading.Thread] = []

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return f"socket://{self.host}:{self.porta}"

    def iniciar(self) -> "PlacaFalsa":
        self._subir(self._aceitar, "placa-accept")
        self._subir(self._pingar, "placa-ping")
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
        """Fecha a conexão sem parar a placa — é o cabo sendo puxado.

        A placa continua aceitando conexão nova, que é o que uma placa faz: quem
        tem de reconectar é o adapter.
        """
        with self._lock_escrita:
            cliente, self._cliente = self._cliente, None
        if cliente is not None:
            try:
                cliente.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                cliente.close()
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

    def _subir(self, alvo, nome: str) -> None:
        thread = threading.Thread(target=alvo, name=nome, daemon=True)
        thread.start()
        self._threads.append(thread)

    # ── Socket ────────────────────────────────────────────────────────────────

    def _aceitar(self) -> None:
        while not self._parar.is_set():
            try:
                conexao, _ = self._servidor.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            conexao.settimeout(0.2)
            with self._lock_escrita:
                self._cliente = conexao
            # A placa se anuncia assim que a porta abre: é o ping do boot, e é
            # o que identifica esta porta para quem estiver varrendo.
            self._enviar({"cmd": "ping", "sub": self.SUBSISTEMA})
            self._atender(conexao)

    def _atender(self, conexao: socket.socket) -> None:
        buffer = bytearray()
        while not self._parar.is_set():
            try:
                dados = conexao.recv(4096)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            if not dados:
                break
            buffer.extend(dados)
            while True:
                corte = buffer.find(b"\n")
                if corte < 0:
                    break
                linha = bytes(buffer[:corte]).decode("utf-8", errors="replace")
                del buffer[: corte + 1]
                self._processar(linha.strip())
        with self._lock_escrita:
            if self._cliente is conexao:
                self._cliente = None

    def _enviar(self, objeto: dict) -> None:
        bruto = (json.dumps(objeto, ensure_ascii=False, separators=(",", ":"))
                 + "\n").encode("utf-8")
        with self._lock_escrita:
            cliente = self._cliente
            if cliente is None:
                return
            try:
                cliente.sendall(bruto)
            except OSError:
                pass

    def _pingar(self) -> None:
        while not self._parar.wait(self.intervalo_ping):
            if self._cliente is not None:
                self._enviar({"cmd": "ping", "sub": self.SUBSISTEMA})

    # ── Protocolo ─────────────────────────────────────────────────────────────

    def _processar(self, linha: str) -> None:
        if not linha:
            return
        self.linhas_recebidas.append(linha)
        inicio = linha.find("{")
        if inicio < 0:
            return
        try:
            mensagem = json.loads(linha[inicio:])
        except ValueError:
            return
        if mensagem.get("resp") == "pong":
            self.pongs_recebidos += 1
            self._adotar_sessao(mensagem)
            return
        cmd = mensagem.get("cmd")
        if cmd:
            self._executar(cmd, mensagem)

    def _adotar_sessao(self, mensagem: dict) -> None:
        """Sessão nova do adapter zera o contador de idempotência.

        O `cmd_id` é monotônico DENTRO de uma sessão do adapter, e um restart do
        processo o faz nascer em 1. A placa guarda o último executado e ignora
        id menor ou igual — certo contra reenvio, errado contra RESTART: todo
        comando até o último id recebia ACK positivo SEM EXECUTAR, e com o ciclo
        por relógio o central dispara o `dispensar` de um `mover` que a mesa
        nunca fez.

        Comparação por DIFERENÇA e não por ordem: relógio do host que ande para
        trás continua sendo uma sessão nova. Zero (ou ausente) significa "ainda
        não sei", e a primeira sessão vista é adotada sem zerar nada — senão
        todo boot da placa descartaria o primeiro comando legítimo.
        """
        sessao = mensagem.get("sessao")
        if not isinstance(sessao, int) or sessao <= 0:
            return
        if self.sessao_adapter is not None and sessao != self.sessao_adapter:
            self._ultimo_cmd_id = 0
        self.sessao_adapter = sessao

    def _executar(self, cmd: str, mensagem: dict) -> None:
        if self.mudo:
            return  # placa travada: nem ACK, nem evento
        # ANTES da checagem de idempotência, e a ordem é o ponto: um comando da
        # sessão nova tem de ser executado, não respondido como repetido. O pong
        # também a carrega, mas o primeiro comando depois do restart pode chegar
        # antes do primeiro pong.
        self._adotar_sessao(mensagem)
        cmd_id = mensagem.get("cmd_id")

        aceitos = {**self.COMANDOS, **self.COMANDOS_BANCADA}
        if cmd not in aceitos:
            self._enviar({"resp": "erro", "cmd_id": cmd_id,
                          "msg": f"comando desconhecido: {cmd}"})
            return

        faltando = [c for c in aceitos[cmd] if c not in mensagem]
        if faltando:
            self._enviar({"resp": "erro", "cmd_id": cmd_id,
                          "msg": f"campos ausentes: {','.join(faltando)}"})
            return

        # Idempotência: o último `cmd_id` executado é guardado, e um id repetido
        # (ou anterior) responde ACK DE NOVO sem executar. É a defesa contra o
        # reenvio que um ACK perdido provoca — reenviar `dispensar` seria dose
        # dobrada no leito.
        if isinstance(cmd_id, int) and cmd_id <= self._ultimo_cmd_id:
            self._enviar({"resp": "ok", "cmd_id": cmd_id, "repetido": True})
            return

        if self.recusar:
            self._enviar({"resp": "erro", "cmd_id": cmd_id,
                          "msg": "recusado pela placa (encenação)"})
            return

        if isinstance(cmd_id, int):
            self._ultimo_cmd_id = cmd_id
        # `sessao` fora do payload: ela é do TRANSPORTE, como `cmd_id`. Deixá-la
        # entrar faria `test_protocolo_placas.py` cobrá-la como campo de
        # comando do contrato de cada subsistema, que ela não é.
        campos = {k: v for k, v in mensagem.items()
                  if k not in ("cmd", "cmd_id", "sessao")}
        self.executados.append({"cmd": cmd, "cmd_id": cmd_id, **campos})

        # ACK primeiro: ele diz "aceitei", e o orquestrador continua esperando o
        # evento de resultado, que é o que diz "terminei".
        self._enviar({"resp": "ok", "cmd_id": cmd_id})

        eventos = self.eventos_para(cmd, campos)
        if not eventos:
            return
        if self.atraso_evento <= 0:
            for evento in eventos:
                self.emitir(evento)
            return
        self._subir(lambda: self._emitir_depois(eventos), f"placa-evt-{cmd_id}")

    def _emitir_depois(self, eventos: list[dict]) -> None:
        if self._parar.wait(self.atraso_evento):
            return
        for evento in eventos:
            self.emitir(evento)

    def emitir(self, payload: dict) -> None:
        """Empurra um evento sem ter sido perguntada (telemetria, resultado)."""
        self._enviar({"evento": payload})

    def emitir_grudado(self, payload: dict, prefixo: str) -> None:
        """Log humano e JSON na MESMA linha, como o firmware de verdade escreve.

        O firmware imprime as duas vozes no mesmo Serial e nada garante que
        caiam em linhas separadas — `Tara c0: offset=8412{"evento":{...}}` é o
        que sai de verdade. Exigir que a linha comece com '{' descartaria
        justamente os primeiros eventos do boot; é o caso que `extrair_json`
        existe para cobrir, e é este método que o encena.
        """
        linha = json.dumps({"evento": payload}, ensure_ascii=False,
                           separators=(",", ":"))
        self.enviar_bruto(prefixo + linha + "\n")

    def enviar_bruto(self, texto: str) -> None:
        """Escreve texto cru na linha, sem enquadrar nada.

        É como se encena o que o firmware realmente faz: log e JSON no mesmo
        Serial, saindo grudados (`Iniciando...{"evento":{...}}`), linha só de
        log, e linha maior que o buffer.
        """
        with self._lock_escrita:
            cliente = self._cliente
            if cliente is None:
                return
            try:
                cliente.sendall(texto.encode("utf-8"))
            except OSError:
                pass

    # ── A subclasse diz o que cada comando produz ─────────────────────────────

    def eventos_para(self, cmd: str, campos: dict) -> list[dict]:
        raise NotImplementedError


def agora() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
