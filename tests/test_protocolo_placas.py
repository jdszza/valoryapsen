# -*- coding: utf-8 -*-
"""
O protocolo serial adapter ↔ firmware existe em TRÊS cópias, e `docs/` é uma delas.

Esta é a segunda família de `tests/test_protocolo_serial.py`, que prende o
contrato do display de 7" entre `main.cpp`, o backend do painel e o simulador
sem placa. Aqui o elenco é outro e o problema é o mesmo: o contrato dos três
firmwares da célula (dispensers, mesa CNC, balança) está escrito à mão em

  * `docs/PROTOCOLO_SERIAL.md`  — o documento que o firmware vai implementar;
  * `<adapter>/main.py`         — quem MANDA o comando;
  * `tests/fakes/placa_*.py`    — as placas falsas, que o ATENDEM.

A diferença para o display é que aqui a terceira cópia — o C++ — ainda não
existe: os firmwares não estão no repositório. Isso torna o documento MAIS
importante, não menos, porque é ele que alguém vai ler para escrever o firmware.
Documento que mente sobre o contrato é pior que documento nenhum: quem lê para
de conferir o código.

Divergir aqui não quebra nada visivelmente. A placa recebe um `cmd` que não
conhece e responde ACK negativo (na melhor das hipóteses) ou nada; o adapter
recebe um evento com um campo a menos e o repassa assim mesmo ao central, que
grava o buraco sem erro. Os dois sintomas chegam longe da causa: OS abortada por
timeout num slot que está íntegro.

O último bloco é a guarda da guarda — a extração é por texto, e regex que para
de casar deixa tudo verde para sempre.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from conftest import ADAPTERS_SERIAIS, PASTA_DO_ADAPTER
from fakes.placa_cnc import PlacaCNC
from fakes.placa_dispenser import PlacaDispenser
from fakes.placa_dispenser_tft import PlacaDispenserTFT
from fakes.placa_weight import PlacaWeight

RAIZ_REPO = Path(__file__).resolve().parent.parent
DOC = RAIZ_REPO / "docs" / "PROTOCOLO_SERIAL.md"
PASTA = PASTA_DO_ADAPTER
PLACAS = {"dispenser": PlacaDispenser, "cnc": PlacaCNC, "weight": PlacaWeight,
          "dispenser_tft": PlacaDispenserTFT}

# Seção do documento que descreve cada subsistema.
SECAO = {"dispenser": "## 3.", "cnc": "## 4.", "weight": "## 5.",
         "dispenser_tft": "## 6."}

# Onde cada subsistema declara, DENTRO do módulo do adapter, os comandos que
# manda e a função por onde eles saem. O `dispenser_tft` é a segunda porta do
# dispenser-adapter: mesmo arquivo, outra tabela (`_COMANDOS_TFT`) e outra
# função de envio (`_enviar_tft`) — os dois lados do arquivo são lidos em
# separado, senão o `slot` das telas apareceria como comando dos mecanismos.
TABELA_DE_COMANDOS = {"dispenser_tft": "_COMANDOS_TFT"}     # default: _ROTAS_SIM
FUNCAO_DE_ENVIO = {"dispenser_tft": "_enviar_tft"}          # default: _enviar


@pytest.fixture(scope="module")
def doc() -> str:
    if not DOC.is_file():
        pytest.fail("docs/PROTOCOLO_SERIAL.md não está no disco — é ele que o "
                    "firmware vai implementar")
    return DOC.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# Extração — cada função devolve o que UM lado diz
# ══════════════════════════════════════════════════════════════════════════════

def _secao(doc: str, subsistema: str) -> str:
    corpo = doc.split(SECAO[subsistema], 1)[1]
    return corpo.split("\n## ", 1)[0]


def _linhas_de_tabela(bloco: str) -> list[list[str]]:
    """Linhas `| a | b | c |` de uma tabela markdown, sem cabeçalho nem régua."""
    linhas = []
    for crua_bruta in bloco.splitlines():
        crua = crua_bruta.strip()
        if not crua.startswith("|"):
            continue
        colunas = [c.strip() for c in crua.strip("|").split("|")]
        if not colunas or set(colunas[0]) <= set("-: "):
            continue           # régua
        if colunas[0] in ("`cmd`", "`tipo`", "direção"):
            continue           # cabeçalho
        linhas.append(colunas)
    return linhas


def _crase(texto: str) -> list[str]:
    return re.findall(r"`(\w+)`", texto)


def _tabela_de_comandos(bloco: str) -> dict[str, dict]:
    comandos = {}
    for colunas in _linhas_de_tabela(bloco):
        nome = _crase(colunas[0])
        if not nome:
            continue
        campos = {}
        # `injetar_falha` (opcional) — o marcador vem logo depois do campo.
        for achado in re.finditer(r"`(\w+)`(\s*\(opcional\))?", colunas[1]):
            campos[achado.group(1)] = achado.group(2) is None
        comandos[nome[0]] = campos
    return comandos


def doc_comandos(doc: str, subsistema: str) -> dict[str, dict]:
    """`cmd` -> {campo: obrigatório?} da tabela "Comandos" da seção."""
    bloco = _secao(doc, subsistema).split("### Comandos", 1)[1].split("### ", 1)[0]
    return _tabela_de_comandos(bloco)


def doc_comandos_bancada(doc: str, subsistema: str) -> dict[str, dict]:
    """A tabela "Comandos de bancada" — os que existem SÓ no serial.

    Eles ficam numa subseção à parte de propósito: `doc_comandos` lê a
    PRIMEIRA tabela de comandos e para na `###` seguinte, então um comando de
    bancada nunca é cobrado de `_ROTAS_SIM` — e não deve ser, porque não há
    simulador HTTP que o atenda. Prometê-lo lá daria 404 no transporte que é o
    default.
    """
    corpo = _secao(doc, subsistema)
    if "### Comandos de bancada" not in corpo:
        return {}
    bloco = corpo.split("### Comandos de bancada", 1)[1].split("### ", 1)[0]
    return _tabela_de_comandos(bloco)


def doc_eventos(doc: str, subsistema: str) -> dict[str, list[str]]:
    """`tipo` -> campos, da tabela "Eventos" da seção."""
    bloco = _secao(doc, subsistema).split("### Eventos", 1)[1].split("\n---", 1)[0]
    eventos = {}
    for colunas in _linhas_de_tabela(bloco):
        nome = _crase(colunas[0])
        if not nome:
            continue
        eventos[nome[0]] = _crase(colunas[1]) if len(colunas) > 1 else []
    return eventos


def adapter_comandos(subsistema: str) -> set[str]:
    """Chaves de `_ROTAS_SIM` — o nome do comando é o mesmo nos dois transportes."""
    fonte = (RAIZ_REPO / PASTA[subsistema] / "main.py").read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    tabela = TABELA_DE_COMANDOS.get(subsistema, "_ROTAS_SIM")
    for no in ast.walk(arvore):
        if (isinstance(no, ast.Assign) and no.targets
                and isinstance(no.targets[0], ast.Name)
                and no.targets[0].id == tabela):
            return {c.value for c in no.value.keys}
    return set()


def adapter_campos_enviados(subsistema: str) -> dict[str, set[str]]:
    """`_enviar("mover", {...})` — as chaves que o adapter de fato manda.

    Só os dicionários LITERAIS são lidos; o que não for literal entra em
    `NAO_LITERAIS` abaixo, com o motivo, para que um payload novo montado de
    outro jeito não passe despercebido.
    """
    fonte = (RAIZ_REPO / PASTA[subsistema] / "main.py").read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    funcao = FUNCAO_DE_ENVIO.get(subsistema, "_enviar")
    enviados: dict[str, set[str]] = {}
    for no in ast.walk(arvore):
        if not (isinstance(no, ast.Call) and isinstance(no.func, ast.Name)
                and no.func.id == funcao and len(no.args) == 2):
            continue
        alvo, payload = no.args
        if not isinstance(alvo, ast.Constant):
            continue
        if isinstance(payload, ast.Dict):
            enviados[alvo.value] = {c.value for c in payload.keys}
        else:
            enviados[alvo.value] = set()
    return enviados


# `homing` monta o corpo por compreensão (`{k: v for k, v in model_dump()...}`)
# para não mandar as coordenadas opcionais quando elas não vêm. É o único, e a
# lista existe para que um segundo caso apareça aqui em vez de sumir.
NAO_LITERAIS = {("cnc", "homing")}


def placa_eventos_emitidos(subsistema: str) -> dict[str, dict]:
    """Roda a placa falsa de verdade e coleta um exemplar de cada evento.

    Comparar SÓ os nomes deixaria passar o caso que mais aparece na prática: o
    nome certo com um campo a menos. O central grava o buraco sem erro.
    """
    placa = PLACAS[subsistema](atraso_evento=0.0)
    exemplos: dict[str, dict] = {}

    for cmd, campos in EXEMPLOS_DE_COMANDO[subsistema].items():
        for evento in placa.eventos_para(cmd, campos):
            exemplos[evento["tipo"]] = evento

    for evento in EXTRAS_DA_PLACA.get(subsistema, lambda p: [])(placa):
        exemplos[evento["tipo"]] = evento
    return exemplos


EXEMPLOS_DE_COMANDO = {
    "dispenser": {
        "carregar": {"dispenser_id": 1, "medicamento": "Dipirona 500mg",
                     "sku": "APSEN-001", "categoria": "analgesico",
                     "quantidade": 10, "os_id": "OS-1"},
        "dispensar": {"dispenser_id": 1, "os_id": "OS-1"},
        "limpar": {"dispenser_id": 1, "solicitado_por": "teste"},
    },
    "cnc": {
        "mover": {"dispenser_alvo": 3, "os_id": "OS-1", "receita": "C",
                  "ciclo_atual": 1, "total_ciclos": 3},
        "homing": {"os_id": "OS-1", "posicao_x": -120.0, "posicao_y": 0.0},
        "estado_celula": {"trava_ativa": True, "trava_slot_id": 3,
                          "os_id": "OS-1", "trava_resumo": "divergência de peso"},
    },
    "weight": {
        "tara": {"os_id": "OS-1"},
        "pesar": {"os_id": "OS-1", "slot_id": 1, "quantidade_esperada": 10,
                  "quantidade_real": 10, "peso_unitario_g": 50.0},
        # Os de bancada entram aqui porque os EVENTOS deles também são
        # contrato: `cfg`, `estado`, `contagem` e `tara_balanca` alimentam o
        # `GET /balanca` do adapter, e um campo a menos ali some do painel sem
        # erro em lugar nenhum — o mesmo modo de falhar dos eventos de OS.
        "peso_unitario":   {"valor_g": 12.5},
        "tara_recipiente": {},
        "tara_canais":     {},
        "contar":          {},
        "config":          {},
        "stream":          {"on": False},
    },
    "dispenser_tft": {
        "slot": {"dispenser_id": 1, "medicamento": "Dipirona 500mg",
                 "sku": "APSEN-001", "categoria": "analgesico",
                 "quantidade_alvo": 10, "quantidade_dispensada": 0,
                 "quantidade_residual": 0, "status": "carregando", "os_id": "OS-1"},
        "estado_celula": {"trava_ativa": True, "trava_slot_id": 3, "os_id": "OS-1",
                          "trava_resumo": "divergência de peso"},
    },
}

# Eventos que a placa emite fora do ciclo comando → resultado. As telas SÓ
# emitem por conta própria: pintar não produz resultado.
EXTRAS_DA_PLACA = {
    "dispenser":     lambda p: [p.telemetria(1)],
    "cnc":           lambda p: [p.movendo("OS-1", 3, 120.0, -150.0)],
    "weight":        lambda p: [p.telemetria(), p.boot(), p.peso(),
                                p.erro_balanca("canal 0 saturado")],
    "dispenser_tft": lambda p: [p.telemetria(), p.erro()],
}


# ══════════════════════════════════════════════════════════════════════════════
# 1. Comandos: documento ↔ adapter ↔ placa
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_o_adapter_manda_exatamente_os_comandos_documentados(doc, subsistema):
    """Comando fora do documento é comando que o firmware não vai implementar."""
    assert adapter_comandos(subsistema) == set(doc_comandos(doc, subsistema))


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_a_placa_atende_exatamente_os_comandos_documentados(doc, subsistema):
    """A placa falsa é o duplo do firmware: um comando a mais ou a menos aqui é
    um teste de bancada que passa com a bancada real falhando, ou o contrário."""
    assert set(PLACAS[subsistema].COMANDOS) == set(doc_comandos(doc, subsistema))


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_os_campos_que_o_adapter_envia_estao_documentados(doc, subsistema):
    """Campo enviado e não documentado é campo que o firmware vai ignorar."""
    documentado = doc_comandos(doc, subsistema)
    for cmd, campos in adapter_campos_enviados(subsistema).items():
        if (subsistema, cmd) in NAO_LITERAIS:
            continue
        assert campos <= set(documentado[cmd]), {
            "comando": cmd,
            "enviado e não documentado": sorted(campos - set(documentado[cmd])),
        }


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_todo_campo_obrigatorio_do_documento_e_realmente_enviado(doc, subsistema):
    """O outro sentido: campo documentado como obrigatório que ninguém manda é
    o firmware esperando por um dado que nunca chega."""
    documentado = doc_comandos(doc, subsistema)
    enviados = adapter_campos_enviados(subsistema)
    for cmd, campos in documentado.items():
        if (subsistema, cmd) in NAO_LITERAIS:
            continue
        obrigatorios = {nome for nome, exigido in campos.items() if exigido}
        assert obrigatorios <= enviados[cmd], {
            "comando": cmd,
            "documentado e não enviado": sorted(obrigatorios - enviados[cmd]),
        }


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_a_placa_so_exige_campo_documentado_como_obrigatorio(doc, subsistema):
    """Exigir um campo que o documento marca como opcional faz a placa recusar
    o comando que o adapter tem todo o direito de mandar — e o `injetar_falha`,
    ausente no caminho normal, é exatamente esse caso."""
    documentado = doc_comandos(doc, subsistema)
    for cmd, exigidos in PLACAS[subsistema].COMANDOS.items():
        obrigatorios = {n for n, exigido in documentado[cmd].items() if exigido}
        assert set(exigidos) <= obrigatorios, {
            "comando": cmd,
            "exigido pela placa sem ser obrigatório no doc":
                sorted(set(exigidos) - obrigatorios),
        }


# ══════════════════════════════════════════════════════════════════════════════
# 2. Eventos: o que a placa emite é o que o documento promete
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_todo_evento_que_a_placa_emite_esta_documentado(doc, subsistema):
    emitidos = set(placa_eventos_emitidos(subsistema))
    assert emitidos <= set(doc_eventos(doc, subsistema)), {
        "emitido e não documentado":
            sorted(emitidos - set(doc_eventos(doc, subsistema)))
    }


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_os_campos_de_cada_evento_batem_com_o_documento(doc, subsistema):
    """Nome certo com campo a menos é o modo de falhar mais provável.

    O adapter repassa o evento CRU ao central, sem interpretar: um campo que
    some não vira erro em lugar nenhum — vira coluna vazia no banco e número
    faltando no painel.
    """
    documentado = doc_eventos(doc, subsistema)
    for tipo, exemplo in placa_eventos_emitidos(subsistema).items():
        emitidos = set(exemplo) - {"tipo"}
        assert emitidos == set(documentado[tipo]), {
            "evento": tipo,
            "emitido e não documentado": sorted(emitidos - set(documentado[tipo])),
            "documentado e não emitido": sorted(set(documentado[tipo]) - emitidos),
        }


def test_limpeza_ok_nao_carrega_os_id(doc):
    """Limpeza é operação de SLOT, não de OS — e a chave de espera do
    orquestrador é `limpeza:{dispenser_id}`, sem prefixo de OS.

    Acrescentar `os_id` aqui não daria erro: daria uma chave que ninguém espera,
    e o orquestrador ficaria bloqueado até o timeout de uma limpeza que
    aconteceu.
    """
    assert "os_id" not in doc_eventos(doc, "dispenser")["limpeza_ok"]
    assert "os_id" not in placa_eventos_emitidos("dispenser")["limpeza_ok"]


def test_dispensado_carrega_o_nome_mesmo_com_residual_zero():
    """Resíduo 0 é o caso de SUCESSO, e era justamente ele que saía sem nome.

    O firmware lia `sl.medicamento` DEPOIS de esvaziar o slot, então toda
    dispensa completa emitia `"medicamento":""`. O adapter repassa o evento
    cru, e `dispensas.medicamento` é VARCHAR(100) NOT NULL no central: o INSERT
    falhava com 1048 e a dispensa que deu certo era a única sem linha no banco
    — some do relatório da OS, do CSV e do XLSX, e o único rastro era um
    `warning`.

    A placa falsa é o documento executável contra o qual o firmware é escrito,
    então a garantia mora aqui também, e não só no C++.
    """
    placa = PlacaDispenser()
    placa.eventos_para("carregar", {
        "dispenser_id": 3, "medicamento": "Dipirona", "sku": "DIP-500",
        "categoria": "analgesico", "quantidade": 5, "os_id": "OS-1",
    })

    (evento,) = placa.eventos_para("dispensar", {"dispenser_id": 3,
                                                 "os_id": "OS-1"})

    assert evento["quantidade_residual"] == 0
    assert evento["medicamento"] == "Dipirona"


def test_a_placa_atende_exatamente_os_comandos_de_bancada_documentados(doc):
    """A mesma cobrança dos comandos de OS, na tabela de bancada.

    Sem isto, a subseção à parte viraria uma porta dos fundos: comando de
    bancada novo entraria na placa falsa sem passar pelo documento, e o
    firmware — que é escrito a partir do documento — não o implementaria.
    """
    assert set(PLACAS["weight"].COMANDOS_BANCADA) == set(
        doc_comandos_bancada(doc, "weight"))


def test_comando_de_bancada_nao_entra_em_rotas_sim(doc):
    """`_ROTAS_SIM` promete as DUAS pernas, e o weight-simulator não tem
    balança para configurar: uma entrada lá daria 404 no transporte default."""
    assert not (set(doc_comandos_bancada(doc, "weight"))
                & adapter_comandos("weight"))


def test_a_placa_so_exige_campo_documentado_nos_comandos_de_bancada(doc):
    documentado = doc_comandos_bancada(doc, "weight")
    for cmd, exigidos in PLACAS["weight"].COMANDOS_BANCADA.items():
        obrigatorios = {n for n, exigido in documentado[cmd].items() if exigido}
        assert set(exigidos) <= obrigatorios, cmd


def test_o_comando_de_pesagem_leva_as_duas_quantidades(doc):
    """Uma só fazia a balança comparar o valor consigo mesma: a mesa crescia
    pelo esperado e uma falha mecânica de 8 em 10 continuava "pesando" 10."""
    campos = doc_comandos(doc, "weight")["pesar"]
    assert "quantidade_esperada" in campos and "quantidade_real" in campos


def test_injetar_falha_e_opcional_nos_dois_comandos_que_o_aceitam(doc):
    """Ausente no caminho normal: o comando sai byte a byte como saía antes de a
    injeção existir, e a placa não pode exigi-lo."""
    assert doc_comandos(doc, "dispenser")["dispensar"]["injetar_falha"] is False
    assert doc_comandos(doc, "weight")["pesar"]["injetar_falha"] is False


# ══════════════════════════════════════════════════════════════════════════════
# 3. Mensagens de serviço e números do enquadramento
# ══════════════════════════════════════════════════════════════════════════════

def _serial_link() -> str:
    return (RAIZ_REPO / "cnc-adapter" / "serial_link.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("trecho", [
    '{"cmd":"ping","sub":"<subsistema>"}',
    '{"resp":"pong","epoch":<unix>,"sessao":<unix>}',
    '{"cmd":"<nome>","cmd_id":<n>,"sessao":<unix>,...campos}',
    '{"resp":"ok","cmd_id":<n>}',
    '{"resp":"erro","cmd_id":<n>,"msg":"..."}',
    '{"evento":{...payload...}}',
])
def test_as_mensagens_de_servico_estao_no_documento(doc, trecho):
    """São iguais nos três subsistemas, e é por elas que a porta é identificada."""
    assert trecho in doc, f"{trecho} sumiu do documento"


@pytest.mark.parametrize("simbolo", ["cmd", "resp", "pong", "epoch", "cmd_id",
                                     "sessao", "evento", "ping", "sub", "msg"])
def test_o_transporte_conhece_cada_simbolo_do_servico(simbolo):
    """O documento e o código têm que falar das MESMAS chaves. Uma tag que só
    existe de um lado é mensagem descartada em silêncio."""
    assert f'"{simbolo}"' in _serial_link(), (
        f"`{simbolo}` está no documento e não aparece em serial_link.py"
    )


def test_o_teto_da_linha_do_documento_e_o_do_codigo(doc):
    """O firmware lê para um buffer fixo e DESCARTA a linha que não couber. Dois
    números diferentes aqui produzem mensagens que somem sem rastro."""
    assert "1024 bytes por linha" in doc
    assert re.search(r"MAX_LINHA_BYTES\s*=\s*1024", _serial_link())


def test_o_baud_do_documento_e_o_default_dos_tres_adapters(doc):
    """Baud divergente dá lixo na linha, não erro."""
    assert "`115200` baud" in doc
    for subsistema in ADAPTERS_SERIAIS:
        fonte = (RAIZ_REPO / PASTA[subsistema] / "main.py").read_text(encoding="utf-8")
        assert re.search(r'SERIAL_BAUD",\s*"115200"', fonte), subsistema


def test_o_prazo_de_ack_do_documento_e_o_default_dos_tres_adapters(doc):
    assert "default **2 s**" in doc
    for subsistema in ADAPTERS_SERIAIS:
        fonte = (RAIZ_REPO / PASTA[subsistema] / "main.py").read_text(encoding="utf-8")
        assert re.search(r'ACK_TIMEOUT_S",\s*"2"', fonte), subsistema


def test_o_documento_registra_que_ack_nao_e_conclusao(doc):
    """É a confusão que trava uma OS, e o firmware é escrito a partir daqui."""
    assert "### ACK não é conclusão" in doc
    assert "`cmd_id` — todo comando leva" in doc


def test_o_documento_lista_os_quatro_subsistemas_e_so_eles(doc):
    """Quatro portas — dispenser, dispenser_tft, cnc, weight —, e o
    `vision-adapter` continua fora da migração: a ausência é decisão."""
    subs = set(re.findall(r'\{"cmd":"ping","sub":"(\w+)"\}', doc))
    assert subs <= set(ADAPTERS_SERIAIS) | {"<subsistema>"}
    for subsistema in ADAPTERS_SERIAIS:
        assert f"`{subsistema}`" in doc
    assert "vision" not in doc.lower().split("## 3.")[1]


def test_as_duas_portas_do_dispenser_adapter_estao_no_documento(doc):
    """`dispenser` e `dispenser_tft` são DUAS portas físicas do MESMO adapter,
    e é o documento que diz isso a quem for escrever o firmware."""
    tabela = doc.split("## 1.", 1)[0]
    assert tabela.count("`dispenser-adapter`") == 2
    assert "`dispenser_tft`" in tabela
    assert "duas portas físicas do mesmo adapter" in doc
    assert "48" in _secao(doc, "dispenser_tft")


# ══════════════════════════════════════════════════════════════════════════════
# 4. Guarda da guarda: a extração precisa ENXERGAR
# ══════════════════════════════════════════════════════════════════════════════
#
# A comparação é por texto, dos dois lados: tabela markdown de um, AST e regex
# do outro. Um extrator que para de casar não faz nada acusar — ele deixa TODOS
# os testes acima verdes para sempre, inclusive com o protocolo quebrado. É a
# mesma guarda que `tests/test_protocolo_serial.py` carrega no último bloco, e
# ela existe lá porque o erro já aconteceu.

# `eventos` é o que o DOCUMENTO lista; `emitidos` é o que a placa falsa produz
# com os exemplos daqui (um exemplar por tipo, não a lista inteira).
MINIMOS = {
    "dispenser":     {"comandos": 3, "eventos": 6, "emitidos": 3, "campos_de_evento": 20},
    "cnc":           {"comandos": 2, "eventos": 6, "emitidos": 3, "campos_de_evento": 20},
    "weight":        {"comandos": 2, "eventos": 5, "emitidos": 3, "campos_de_evento": 20},
    # As telas só emitem telemetria e erro: 2 eventos, 7 campos.
    "dispenser_tft": {"comandos": 2, "eventos": 2, "emitidos": 2, "campos_de_evento": 7},
}


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_a_leitura_do_documento_acha_o_que_deve(doc, subsistema):
    comandos = doc_comandos(doc, subsistema)
    eventos = doc_eventos(doc, subsistema)
    assert len(comandos) >= MINIMOS[subsistema]["comandos"], comandos
    assert len(eventos) >= MINIMOS[subsistema]["eventos"], eventos
    # Tabela lida sem campo nenhum é tabela não lida.
    assert all(campos for campos in comandos.values()), comandos
    assert sum(len(c) for c in eventos.values()) >= MINIMOS[subsistema]["campos_de_evento"], eventos


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_a_leitura_do_adapter_acha_o_que_deve(subsistema):
    assert len(adapter_comandos(subsistema)) >= MINIMOS[subsistema]["comandos"]
    enviados = adapter_campos_enviados(subsistema)
    assert len(enviados) >= MINIMOS[subsistema]["comandos"], enviados
    literais = [c for c, campos in enviados.items() if campos]
    assert len(literais) >= MINIMOS[subsistema]["comandos"] - 1, enviados


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_a_placa_falsa_emite_mais_de_um_evento(subsistema):
    """Placa que não emite nada faria o bloco 2 passar sem comparar coisa
    alguma — o caso em que o teste some sem ninguém notar."""
    emitidos = placa_eventos_emitidos(subsistema)
    assert len(emitidos) >= MINIMOS[subsistema]["emitidos"], sorted(emitidos)
    assert all(len(exemplo) > 2 for exemplo in emitidos.values()), emitidos


def test_o_unico_payload_nao_literal_e_o_do_homing():
    """A lista de exceções não pode crescer em silêncio: um payload novo montado
    por compreensão sairia da comparação de campos sem nada acusar."""
    achados = set()
    for subsistema in ADAPTERS_SERIAIS:
        for cmd, campos in adapter_campos_enviados(subsistema).items():
            if not campos:
                achados.add((subsistema, cmd))
    assert achados == NAO_LITERAIS, achados


@pytest.mark.parametrize("caminho", [
    "docs/PROTOCOLO_SERIAL.md",
    "tests/fakes/placa_base.py",
    "cnc-adapter/serial_link.py",
    "dispenser-adapter/serial_link.py",
    "weight-adapter/serial_link.py",
])
def test_o_git_nao_esta_engolindo_os_arquivos_novos(caminho):
    """O documento é o insumo do firmware, e as placas falsas são o duplo dele.

    Arquivo que o `.gitignore` engole não chega a quem for escrever o firmware —
    e o commit passa sem o git dizer nada. Este repositório já levou essa
    armadilha uma vez: a regra `backend/` sem barra inicial, herdada da era MQTT,
    casava em qualquer profundidade e comeu `painel_operador/backend/` inteiro.
    """
    import subprocess

    resultado = subprocess.run(
        ["git", "check-ignore", "-q", caminho],
        cwd=RAIZ_REPO, capture_output=True, text=True, check=False,
    )
    if resultado.returncode > 1:
        pytest.skip("git indisponível")
    assert resultado.returncode == 1, (
        f"{caminho} está sendo ignorado pelo .gitignore — o commit sairia sem "
        f"ele e sem aviso nenhum"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4. A mesa CNC — endereçada por dispenser, e a trava chega até ela
# ══════════════════════════════════════════════════════════════════════════════
#
# O contrato da mesa mudou de FORMA, não só de campo: ela deixou de receber
# coordenada e passou a REPORTAR a posição alcançada, e o evento que ela emite
# deixou de ser portão. A placa falsa é o documento executável disso, e estes
# testes prendem o que a tabela do §4 não mostra.

def test_o_endereco_do_mover_e_o_dispenser_e_nao_a_receita():
    """`dispenser_alvo` é exigido; `receita` não.

    O endereço é o slot — sem ele não há para onde ir, e inventar um default (o
    primeiro, o último visitado) mandaria a mesa a um lugar que ninguém pediu,
    com a câmera lendo o SKU de quem estivesse lá.

    `receita` diz QUAL ordem está rodando: é log, não endereço. Exigi-la
    transformaria um campo de rastreio em motivo de recusa, e a mesa sabe ir a
    um dispenser sem saber de qual OS ele faz parte.
    """
    assert set(PlacaCNC.COMANDOS["mover"]) == {"dispenser_alvo", "os_id"}
    assert "receita" not in PlacaCNC.COMANDOS["mover"]
    assert "posicao_x" not in PlacaCNC.COMANDOS["mover"]
    assert "posicao_y" not in PlacaCNC.COMANDOS["mover"]


def test_a_bancada_da_placa_falsa_nao_casa_com_o_modelo_do_central(carregar_orquestrador):
    """De propósito diferentes — é isso que dá valor à posição do evento.

    Iguais, toda asserção sobre posição passaria por concordância acidental: o
    central compararia o próprio número com uma cópia dele, e continuaria verde
    no dia em que voltasse a ignorar o que a máquina informa.
    """
    from fakes.placa_cnc import WAYPOINTS

    modelo = carregar_orquestrador().modulo.POSICOES
    for slot, ponto in WAYPOINTS.items():
        assert ponto != modelo[slot], (
            f"a bancada da placa falsa casou com o modelo do central em D{slot}")


def test_dispenser_fora_de_faixa_nao_inventa_posicao():
    """Saturar na ponta (tratar 9 como "o 8") é pior que recusar: a mesa iria a
    um slot REAL com a OS achando que foi a outro."""
    placa = PlacaCNC(atraso_evento=0.0)
    eventos = placa.eventos_para("mover", {"dispenser_alvo": 99, "os_id": "OS-1"})

    assert [e["tipo"] for e in eventos] == ["erro"]
    assert eventos[0]["codigo_erro"] == "dispenser_invalido"


def test_a_mesa_travada_recusa_mover_e_volta_a_aceitar_na_liberacao():
    """Sob o ciclo por relógio, a mesa é a peça que continua andando sozinha se
    ninguém a avisar — e o supervisor está com as mãos na bancada."""
    placa = PlacaCNC(atraso_evento=0.0)
    placa.eventos_para("estado_celula", {"trava_ativa": True, "trava_slot_id": 3})

    eventos = placa.eventos_para("mover", {"dispenser_alvo": 2, "os_id": "OS-1"})
    assert eventos[0]["codigo_erro"] == "travado"

    placa.eventos_para("estado_celula", {"trava_ativa": False})
    eventos = placa.eventos_para("mover", {"dispenser_alvo": 2, "os_id": "OS-1"})
    assert eventos[0]["tipo"] == "posicionado"


def test_estado_celula_repetido_nao_refaz_nada():
    """Receber `true` duas vezes é inofensivo — o central reenvia o aviso sem
    saber o que a placa já sabe, e o firmware age só na TRANSIÇÃO.

    A primeira vez PRODUZ evento: na mesa, `estado_celula` não é só pintura —
    ela vai ao HOME por conta própria, que é onde o supervisor espera
    encontrá-la. É a segunda que não refaz nada.
    """
    placa = PlacaCNC(atraso_evento=0.0)

    primeira = placa.eventos_para("estado_celula", {"trava_ativa": True})
    assert [e["tipo"] for e in primeira] == ["concluido"]
    assert placa.eventos_para("estado_celula", {"trava_ativa": True}) == []
    assert placa.trava_ativa is True


def test_a_trava_que_interrompe_um_mover_emite_erro_e_depois_o_homing():
    """Dois eventos, nesta ordem: o `erro` com `travado` diz a quem esperava a
    chegada que ela não vem; o homing é a mesa indo para onde o supervisor vai
    procurá-la."""
    placa = PlacaCNC(atraso_evento=0.0)
    placa.movimento_em_curso = True

    eventos = placa.eventos_para("estado_celula",
                                 {"trava_ativa": True, "trava_slot_id": 3,
                                  "os_id": "OS-1"})

    assert [e["tipo"] for e in eventos] == ["erro", "concluido"]
    assert eventos[0]["codigo_erro"] == "travado"


def test_a_liberacao_nao_refaz_o_homing():
    """A mesa já está no HOME desde a ativação, e um segundo homing custaria
    segundos no exato momento em que o supervisor liberou a produção."""
    placa = PlacaCNC(atraso_evento=0.0)
    placa.eventos_para("estado_celula", {"trava_ativa": True})

    assert placa.eventos_para("estado_celula", {"trava_ativa": False}) == []


@pytest.mark.parametrize("com_movimento", [False, True])
def test_homing_que_falha_na_trava_nao_emite_concluido(com_movimento):
    """A regra do §4, nos caminhos em que o firmware a violava.

    `concluido` depois de um homing que estourou o prazo afirmaria que a mesa
    está no HOME, e ela está onde o eixo travou — com `ja_fez_homing` em false,
    ou seja, todo `mover` seguinte recusado com `sem_homing`. O supervisor
    libera a trava e a OS SEGUINTE aborta com um erro que aponta para o lugar
    errado.
    """
    placa = PlacaCNC(atraso_evento=0.0)
    placa.homing_falha = True
    placa.movimento_em_curso = com_movimento

    eventos = placa.eventos_para("estado_celula",
                                 {"trava_ativa": True, "os_id": "OS-1"})

    assert "concluido" not in [e["tipo"] for e in eventos]
    assert eventos[-1]["codigo_erro"] == "homing_falhou"


def test_o_concluido_da_trava_leva_a_os_que_travou():
    """É este `os_id` que fazia o central marcar como CONCLUÍDA a OS que está
    parada esperando um supervisor (ver `tests/test_central_eventos.py`). A
    placa falsa em silêncio era o que deixava isso passar."""
    placa = PlacaCNC(atraso_evento=0.0)

    (evento,) = placa.eventos_para("estado_celula",
                                   {"trava_ativa": True, "os_id": "OS-42"})

    assert evento["os_id"] == "OS-42"


def test_a_mesa_muda_e_encenavel():
    """Nem chegada, nem erro — o caso que DEFINE o modelo por relógio.

    Sem este modo, nenhum teste distingue "a mesa não confirmou" de "a mesa
    disse que falhou", e os dois têm desfechos opostos: o primeiro segue como
    pendência, o segundo aborta a OS.
    """
    placa = PlacaCNC(atraso_evento=0.0)
    placa.mover_mudo = True

    assert placa.eventos_para("mover", {"dispenser_alvo": 2, "os_id": "OS-1"}) == []




# ══════════════════════════════════════════════════════════════════════════════
# 5. O `codigo_erro` da mesa é o MESMO nas três implementações
# ══════════════════════════════════════════════════════════════════════════════
#
# Esta seção nasceu de uma divergência que esteve no repositório com a suíte
# inteira verde: o `cnc_simulator` emitia `dispenser_sem_waypoint` onde o
# firmware emite `dispenser_invalido` — para a MESMA condição —, e o comentário
# em cima da linha dizia, em português, que os dois usavam o mesmo código. O
# comentário foi escrito antes de o firmware existir e ninguém o conferiu
# depois.
#
# Divergir aqui não quebra nada visivelmente. O central agrupa alarme por
# `codigo_erro` e a tela de necessidades manda o técnico à peça a partir dele:
# um código que só aparece com a placa montada é um alarme que ninguém sabe ler,
# descoberto no pior dia possível.

RAIZ_CNC = RAIZ_REPO / "cnc" / "receitas_manuais" / "receitas_manuais.ino"
SIMULADOR_CNC = RAIZ_REPO / "cnc_simulator" / "simulator.py"
DOC_PROTOCOLO = RAIZ_REPO / "docs" / "PROTOCOLO_SERIAL.md"

# O único código que existe SÓ no simulador, e a exceção é registrada aqui e no
# §4: a placa aceita qualquer `receita` (ela é log, não endereço — quem acha a
# posição é o waypoint do dispensador), e quem a valida é o simulador, para
# encenar na demonstração a ordem cujo roteiro ninguém gravou.
SO_NO_SIMULADOR = {"receita_desconhecida"}


def _codigos_do_documento() -> set[str]:
    texto = DOC_PROTOCOLO.read_text(encoding="utf-8")
    secao = texto.split("## 4.", 1)[1].split("\n## ", 1)[0]
    bloco = secao.split("### `codigo_erro`", 1)[1].split("\n### ", 1)[0]
    codigos = set()
    for linha in bloco.splitlines():
        if not linha.strip().startswith("|"):
            continue
        col = [c.strip() for c in linha.strip("|").split("|")]
        if not col or set(col[0]) <= set("-: ") or col[0] == "`codigo_erro`":
            continue
        achado = re.findall(r"`(\w+)`", col[0])
        if achado:
            codigos.add(achado[0])
    return codigos


def _codigos_do_firmware() -> set[str]:
    texto = RAIZ_CNC.read_text(encoding="utf-8")
    return set(re.findall(r'#define ERRO_\w+\s+"(\w+)"', texto))


def _codigos_do_simulador() -> set[str]:
    texto = SIMULADOR_CNC.read_text(encoding="utf-8")
    return set(re.findall(r'_recusar\([^)]*?,\s*"(\w+)"\s*,', texto, re.S))


def test_o_documento_lista_exatamente_os_codigos_do_firmware():
    """O §4 é o que alguém lê para escrever o outro lado. Um código a menos ali
    é um alarme que chega sem tradução; um a mais é uma promessa vazia."""
    doc = _codigos_do_documento()
    firmware = _codigos_do_firmware()

    assert len(doc) >= 6, (
        f"o extrator achou só {sorted(doc)} no §4 — ele parou de casar, e um "
        f"extrator quebrado deixa este arquivo verde para sempre")
    assert doc == firmware | SO_NO_SIMULADOR, {
        "no documento e não no firmware": sorted(doc - firmware - SO_NO_SIMULADOR),
        "no firmware e não no documento": sorted(firmware - doc),
    }


def test_o_simulador_nao_inventa_codigo_proprio():
    """A divergência que este bloco existe para impedir.

    O simulador é o que roda no CI, no Docker e na demonstração — ou seja, é o
    que produz os alarmes que todo mundo vê. Se ele usar um vocabulário próprio,
    o dia em que a placa entrar é o dia em que os alarmes mudam de nome.
    """
    simulador = _codigos_do_simulador()
    firmware = _codigos_do_firmware()

    assert len(simulador) >= 3, (
        f"o extrator achou só {sorted(simulador)} no simulador — ele parou de "
        f"casar")
    fora = simulador - firmware - SO_NO_SIMULADOR
    assert not fora, {
        "código que só o simulador conhece": sorted(fora),
        "dica": "use o mesmo do firmware, ou documente a exceção no §4",
    }


def test_a_placa_falsa_usa_o_vocabulario_do_firmware():
    """Ela é o documento executável: um código próprio aqui faria o teste passar
    com a bancada real emitindo outra coisa."""
    from fakes.placa_cnc import PlacaCNC

    placa = PlacaCNC(atraso_evento=0.0)
    emitidos = set()
    for e in placa.eventos_para("mover", {"dispenser_alvo": 99, "os_id": "OS-1"}):
        emitidos.add(e.get("codigo_erro"))
    placa.eventos_para("estado_celula", {"trava_ativa": True})
    for e in placa.eventos_para("mover", {"dispenser_alvo": 2, "os_id": "OS-1"}):
        emitidos.add(e.get("codigo_erro"))

    assert emitidos, "a placa falsa não emite `erro` nenhum"
    assert emitidos <= _codigos_do_firmware(), sorted(emitidos - _codigos_do_firmware())
