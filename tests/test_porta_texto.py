# -*- coding: utf-8 -*-
"""O tradutor da porta NR-12 em campo — `porta-adapter/porta_texto.py` (§10).

A porta fala TEXTO (`EVT:<CODIGO>[;UID=<HEX>] | <texto>` e a linha dos fins de
curso), decide a tag sozinha e não recebe nada do PC. O tradutor transforma
cada linha nos eventos do vocabulário de sempre, na ORDEM da tabela do §10, e
só depois de uma linha que PROVE o estado da porta.

Três blocos:

  1. a tabela do §10, linha a linha — os eventos exatos, na ordem;
  2. CONTRA O FIRMWARE: todo `evento("X", ...)` do `nr12_oficial.ino` tem
     tratamento declarado, o formato que `evento()` e `imprimirSensores()`
     montam é o que o parser espera, e todo texto fixo que o receptor imprime
     sozinho é reconhecido como "não sobe". Lê C++ por texto, com PISO, e foi
     validado por MUTAÇÃO (bloco 3): extrator que para de casar deixaria tudo
     verde para sempre;
  3. as cópias dos dois `.ino` estão INTACTAS (sha256 fixado).
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from test_dispenser_firmware import _corpo_da_funcao, sem_comentarios

RAIZ_REPO = Path(__file__).resolve().parent.parent
MODULO = RAIZ_REPO / "porta-adapter" / "porta_texto.py"
PORTA_INO = RAIZ_REPO / "porta" / "nr12_oficial" / "nr12_oficial.ino"
RECEPTOR_INO = RAIZ_REPO / "porta" / "receptor_esp32" / "receptor_esp32.ino"

SHA_PORTA = "d1ac8141902d65ca855e04b0544d67fc56f3d0afb5dd536d03faf0f241a790c1"
SHA_RECEPTOR = "877964c38e7b78081ff47af2b465ce2db1645c1c2d0f661af6d9b9852187fde3"

UID = "04A1B2C3"
TS = "2026-10-22T10:00:00"


def _carregar():
    nome = "apsen_porta_texto"
    if nome in sys.modules:
        return sys.modules[nome]
    spec = importlib.util.spec_from_file_location(nome, MODULO)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo
    spec.loader.exec_module(modulo)
    return modulo


pt = _carregar()


def _linha(texto: str):
    return pt.interpretar_linha(texto)


def _aplicar(modelo, *textos, agora: float = 0.0) -> list[dict]:
    eventos = []
    for texto in textos:
        eventos += modelo.aplicar(_linha(texto), agora, ts=TS)
    return eventos


def _tipos(eventos) -> list[str]:
    return [e["tipo"] for e in eventos]


def _evt(codigo: str, uid: str | None = None, texto: str = "texto humano") -> str:
    """A linha exatamente como `evento()` do firmware a monta."""
    return "EVT:" + codigo + (f";UID={uid}" if uid else "") + " | " + texto


def _es(teto: str = "FECHADO", frente: str = "FECHADO") -> str:
    """A linha exatamente como `imprimirSensores()` a monta."""
    return f"   ES1(D4)={teto}  ES2(D18)={frente}  "


def _conhecido(estado="FECHADA", rearme=False):
    """Um modelo que já passou por uma linha que provou o estado."""
    m = pt.ModeloPorta()
    _aplicar(m, _evt("BOOT_OK"), _es())
    if estado == "LIBERADA":
        _aplicar(m, _evt("PAUSA", UID))
    elif rearme:
        _aplicar(m, _evt("PAUSA", UID), _evt("LIBERACAO_EXPIRADA"))
    return m


# ══════════════════════════════════════════════════════════════════════════════
# 1. A tabela do §10, linha a linha
# ══════════════════════════════════════════════════════════════════════════════

def test_boot_ok_sobe_boot_e_estado_fechada():
    m = pt.ModeloPorta()
    eventos = _aplicar(m, _evt("BOOT_OK"))
    assert _tipos(eventos) == ["boot", "estado"]
    assert eventos[0] == {"origem": "porta", "tipo": "boot", "teto": "fechada",
                          "frente": "fechada", "ts": TS}
    estado = eventos[1]
    assert estado["fonte"] == "adapter" and estado["estado"] == "FECHADA"
    assert estado["aguardando_rearme"] is False
    assert (estado["teto"], estado["frente"]) == ("fechada", "fechada")
    # A linha ES do boot, com nada mudado, não sobe nada.
    assert _aplicar(m, _es()) == []


def test_pausa_e_pedido_com_autorizacao_da_porta_e_liberada_e_estado():
    m = _conhecido()
    eventos = _aplicar(m, _evt("PAUSA", UID))
    assert _tipos(eventos) == ["pedido_acesso", "liberada", "estado"]
    assert eventos[0]["uid"] == UID and eventos[0]["autorizada_pela_porta"] is True
    assert eventos[1]["janela_ms"] == 10000
    assert eventos[2]["estado"] == "LIBERADA"
    assert (eventos[2]["teto"], eventos[2]["frente"]) == ("fechada", "fechada")


@pytest.mark.parametrize("codigo,autorizada,estado", [
    ("ALERTA_SEM_AUTORIZACAO", False, "ABERTA_SEM_AUTORIZACAO"),
    ("ALERTA_PORTA_ABERTA", True, "ABERTA_AUTORIZADA"),
])
@pytest.mark.parametrize("teto,frente,portas", [
    ("ABERTO", "FECHADO", ["teto"]),
    ("FECHADO", "ABERTO", ["frente"]),
    ("ABERTO", "ABERTO", ["teto", "frente"]),
])
def test_alerta_mais_linha_es_e_um_porta_aberta_por_porta(codigo, autorizada, estado,
                                                          teto, frente, portas):
    m = _conhecido(estado="LIBERADA") if autorizada else _conhecido()
    assert _aplicar(m, _evt(codigo)) == [], "subiu antes da linha ES"
    eventos = _aplicar(m, _es(teto, frente))
    assert _tipos(eventos) == ["porta_aberta"] * len(portas) + ["estado"]
    assert [e["porta"] for e in eventos[:-1]] == portas
    assert all(e["autorizada"] is autorizada for e in eventos[:-1])
    assert eventos[-1]["estado"] == estado


def test_sem_a_linha_es_no_prazo_sobe_com_porta_none():
    m = _conhecido()
    _aplicar(m, _evt("ALERTA_SEM_AUTORIZACAO"), agora=10.0)
    assert m.vencer_pendente(10.1, 0.3, ts=TS) == []
    eventos = m.vencer_pendente(10.31, 0.3, ts=TS)
    assert _tipos(eventos) == ["porta_aberta", "estado"]
    assert eventos[0]["porta"] is None and eventos[0]["autorizada"] is False
    assert eventos[1]["estado"] == "ABERTA_SEM_AUTORIZACAO"
    # A linha ES atrasada só atualiza o que mudou — não abre a porta de novo.
    assert _tipos(_aplicar(m, _es("FECHADO", "ABERTO"))) == ["estado"]


def test_o_estado_nao_sobe_entre_o_alerta_e_a_linha_es():
    """Um `estado` aberto sem porta, antes do `porta_aberta`, poria o central a
    decidir pelo estado o que o evento vai dizer em milissegundos."""
    m = _conhecido()
    _aplicar(m, _evt("ALERTA_SEM_AUTORIZACAO"))
    assert m.estado_evento(TS) is None


def test_liberacao_expirada_entra_em_rearme():
    m = _conhecido(estado="LIBERADA")
    eventos = _aplicar(m, _evt("LIBERACAO_EXPIRADA"))
    assert _tipos(eventos) == ["liberacao_expirada", "estado"]
    assert eventos[0]["motivo"] == "janela_expirou"
    assert eventos[1]["estado"] == "FECHADA" and eventos[1]["aguardando_rearme"] is True


def test_porta_fechada_entra_em_rearme_com_as_duas_fechadas():
    m = _conhecido(estado="LIBERADA")
    _aplicar(m, _evt("ALERTA_PORTA_ABERTA"), _es("FECHADO", "ABERTO"))
    eventos = _aplicar(m, _evt("PORTA_FECHADA"))
    assert _tipos(eventos) == ["porta_fechada", "estado"]
    assert eventos[1]["aguardando_rearme"] is True
    assert (eventos[1]["teto"], eventos[1]["frente"]) == ("fechada", "fechada")


def test_rearme_sobe_o_estado_fechada_ANTES_do_rearme():
    m = _conhecido(rearme=True)
    eventos = _aplicar(m, _evt("REARME_OK", UID))
    assert _tipos(eventos) == ["estado", "rearme"]
    assert eventos[0]["estado"] == "FECHADA" and eventos[0]["aguardando_rearme"] is False
    assert eventos[1]["uid"] == UID


def test_tag_negada_sobe_e_nao_muda_o_estado():
    m = _conhecido()
    antes = m.resumo()
    eventos = _aplicar(m, _evt("TAG_NEGADA", "DEADBEEF"))
    assert eventos == [{"origem": "porta", "tipo": "tag_negada", "uid": "DEADBEEF", "ts": TS}]
    assert {k: v for k, v in m.resumo().items() if k != "ultimo_uid" and k != "ultimo_codigo"} \
        == {k: v for k, v in antes.items() if k != "ultimo_uid" and k != "ultimo_codigo"}


@pytest.mark.parametrize("codigo", ["REG_INICIO", "REG_SALVA", "REG_JA_EXISTE",
                                    "REG_ERRO", "REG_CANCELADO", "REG_FIM"])
def test_registro_de_tag_e_bancada_e_nao_sobe(codigo):
    m = _conhecido()
    assert _aplicar(m, _evt(codigo, UID)) == []
    assert m.ultimo_codigo_bancada == codigo


def test_codigo_desconhecido_nao_sobe_e_e_contado():
    m = _conhecido()
    assert _aplicar(m, _evt("VIVO")) == []
    assert m.codigos_desconhecidos == 1


@pytest.mark.parametrize("texto", [
    "",
    "=== RECEPTOR Serial2 | segundo ESP32 ===",
    "Serial2: RX=GPIO16 (pull-up ON)  TX=GPIO17  baud=115200",
    "Aguardando mensagens do ESP32 da porta...",
    "Comandos: X (hex on/off) | F (filtro de ruido on/off) | T (enviar teste p/ a porta) | S (estatisticas) | H (ajuda)",
    ">> modo HEX LIGADO",
    ">> bytes=10  linhas=1  descartados=0  filtro=ON  ultimo byte ha 5 ms",
    "[linha longa sem fim] EVT:PAUSA;UID=04A1B2C3 | lixo" + "x" * 400,
])
def test_linhas_do_receptor_nao_sobem(texto):
    m = _conhecido()
    assert _linha(texto) is None
    assert pt.linha_do_receptor(texto)
    assert _aplicar(m, texto) == []


def test_aviso_do_receptor_vira_diagnostico_e_a_primeira_linha_da_porta_o_apaga():
    m = pt.ModeloPorta()
    aviso = ("[aviso] Nenhum byte recebido no Serial2 ate agora. Confira: TX(17) da "
             "porta -> RX(16) daqui, GND comum, baud 115200.")
    assert _linha(aviso).tipo == "receptor_sem_dados"
    assert _aplicar(m, aviso) == []
    assert m.receptor_sem_dados is True
    _aplicar(m, _evt("TAG_NEGADA", UID))
    assert m.receptor_sem_dados is False


# ── estado desconhecido ──────────────────────────────────────────────────────

def test_nada_sobe_com_o_estado_desconhecido():
    m = pt.ModeloPorta()
    assert m.estado_evento(TS) is None
    assert _aplicar(m, _evt("TAG_NEGADA", UID), _es(), _evt("REG_INICIO")) == []
    assert not m.conhecido, "TAG_NEGADA, REG_* e a linha ES não provam nada"


@pytest.mark.parametrize("linhas", [
    [_evt("BOOT_OK")],
    [_evt("PAUSA", UID)],
    [_evt("LIBERACAO_EXPIRADA")],
    [_evt("PORTA_FECHADA")],
    [_evt("REARME_OK", UID)],
    [_evt("ALERTA_SEM_AUTORIZACAO")],
    [_evt("ALERTA_PORTA_ABERTA")],
], ids=lambda l: l[0].split(" ")[0])
def test_toda_linha_que_prova_o_estado_o_torna_conhecido(linhas):
    m = pt.ModeloPorta()
    _aplicar(m, *linhas)
    assert m.conhecido


def test_esquecer_volta_a_desconhecido():
    m = _conhecido()
    m.esquecer()
    assert not m.conhecido and m.estado_evento(TS) is None
    assert _aplicar(m, _evt("TAG_NEGADA", UID)) == []


# ── tolerância do parser ─────────────────────────────────────────────────────

@pytest.mark.parametrize("texto", [
    "EVT:PAUSA;UID=04a1b2c3 | Acesso autorizado. Maquina em pausa. Pode abrir.\r",
    "  EVT:PAUSA;UID=04A1B2C3 | x",
    "~#@EVT:PAUSA;UID=04A1B2C3 | x",
    "  -> EVT:PAUSA;UID=04A1B2C3 | x",
    "EVT:PAUSA;UID=04A1B2C3 | texto | com | barras",
])
def test_lixo_antes_crlf_e_uid_em_minuscula(texto):
    linha = _linha(texto)
    assert linha == pt.Linha("evt", codigo="PAUSA", uid=UID)


def test_so_o_que_vem_antes_do_separador_e_maquina():
    assert _linha("EVT:REARME_OK;UID=AB | EVT:PAUSA").codigo == "REARME_OK"
    assert _linha("EVT:BOOT_OK | Porta fechada. Sistema armado.").uid is None
    assert _linha("EVT:BOOT_OK lixo | x") is None, "lixo DEPOIS do código não é código"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Contra o firmware
# ══════════════════════════════════════════════════════════════════════════════

# Pisos: quantos símbolos cada extrator tem de achar no firmware real.
PISO_CODIGOS = 14
PISO_TEXTOS_RECEPTOR = 10


def _sem_crlf(caminho: Path) -> str:
    return caminho.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")


def codigos_do_firmware(fonte: str) -> list[str]:
    """Todo `evento("X", ...)` do `.ino` da porta (com repetição)."""
    return re.findall(r'\bevento\(\s*"([A-Z0-9_]+)"', sem_comentarios(fonte))


def formato_do_evento(fonte: str) -> list[str]:
    """Os literais de `evento()`, na ordem em que montam a linha."""
    corpo = _corpo_da_funcao(sem_comentarios(fonte), "static void evento(")
    corpo = corpo[corpo.index("{"):]          # sem o default `uid = ""` da assinatura
    return re.findall(r'"((?:[^"\\]|\\.)*)"', corpo)


def nomes_dos_sensores(fonte: str) -> list[str]:
    """Os nomes da tabela `endStops[]`, na ordem (ES1 primeiro)."""
    codigo = sem_comentarios(fonte)
    bloco = codigo[codigo.index("EndStop endStops[]"):]
    bloco = bloco[: bloco.index("};")]
    return re.findall(r'\{\s*PIN_ES\d\s*,\s*"([^"]+)"', bloco)


def niveis_dos_sensores(fonte: str) -> list[str]:
    corpo = _corpo_da_funcao(sem_comentarios(fonte), "static void imprimirSensores(")
    return [s.strip() for s in re.findall(r'"(FECHADO\s*|ABERTO\s*)"', corpo)]


def textos_do_receptor(fonte: str) -> list[str]:
    """O texto fixo de todo Serial.print/println/printf do receptor (não o
    Serial2): o literal até o primeiro especificador de formato."""
    codigo = sem_comentarios(fonte)
    textos = []
    for m in re.finditer(r'\bSerial\.(?:print|println|printf)\(\s*"((?:[^"\\]|\\.)*)"', codigo):
        texto = m.group(1).replace("\\n", "")
        textos.append(texto.split("%", 1)[0] if "%" in texto else texto)
    return textos


# Impresso só no modo HEX, que se liga com `X` pelo Monitor Serial — e o PC
# nunca escreve na COM do receptor. O `"  -> "` é o prefixo da linha repassada
# nesse modo, e o parser tolera o lixo antes do `EVT:` justamente por isso.
TEXTOS_DO_MODO_HEX = {"", "  -> "}
# Continuações no MEIO de uma linha: o `estatisticas()` do receptor abre a
# linha com `printf(">> bytes=... ")` e a fecha com um destes. A linha inteira
# começa com ">> ", que é o que o adapter vê.
CONTINUACOES = {"NADA recebido ainda.", "ultimo byte ha "}


def problemas_contra_o_firmware(porta: str, receptor: str) -> list[str]:
    """Tudo o que o adapter deixaria de entender. Lista vazia = em dia."""
    problemas = []
    codigos = codigos_do_firmware(porta)
    if len(codigos) < PISO_CODIGOS:
        problemas.append(f"extrator de códigos achou só {len(codigos)}")
    tratados = set(pt.CODIGOS_QUE_SOBEM) | set(pt.CODIGOS_DE_BANCADA)
    for codigo in sorted(set(codigos) - tratados):
        problemas.append(f"código {codigo} sem tratamento declarado em porta_texto.py")
    for codigo in sorted(tratados - set(codigos)):
        problemas.append(f"porta_texto.py trata {codigo}, que o firmware não emite")

    formato = formato_do_evento(porta)
    if formato != [pt.PREFIXO_EVT, pt.SEP_UID, pt.SEP_TEXTO]:
        problemas.append(f"evento() monta a linha com {formato}")

    nomes = nomes_dos_sensores(porta)
    if nomes != [pt.SENSOR_TETO, pt.SENSOR_FRENTE]:
        problemas.append(f"endStops[] nomeia {nomes}")
    niveis = niveis_dos_sensores(porta)
    if sorted(niveis) != sorted(pt.NIVEL):
        problemas.append(f"imprimirSensores() escreve {niveis}")

    textos = textos_do_receptor(receptor)
    if len(textos) < PISO_TEXTOS_RECEPTOR:
        problemas.append(f"extrator do receptor achou só {len(textos)}")
    for texto in sorted(CONTINUACOES - set(textos)):
        problemas.append(f"a continuação {texto!r} sumiu do receptor — revise a lista")
    for texto in textos:
        if texto in TEXTOS_DO_MODO_HEX or texto in CONTINUACOES:
            continue
        if not pt.linha_do_receptor(texto) or pt.interpretar_linha(texto) not in (
                None, pt.Linha("receptor_sem_dados")):
            problemas.append(f"o receptor imprime {texto!r} e o adapter não o reconhece")
    return problemas


@pytest.fixture(scope="module")
def porta_ino() -> str:
    return _sem_crlf(PORTA_INO)


@pytest.fixture(scope="module")
def receptor_ino() -> str:
    return _sem_crlf(RECEPTOR_INO)


def test_os_extratores_enxergam_o_firmware(porta_ino, receptor_ino):
    """Guarda da guarda: extrator que para de casar deixa tudo verde."""
    assert len(codigos_do_firmware(porta_ino)) >= PISO_CODIGOS
    assert len(set(codigos_do_firmware(porta_ino))) == 14
    assert len(textos_do_receptor(receptor_ino)) >= PISO_TEXTOS_RECEPTOR
    assert formato_do_evento(porta_ino) and nomes_dos_sensores(porta_ino)


def test_o_adapter_entende_tudo_que_o_firmware_emite(porta_ino, receptor_ino):
    assert problemas_contra_o_firmware(porta_ino, receptor_ino) == []


def test_a_linha_montada_como_o_firmware_monta_e_entendida(porta_ino):
    """O formato extraído, montado de volta, passa pelo parser."""
    prefixo, sep_uid, sep_texto = formato_do_evento(porta_ino)
    linha = prefixo + "PAUSA" + sep_uid + UID + sep_texto + "Acesso autorizado."
    assert _linha(linha) == pt.Linha("evt", codigo="PAUSA", uid=UID)
    teto, frente = nomes_dos_sensores(porta_ino)
    es = "   " + teto + "=" + "ABERTO  " + frente + "=" + "FECHADO  "
    assert _linha(es) == pt.Linha("sensores", teto="aberta", frente="fechada")


# ══════════════════════════════════════════════════════════════════════════════
# 3. Mutação: cada quebra deliberada tem de dar vermelho
# ══════════════════════════════════════════════════════════════════════════════

MUTACOES_PORTA = {
    "código renomeado": ('evento("PAUSA"', 'evento("PAUSA_NOVA"'),
    "código novo": ('evento("BOOT_OK", "Porta fechada. Sistema armado.");',
                    'evento("BOOT_OK", "Porta fechada. Sistema armado.");\n'
                    '    evento("VIVO", "linha de vida");'),
    "código que sumiu": ('evento("REG_FIM", "Saiu do modo registro. Operacao normal.");',
                         'Serial.println("Saiu do modo registro.");'),
    "prefixo EVT trocado": ('String linha = "EVT:";', 'String linha = "EV:";'),
    "separador do UID trocado": ('linha += ";UID=";', 'linha += ";ID=";'),
    "separador do texto trocado": ('linha += " | ";', 'linha += " - ";'),
    "nome do ES1 trocado": ('"ES1(D4)",  true', '"ES1(D5)",  true'),
    "ES1 e ES2 invertidos": ('{ PIN_ES1, "ES1(D4)",  true, true, 0 },\n  { PIN_ES2, "ES2(D18)", true, true, 0 }',
                             '{ PIN_ES2, "ES2(D18)", true, true, 0 },\n  { PIN_ES1, "ES1(D4)",  true, true, 0 }'),
    "nível trocado": ('"FECHADO  "', '"FECHADA  "'),
}
MUTACOES_RECEPTOR = {
    "banner trocado": ('"=== RECEPTOR Serial2', '"=== RX Serial2'),
    "linha nova do receptor": ('Serial.println("Aguardando mensagens do ESP32 da porta...");',
                               'Serial.println("Aguardando mensagens do ESP32 da porta...");\n'
                               '  Serial.println("Pronto.");'),
    "aviso reescrito": ('"[aviso] Nenhum byte recebido no Serial2', '"[aviso] Serial2 mudo'),
}


@pytest.mark.parametrize("nome", sorted(MUTACOES_PORTA))
def test_mutacao_da_porta_e_vermelha(nome, porta_ino, receptor_ino):
    antes, depois = MUTACOES_PORTA[nome]
    assert antes in porta_ino, f"a mutação '{nome}' não encontrou o trecho"
    mutado = porta_ino.replace(antes, depois)
    assert problemas_contra_o_firmware(mutado, receptor_ino), nome


@pytest.mark.parametrize("nome", sorted(MUTACOES_RECEPTOR))
def test_mutacao_do_receptor_e_vermelha(nome, porta_ino, receptor_ino):
    antes, depois = MUTACOES_RECEPTOR[nome]
    assert antes in receptor_ino, f"a mutação '{nome}' não encontrou o trecho"
    mutado = receptor_ino.replace(antes, depois)
    assert problemas_contra_o_firmware(porta_ino, mutado), nome


# ══════════════════════════════════════════════════════════════════════════════
# 4. As cópias dos firmwares estão intactas
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("caminho,esperado", [(PORTA_INO, SHA_PORTA),
                                              (RECEPTOR_INO, SHA_RECEPTOR)],
                         ids=["nr12_oficial", "receptor_esp32"])
def test_as_copias_dos_firmwares_estao_intactas(caminho, esperado):
    """\\r\\n normalizado: o checkout no Windows troca o fim de linha sem
    ninguém ter mexido (a mesma regra de `test_visao_intocada.py`)."""
    real = hashlib.sha256(caminho.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    assert real == esperado, (
        f"{caminho.relative_to(RAIZ_REPO)} mudou: a cópia tem de ser IGUAL ao que "
        f"está gravado na placa — atualize as duas juntas (e este sha256).")
