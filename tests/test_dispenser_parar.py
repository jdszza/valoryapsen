# -*- coding: utf-8 -*-
"""`parar` — o dispenser termina a unidade, para, e diz quantas caíram.

Com a porta NR-12 aberta o central para a célula inteira, e o dispenser que
estiver no meio de uma dispensa TERMINA a unidade atual e para. O central
precisa saber quantas já caíram para retomar o slot pedindo só o que falta —
sem a contagem, retomar seria adivinhar, e adivinhar aqui é dose dobrada ou
dose curta no leito.

Pelo adapter, com a placa falsa dos mecanismos por `socket://` (a mesma regra
que o firmware implementa), e pelo simulador HTTP, que é o transporte default.
"""
import time

import pytest

from fakes.placa_dispenser import PlacaDispenser
from test_dispenser_tft import adapter_com_telas  # noqa: F401 — fixture
from test_serial_link import _ate

pytest.importorskip("serial", reason="pyserial (tests/requirements-dev.txt)")


@pytest.fixture
def montagem(adapter_com_telas):  # noqa: F811
    m = adapter_com_telas(mecanismos_serial=True)
    m.placa_disp.dispensa_lenta = True
    return m


def _carregar(m, slot=2, quantidade=5):
    m.chamar(m.modulo.cmd_carregar(m.modulo.ComandoCarregarReq(
        dispenser_id=slot, medicamento="DONAREN", sku="APSEN-002",
        categoria="x", quantidade=quantidade, os_id="OS-1")))


def _no_central(m, tipo):
    return [c["json"] for c in m.cliente.posts
            if c["url"].endswith("/api/v1/eventos/dispenser") and c["json"]["tipo"] == tipo]


def test_parar_com_3_de_5_emite_dispensado_parcial_3_de_5(montagem):
    m = montagem
    _carregar(m)
    m.chamar(m.modulo.cmd_dispensar(m.modulo.ComandoDispensarReq(dispenser_id=2, os_id="OS-1")))
    m.placa_disp.avancar(3)
    m.chamar(m.modulo.cmd_parar(m.modulo.ComandoPararReq(motivo="porta_aberta")))

    assert _ate(lambda: _no_central(m, "dispensado_parcial"))
    (parcial,) = _no_central(m, "dispensado_parcial")
    assert parcial["quantidade_confirmada"] == 3
    assert parcial["quantidade_pedida"] == 5
    assert parcial["os_id"] == "OS-1" and parcial["dispenser_id"] == 2
    assert not _no_central(m, "dispensado"), "parar não é dispensa concluída"

    # A retomada pede SÓ o que falta, e o slot ainda guarda o resto.
    m.placa_disp.dispensa_lenta = False
    m.chamar(m.modulo.cmd_dispensar(m.modulo.ComandoDispensarReq(
        dispenser_id=2, os_id="OS-1", quantidade=2)))
    assert _ate(lambda: _no_central(m, "dispensado"))
    assert m.placa_disp.executados[-1]["quantidade"] == 2
    assert _no_central(m, "dispensado")[0]["quantidade_dispensada"] == 2


def test_parar_sem_dispensa_em_curso_e_so_o_ack(montagem):
    m = montagem
    resposta = m.chamar(m.modulo.cmd_parar(m.modulo.ComandoPararReq()))
    assert resposta["ok"] is True
    time.sleep(0.2)
    assert not _no_central(m, "dispensado_parcial")
    assert m.placa_disp.executados[-1]["cmd"] == "parar"


def test_parar_repetido_com_o_mesmo_id_e_idempotente(montagem):
    """Reenvio do MESMO `cmd_id` é respondido sem executar — a regra de todo
    comando, e o `parar` não é exceção."""
    m = montagem
    _carregar(m)
    m.chamar(m.modulo.cmd_dispensar(m.modulo.ComandoDispensarReq(dispenser_id=2, os_id="OS-1")))
    m.placa_disp.avancar(1)
    link = m.modulo._link
    with link._lock_espera:
        link._proximo_id += 1
        cmd_id = link._proximo_id
        espera = m.modulo.serial_link._Espera()
        link._aguardando[cmd_id] = espera
    link._escrever({"cmd": "parar", "cmd_id": cmd_id, "sessao": m.modulo.serial_link.SESSAO})
    assert espera.evento.wait(2)
    espera2 = m.modulo.serial_link._Espera()
    with link._lock_espera:
        link._aguardando[cmd_id] = espera2
    link._escrever({"cmd": "parar", "cmd_id": cmd_id, "sessao": m.modulo.serial_link.SESSAO})
    assert espera2.evento.wait(2)
    assert espera2.resposta.get("repetido") is True

    assert _ate(lambda: _no_central(m, "dispensado_parcial"))
    time.sleep(0.2)
    assert len(_no_central(m, "dispensado_parcial")) == 1
    assert [e["cmd"] for e in m.placa_disp.executados].count("parar") == 1


def test_a_tela_do_slot_parado_mostra_pausado(montagem):
    m = montagem
    _carregar(m)
    m.chamar(m.modulo.cmd_dispensar(m.modulo.ComandoDispensarReq(dispenser_id=2, os_id="OS-1")))
    m.placa_disp.avancar(2)
    m.chamar(m.modulo.cmd_parar(m.modulo.ComandoPararReq()))
    assert _ate(lambda: m.placa_tft.telas.get(2, {}).get("status") == "pausado")


def test_dispensar_sem_quantidade_sai_como_sempre(carregar_adapter):
    import asyncio
    modulo = carregar_adapter("dispenser")
    enviados = []

    async def _fake(path, payload, timeout=None):
        enviados.append(payload)
        return {"ok": True}
    modulo._post_sim = _fake
    asyncio.run(modulo.cmd_dispensar(modulo.ComandoDispensarReq(dispenser_id=1, os_id="OS-1")))
    asyncio.run(modulo.cmd_dispensar(modulo.ComandoDispensarReq(
        dispenser_id=1, os_id="OS-1", quantidade=3)))
    assert "quantidade" not in enviados[0]
    assert enviados[1]["quantidade"] == 3


# ── O simulador (transporte http, o default) ─────────────────────────────────

def test_o_simulador_para_e_emite_a_parcial(carregar_simulador):
    import threading
    sim = carregar_simulador("dispenser", env={"T_CARGA_UNID": "0", "T_DISPENSA_UNID": "0.05",
                                               "PROB_ERRO_MECANICO": "0"})
    mod = sim.modulo
    mod._do_carregar(3, "DONAREN", "APSEN-002", "x", 10, "OS-1")
    with mod._lock:
        mod._estado[3]["status"] = "dispensando"
    t = threading.Thread(target=mod._do_dispensar, args=(3, "OS-1"))
    t.start()
    time.sleep(0.18)
    mod.executar_parar(mod.PararReq())
    t.join(3)
    (parcial,) = sim.eventos_do_tipo("dispensado_parcial")
    assert 1 <= parcial["quantidade_confirmada"] < 10
    assert parcial["quantidade_pedida"] == 10
    assert not sim.eventos_do_tipo("dispensado")
    assert mod._estado[3]["status"] == "pronto"
    assert mod._estado[3]["qtd_alvo"] == 10 - parcial["quantidade_confirmada"]
