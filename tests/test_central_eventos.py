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


# ── Contador de alarmes ativos ────────────────────────────────────────────────
#
# `_estado["alarmes_ativos"]` já foi um contador incrementado à mão nos
# handlers: só subia. Resolver um alarme pela IHM não o baixava e um restart o
# zerava com o banco cheio de alarmes abertos — o badge do dashboard crescia
# para sempre sem relação com a realidade. Hoje o número é sempre uma leitura
# de `get_total_alarmes_ativos()`, e é isso que estes testes prendem.


class ContadorDeAlarmes:
    """Duplo de `get_total_alarmes_ativos`: a única fonte do badge.

    `abertos` é o que a tabela `alarmes` responderia. O teste mexe nele para
    encenar alarme criado ou resolvido por QUALQUER produtor — inclusive o
    orquestrador, que grava alarme de trava e de abort sem passar pelo main.
    """

    def __init__(self, abertos: int):
        self.abertos = abertos
        self.leituras = 0

    def __call__(self) -> int:
        self.leituras += 1
        return self.abertos


def _contador(central, monkeypatch, abertos: int) -> ContadorDeAlarmes:
    contador = ContadorDeAlarmes(abertos)
    monkeypatch.setattr(central.modulo, "get_total_alarmes_ativos", contador)
    return contador


def _evento_com_alarme(central) -> None:
    """Erro de dispenser — o caminho mais curto que grava um alarme."""
    _evento(central, {
        "tipo":         "erro",
        "dispenser_id": 1,
        "codigo_erro":  "motor_travado",
        "descricao":    "Motor do dispenser 1 travado",
    })


def test_alarme_novo_le_o_total_do_banco(carregar_central, monkeypatch):
    """O badge sai do COUNT, não de um incremento: 0 → 4 em um evento só."""
    central = carregar_central()
    contador = _contador(central, monkeypatch, abertos=4)

    _evento_com_alarme(central)

    assert central.banco.chamadas_de("salvar_alarme")
    assert contador.leituras == 1
    assert central.modulo._estado["alarmes_ativos"] == 4


def test_evento_sem_alarme_nao_consulta_o_banco(carregar_central, monkeypatch):
    """O valor entra em todo broadcast: telemetria não pode virar query."""
    central = carregar_central()
    contador = _contador(central, monkeypatch, abertos=4)

    _evento_com_alarme(central)          # força a leitura e aquece o cache
    for slot in (2, 3, 4):
        _evento(central, {"tipo": "status", "dispenser_id": slot, "quantidade": 5})

    assert contador.leituras == 1
    assert central.modulo._estado["alarmes_ativos"] == 4


def test_resolver_alarme_abaixa_o_contador(carregar_central, monkeypatch):
    """Resolver é o único caminho que DIMINUI — e não espera o TTL do cache."""
    central = carregar_central()
    contador = _contador(central, monkeypatch, abertos=4)
    _evento_com_alarme(central)
    assert central.modulo._estado["alarmes_ativos"] == 4

    contador.abertos = 3                 # o UPDATE resolvido=1 já entrou
    resposta = central.modulo.manut_resolver_alarme(7, {"sub": "tecnico1"})

    assert central.banco.chamadas_de("resolver_alarme")
    assert resposta["alarmes_ativos"] == 3
    assert central.modulo._estado["alarmes_ativos"] == 3


def test_reinicio_carrega_o_contador_do_banco(carregar_central, monkeypatch):
    """Processo novo nasce com a memória zerada; o banco, não."""
    central = carregar_central()
    contador = _contador(central, monkeypatch, abertos=5)
    assert central.modulo._estado["alarmes_ativos"] == 0   # antes da lifespan

    async def _subir_e_descer():
        async with central.modulo.lifespan(central.modulo.app):
            # Startup concluído: é aqui que o dashboard faz o primeiro GET.
            assert central.modulo._estado["alarmes_ativos"] == 5

    asyncio.run(_subir_e_descer())
    assert contador.leituras == 1


# ── Volume de escrita e de broadcast ──────────────────────────────────────────
#
# O cnc_simulator emite "movendo" a cada 0.5s durante TODO o movimento: eram
# centenas de linhas em `cnc_eventos` por OS para descrever uma trajetória que
# o dashboard já mostra ao vivo e que ninguém consulta depois. Cada evento
# ainda disparava `copy.deepcopy` do estado inteiro + JSON + envio a todos os
# clientes. Persistir só transição e segurar o broadcast dos periódicos é o que
# estes testes prendem.

def _evento_cnc(central, tipo: str, **extra) -> None:
    asyncio.run(central.modulo._handle_evento_cnc({
        "tipo": tipo, "os_id": "OS-42", "dispenser_alvo": 3,
        "posicao_x": 120.0, "posicao_y": 0.0, **extra,
    }))


def test_movendo_nao_vira_linha_no_banco(carregar_central):
    central = carregar_central()

    for _ in range(20):
        _evento_cnc(central, "movendo")

    assert central.banco.chamadas_de("salvar_cnc_evento") == []
    # ...mas o dashboard continua vendo a CNC andar.
    assert central.modulo._estado["cnc"]["status"] == "movendo"
    assert central.modulo._estado["cnc"]["posicao_x"] == 120.0


@pytest.mark.parametrize("tipo", ["posicionado", "concluido", "erro"])
def test_transicoes_de_cnc_continuam_sendo_gravadas(carregar_central, tipo):
    central = carregar_central()

    _evento_cnc(central, tipo)

    (chamada,) = central.banco.chamadas_de("salvar_cnc_evento")
    assert chamada["args"][1] == tipo


def test_amostragem_de_trajetoria_grava_1_em_n(carregar_central, monkeypatch):
    """Rastro de trajetória é opcional e sai caro — por isso vem desligado."""
    central = carregar_central()
    monkeypatch.setattr(central.modulo.settings, "CNC_AMOSTRAGEM_MOVENDO", 5)

    for _ in range(20):
        _evento_cnc(central, "movendo")

    assert len(central.banco.chamadas_de("salvar_cnc_evento")) == 4


# ── Throttle do broadcast ─────────────────────────────────────────────────────

def _contar_broadcasts(central, monkeypatch) -> list:
    """Intercepta o envio no ponto em que o payload já foi montado."""
    enviados = []
    monkeypatch.setattr(central.modulo, "_enfileirar_broadcast", enviados.append)
    return enviados


def test_rajada_de_movendo_gera_no_maximo_um_broadcast(carregar_central, monkeypatch):
    """30 eventos (15s de movimento real) em rajada: um envio, não trinta."""
    central = carregar_central()
    enviados = _contar_broadcasts(central, monkeypatch)

    for _ in range(30):
        _evento_cnc(central, "movendo")

    assert len(enviados) == 1


def test_telemetria_de_dispenser_tambem_e_segurada(carregar_central, monkeypatch):
    """12 eventos periódicos a cada 15s — o outro emissor de alta frequência."""
    central = carregar_central()
    enviados = _contar_broadcasts(central, monkeypatch)

    for slot in range(1, 7):
        _evento(central, {"tipo": "status", "dispenser_id": slot, "quantidade": 5})
        _evento(central, {"tipo": "telemetria", "dispenser_id": slot, "valor_c": 30.0})

    assert len(enviados) == 1


@pytest.mark.parametrize("evento", [
    {"tipo": "carregado",  "dispenser_id": 1, "os_id": "OS-42", "quantidade_total": 10},
    {"tipo": "erro",       "dispenser_id": 1, "descricao": "Motor travado"},
    {"tipo": "limpeza_ok", "dispenser_id": 1},
])
def test_transicao_importante_ignora_o_throttle(carregar_central, monkeypatch, evento):
    """Alarme e fim de etapa não podem esperar meio segundo na fila."""
    central = carregar_central()
    _evento_cnc(central, "movendo")          # consome a janela do throttle
    enviados = _contar_broadcasts(central, monkeypatch)

    _evento(central, evento)

    assert len(enviados) == 1


def test_estado_segurado_pelo_throttle_e_enviado_pelo_flusher(carregar_central,
                                                              monkeypatch):
    """O que o throttle segura não pode se perder: a CNC pararia na tela."""
    central = carregar_central()
    monkeypatch.setattr(central.modulo, "_BROADCAST_MIN_INTERVALO_S", 0.05)
    enviados = _contar_broadcasts(central, monkeypatch)

    _evento_cnc(central, "movendo")                       # 1º: passa
    _evento_cnc(central, "movendo", posicao_x=240.0)      # 2º: segurado
    assert len(enviados) == 1
    assert central.modulo._broadcast_pendente is True

    async def _um_tique():
        tarefa = asyncio.create_task(central.modulo._broadcast_flusher())
        await asyncio.sleep(0.15)
        tarefa.cancel()

    asyncio.run(_um_tique())

    assert len(enviados) == 2
    assert enviados[-1]["cnc"]["posicao_x"] == 240.0      # a posição mais recente
