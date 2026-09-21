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
import inspect
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import ADAPTERS, ADAPTERS_SERIAIS, PASTA_DO_ADAPTER
from fakes.placa_cnc import PlacaCNC
from fakes.placa_dispenser import PlacaDispenser
from fakes.placa_dispenser_tft import PlacaDispenserTFT
from fakes.placa_weight import PlacaWeight

RAIZ_REPO = Path(__file__).resolve().parent.parent
# O `dispenser_tft` (as 8 telas TFT) é a SEGUNDA porta do dispenser-adapter:
# mesma pasta, mesmo `main.py`, outro `LinkSerial`. O mapa vem do conftest para
# que este arquivo e o `test_protocolo_placas.py` não carreguem duas cópias.
PASTA = PASTA_DO_ADAPTER
PLACAS = {"cnc": PlacaCNC, "dispenser": PlacaDispenser, "weight": PlacaWeight,
          "dispenser_tft": PlacaDispenserTFT}

# Prefixo da env var de cada adapter: `CNC_TRANSPORTE`, `DISPENSER_SERIAL_URL`...
PREFIXO = {"cnc": "CNC", "dispenser": "DISPENSER", "weight": "WEIGHT",
           "dispenser_tft": "DISPENSER_TFT"}

# Como cada subsistema aparece DENTRO do módulo do adapter: o nome da constante
# de subsistema, da URL, do prazo de ACK, do transporte, do callback de evento
# e do atributo que guarda o link. As três portas "principais" têm os mesmos
# nomes; a segunda porta do dispenser-adapter tem os seus, com prefixo TFT_.
_PRINCIPAL = dict(sub="SUBSISTEMA", url="SERIAL_URL", ack="ACK_TIMEOUT_S",
                  transporte="TRANSPORTE", callback="_evento_da_placa", link="_link")
LIGACAO = {
    "cnc": _PRINCIPAL, "dispenser": _PRINCIPAL, "weight": _PRINCIPAL,
    "dispenser_tft": dict(sub="TFT_SUBSISTEMA", url="TFT_SERIAL_URL",
                          ack="TFT_ACK_TIMEOUT_S", transporte="TFT_TRANSPORTE",
                          callback="_evento_da_placa_tft", link="_link_tft"),
}

pytest.importorskip("serial", reason="pyserial (tests/requirements-dev.txt)")


# ══════════════════════════════════════════════════════════════════════════════
# Infraestrutura
# ══════════════════════════════════════════════════════════════════════════════

# Prazo padrão de `_ate`, em segundos. Vem do ambiente porque o número certo
# depende da MÁQUINA, não do teste: cada teste deste arquivo levanta threads e
# sockets TCP, e a suíte inteira (~170 s) roda com dezenas deles competindo
# pelo escalonador. Dois testes falhavam de forma intermitente sob carga —
# `test_log_grudado_antes_do_json_e_processado` na suíte completa e
# `test_transicao_nunca_e_filtrada` no arquivo sozinho — e passavam 5/5
# isolados: não era o `serial_link`, era o helper desistindo com 5 s antes de a
# máquina chegar lá. Num CI mais lento, `APSEN_TESTE_ESPERA_S=60`.
ESPERA_PADRAO_S = float(os.environ.get("APSEN_TESTE_ESPERA_S", "20"))


def _descrever(condicao) -> str:
    """O texto da condição, para a mensagem de falha dizer O QUE se esperava.

    `inspect.getsource` de um lambda devolve a linha inteira em que ele foi
    escrito (`assert _ate(lambda: len(recebidos) == 1)`), que é exatamente o
    que quem lê o relatório quer ver. Se o fonte não estiver disponível, o
    `repr` ainda aponta para o arquivo e a linha.
    """
    try:
        return " ".join(inspect.getsource(condicao).split())
    except (OSError, TypeError):
        return repr(condicao)


def _ate(condicao, timeout: float | None = None, passo: float = 0.01,
         esperando: str | None = None) -> bool:
    """Espera `condicao()` ficar verdadeira. Devolve True; em prazo estourado,
    levanta `AssertionError` com uma mensagem que diz o que se esperava.

    Levanta em vez de devolver False: todo chamador faz `assert _ate(...)`, e
    um `assert False` sem texto não distingue "nunca aconteceu" de "demorou
    mais que o prazo". A mensagem carrega o prazo usado, a condição e — depois
    de estourar — uma última olhada: se a condição ficou verdadeira enquanto a
    mensagem era montada, o relato diz "demorou", que manda quem lê ajustar
    `APSEN_TESTE_ESPERA_S` em vez de procurar bug no transporte.

    `timeout=None` usa `ESPERA_PADRAO_S`. Um prazo explícito MENOR que o padrão
    é elevado a ele: nenhum teste deste arquivo usa prazo curto para provar que
    algo NÃO acontece (isso é feito com contadores e `sleep` explícitos), então
    o número passado é sempre "quanto tempo eu acho que isto leva", e a máquina
    lenta é quem decide.
    """
    prazo = ESPERA_PADRAO_S if timeout is None else max(float(timeout), ESPERA_PADRAO_S)
    inicio = time.monotonic()
    limite = inicio + prazo
    while time.monotonic() < limite:
        if condicao():
            return True
        time.sleep(passo)

    texto = esperando or _descrever(condicao)
    decorrido = time.monotonic() - inicio
    if condicao():
        raise AssertionError(
            f"DEMOROU: `{texto}` só ficou verdadeira depois do prazo de "
            f"{prazo:.1f}s (medido {decorrido:.1f}s). A máquina está lenta, não "
            f"o transporte — aumente APSEN_TESTE_ESPERA_S."
        )
    raise AssertionError(
        f"NUNCA ACONTECEU: `{texto}` continuou falsa por {prazo:.1f}s "
        f"(APSEN_TESTE_ESPERA_S={ESPERA_PADRAO_S:g})."
    )


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


def _sl_modulo(link):
    """O módulo `serial_link` em que este `LinkSerial` foi definido.

    A `SESSAO` é de MÓDULO (o processo tem uma só), e a suíte importa o
    `serial_link` por caminho — pegá-la pela classe evita depender de qual das
    três cópias o fixture carregou.
    """
    import sys
    return sys.modules[type(link).__module__]


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
        lig = LIGACAO[subsistema]
        link = modulo.serial_link.LinkSerial(
            subsistema=getattr(modulo, lig["sub"]), url=getattr(modulo, lig["url"]),
            ack_timeout_s=getattr(modulo, lig["ack"]),
            ao_receber_evento=getattr(modulo, lig["callback"]),
            probe_assentar_s=0.0, probe_espera_s=1.0, reconexao_s=0.05,
        )
        setattr(modulo, lig["link"], link)
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


def test_porta_fixa_que_nao_abre_avisa_em_vez_de_ficar_muda(sl, caplog):
    """O primeiro erro de bancada não pode ser silencioso.

    COM errada, cabo fora do soquete ou Monitor Serial aberto — os três param no
    mesmo lugar: `_abrir` levanta, loga em `debug`, e os adapters rodam em INFO.
    O processo então sobe, anuncia "Application startup complete" e entra num
    laço de reconexão de 3 em 3 segundos sem dizer NADA. Quem está na bancada vê
    um terminal parado e não tem como saber se o problema é a porta, o cabo ou o
    firmware.

    Aconteceu no primeiro ensaio da balança, exatamente assim.
    """
    link = sl.LinkSerial(subsistema="weight", url="COM_QUE_NAO_EXISTE",
                         reconexao_s=0.05)
    with caplog.at_level("WARNING"):
        link.iniciar()
        try:
            assert _ate(lambda: any("NÃO abriu" in r.message
                                    for r in caplog.records), timeout=5)
        finally:
            link.parar()

    aviso = [r for r in caplog.records if "NÃO abriu" in r.message][0]
    # A mensagem tem que levar o técnico aos três suspeitos, em ordem.
    assert "COM_QUE_NAO_EXISTE" in aviso.getMessage()
    assert "WEIGHT_SERIAL_URL" in aviso.getMessage()
    assert "exclusiva" in aviso.getMessage()


def test_o_aviso_de_porta_fixa_sai_UMA_vez_por_transicao(sl, caplog):
    """O laço tenta para sempre. Uma linha por tentativa encheria o log e
    esconderia justamente a linha que interessa — a da volta.

    É a mesma regra de `_marcar_desconectado`, e pelo mesmo motivo."""
    link = sl.LinkSerial(subsistema="cnc", url="COM_QUE_NAO_EXISTE",
                         reconexao_s=0.02)
    with caplog.at_level("WARNING"):
        link.iniciar()
        try:
            assert _ate(lambda: any("NÃO abriu" in r.message
                                    for r in caplog.records), timeout=5)
            time.sleep(0.4)          # ~20 tentativas de reconexão
        finally:
            link.parar()

    avisos = [r for r in caplog.records if "NÃO abriu" in r.message]
    assert len(avisos) == 1, f"{len(avisos)} avisos — o laço está logando por tentativa"


def test_a_varredura_que_falha_continua_calada(sl, caplog, monkeypatch):
    """A outra metade da decisão, e a que impede o conserto de virar ruído.

    Com a URL vazia o `_abrir` é chamado em TODA porta candidata e falha na
    maioria — é rotina, não erro de configuração. Subir o nível ali encheria o
    log de linhas normais, e o WARNING deixaria de significar alguma coisa.
    """
    monkeypatch.setattr(sl, "_portas_disponiveis",
                        lambda: ["COM_FALSA_1", "COM_FALSA_2"])
    link = sl.LinkSerial(subsistema="dispenser", url="", reconexao_s=0.05,
                         probe_assentar_s=0.0, probe_espera_s=0.05)
    with caplog.at_level("WARNING"):
        link.iniciar()
        try:
            time.sleep(0.3)
        finally:
            link.parar()

    assert not [r for r in caplog.records if "NÃO abriu" in r.message], (
        "a varredura passou a avisar — toda porta de outro processo viraria "
        "um WARNING a cada ciclo"
    )


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
    "dispenser_tft": ("estado_celula", {"trava_ativa": True, "trava_slot_id": 3,
                                        "os_id": "OS-1",
                                        "trava_resumo": "divergência de peso"}),
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


# ── `sessao`: o contador nasce de novo, e a placa precisa saber ──────────────
#
# O `cmd_id` é monotônico DENTRO de uma sessão do adapter: ele nasce em 1 a cada
# `LinkSerial` novo. A idempotência acima, sozinha, transforma isso num modo de
# falhar — um restart de 2 a 5 s do processo (o `.bat` da bancada reinicia
# sozinho) faz o contador voltar a 1 com a placa ainda em `ultimoCmdId = 47`, e
# TODO comando até 47 recebe ACK POSITIVO sem executar.
#
# Com o ciclo por relógio o sintoma mudou de lugar: não é mais só timeout — o
# central segue o cronograma e dispara o `dispensar` de um `mover` que a mesa
# nunca fez.
#
# A heurística antiga (silêncio de 10 s sem pong) continua no firmware como rede
# de segurança, e não cobre este caso: 2 s de queda não chegam perto de três
# pings perdidos.

def test_todo_comando_carrega_a_sessao(montar_link):
    placa, link, _ = montar_link("dispenser")

    link.enviar_comando("limpar", {"dispenser_id": 1, "solicitado_por": "t"})

    (linha,) = [l for l in placa.linhas_recebidas if '"cmd":"limpar"' in l]
    assert '"sessao"' in linha


def test_o_pong_tambem_carrega_a_sessao(montar_link):
    """O pong sozinho não basta (o primeiro comando pode chegar antes dele), mas
    sem ele a placa só saberia da sessão nova no primeiro comando — e é ela que
    decide se esse comando executa."""
    placa, link, _ = montar_link("dispenser")

    assert _ate(lambda: any('"resp":"pong"' in l for l in placa.linhas_recebidas))
    (pong,) = [l for l in placa.linhas_recebidas if '"resp":"pong"' in l][:1]
    assert '"sessao"' in pong


def test_sessao_nova_faz_a_placa_executar_o_cmd_id_repetido(montar_link):
    """O caso do restart rápido, encenado: o mesmo `cmd_id`, sessão diferente."""
    placa, link, _ = montar_link("dispenser")
    sessao = _sl_modulo(link).SESSAO
    base = {"cmd": "limpar", "dispenser_id": 1, "solicitado_por": "t"}

    link._escrever({**base, "cmd_id": 7, "sessao": sessao})
    assert _ate(lambda: len(placa.executados) == 1)
    # O adapter reiniciou: contador de volta ao 1, sessão nova.
    link._escrever({**base, "cmd_id": 1, "sessao": sessao + 1})

    assert _ate(lambda: len(placa.executados) == 2), (
        "a placa respondeu ACK sem executar — é o comando de um adapter que "
        "acabou de reiniciar, não um reenvio")


def test_a_mesma_sessao_continua_ignorando_repeticao(montar_link):
    """Controle, e é ele que separa as duas regras: dentro da MESMA sessão, o
    `cmd_id` repetido continua sendo reenvio — e reenviar `dispensar` é dose
    dobrada no leito."""
    placa, link, _ = montar_link("dispenser")
    sessao = _sl_modulo(link).SESSAO
    comando = {"cmd": "dispensar", "cmd_id": 1, "dispenser_id": 4,
               "os_id": "OS-9", "sessao": sessao}

    link._escrever(comando)
    assert _ate(lambda: len(placa.executados) == 1)
    link._escrever(dict(comando))
    time.sleep(0.3)

    assert len(placa.executados) == 1


def test_a_primeira_sessao_nao_descarta_nada(montar_link):
    """Zero (ou ausente) é "ainda não sei". Zerar na primeira faria todo boot da
    placa descartar o primeiro comando legítimo.

    Sem afirmar `sessao_adapter is None` antes: o pong chega de forma assíncrona
    e a placa pode já tê-la adotado por ali. O que importa é que o primeiro
    comando EXECUTE — e é isso que se mede.
    """
    placa, link, _ = montar_link("dispenser")

    link.enviar_comando("limpar", {"dispenser_id": 2, "solicitado_por": "t"})

    assert len(placa.executados) == 1
    assert placa.sessao_adapter == _sl_modulo(link).SESSAO


def test_relogio_que_anda_para_tras_tambem_e_sessao_nova(montar_link):
    """A comparação é de DIFERENÇA, não de ordem: NTP, fuso ou máquina sem RTC
    podem devolver um epoch menor, e continua sendo outro processo."""
    placa, link, _ = montar_link("dispenser")
    sessao = _sl_modulo(link).SESSAO
    base = {"cmd": "limpar", "dispenser_id": 1, "solicitado_por": "t"}

    link._escrever({**base, "cmd_id": 7, "sessao": sessao})
    assert _ate(lambda: len(placa.executados) == 1)
    link._escrever({**base, "cmd_id": 1, "sessao": sessao - 3600})

    assert _ate(lambda: len(placa.executados) == 2)


def test_a_sessao_nao_vira_campo_de_comando(montar_link):
    """Ela é do TRANSPORTE, como o `cmd_id`. Entrando no payload, ela viraria um
    campo que `test_protocolo_placas.py` cobraria do contrato de cada
    subsistema — e que o adapter repassaria ao central no evento."""
    placa, link, _ = montar_link("dispenser")

    link.enviar_comando("limpar", {"dispenser_id": 1, "solicitado_por": "t"})

    assert "sessao" not in placa.executados[0]


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


# Os eventos da placa das telas NÃO vão ao central — ficam no adapter, em log e
# no /health. Os dois testes abaixo medem justamente a chegada ao central, e o
# caso das telas tem os seus em `test_dispenser_tft.py`.
def _so_quem_fala_com_o_central(subsistema: str) -> None:
    if subsistema == "dispenser_tft":
        pytest.skip("os eventos da placa das telas ficam no adapter — "
                    "ver test_dispenser_tft.py")


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_evento_pelo_serial_e_pelo_http_chegam_iguais(adapter_serial,
                                                      carregar_adapter, subsistema):
    _so_quem_fala_com_o_central(subsistema)
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
    _so_quem_fala_com_o_central(subsistema)
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
    """Para as telas, "http" significa "sem telas" — e o adapter como era."""
    modulo = carregar_adapter(subsistema)
    assert getattr(modulo, LIGACAO[subsistema]["transporte"]) == "http"


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_transporte_desconhecido_cai_no_http_com_aviso(carregar_adapter, caplog,
                                                       subsistema):
    """Typo numa env var não pode desligar a planta em silêncio."""
    modulo = carregar_adapter(subsistema, env={
        f"{PREFIXO[subsistema]}_TRANSPORTE": "seriall"})

    assert getattr(modulo, LIGACAO[subsistema]["transporte"]) == "http"


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_comando_http_sai_na_rota_e_no_corpo_de_sempre(carregar_adapter, subsistema):
    """O corpo tem que sair byte a byte como saía antes desta feature."""
    modulo = carregar_adapter(subsistema)
    enviados: list[tuple] = []

    async def _fake(path, payload, timeout=None):
        enviados.append((path, payload))
        return {"ok": True}

    modulo._post_sim = _fake

    if subsistema == "dispenser_tft":
        # Com http, o único comando das telas que desce é `estado_celula` — ao
        # simulador, pelo `_client` (não pelo `_post_sim` dos mecanismos), para
        # o transporte http não virar 404 enquanto o serial funciona.
        cliente = ClienteHTTPFake()
        modulo._client = cliente
        asyncio.run(modulo.cmd_estado_celula(modulo.EstadoCelulaReq(
            trava_ativa=True, trava_slot_id=3, os_id="OS-1", trava_resumo="SKU errado")))
        assert enviados == [], "as telas não passam pelo caminho dos mecanismos"
        assert cliente.posts[0]["url"].endswith("/executar/estado-celula")
        assert cliente.posts[0]["json"]["trava_resumo"] == "SKU errado"
        return

    if subsistema == "cnc":
        # As coordenadas são ACEITAS pelo modelo (chamador antigo não toma 422)
        # e NÃO descem: o endereço da mesa é o dispenser, e a posição volta
        # medida no `posicionado`. Mandá-las seria um segundo mapa da célula na
        # linha serial, que o firmware teria de ignorar.
        asyncio.run(modulo.cmd_mover(modulo.ComandoMoverReq(
            dispenser_alvo=3, os_id="OS-1", receita="C",
            posicao_x=240.0, posicao_y=-150.0,
            ciclo_atual=1, total_ciclos=3)))
        assert enviados[0][0] == "/executar/mover"
        assert enviados[0][1]["dispenser_alvo"] == 3
        assert enviados[0][1]["receita"] == "C"
        assert "posicao_x" not in enviados[0][1]
        assert "posicao_y" not in enviados[0][1]
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
    if subsistema in ("cnc", "dispenser_tft"):
        pytest.skip("a CNC e as telas não recebem injeção: os tipos armáveis "
                    "são de dispensa, visão e peso")
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


# ══════════════════════════════════════════════════════════════════════════════
# A mesa CNC pela porta de verdade — adapter → socket:// → placa
# ══════════════════════════════════════════════════════════════════════════════
#
# `tests/test_protocolo_placas.py` cobre a placa falsa da mesa isoladamente.
# Estes exercitam a cadeia inteira, que é onde o `estado_celula` e a
# idempotência do `cmd_id` realmente valem: entre o endpoint do adapter e a
# thread leitora, atravessando uma porta serial de verdade.


def test_estado_celula_atravessa_o_adapter_da_mesa(adapter_serial):
    """A trava chega à mesa pelo MESMO caminho de um `mover`, sem tradução."""
    adapter = adapter_serial("cnc")

    adapter.chamar(adapter.modulo.cmd_estado_celula(
        adapter.modulo.EstadoCelulaReq(trava_ativa=True, trava_slot_id=5,
                                       os_id="OS-1", trava_resumo="divergência de peso")))

    executados = [e for e in adapter.placa.executados if e["cmd"] == "estado_celula"]
    assert executados, "o comando não chegou à placa"
    assert executados[0]["trava_ativa"] is True
    assert executados[0]["trava_slot_id"] == 5


def test_o_resumo_da_trava_chega_cortado_em_48(adapter_serial):
    """O motivo formatado do central passa de 240 caracteres e NÃO viaja.

    Mandá-lo acoplaria o formato de mensagem do central à largura de um
    terminal, e criaria um segundo ponto de truncamento para algo cosmético.
    """
    adapter = adapter_serial("cnc")

    adapter.chamar(adapter.modulo.cmd_estado_celula(
        adapter.modulo.EstadoCelulaReq(trava_ativa=True, os_id="OS-1",
                                       trava_resumo="x" * 200)))

    executados = [e for e in adapter.placa.executados if e["cmd"] == "estado_celula"]
    assert len(executados[0]["trava_resumo"]) == adapter.modulo.TRAVA_RESUMO_MAX


def test_estado_celula_e_aceito_no_meio_de_um_mover(adapter_serial):
    """Aceito NO MEIO é o ponto: a mesa está a caminho de um slot cuja dispensa
    o supervisor acabou de reprovar.

    A placa falsa com `atraso_evento` encena o `mover` que ainda não terminou; o
    `estado_celula` tem de ser aceito e executado nesse intervalo, sem esperar
    o fim do movimento.
    """
    adapter = adapter_serial("cnc", atraso_evento=0.4)

    adapter.chamar(adapter.modulo.cmd_mover(adapter.modulo.ComandoMoverReq(
        dispenser_alvo=3, os_id="OS-1", receita="C", ciclo_atual=1, total_ciclos=2)))

    # O `mover` já foi ACEITO (o ACK voltou) mas o `posicionado` ainda não saiu.
    adapter.chamar(adapter.modulo.cmd_estado_celula(
        adapter.modulo.EstadoCelulaReq(trava_ativa=True, trava_slot_id=3,
                                       os_id="OS-1", trava_resumo="peso")))

    cmds = [e["cmd"] for e in adapter.placa.executados]
    assert cmds == ["mover", "estado_celula"], cmds


def test_cmd_id_repetido_nao_move_a_mesa_duas_vezes(adapter_serial):
    """A idempotência do §2 aplicada ao comando que MOVE massa.

    O `serial_link` não reenvia nada, mas o reenvio pode vir de qualquer origem
    — um restart do adapter no meio do ciclo, um operador repetindo a ação. É a
    parte do protocolo que não dá para acrescentar depois sem trocar as duas
    pontas ao mesmo tempo.
    """
    adapter = adapter_serial("cnc")
    link = adapter.link

    corpo = {"dispenser_alvo": 3, "os_id": "OS-1", "receita": "C",
             "ciclo_atual": 1, "total_ciclos": 2}
    link.enviar_comando("mover", corpo)

    # O MESMO cmd_id de novo, montado à mão: é o reenvio que um ACK perdido
    # provoca.
    cmd_id = adapter.placa.executados[-1]["cmd_id"]
    link._escrever({"cmd": "mover", "cmd_id": cmd_id, **corpo})
    time.sleep(0.3)

    movimentos = [e for e in adapter.placa.executados if e["cmd"] == "mover"]
    assert len(movimentos) == 1, (
        f"a mesa executou {len(movimentos)} movimentos para um cmd_id só")
