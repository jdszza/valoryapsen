# -*- coding: utf-8 -*-
"""O contrato da porta NR-12 — §9 (firmware JSON) e §10 (o que está em campo).

**§9** descreve o firmware JSON `porta/porta_nr12/`, que continua no
repositório mas NÃO está gravado em campo desde 22/10/2026. As seções 1 a 3
confrontam o documento com a placa falsa dele:

  * `docs/PROTOCOLO_SERIAL.md` §9 — o documento;
  * `tests/fakes/placa_porta.py`  — a placa falsa, modelo da máquina de estados
                                    do firmware JSON;
  * o transporte exercitado de verdade, por `socket://`, com o `serial_link`
    do outro lado do fio: ping/pong, o comando `porta_*` descendo com o ACK da
    porta, e a porta que para de falar.

A antiga seção 4 ("documento × adapter — quem MANDA o comando") saiu: o
`porta-adapter` não manda mais comando nenhum (§10).

**§10** é o contrato de campo: a porta fala texto, o adapter só lê e traduz.
A seção 4 daqui confronta a tabela do §10 com `porta-adapter/porta_texto.py` —
os códigos `EVT:` tratados e os eventos que sobem.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from fakes.placa_porta import ESTADOS, PlacaPorta

RAIZ_REPO = Path(__file__).resolve().parent.parent
DOC = RAIZ_REPO / "docs" / "PROTOCOLO_SERIAL.md"

# Os dois de vigia são do ADAPTER: quem perdeu o link não tem como avisar.
EVENTOS_DO_ADAPTER = {"porta_link_perdido", "porta_link_ok"}


def _modelo(**kw) -> PlacaPorta:
    """A máquina de estados, sem fio: a seção 2 testa só a decisão.

    A `PlacaFalsa` abre o socket de escuta no construtor; fechado aqui, sem
    `iniciar()`, o que a encenação põe "no fio" vai para lugar nenhum.
    """
    porta = PlacaPorta(**kw)
    porta.parar()
    return porta


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

    porta = _modelo()
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
    da_porta = set(documentado) - EVENTOS_DO_ADAPTER
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
    porta = _modelo()
    (pedido, estado) = porta.passar_tag("04A1B2C3")
    assert pedido["tipo"] == "pedido_acesso"
    assert estado["estado"] == "AGUARDANDO_CENTRAL" and not porta.led_pode_abrir


def test_liberar_sem_pedido_e_recusado():
    porta = _modelo()
    ack, eventos = porta.executar({"cmd": "porta_liberar", "cmd_id": 1, "janela_ms": 5000})
    assert ack["resp"] == "erro" and not eventos and not porta.led_pode_abrir


def test_abrir_apaga_o_pode_abrir_e_fechar_exige_rearme():
    porta = _modelo()
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
    porta = _modelo()
    assert porta.abrir("frente")[0]["autorizada"] is False
    porta.fechar("frente")
    assert porta.aguardando_rearme
    assert porta.abrir("teto")[0]["autorizada"] is False


def test_negar_volta_a_fechada_sem_rearme():
    porta = _modelo()
    porta.passar_tag("04A1B2C3")
    ack, eventos = porta.executar({"cmd": "porta_negar", "cmd_id": 1,
                                   "motivo": "maquina_nao_confirmou_parada"})
    assert ack["resp"] == "ok"
    assert eventos[-1] == {**eventos[-1], "tipo": "liberacao_expirada",
                           "motivo": "maquina_nao_confirmou_parada"}
    assert porta.estado_atual == "FECHADA" and not porta.aguardando_rearme


def test_janela_vencida_entra_em_rearme_e_central_mudo_nao():
    porta = _modelo()
    porta.passar_tag("04A1B2C3")
    assert porta.expirar()[-1]["motivo"] == "central_nao_respondeu"
    assert not porta.aguardando_rearme
    porta.passar_tag("04A1B2C3")
    porta.executar({"cmd": "porta_liberar", "cmd_id": 1, "janela_ms": 5000})
    assert porta.expirar()[-1]["motivo"] == "janela_expirou"
    assert porta.aguardando_rearme


# ══════════════════════════════════════════════════════════════════════════════
# 3. A porta pela USB dela, por socket://, com o serial_link do outro lado
# ══════════════════════════════════════════════════════════════════════════════

pytest.importorskip("serial", reason="pyserial (tests/requirements-dev.txt)")

# A cópia de `serial_link.py` que o firmware JSON usaria. O `porta-adapter`
# não tem mais a dele (em campo ele só lê texto, §10); as três que sobraram são
# idênticas (`tests/test_serial_link.py`), então qualquer uma serve.
PASTA_SERIAL_LINK = "cnc-adapter"


@pytest.fixture
def montagem():
    import importlib.util
    import sys
    import threading
    import time

    caminho = RAIZ_REPO / PASTA_SERIAL_LINK / "serial_link.py"
    spec = importlib.util.spec_from_file_location("apsen_sl_porta", caminho)
    sl = importlib.util.module_from_spec(spec)
    sys.modules["apsen_sl_porta"] = sl
    spec.loader.exec_module(sl)

    eventos: list[dict] = []
    lock = threading.Lock()

    def receber(payload):
        with lock:
            eventos.append(payload)

    porta = PlacaPorta(intervalo_ping=0.2, heartbeat_s=0.05).iniciar()
    link = sl.LinkSerial("porta", url=porta.url, ack_timeout_s=1.0,
                         ao_receber_evento=receber, probe_assentar_s=0.0,
                         probe_espera_s=1.0, reconexao_s=0.05)
    link.iniciar()
    limite = time.time() + 5
    while not link.conectado and time.time() < limite:
        time.sleep(0.01)

    class M:
        pass
    m = M()
    m.sl, m.porta, m.link = sl, porta, link

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
    porta.parar()
    sys.modules.pop("apsen_sl_porta", None)


def test_a_porta_pinga_e_o_adapter_responde_pong(montagem):
    """Quem inicia o ping é a placa, e quem responde é o lado do adapter —
    não mais a placa das telas."""
    assert montagem.ate(lambda: montagem.porta.pongs_recebidos > 0)
    assert montagem.porta.sessao_adapter is not None


def test_o_heartbeat_sobe_com_origem_porta(montagem):
    assert montagem.ate(lambda: "estado" in montagem.tipos())


def test_comando_porta_desce_e_o_ack_e_o_da_porta(montagem):
    montagem.porta.passar_tag("04A1B2C3")
    assert montagem.ate(lambda: "pedido_acesso" in montagem.tipos())
    resposta = montagem.link.enviar_comando("porta_liberar", {"janela_ms": 4000})
    assert resposta["resp"] == "ok"
    assert montagem.porta.executados[-1]["cmd"] == "porta_liberar"
    assert montagem.ate(lambda: "liberada" in montagem.tipos())


def test_ack_negativo_da_porta_vira_ack_negativo(montagem):
    with pytest.raises(montagem.sl.AckNegativo, match="sem_pedido_pendente"):
        montagem.link.enviar_comando("porta_liberar", {"janela_ms": 4000})


def test_comando_fora_do_contrato_e_ack_negativo(montagem):
    with pytest.raises(montagem.sl.AckNegativo, match="comando desconhecido"):
        montagem.link.enviar_comando("slot", {"dispenser_id": 1})


def test_porta_estado_pede_um_estado_na_hora(montagem):
    montagem.link.enviar_comando("porta_estado", {})
    assert montagem.porta.executados[-1]["cmd"] == "porta_estado"


def test_cmd_id_repetido_nao_executa_de_novo(montagem):
    """A idempotência é da placa, como em todas: reenviar `porta_liberar`
    acenderia o "pode abrir" duas vezes para um pedido só."""
    montagem.porta.passar_tag("04A1B2C3")
    ack, _ = montagem.porta.executar({"cmd": "porta_liberar", "cmd_id": 50,
                                      "janela_ms": 4000})
    assert ack["resp"] == "ok"
    repetido, eventos = montagem.porta.executar({"cmd": "porta_liberar", "cmd_id": 50,
                                                 "janela_ms": 4000})
    assert repetido == {"resp": "ok", "cmd_id": 50, "repetido": True} and not eventos


def test_porta_que_para_de_falar_some_do_fio_inteira(montagem):
    """`silenciar` é a placa travada com a USB de pé: sem heartbeat, sem ping
    e sem ACK. É o caso que só a vigia do heartbeat do adapter pega."""
    assert montagem.ate(lambda: "estado" in montagem.tipos())
    montagem.porta.silenciar()
    import time
    time.sleep(0.15)
    antes = len(montagem.tipos())
    time.sleep(0.4)                       # ~8 heartbeats a 50 ms
    assert len(montagem.tipos()) == antes, "a porta travada continuou falando"
    with pytest.raises(montagem.sl.SemAck):
        montagem.link.enviar_comando("porta_estado", {}, ack_timeout_s=0.3)
    assert montagem.link.conectado, "silenciar não é cabo solto"
    montagem.porta.falar()
    assert montagem.ate(lambda: len(montagem.tipos()) > antes)


# ══════════════════════════════════════════════════════════════════════════════
# 4. §10 × tradutor — o que está em campo
# ══════════════════════════════════════════════════════════════════════════════

TRADUTOR = RAIZ_REPO / "porta-adapter" / "porta_texto.py"


def _secao_10() -> str:
    texto = DOC.read_text(encoding="utf-8")
    return texto.split("## 10. Porta NR-12 em campo", 1)[1].split("\n## ", 1)[0]


def _tabela_10() -> list[list[str]]:
    bloco = _secao_10().split("### Tradução", 1)[1].split("### ", 1)[0]
    linhas = []
    for crua in bloco.splitlines():
        crua = crua.strip()
        if crua.startswith("|") and not crua.startswith("|---"):
            linhas.append([c.strip() for c in crua.strip("|").split("|")])
    return linhas[1:]                       # sem o cabeçalho


def doc10_codigos() -> set[str]:
    return {c for linha in _tabela_10() for c in re.findall(r"`EVT:(\w+)`", linha[0])}


def doc10_eventos() -> set[str]:
    return {e for linha in _tabela_10() for e in re.findall(r"`(\w+)`", linha[2])}


def _constante(arvore, nome: str) -> set[str]:
    for no in ast.walk(arvore):
        if (isinstance(no, ast.Assign) and isinstance(no.targets[0], ast.Name)
                and no.targets[0].id == nome):
            return {e.value for e in no.value.elts}
    return set()


def tradutor_codigos() -> set[str]:
    arvore = ast.parse(TRADUTOR.read_text(encoding="utf-8"))
    return _constante(arvore, "CODIGOS_QUE_SOBEM") | _constante(arvore, "CODIGOS_DE_BANCADA")


def tradutor_eventos() -> set[str]:
    """O primeiro argumento de toda chamada `self._ev("tipo", ...)`."""
    arvore = ast.parse(TRADUTOR.read_text(encoding="utf-8"))
    return {no.args[0].value for no in ast.walk(arvore)
            if isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)
            and no.func.attr == "_ev" and no.args and isinstance(no.args[0], ast.Constant)}


def test_os_extratores_enxergam_a_secao_10():
    """Guarda da guarda, com piso: 14 códigos e 9 eventos hoje."""
    assert len(doc10_codigos()) >= 14 and len(tradutor_codigos()) >= 14
    assert len(doc10_eventos()) >= 9 and len(tradutor_eventos()) >= 9


def test_a_secao_10_lista_exatamente_os_codigos_que_o_tradutor_trata():
    assert doc10_codigos() == tradutor_codigos()


def test_a_secao_10_lista_exatamente_os_eventos_que_o_tradutor_sobe():
    assert doc10_eventos() == tradutor_eventos()


def test_os_eventos_do_tradutor_sao_do_vocabulario_da_secao_9():
    """O central não mudou de vocabulário: todo evento que sobe já existia."""
    assert tradutor_eventos() <= set(doc_eventos())
