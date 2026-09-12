# -*- coding: utf-8 -*-
"""
O protocolo serial display ↔ backend existe em TRÊS cópias, e nada as unia.

O display de 7" fala com o `painel_operador` por uma linha JSON por mensagem,
pelo cabo USB. Esse contrato é escrito à mão em três lugares, **em duas
linguagens**:

  * `painel_operador/firmware/src/main.cpp`      — C++, quem PERGUNTA
  * `painel_operador/backend/app.py`             — Python, quem RESPONDE
  * `painel_operador/firmware/simulador_serial.py` — Python, o duplo sem placa

Divergir aqui **não quebra nada visivelmente**. O display manda um `cmd` que o
backend não conhece e fica esperando até o `serial_request` estourar; ou o
backend responde com uma tag de `resp` que o display não aguarda, e a resposta é
descartada em silêncio (`s2_awaited_resp_type` não casa). Os dois sintomas são o
mesmo: tela vazia, display `OFFLINE`, e nada no log dizendo por quê — que é
exatamente o modo de falhar que este repositório persegue em toda parte.

É a mesma família de `tests/test_adapters.py` (quatro `_post_central`
comparados entre si) e de `tests/test_injecao.py` (quatro cópias das strings de
tipo contra o catálogo). A diferença é que aqui uma das cópias é C++, então a
comparação é por extração de texto — e a extração TEM que ser conferida, senão
o teste vira verde permanente. O último bloco deste arquivo é essa guarda.

Nota sobre o firmware: ele não compila na suíte, e nenhum teste finge que
compila. O que se prende aqui é o CONTRATO, que é texto dos dois lados.
"""
import json
import re
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent
PAINEL = RAIZ_REPO / "painel_operador"
FIRMWARE = PAINEL / "firmware" / "src" / "main.cpp"
BACKEND = PAINEL / "backend" / "app.py"
SIMULADOR = PAINEL / "firmware" / "simulador_serial.py"


def _ler(caminho: Path) -> str:
    if not caminho.is_file():
        pytest.skip(f"{caminho.relative_to(RAIZ_REPO)} não está no disco")
    return caminho.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def fw() -> str:
    return _ler(FIRMWARE)


@pytest.fixture(scope="module")
def be() -> str:
    return _ler(BACKEND)


@pytest.fixture(scope="module")
def sim() -> str:
    return _ler(SIMULADOR)


def _achar(texto: str, *padroes: str) -> set:
    encontrados: set = set()
    for padrao in padroes:
        encontrados |= set(re.findall(padrao, texto))
    return encontrados


def _tabela(texto: str, nome: str) -> dict:
    """Lê um dicionário literal `NOME = { "a": "b", ... }` do fonte.

    O backend e o simulador mapeiam `cmd de leitura -> tag do resp` numa tabela
    em vez de num `if` por comando; ignorá-la faria o teste "não achar" quatro
    comandos que existem.
    """
    achado = re.search(re.escape(nome) + r"\s*=\s*\{(.*?)\n\}", texto, re.S)
    if not achado:
        return {}
    corpo = achado.group(1)
    # `"get_ordens": "ordens"` (backend) e `"get_ordens": ("ordens", DADOS)`
    # (simulador) — a tag é a primeira string depois dos dois-pontos.
    return dict(re.findall(r'"(\w+)"\s*:\s*\(?\s*"(\w+)"', corpo))


# ══════════════════════════════════════════════════════════════════════════════
# Extração — cada função devolve o que UM lado fala
# ══════════════════════════════════════════════════════════════════════════════

def fw_cmds_enviados(fw: str) -> set:
    """`cmd` que o firmware manda ao backend.

    Duas formas no fonte, porque o firmware usa as duas: literal dentro de
    `serial_request("{\\"cmd\\":\\"ping\\"}", ...)` e montado com ArduinoJson
    (`doc["cmd"] = "set_status"`).

    As linhas de ACK são excluídas: `{"ack":"ok","cmd":"nova_ordem"}` ecoa qual
    comando de DEBUG foi aceito — é resposta, não pergunta. Sem essa exclusão o
    teste acusaria `nova_ordem` e `dispenser` como comandos que o backend
    deveria tratar, e eles nunca saem do display.
    """
    sem_ack = "\n".join(l for l in fw.splitlines() if r'\"ack\"' not in l)
    return _achar(sem_ack,
                  r'\\"cmd\\":\\"(\w+)\\"',
                  r'doc\["cmd"\]\s*=\s*"(\w+)"')


def fw_resps_esperados(fw: str) -> set:
    """Tag de `resp` que o firmware aguarda — 2º argumento de `serial_request`."""
    return _achar(fw, r'serial_request\s*\(\s*[^,]+,\s*"(\w+)"')


def fw_events_emitidos(fw: str) -> set:
    """`event` que o firmware empurra sem ser perguntado.

    Também em duas formas: ArduinoJson e `snprintf` com o JSON escrito à mão
    (é o caso de `ordem_concluida`).
    """
    return _achar(fw,
                  r'doc\["event"\]\s*=\s*"(\w+)"',
                  r'\\"event\\":\\"(\w+)\\"')


def fw_pushes_tratados(fw: str) -> set:
    """`push` que o firmware reconhece. A variável é `push_type`, não `push`."""
    return _achar(fw, r'strcmp\s*\(\s*push_type\s*,\s*"(\w+)"\s*\)')


def fw_cmds_debug_aceitos(fw: str) -> set:
    """Comandos de DEBUG que o firmware aceita do simulador manual."""
    corpo = fw.split("handle_debug_cmd", 1)[-1]
    return _achar(corpo, r'strcmp\s*\(\s*cmd\s*,\s*"(\w+)"\s*\)\s*==\s*0')


def be_cmds_tratados(be: str) -> set:
    return (_achar(be, r'cmd\s*==\s*"(\w+)"')
            | _tuplas(be, "cmd")
            | set(_tabela(be, "_GET_RESP_TAG")))


def be_resps_emitidos(be: str) -> set:
    return (_achar(be, r'"resp"\s*:\s*"(\w+)"')
            | set(_tabela(be, "_GET_RESP_TAG").values()))


def be_pushes_enviados(be: str) -> set:
    return _achar(be, r'"push"\s*:\s*"(\w+)"')


def be_events_tratados(be: str) -> set:
    return _achar(be, r'event\s*==\s*"(\w+)"') | _tuplas(be, "event")


def sim_cmds_tratados(sim: str) -> set:
    return (_achar(sim, r'cmd\s*==\s*"(\w+)"')
            | _tuplas(sim, "cmd")
            | set(_tabela(sim, "_RESPOSTAS_GET")))


def sim_resps_emitidos(sim: str) -> set:
    return (_achar(sim, r'"resp"\s*:\s*"(\w+)"')
            | set(_tabela(sim, "_RESPOSTAS_GET").values()))


def sim_cmds_debug_enviados(sim: str) -> set:
    return _achar(sim, r'"cmd"\s*:\s*"(\w+)"')


def _tuplas(texto: str, variavel: str) -> set:
    """`cmd in ("sync_dispensers", "set_dispenser_med")` — despacho por tupla.

    Sem isto, dois comandos do backend e do simulador ficariam invisíveis.
    """
    encontrados: set = set()
    for grupo in re.findall(re.escape(variavel) + r'\s+in\s*\(([^)]*)\)', texto):
        encontrados |= set(re.findall(r'"(\w+)"', grupo))
    return encontrados


# ══════════════════════════════════════════════════════════════════════════════
# 1. Firmware ↔ backend: o par que roda na bancada
# ══════════════════════════════════════════════════════════════════════════════

def test_todo_cmd_do_firmware_tem_tratamento_no_backend(fw, be):
    """Comando sem tratamento = display esperando até o timeout, tela vazia."""
    faltando = fw_cmds_enviados(fw) - be_cmds_tratados(be)
    assert not faltando, (
        f"o display manda {sorted(faltando)} e o backend não trata — o "
        f"`serial_request` vai estourar o timeout e a tela fica vazia"
    )


def test_todo_resp_esperado_pelo_firmware_e_emitido_pelo_backend(fw, be):
    """Tag errada = resposta descartada em silêncio.

    `handle_serial_line` só aceita a resposta quando ela casa com
    `s2_awaited_resp_type`. Uma tag diferente não vira erro: a linha é
    simplesmente ignorada, e o display espera o timeout inteiro.
    """
    faltando = fw_resps_esperados(fw) - be_resps_emitidos(be)
    assert not faltando, (
        f"o display aguarda resp {sorted(faltando)} que o backend nunca emite — "
        f"a resposta seria descartada sem erro nenhum"
    )


def test_todo_push_do_backend_e_reconhecido_pelo_firmware(fw, be):
    """Push não reconhecido cai no `return` silencioso do `handle_serial_line`."""
    faltando = be_pushes_enviados(be) - fw_pushes_tratados(fw)
    assert not faltando, (
        f"o backend empurra push {sorted(faltando)} que o firmware ignora"
    )


def test_todo_event_do_firmware_e_tratado_pelo_backend(fw, be):
    """Evento é fire-and-forget: o display não espera resposta.

    Por isso um evento ignorado não trava nada — ele só some. É o caso mais
    silencioso dos quatro, e o que mais precisa de uma guarda.
    """
    faltando = fw_events_emitidos(fw) - be_events_tratados(be)
    assert not faltando, (
        f"o display emite event {sorted(faltando)} que o backend descarta"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. Firmware ↔ simulador: o par que roda sem placa
# ══════════════════════════════════════════════════════════════════════════════

def test_simulador_responde_a_todo_cmd_do_firmware(fw, sim):
    """O simulador existe para exercitar o protocolo sem hardware.

    Se ele ficar para trás de um comando novo, o display fica OFFLINE nos testes
    de bancada e `fetch_ordens_api` nunca roda — que é justamente o caminho por
    onde o `os_id` longo do central e o campo `origem` chegam.
    """
    faltando = fw_cmds_enviados(fw) - sim_cmds_tratados(sim)
    assert not faltando, (
        f"o simulador não responde a {sorted(faltando)} — sem placa, esse "
        f"caminho fica sem cobertura nenhuma"
    )


def test_simulador_emite_todo_resp_que_o_firmware_espera(fw, sim):
    faltando = fw_resps_esperados(fw) - sim_resps_emitidos(sim)
    assert not faltando, f"o simulador nunca emite resp {sorted(faltando)}"


def test_firmware_aceita_todo_cmd_de_debug_do_simulador(fw, sim):
    """A outra metade do simulador: ele também EMPURRA comandos de debug."""
    faltando = sim_cmds_debug_enviados(sim) - fw_cmds_debug_aceitos(fw)
    assert not faltando, (
        f"o simulador empurra {sorted(faltando)} e o firmware responde "
        f'"cmd desconhecido"'
    )


def test_backend_e_simulador_tratam_o_mesmo_conjunto(be, sim):
    """Os dois respondem ao MESMO display, então divergir é ter dois contratos.

    O simulador é o duplo do backend; se ele aceitar um comando a mais ou a
    menos, um teste de bancada passa e a bancada real falha — ou o contrário.
    """
    assert be_cmds_tratados(be) == sim_cmds_tratados(sim), {
        "só no backend": sorted(be_cmds_tratados(be) - sim_cmds_tratados(sim)),
        "só no simulador": sorted(sim_cmds_tratados(sim) - be_cmds_tratados(be)),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3. O contrato completo, escrito por extenso
# ══════════════════════════════════════════════════════════════════════════════
#
# As listas acima provam que os três lados CONCORDAM; esta prova que eles
# concordam no conjunto CERTO. Sem ela, apagar o mesmo comando dos três lugares
# passaria — e o repositório já registra esse formato de bug na seção
# "Estado terminal novo é slot que ninguém libera".

CMDS = {
    "ping", "get_ordens", "get_catalogo", "get_operadores", "get_dispensers",
    "validar_operador", "set_status", "sync_dispensers", "set_dispenser_med",
}
RESPS = {"pong", "ordens", "catalogo", "operadores", "dispensers", "operador", "ok"}
EVENTS = {"historico", "desvio", "ordem_concluida"}
PUSHES = {"ordem_status", "dispensers"}
CMDS_DEBUG = {"nova_ordem", "dispenser", "status"}


def test_o_conjunto_de_comandos_e_exatamente_este(fw, be):
    assert fw_cmds_enviados(fw) == CMDS
    assert be_cmds_tratados(be) == CMDS


def test_o_conjunto_de_respostas_e_exatamente_este(fw, be):
    assert fw_resps_esperados(fw) == RESPS
    assert be_resps_emitidos(be) == RESPS


def test_o_conjunto_de_eventos_e_pushes_e_exatamente_este(fw, be):
    assert fw_events_emitidos(fw) == EVENTS
    assert be_events_tratados(be) == EVENTS
    assert be_pushes_enviados(be) == PUSHES
    assert fw_pushes_tratados(fw) == PUSHES


def test_os_cmds_de_debug_sao_exatamente_estes(fw, sim):
    assert fw_cmds_debug_aceitos(fw) == CMDS_DEBUG
    assert sim_cmds_debug_enviados(sim) == CMDS_DEBUG


def test_o_bloco_de_documentacao_do_firmware_lista_o_protocolo_real(fw):
    """O comentário no topo da seção serial é o que alguém lê antes de mexer.

    Ele já esteve desatualizado: não listava `validar_operador` nem o push
    `dispensers`. Documentação que mente sobre o contrato é pior que
    documentação nenhuma — quem lê para de conferir o código.
    """
    bloco = fw.split("SERIAL USB", 1)[1].split("static char s2_buf", 1)[0]
    for cmd in CMDS:
        assert f'"cmd":"{cmd}"' in bloco, f"{cmd} não está documentado no bloco"
    for push in PUSHES:
        assert f'"push":"{push}"' in bloco, f"push {push} não está documentado"
    for evento in EVENTS:
        assert f'"event":"{evento}"' in bloco, f"event {evento} não documentado"


# ══════════════════════════════════════════════════════════════════════════════
# 4. Guarda da guarda: a extração precisa ENXERGAR
# ══════════════════════════════════════════════════════════════════════════════
#
# Este bloco existe porque a primeira versão desta comparação foi escrita com
# regex ingênuo e acusou QUATRO divergências que não existiam — e o erro oposto
# é pior: um regex que não casa com nada deixa todos os testes acima verdes para
# sempre, inclusive com o protocolo quebrado.

@pytest.mark.parametrize("extrator,minimo", [
    ("fw_cmds_enviados", 9),
    ("fw_resps_esperados", 7),
    ("fw_events_emitidos", 3),
    ("fw_pushes_tratados", 2),
    ("fw_cmds_debug_aceitos", 3),
    ("be_cmds_tratados", 9),
    ("be_resps_emitidos", 7),
    ("be_pushes_enviados", 2),
    ("be_events_tratados", 3),
    ("sim_cmds_tratados", 9),
    ("sim_resps_emitidos", 7),
    ("sim_cmds_debug_enviados", 3),
])
def test_cada_extrator_acha_o_que_deve(fw, be, sim, extrator, minimo):
    """Extrator que para de casar deixa a comparação verde para sempre."""
    fonte = {"fw": fw, "be": be, "sim": sim}[extrator.split("_")[0]]
    achado = globals()[extrator](fonte)
    assert len(achado) >= minimo, (
        f"{extrator} achou só {len(achado)} ({sorted(achado)}) — esperado ao "
        f"menos {minimo}. O padrão de busca provavelmente parou de casar com o "
        f"fonte, e a comparação virou verde permanente."
    )


def test_o_ack_nao_e_confundido_com_comando(fw):
    """A exclusão que custou a primeira rodada de falsos positivos.

    `{"ack":"ok","cmd":"nova_ordem"}` é o firmware CONFIRMANDO um comando de
    debug recebido. Contá-lo como comando enviado faria o teste exigir que o
    backend tratasse `nova_ordem` — que o display nunca manda.
    """
    assert r'\"ack\"' in fw, "as linhas de ack sumiram; revise `fw_cmds_enviados`"
    assert "nova_ordem" not in fw_cmds_enviados(fw)
    assert "dispenser" not in fw_cmds_enviados(fw)
    # E elas continuam sendo comandos de DEBUG válidos, pelo outro lado.
    assert {"nova_ordem", "dispenser"} <= fw_cmds_debug_aceitos(fw)


def test_baud_do_firmware_e_o_do_backend_e_o_do_simulador(fw, be, sim):
    """115200 nos três. Divergir aqui dá lixo na linha, não erro.

    E o `platformio.ini` entra na conta: sem `monitor_speed`, o monitor abre a
    9600 e mostra lixo — sintoma que parece firmware quebrado.
    """
    assert "Serial.begin(115200)" in _ler(PAINEL / "firmware" / "src" / "display.h")
    assert re.search(r"SERIAL_BAUD\s*=\s*115200", be)
    assert re.search(r"BAUD_RATE\s*=\s*115200", sim)
    ini = _ler(PAINEL / "firmware" / "platformio.ini")
    assert re.search(r"monitor_speed\s*=\s*115200", ini)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Runtime: o backend responde de verdade a cada comando
# ══════════════════════════════════════════════════════════════════════════════
#
# Os blocos acima provam que os NOMES concordam. Este prova que o backend
# realmente atende cada um — contra o SQLite de verdade, pelo mesmo
# `_handle_serial_message` que a thread da ponte serial chama. Nome certo com
# handler quebrado daria os mesmos sintomas de nome errado.


class ConexaoFake:
    """Duplo da `serial.Serial`: guarda o que o backend escreveria no cabo."""

    def __init__(self):
        self.escrito: list = []

    def write(self, dados):
        self.escrito.append(dados.decode())

    def flush(self):
        pass


def _responder(painel, msg: dict) -> tuple:
    """Passa uma mensagem pelo dispatch real e devolve (linha crua, dict)."""
    conn = ConexaoFake()
    painel.modulo._handle_serial_message(conn, msg)
    assert conn.escrito, f"o backend não respondeu nada a {msg}"
    bruto = conn.escrito[0]
    return bruto, json.loads(bruto)


# cmd -> tag de `resp` que o firmware aguarda (a tabela do `_GET_RESP_TAG`,
# escrita aqui à mão de propósito: é o contrato visto de fora).
GET_ESPERADO = {
    "get_ordens": "ordens",
    "get_catalogo": "catalogo",
    "get_operadores": "operadores",
    "get_dispensers": "dispensers",
}


@pytest.mark.parametrize("cmd,tag", sorted(GET_ESPERADO.items()))
def test_cada_get_responde_com_a_tag_e_uma_lista(carregar_painel, cmd, tag):
    painel = carregar_painel()
    _, resposta = _responder(painel, {"cmd": cmd})
    assert resposta["resp"] == tag
    assert isinstance(resposta["data"], list), f"{cmd} devolveu {type(resposta['data'])}"


def test_ping_responde_pong_com_epoch(carregar_painel):
    """O `ping` é o handshake que faz o backend DESCOBRIR a porta do display —
    e o `epoch` é como o display acerta o relógio sem NTP."""
    painel = carregar_painel()
    _, resposta = _responder(painel, {"cmd": "ping"})
    assert resposta["resp"] == "pong"
    assert isinstance(resposta["epoch"], int) and resposta["epoch"] > 0


@pytest.mark.parametrize("cmd,extra", [
    ("set_status", {"numero_os": "NAO-EXISTE", "status": "Pendente"}),
    ("sync_dispensers", {"itens": []}),
    ("set_dispenser_med", {"slot": 1, "nome": "X"}),
])
def test_comandos_de_escrita_respondem_ok_com_veredito(carregar_painel, cmd, extra):
    """`resp: "ok"` é a TAG, não o veredito — quem diz se deu certo é o campo
    `ok`. Confundir os dois faria o display comemorar uma escrita recusada."""
    painel = carregar_painel()
    _, resposta = _responder(painel, {"cmd": cmd, **extra})
    assert resposta["resp"] == "ok"
    assert isinstance(resposta["ok"], bool)


def test_toda_resposta_e_uma_linha_json_terminada_em_newline(carregar_painel):
    """O parser do firmware quebra por `\n` e exige que a linha comece com `{`.

    Uma resposta com quebra de linha no meio viraria duas linhas: a primeira
    JSON inválido, a segunda sem `{` — e as duas descartadas em silêncio.
    """
    painel = carregar_painel()
    for cmd in sorted(CMDS):
        bruto, _ = _responder(painel, {"cmd": cmd, "itens": [], "slot": 1,
                                       "nome": "X", "numero_os": "N",
                                       "status": "Pendente", "pin": "0000"})
        assert bruto.endswith("\n"), cmd
        assert bruto.count("\n") == 1, f"{cmd} respondeu em mais de uma linha"
        assert bruto.startswith("{"), cmd


def test_nenhuma_resposta_estoura_o_buffer_do_firmware(carregar_painel):
    """`static char s2_buf[4096]` — e o firmware DESCARTA a linha que não couber.

    É a mesma família do truncamento de `os_id` que o CLAUDE.md registra: o
    limite tem que sair da ORIGEM do dado, não do tamanho que ele tem hoje. Com
    a célula cheia, `get_dispensers` e `get_catalogo` são as duas maiores.
    """
    painel = carregar_painel()
    for cmd in sorted(GET_ESPERADO):
        bruto, _ = _responder(painel, {"cmd": cmd})
        assert len(bruto.encode("utf-8")) < 4096, (
            f"{cmd} respondeu {len(bruto)} bytes e o buffer do firmware é 4096 — "
            f"a linha seria descartada sem erro nenhum"
        )


def test_cmd_desconhecido_nao_deixa_o_display_esperando(carregar_painel):
    """Silêncio aqui custa o timeout inteiro do `serial_request` na tela."""
    painel = carregar_painel()
    conn = ConexaoFake()
    painel.modulo._handle_serial_message(conn, {"cmd": "comando_que_nao_existe"})
    # Pode responder erro ou nada, mas se responder tem que ser JSON de uma linha.
    for bruto in conn.escrito:
        assert bruto.endswith("\n") and json.loads(bruto)


def test_pin_nunca_volta_na_resposta(carregar_painel):
    """O PIN sobe do display para o backend; ele não pode voltar pelo cabo."""
    painel = carregar_painel()
    bruto, _ = _responder(painel, {"cmd": "validar_operador",
                                   "nome": "Administrador", "pin": "4321"})
    assert "4321" not in bruto
