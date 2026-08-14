"""
Testes do handler de eventos de dispenser do computador central.

O contrato em teste é a divisão de fontes de verdade:

  - o simulador manda no HARDWARE/ESTOQUE (medicamento, sku, categoria,
    quantidade) e o central aceita esses campos de qualquer evento;
  - o orquestrador manda no FLUXO (status da etapa, os_id em execução) e a
    telemetria periódica — o evento "status", emitido a cada 15s para os 6
    slots — não pode encostar nele.

Quando a telemetria invadia o fluxo, o reset de fim de OS era desfeito no
ciclo seguinte: o slot ficava eternamente "concluido" no dashboard, a limpeza
respondia 409 e `dispenser_estado.ultima_os_id` congelava numa OS antiga.
"""
import asyncio

import pytest
from fastapi import HTTPException


def _evento(central, payload: dict) -> None:
    """Entrega um evento ao central como o dispenser-adapter faria."""
    asyncio.run(central.modulo._handle_evento_dispenser(payload))


# ── Telemetria periódica (tipo "status") ───────────────────────────────────────

def test_telemetria_nao_ressuscita_fluxo_de_os_encerrada(carregar_central):
    """Slot já resetado pelo orquestrador não volta a "concluido" na telemetria."""
    central = carregar_central()
    central.slot(3).update({"status": "idle", "os_id": None})

    _evento(central, {
        "tipo":         "status",
        "dispenser_id": 3,
        "status":       "concluido",     # sobra do snapshot do simulador
        "os_id":        "OS-ANTIGA",     # idem
        "medicamento":  "Dipirona",
        "sku":          "DIP-500",
        "categoria":    "analgesico",
        "quantidade":   12,
    })

    slot = central.slot(3)
    # Fluxo: intocado.
    assert slot["status"] == "idle"
    assert slot["os_id"] is None
    # Estoque: veio do payload.
    assert slot["quantidade"] == 12
    assert slot["medicamento"] == "Dipirona"
    assert slot["sku"] == "DIP-500"
    assert slot["categoria"] == "analgesico"


def test_telemetria_nao_grava_os_antiga_em_ultima_os_id(carregar_central):
    """`ultima_os_id` sai do estado em memória do central, não do payload."""
    central = carregar_central()
    central.slot(3).update({"status": "idle", "os_id": None})

    _evento(central, {
        "tipo":         "status",
        "dispenser_id": 3,
        "status":       "concluido",
        "os_id":        "OS-ANTIGA",
        "medicamento":  "Dipirona",
        "categoria":    "analgesico",
        "quantidade":   12,
    })

    (chamada,) = central.banco.chamadas_de("salvar_dispenser_estado")
    disp_id, quantidade, os_id, medicamento, categoria = chamada["args"]
    assert (disp_id, quantidade) == (3, 12)
    assert os_id is None
    assert (medicamento, categoria) == ("Dipirona", "analgesico")


def test_telemetria_preserva_os_em_execucao(carregar_central):
    """Com OS em andamento, a telemetria mantém o os_id que o central atribuiu."""
    central = carregar_central()
    central.slot(2).update({"status": "aguardando_carga", "os_id": "OS-ATUAL"})

    _evento(central, {
        "tipo":         "status",
        "dispenser_id": 2,
        "status":       "idle",          # o simulador ainda nem começou a carga
        "os_id":        None,
        "medicamento":  "Amoxicilina",
        "quantidade":   5,
    })

    assert central.slot(2)["status"] == "aguardando_carga"
    assert central.slot(2)["os_id"] == "OS-ATUAL"
    (chamada,) = central.banco.chamadas_de("salvar_dispenser_estado")
    assert chamada["args"][2] == "OS-ATUAL"


def test_telemetria_zera_estoque_quando_slot_esvazia(carregar_central):
    """Quantidade zero limpa medicamento/categoria na linha do banco."""
    central = carregar_central()
    central.slot(1).update({"medicamento": "Dipirona", "quantidade": 8})

    _evento(central, {
        "tipo":         "status",
        "dispenser_id": 1,
        "medicamento":  None,
        "categoria":    None,
        "quantidade":   0,
    })

    assert central.slot(1)["quantidade"] == 0
    (chamada,) = central.banco.chamadas_de("salvar_dispenser_estado")
    assert chamada["args"][1] == 0
    assert chamada["args"][3] is None      # medicamento
    assert chamada["args"][4] is None      # categoria


# ── Eventos de transição de fluxo ──────────────────────────────────────────────

def test_dispensado_move_o_fluxo_e_o_estoque(carregar_central):
    """O evento de transição, esse sim, muda status e quantidades."""
    central = carregar_central()
    central.slot(4).update({
        "status":      "dispensando",
        "os_id":       "OS-42",
        "medicamento": "Dipirona",
        "categoria":   "analgesico",
        "quantidade":  10,
    })

    _evento(central, {
        "tipo":                  "dispensado",
        "dispenser_id":          4,
        "os_id":                 "OS-42",
        "quantidade_dispensada": 10,
        "quantidade_alvo":       10,
        "quantidade_residual":   0,
        "falha_mecanica":        False,
    })

    slot = central.slot(4)
    assert slot["status"] == "concluido"
    assert slot["quantidade_dispensada"] == 10
    assert slot["quantidade_residual"] == 0
    assert slot["quantidade"] == 0
    # Residual zerado: o slot esvaziou.
    assert slot["medicamento"] is None
    assert slot["categoria"] is None

    (chamada,) = central.banco.chamadas_de("salvar_dispenser_estado")
    assert chamada["args"][2] == "OS-42"
    assert central.banco.chamadas_de("salvar_dispensa")
    assert not central.banco.chamadas_de("salvar_alarme")


def test_dispensa_parcial_mantem_residual_e_abre_alarme(carregar_central):
    central = carregar_central()
    central.slot(5).update({
        "status":      "dispensando",
        "os_id":       "OS-42",
        "medicamento": "Amoxicilina",
        "quantidade":  10,
    })

    _evento(central, {
        "tipo":                  "dispensado",
        "dispenser_id":          5,
        "os_id":                 "OS-42",
        "quantidade_dispensada": 7,
        "quantidade_alvo":       10,
        "quantidade_residual":   3,
        "falha_mecanica":        True,
        "motivo_falha":          "falha_mecanica",
    })

    slot = central.slot(5)
    assert slot["status"] == "dispensando"
    assert slot["quantidade"] == 3
    assert slot["medicamento"] == "Amoxicilina"
    assert central.banco.chamadas_de("salvar_alarme")


def test_limpeza_ok_solta_o_slot(carregar_central):
    central = carregar_central()
    central.slot(6).update({
        "status":      "concluido",
        "os_id":       "OS-42",
        "medicamento": "Dipirona",
        "quantidade":  3,
    })

    _evento(central, {"tipo": "limpeza_ok", "dispenser_id": 6})

    slot = central.slot(6)
    assert slot["status"] == "limpo"
    assert slot["os_id"] is None
    assert slot["quantidade"] == 0
    assert slot["medicamento"] is None
    assert central.banco.chamadas_de("limpar_dispenser_estado")


# ── Limpeza manual pela IHM ────────────────────────────────────────────────────

def _permitir_comando_limpar(central, monkeypatch):
    """Neutraliza o HTTP para o dispenser-adapter, aceitando o comando."""
    async def _cmd_limpar(dispenser_id, solicitado_por):
        return True

    monkeypatch.setattr(central.modulo.orch, "cmd_limpar", _cmd_limpar)


def test_limpeza_permitida_em_slot_concluido(carregar_central, monkeypatch):
    """Slot que terminou a dispensa pode ser limpo — é o caso de uso do botão."""
    central = carregar_central()
    _permitir_comando_limpar(central, monkeypatch)
    central.modulo._estado["os_ativa"] = None
    central.slot(2).update({"status": "concluido", "medicamento": "Dipirona"})

    resposta = asyncio.run(
        central.modulo.manut_limpar_dispenser(2, {"sub": "tecnico1"})
    )

    assert resposta["ok"] is True
    assert central.banco.chamadas_de("salvar_manutencao")


@pytest.mark.parametrize("status_slot", ["carregando", "dispensando"])
def test_limpeza_bloqueada_com_operacao_em_curso(carregar_central, monkeypatch,
                                                 status_slot):
    central = carregar_central()
    _permitir_comando_limpar(central, monkeypatch)
    central.modulo._estado["os_ativa"] = None
    central.slot(2).update({"status": status_slot})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(central.modulo.manut_limpar_dispenser(2, {"sub": "tecnico1"}))

    assert exc.value.status_code == 409
    assert status_slot in exc.value.detail


def test_limpeza_bloqueada_com_os_ativa(carregar_central, monkeypatch):
    """Bloqueio legítimo: nenhum slot se mexe enquanto uma OS roda."""
    central = carregar_central()
    _permitir_comando_limpar(central, monkeypatch)
    central.modulo._estado["os_ativa"] = {"os_id": "OS-42"}
    central.slot(2).update({"status": "concluido"})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(central.modulo.manut_limpar_dispenser(2, {"sub": "tecnico1"}))

    assert exc.value.status_code == 409
    assert "OS-42" in exc.value.detail
