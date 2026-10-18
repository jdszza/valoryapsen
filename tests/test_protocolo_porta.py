# -*- coding: utf-8 -*-
"""O contrato da porta NR-12 (§9) — documento × placa falsa × repasse.

A porta é o quinto firmware da célula e o primeiro sem porta própria no mini
PC: ela viaja DENTRO da porta das telas. Por isso ela não entra em
`ADAPTERS_SERIAIS` (não há `LinkSerial` dela) e tem este arquivo próprio, com
as mesmas três cópias de sempre confrontadas:

  * `docs/PROTOCOLO_SERIAL.md` §9 — o documento;
  * `tests/fakes/placa_porta.py`  — o modelo da máquina de estados do firmware;
  * `dispenser-adapter/main.py`   — quem manda o comando (task 5, mais abaixo).

E o REPASSE da placa das telas é exercitado de verdade, por `socket://`, com o
`serial_link` do adapter do outro lado: o comando `porta_*` desce, o ACK que
volta é o da porta, e o heartbeat que some vira `porta_link_perdido`.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from fakes.placa_dispenser_tft import PlacaDispenserTFT
from fakes.placa_porta import ESTADOS, PlacaPorta

RAIZ_REPO = Path(__file__).resolve().parent.parent
DOC = RAIZ_REPO / "docs" / "PROTOCOLO_SERIAL.md"

EVENTOS_DA_PLACA_DAS_TELAS = {"porta_link_perdido", "porta_link_ok"}


def _secao() -> str:
    texto = DOC.read_text(encoding="utf-8")
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
    return _tabela(_secao().split("### Comandos", 1)[1].split("### ", 1)[0])


def doc_eventos() -> dict[str, list[str]]:
    return _tabela(_secao().split("### Eventos", 1)[1].split("**Vocabulário", 1)[0])


def _todos_os_eventos_da_porta() -> dict[str, dict]:
    """Um exemplar de cada evento, produzido pela encenação da porta falsa."""
    exemplos: dict[str, dict] = {}

    def guardar(eventos):
        for e in eventos:
            exemplos[e["tipo"]] = e

    porta = PlacaPorta()
    guardar([porta.boot(), porta.estado()])
    guardar(porta.passar_tag("FFFFFFFF"))                       # tag_negada
    guardar(porta.passar_tag("04A1B2C3"))                       # pedido_acesso
    guardar(porta.executar({"cmd": "porta_liberar", "cmd_id": 1, "janela_ms": 5000})[1])
    guardar(porta.abrir("frente"))                              # porta_aberta
    guardar(porta.fechar("frente"))                             # porta_fechada
    guardar(porta.passar_tag("04A1B2C3"))                       # rearme
    guardar(porta.passar_tag("04A1B2C3"))                       # pedido de novo
    guardar(porta.expirar())                                    # liberacao_expirada
    return exemplos


# ══════════════════════════════════════════════════════════════════════════════
# 1. Documento × placa falsa
# ══════════════════════════════════════════════════════════════════════════════

def test_os_extratores_enxergam_a_secao():
    """Guarda da guarda: extrator que para de casar deixa tudo verde."""
    assert len(doc_comandos()) >= 3
    assert len(doc_eventos()) >= 11


def test_a_placa_atende_exatamente_os_comandos_documentados():
    assert set(PlacaPorta.COMANDOS) == set(doc_comandos())


def test_a_placa_so_exige_campo_documentado():
    documentado = doc_comandos()
    for cmd, exigidos in PlacaPorta.COMANDOS.items():
        assert set(exigidos) <= set(documentado[cmd]), cmd


def test_todo_evento_da_porta_bate_campo_a_campo_com_o_documento():
    """Nome certo com campo a menos é o modo de falhar mais provável: o adapter
    repassa cru, e o campo que some vira coluna vazia no histórico."""
    documentado = doc_eventos()
    emitidos = _todos_os_eventos_da_porta()
    da_porta = set(documentado) - EVENTOS_DA_PLACA_DAS_TELAS
    assert set(emitidos) == da_porta, {
        "emitido e não documentado": sorted(set(emitidos) - da_porta),
        "documentado e não emitido": sorted(da_porta - set(emitidos)),
    }
    for tipo, exemplo in emitidos.items():
        assert set(exemplo) - {"tipo"} == set(documentado[tipo]), tipo


def test_o_vocabulario_de_estado_e_o_do_documento():
    trecho = _secao().split("* `estado` ∈", 1)[1].split("*", 1)[0]
    assert set(re.findall(r"`(\w+)`", trecho)) == set(ESTADOS)


# ══════════════════════════════════════════════════════════════════════════════
# 2. A máquina de estados (o modelo que o firmware implementa)
# ══════════════════════════════════════════════════════════════════════════════

def test_a_tag_nao_autoriza_nada_sozinha():
    porta = PlacaPorta()
    (pedido, estado) = porta.passar_tag("04A1B2C3")
    assert pedido["tipo"] == "pedido_acesso"
    assert estado["estado"] == "AGUARDANDO_CENTRAL" and not porta.led_pode_abrir


def test_liberar_sem_pedido_e_recusado():
    porta = PlacaPorta()
    ack, eventos = porta.executar({"cmd": "porta_liberar", "cmd_id": 1, "janela_ms": 5000})
    assert ack["resp"] == "erro" and not eventos and not porta.led_pode_abrir


def test_abrir_apaga_o_pode_abrir_e_fechar_exige_rearme():
    porta = PlacaPorta()
    porta.passar_tag("04A1B2C3")
    porta.executar({"cmd": "porta_liberar", "cmd_id": 1, "janela_ms": 5000})
    assert porta.led_pode_abrir
    aberta = porta.abrir("teto")[0]
    assert aberta["autorizada"] is True and not porta.led_pode_abrir
    segunda = porta.abrir("frente")[0]
    assert segunda == {**segunda, "porta": "frente", "autorizada": True}
    porta.fechar("teto")
    fechou = porta.fechar("frente")
    assert fechou[0]["tipo"] == "porta_fechada" and porta.aguardando_rearme
    rearme = porta.passar_tag("04A1B2C3")[0]
    assert rearme["tipo"] == "rearme"
    # E a tag seguinte já é um pedido normal — some o "passar duas vezes".
    assert porta.passar_tag("04A1B2C3")[0]["tipo"] == "pedido_acesso"


def test_abrir_sem_liberacao_e_sem_autorizacao_inclusive_com_rearme_pendente():
    porta = PlacaPorta()
    assert porta.abrir("frente")[0]["autorizada"] is False
    porta.fechar("frente")
    assert porta.aguardando_rearme
    assert porta.abrir("teto")[0]["autorizada"] is False


def test_negar_volta_a_fechada_sem_rearme():
    porta = PlacaPorta()
    porta.passar_tag("04A1B2C3")
    ack, eventos = porta.executar({"cmd": "porta_negar", "cmd_id": 1,
                                   "motivo": "maquina_nao_confirmou_parada"})
    assert ack["resp"] == "ok"
    assert eventos[-1] == {**eventos[-1], "tipo": "liberacao_expirada",
                           "motivo": "maquina_nao_confirmou_parada"}
    assert porta.estado_atual == "FECHADA" and not porta.aguardando_rearme


def test_janela_vencida_entra_em_rearme_e_central_mudo_nao():
    porta = PlacaPorta()
    porta.passar_tag("04A1B2C3")
    assert porta.expirar()[-1]["motivo"] == "central_nao_respondeu"
    assert not porta.aguardando_rearme
    porta.passar_tag("04A1B2C3")
    porta.executar({"cmd": "porta_liberar", "cmd_id": 1, "janela_ms": 5000})
    assert porta.expirar()[-1]["motivo"] == "janela_expirou"
    assert porta.aguardando_rearme


# ══════════════════════════════════════════════════════════════════════════════
# 3. O repasse da placa das telas, por socket://, com o serial_link do adapter
# ══════════════════════════════════════════════════════════════════════════════

pytest.importorskip("serial", reason="pyserial (tests/requirements-dev.txt)")


@pytest.fixture
def montagem():
    import importlib.util
    import sys
    import threading
    import time

    caminho = RAIZ_REPO / "dispenser-adapter" / "serial_link.py"
    spec = importlib.util.spec_from_file_location("apsen_sl_porta", caminho)
    sl = importlib.util.module_from_spec(spec)
    sys.modules["apsen_sl_porta"] = sl
    spec.loader.exec_module(sl)

    eventos: list[dict] = []
    lock = threading.Lock()

    def receber(payload):
        with lock:
            eventos.append(payload)

    porta = PlacaPorta()
    placa = PlacaDispenserTFT(intervalo_ping=0.2).iniciar()
    placa.conectar_porta(porta, heartbeat_s=0.05, vigia_s=0.25)
    link = sl.LinkSerial("dispenser_tft", url=placa.url, ack_timeout_s=1.0,
                         ao_receber_evento=receber, probe_assentar_s=0.0,
                         probe_espera_s=1.0, reconexao_s=0.05)
    link.iniciar()
    limite = time.time() + 5
    while not link.conectado and time.time() < limite:
        time.sleep(0.01)

    class M:
        pass
    m = M()
    m.sl, m.porta, m.placa, m.link = sl, porta, placa, link

    def tipos():
        with lock:
            return [e.get("tipo") for e in eventos]
    m.tipos = tipos

    def ate(cond, timeout=3.0):
        limite = time.time() + timeout
        while time.time() < limite:
            if cond():
                return True
            time.sleep(0.01)
        return False
    m.ate = ate
    yield m
    link.parar()
    placa.parar()
    sys.modules.pop("apsen_sl_porta", None)


def test_o_heartbeat_sobe_com_origem_porta(montagem):
    assert montagem.ate(lambda: "estado" in montagem.tipos())


def test_comando_porta_desce_e_o_ack_e_o_da_porta(montagem):
    montagem.placa.da_porta(montagem.porta.passar_tag("04A1B2C3"))
    assert montagem.ate(lambda: "pedido_acesso" in montagem.tipos())
    resposta = montagem.link.enviar_comando("porta_liberar", {"janela_ms": 4000})
    assert resposta["resp"] == "ok"
    assert montagem.porta.executados[-1]["cmd"] == "porta_liberar"
    assert montagem.ate(lambda: "liberada" in montagem.tipos())
    # A placa das telas não executou nada: o comando foi da porta.
    assert not [e for e in montagem.placa.executados if e["cmd"].startswith("porta_")]


def test_ack_negativo_da_porta_vira_ack_negativo(montagem):
    with pytest.raises(montagem.sl.AckNegativo):
        montagem.link.enviar_comando("porta_liberar", {"janela_ms": 4000})


def test_porta_estado_pede_um_estado_na_hora(montagem):
    montagem.link.enviar_comando("porta_estado", {})
    assert montagem.porta.executados[-1]["cmd"] == "porta_estado"


def test_heartbeat_que_some_vira_link_perdido_e_volta_com_ok_antes_do_estado(montagem):
    assert montagem.ate(lambda: "estado" in montagem.tipos())
    montagem.porta.silenciar()
    assert montagem.ate(lambda: "porta_link_perdido" in montagem.tipos())
    # Com o link caído, o comando é recusado NA HORA pela placa das telas.
    with pytest.raises(montagem.sl.AckNegativo, match="porta_sem_link"):
        montagem.link.enviar_comando("porta_estado", {})
    antes = len(montagem.tipos())
    montagem.porta.falar()
    assert montagem.ate(lambda: "porta_link_ok" in montagem.tipos()[antes:])
    depois = montagem.tipos()[antes:]
    assert depois.index("porta_link_ok") < len(depois) - 1
    assert depois[depois.index("porta_link_ok") + 1] == "estado"
    assert montagem.tipos().count("porta_link_perdido") == 1


# ══════════════════════════════════════════════════════════════════════════════
# 4. Documento × adapter — quem MANDA o comando
# ══════════════════════════════════════════════════════════════════════════════

def _adapter():
    return ast.parse((RAIZ_REPO / "dispenser-adapter" / "main.py").read_text(encoding="utf-8"))


def adapter_comandos_porta() -> set[str]:
    for no in ast.walk(_adapter()):
        if (isinstance(no, ast.Assign) and isinstance(no.targets[0], ast.Name)
                and no.targets[0].id == "_COMANDOS_PORTA"):
            return {c.value for c in no.value.keys}
    return set()


def adapter_campos_porta() -> dict[str, set[str]]:
    enviados = {}
    for no in ast.walk(_adapter()):
        if (isinstance(no, ast.Call) and isinstance(no.func, ast.Name)
                and no.func.id == "_enviar_porta" and len(no.args) == 2
                and isinstance(no.args[0], ast.Constant)
                and isinstance(no.args[1], ast.Dict)):
            enviados[no.args[0].value] = {c.value for c in no.args[1].keys}
    return enviados


def test_o_adapter_manda_exatamente_os_comandos_documentados():
    assert adapter_comandos_porta() == set(doc_comandos())
    assert set(adapter_campos_porta()) == set(doc_comandos()), (
        "todo comando documentado tem de ter uma rota que o envie")


def test_os_campos_que_o_adapter_envia_sao_os_documentados():
    documentado = doc_comandos()
    for cmd, campos in adapter_campos_porta().items():
        assert campos == set(documentado[cmd]), cmd
