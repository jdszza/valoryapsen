# -*- coding: utf-8 -*-
"""O dispenser-adapter e a porta NR-12 — mais uma origem, nenhuma decisão.

A porta viaja dentro da porta das telas (§9). Para o adapter ela é:

  1. uma ORIGEM de evento: tudo que chega com `"origem":"porta"` sobe CRU ao
     central, pela rota irmã `/api/v1/eventos/porta`, na ordem em que veio;
  2. três comandos REST que viram `porta_*` na linha serial das telas, com o
     ACK que a PORTA der (o mesmo 502/503 de todo comando);
  3. um LINK — a USB das telas, a vigia da placa das telas e a idade do
     heartbeat somados num estado só, e só a TRANSIÇÃO sobe:
     `porta_link_perdido` uma vez, `porta_link_ok` só depois do primeiro
     `estado`.

Montado com as placas falsas de verdade, por `socket://` (a das telas com a
porta falsa atrás dela), e o cliente HTTP duplado no lugar do central.
"""
import asyncio

import pytest

from fakes.placa_porta import PlacaPorta
from test_dispenser_tft import adapter_com_telas  # noqa: F401 — fixture
from test_serial_link import _ate

pytest.importorskip("serial", reason="pyserial (tests/requirements-dev.txt)")

UID = "04A1B2C3"


@pytest.fixture
def com_porta(adapter_com_telas):  # noqa: F811
    montagem = adapter_com_telas(mecanismos_serial=False)
    porta = PlacaPorta(tags=(UID,))
    montagem.placa_tft.conectar_porta(porta, heartbeat_s=0.05, vigia_s=0.3)
    montagem.porta = porta
    return montagem


def _da_porta(m) -> list:
    return [c["json"] for c in m.cliente.posts
            if c["url"].endswith("/api/v1/eventos/porta")]


def _tipos(m) -> list:
    return [e["tipo"] for e in _da_porta(m)]


# ── 1. Evento sobe, cru, pela rota irmã ──────────────────────────────────────

def test_o_primeiro_estado_sobe_depois_do_link_ok(com_porta):
    assert _ate(lambda: "estado" in _tipos(com_porta))
    tipos = _tipos(com_porta)
    assert tipos.index("porta_link_ok") < tipos.index("estado")


def test_evento_da_porta_sobe_cru_na_rota_da_porta(com_porta):
    assert _ate(lambda: "estado" in _tipos(com_porta))
    com_porta.placa_tft.da_porta(com_porta.porta.passar_tag(UID))
    assert _ate(lambda: "pedido_acesso" in _tipos(com_porta))
    pedido = next(e for e in _da_porta(com_porta) if e["tipo"] == "pedido_acesso")
    assert pedido["uid"] == UID and pedido["origem"] == "porta"
    # E nunca pela rota dos dispensers: o central leria um evento sem slot.
    assert not [c for c in com_porta.cliente.posts
                if c["url"].endswith("/api/v1/eventos/dispenser")]


def test_heartbeat_repetido_nao_sobe_a_cada_500ms(com_porta):
    """Vinte heartbeats idênticos são UM `estado` no central — o resto é
    transporte, e o central não decide nada com a repetição."""
    assert _ate(lambda: "estado" in _tipos(com_porta))
    import time
    time.sleep(0.6)            # ~12 heartbeats a 50 ms
    assert _tipos(com_porta).count("estado") == 1


def test_estado_que_muda_sobe_na_hora(com_porta):
    assert _ate(lambda: "estado" in _tipos(com_porta))
    com_porta.placa_tft.da_porta(com_porta.porta.abrir("frente"))
    assert _ate(lambda: "porta_aberta" in _tipos(com_porta))
    assert _ate(lambda: _tipos(com_porta).count("estado") >= 2)


# ── 2. Comando desce com o ACK da porta ──────────────────────────────────────

def test_liberar_desce_e_volta_com_o_ack_da_porta(com_porta):
    m = com_porta
    m.placa_tft.da_porta(m.porta.passar_tag(UID))
    resposta = m.chamar(m.modulo.cmd_porta_liberar(m.modulo.PortaLiberarReq(janela_ms=5000)))
    assert resposta["ok"] is True
    assert m.porta.executados[-1] == {**m.porta.executados[-1],
                                      "cmd": "porta_liberar", "janela_ms": 5000}
    assert _ate(lambda: "liberada" in _tipos(m))


def test_liberar_sem_pedido_e_502(com_porta):
    from fastapi import HTTPException
    m = com_porta
    with pytest.raises(HTTPException) as erro:
        m.chamar(m.modulo.cmd_porta_liberar(m.modulo.PortaLiberarReq(janela_ms=5000)))
    assert erro.value.status_code == 502


def test_negar_e_estado_descem(com_porta):
    m = com_porta
    m.placa_tft.da_porta(m.porta.passar_tag(UID))
    m.chamar(m.modulo.cmd_porta_negar(m.modulo.PortaNegarReq(motivo="maquina_nao_confirmou_parada")))
    m.chamar(m.modulo.cmd_porta_estado())
    assert [e["cmd"] for e in m.porta.executados][-2:] == ["porta_negar", "porta_estado"]
    assert _ate(lambda: "liberacao_expirada" in _tipos(m))


def test_janela_fora_da_faixa_e_422_na_borda(com_porta):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        com_porta.modulo.PortaLiberarReq(janela_ms=10)


def test_sem_telas_por_serial_nao_existe_porta(carregar_adapter):
    """O simulador não tem porta: fingir um ACK faria o central destravar uma
    porta imaginária."""
    from fastapi import HTTPException
    modulo = carregar_adapter("dispenser")
    with pytest.raises(HTTPException) as erro:
        asyncio.run(modulo.cmd_porta_estado())
    assert erro.value.status_code == 503


# ── 3. O link: uma queda, uma linha; volta só com `estado` ───────────────────

class _LinkCaido:
    conectado = False


def test_usb_das_telas_cai_vira_link_perdido_e_volta_so_com_estado(com_porta, monkeypatch):
    m = com_porta
    assert _ate(lambda: "estado" in _tipos(m))
    real = m.modulo._link_tft

    monkeypatch.setattr(m.modulo, "_link_tft", _LinkCaido())
    m.chamar(m.modulo._vigiar_porta_uma_vez())
    m.chamar(m.modulo._vigiar_porta_uma_vez())        # a segunda rodada não repete
    assert _tipos(m).count("porta_link_perdido") == 1

    antes_pedidos = len([e for e in m.porta.executados if e["cmd"] == "porta_estado"])
    monkeypatch.setattr(m.modulo, "_link_tft", real)
    m.chamar(m.modulo._vigiar_porta_uma_vez())
    # Ao reabrir, o adapter PEDE um `estado` em vez de esperar o heartbeat.
    assert len([e for e in m.porta.executados if e["cmd"] == "porta_estado"]) == antes_pedidos + 1
    assert _ate(lambda: _tipos(m).count("porta_link_ok") == 2)
    tipos = _tipos(m)
    ultimo_ok = len(tipos) - 1 - tipos[::-1].index("porta_link_ok")
    assert "estado" in tipos[ultimo_ok:], "o link voltou sem um `estado` depois"


def test_heartbeat_que_some_vira_UM_link_perdido(com_porta):
    """A placa das telas avisa E a vigia do adapter percebe — e o central
    recebe uma linha, não duas."""
    m = com_porta
    assert _ate(lambda: "estado" in _tipos(m))
    m.modulo.PORTA_HEARTBEAT_MAX_S = 0.2
    m.porta.silenciar()
    assert _ate(lambda: "porta_link_perdido" in _tipos(m))
    m.chamar(m.modulo._vigiar_porta_uma_vez())
    import time
    time.sleep(0.4)
    m.chamar(m.modulo._vigiar_porta_uma_vez())
    assert _tipos(m).count("porta_link_perdido") == 1

    m.porta.falar()
    assert _ate(lambda: _tipos(m).count("porta_link_ok") == 2)


def test_health_publica_a_porta(com_porta):
    m = com_porta
    assert _ate(lambda: "estado" in _tipos(m))
    corpo = m.chamar(m.modulo.health())
    porta = corpo["porta"]
    assert porta["link_ok"] is True
    assert porta["idade_heartbeat_s"] is not None and porta["idade_heartbeat_s"] < 2
    assert porta["ultimo_estado"]["estado"] == "FECHADA"
