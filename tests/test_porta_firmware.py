# -*- coding: utf-8 -*-
"""O firmware JSON da porta NR-12 (`porta/porta_nr12/`) — o que só existe nele.

**Não é o que está gravado em campo.** Desde 22/10/2026 a porta em campo é o
`porta/nr12_oficial/` com o ESP receptor, que falam texto e não recebem nada do
PC (§10); quem os confere é `tests/test_porta_texto.py`. Este arquivo continua
cobrando o firmware JSON, que segue no repositório — o texto abaixo descreve
ESSE firmware, no presente dele.

O que vale para toda placa que inclui `apsen_serial.h` (as quatro cópias
idênticas, o subsistema declarado, o laço que não lê a serial por conta
própria, o buffer antes do `begin`) já é cobrado em
`tests/test_dispenser_firmware.py`, que passou a incluir a porta. Aqui fica o
que é DESTA placa:

  * **uma USB, duas vozes.** A porta tem USB própria no mini PC (o dono é o
    `porta-adapter`), e máquina e bancada dividem essa USB, separadas pelo
    '{' — a regra do header nas outras placas. Quem lê a USB é o
    `serialPoll()` do header, e só ele: um segundo leitor no `.ino` ficaria
    com metade dos bytes de um JSON. Os comandos de bancada (R/S/C/L/E)
    chegam por `linhaHumana()`, uma letra por linha. Nada de Serial2 nem de
    `APSEN_SERIAL`: a UART com a placa das telas saiu, e GPIO16/17 ficaram
    livres;
  * **não há fechadura, e o "pode abrir" é do central.** A porta é presa
    por ímã: quem puxa, abre. O que a placa controla é a AUTORIZAÇÃO, e o
    sinal dela é o LED — aceso só em LIBERADA, que só o `porta_liberar` alcança;
    e nenhum pino de fechadura voltou ao sketch;
  * **o contrato do §9**, campo por campo, contra `docs/PROTOCOLO_SERIAL.md`.

Lê C++ por texto, como os vizinhos: cada extrator tem PISO, porque um extrator
que para de casar deixa tudo verde para sempre.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_dispenser_firmware import _corpo_da_funcao, sem_comentarios

RAIZ_REPO = Path(__file__).resolve().parent.parent
SKETCH = RAIZ_REPO / "porta" / "porta_nr12" / "porta_nr12.ino"
DOC = RAIZ_REPO / "docs" / "PROTOCOLO_SERIAL.md"

# Os dois eventos de vigia são do `porta-adapter`, não da porta — quem perdeu
# o link não tem como avisar que o perdeu. O firmware da porta não os emite, e
# é isso que o teste do contrato tem de saber.
EVENTOS_DO_ADAPTER = {"porta_link_perdido", "porta_link_ok"}


@pytest.fixture(scope="module")
def fonte() -> str:
    if not SKETCH.is_file():
        pytest.fail(f"{SKETCH} não está no disco — é ele que grava na placa")
    return SKETCH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def codigo(fonte) -> str:
    return sem_comentarios(fonte)


# ── Extração do documento (§9) ───────────────────────────────────────────────

def _secao_porta() -> str:
    texto = DOC.read_text(encoding="utf-8")
    assert "## 9. `porta`" in texto, "o §9 da porta sumiu do documento"
    return texto.split("## 9. `porta`", 1)[1].split("\n## ", 1)[0]


def _tabela(bloco: str) -> dict[str, list[str]]:
    linhas = {}
    for crua in bloco.splitlines():
        crua = crua.strip()
        if not crua.startswith("|"):
            continue
        colunas = [c.strip() for c in crua.strip("|").split("|")]
        nome = re.findall(r"`(\w+)`", colunas[0])
        if not nome or colunas[0] in ("`cmd`", "`tipo`"):
            continue
        linhas[nome[0]] = re.findall(r"`(\w+)`", colunas[1]) if len(colunas) > 1 else []
    return linhas


def doc_comandos() -> dict[str, list[str]]:
    bloco = _secao_porta().split("### Comandos", 1)[1].split("### ", 1)[0]
    return _tabela(bloco)


def doc_eventos() -> dict[str, list[str]]:
    bloco = _secao_porta().split("### Eventos", 1)[1].split("**Vocabulário", 1)[0]
    return _tabela(bloco)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Uma USB, duas vozes
# ══════════════════════════════════════════════════════════════════════════════

def test_a_voz_de_maquina_e_a_usb_sem_serial2_nem_macro(fonte, codigo):
    """A porta fala pela USB, como as outras placas. Um `APSEN_SERIAL`
    redefinido ou um Serial2 de volta seria a UART com a placa das telas
    voltando por um merge — e o adapter, na USB, ouviria silêncio."""
    assert "Serial2" not in fonte, "Serial2 voltou ao sketch da porta"
    assert "APSEN_SERIAL" not in fonte, "o sketch redefine a porta da voz de máquina"
    assert "PIN_UART" not in codigo
    setup = _corpo_da_funcao(codigo, "void setup()")
    assert "Serial.begin(115200)" in setup


def test_gpio16_e_17_ficaram_livres(codigo):
    """Eram a UART com a placa das telas. Nenhum `#define` de pino usa 16/17."""
    pinos = {int(n) for n in re.findall(r"#define\s+PIN_\w+\s+(\d+)", codigo)}
    assert len(pinos) >= 8, f"o extrator achou só {sorted(pinos)}"
    assert not ({16, 17} & pinos), sorted(pinos)


def test_o_ino_nao_le_a_usb_por_conta_propria(codigo):
    """Quem lê a USB é o `serialPoll()` do header, e só ele.

    Com a bancada e a máquina na MESMA USB, o antigo `tratarBancada()` — que
    lia `Serial` caractere a caractere — viraria um segundo leitor da mesma
    linha: cada um ficaria com metade dos bytes de um JSON, e as duas metades
    sumiriam sem erro em lugar nenhum.
    """
    for proibido in ("Serial.read", "Serial.available", "Serial.readString",
                     "tratarBancada"):
        assert proibido not in codigo, proibido
    assert "serialPoll();" in _corpo_da_funcao(codigo, "void loop()")


def test_os_comandos_de_bancada_chegam_por_linha_humana(codigo):
    """R/S/C/L/E saem de `linhaHumana()` — a linha SEM '{' que o header já
    separou na linha crua. Uma letra por linha, em maiúscula ou minúscula,
    espaços ignorados; mais de uma letra não vira sequência de comandos."""
    humana = _corpo_da_funcao(codigo, "void linhaHumana(char* linha)")
    assert "comandoBancada(" in humana
    assert "maiuscula(" in humana
    assert "' '" in humana, "a linha humana deixou de ignorar espaço"
    assert "letras > 1" in humana, "mais de uma letra virou sequência de comandos"
    bancada = _corpo_da_funcao(codigo, "static void comandoBancada(char c)")
    for letra in "RSCLE":
        assert f"case '{letra}'" in bancada, f"o comando de bancada {letra} sumiu"
    # Só `linhaHumana` chama o comando de bancada: um segundo caminho seria
    # uma segunda porta de entrada da voz do humano.
    assert codigo.count("comandoBancada(") == 2, "definição + a chamada em linhaHumana"


def test_nenhum_log_humano_leva_chave(fonte):
    """Linha com '{' é voz de MÁQUINA do outro lado do fio. Um log que
    levasse '{' viraria uma tentativa de JSON no adapter — descartada como
    inválida no melhor caso, e lida como mensagem no pior."""
    chamadas = re.findall(r"(?:logMsg|Serial\.print(?:ln|f)?)\((.*)", fonte)
    assert len(chamadas) >= 20, f"o extrator achou só {len(chamadas)} logs"
    for chamada in chamadas:
        if chamada.startswith("const char* tag"):
            continue                      # a definição de logMsg
        literais = re.findall(r'"((?:\\.|[^"\\])*)"', chamada)
        for literal in literais:
            assert "{" not in literal, chamada


# ══════════════════════════════════════════════════════════════════════════════
# 2. Sem fechadura: o LED "pode abrir" é o sinal da autorização
# ══════════════════════════════════════════════════════════════════════════════

def test_nao_ha_fechadura_eletrica_no_sketch(codigo):
    """A porta é presa por ímã simples, e o fim de curso é o único sensor dela.
    Um pino de fechadura que voltasse seria uma peça que a bancada não tem."""
    assert "olenoide" not in codigo and "PIN_TRAVA" not in codigo
    assert "GPIO26" not in codigo and "#define PIN_LED_OK   25" in codigo


def test_so_o_porta_liberar_leva_a_liberada(codigo):
    """O central decide se a abertura é autorizada. Um segundo caminho até
    LIBERADA acenderia o "pode abrir" com a máquina andando."""
    chegadas = re.findall(r"mudarEstado\(LIBERADA\)", codigo)
    assert len(chegadas) == 1, f"{len(chegadas)} caminhos chegam a LIBERADA"
    liberar = _corpo_da_funcao(codigo, "static void cmdPortaLiberar(")
    assert "mudarEstado(LIBERADA)" in liberar
    # E só para um pedido pendente: autorizar uma porta que não pediu nada
    # seria acender o "pode abrir" por um comando atrasado.
    assert liberar.index("estado != AGUARDANDO_CENTRAL") < liberar.index("mudarEstado(LIBERADA)")


def test_o_led_segue_o_estado_e_nasce_apagado(codigo):
    led = _corpo_da_funcao(codigo, "void atualizarLed()")
    assert "estado == LIBERADA ? HIGH : LOW" in led
    assert "atualizarLed();" in _corpo_da_funcao(codigo, "void loop()")
    assert "atualizarLed();" in _corpo_da_funcao(codigo, "static void mudarEstado(")
    setup = _corpo_da_funcao(codigo, "void setup()")
    assert setup.index("digitalWrite(PIN_LED_OK, LOW)") < setup.index("Serial.begin"), (
        "o \"pode abrir\" tem de nascer apagado antes de qualquer outra coisa")
    # Ninguém mais escreve no pino: o LED é o estado, não um flag a parte.
    assert codigo.count("digitalWrite(PIN_LED_OK") == 2


def test_es2_e_uma_constante_unica_com_o_aviso_do_pino_de_boot(fonte):
    assert fonte.count("#define PIN_ENDSTOP2") == 1
    assert "mude para 33 (D2 e pino de boot)" in fonte


def test_a_espera_pelo_central_cabe_o_teto_da_parada():
    """A porta desiste do pedido em ESPERA_CENTRAL_MS. Se ela fosse MENOR que
    o teto de `PORTA_PARADA_MAX_S` do central, a porta abandonaria um pedido
    que o central ia aceitar — e o `porta_liberar` atrasado seria recusado."""
    texto = SKETCH.read_text(encoding="utf-8")
    espera = int(re.search(r"ESPERA_CENTRAL_MS\s*=\s*(\d+)", texto).group(1))
    config = (RAIZ_REPO / "central-computer" / "config.py").read_text(encoding="utf-8")
    teto = float(re.search(r"PORTA_PARADA_MAX_S_TETO\s*=\s*([\d.]+)", config).group(1))
    assert espera > teto * 1000 + 500


# ══════════════════════════════════════════════════════════════════════════════
# 3. O contrato do §9, campo por campo
# ══════════════════════════════════════════════════════════════════════════════

def _comandos_tratados(codigo: str) -> set[str]:
    corpo = _corpo_da_funcao(codigo, "void executarComando")
    return set(re.findall(r'strcmp\(cmd,\s*"(\w+)"\)', corpo))


def test_a_placa_trata_exatamente_os_comandos_do_documento(codigo):
    assert _comandos_tratados(codigo) == set(doc_comandos())


def _eventos_do_firmware(codigo: str) -> dict[str, str]:
    """`tipo` -> o literal de formato que o monta.

    Dois jeitos de montar: o tipo literal (`"tipo":"estado"`) e o tipo por
    parâmetro (`emitComUid("rearme", uid)`), que divide o MESMO literal entre
    os três eventos que só levam UID.
    """
    blocos: dict[str, str] = {}
    for achado in re.finditer(r'\\"tipo\\":\\"(\w+)\\"', codigo):
        fim = codigo.find('",', achado.end())
        fim2 = codigo.find(");", achado.end())
        blocos[achado.group(1)] = codigo[achado.start() - 60:max(fim, fim2)]
    corpo_uid = _corpo_da_funcao(codigo, "static void emitComUid(")
    for tipo in re.findall(r'emitComUid\("(\w+)"', codigo):
        blocos[tipo] = corpo_uid
    return blocos


def test_cada_evento_carrega_os_campos_do_documento(codigo):
    blocos = _eventos_do_firmware(codigo)
    documentados = doc_eventos()
    da_porta = set(documentados) - EVENTOS_DO_ADAPTER
    assert len(blocos) >= 9, f"o extrator achou só {sorted(blocos)}"
    assert set(blocos) == da_porta, {
        "emitido e não documentado": sorted(set(blocos) - da_porta),
        "documentado e não emitido": sorted(da_porta - set(blocos)),
    }
    faltando = {}
    for tipo in da_porta:
        ausentes = [c for c in documentados[tipo] if f'\\"{c}\\"' not in blocos[tipo]]
        if ausentes:
            faltando[tipo] = ausentes
    assert not faltando, faltando


def test_todo_evento_leva_a_origem(codigo):
    """O central filtra por ele. Com a USB própria ele é redundante — a COM já
    diz de quem é a linha —, e fica: tirá-lo mudaria firmware e rota juntos."""
    # O literal C inteiro, com as aspas escapadas DENTRO dele.
    literais = re.findall(r'"\{\\"evento\\":\{(?:\\.|[^"\\])*"', codigo)
    assert len(literais) >= 7
    for literal in literais:
        assert '\\"origem\\":\\"porta\\"' in literal, literal


def test_a_porta_nao_emite_os_eventos_de_vigia(codigo):
    """`porta_link_perdido`/`porta_link_ok` são do `porta-adapter`: quem perdeu
    o link não tem como avisar que o perdeu."""
    for tipo in EVENTOS_DO_ADAPTER:
        assert tipo not in codigo


def test_os_estados_sao_o_vocabulario_fechado_do_documento(codigo):
    secao = _secao_porta()
    trecho = secao.split("* `estado` ∈", 1)[1].split("*", 1)[0]
    documentados = set(re.findall(r"`(\w+)`", trecho))
    emitidos = set(re.findall(r'return "(\w+)";',
                              _corpo_da_funcao(codigo, "static const char* nomeEstado(")))
    assert emitidos == documentados
