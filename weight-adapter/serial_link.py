# -*- coding: utf-8 -*-
"""Transporte serial dos adapters — uma linha JSON por mensagem, um dono por porta.

ESTE ARQUIVO É UMA CÓPIA IDÊNTICA nos três adapters que falam com firmware:
`cnc-adapter/`, `dispenser-adapter/` e `weight-adapter/`.
`tests/test_serial_link.py` compara os três byte a byte e reprova se
divergirem — a mesma forma de `tests/test_adapters.py` com o `_post_central`.

Por que cópia e não um `shared/`: cada adapter tem o seu próprio contexto de
build no compose (`build: ./cnc-adapter`), e um diretório compartilhado
obrigaria os três a virarem `context: .` + `dockerfile:`, arrastando o
repositório inteiro para dentro das três imagens e passando a exigir um
`.dockerignore`. O CLAUDE.md já registra essa avaliação para os templates de OS
— com a diferença de que lá a alternativa era um GET, e aqui não existe GET que
resolva: este código roda ANTES de haver qualquer rede. Se um dia existirem
cinco cópias, a conta muda.

O que este módulo NÃO faz, de propósito:

  * não é async, e não importa asyncio. `Serial.read()` e `Serial.write()` são
    bloqueantes; se o event loop do FastAPI encostasse neles, uma leitura
    pendurada congelaria o adapter inteiro — inclusive o `/ping` que o
    healthcheck do compose consulta como portão de subida. A leitura mora numa
    thread dedicada; quem chama `enviar_comando` do lado async é obrigado a
    passar por `asyncio.to_thread`, e há teste de AST cobrando isso;
  * não retenta comando nenhum. Reenviar um `dispensar` é dose dobrada no
    leito, e é a razão de existir o `cmd_id` (ver `enviar_comando`);
  * não interpreta payload de evento. O evento atravessa como veio, do mesmo
    jeito que `injetar_falha` e as duas quantidades da pesagem atravessam hoje.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

try:  # pyserial só é necessário onde o transporte é serial.
    import serial
    from serial.tools import list_ports as _list_ports
except Exception:  # noqa: BLE001 — ImportError, e também o duplo da suíte
    serial = None       # type: ignore[assignment]
    _list_ports = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ── Tetos e cadências ─────────────────────────────────────────────────────────

# Teto de UMA linha, nos dois sentidos. O firmware lê para um buffer fixo e
# DESCARTA a linha que não couber, sem erro — então o limite tem que sair da
# ORIGEM do dado, e não do tamanho que ele tem hoje. A maior mensagem do
# contrato é o evento `peso_ok` (~450 B com todos os campos); 1024 deixa mais de
# 2x de folga, e é esse o buffer que o firmware tem que declarar.
MAX_LINHA_BYTES = 1024

# Timeout do `read()`: é a granularidade com que a thread leitora acorda para
# conferir o pedido de parada. Não é timeout de mensagem nem de ACK.
LEITURA_TIMEOUT_S = 0.5

# Espera entre tentativas de (re)conexão. Cabo solto, placa reiniciada e ponte
# TCP caída são rotina: o laço tenta para sempre e loga só a TRANSIÇÃO.
RECONEXAO_S = 3.0

# Sondagem de porta (só quando a URL vem vazia): quanto se espera o ping que a
# placa emite sozinha, e quanto se deixa a porta assentar depois de abrir.
PROBE_ESPERA_S = 8.0
PROBE_ASSENTAR_S = 1.5

# Tipos de evento que a placa emite PERIODICAMENTE. Repetição idêntica destes é
# descartada aqui em vez de encaminhada ao central — é a mesma regra que o
# central já aplica no broadcast do WebSocket, e aqui ela tem um segundo motivo:
# num canal de 115200 baud, despejo periódico compete com os ACKs que este mesmo
# adapter está esperando, ou seja, com o caminho crítico da OS.
TIPOS_PERIODICOS = frozenset({"telemetria", "status", "movendo", "retornando"})

# Campos que mudam a cada emissão e não contam como "mudou alguma coisa".
CAMPOS_VOLATEIS = frozenset({"ts"})

# Marcador interno: não é `resp` de ninguém — é este lado dizendo ao comando em
# voo que a porta caiu debaixo dele. Nunca sai nem entra pela linha, e por isso
# não colide com uma resposta da placa.
_RESP_PORTA_CAIU = "__porta_caiu__"


# ── Exceções ──────────────────────────────────────────────────────────────────
#
# Os três casos existem separados porque viram status HTTP diferentes no
# adapter, e o orquestrador precisa ver exatamente o que o transporte HTTP já
# lhe mostra hoje: recusa da ponta de lá é 502, ponta de lá inalcançável é 503.

class ErroLink(Exception):
    """Base — qualquer falha do transporte serial."""


class Desconectado(ErroLink):
    """A porta não está aberta. Vira 503, como simulador inalcançável."""


class SemAck(ErroLink):
    """A placa não confirmou dentro do prazo. Vira 503, pelo mesmo motivo."""


class AckNegativo(ErroLink):
    """A placa RECUSOU o comando. Vira 502, como simulador respondendo >= 300."""


# ── Framing ───────────────────────────────────────────────────────────────────

def extrair_json(bruto: str) -> Optional[dict]:
    """Extrai o objeto JSON de uma linha da placa, mesmo grudado em log.

    O firmware imprime texto e JSON no MESMO Serial, e nada garante que caiam em
    linhas separadas — no boot sai literalmente:

        Iniciando HX711...{"cmd":"ping","sub":"weight"}

    Exigir que a linha COMECE com '{' descarta justamente os pings do boot, que
    são os primeiros a chegar e são o que identifica a porta. A detecção passaria
    a depender dos pings avulsos posteriores, dentro de uma janela apertada, e
    falharia de forma intermitente com nada no log explicando por quê.

    Copiada de `painel_operador/backend/app.py`, onde o sintoma já aconteceu com
    o display. Devolve None quando não há JSON válido na linha — e linha sem
    JSON é log, nunca exceção.
    """
    inicio = bruto.find("{")
    if inicio < 0:
        return None
    try:
        analisado = json.loads(bruto[inicio:])
    except (ValueError, TypeError):
        return None
    return analisado if isinstance(analisado, dict) else None


class _Espera:
    """Um comando em voo, esperando o ACK da placa."""

    __slots__ = ("evento", "resposta")

    def __init__(self) -> None:
        self.evento = threading.Event()
        self.resposta: Optional[dict] = None


class LinkSerial:
    """Dono ÚNICO de uma porta serial.

    Um processo abre a porta e mais ninguém: duas threads escrevendo intercalam
    bytes no meio de uma linha, e o outro lado descarta a linha inteira como
    JSON inválido. Por isso toda escrita passa por `_escrever`, que segura
    `_lock_escrita` — nunca se escreve no objeto `Serial` direto.

    `ao_receber_evento` é chamado NA THREAD LEITORA, com o payload cru do
    evento. Quem quiser levá-lo para o event loop que faça o salto do lado de
    lá (`asyncio.run_coroutine_threadsafe`); este módulo não conhece asyncio.
    """

    def __init__(
        self,
        subsistema: str,
        url: str = "",
        baud: int = 115200,
        ack_timeout_s: float = 2.0,
        ao_receber_evento: Optional[Callable[[dict], None]] = None,
        probe_espera_s: float = PROBE_ESPERA_S,
        probe_assentar_s: float = PROBE_ASSENTAR_S,
        reconexao_s: float = RECONEXAO_S,
    ) -> None:
        self.subsistema = subsistema
        self.url = (url or "").strip()
        self.baud = int(baud)
        self.ack_timeout_s = float(ack_timeout_s)
        self._ao_receber_evento = ao_receber_evento
        self._probe_espera_s = float(probe_espera_s)
        self._probe_assentar_s = float(probe_assentar_s)
        self._reconexao_s = float(reconexao_s)

        self._conn = None
        self._lock_escrita = threading.Lock()
        self._lock_espera = threading.Lock()
        self._aguardando: dict[int, _Espera] = {}
        self._proximo_id = 0
        self._parar = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._buffer = bytearray()
        self._descartando = False

        self._url_aberta = ""
        self._conectado_desde: Optional[float] = None
        self._ultimo_ping_placa: Optional[float] = None
        self._linhas_truncadas = 0
        self._eventos_descartados = 0
        self._sub_divergente_avisado = False
        self._ultima_periodica: dict[tuple, str] = {}

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    def iniciar(self) -> None:
        """Sobe a thread leitora.

        Não bloqueia e não levanta por porta ausente: placa fora do ar é rotina,
        e o adapter tem que subir assim mesmo para o `/ping` do healthcheck
        responder. Quem conta a verdade sobre a placa é o `/health`.
        """
        if serial is None:
            raise ErroLink(
                "pyserial não está instalado — o transporte serial precisa dele "
                "(o transporte 'http' não)."
            )
        if self._thread is not None:
            return
        self._parar.clear()
        self._thread = threading.Thread(
            target=self._laco, name=f"serial-{self.subsistema}", daemon=True
        )
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
        """O que o `/health` do adapter publica sobre a placa.

        O `/ping` NÃO consulta nada disto: ele responde por ESTE processo.
        Atrelar o portão do compose ao hardware faria um cabo solto marcar o
        serviço como unhealthy e derrubar em cascata quem depende dele.
        """
        return {
            "subsistema": self.subsistema,
            "url_configurada": self.url or "(varredura)",
            "url_aberta": self._url_aberta,
            "baud": self.baud,
            "conectado": self.conectado,
            "conectado_desde": _iso(self._conectado_desde),
            "ultimo_ping_placa": _iso(self._ultimo_ping_placa),
            "ack_timeout_s": self.ack_timeout_s,
            "linhas_truncadas": self._linhas_truncadas,
            "eventos_periodicos_descartados": self._eventos_descartados,
        }

    # ── Envio de comando ──────────────────────────────────────────────────────

    def enviar_comando(self, nome: str, campos: dict,
                       ack_timeout_s: Optional[float] = None) -> dict:
        """Escreve o comando e BLOQUEIA até o ACK. Nunca reenvia.

        O ACK diz que a placa ACEITOU o comando, não que o executou: um `mover`
        leva segundos e um `dispensar` leva mais. O resultado chega depois, como
        evento assíncrono, e é ele que o orquestrador espera em
        `aguardar_evento`. Por isso o prazo aqui é curto (2 s) e o timeout de
        CONCLUSÃO continua onde sempre esteve, no orquestrador.

        Todo comando leva um `cmd_id` monotônico por porta, e a placa guarda o
        último executado por subsistema: reenviar é a reação natural a um ACK
        perdido, e reenviar `dispensar` é dose dobrada no leito. O reenvio não
        acontece aqui — nem por engano, nem por configuração — mas o `cmd_id`
        existe para que a ponta de lá sobreviva a um reenvio de qualquer origem.
        É a parte do protocolo que não dá para acrescentar depois sem trocar as
        duas pontas ao mesmo tempo.
        """
        if not self.conectado:
            raise Desconectado(
                f"{self.subsistema}: porta desconectada — comando '{nome}' "
                f"recusado na hora"
            )

        with self._lock_espera:
            self._proximo_id += 1
            cmd_id = self._proximo_id
            espera = _Espera()
            self._aguardando[cmd_id] = espera

        prazo = self.ack_timeout_s if ack_timeout_s is None else float(ack_timeout_s)
        try:
            self._escrever({"cmd": nome, "cmd_id": cmd_id, **campos})
            if not espera.evento.wait(prazo):
                raise SemAck(
                    f"{self.subsistema}: placa não confirmou '{nome}' "
                    f"(cmd_id={cmd_id}) em {prazo:.1f}s"
                )
            resposta = espera.resposta or {}
            # A porta caiu com o comando em voo: é ponta de lá INALCANÇÁVEL
            # (503), não recusa dela (502). Quem lê o log precisa da diferença —
            # uma manda olhar o cabo, a outra manda olhar o comando.
            if resposta.get("resp") == _RESP_PORTA_CAIU:
                raise Desconectado(
                    f"{self.subsistema}: porta caiu durante '{nome}' "
                    f"(cmd_id={cmd_id}): {resposta.get('msg')}"
                )
            if resposta.get("resp") != "ok":
                raise AckNegativo(
                    f"{self.subsistema}: placa recusou '{nome}' (cmd_id={cmd_id}): "
                    f"{resposta.get('msg') or 'sem motivo'}"
                )
            return resposta
        finally:
            with self._lock_espera:
                self._aguardando.pop(cmd_id, None)

    # ── Escrita ───────────────────────────────────────────────────────────────

    def _escrever(self, objeto: dict) -> None:
        linha = json.dumps(objeto, ensure_ascii=False, separators=(",", ":")) + "\n"
        bruto = linha.encode("utf-8")
        # Linha que SAI não é truncada: comando truncado é JSON inválido, a placa
        # o descarta e o adapter fica esperando um ACK que nunca vem. Falhar aqui
        # aponta para o comando; truncar apontaria para a placa.
        if len(bruto) > MAX_LINHA_BYTES:
            raise ErroLink(
                f"{self.subsistema}: comando '{objeto.get('cmd')}' tem "
                f"{len(bruto)} bytes e o teto da linha é {MAX_LINHA_BYTES}"
            )
        with self._lock_escrita:
            conn = self._conn
            if conn is None:
                raise Desconectado(
                    f"{self.subsistema}: porta fechada durante a escrita"
                )
            try:
                conn.write(bruto)
                conn.flush()
            except Exception as exc:  # noqa: BLE001 — SerialException, OSError...
                raise Desconectado(
                    f"{self.subsistema}: falha ao escrever na porta: {exc}"
                )

    # ── Laço leitor + reconexão ───────────────────────────────────────────────

    def _laco(self) -> None:
        while not self._parar.is_set():
            if self._conn is None:
                conn = self._conectar()
                if conn is None:
                    self._parar.wait(self._reconexao_s)
                    continue
                self._marcar_conectado(conn)

            conn = self._conn
            if conn is None:
                continue
            try:
                dados = conn.read(max(1, getattr(conn, "in_waiting", 0) or 1))
            except Exception as exc:  # noqa: BLE001
                self._marcar_desconectado(f"erro de leitura: {exc}")
                continue

            if not dados:
                continue
            for linha in self._consumir(dados):
                try:
                    self._processar(linha)
                except Exception:  # noqa: BLE001
                    # A thread leitora é única: mensagem malformada não pode
                    # derrubá-la, senão a porta fica aberta e muda.
                    logger.exception("[%s] falha processando linha da placa",
                                     self.subsistema)

    def _consumir(self, dados: bytes) -> list[str]:
        """Acumula bytes e devolve as linhas COMPLETAS que se formaram.

        Um `read()` não devolve uma linha: devolve o que estava no buffer do SO.
        Sem acumular, uma mensagem partida em duas leituras viraria dois JSON
        inválidos e sumiria sem rastro.

        Linha maior que `MAX_LINHA_BYTES` é descartada DE PROPÓSITO e logada —
        nunca partida em duas. Meia linha é JSON inválido, e as duas metades
        seriam descartadas em silêncio mais adiante.

        O teto é conferido nos DOIS pontos em que ele pode estourar: com a linha
        ainda aberta (o `\\n` não chegou) e com ela já fechada (a linha inteira
        veio numa leitura só). Conferir só o primeiro deixaria passar exatamente
        o caso mais comum — o firmware escrevendo de uma vez.
        """
        linhas: list[str] = []
        self._buffer.extend(dados)
        while True:
            corte = self._buffer.find(b"\n")
            if corte < 0:
                if len(self._buffer) > MAX_LINHA_BYTES:
                    self._descartar_linha_longa()
                    del self._buffer[:]
                    self._descartando = True
                return linhas
            crua = bytes(self._buffer[:corte])
            del self._buffer[: corte + 1]
            if self._descartando:
                self._descartando = False  # o rabo da linha que já foi descartada
                continue
            if len(crua) > MAX_LINHA_BYTES:
                self._descartar_linha_longa()
                continue
            linhas.append(crua.decode("utf-8", errors="replace").strip())

    def _descartar_linha_longa(self) -> None:
        self._linhas_truncadas += 1
        logger.warning(
            "[%s] linha acima de %d bytes descartada inteira — o firmware "
            "provavelmente escreveu mais do que o contrato permite.",
            self.subsistema, MAX_LINHA_BYTES,
        )

    def _processar(self, linha: str) -> None:
        if not linha:
            return
        mensagem = extrair_json(linha)
        if mensagem is None:
            logger.info("[%s] %s", self.subsistema, linha)  # log da placa, não erro
            return
        prefixo = linha[: linha.find("{")].strip()
        if prefixo:
            logger.info("[%s] %s", self.subsistema, prefixo)

        if mensagem.get("cmd") == "ping":
            self._responder_ping(mensagem)
        elif "resp" in mensagem:
            self._entregar_ack(mensagem)
        elif "evento" in mensagem:
            self._encaminhar_evento(mensagem.get("evento"))
        else:
            logger.warning("[%s] mensagem sem cmd/resp/evento: %s",
                           self.subsistema, linha[:200])

    def _responder_ping(self, mensagem: dict) -> None:
        sub = mensagem.get("sub")
        if sub and sub != self.subsistema and not self._sub_divergente_avisado:
            self._sub_divergente_avisado = True
            logger.error(
                "[%s] a placa nesta porta se identifica como '%s' — cabo trocado? "
                "Comandos deste adapter iriam para o hardware errado.",
                self.subsistema, sub,
            )
        self._ultimo_ping_placa = time.time()
        try:
            self._escrever({"resp": "pong", "epoch": int(time.time())})
        except ErroLink as exc:
            logger.warning("[%s] pong não saiu: %s", self.subsistema, exc)

    def _entregar_ack(self, mensagem: dict) -> None:
        if mensagem.get("resp") == "pong":
            return  # eco da própria porta (loop://) — não é ACK de ninguém
        cmd_id = mensagem.get("cmd_id")
        with self._lock_espera:
            espera = self._aguardando.get(cmd_id)
        if espera is None:
            # ACK de comando que já expirou: o prazo estourou e o endpoint já
            # respondeu. Registrar é o que separa "placa muda" de "placa lenta".
            logger.warning("[%s] ACK fora de hora (cmd_id=%s, resp=%s)",
                           self.subsistema, cmd_id, mensagem.get("resp"))
            return
        espera.resposta = mensagem
        espera.evento.set()

    def _encaminhar_evento(self, payload) -> None:
        if not isinstance(payload, dict):
            logger.warning("[%s] evento sem objeto: %r", self.subsistema, payload)
            return
        if self._periodica_repetida(payload):
            self._eventos_descartados += 1
            return
        if self._ao_receber_evento is None:
            return
        self._ao_receber_evento(payload)

    def _periodica_repetida(self, payload: dict) -> bool:
        """True quando este evento periódico é IDÊNTICO ao anterior do mesmo alvo.

        Só vale para os tipos periódicos: transição sai sempre, na hora — atraso
        ali é atraso de decisão do operador. E a comparação ignora `ts`, senão
        nenhuma telemetria seria igual a nada e o filtro não filtraria nada.
        """
        tipo = payload.get("tipo")
        if tipo not in TIPOS_PERIODICOS:
            return False
        chave = (
            tipo,
            payload.get("dispenser_id"),
            payload.get("slot_id"),
            payload.get("componente"),
            payload.get("tipo_leitura"),
        )
        assinatura = json.dumps(
            {k: v for k, v in payload.items() if k not in CAMPOS_VOLATEIS},
            ensure_ascii=False, sort_keys=True, default=str,
        )
        anterior = self._ultima_periodica.get(chave)
        self._ultima_periodica[chave] = assinatura
        return anterior == assinatura

    # ── Conexão ───────────────────────────────────────────────────────────────

    def _conectar(self):
        """URL fixa manda e não há varredura; vazia, varre e escuta o ping.

        A detecção é por PING DA PLACA, nunca por VID/PID: o VID/PID de um
        conversor USB-serial é o mesmo em placas de fabricantes diferentes, e
        casar por ele acha a placa errada — que aqui significa mandar `dispensar`
        para a balança. Quem sempre inicia o ping é a placa; este lado escuta e
        responde pong.
        """
        if self.url:
            return self._abrir(self.url)

        for porta in _portas_disponiveis():
            conn = self._sondar(porta)
            if conn is not None:
                return conn
        return None

    def _abrir(self, url: str):
        """`serial_for_url` aceita `/dev/ttyUSB0`, `COM4`, `socket://h:p`,
        `rfc2217://h:p` e `loop://` com a MESMA API.

        É o que permite a este código não saber em qual sistema operacional
        está: no alvo (mini PC Linux) a porta entra no container pelo `devices:`
        do compose; no plano B (Windows) uma ponte RFC2217 a expõe como socket.
        Ramificar por sistema operacional aqui seria a única peça do projeto que
        precisaria saber disso.
        """
        try:
            conn = serial.serial_for_url(url, do_not_open=True)
            conn.baudrate = self.baud
            conn.timeout = LEITURA_TIMEOUT_S
            conn.write_timeout = LEITURA_TIMEOUT_S
            # DTR/RTS desligados ANTES de abrir. Em placas ESP32-S3 esses dois
            # sinais SÃO o circuito de reset/boot: abrir a porta do jeito padrão
            # do pyserial reinicia a placa, e a sondagem entra num ciclo em que
            # ela nunca termina de bootar — abre (reset) → a placa leva segundos
            # bootando → o probe desiste e fecha (outro reset) → repete. Já
            # aconteceu neste projeto, com o display do painel de bancada.
            # Sondar um dispositivo não pode reiniciá-lo.
            conn.dtr = False
            conn.rts = False
            conn.open()
            return conn
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] não abriu %s: %s", self.subsistema, url, exc)
            return None

    def _sondar(self, url: str):
        """Abre a candidata e escuta o ping DESTE subsistema."""
        conn = self._abrir(url)
        if conn is None:
            return None
        if self._probe_assentar_s:
            time.sleep(self._probe_assentar_s)
        buffer = bytearray()
        limite = time.time() + self._probe_espera_s
        try:
            while time.time() < limite and not self._parar.is_set():
                dados = conn.read(max(1, getattr(conn, "in_waiting", 0) or 1))
                if not dados:
                    continue
                buffer.extend(dados)
                while True:
                    corte = buffer.find(b"\n")
                    if corte < 0:
                        if len(buffer) > MAX_LINHA_BYTES:
                            del buffer[:]
                        break
                    linha = bytes(buffer[:corte]).decode("utf-8", errors="replace")
                    del buffer[: corte + 1]
                    mensagem = extrair_json(linha)
                    if (mensagem and mensagem.get("cmd") == "ping"
                            and mensagem.get("sub") == self.subsistema):
                        conn.write(
                            (json.dumps({"resp": "pong", "epoch": int(time.time())})
                             + "\n").encode("utf-8")
                        )
                        conn.flush()
                        self._ultimo_ping_placa = time.time()
                        return conn
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] sondagem de %s falhou: %s", self.subsistema, url, exc)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return None

    def _marcar_conectado(self, conn) -> None:
        self._conn = conn
        self._url_aberta = str(getattr(conn, "port", "") or self.url)
        self._conectado_desde = time.time()
        self._sub_divergente_avisado = False
        del self._buffer[:]
        self._descartando = False
        # O log tem que dizer QUAL porta este processo tomou: com três adapters
        # e três cabos, "conectado" sem a porta não diz se o cabo certo entrou
        # no soquete certo.
        logger.info("[%s] porta tomada: %s @ %d baud (dono único deste processo)",
                    self.subsistema, self._url_aberta, self.baud)

    def _marcar_desconectado(self, motivo: str) -> None:
        ja_estava_fora = self._conn is None
        url = self._url_aberta or self.url
        self._fechar()
        if not ja_estava_fora:
            # Só a TRANSIÇÃO vira linha. O laço tenta de novo para sempre; uma
            # linha por tentativa encheria o log e esconderia a volta.
            logger.warning("[%s] porta %s desconectada — %s. Reconectando...",
                           self.subsistema, url, motivo)
        self._acordar_esperas(motivo)

    def _fechar(self) -> None:
        with self._lock_escrita:
            conn, self._conn = self._conn, None
        self._conectado_desde = None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _acordar_esperas(self, motivo: str) -> None:
        """Comando em voo quando a porta cai não espera o prazo inteiro.

        Ele vira `Desconectado` na hora, em vez de segurar o endpoint até o
        `ack_timeout_s`: quem está do outro lado é o orquestrador, com o relógio
        do `TIMEOUT_*` já correndo desde antes.
        """
        with self._lock_espera:
            pendentes = list(self._aguardando.values())
        for espera in pendentes:
            espera.resposta = {"resp": _RESP_PORTA_CAIU, "msg": motivo}
            espera.evento.set()


def _portas_disponiveis() -> list[str]:
    if _list_ports is None:
        return []
    try:
        return [p.device for p in _list_ports.comports()]
    except Exception:  # noqa: BLE001
        return []


def _iso(momento: Optional[float]) -> Optional[str]:
    if momento is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(momento))
