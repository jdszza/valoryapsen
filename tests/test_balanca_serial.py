# -*- coding: utf-8 -*-
"""A balança de bancada atravessando a serial — o que o firmware 2.3 acrescentou.

`tests/test_serial_link.py` já cobre o TRANSPORTE (enquadramento, ACK, `cmd_id`,
reconexão) e `tests/test_protocolo_placas.py` já confronta o CONTRATO (documento
× adapter × placa falsa). O que falta é o que só existe na balança:

  * a placa tem DUAS vozes na mesma porta. A da OS sobe ao central; a da
    bancada — `boot`, `peso`, `contagem`, `cfg`, `estado`, `tara_balanca`,
    `erro_balanca` — para no adapter e sai por `GET /balanca`. Um evento de
    bancada que vazasse para o central não daria erro em lugar nenhum: o evento
    atravessa sem interpretação, e o central gravaria a linha estranha calado;
  * o firmware imprime log humano e JSON no MESMO Serial, e eles saem grudados.
    Linha sem `{` é log e não pode virar exceção; linha com `{` e JSON
    quebrado, idem;
  * `boot` é o único evento que muda o comportamento do adapter (marca a tara
    como não confiável), e é o caso em que o firmware e o transporte se
    contradizem por bom motivo: o adapter não reinicia a placa (DTR/RTS saem
    desligados antes do `open()`), então todo boot que chega é um boot que
    ninguém pediu — e o `setup()` da balança tara sozinho depois de 5 s.

O elenco é o mesmo dos outros testes de serial: a placa falsa por `socket://`,
que é um handler do próprio pyserial, atravessando o `serial_for_url` de
verdade. Nenhum teste abre porta física.

**A última seção lê C++ por texto, e isso tem uma guarda própria.** O firmware
não compila na suíte e não há AST para ele — é a mesma situação de
`tests/test_protocolo_serial.py` com o `main.cpp` do display, e o mesmo risco:
um extrator que pare de casar deixa tudo verde para sempre, inclusive com o
firmware quebrado. Por isso estes testes foram validados por MUTAÇÃO: nove
quebras deliberadas do contrato (estado atribuído fora do setter, `MAX_OS_ID_LEN`
encolhido, stream fora dos 5 Hz, `CountResult` renomeado, `fmtF` sem a guarda de
`NaN`, o contador de `cmd_id` sem o reset de sessão, `peso` fora de
`_EVENTOS_BANCADA`, a tara não perdendo a confiança, o desvio da bancada
desligado) — 9 de 9 ficaram vermelhas.
"""
from __future__ import annotations

import ast
import asyncio
import re
import threading
import time
from pathlib import Path

import pytest

from fakes.placa_weight import ESTADOS, STATUS_CONTAGEM, PlacaWeight

RAIZ_REPO = Path(__file__).resolve().parent.parent
SKETCH = RAIZ_REPO / "weight" / "balanca2_3" / "balanca2_3.ino"
SKETCH_ANTIGO = RAIZ_REPO / "weight" / "balanca2.2.ino"
ADAPTER = RAIZ_REPO / "weight-adapter" / "main.py"


@pytest.fixture(scope="module")
def firmware() -> str:
    if not SKETCH.is_file():
        pytest.fail(f"{SKETCH} não está no disco — é ele que grava na placa")
    return SKETCH.read_text(encoding="utf-8")


# ── Leitura do C++ ────────────────────────────────────────────────────────────
#
# A comparação é por TEXTO, porque a outra ponta é C++ e não há AST para ela —
# a mesma situação de `tests/test_protocolo_serial.py` com o `main.cpp` do
# display. Duas armadilhas já custaram falso positivo aqui e estão resolvidas
# nestes dois helpers:
#
#   * a DECLARAÇÃO adiantada casa com a mesma assinatura da definição (o
#     `.ino` precisa delas porque `saveCountConfig()` chama emissor definido lá
#     embaixo). Procurar a primeira ocorrência pegava o `;` e devolvia o corpo
#     de outra função;
#   * o comentário que EXPLICA por que algo não é usado contém o nome da coisa
#     não usada. `transientBuffer` aparece no comentário de
#     `atualizarEstabilidade` dizendo justamente que ela não o usa.

def _sem_comentario(texto: str) -> str:
    texto = re.sub(r"/\*.*?\*/", "", texto, flags=re.S)
    return re.sub(r"//[^\n]*", "", texto)


def _corpo(fonte: str, assinatura: str) -> str:
    """Corpo da DEFINIÇÃO de uma função, sem comentários."""
    for achado in re.finditer(re.escape(assinatura), fonte):
        resto = fonte[achado.start():]
        if resto.split("\n", 1)[0].rstrip().endswith(";"):
            continue                      # declaração adiantada
        fim = resto.find("\n}\n")
        if fim < 0:
            fim = resto.rfind("\n}")       # a última função do arquivo (o `loop`)
        return _sem_comentario(resto[:fim])
    raise AssertionError(f"{assinatura} não tem definição no sketch")


@pytest.fixture
def balanca(carregar_adapter):
    """weight-adapter em transporte serial, ligado à placa falsa.

    A placa sobe ANTES do link, como uma placa de verdade: quem varre e espera
    é o adapter. `intervalo_ping` curto para o pong não atrasar o teste.
    """
    placa = PlacaWeight(atraso_evento=0.0, intervalo_ping=0.05).iniciar()
    modulo = carregar_adapter("weight", env={
        "WEIGHT_TRANSPORTE": "serial",
        "WEIGHT_SERIAL_URL": placa.url,
        "WEIGHT_ACK_TIMEOUT_S": "2",
    })
    link = modulo.serial_link.LinkSerial(
        subsistema="weight", url=placa.url, ack_timeout_s=2.0,
        ao_receber_evento=modulo._evento_da_placa,
    )
    modulo._link = link

    # O event loop do adapter, num thread próprio. Ele não é decoração: o
    # evento chega na THREAD LEITORA e salta para o loop por
    # `run_coroutine_threadsafe` — sem loop, `_evento_da_placa` descarta e o
    # teste do encaminhamento mediria o descarte.
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    modulo._loop = loop

    link.iniciar()
    assert placa.esperar_conexao(5.0), "a placa falsa não recebeu conexão"
    _esperar(lambda: link.conectado, "o link não conectou")

    yield modulo, placa, link

    link.parar()
    placa.parar()
    loop.call_soon_threadsafe(loop.stop)
    thread.join(2.0)
    loop.close()


def _esperar(condicao, mensagem: str, timeout: float = 5.0) -> None:
    limite = time.time() + timeout
    while time.time() < limite:
        if condicao():
            return
        time.sleep(0.01)
    pytest.fail(mensagem)


# ══════════════════════════════════════════════════════════════════════════════
# 1. O parser de linha — o que o firmware realmente escreve na porta
# ══════════════════════════════════════════════════════════════════════════════

def test_linha_humana_e_ignorada_sem_derrubar_a_thread(balanca):
    """A 2.2 imprimia só para humano, e tudo aquilo continua saindo.

    São dezenas de linhas por boot (`Calibracao carregada.`, o mapa de canais,
    a ajuda do `?`). Cada uma tem que ser log e nada mais — uma exceção aqui
    mata a thread leitora, a porta fica aberta e muda, e o sintoma chega como
    OS abortada por timeout com a bancada intacta.
    """
    modulo, placa, link = balanca
    for linha in ("Calibracao carregada.",
                  "Tara c0: offset=8412",
                  "=== MAPA DE CANAIS ===",
                  "  t = tara canais ativos",
                  "AVISO: Canal 2 SATURADO!"):
        placa.enviar_bruto(linha + "\n")

    # A prova de que a thread sobreviveu: um evento DEPOIS do lixo ainda chega.
    placa.emitir(placa.cfg())
    _esperar(lambda: modulo._bancada["cfg"] is not None,
             "a thread leitora morreu no meio do log humano")


def test_json_invalido_e_ignorado(balanca):
    """Linha com `{` e JSON quebrado é log, nunca exceção.

    Acontece de verdade: o firmware pode ter a linha cortada por um reset no
    meio da escrita, e `extrair_json` devolve None em vez de levantar.
    """
    modulo, placa, link = balanca
    placa.enviar_bruto('{"evento":{"tipo":"peso",\n')
    placa.enviar_bruto('{isto nao e json}\n')

    placa.emitir(placa.boot())
    _esperar(lambda: modulo._bancada["boot"] is not None,
             "JSON quebrado derrubou a leitura")


def test_log_grudado_no_json_nao_perde_o_evento(balanca):
    """`Iniciando HX711...{"evento":{...}}` — as duas vozes na MESMA linha.

    Exigir que a linha comece com `{` descartaria justamente os primeiros
    eventos do boot, que são os que dizem que a placa reiniciou.
    """
    modulo, placa, link = balanca
    placa.emitir_grudado(placa.boot(), "(Timeout) Tara automatica...")
    _esperar(lambda: modulo._bancada["boot"] is not None,
             "evento grudado em log foi descartado")
    assert modulo._bancada["boot"]["fw"] == "2.3"


def test_o_stream_de_peso_chega_e_fica_no_adapter(balanca):
    modulo, placa, link = balanca
    placa.mesa_g = 152.37
    placa.emitir(placa.peso(estavel=True))

    _esperar(lambda: modulo._bancada["peso"] is not None, "o stream não chegou")
    peso = modulo._bancada["peso"]
    assert peso["total_g"] == pytest.approx(152.37)
    assert peso["estavel"] is True
    # Canal inativo é `null`, e não zero: zero é uma leitura.
    assert peso["canais_g"][3] is None


def test_a_contagem_por_peso_chega_inteira(balanca):
    """O resultado de `countByWeight` é o dado da bancada — e é só dela."""
    modulo, placa, link = balanca
    placa.mesa_g = 152.40
    placa.uw_g = 10.24
    placa.emitir(placa.contagem(152.40, 102.40, 10.0))

    _esperar(lambda: modulo._bancada["contagem"] is not None, "sem contagem")
    contagem = modulo._bancada["contagem"]
    assert contagem["contagem"] == 10
    assert contagem["status"] in STATUS_CONTAGEM
    assert contagem["aceite"] is True


def test_a_cfg_chega_com_os_sete_campos(balanca):
    """`cfg` é o retrato da NVS da placa: um campo a menos some do painel."""
    modulo, placa, link = balanca
    placa.emitir(placa.cfg())
    _esperar(lambda: modulo._bancada["cfg"] is not None, "sem cfg")
    assert set(modulo._bancada["cfg"]) == {
        "tipo", "uw_g", "tara_g", "tol_g", "min", "max", "sreads",
        "sthres_g", "ts",
    }


# ══════════════════════════════════════════════════════════════════════════════
# 2. As duas vozes: o que sobe ao central e o que para no adapter
# ══════════════════════════════════════════════════════════════════════════════

def test_evento_de_bancada_nao_vai_ao_central(balanca, monkeypatch):
    """O central não tem endpoint de balança de bancada.

    Encaminhar não daria erro — o evento atravessa sem interpretação —, daria
    linhas estranhas num histórico que hoje é só de pesagem de OS.
    """
    modulo, placa, link = balanca
    postados: list[dict] = []

    async def _espiao(payload):
        postados.append(payload)
        return True

    monkeypatch.setattr(modulo, "_post_central", _espiao)

    for evento in (placa.boot(), placa.peso(), placa.cfg(),
                   placa.evento_estado("TRANSIENT"),
                   placa.contagem(100.0, 50.0, 5.0),
                   placa.tara_balanca("canais", None),
                   placa.erro_balanca("canal 0 saturado")):
        placa.emitir(evento)

    _esperar(lambda: modulo._bancada["erro_balanca"] is not None,
             "o último evento de bancada não chegou")
    time.sleep(0.1)   # folga para um encaminhamento indevido aparecer
    assert postados == [], f"evento de bancada vazou para o central: {postados}"


def test_evento_de_os_continua_subindo_ao_central(balanca, monkeypatch):
    """O controle do teste acima: "não encaminhou" não pode ser confundido com
    "o caminho de encaminhamento sumiu"."""
    modulo, placa, link = balanca
    postados: list[dict] = []

    async def _espiao(payload):
        postados.append(payload)
        return True

    monkeypatch.setattr(modulo, "_post_central", _espiao)
    placa.emitir(placa.telemetria())
    _esperar(lambda: postados, "a telemetria não subiu ao central")
    assert postados[0]["tipo"] == "telemetria"


def test_a_lista_de_eventos_de_bancada_e_a_do_documento():
    """Documento e código falando das mesmas chaves.

    Um `tipo` que só existisse de um lado seria encaminhado ao central por
    engano (se faltasse no código) ou nunca implementado pelo firmware (se
    faltasse no documento).
    """
    doc = (RAIZ_REPO / "docs" / "PROTOCOLO_SERIAL.md").read_text(encoding="utf-8")
    fonte = ADAPTER.read_text(encoding="utf-8")
    bloco = re.search(r"_EVENTOS_BANCADA\s*=\s*frozenset\(\{(.*?)\}\)",
                      fonte, re.S)
    assert bloco, "_EVENTOS_BANCADA sumiu do weight-adapter"
    no_codigo = set(re.findall(r'"(\w+)"', bloco.group(1)))

    secao = doc.split("## 5.", 1)[1].split("\n## ", 1)[0]
    tabela = secao.split("### Eventos", 1)[1].split("\n---", 1)[0]
    documentados = set(re.findall(r"^\| `(\w+)` \|", tabela, re.M)) - {"tipo"}

    assert no_codigo <= documentados, {
        "no código e não documentado": sorted(no_codigo - documentados)}
    # E o par: o que sobe ao central é exatamente o resto.
    assert documentados - no_codigo == {
        "tara_ok", "peso_ok", "peso_divergencia", "erro_sensor", "telemetria"}


# ══════════════════════════════════════════════════════════════════════════════
# 3. `boot` inesperado: a tara deixa de ser confiável
# ══════════════════════════════════════════════════════════════════════════════

def test_boot_marca_a_tara_como_nao_confiavel(balanca, caplog):
    """Abrir a porta NÃO reinicia a placa (DTR/RTS saem desligados), então todo
    boot que chega aqui é um boot que ninguém pediu — e o `setup()` da balança
    tara sozinho depois de 5 s. Com peso na mesa, a tara levou o peso junto."""
    modulo, placa, link = balanca
    assert modulo._bancada["tara_confiavel"] is True

    with caplog.at_level("WARNING"):
        placa.emitir(placa.boot())
        _esperar(lambda: modulo._bancada["boots_vistos"] == 1, "boot não contado")

    assert modulo._bancada["tara_confiavel"] is False
    assert any("REINICIOU" in r.message for r in caplog.records)


def test_a_porta_abre_com_dtr_e_rts_desligados_ANTES_do_open(carregar_adapter):
    """A outra metade do `boot` inesperado, e a que o impede de acontecer.

    DTR e RTS são o circuito de reset/boot do ESP32: abrir a porta do jeito
    padrão do pyserial reinicia a placa. Aqui isso não seria só uma
    inconveniência — o `setup()` da balança tara sozinho depois de 5 s, então
    um reset com peso na mesa deixa a tara errada, e nada acusa. Já aconteceu
    neste projeto, com o display do painel de bancada.

    A ordem é o ponto: desligar DEPOIS do `open()` não desfaz o pulso que já
    reiniciou a placa.
    """
    modulo = carregar_adapter("weight", env={"WEIGHT_TRANSPORTE": "serial"})

    class SerialEspiao:
        def __init__(self):
            self.ordem: list[str] = []

        def __setattr__(self, nome, valor):
            if nome != "ordem":
                self.ordem.append(f"{nome}={valor}")
            object.__setattr__(self, nome, valor)

        def open(self):
            self.ordem.append("open()")

    espiao = SerialEspiao()

    class ModuloSerialFalso:
        @staticmethod
        def serial_for_url(url, do_not_open=False):
            assert do_not_open, "abrir aqui já reiniciaria a placa"
            return espiao

    link = modulo.serial_link.LinkSerial(subsistema="weight", url="COM9")
    original = modulo.serial_link.serial
    modulo.serial_link.serial = ModuloSerialFalso
    try:
        assert link._abrir("COM9") is espiao
    finally:
        modulo.serial_link.serial = original

    assert "open()" in espiao.ordem
    for sinal in ("dtr=False", "rts=False"):
        assert sinal in espiao.ordem, espiao.ordem
        assert espiao.ordem.index(sinal) < espiao.ordem.index("open()"), espiao.ordem


def test_boot_nao_bloqueia_nada(balanca, monkeypatch):
    """Diagnóstico, não portão: a OS segue, e quem decide é quem lê.

    Recusar pesagem por causa disso trocaria um número possivelmente errado por
    uma planta parada — e a tara pode estar certíssima (mesa vazia no reset).
    """
    modulo, placa, link = balanca
    placa.emitir(placa.boot())
    _esperar(lambda: modulo._bancada["tara_confiavel"] is False, "sem boot")

    resposta = asyncio.run(modulo.cmd_pesar(modulo.PesarReq(
        os_id="OS-1", slot_id=1, quantidade_esperada=10, quantidade_real=10,
        peso_unitario_g=50.0)))
    assert resposta["ok"] is True


def test_a_tara_da_os_devolve_a_confianca(balanca):
    """É exatamente o ato que o boot invalidou — e por isso é ele que a devolve,
    e não um botão à parte que alguém clicaria sem esvaziar a mesa."""
    modulo, placa, link = balanca
    placa.emitir(placa.boot())
    _esperar(lambda: modulo._bancada["tara_confiavel"] is False, "sem boot")

    asyncio.run(modulo.cmd_tara(modulo.TaraReq(os_id="OS-1")))
    assert modulo._bancada["tara_confiavel"] is True


# ══════════════════════════════════════════════════════════════════════════════
# 4. Comandos de bancada
# ══════════════════════════════════════════════════════════════════════════════

def test_peso_unitario_chega_a_placa_e_volta_como_cfg(balanca):
    modulo, placa, link = balanca
    asyncio.run(modulo.bancada_peso_unitario(modulo.PesoUnitarioReq(valor_g=12.5)))

    executados = [e for e in placa.executados if e["cmd"] == "peso_unitario"]
    assert executados and executados[0]["valor_g"] == 12.5
    _esperar(lambda: (modulo._bancada["cfg"] or {}).get("uw_g") == 12.5,
             "a placa não publicou a config nova")


@pytest.mark.parametrize("valor", [0, -1.0])
def test_peso_unitario_invalido_nao_chega_a_placa(balanca, valor):
    """`valor_g <= 0` desliga a contagem por peso inteira (`NO_UNIT_WEIGHT`), e
    um zero que chegasse lá viraria uma balança que não conta mais — sem erro em
    lugar nenhum até alguém pedir uma contagem."""
    modulo, placa, link = balanca
    with pytest.raises(modulo.HTTPException) as erro:
        asyncio.run(modulo.bancada_peso_unitario(
            modulo.PesoUnitarioReq(valor_g=valor)))
    assert erro.value.status_code == 422
    assert not [e for e in placa.executados if e["cmd"] == "peso_unitario"]


def test_contar_e_taras_chegam_a_placa(balanca):
    modulo, placa, link = balanca
    asyncio.run(modulo.bancada_tara_recipiente())
    asyncio.run(modulo.bancada_tara_canais())
    asyncio.run(modulo.bancada_contar())
    asyncio.run(modulo.bancada_config())
    asyncio.run(modulo.bancada_stream(modulo.StreamReq(on=False)))

    enviados = [e["cmd"] for e in placa.executados]
    assert enviados == ["tara_recipiente", "tara_canais", "contar",
                        "config", "stream"]


def test_a_sequencia_da_tara_de_recipiente_chega_inteira(balanca):
    """A tara do pote é uma SEQUÊNCIA de estados, não um evento: quem lê o
    painel precisa ver a máquina sair de IDLE e voltar a esperar depósito."""
    modulo, placa, link = balanca
    asyncio.run(modulo.bancada_tara_recipiente())
    _esperar(lambda: (modulo._bancada["estado"] or {}).get("estado")
             == "AWAITING_DEPOSIT", "a máquina não chegou a AWAITING_DEPOSIT")
    assert modulo._bancada["tara_balanca"]["alvo"] == "recipiente"


def test_comando_de_bancada_com_transporte_http_responde_503(carregar_adapter):
    """Sem placa não há o que configurar, e dizer isso é o que separa "não há
    balança nesta montagem" de "a balança recusou"."""
    modulo = carregar_adapter("weight", env={"WEIGHT_TRANSPORTE": "http"})
    with pytest.raises(modulo.HTTPException) as erro:
        asyncio.run(modulo.bancada_config())
    assert erro.value.status_code == 503
    assert "serial" in erro.value.detail


def test_o_balanca_vem_todo_nulo_sem_placa(carregar_adapter):
    """E isso não é erro: não há balança de bancada atrás do weight-simulator."""
    modulo = carregar_adapter("weight", env={"WEIGHT_TRANSPORTE": "http"})
    corpo = asyncio.run(modulo.balanca())
    assert corpo["transporte"] == "http"
    assert corpo["peso"] is None and corpo["cfg"] is None
    assert corpo["tara_confiavel"] is True


def test_comando_de_bancada_nao_passa_pela_rota_do_simulador():
    """`_COMANDOS_BANCADA` e `_ROTAS_SIM` são tabelas disjuntas.

    Uma entrada em `_ROTAS_SIM` promete as duas pernas — o mesmo comando por
    HTTP e por serial —, e o `weight-simulator` não tem balança para
    configurar: a perna HTTP daria 404 no transporte que é o default.
    """
    arvore = ast.parse(ADAPTER.read_text(encoding="utf-8"))
    tabelas = {}
    for no in ast.walk(arvore):
        if (isinstance(no, ast.Assign) and no.targets
                and isinstance(no.targets[0], ast.Name)
                and no.targets[0].id in ("_ROTAS_SIM", "_COMANDOS_BANCADA")):
            tabelas[no.targets[0].id] = {c.value for c in no.value.keys}

    assert set(tabelas) == {"_ROTAS_SIM", "_COMANDOS_BANCADA"}
    assert not (tabelas["_ROTAS_SIM"] & tabelas["_COMANDOS_BANCADA"])


def test_o_envio_de_bancada_tambem_passa_por_to_thread():
    """A mesma regra do `_enviar`: `Serial.write`/`read` são bloqueantes, e uma
    leitura pendurada congelaria o adapter inteiro — inclusive o `/ping` que o
    compose consulta como portão."""
    fonte = ADAPTER.read_text(encoding="utf-8")
    assert re.search(r"asyncio\.to_thread\(\s*_link\.enviar_comando", fonte)
    corpo = fonte.split("async def _enviar_bancada", 1)[1].split("\nasync def", 1)[0]
    assert "asyncio.to_thread(_link.enviar_comando" in corpo


# ══════════════════════════════════════════════════════════════════════════════
# 5. O firmware: o que a 2.3 prometeu e o que ela NÃO podia mexer
# ══════════════════════════════════════════════════════════════════════════════

def test_o_sketch_antigo_continua_intacto():
    """A 2.2 é a referência de bancada: ela é o que se grava quando a 2.3
    estiver sob suspeita."""
    assert SKETCH_ANTIGO.is_file()
    antigo = SKETCH_ANTIGO.read_text(encoding="utf-8")
    assert "FW_VERSION" not in antigo
    assert "evento" not in antigo


@pytest.mark.parametrize("simbolo", [
    "countByWeight", "roundCount", "calcNetWeight", "calcExactCount",
    "validateCountRange", "isWeightStable", "feedTransientBuffer",
    "resetTransientBuffer", "readRawMedian", "movavg", "hx_read_raw",
    "runTests",
])
def test_a_logica_de_contagem_e_do_hx711_sobreviveu_igual(firmware, simbolo):
    """A 2.3 acrescenta uma VOZ; ela não reescreve a balança.

    O corpo destas funções é comparado byte a byte com o da 2.2 no teste
    seguinte — aqui só se cobra que elas continuem existindo, para que "sumiu"
    não seja confundido com "não mudou".
    """
    assert f"{simbolo}(" in firmware


def test_as_funcoes_de_calculo_sao_identicas_as_da_22(firmware):
    """Byte a byte, e é isso que separa "acrescentou" de "reescreveu".

    Os testes T1..T13 do próprio sketch validam essas contas contra tolerância,
    faixa e arredondamento; reescrevê-las passaria despercebido aqui e daria
    uma contagem diferente na bancada, que é o número que o operador confere
    contra a prateleira.
    """
    antigo = SKETCH_ANTIGO.read_text(encoding="utf-8")

    def corpo(fonte: str, assinatura: str) -> str:
        inicio = fonte.index(assinatura)
        return fonte[inicio:fonte.index("\n}\n", inicio)]

    for assinatura in ("float calcNetWeight(",
                       "float calcExactCount(",
                       "int roundCount(",
                       "bool validateCountRange(",
                       "bool isWeightStable(",
                       "void feedTransientBuffer(",
                       "void resetTransientBuffer(",
                       "CountOutput countByWeight(",
                       "int runTests("):
        assert corpo(firmware, assinatura) == corpo(antigo, assinatura), assinatura


def test_o_firmware_nao_ganhou_arduinojson(firmware):
    """Sete mensagens não pagam uma dependência numa placa apertada de RAM — e
    `snprintf` deixa o teto da linha visível no ponto em que ele é conferido."""
    assert "#include <ArduinoJson" not in firmware
    assert "#include <time.h>" in firmware


def test_todo_countstate_passa_por_setcountstate(firmware):
    """Atribuir `countState` direto é a tela do operador parada num estado que
    a máquina já deixou para trás — e nada quebra por causa disso."""
    setter = _corpo(firmware, "void setCountState(")
    assert "countState = s;" in setter
    fora_do_setter = _sem_comentario(firmware).replace(setter, "")
    # `^\s*countState` de propósito: a declaração global é
    # `CountState countState = CountState::IDLE;`, começa com o TIPO, e não é
    # transição nenhuma — é o estado com que a placa nasce.
    assert not re.search(r"^\s*countState\s*=\s*CountState::",
                         fora_do_setter, re.M)


def test_o_stream_e_independente_do_autoprint(firmware):
    """Um é a voz do humano e o outro a da máquina: desligar `a` no Monitor
    Serial não pode calar o adapter."""
    laco = _corpo(firmware, "void loop()")
    assert "if (autoPrint)" in laco
    assert "if (streamOn &&" in laco
    # O stream NÃO está dentro do `if (autoPrint)`.
    dentro = laco.split("if (autoPrint) {", 1)[1].split("}", 1)[0]
    assert "emitPeso" not in dentro


def test_o_stream_sai_a_cinco_hz(firmware):
    assert re.search(r"#define STREAM_INTERVAL_MS\s+200", firmware)


def test_a_estabilidade_do_stream_nao_reusa_o_buffer_da_contagem(firmware):
    """`transientBuffer` e `transientStable` pertencem à máquina de contagem:
    um stream de fundo mexendo neles alteraria o resultado de uma contagem em
    andamento."""
    corpo = _corpo(firmware, "static void atualizarEstabilidade(")
    assert "transientBuffer" not in corpo
    assert "transientStable" not in corpo
    assert "streamEstaveis" in corpo


def test_o_teto_da_linha_do_firmware_e_o_do_transporte(firmware):
    """Dois números diferentes aqui produzem mensagens que somem sem rastro: o
    adapter descarta a linha acima do teto e não avisa ninguém do outro lado."""
    assert re.search(r"MAX_LINHA_BYTES\s*=\s*1024", firmware)
    serial_link = (RAIZ_REPO / "weight-adapter" / "serial_link.py").read_text(
        encoding="utf-8")
    assert re.search(r"MAX_LINHA_BYTES\s*=\s*1024", serial_link)


def test_o_os_id_cabe_inteiro_no_firmware(firmware):
    """`{template}-{AAAAMMDDTHHMMSS}-{6 hex}` passa de 36 caracteres, e dois
    disparos do mesmo template diferem no FIM da string: cortar o fim faz dois
    ids virarem um, e o `peso_ok` vai para a OS errada."""
    achado = re.search(r"#define MAX_OS_ID_LEN\s+(\d+)", firmware)
    assert achado, "MAX_OS_ID_LEN sumiu"
    assert int(achado.group(1)) >= 60, "o central grava `os_id` em VARCHAR(60)"


def test_todo_float_do_firmware_sai_por_fmtf(firmware):
    """`NaN`/`inf` não existem em JSON: emiti-los como `nan` faria o
    `json.loads` do adapter descartar a linha INTEIRA, e um campo estragado
    levaria junto os quinze que estavam certos."""
    corpo = _corpo(firmware, "static void fmtF(")
    assert "isnan" in corpo and "isinf" in corpo and "null" in corpo

    # E nenhum emissor formata float direto no `snprintf` da linha.
    secao = firmware.split("//   SAIDA PARA O PC CENTRAL\n", 1)[1]
    secao = secao.split("//   SETUP", 1)[0]
    emissores = secao.split("static void fmtF(", 1)[1]
    assert not re.search(r'\\"\w+\\":%\.\d+f', emissores), (
        "float indo direto para a linha, sem passar por fmtF")


def test_o_firmware_nao_manda_o_pc_usar_comando_bloqueante(firmware):
    """`u` e `c0..c3` bloqueiam em `readSerialLine()` por até 15 s esperando
    alguém digitar, e nesse tempo a placa não lê comando nem responde ACK."""
    assert "readSerialLine" in firmware          # continuam existindo p/ o humano
    # O caminho de máquina tem equivalente não bloqueante.
    assert '"peso_unitario"' in firmware
    assert "readSerialLine" not in _corpo(firmware, "static void cmdBancada(")


def test_as_duas_vozes_se_separam_pelo_abre_chaves(firmware):
    """É o único separador, e é o que mantém o Monitor Serial funcionando como
    na 2.2: comando de uma letra continua indo direto para `processCommand`."""
    corpo = _corpo(firmware, "void despacharLinha(")
    assert "indexOf('{')" in corpo
    assert "processarJson" in corpo and "processCommand" in corpo


def test_o_vocabulario_do_firmware_e_o_da_placa_falsa(firmware):
    """Nome divergente aqui não dá erro: dá uma tela que nunca sai de
    "desconhecido"."""
    for estado in ESTADOS:
        assert f'return "{estado}";' in firmware, estado
    for status in STATUS_CONTAGEM:
        assert f'return "{status}";' in firmware, status


def test_o_firmware_responde_ao_contrato_de_servico(firmware):
    """Ping, pong, ACK e envelope de evento: sem qualquer um deles a porta nem
    chega a ser identificada."""
    assert '"{\\"cmd\\":\\"ping\\",\\"sub\\":\\"weight\\"}"' in firmware
    assert '\\"resp\\":\\"ok\\",\\"cmd_id\\"' in firmware
    assert '\\"resp\\":\\"erro\\",\\"cmd_id\\"' in firmware
    assert '{\\"evento\\":{' in firmware
    assert '"pong"' in firmware and '"epoch"' in firmware


def test_o_contador_de_cmd_id_sobrevive_a_um_restart_do_adapter(firmware):
    """O `cmd_id` é monotônico DENTRO de uma sessão do adapter: um restart do
    processo o faz nascer em 1 de novo, e a placa veria esse 1 como repetição.

    O ACK sairia positivo, o comando NÃO executaria, e o orquestrador esperaria
    para sempre um evento que ninguém ia produzir — e, com o ciclo por relógio,
    o central ainda dispara o `dispensar` de um `mover` que nunca aconteceu.

    **Duas defesas, e a segunda é a que cobre o caso comum.** O silêncio de
    pongs (a placa só os recebe em resposta ao próprio ping) detecta o adapter
    que ficou fora por mais de 10 s; o restart RÁPIDO — 2 a 5 s, que é o que o
    `.bat` da bancada faz sozinho — não chega perto disso. Para ele existe
    `sessao`, o epoch de boot do processo, que viaja no pong e em todo comando.
    """
    corpo = _corpo(firmware, "static void processarJson(")
    assert "ultimoCmdId = 0" in corpo
    assert "SESSAO_SILENCIO_MS" in corpo
    assert "adotarSessao(linha)" in corpo


def test_a_sessao_e_adotada_antes_da_checagem_de_idempotencia(firmware):
    """Ordem, não presença: um comando da sessão NOVA tem de ser executado, não
    respondido como repetido. O pong também carrega `sessao`, mas o primeiro
    comando depois do restart pode chegar antes do primeiro pong."""
    corpo = _corpo(firmware, "static void processarJson(")
    # A adoção no caminho do COMANDO — a do pong sai antes, no ramo do `resp`.
    adocao = corpo.rindex("adotarSessao(linha)")
    checagem = corpo.index("cmd_id <= ultimoCmdId")

    assert adocao < checagem


# ══════════════════════════════════════════════════════════════════════════════
# A QUARTA cópia do enquadramento — comparada com `apsen_serial.h`
# ══════════════════════════════════════════════════════════════════════════════
#
# As outras três placas (dispensers, telas, mesa) incluem `apsen_serial.h`, e
# `tests/test_dispenser_firmware.py` cobra que as três cópias do header sejam
# byte a byte iguais. A balança **não** o inclui: ela foi o primeiro firmware a
# sair do papel, e o enquadramento, o ping, o ACK e o `cmd_id` estão escritos
# inline nela.
#
# Adotar o header aqui é o caminho certo e continua pendente — ele exige
# reescrever a camada serial de um firmware que já está GRAVADO e rodando na
# bancada, e isso é trabalho de quem consegue compilar e regravar. O que não
# podia continuar é a quarta cópia sem NADA comparando: divergir aqui não
# quebra nada visivelmente — a placa fala um dialeto e o adapter descarta as
# linhas em silêncio.
#
# Estes testes fecham essa lacuna sem depender da adoção: os NÚMEROS e as
# FORMAS do contrato de serviço são lidos do header e cobrados da balança.

HEADER = RAIZ_REPO / "dispenser" / "servos_hub" / "apsen_serial.h"


@pytest.fixture(scope="module")
def header() -> str:
    if not HEADER.is_file():
        pytest.fail(f"{HEADER} não está no disco — é o contrato das outras três")
    return HEADER.read_text(encoding="utf-8")


def _constante(texto: str, nome: str) -> str:
    """O valor de uma constante, nas DUAS formas que as cópias usam.

    `PING_INTERVAL_MS` é `#define` nos dois lados; `MAX_LINHA_BYTES` e
    `SESSAO_SILENCIO_MS` são `static const ... = <valor>;`. Aceitar só uma das
    formas faria o extrator "não achar" — e por isso ele GRITA em vez de
    devolver vazio: extrator que devolve nada deixa a comparação passar sempre.
    """
    for padrao in (rf"#define\s+{nome}\s+([^\s/]+)",
                   rf"{nome}\s*=\s*([^;]+);"):
        achado = re.search(padrao, texto)
        if achado:
            return achado.group(1).strip()
    raise AssertionError(f"`{nome}` não foi encontrada — o extrator quebrou")


@pytest.mark.parametrize("nome", ["MAX_LINHA_BYTES", "PING_INTERVAL_MS",
                                  "SESSAO_SILENCIO_MS"])
def test_os_numeros_do_servico_sao_os_do_header(firmware, header, nome):
    """Um teto diferente de linha parte a mensagem de um lado e não do outro;
    um `SESSAO_SILENCIO_MS` diferente faz a balança ser a única placa que não
    percebe o adapter voltar."""
    assert _constante(firmware, nome) == _constante(header, nome)


# As formas do ACK, com as aspas ESCAPADAS como o C++ as escreve. Montadas por
# concatenação em vez de escritas inteiras: uma barra invertida a mais ou a
# menos aqui não falha — casa com nada, e o teste fica verde para sempre.
_AS = chr(92) + chr(34)      # a sequência barra-invertida + aspas

ACKS = [
    _AS + "resp" + _AS + ":" + _AS + "ok" + _AS + "," + _AS + "cmd_id" + _AS
    + ":%ld," + _AS + "repetido" + _AS + ":true",
    _AS + "resp" + _AS + ":" + _AS + "ok" + _AS + "," + _AS + "cmd_id" + _AS + ":%ld",
    _AS + "resp" + _AS + ":" + _AS + "erro" + _AS + "," + _AS + "cmd_id" + _AS
    + ":%ld," + _AS + "msg" + _AS + ":" + _AS + "%s" + _AS,
]


@pytest.mark.parametrize("forma", ACKS)
def test_as_formas_do_ack_sao_as_do_header(firmware, header, forma):
    """O adapter casa o ACK pelo `cmd_id` e decide por `resp`. Um campo a menos
    aqui é um comando que fica esperando ACK até o prazo vencer."""
    assert forma in header, "o extrator quebrou: a forma sumiu do header"
    assert forma in firmware


def test_a_adocao_de_sessao_e_a_mesma_regra_do_header(firmware, header):
    """Comparação por DIFERENÇA e não por ordem, e a primeira sessão adotada sem
    zerar nada. Duas linhas, e as duas decidem se um comando executa."""
    for copia in (firmware, header):
        corpo = _corpo(copia, "adotarSessao(const char")
        assert "sessaoAdapter != 0" in corpo, "a primeira sessão zeraria o contador"
        assert "nova != sessaoAdapter" in corpo, "comparação por ordem, não por diferença"
        assert "ultimoCmdId = 0" in corpo


def test_a_regra_de_idempotencia_e_a_mesma_do_header(firmware, header):
    for copia in (firmware, header):
        assert "cmd_id <= ultimoCmdId" in copia
        assert "ackOk(cmd_id, true)" in copia
