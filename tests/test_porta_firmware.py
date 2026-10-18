# -*- coding: utf-8 -*-
"""O firmware da porta NR-12 — o que só existe nele.

O que vale para toda placa que inclui `apsen_serial.h` (as quatro cópias
idênticas, o subsistema declarado, o laço que não lê a serial por conta
própria, o buffer antes do `begin`) já é cobrado em
`tests/test_dispenser_firmware.py`, que passou a incluir a porta. Aqui fica o
que é DESTA placa:

  * **duas portas seriais, dois donos.** A voz de máquina é o Serial2, e ela
    só entra pelo `serialPoll()` do header. Uma leitura caractere a caractere
    do Serial2 no `.ino` seria um segundo leitor da MESMA linha — cada um
    ficaria com metade dos bytes de um JSON, e as duas metades sumiriam em
    silêncio. E os comandos de bancada (R/S/C/L/E) moram SÓ na USB: um 'R'
    perdido no fio entre as placas não pode pôr a porta em modo registro;
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

# Os dois eventos de vigia são da placa das TELAS, não da porta — o firmware
# da porta não os emite, e é isso que o teste do contrato tem de saber.
EVENTOS_DA_PLACA_DAS_TELAS = {"porta_link_perdido", "porta_link_ok"}


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
# 1. Duas portas seriais, dois donos
# ══════════════════════════════════════════════════════════════════════════════

def test_a_voz_de_maquina_e_o_serial2(codigo):
    """O header fala pela macro `APSEN_SERIAL`, e ela tem de vir ANTES do
    include — depois, o header já teria fixado a USB."""
    i_define = codigo.index("#define APSEN_SERIAL Serial2")
    i_include = codigo.index('#include "apsen_serial.h"')
    assert i_define < i_include


def test_o_ino_nao_le_o_serial2_caractere_a_caractere(codigo):
    """Quem lê o Serial2 é o `serialPoll()` do header, e só ele.

    Um segundo leitor da mesma porta dividiria os bytes de uma linha entre os
    dois: cada metade vira JSON inválido e some sem erro em lugar nenhum.
    """
    assert "Serial2.read" not in codigo
    assert "Serial2.available" not in codigo
    assert "Serial2.readString" not in codigo


def test_os_comandos_de_bancada_moram_so_na_usb(codigo):
    """R/S/C/L/E leem `Serial` (a USB), e a linha sem '{' do Serial2 só vira
    log — nunca um comando de bancada."""
    bancada = _corpo_da_funcao(codigo, "void tratarBancada()")
    assert "Serial.read()" in bancada
    assert "Serial2" not in bancada
    for letra in "RSCLE":
        assert f"case '{letra}'" in bancada, f"o comando de bancada {letra} sumiu"

    humana = _corpo_da_funcao(codigo, "void linhaHumana(char* linha)")
    assert "tratarBancada" not in humana and "case '" not in humana
    assert "modoReg" not in humana, "linha do Serial2 mexendo no modo registro"


def test_a_uart_usa_os_pinos_do_contrato(codigo):
    assert "#define PIN_UART_RX  16" in codigo
    assert "#define PIN_UART_TX  17" in codigo
    assert "Serial2.begin(115200, SERIAL_8N1, PIN_UART_RX, PIN_UART_TX)" in codigo


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
    da_porta = set(documentados) - EVENTOS_DA_PLACA_DAS_TELAS
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
    """É o campo que separa, no adapter, o evento da porta do das telas."""
    # O literal C inteiro, com as aspas escapadas DENTRO dele.
    literais = re.findall(r'"\{\\"evento\\":\{(?:\\.|[^"\\])*"', codigo)
    assert len(literais) >= 7
    for literal in literais:
        assert '\\"origem\\":\\"porta\\"' in literal, literal


def test_a_porta_nao_emite_os_eventos_de_vigia(codigo):
    """`porta_link_perdido`/`porta_link_ok` são da placa das telas: quem perdeu
    o link não tem como avisar que o perdeu."""
    for tipo in EVENTOS_DA_PLACA_DAS_TELAS:
        assert tipo not in codigo


def test_os_estados_sao_o_vocabulario_fechado_do_documento(codigo):
    secao = _secao_porta()
    trecho = secao.split("* `estado` ∈", 1)[1].split("*", 1)[0]
    documentados = set(re.findall(r"`(\w+)`", trecho))
    emitidos = set(re.findall(r'return "(\w+)";',
                              _corpo_da_funcao(codigo, "static const char* nomeEstado(")))
    assert emitidos == documentados
