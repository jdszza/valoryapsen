# -*- coding: utf-8 -*-
"""O transporte serial dos adapters, exercitado de ponta a ponta sem placa.

Os três adapters que vão falar com firmware — dispenser, CNC e balança — passaram
a ter DOIS transportes para a mesma perna de cima: `http` (o simulador, o default,
o que roda no CI e no Docker) e `serial` (o firmware, por uma porta USB). A perna
de cima não muda: mesmos endpoints, mesmos modelos Pydantic, mesmo `_post_central`
com o retry que `tests/test_adapters.py` cobra, mesmo payload de evento atravessando
sem interpretação.

Nenhum teste daqui abre porta física. As placas falsas de `tests/fakes/` servem o
contrato por `socket://`, que é um handler de URL do PRÓPRIO pyserial: o mesmo
`serial_for_url()` que abriria `/dev/ttyUSB0` ou `COM4`. Ou seja, o caminho de
abertura testado é o de verdade — o que muda é o que está do outro lado do fio.

O contrato está em `docs/PROTOCOLO_SERIAL.md`; `tests/test_protocolo_placas.py`
confere que ele e o código concordam.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import ADAPTERS, ADAPTERS_SERIAIS
from fakes.placa_cnc import PlacaCNC
from fakes.placa_dispenser import PlacaDispenser
from fakes.placa_weight import PlacaWeight

RAIZ_REPO = Path(__file__).resolve().parent.parent
PASTA = {nome: pasta for nome, pasta, _ in ADAPTERS}
PLACAS = {"cnc": PlacaCNC, "dispenser": PlacaDispenser, "weight": PlacaWeight}

# Prefixo da env var de cada adapter: `CNC_TRANSPORTE`, `DISPENSER_SERIAL_URL`...
PREFIXO = {"cnc": "CNC", "dispenser": "DISPENSER", "weight": "WEIGHT"}

pytest.importorskip("serial", reason="pyserial (tests/requirements-dev.txt)")


# ══════════════════════════════════════════════════════════════════════════════
# Infraestrutura
# ══════════════════════════════════════════════════════════════════════════════

def _ate(condicao, timeout: float = 5.0, passo: float = 0.01) -> bool:
    limite = time.time() + timeout
    while time.time() < limite:
        if condicao():
            return True
        time.sleep(passo)
    return False


def _carregar_serial_link(pasta: str = "cnc-adapter"):
    """Importa uma das cópias de `serial_link.py` por caminho.

    Qual delas não importa: um teste deste arquivo compara as três byte a byte.
    """
    caminho = RAIZ_REPO / pasta / "serial_link.py"
    nome = f"apsen_serial_link_{pasta.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(nome, caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(scope="module")
def sl():
    return _carregar_serial_link()


class ClienteHTTPFake:
    """Duplo do `httpx.AsyncClient` que os adapters guardam em `_client`."""

    class _Resposta:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.text = ""

    def __init__(self, status: int = 200):
        self.status = status
        self.posts: list[dict] = []
        self.gets: list[str] = []

    async def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        return self._Resposta(self.status)

    async def get(self, url, timeout=None):
        self.gets.append(url)
        return self._Resposta(self.status)


@pytest.fixture
def montar_link(sl):
    """Uma placa falsa + um `LinkSerial` já conectado a ela."""
    criados: list[tuple] = []

    def _montar(subsistema: str, ack_timeout_s: float = 1.0,
                atraso_evento: float = 0.0, ao_receber=None, **kw_placa):
        placa = PLACAS[subsistema](atraso_evento=atraso_evento,
                                   intervalo_ping=0.2, **kw_placa).iniciar()
        recebidos: list[dict] = []
        link = sl.LinkSerial(
            subsistema=subsistema,
            url=placa.url,
            ack_timeout_s=ack_timeout_s,
            ao_receber_evento=ao_receber or recebidos.append,
            probe_assentar_s=0.0, probe_espera_s=1.0, reconexao_s=0.05,
        )
        link.iniciar()
        criados.append((placa, link))
        assert _ate(lambda: link.conectado), "o link não conectou na placa falsa"
        return placa, link, recebidos

    yield _montar

    for placa, link in criados:
        link.parar()
        placa.parar()


class AdapterSerial:
    """Um adapter carregado com `<SUB>_TRANSPORTE=serial`, ligado a uma placa falsa.

    Monta o que a `lifespan` montaria: o event loop (numa thread, porque o
    pytest não roda dentro de um), o `_client` duplado e o `LinkSerial` com
    `ao_receber_evento=modulo._evento_da_placa`. É a cadeia inteira —
    placa → porta → thread leitora → event loop → `_post_central`.
    """

    def __init__(self, modulo, placa, link, loop, cliente):
        self.modulo = modulo
        self.placa = placa
        self.link = link
        self.loop = loop
        self.cliente = cliente

    def chamar(self, corotina, timeout: float = 5.0):
        return asyncio.run_coroutine_threadsafe(corotina, self.loop).result(timeout)

    @property
    def eventos_no_central(self) -> list[dict]:
        return [c["json"] for c in self.cliente.posts]


@pytest.fixture
def adapter_serial(carregar_adapter, sl):
    criados: list[AdapterSerial] = []

    def _montar(subsistema: str, ack_timeout_s: float = 1.0,
                atraso_evento: float = 0.0):
        prefixo = PREFIXO[subsistema]
        placa = PLACAS[subsistema](atraso_evento=atraso_evento,
                                   intervalo_ping=0.2).iniciar()
        modulo = carregar_adapter(subsistema, env={
            f"{prefixo}_TRANSPORTE": "serial",
            f"{prefixo}_SERIAL_URL": placa.url,
            f"{prefixo}_ACK_TIMEOUT_S": str(ack_timeout_s),
        })

        loop = asyncio.new_event_loop()
        pronto = threading.Event()

        def _rodar():
            asyncio.set_event_loop(loop)
            loop.call_soon(pronto.set)
            loop.run_forever()

        thread = threading.Thread(target=_rodar, name="loop-adapter", daemon=True)
        thread.start()
        pronto.wait(5)

        cliente = ClienteHTTPFake()
        modulo._client = cliente
        modulo._loop = loop
        # O `LinkSerial` vem do `serial_link` que ESTE módulo importou, não de
        # uma segunda carga do mesmo arquivo: as exceções são classes, e duas
        # cargas produzem duas hierarquias — o `except serial_link.AckNegativo`
        # do adapter não pegaria a exceção da outra cópia, e o teste mediria um
        # mapeamento de erro que na bancada não existe.
        link = modulo.serial_link.LinkSerial(
            subsistema=modulo.SUBSISTEMA, url=modulo.SERIAL_URL,
            ack_timeout_s=modulo.ACK_TIMEOUT_S,
            ao_receber_evento=modulo._evento_da_placa,
            probe_assentar_s=0.0, probe_espera_s=1.0, reconexao_s=0.05,
        )
        modulo._link = link
        link.iniciar()
        assert _ate(lambda: link.conectado)

        pronto_obj = AdapterSerial(modulo, placa, link, loop, cliente)
        criados.append((pronto_obj, thread))
        return pronto_obj

    yield _montar

    for objeto, thread in criados:
        objeto.link.parar()
        objeto.placa.parar()
        objeto.loop.call_soon_threadsafe(objeto.loop.stop)
        thread.join(3)
        objeto.loop.close()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Detecção de porta — pelo PING da placa, nunca por VID/PID
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_a_placa_pinga_e_o_adapter_responde_pong(montar_link, subsistema):
    """Quem inicia é a placa; este lado escuta e responde.

    É esse handshake que identifica a porta — e é por isso que a detecção não
    olha VID/PID: o VID/PID de um conversor USB-serial é o mesmo em placas de
    fabricantes diferentes, e casar por ele acha a placa errada.
    """
    placa, link, _ = montar_link(subsistema)

    assert _ate(lambda: placa.pongs_recebidos > 0)
    assert link.estado()["ultimo_ping_placa"] is not None


def test_varredura_aceita_a_primeira_porta_do_SUBSISTEMA_certo(sl, monkeypatch):
    """Com a URL vazia, varre — e a balança na frente não engana o adapter da CNC.

    Casar pela porta errada aqui significaria mandar `dispensar` para a balança:
    o comando sai, nada dispensa, e a OS morre por timeout de um slot que está
    íntegro.
    """
    balanca = PlacaWeight(intervalo_ping=0.1).iniciar()
    mesa = PlacaCNC(intervalo_ping=0.1).iniciar()
    monkeypatch.setattr(sl, "_portas_disponiveis",
                        lambda: [balanca.url, mesa.url])
    link = sl.LinkSerial(subsistema="cnc", url="", ack_timeout_s=1.0,
                         probe_assentar_s=0.0, probe_espera_s=0.6,
                         reconexao_s=0.05)
    link.iniciar()
    try:
        assert _ate(lambda: link.conectado, timeout=10)
        assert link.estado()["url_aberta"] == mesa.url
        assert balanca.pongs_recebidos == 0, (
            "respondeu pong à balança — a sondagem casou com o subsistema errado"
        )
    finally:
        link.parar()
        mesa.parar()
        balanca.parar()


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_url_configurada_manda_e_nao_ha_varredura(sl, montar_link, subsistema,
                                                  monkeypatch):
    """Porta declarada é porta declarada: varrer por cima seria abrir portas de
    outros processos por conta própria."""
    chamou = []
    monkeypatch.setattr(sl, "_portas_disponiveis",
                        lambda: chamou.append(1) or [])
    _, link, _ = montar_link(subsistema)

    assert link.conectado
    assert not chamou


# ══════════════════════════════════════════════════════════════════════════════
# 2. ACK — aceitei, não terminei
# ══════════════════════════════════════════════════════════════════════════════

COMANDO_EXEMPLO = {
    "cnc": ("mover", {"dispenser_alvo": 3, "os_id": "OS-1",
                      "posicao_x": 240.0, "posicao_y": -150.0,
                      "ciclo_atual": 1, "total_ciclos": 3}),
    "dispenser": ("dispensar", {"dispenser_id": 2, "os_id": "OS-1"}),
    "weight": ("pesar", {"os_id": "OS-1", "slot_id": 2,
                         "quantidade_esperada": 10, "quantidade_real": 10,
                         "peso_unitario_g": 50.0}),
}


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_ack_dentro_do_prazo_confirma_o_comando(montar_link, subsistema):
    placa, link, _ = montar_link(subsistema)
    nome, campos = COMANDO_EXEMPLO[subsistema]

    resposta = link.enviar_comando(nome, campos)

    assert resposta["resp"] == "ok"
    assert [e["cmd"] for e in placa.executados] == [nome]


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_ack_ausente_levanta_sem_ack(montar_link, sl, subsistema):
    """Placa travada: o comando saiu e ninguém confirmou.

    O prazo é curto de propósito — 2 s por default —, porque ele é só do ACK. O
    prazo da CONCLUSÃO continua no orquestrador e não é tocado por esta feature.
    """
    placa, link, _ = montar_link(subsistema, ack_timeout_s=0.2)
    placa.mudo = True
    nome, campos = COMANDO_EXEMPLO[subsistema]

    with pytest.raises(sl.SemAck):
        link.enviar_comando(nome, campos)


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_ack_negativo_levanta_ack_negativo(montar_link, sl, subsistema):
    placa, link, _ = montar_link(subsistema, ack_timeout_s=0.5)
    placa.recusar = True
    nome, campos = COMANDO_EXEMPLO[subsistema]

    with pytest.raises(sl.AckNegativo):
        link.enviar_comando(nome, campos)

    assert not placa.executados


def test_ack_chega_antes_do_evento_de_conclusao(montar_link):
    """A distinção que, confundida, trava uma OS.

    Se o adapter tratasse o ACK como conclusão, o orquestrador receberia
    "dispensado" no instante em que o comando foi aceito — antes de qualquer
    unidade cair na mesa — e a balança pesaria uma mesa vazia.
    """
    _, link, recebidos = montar_link("dispenser", atraso_evento=0.4)
    link.enviar_comando("carregar", {
        "dispenser_id": 1, "medicamento": "Dipirona 500mg", "sku": "APSEN-001",
        "categoria": "analgesico", "quantidade": 10, "os_id": "OS-1",
    })

    assert recebidos == [], "o evento de conclusão chegou junto com o ACK"
    assert _ate(lambda: len(recebidos) == 1, timeout=3)
    assert recebidos[0]["tipo"] == "carregado"


# ══════════════════════════════════════════════════════════════════════════════
# 3. `cmd_id` — e a ausência de reenvio
# ══════════════════════════════════════════════════════════════════════════════

def test_cmd_id_e_monotonico_por_porta(montar_link):
    placa, link, _ = montar_link("dispenser")
    for slot in (1, 2, 3):
        link.enviar_comando("limpar", {"dispenser_id": slot,
                                       "solicitado_por": "teste"})

    assert [e["cmd_id"] for e in placa.executados] == [1, 2, 3]


def test_o_adapter_nao_reenvia_comando_confirmado(montar_link):
    """Uma linha por comando. Retry no transporte serial não existe — nem por
    configuração —, e por um motivo só: reenviar `dispensar` é dose dobrada."""
    placa, link, _ = montar_link("dispenser")
    link.enviar_comando("dispensar", {"dispenser_id": 4, "os_id": "OS-9"})

    time.sleep(0.2)
    comandos = [l for l in placa.linhas_recebidas if '"cmd":"dispensar"' in l]
    assert len(comandos) == 1, comandos


def test_cmd_id_repetido_nao_executa_duas_vezes_na_placa(montar_link):
    """A defesa que tem que existir do lado de LÁ.

    O `serial_link` não reenvia, mas um reenvio pode vir de qualquer origem: um
    restart do adapter no meio do ciclo, um operador repetindo a ação, uma versão
    futura que decida retentar. É a parte do protocolo que não dá para
    acrescentar depois sem trocar as duas pontas ao mesmo tempo.
    """
    placa, link, _ = montar_link("dispenser")
    comando = {"cmd": "dispensar", "cmd_id": 1, "dispenser_id": 4, "os_id": "OS-9"}

    link._escrever(comando)
    assert _ate(lambda: len(placa.executados) == 1)
    link._escrever(comando)          # o reenvio que o ACK perdido provocaria
    link._escrever(dict(comando))    # e mais um, para não passar por acaso
    time.sleep(0.3)

    assert len(placa.executados) == 1, (
        "a placa executou o mesmo cmd_id mais de uma vez — dose dobrada no leito"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4. Enquadramento — o lixo antes do '{' não pode matar a mensagem
# ══════════════════════════════════════════════════════════════════════════════

def test_log_grudado_antes_do_json_e_processado(montar_link):
    """O firmware imprime log e JSON no mesmo Serial, e eles saem grudados.

    Exigir que a linha comece com '{' descarta justamente os pings do boot, que
    são os primeiros a chegar — e a detecção de porta passa a depender dos pings
    avulsos posteriores, falhando de forma intermitente sem nada no log.
    """
    placa, link, recebidos = montar_link("cnc")
    placa.enviar_bruto(
        'Iniciando eixos...{"evento":{"tipo":"concluido","os_id":"OS-1"}}\n'
    )

    assert _ate(lambda: len(recebidos) == 1)
    assert recebidos[0]["tipo"] == "concluido"


def test_linha_sem_json_vira_log_e_nao_derruba_a_thread(montar_link):
    placa, link, recebidos = montar_link("cnc")
    placa.enviar_bruto("boot: homing sensor OK\n")
    placa.enviar_bruto("=== APSEN CNC v1 ===\n")
    placa.enviar_bruto('{"evento":{"tipo":"concluido","os_id":"OS-2"}}\n')

    assert _ate(lambda: len(recebidos) == 1)
    assert recebidos[0]["os_id"] == "OS-2"
    assert link.conectado, "a thread leitora morreu com a linha de log"


def test_mensagem_partida_em_duas_leituras_e_remontada(sl):
    """Um `read()` devolve o que estava no buffer do SO, não uma linha."""
    link = sl.LinkSerial("cnc")

    assert link._consumir(b'{"evento":{"tipo":"conc') == []
    assert link._consumir(b'luido"}}\n') == ['{"evento":{"tipo":"concluido"}}']


def test_linha_acima_do_teto_e_descartada_inteira_e_a_seguinte_sobrevive(sl):
    """Nunca partida em duas: meia linha é JSON inválido, e as duas metades
    sumiriam em silêncio mais adiante — que é o pior modo de falhar."""
    link = sl.LinkSerial("cnc")
    gigante = b'{"evento":{"lixo":"' + b"x" * (sl.MAX_LINHA_BYTES + 50) + b'"}}\n'

    saida = link._consumir(gigante + b'{"evento":{"tipo":"concluido"}}\n')

    assert saida == ['{"evento":{"tipo":"concluido"}}']
    assert link._linhas_truncadas == 1


def test_comando_acima_do_teto_falha_em_vez_de_sair_truncado(montar_link, sl):
    """No sentido de SAÍDA a decisão é outra, e de propósito: comando truncado é
    JSON inválido, a placa o descarta e o adapter espera um ACK que nunca vem.
    Falhar aqui aponta para o comando; truncar apontaria para a placa."""
    _, link, _ = montar_link("dispenser")

    with pytest.raises(sl.ErroLink):
        link.enviar_comando("carregar", {
            "dispenser_id": 1, "medicamento": "M" * sl.MAX_LINHA_BYTES,
            "sku": "X", "categoria": "y", "quantidade": 1, "os_id": "OS-1",
        })


# ══════════════════════════════════════════════════════════════════════════════
# 5. Queda e volta da porta
# ══════════════════════════════════════════════════════════════════════════════

def test_porta_cai_recusa_na_hora_e_reconecta_sozinha(montar_link, sl):
    """Cabo solto é rotina, não é queda do processo.

    Enquanto está fora, o comando é recusado NA HORA — esperar o prazo do ACK
    gastaria o relógio do `TIMEOUT_*` do orquestrador para descobrir o que já se
    sabe.
    """
    placa, link, _ = montar_link("dispenser", ack_timeout_s=5.0)
    placa.derrubar_cabo()
    assert _ate(lambda: not link.conectado)

    inicio = time.time()
    with pytest.raises(sl.ErroLink):
        link.enviar_comando("limpar", {"dispenser_id": 1, "solicitado_por": "x"})
    assert time.time() - inicio < 1.0, "esperou o prazo do ACK em vez de recusar"

    assert _ate(lambda: link.conectado, timeout=10), "não reconectou sozinho"
    assert link.enviar_comando("limpar",
                               {"dispenser_id": 1, "solicitado_por": "x"})["resp"] == "ok"


def test_comando_em_voo_nao_espera_o_prazo_inteiro_quando_a_porta_cai(montar_link, sl):
    """E o erro é `Desconectado` (503), não `AckNegativo` (502).

    A diferença é para quem lê o log: uma manda olhar o cabo, a outra manda
    olhar o comando. A placa não recusou nada — ela sumiu no meio.
    """
    placa, link, _ = montar_link("dispenser", ack_timeout_s=10.0)
    placa.mudo = True

    erro: list = []

    def _enviar():
        try:
            link.enviar_comando("dispensar", {"dispenser_id": 1, "os_id": "OS-1"})
        except sl.ErroLink as exc:
            erro.append(exc)

    thread = threading.Thread(target=_enviar, daemon=True)
    thread.start()
    time.sleep(0.2)
    placa.derrubar_cabo()
    thread.join(3)

    assert erro, "o comando ficou pendurado esperando os 10s do prazo"
    assert isinstance(erro[0], sl.Desconectado), type(erro[0])


def test_o_marcador_de_porta_caida_nunca_sai_pela_linha(sl, montar_link):
    """Guarda do marcador interno: ele existe para não colidir com nenhuma
    resposta da placa, e uma placa que o emitisse veria o adapter tratar um ACK
    de verdade como queda de cabo."""
    placa, link, _ = montar_link("dispenser")
    link.enviar_comando("limpar", {"dispenser_id": 1, "solicitado_por": "x"})

    assert all(sl._RESP_PORTA_CAIU not in linha
               for linha in placa.linhas_recebidas), placa.linhas_recebidas


def test_ping_do_adapter_nao_olha_para_a_placa(adapter_serial):
    """O /ping é o portão do compose: ele responde por ESTE processo.

    Atrelá-lo ao hardware faria um cabo solto marcar o serviço como unhealthy e
    derrubar em cascata quem depende dele — que é justamente a hora em que o
    resto da planta precisa continuar de pé.
    """
    adapter = adapter_serial("dispenser")
    adapter.placa.derrubar_cabo()
    assert _ate(lambda: not adapter.link.conectado)

    assert adapter.modulo.ping()["status"] == "ok"


def test_health_marca_degradado_com_a_placa_fora(adapter_serial):
    """Quem conta a verdade sobre a placa é o /health, com url e desde quando."""
    adapter = adapter_serial("dispenser")
    saudavel = adapter.chamar(adapter.modulo.health())
    assert saudavel["status"] == "ok"
    assert saudavel["serial"]["conectado"] is True
    assert saudavel["serial"]["url_aberta"] == adapter.placa.url

    adapter.placa.derrubar_cabo()
    assert _ate(lambda: not adapter.link.conectado)

    degradado = adapter.chamar(adapter.modulo.health())
    assert degradado["status"] == "degradado"
    assert degradado["serial"]["conectado"] is False
    assert degradado["serial"]["conectado_desde"] is None


def test_comando_com_a_placa_fora_responde_503(adapter_serial):
    """O mesmo status que o transporte HTTP dá para simulador inalcançável."""
    from fastapi import HTTPException

    adapter = adapter_serial("dispenser")
    adapter.placa.derrubar_cabo()
    assert _ate(lambda: not adapter.link.conectado)

    req = adapter.modulo.ComandoLimparReq(dispenser_id=1, solicitado_por="teste")
    with pytest.raises(HTTPException) as exc:
        adapter.chamar(adapter.modulo.cmd_limpar(req))
    assert exc.value.status_code == 503


def test_ack_negativo_responde_502(adapter_serial):
    """Recusa da ponta de lá — o mesmo status que um simulador respondendo 4xx."""
    from fastapi import HTTPException

    adapter = adapter_serial("dispenser", ack_timeout_s=0.5)
    adapter.placa.recusar = True

    req = adapter.modulo.ComandoLimparReq(dispenser_id=1, solicitado_por="teste")
    with pytest.raises(HTTPException) as exc:
        adapter.chamar(adapter.modulo.cmd_limpar(req))
    assert exc.value.status_code == 502


# ══════════════════════════════════════════════════════════════════════════════
# 6. O evento chega ao central IDÊNTICO ao que o caminho HTTP entrega
# ══════════════════════════════════════════════════════════════════════════════
#
# É este o teste que garante que `central-computer/` não precisou mudar: se o
# payload que sai do adapter for o mesmo nos dois transportes, o central não tem
# como saber qual deles está embaixo.

EVENTOS_PARA_COMPARAR = {
    "dispenser": {
        "tipo": "dispensado", "dispenser_id": 4, "os_id": "OS-7",
        "medicamento": "Dipirona 500mg", "quantidade_dispensada": 9,
        "quantidade_alvo": 10, "falha_mecanica": True,
        "motivo_falha": "falha_mecanica", "quantidade_residual": 0,
        "falha_injetada": True, "ts": "2026-09-11T10:00:00",
    },
    "cnc": {
        "tipo": "posicionado", "os_id": "OS-7", "dispenser_alvo": 4,
        "posicao_x": 360.0, "posicao_y": -150.0, "ciclo_atual": 2,
        "total_ciclos": 3, "ts": "2026-09-11T10:00:00",
    },
    "weight": {
        "tipo": "peso_divergencia", "os_id": "OS-7", "slot_id": 4,
        "quantidade_esperada": 10, "quantidade_real": 9, "peso_unitario_g": 50.0,
        "peso_esperado_g": 500.0, "peso_medido_g": 450.0, "desvio_g": -50.0,
        "desvio_pct": 10.0, "tolerancia_pct": 5.0, "dentro_tolerancia": False,
        "falha_injetada": False, "ts": "2026-09-11T10:00:00",
    },
}


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_evento_pelo_serial_e_pelo_http_chegam_iguais(adapter_serial,
                                                      carregar_adapter, subsistema):
    payload = EVENTOS_PARA_COMPARAR[subsistema]

    # Serial: placa → porta → thread leitora → event loop → _post_central.
    adapter = adapter_serial(subsistema)
    adapter.placa.emitir(payload)
    assert _ate(lambda: adapter.eventos_no_central)
    pelo_serial = adapter.eventos_no_central[0]

    # HTTP: o endpoint /eventos de sempre, com o mesmo corpo.
    modulo_http = carregar_adapter(subsistema)
    cliente = ClienteHTTPFake()
    modulo_http._client = cliente
    asyncio.run(modulo_http.receber_evento(modulo_http.EventoReq(**payload)))
    pelo_http = cliente.posts[0]["json"]

    assert pelo_serial == pelo_http
    assert cliente.posts[0]["url"].endswith(
        next(rota for n, _, rota in ADAPTERS if n == subsistema))


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_evento_fora_do_contrato_e_descartado_sem_derrubar_a_thread(adapter_serial,
                                                                    subsistema):
    """Sem `tipo` não há evento — mas descartar não pode matar a ponte."""
    adapter = adapter_serial(subsistema)
    adapter.placa.emitir({"sem_tipo": 1})
    time.sleep(0.2)
    assert adapter.eventos_no_central == []

    adapter.placa.emitir(EVENTOS_PARA_COMPARAR[subsistema])
    assert _ate(lambda: len(adapter.eventos_no_central) == 1)


# ══════════════════════════════════════════════════════════════════════════════
# 7. Telemetria periódica não enche o canal nem o banco
# ══════════════════════════════════════════════════════════════════════════════

def test_telemetria_repetida_identica_nao_e_encaminhada(montar_link):
    """Mesma regra que o central já aplica no broadcast do WebSocket.

    Aqui ela tem um segundo motivo: num canal de 115200 baud, despejo periódico
    compete com os ACKs que este mesmo adapter está esperando.
    """
    placa, link, recebidos = montar_link("dispenser")

    placa.emitir(placa.telemetria(1, 27.4))
    assert _ate(lambda: len(recebidos) == 1)
    placa.emitir(placa.telemetria(1, 27.4))   # idêntica a menos do `ts`
    placa.emitir(placa.telemetria(1, 27.4))
    time.sleep(0.3)
    assert len(recebidos) == 1, "a repetição idêntica foi encaminhada"

    placa.emitir(placa.telemetria(1, 31.0))   # mudou: passa
    assert _ate(lambda: len(recebidos) == 2)
    assert link.estado()["eventos_periodicos_descartados"] == 2


def test_telemetria_de_slots_diferentes_nao_se_cancela(montar_link):
    """O filtro é por ALVO. Sem isso, o segundo slot sumiria por parecer igual
    ao primeiro — e o painel mostraria uma bancada de um slot só."""
    placa, link, recebidos = montar_link("dispenser")
    for slot in (1, 2, 3):
        placa.emitir(placa.telemetria(slot, 27.4))

    assert _ate(lambda: len(recebidos) == 3)


def test_transicao_nunca_e_filtrada(montar_link):
    """Duas dispensas idênticas são dois fatos, não um repetido.

    Filtrar transição perderia o segundo `dispensado` — e o orquestrador está
    bloqueado esperando exatamente por ele.
    """
    placa, link, recebidos = montar_link("dispenser")
    evento = EVENTOS_PARA_COMPARAR["dispenser"]
    placa.emitir(dict(evento))
    placa.emitir(dict(evento))

    assert _ate(lambda: len(recebidos) == 2)


# ══════════════════════════════════════════════════════════════════════════════
# 8. O transporte HTTP não mudou — é o default, e o CI depende disso
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_o_default_e_http(carregar_adapter, subsistema):
    assert carregar_adapter(subsistema).TRANSPORTE == "http"


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_transporte_desconhecido_cai_no_http_com_aviso(carregar_adapter, caplog,
                                                       subsistema):
    """Typo numa env var não pode desligar a planta em silêncio."""
    modulo = carregar_adapter(subsistema, env={
        f"{PREFIXO[subsistema]}_TRANSPORTE": "seriall"})

    assert modulo.TRANSPORTE == "http"


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_comando_http_sai_na_rota_e_no_corpo_de_sempre(carregar_adapter, subsistema):
    """O corpo tem que sair byte a byte como saía antes desta feature."""
    modulo = carregar_adapter(subsistema)
    enviados: list[tuple] = []

    async def _fake(path, payload, timeout=None):
        enviados.append((path, payload))
        return {"ok": True}

    modulo._post_sim = _fake

    if subsistema == "cnc":
        asyncio.run(modulo.cmd_mover(modulo.ComandoMoverReq(
            dispenser_alvo=3, os_id="OS-1", posicao_x=240.0, posicao_y=-150.0,
            ciclo_atual=1, total_ciclos=3)))
        assert enviados[0][0] == "/executar/mover"
        assert enviados[0][1]["dispenser_alvo"] == 3
    elif subsistema == "dispenser":
        asyncio.run(modulo.cmd_dispensar(modulo.ComandoDispensarReq(
            dispenser_id=2, os_id="OS-1")))
        assert enviados[0][0] == "/executar/dispensar"
        assert enviados[0][1] == {"dispenser_id": 2, "os_id": "OS-1",
                                  "injetar_falha": None}
    else:
        asyncio.run(modulo.cmd_pesar(modulo.PesarReq(
            os_id="OS-1", slot_id=2, quantidade_esperada=10,
            quantidade_real=9, peso_unitario_g=50.0)))
        assert enviados[0][0] == "/executar/pesar"
        assert enviados[0][1]["quantidade_real"] == 9


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_injetar_falha_atravessa_o_serial_sem_interpretacao(adapter_serial,
                                                            subsistema):
    """A demonstração de falhas do console tem que seguir funcionando com
    hardware real — e o adapter não pode ser quem decide o que ela significa."""
    if subsistema == "cnc":
        pytest.skip("a CNC não recebe injeção: os tipos armáveis são de "
                    "dispensa, visão e peso")
    adapter = adapter_serial(subsistema)

    if subsistema == "dispenser":
        req = adapter.modulo.ComandoDispensarReq(
            dispenser_id=1, os_id="OS-1", injetar_falha="falha_mecanica")
        adapter.chamar(adapter.modulo.cmd_dispensar(req))
    else:
        req = adapter.modulo.PesarReq(
            os_id="OS-1", slot_id=1, quantidade_esperada=10, quantidade_real=10,
            peso_unitario_g=50.0, injetar_falha="divergencia_peso")
        adapter.chamar(adapter.modulo.cmd_pesar(req))

    assert adapter.placa.executados[0]["injetar_falha"] in (
        "falha_mecanica", "divergencia_peso")


# ══════════════════════════════════════════════════════════════════════════════
# 9. Serial NUNCA no event loop (AST)
# ══════════════════════════════════════════════════════════════════════════════
#
# Mesma família da varredura que o `tests/test_orchestrator.py` faz atrás de
# `database.*` chamado fora do `to_thread`. `Serial.read()` e `Serial.write()`
# são bloqueantes: uma leitura pendurada congela o adapter inteiro, inclusive o
# /ping que o compose consulta como portão de subida.

# `iniciar`/`parar` ficam DE FORA: `iniciar` só sobe uma thread e `parar` espera
# a thread sair, o que é limitado pelo timeout de leitura (0,5 s) e acontece no
# shutdown, não no caminho de uma requisição.
METODOS_BLOQUEANTES = {"enviar_comando", "write", "flush", "read", "readline",
                       "read_until"}


def _fonte(caminho: Path) -> str:
    return caminho.read_text(encoding="utf-8")


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_nenhuma_chamada_serial_bloqueante_dentro_de_async_def(subsistema):
    arvore = ast.parse(_fonte(RAIZ_REPO / PASTA[subsistema] / "main.py"))
    infracoes = []
    for no in ast.walk(arvore):
        if not isinstance(no, ast.AsyncFunctionDef):
            continue
        for interno in ast.walk(no):
            if not isinstance(interno, ast.Call):
                continue
            func = interno.func
            if (isinstance(func, ast.Attribute)
                    and func.attr in METODOS_BLOQUEANTES):
                infracoes.append(f"{no.name}: {ast.unparse(func)}()")
    assert not infracoes, (
        f"{PASTA[subsistema]}/main.py chama serial bloqueante dentro de "
        f"`async def`: {infracoes}. Passe por `asyncio.to_thread`."
    )


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_o_envio_serial_passa_por_to_thread(subsistema):
    """Guarda da guarda: o teste acima também passa se a chamada SUMIR."""
    fonte = _fonte(RAIZ_REPO / PASTA[subsistema] / "main.py")
    assert re.search(r"asyncio\.to_thread\(\s*_link\.enviar_comando", fonte), (
        "ninguém mais despacha comando pelo serial — o teste de AST acima "
        "virou verde permanente"
    )


def test_serial_link_nao_conhece_asyncio():
    """A separação que torna o AST acima verificável: o transporte é de threads,
    e quem faz o salto para o event loop é o adapter."""
    fonte = _fonte(RAIZ_REPO / "cnc-adapter" / "serial_link.py")
    arvore = ast.parse(fonte)

    assert not [n for n in ast.walk(arvore) if isinstance(n, ast.AsyncFunctionDef)]
    assert not re.search(r"^\s*import asyncio", fonte, re.M)


# ══════════════════════════════════════════════════════════════════════════════
# 10. As três cópias de serial_link.py são a MESMA
# ══════════════════════════════════════════════════════════════════════════════

def test_as_tres_copias_de_serial_link_sao_identicas():
    """Cópia e não `shared/`: cada adapter tem o seu contexto de build no
    compose, e compartilhar um diretório arrastaria o repositório inteiro para
    dentro das três imagens.

    O preço da escolha é este teste — a mesma forma de
    `test_adapters.py::test_os_quatro_adapters_usam_a_mesma_politica`. Divergir
    aqui é ter um adapter com o enquadramento de ontem, e ele seria justamente o
    que ninguém está depurando naquele dia.
    """
    conteudos = {
        pasta: (RAIZ_REPO / pasta / "serial_link.py").read_bytes()
        for pasta in (PASTA[n] for n in ADAPTERS_SERIAIS)
    }
    assert len(set(conteudos.values())) == 1, {
        pasta: len(dados) for pasta, dados in conteudos.items()
    }


def test_o_vision_adapter_nao_ganhou_transporte_serial():
    """A visão continua por HTTP, e isso é escolha registrada — um `serial_link`
    que aparecesse lá seria uma quarta cópia sem placa do outro lado."""
    assert not (RAIZ_REPO / "vision-adapter" / "serial_link.py").exists()


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_a_imagem_do_adapter_leva_pyserial_e_o_serial_link(subsistema):
    """Import novo exige linha nova no `requirements.txt`, e arquivo novo exige
    que o Dockerfile o copie — nenhum teste da suíte lembra disso por você (o
    `weight-adapter` copiava só o `main.py`)."""
    pasta = RAIZ_REPO / PASTA[subsistema]
    assert "pyserial" in (pasta / "requirements.txt").read_text(encoding="utf-8")
    dockerfile = (pasta / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY . ." in dockerfile or "serial_link.py" in dockerfile
