# -*- coding: utf-8 -*-
"""Os firmwares que compartilham `apsen_serial.h` — o que só existe neles.

O grosso daqui é dos dois firmwares do dispenser, e a família cresceu quando a
mesa CNC ganhou a voz de máquina: são TRÊS cópias do header, e a igualdade
entre elas é cobrada abaixo. O que é específico de cada placa continua
parametrizado por `SKETCHES` (as duas do dispenser); o que o HEADER exige de
quem o inclui vale para as três, e usa `SKETCHES_COM_HEADER`.

`tests/test_serial_link.py` já cobre o TRANSPORTE, `tests/test_protocolo_placas.py`
já confronta o CONTRATO (documento × adapter × placa falsa) e
`tests/test_dispenser_tft.py` já cobre o lado do adapter. O que falta é o que
nasceu com o firmware:

  * **as três cópias de `apsen_serial.h`.** A Arduino IDE compila a PASTA do
    sketch, e um `#include "../apsen_serial.h"` não sobrevive à cópia que o
    build faz para o diretório temporário. As saídas eram transformar o header
    numa biblioteca Arduino instalada na máquina — infraestrutura que faz o
    firmware parar de compilar em qualquer máquina que não a tenha — ou
    duplicar. É a mesma avaliação que `serial_link.py` já registra para as suas
    três cópias, e o preço da escolha é este teste;
  * **a string de `injetar_falha`.** Ela existe no catálogo do central, nos
    simuladores, na placa falsa e agora no firmware. Divergir aqui não quebra
    nada VISIVELMENTE: o console diz "armado" e o gatilho simplesmente nunca
    dispara;
  * **os campos de cada evento.** O adapter repassa o evento CRU ao central,
    sem traduzir — um nome de campo diferente não dá erro em lugar nenhum, dá
    uma coluna vazia no banco.

**Esta suíte lê C++ por texto**, porque o firmware não compila aqui e não há
AST para ele. É a mesma armadilha que `test_protocolo_serial.py` e
`test_balanca_serial.py` registram: um extrator que pare de casar deixa tudo
verde para sempre, inclusive com o protocolo quebrado. A guarda é a mesma —
cada extrator tem um PISO de quantos símbolos precisa achar, e o piso falha
antes da comparação.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent
PASTA_MECANISMOS = RAIZ_REPO / "dispenser" / "servos_hub"
PASTA_TELAS = RAIZ_REPO / "dispenser" / "telas_tft"
PASTA_CNC = RAIZ_REPO / "cnc" / "receitas_manuais"

# As duas placas do DISPENSER. Quase todo teste daqui é sobre elas — dispensa,
# índice de canal, pintura de tela —, e a mesa não tem nada disso.
SKETCHES = {
    "dispenser": PASTA_MECANISMOS / "servos_hub.ino",
    "dispenser_tft": PASTA_TELAS / "telas_tft.ino",
}

# Todo sketch que inclui `apsen_serial.h`. A mesa entrou aqui e NÃO em
# `SKETCHES`: o que vale para ela é o contrato do header — declarar o
# subsistema, definir as três funções e não tocar na linha crua —, não a
# mecânica dos dispensers. Juntar as duas tabelas faria os testes de dispensa
# cobrarem `canalDoSlot` de uma placa que move uma mesa.
SKETCHES_COM_HEADER = {**SKETCHES, "cnc": PASTA_CNC / "receitas_manuais.ino"}

HEADERS = {
    "dispenser": PASTA_MECANISMOS / "apsen_serial.h",
    "dispenser_tft": PASTA_TELAS / "apsen_serial.h",
    "cnc": PASTA_CNC / "apsen_serial.h",
}


def _fonte(caminho: Path) -> str:
    if not caminho.is_file():
        pytest.fail(f"{caminho} não está no disco — é ele que grava na placa")
    return caminho.read_text(encoding="utf-8")


def sem_comentarios(texto: str) -> str:
    """Tira `//` e `/* */`, preservando o comprimento com espaços.

    Esta armadilha já custou falso positivo na balança e custou de novo aqui: o
    comentário que explica POR QUE algo não é usado contém o nome da coisa não
    usada. `// `moverCalibrado`, NUNCA `moverRaw`` faz um teste que procura
    "moverRaw" achar exatamente a linha que promete não usá-lo.

    Preservar o comprimento é o que mantém os `index()` das outras extrações
    apontando para o mesmo lugar nas duas versões do texto.
    """
    saida = list(texto)
    i, n = 0, len(texto)
    while i < n:
        c = texto[i]
        if c == '"':                       # literal: pula inteiro
            i += 1
            while i < n and texto[i] != '"':
                i += 2 if texto[i] == "\\" else 1
            i += 1
        elif c == "'":
            i += 1
            while i < n and texto[i] != "'":
                i += 2 if texto[i] == "\\" else 1
            i += 1
        elif texto.startswith("//", i):
            while i < n and texto[i] != "\n":
                saida[i] = " "
                i += 1
        elif texto.startswith("/*", i):
            fim = texto.find("*/", i + 2)
            fim = n if fim < 0 else fim + 2
            for j in range(i, fim):
                if saida[j] != "\n":
                    saida[j] = " "
            i = fim
        else:
            i += 1
    return "".join(saida)


@pytest.fixture(scope="module")
def firmware() -> dict[str, str]:
    return {sub: _fonte(caminho) for sub, caminho in SKETCHES_COM_HEADER.items()}


# ══════════════════════════════════════════════════════════════════════════════
# 1. As três cópias de apsen_serial.h são a MESMA
# ══════════════════════════════════════════════════════════════════════════════

def test_as_copias_do_header_sao_identicas():
    """Cópia e não biblioteca compartilhada — ver o cabeçalho deste arquivo.

    Divergir aqui é ter uma das placas falando o enquadramento de ontem, e ela
    seria justamente a que ninguém está depurando naquele dia.

    Eram duas; com a mesa, são três. O header registra no próprio cabeçalho que
    a conta muda se um dia forem cinco — o custo deste teste é constante, o de
    manter as cópias em sincronia não.
    """
    conteudos = {pasta: _fonte(pasta).encode("utf-8") for pasta in HEADERS.values()}
    assert len(set(conteudos.values())) == 1, {
        str(p): len(d) for p, d in conteudos.items()
    }


def test_o_header_exige_o_subsistema_de_quem_o_inclui():
    """Sem `APSEN_SUBSISTEMA`, o ping sairia sem `sub` — e é o `sub` que
    identifica a porta. Falhar na compilação é melhor que uma porta anônima."""
    texto = _fonte(HEADERS["dispenser"])
    assert "#ifndef APSEN_SUBSISTEMA" in texto
    assert "#error" in texto


@pytest.mark.parametrize("sub", sorted(SKETCHES_COM_HEADER))
def test_cada_sketch_declara_o_proprio_subsistema(firmware, sub):
    assert f'#define APSEN_SUBSISTEMA "{sub}"' in firmware[sub]


@pytest.mark.parametrize("sub", sorted(SKETCHES_COM_HEADER))
def test_cada_sketch_fornece_o_que_o_header_pede(firmware, sub):
    """O header declara três funções e não as define: quem as define é o .ino.

    Faltando uma, o erro é de link — mas o teste diz QUAL falta, que é o que
    alguém precisa saber às 23h na bancada.
    """
    for assinatura in (
        "void logMsg(const char* tag, const char* fmt, ...)",
        "void executarComando(const char* cmd, const char* linha, long cmd_id)",
        "void linhaHumana(char* linha)",
    ):
        assert assinatura in firmware[sub], f"{sub}: falta `{assinatura}`"


# ══════════════════════════════════════════════════════════════════════════════
# 2. A voz de máquina não pode passar pelo caminho do humano
# ══════════════════════════════════════════════════════════════════════════════

def test_o_toupper_mora_no_caminho_humano_e_so_nele():
    """O bug que a separação evita: `toupper()` em cima de
    {"cmd":"dispensar"} produz {"CMD":"DISPENSAR"}, que não casa com chave
    nenhuma — e o comando do adapter morreria sem ACK, deixando o orquestrador
    esperando o prazo inteiro."""
    header = sem_comentarios(_fonte(HEADERS["dispenser"]))
    assert "toupper" not in header, (
        "o header lê a linha CRUA: um toupper() aqui destruiria a voz de máquina")

    mecanismos = sem_comentarios(_fonte(SKETCHES["dispenser"]))
    corpo = mecanismos[mecanismos.index("void linhaHumana(char* linha)"):]
    corpo = corpo[: corpo.index("\n}\n")]
    assert "toupper" in corpo, "o caminho humano perdeu as maiúsculas"


def test_o_portao_do_manut_nao_alcanca_a_linha_de_maquina():
    """`processar()` recusa todo comando com o terminal fechado. A linha com
    '{' não passa por ele: o comando do adapter seria recusado SEM ACK."""
    header = sem_comentarios(_fonte(HEADERS["dispenser"]))
    despacho = header[header.index("static void despacharLinha"):]
    despacho = despacho[: despacho.index("\n}\n")]
    assert "strchr(linha, '{')" in despacho
    assert "processarJson" in despacho
    assert "processar(" not in despacho, (
        "o despacho da linha crua não pode chamar o terminal humano direto")


def test_o_fim_de_linha_por_tempo_vale_so_para_o_humano(firmware):
    """Mensagem de máquina sempre termina em \\n. Executar por tempo um JSON
    grande que chegou em pedaços seria executar metade de um `dispensar`."""
    header = _fonte(HEADERS["dispenser"])
    trecho = header[header.index("SERIAL_FIM_LINHA_MS) {", header.index("void serialPoll")) - 400:]
    assert "memchr(buf[n], '{', len[n]) == NULL" in trecho


# ══════════════════════════════════════════════════════════════════════════════
# 3. A injeção de falha: a quinta cópia da mesma string
# ══════════════════════════════════════════════════════════════════════════════

def test_a_string_de_injecao_e_a_do_catalogo_do_central(firmware):
    """String divergente não quebra nada visivelmente — a placa ignora, o
    central acha que injetou, e o gatilho armado nunca dispara."""
    import sys

    sys.path.insert(0, str(RAIZ_REPO / "central-computer"))
    try:
        catalogo = re.search(
            r'TIPO_FALHA_MECANICA\s*=\s*"([^"]+)"',
            (RAIZ_REPO / "central-computer" / "injecao.py").read_text(encoding="utf-8"),
        )
    finally:
        sys.path.pop(0)
    assert catalogo, "não achei TIPO_FALHA_MECANICA em injecao.py"
    esperado = catalogo.group(1)

    achado = re.search(r'#define INJECAO_FALHA_MECANICA "([^"]+)"', firmware["dispenser"])
    assert achado, "o firmware dos mecanismos não declara INJECAO_FALHA_MECANICA"
    assert achado.group(1) == esperado

    simulador = re.search(
        r'INJECAO_FALHA_MECANICA\s*=\s*"([^"]+)"',
        (RAIZ_REPO / "dispenser_simulator" / "simulator.py").read_text(encoding="utf-8"),
    )
    assert simulador and simulador.group(1) == esperado


def test_injecao_desconhecida_e_ignorada_com_aviso(firmware):
    """Um typo do console não pode virar dispensa diferente da que se pediu —
    nem ser recusado, porque o comando em si está correto."""
    assert "desconhecida - ignorada" in firmware["dispenser"]


def test_a_falha_injetada_solta_uma_unidade_a_menos(firmware):
    """1 em 15 já passa da tolerância de 5% da balança: a menor falha possível
    já é detectável pelo Triple Check em qualquer template."""
    assert "a_soltar = alvo - 1" in firmware["dispenser"]


# ══════════════════════════════════════════════════════════════════════════════
# 4. Os eventos: campo por campo, contra a placa falsa
# ══════════════════════════════════════════════════════════════════════════════

# Campos que o `dispenser_simulator` emite e que o central lê. O adapter repassa
# o evento CRU, então um nome diferente aqui não dá erro: dá coluna vazia.
CAMPOS_ESPERADOS = {
    "dispenser": {
        "carregado": ("dispenser_id", "os_id", "medicamento", "sku", "categoria",
                      "quantidade_total", "quantidade_residual", "via_residual", "ts"),
        "dispensado": ("dispenser_id", "os_id", "medicamento", "quantidade_dispensada",
                       "quantidade_alvo", "falha_mecanica", "motivo_falha",
                       "quantidade_residual", "falha_injetada", "ts"),
        "limpeza_ok": ("dispenser_id", "medicamento_limpo", "solicitado_por", "ts"),
        "erro": ("dispenser_id", "os_id", "codigo_erro", "descricao", "ts"),
        "telemetria": ("dispenser_id", "componente", "tipo_leitura", "valor_c",
                       "unidade", "ts"),
        "status": ("dispenser_id", "medicamento", "sku", "categoria", "quantidade",
                   "status", "os_id", "qtd_alvo", "qtd_dispensada", "ts"),
    },
    "dispenser_tft": {
        "telemetria": ("telas_ok", "brilho_pct", "ts"),
        "erro": ("dispenser_id", "codigo_erro", "descricao", "ts"),
    },
}

PISO_EVENTOS = {"dispenser": 6, "dispenser_tft": 2}


def _blocos_de_evento(texto: str) -> dict[str, str]:
    """Todo evento sai dentro de `{"evento":{...}}`, montado por snprintf.

    A extração pega do `"tipo":"<nome>"` até o fecho do literal. `dispensado`
    tem o tipo montado por variável? Não: ele é literal, e este extrator falha
    ruidosamente se alguém mudar isso — é para isso que serve o piso.
    """
    blocos: dict[str, str] = {}
    for achado in re.finditer(r'\\"tipo\\":\\"(\w+)\\"', texto):
        tipo = achado.group(1)
        # O literal de formato continua até o `);` que fecha o `emitir(...)`.
        fim = texto.find('");', achado.end())
        if fim < 0:
            fim = texto.find('",', achado.end())
        blocos.setdefault(tipo, texto[achado.start():fim if fim > 0 else achado.end() + 800])
    return blocos


@pytest.mark.parametrize("sub", sorted(CAMPOS_ESPERADOS))
def test_cada_evento_carrega_todos_os_campos_do_contrato(firmware, sub):
    blocos = _blocos_de_evento(firmware[sub])
    assert len(blocos) >= PISO_EVENTOS[sub], (
        f"{sub}: o extrator achou só {sorted(blocos)} — ele parou de casar, e um "
        f"extrator quebrado deixa este arquivo verde para sempre")

    faltando: dict[str, list[str]] = {}
    for tipo, campos in CAMPOS_ESPERADOS[sub].items():
        assert tipo in blocos, f"{sub}: o firmware não emite `{tipo}`"
        ausentes = [c for c in campos if f'\\"{c}\\"' not in blocos[tipo]]
        if ausentes:
            faltando[tipo] = ausentes
    assert not faltando, f"{sub}: campos ausentes {faltando}"


def test_limpeza_ok_nao_carrega_os_id(firmware):
    """Contrato, não esquecimento: a chave de espera do orquestrador é
    `limpeza:{dispenser_id}`, sem prefixo de OS."""
    bloco = _blocos_de_evento(firmware["dispenser"])["limpeza_ok"]
    assert '\\"os_id\\"' not in bloco


def test_a_placa_das_telas_nao_emite_evento_de_resultado(firmware):
    """`slot` e `estado_celula` só PINTAM: o ACK já disse "aceitei", e não há
    "terminei" a esperar. Um evento novo aqui viraria lixo no adapter."""
    tipos = set(_blocos_de_evento(firmware["dispenser_tft"]))
    assert tipos == {"telemetria", "erro"}, tipos


# ══════════════════════════════════════════════════════════════════════════════
# 5. Os comandos de cada placa são os do documento
# ══════════════════════════════════════════════════════════════════════════════

def _comandos_tratados(texto: str) -> set[str]:
    texto = sem_comentarios(texto)
    corpo = texto[texto.index("void executarComando"):]
    corpo = corpo[: corpo.index("\n}\n")]
    return set(re.findall(r'strcmp\(cmd,\s*"(\w+)"\)', corpo))


@pytest.mark.parametrize("sub, esperados", [
    ("dispenser", {"carregar", "dispensar", "limpar"}),
    ("dispenser_tft", {"slot", "estado_celula"}),
])
def test_cada_placa_trata_exatamente_os_comandos_do_contrato(firmware, sub, esperados):
    assert _comandos_tratados(firmware[sub]) == esperados


def test_o_documento_e_as_placas_falsas_concordam_com_o_firmware(firmware):
    """Quarta ponta da mesma comparação: o firmware contra a placa falsa, que é
    o documento executável contra o qual ele foi escrito."""
    from fakes.placa_dispenser import PlacaDispenser
    from fakes.placa_dispenser_tft import PlacaDispenserTFT

    assert _comandos_tratados(firmware["dispenser"]) == set(PlacaDispenser.COMANDOS)
    assert _comandos_tratados(firmware["dispenser_tft"]) == set(PlacaDispenserTFT.COMANDOS)


# ══════════════════════════════════════════════════════════════════════════════
# 6. A mecânica: uma unidade por ciclo, e o servo respeita a calibração
# ══════════════════════════════════════════════════════════════════════════════

def test_a_dispensa_usa_mover_calibrado_e_nunca_mover_raw(firmware):
    """Fora do MANUT o movimento respeita a calibração, e é ela que impede o
    servo de bater no fim de curso no meio de uma OS."""
    texto = sem_comentarios(firmware["dispenser"])
    corpo = texto[texto.index("static bool soltarUmaUnidade"):]
    corpo = corpo[: corpo.index("\n}\n")]
    assert "moverCalibrado" in corpo
    assert "moverRaw" not in corpo


def test_o_pulso_que_nao_vem_interrompe_a_dispensa(firmware):
    """Não tenta o mesmo comprimido de novo (dose dobrada se ele caiu sem o
    sensor ver) e não segue para o próximo (se atolou, insistir agrava)."""
    texto = sem_comentarios(firmware["dispenser"])
    corpo = texto[texto.index("  for (int i = 1; i <= a_soltar; i++) {"):]
    corpo = corpo[: corpo.index("\n  }\n")]
    assert "break;" in corpo
    assert "interrompeu = true" in corpo


def test_o_nome_e_copiado_antes_de_o_slot_ser_esvaziado(firmware):
    """Ordem, não presença — e a ordem errada só quebrava o caso de SUCESSO.

    `emitDispensado` recebe uma CÓPIA de `sl.medicamento`, e a limpeza do slot
    roda quando `residual == 0`, que é a dispensa que deu certo. Copiando
    depois, o evento saía com `"medicamento":""` justamente aí — e do outro
    lado `dispensas.medicamento` é VARCHAR(100) NOT NULL: o INSERT falhava e a
    linha não existia. A dispensa completa era a única sem rastro no banco.
    """
    corpo = _corpo_da_funcao(sem_comentarios(firmware["dispenser"]),
                             "static void cmdDispensar")
    copia   = corpo.index("copiarCampo(med_evento")
    limpeza = corpo.index("sl.medicamento[0] =")
    emissao = corpo.index("emitDispensado(")
    assert copia < limpeza, (
        "a cópia do nome tem de vir ANTES da limpeza do slot — depois dela, "
        "`med_evento` nasce vazio em toda dispensa completa")
    assert limpeza < emissao


def test_o_evento_sai_da_copia_e_nao_do_slot_ja_limpo(firmware):
    """O contrapeso do teste acima: passar `sl.medicamento` direto parece uma
    simplificação e reintroduz o bug inteiro, sem mexer na ordem de nada."""
    corpo = _corpo_da_funcao(sem_comentarios(firmware["dispenser"]),
                             "static void cmdDispensar")
    assert "emitDispensado(slot, os_id, med_evento" in corpo


def test_a_conversao_de_indice_mora_em_um_lugar_so(firmware):
    """Um off-by-one aqui dispensa do dispenser VIZINHO, e o sintoma chega como
    divergência de SKU na câmera — indistinguível de medicamento trocado."""
    for sub in SKETCHES:
        texto = sem_comentarios(firmware[sub])
        assert texto.count("- 1);") >= 1
        assert "canalDoSlot" in texto and "slotDoCanal" in texto
        # Nenhuma outra aritmética de índice solta nos comandos.
        assert "dispenser_id - 1" not in texto


# ══════════════════════════════════════════════════════════════════════════════
# 7. As duas vozes na mesma porta — o '{' separa, e nada o atravessa
# ══════════════════════════════════════════════════════════════════════════════
#
# Esta seção vale para TODO sketch que inclui o header, e nasceu com a mesa: ela
# foi a primeira placa a ganhar a voz de máquina DEPOIS de já ter um terminal
# humano completo, ou seja, a primeira em que o caminho humano existia antes e
# podia engolir a linha de máquina.
#
# O modo de falhar não dá erro em lugar nenhum. Um `toUpperCase` em cima de
# {"cmd":"mover","cmd_id":7} produz {"CMD":"MOVER","CMD_ID":7}: o scanner do
# header não acha chave nenhuma, `executarComando` nunca é chamado, nenhum ACK
# sai, e o orquestrador espera até o `ack_timeout_s`. A placa segue respondendo
# ao humano normalmente o tempo todo.

def _corpo_da_funcao(texto: str, assinatura: str) -> str:
    """O corpo de uma função de topo, comentários já removidos.

    O `\\n` no fim é acrescentado porque a última função do arquivo pode fechar
    sem quebra de linha depois do `}`. Sem isto, o extrator não acha o
    terminador e levanta `ValueError` — e um extrator que levanta é melhor que
    um que devolve o arquivo inteiro, mas nenhum dos dois é o que se quer aqui:
    a última função de um `.ino` é justamente o `loop()`.
    """
    inicio = texto.index(assinatura)
    corpo = texto[inicio:] + "\n"
    return corpo[: corpo.index("\n}\n")]


@pytest.mark.parametrize("sub", sorted(SKETCHES_COM_HEADER))
def test_o_laco_nao_le_a_serial_por_conta_propria(firmware, sub):
    """Quem lê a porta é o `serialPoll()` do header, e só ele.

    `readStringUntil` faz as duas coisas incompatíveis com um canal que agora
    tem prazo de ACK: BLOQUEIA até o timeout do Serial quando a linha não fecha
    (um caractere solto do terminal segurando o `mover` que o orquestrador
    espera) e aloca uma String no heap por linha.

    Uma segunda leitura da porta é pior que lenta: os dois leitores dividiriam
    os bytes da MESMA linha, e cada metade vira uma mensagem inválida que some
    em silêncio.
    """
    texto = sem_comentarios(firmware[sub])
    laco = _corpo_da_funcao(texto, "void loop()")

    assert "serialPoll()" in laco, f"{sub}: o laço não lê a serial"
    assert "pingPoll()" in laco, (
        f"{sub}: sem pingPoll o adapter nunca identifica esta porta — quem "
        f"inicia o ping é SEMPRE a placa")
    assert "readStringUntil" not in texto, f"{sub}: leitura própria da serial"
    # `Serial.read()` NÃO entra na proibição: drenar a porta (abortar um
    # ensaio de bancada, esvaziar lixo antes de um comando) é uso legítimo, e o
    # próprio header drena no terceiro nível de reentrância. O que não pode
    # voltar é MONTAR UMA LINHA por fora — é isso que divide os bytes de uma
    # mensagem entre dois leitores.


@pytest.mark.parametrize("sub", sorted(SKETCHES_COM_HEADER))
def test_o_buffer_de_recepcao_e_dimensionado_antes_do_serial_begin(firmware, sub):
    """`setRxBufferSize` depois do `begin` não tem efeito, e o sintoma é uma
    mensagem grande chegando PARTIDA — meia linha é JSON inválido, que some sem
    erro. A ordem é a feature."""
    texto = sem_comentarios(firmware[sub])
    setup = _corpo_da_funcao(texto, "void setup()")

    assert "apsenSerialInit()" in setup, f"{sub}: buffer de recepção no default"
    assert setup.index("apsenSerialInit()") < setup.index("Serial.begin"), (
        f"{sub}: apsenSerialInit() DEPOIS do Serial.begin() não tem efeito")


@pytest.mark.parametrize("sub", sorted(SKETCHES_COM_HEADER))
def test_maiusculas_so_no_caminho_humano(firmware, sub):
    """A transformação do humano não pode alcançar uma linha de máquina.

    O corte por '{' mora em `despacharLinha()`, no header, e é feito na linha
    CRUA. O que este teste prende é o outro lado da invariante: que a conversão
    para maiúsculas aconteça DEPOIS do corte — ou seja, dentro de `linhaHumana`
    ou do parser que só ela chama —, e nunca em `loop()` nem em nenhum lugar
    que veja a linha antes de `despacharLinha`.
    """
    texto = sem_comentarios(firmware[sub])

    for funcao in ("void loop()", "void setup()"):
        corpo = _corpo_da_funcao(texto, funcao)
        assert "toUpperCase" not in corpo and "toupper" not in corpo, (
            f"{sub}: {funcao} transforma a linha antes do corte por '{{'")

    # Onde a conversão PODE morar: `linhaHumana` e as funções que só ela chama.
    # Como o alvo é texto, a checagem é de posição — a conversão tem de estar
    # depois do início de `linhaHumana` ou dentro do parser humano.
    if "toUpperCase" not in texto and "toupper" not in texto:
        return                      # placa sem terminal humano (as telas)

    humana = _corpo_da_funcao(texto, "void linhaHumana(char* linha)")
    parser = [nome for nome in ("processar_comando", "processar")
              if f"{nome}(" in humana]
    assert parser, f"{sub}: `linhaHumana` não chama parser humano nenhum"


def test_o_parser_humano_da_mesa_recusa_linha_de_maquina():
    """A guarda mora onde a violação acontece, e é o que sobrevive a um chamador novo.

    A mesa é a única placa cujo parser humano é uma função grande, antiga e
    chamável de qualquer lugar — ela existia muito antes da voz de máquina. O
    corte do header protege o caminho de HOJE; esta guarda protege o do dia em
    que alguém acrescentar um auto-teste no boot ou um replay de linha guardada
    e não reler o header.
    """
    texto = sem_comentarios(_fonte(SKETCHES_COM_HEADER["cnc"]))
    corpo = _corpo_da_funcao(texto, "void processar_comando(String cmd)")

    guarda = corpo.index("indexOf('{')")
    assert guarda < corpo.index("toUpperCase"), (
        "a recusa da linha de máquina tem de vir ANTES do toUpperCase — depois "
        "dele, o estrago já aconteceu")


def test_a_mesa_so_tem_um_caminho_para_o_parser_humano(firmware):
    """Uma porta de entrada, e é `linhaHumana`.

    O `loop()` chamava `processar_comando` direto, com a linha que ele mesmo
    lia da serial. Era o caminho por onde a linha de máquina chegaria ao
    `toUpperCase`.
    """
    texto = sem_comentarios(firmware["cnc"])
    chamadas = re.findall(r"\bprocessar_comando\s*\(", texto)

    # A definição + a chamada de `linhaHumana`. Uma terceira é um caminho novo.
    assert len(chamadas) == 2, (
        f"`processar_comando` aparece {len(chamadas)}x — deveriam ser a "
        f"definição e a chamada de `linhaHumana`")
    assert "processar_comando(" in _corpo_da_funcao(
        texto, "void linhaHumana(char* linha)")


def test_nenhuma_linha_humana_da_mesa_comeca_com_chave():
    """O boot da mesa é barulhento de propósito (ajuda, status, slots, homing) e
    isso fica: é o terminal que o operador conhece.

    O que não pode é uma dessas linhas conter '{'. O adapter procura o primeiro
    '{' da linha e tenta interpretar dali — texto com chave que por acaso
    formasse JSON válido viraria mensagem de máquina, e uma linha de ajuda
    entraria no lugar de um evento.
    """
    fonte = _fonte(SKETCHES_COM_HEADER["cnc"])
    literais = re.findall(r'Serial\.(?:print|println|printf)\(\s*"([^"]*)"', fonte)
    assert len(literais) >= 40, (
        f"o extrator achou só {len(literais)} literais de saída — ele parou de "
        f"casar, e um extrator quebrado deixa este teste verde para sempre")

    com_chave = [t for t in literais if "{" in t]
    assert not com_chave, f"saída humana com '{{': {com_chave}"


# ══════════════════════════════════════════════════════════════════════════════
# 8. A mesa CNC — endereçada por dispenser, e a posição volta MEDIDA
# ══════════════════════════════════════════════════════════════════════════════

CNC = "cnc"


def _cnc(firmware) -> str:
    return sem_comentarios(firmware[CNC])


def test_a_traducao_dispenser_para_coordenada_mora_em_um_lugar_so(firmware):
    """Uma segunda conversão seria um segundo mapa da célula.

    Os dois concordariam só enquanto ninguém recalibrasse um dispenser. Depois
    disso, um caminho mandaria a mesa ao lugar certo e o outro ao lugar de
    ontem — e o sintoma (câmera lendo o SKU do vizinho) é o quadro exato de uma
    falha mecânica, que é onde o técnico vai procurar.
    """
    texto = _cnc(firmware)
    assert "const DispenserPos* dispenser_pos(uint8_t n)" in texto

    corpo = _corpo_da_funcao(texto, "const DispenserPos* dispenser_pos(uint8_t n)")

    # Fora da tabela de `dispenser_pos`, nenhum endereço de D1..D8 é tomado:
    # todo outro uso é pelo símbolo, dentro de um WP() de receita.
    fora = [l.strip() for l in texto.splitlines()
            if re.search(r"&D[1-8]\b", l) and l.strip() not in corpo]
    assert not fora, f"referência a &D1..&D8 fora de dispenser_pos: {fora}"

    # Fora da faixa devolve nullptr — não satura na ponta. Um `dispenser_alvo`
    # de 9 não é "o 8": é um comando que não se deve executar.
    assert "return nullptr" in corpo


def test_toda_recusa_de_mover_leva_ack_negativo_E_evento(firmware):
    """Os dois, porque vão para leitores diferentes.

    O ACK negativo vira 502 no adapter e faz o orquestrador cancelar o
    cronograma ANTES da hora do `dispensar`. O evento vira alarme e linha de
    histórico, com o `codigo_erro` que manda o técnico à peça certa. Só o ACK
    deixaria a recusa sem rastro; só o evento deixaria o central seguindo o
    relógio até despejar medicamento numa mesa que não chegou.
    """
    texto = _cnc(firmware)
    recusa = _corpo_da_funcao(texto, "static void recusarMover(")

    assert "ackErro(" in recusa, "recusa sem ACK negativo: o cronograma não é cancelado"
    assert "emitErroCnc(" in recusa, "recusa sem evento: a recusa não deixa rastro"


def test_todo_codigo_de_erro_da_mesa_vem_da_tabela_fechada(firmware):
    """Código inventado na hora não dá erro — vira linha que ninguém sabe ler.

    O central agrupa alarme por `codigo_erro`, e a tela de necessidades manda o
    técnico à peça a partir dele.
    """
    texto = _cnc(firmware)
    tabela = set(re.findall(r'#define ERRO_\w+\s+"(\w+)"', texto))

    assert tabela == {"dispenser_invalido", "fora_do_envelope", "sem_homing",
                      "limit_disparado", "travado", "homing_falhou"}, tabela

    # Nenhum código passado como string crua: todos entram por macro, que é o
    # que faz a tabela ser a única fonte.
    crus = set(re.findall(r'(?:recusarMover|emitErroCnc)\([^;]*?,\s*"(\w+)"\s*,',
                          texto, re.S))
    assert not crus - tabela, f"codigo_erro fora da tabela: {sorted(crus - tabela)}"


def test_o_ack_positivo_do_mover_vem_depois_de_tudo_que_da_para_conferir(firmware):
    """A ordem É o modelo.

    Toda recusa que consegue ser recusa acontece ANTES do ACK, porque é o ACK
    negativo que cancela o cronograma a tempo. Depois do ACK positivo só resta o
    evento — e ele chega ao central pelo caminho lento, competindo com o relógio
    que já está correndo.
    """
    texto = _cnc(firmware)
    corpo = _corpo_da_funcao(texto, "static void cmdMover(")

    i_ack = corpo.index("ackOk(cmd_id, false)")
    recusas = [m.start() for m in re.finditer(r"recusarMover\(", corpo)]
    assert len(recusas) >= 4, (
        f"só {len(recusas)} recusas antes do ACK — o extrator parou de casar, ou "
        f"uma checagem sumiu")
    assert max(recusas) < i_ack, "há recusa DEPOIS do ACK positivo"

    # O movimento é a última coisa, e só ele pode falhar depois do ACK.
    assert i_ack < corpo.index("mover_para(")


def test_um_mover_por_vez(firmware):
    """`sync_move` lê a serial durante o movimento, e ler significa despachar.

    Sem a guarda, um `mover` chegando no meio de outro entraria em `sync_move`
    de DENTRO de `sync_move`: os dois trajetos se intercalariam pulso a pulso, a
    mesa iria a um terceiro lugar que ninguém pediu, e o tracker sairia coerente
    com ela. O header resolve a reentrância do BUFFER; o comando que volta a
    mover é do sketch.
    """
    texto = _cnc(firmware)
    corpo = _corpo_da_funcao(texto, "static void cmdMover(")

    assert "movimento_em_curso" in corpo, "cmdMover aceita um segundo mover"
    assert corpo.index("movimento_em_curso") < corpo.index("ackOk(cmd_id, false)")

    # A flag é liberada em TODAS as saídas de sync_move depois de armada — uma
    # saída esquecida trava a mesa para sempre, sem erro em lugar nenhum.
    sync = _corpo_da_funcao(texto, "bool sync_move(")
    armado = sync.index("movimento_em_curso = true")
    saidas = [m.start() for m in re.finditer(r"return (?:true|false);", sync)
              if m.start() > armado]
    liberacoes = [m.start() for m in re.finditer(r"movimento_em_curso = false", sync)]
    assert len(liberacoes) == len(saidas), (
        f"{len(saidas)} saídas depois de armar a flag, {len(liberacoes)} liberações")


def test_homing_que_falha_nao_afirma_uma_origem(firmware):
    """O bug que este firmware tinha, e ele não dava erro em lugar nenhum.

    `seek_limit` era `void`: um eixo que nunca achava o fim de curso apenas
    voltava, e `homing_completo` seguia zerando o tracker e marcando
    `ja_fez_homing = true`. A placa passava a afirmar uma origem que nunca foi
    estabelecida, e TODO waypoint absoluto saía deslocado pela distância que
    faltou. A mesa pararia ao lado do dispenser e a câmera leria o SKU do
    vizinho — falha mecânica, para quem fosse procurar.
    """
    texto = _cnc(firmware)

    assert "bool seek_limit(" in texto, "seek_limit voltou a ser void"
    corpo = _corpo_da_funcao(texto, "bool homing_completo()")

    i_falha = corpo.index("ja_fez_homing = false")
    i_zera = corpo.index("steps_A =")
    i_ok = corpo.index("ja_fez_homing = true")
    assert i_falha < i_zera < i_ok, (
        "o caminho de falha tem de sair antes de zerar o tracker: guardar um "
        "zero errado é pior que não ter zero — o soft-limit passaria a proteger "
        "um envelope deslocado")


def test_todo_concluido_depois_de_homing_confere_o_retorno(firmware):
    """A regra do `cmdHoming` vale nos DOIS caminhos de trava, e não valia.

    O §4 do protocolo já dizia: homing que estoura o prazo não emite
    `concluido`, porque `concluido` afirmaria que a mesa está no HOME e ela
    está onde o eixo travou. Nos caminhos de trava o `homing_completo()` saía
    com o retorno ignorado e o `emitConcluido` logo abaixo — e ali é PIOR que
    no `cmdHoming`: `ja_fez_homing` fica `false`, todo `mover` seguinte é
    recusado com `sem_homing`, e a OS que aborta é a de DEPOIS da liberação,
    com um erro que aponta para o lugar errado.

    A checagem é sobre a chamada NUA (`homing_completo();`), porque a forma
    conferida (`if (!homing_completo())`) não tem o ponto e vírgula.
    """
    texto = sem_comentarios(firmware["cnc"])
    nuas = [m.start() for m in re.finditer(r"homing_completo\(\);", texto)]
    # Piso e TETO: as duas únicas chamadas que podem ignorar o retorno são as
    # que não emitem evento nenhum — o `H` do terminal humano e o homing
    # automático do `setup()`. `homing_completo` já imprime a falha ali.
    assert len(nuas) == 2, (
        f"{len(nuas)} chamadas nuas a homing_completo() — o extrator mudou, ou "
        f"alguém acrescentou uma que ignora o retorno")

    for achado in re.finditer(r"emitConcluido\(", texto):
        janela = texto[max(0, achado.start() - 600):achado.start()]
        assert "homing_completo();" not in janela, (
            "um `emitConcluido` está logo depois de um homing cujo retorno "
            "ninguém conferiu — ele afirma um HOME que pode não existir")


def test_homing_que_falha_nao_emite_concluido(firmware):
    """`concluido` afirma que a mesa está no HOME. Depois de um timeout ela está
    onde o eixo travou."""
    texto = _cnc(firmware)
    corpo = _corpo_da_funcao(texto, "static void cmdHoming(")

    i_falha = corpo.index("ERRO_HOMING_FALHOU")
    i_concluido = corpo.index("emitConcluido(")
    assert i_falha < i_concluido
    assert "return;" in corpo[i_falha:i_concluido], (
        "o caminho de falha não retorna: `concluido` sairia junto com o `erro`")


def test_a_mesa_nao_emite_nada_periodico_no_movimento(firmware):
    """Decisão da frente, e a razão é de TEMPO, não estética.

    Uma linha de ~200 B a 115200 baud custa ~17 ms, o curso mais longo da célula
    dura ~2 s, e o central dispara o `dispensar` por RELÓGIO. Cada linha emitida
    no caminho empurra a chegada real para depois da hora agendada — comprimido
    caindo com a mesa ainda em trânsito.
    """
    texto = _cnc(firmware)
    emitidos = set(re.findall(r'\\"tipo\\":\\"(\w+)\\"', texto))

    assert emitidos == {"posicionado", "concluido", "erro"}, emitidos
    assert "progresso_pct" not in texto


def test_a_posicao_do_evento_e_lida_depois_do_movimento(firmware):
    """A MEDIDA, não o alvo.

    Reemitir o alvo faria o evento confirmar o que o comando já dizia, e o
    central passaria a registrar uma posição que ninguém verificou — que é
    exatamente o que ele deixou de fazer quando parou de mandar a coordenada.
    """
    texto = _cnc(firmware)
    emissor = _corpo_da_funcao(texto, "static void emitPosicionado(")

    assert "pos_x_mm()" in emissor and "pos_y_mm()" in emissor, (
        "a posição do evento não vem do tracker")

    # E o emissor não recebe coordenada por parâmetro: se recebesse, alguém
    # poderia passar o ALVO sem que nada quebrasse.
    assinatura = texto[texto.index("static void emitPosicionado("):]
    assinatura = assinatura[: assinatura.index(")")]
    assert "float" not in assinatura, f"emitPosicionado recebe coordenada: {assinatura}"


def test_a_mesa_nao_ganhou_comando_de_bancada_por_json(firmware):
    """FEED, ACCEL, STEP, NOVOCENTRO, REC/MARK/SAVE só no terminal.

    Eles mudam a CALIBRAÇÃO da máquina, e calibração mudada de fora não deixa
    rastro na bancada onde alguém vai procurar por que a mesa passou a parar
    dois milímetros adiante.

    A lista é fechada nos dois sentidos de propósito: um comando a mais aqui é
    uma porta nova, e um a menos é uma porta que o adapter promete e a placa não
    atende. `estado_celula` entrou com a trava do Triple Check — ele não muda
    calibração nenhuma, ele PARA a mesa.
    """
    assert _comandos_tratados(firmware[CNC]) == {"mover", "homing", "estado_celula"}


def test_a_mesa_le_a_serial_durante_o_movimento(firmware):
    """Ler durante o movimento não é conforto — é o que impede a linha PARTIDA.

    O curso mais longo da célula leva ~2 s, e nesse tempo cabem pongs, um
    `estado_celula` e o que mais o adapter mandar. Sem ler, o buffer de recepção
    enche e a mensagem seguinte chega cortada — e meia linha é JSON inválido,
    que some sem erro em lugar nenhum.

    E o LUGAR importa: no topo do laço, nunca entre o `digitalWrite(HIGH)` e o
    `digitalWrite(LOW)`. No meio do pulso, o tempo gasto lendo alarga o pulso
    que o driver está recebendo; no topo, ele só adia o próximo passo.
    """
    texto = _cnc(firmware)
    sync = _corpo_da_funcao(texto, "bool sync_move(")

    i_poll = sync.find("serialPoll();")
    assert i_poll >= 0, "sync_move não lê a serial: linha partida no meio do movimento"

    i_high = sync.index("digitalWrite(MOTOR_X.step, HIGH)")
    i_low = sync.index("digitalWrite(MOTOR_X.step, LOW)")
    assert i_poll < i_high < i_low, (
        "serialPoll está DENTRO do pulso (entre HIGH e LOW) — leitura ali alarga "
        "o pulso que o driver recebe")

    # E uma só: um segundo poll por passo dobraria o custo por pulso sem ler
    # nada a mais, porque o primeiro já drenou o que havia.
    assert sync.count("serialPoll();") == 1
