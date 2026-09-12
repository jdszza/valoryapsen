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

from conftest import ADAPTERS, ADAPTERS_SERIAIS
from fakes.placa_cnc import PlacaCNC
from fakes.placa_dispenser import PlacaDispenser
from fakes.placa_weight import PlacaWeight

RAIZ_REPO = Path(__file__).resolve().parent.parent
DOC = RAIZ_REPO / "docs" / "PROTOCOLO_SERIAL.md"
PASTA = {nome: pasta for nome, pasta, _ in ADAPTERS}
PLACAS = {"dispenser": PlacaDispenser, "cnc": PlacaCNC, "weight": PlacaWeight}

# Seção do documento que descreve cada subsistema.
SECAO = {"dispenser": "## 3.", "cnc": "## 4.", "weight": "## 5."}


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
    for crua in bloco.splitlines():
        crua = crua.strip()
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


def doc_comandos(doc: str, subsistema: str) -> dict[str, dict]:
    """`cmd` -> {campo: obrigatório?} da tabela "Comandos" da seção."""
    bloco = _secao(doc, subsistema).split("### Comandos", 1)[1].split("### ", 1)[0]
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
    for no in ast.walk(arvore):
        if (isinstance(no, ast.Assign) and no.targets
                and isinstance(no.targets[0], ast.Name)
                and no.targets[0].id == "_ROTAS_SIM"):
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
    enviados: dict[str, set[str]] = {}
    for no in ast.walk(arvore):
        if not (isinstance(no, ast.Call) and isinstance(no.func, ast.Name)
                and no.func.id == "_enviar" and len(no.args) == 2):
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
        "mover": {"dispenser_alvo": 3, "os_id": "OS-1", "posicao_x": 240.0,
                  "posicao_y": -150.0, "ciclo_atual": 1, "total_ciclos": 3},
        "homing": {"os_id": "OS-1", "posicao_x": -120.0, "posicao_y": 0.0},
    },
    "weight": {
        "tara": {"os_id": "OS-1"},
        "pesar": {"os_id": "OS-1", "slot_id": 1, "quantidade_esperada": 10,
                  "quantidade_real": 10, "peso_unitario_g": 50.0},
    },
}

# Eventos que a placa emite fora do ciclo comando → resultado.
EXTRAS_DA_PLACA = {
    "dispenser": lambda p: [p.telemetria(1)],
    "cnc":       lambda p: [p.movendo("OS-1", 3, 120.0, -150.0)],
    "weight":    lambda p: [p.telemetria()],
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
    '{"resp":"pong","epoch":<unix>}',
    '{"cmd":"<nome>","cmd_id":<n>,...campos}',
    '{"resp":"ok","cmd_id":<n>}',
    '{"resp":"erro","cmd_id":<n>,"msg":"..."}',
    '{"evento":{...payload...}}',
])
def test_as_mensagens_de_servico_estao_no_documento(doc, trecho):
    """São iguais nos três subsistemas, e é por elas que a porta é identificada."""
    assert trecho in doc, f"{trecho} sumiu do documento"


@pytest.mark.parametrize("simbolo", ["cmd", "resp", "pong", "epoch", "cmd_id",
                                     "evento", "ping", "sub", "msg"])
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


def test_o_documento_lista_os_tres_subsistemas_e_so_eles(doc):
    """O `vision-adapter` ficou de fora da migração — e a ausência é decisão."""
    subs = set(re.findall(r'\{"cmd":"ping","sub":"(\w+)"\}', doc))
    assert subs <= set(ADAPTERS_SERIAIS) | {"<subsistema>"}
    for subsistema in ADAPTERS_SERIAIS:
        assert f"`{subsistema}`" in doc
    assert "vision" not in doc.lower().split("## 3.")[1]


# ══════════════════════════════════════════════════════════════════════════════
# 4. Guarda da guarda: a extração precisa ENXERGAR
# ══════════════════════════════════════════════════════════════════════════════
#
# A comparação é por texto, dos dois lados: tabela markdown de um, AST e regex
# do outro. Um extrator que para de casar não faz nada acusar — ele deixa TODOS
# os testes acima verdes para sempre, inclusive com o protocolo quebrado. É a
# mesma guarda que `tests/test_protocolo_serial.py` carrega no último bloco, e
# ela existe lá porque o erro já aconteceu.

MINIMOS = {
    "dispenser": {"comandos": 3, "eventos": 6},
    "cnc":       {"comandos": 2, "eventos": 6},
    "weight":    {"comandos": 2, "eventos": 5},
}


@pytest.mark.parametrize("subsistema", ADAPTERS_SERIAIS)
def test_a_leitura_do_documento_acha_o_que_deve(doc, subsistema):
    comandos = doc_comandos(doc, subsistema)
    eventos = doc_eventos(doc, subsistema)
    assert len(comandos) >= MINIMOS[subsistema]["comandos"], comandos
    assert len(eventos) >= MINIMOS[subsistema]["eventos"], eventos
    # Tabela lida sem campo nenhum é tabela não lida.
    assert all(campos for campos in comandos.values()), comandos
    assert sum(len(c) for c in eventos.values()) >= 20, eventos


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
    assert len(emitidos) >= 3, sorted(emitidos)
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
