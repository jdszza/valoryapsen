"""
Testes do handler de eventos de dispenser do computador central.

O contrato em teste é a divisão de fontes de verdade:

  - o simulador manda no HARDWARE/ESTOQUE (medicamento, sku, categoria,
    quantidade) e o central aceita esses campos de qualquer evento;
  - o orquestrador manda no FLUXO (status da etapa, os_id em execução) e a
    telemetria periódica — o evento "status", emitido a cada 15s para todos os
    slots — não pode encostar nele.

Quando a telemetria invadia o fluxo, o reset de fim de OS era desfeito no
ciclo seguinte: o slot ficava eternamente "concluido" no dashboard, a limpeza
respondia 409 e `dispenser_estado.ultima_os_id` congelava numa OS antiga.
"""
import ast
import asyncio
import logging

import pytest
from fastapi import HTTPException

from conftest import CENTRAL_DIR, NUM_SLOTS


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


# ── O nome do medicamento sobrevive ao slot que esvaziou ──────────────────────
#
# A sequência abaixo é a NORMAL, não uma borda: o firmware conta a última
# unidade, esvazia `sl.medicamento`, emite `status` com medicamento nulo e só
# então emite `dispensado`. O central lia o nome do seu próprio snapshot — que
# o periódico acabara de apagar — e chamava `salvar_dispensa(..., None, ...)`.
# `dispensas.medicamento` é VARCHAR(100) NOT NULL: o INSERT falhava com 1048,
# `_db` logava um `warning`, e a dispensa que deu CERTO era exatamente a que
# não virava linha. Some do relatório da OS, do CSV e do XLSX, sem erro.

def _dispensa_completa(disp_id: int) -> dict:
    return {
        "tipo":                  "dispensado",
        "dispenser_id":          disp_id,
        "os_id":                 "OS-1",
        "quantidade_dispensada": 5,
        "quantidade_alvo":       5,
        "quantidade_residual":   0,
        "falha_mecanica":        False,
    }


def test_status_nulo_nao_apaga_o_que_o_slot_ja_sabe(carregar_central):
    """`null` num periódico é "não informado", não "não tem"."""
    central = carregar_central()
    central.slot(1).update({
        "medicamento": "Dipirona", "sku": "DIP-500", "categoria": "analgesico",
        "quantidade": 5,
    })

    _evento(central, {
        "tipo": "status", "dispenser_id": 1,
        "medicamento": None, "sku": None, "categoria": None, "quantidade": 0,
    })

    slot = central.slot(1)
    assert slot["quantidade"] == 0          # o estoque, esse o periódico manda
    assert slot["medicamento"] == "Dipirona"
    assert slot["sku"] == "DIP-500"
    assert slot["categoria"] == "analgesico"


def test_dispensa_grava_o_medicamento_mesmo_depois_do_status_vazio(carregar_central):
    """carregado → status(medicamento=None) → dispensado: a linha sai com nome."""
    central = carregar_central()

    _evento(central, {
        "tipo": "carregado", "dispenser_id": 1, "os_id": "OS-1",
        "medicamento": "Dipirona", "sku": "DIP-500", "categoria": "analgesico",
        "quantidade_total": 5,
    })
    # O slot esvaziou e a telemetria já saiu: é ela que chega primeiro.
    _evento(central, {
        "tipo": "status", "dispenser_id": 1,
        "medicamento": None, "sku": None, "categoria": None, "quantidade": 0,
    })
    _evento(central, _dispensa_completa(1))

    (chamada,) = central.banco.chamadas_de("salvar_dispensa")
    os_id, disp_id, medicamento, qtd, alvo, validado, falha = chamada["args"]
    assert (os_id, disp_id) == ("OS-1", 1)
    assert medicamento == "Dipirona", (
        "a dispensa foi gravada sem medicamento — a coluna é NOT NULL e o "
        "INSERT falha em silêncio")
    assert validado is True


def test_o_proprio_dispensado_serve_de_fonte_do_nome(carregar_central):
    """Central sem o nome em memória (restart no meio da OS): vale o do evento."""
    central = carregar_central()
    central.slot(2).update({"status": "dispensando", "os_id": "OS-1"})

    _evento(central, {**_dispensa_completa(2), "medicamento": "Amoxicilina"})

    (chamada,) = central.banco.chamadas_de("salvar_dispensa")
    assert chamada["args"][2] == "Amoxicilina"


def test_sem_nome_em_lugar_nenhum_a_linha_ainda_sai(carregar_central):
    """Entre gravar "(desconhecido)" e não gravar nada, um sistema de
    medicação grava: o que aconteceu com o paciente não pode depender de o
    nome ter chegado."""
    central = carregar_central()
    central.slot(3).update({"status": "dispensando", "os_id": "OS-1"})

    _evento(central, _dispensa_completa(3))

    (chamada,) = central.banco.chamadas_de("salvar_dispensa")
    medicamento = chamada["args"][2]
    assert medicamento, "coluna NOT NULL: string vazia também derruba o INSERT"
    assert medicamento == "(desconhecido)"


# ── Limpeza manual pelo app de manutenção ─────────────────────────────────────

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
# handlers: só subia. Resolver um alarme pelo app de manutenção não o baixava e
# um restart o
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


# ── Telemetria da CNC: leitura de sensor, não posição ─────────────────────────
#
# O `_estado["cnc"].update(...)` rodava para TODO tipo de evento, e a
# telemetria não traz posição, alvo nem ciclo: os defaults do `payload.get`
# entravam no snapshot como se fossem medida. Entre dois eventos de movimento a
# mesa "voltava" para (0,0) com `dispenser_alvo` nulo — e o painel é o único
# lugar onde se vê a mesa nesse intervalo. Pior: `prevoo.itens_celula` compara
# a posição PUBLICADA com o HOME, então a tela de conferência passava a
# concordar com uma origem que ninguém mediu. O handler do dispenser já
# separava a periódica do fluxo; este não separava.

def _telemetria_cnc(central, **extra) -> None:
    asyncio.run(central.modulo._handle_evento_cnc({
        "tipo": "telemetria", "componente": "motor_x",
        "valor": 48.2, "unidade": "°C", **extra,
    }))


def test_telemetria_de_cnc_nao_apaga_a_posicao_nem_o_alvo(carregar_central):
    central = carregar_central()
    _evento_cnc(central, "posicionado")
    antes = dict(central.modulo._estado["cnc"])

    _telemetria_cnc(central)

    assert central.modulo._estado["cnc"] == antes


def test_telemetria_de_cnc_continua_virando_leitura_de_sensor(carregar_central):
    """Controle: o que ela SEMPRE fez não pode ter sumido com a separação."""
    central = carregar_central()

    _telemetria_cnc(central)

    (chamada,) = central.banco.chamadas_de("salvar_leitura_sensor")
    assert chamada["args"] == ("motor_x", "temperatura", 48.2, "°C")
    assert central.banco.chamadas_de("salvar_cnc_evento") == []


def test_a_linha_de_log_da_telemetria_nao_inventa_posicao(carregar_central):
    """Mesma mentira, outra superfície: `/log/eventos` é o que a bancada lê."""
    central = carregar_central()

    _telemetria_cnc(central)

    (linha,) = [e for e in central.modulo._log_eventos
                if e["tipo"].startswith("cnc_")]
    assert "0.0" not in linha["msg"], linha["msg"]
    assert "motor_x" in linha["msg"]


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

    for slot in range(1, NUM_SLOTS + 1):
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


# ── Visão: as três câmeras ────────────────────────────────────────────────────
#
# A célula tem uma câmera por fileira de dispensers e uma sobre a mesa
# (a da balança). O TIPO do evento é o mesmo nas duas câmeras de dispenser —
# quem separa esquerda de direita é o campo `camera`.
#
# Duas coisas precisam ser verdade ao mesmo tempo, e é fácil quebrar uma
# consertando a outra: cada leitura tem que pousar na chave da SUA câmera (sem
# apagar a última leitura das outras duas, que é o que faria "a câmera da
# direita parou" sumir do painel), e o orquestrador tem que continuar sendo
# avisado pela chave do SLOT — ele espera por um slot, não por uma lente.

SLOT_ESQ = 1
SLOT_DIR = NUM_SLOTS
CHAVES_VISAO = ("camera_dispenser_esq", "camera_dispenser_dir", "camera_mesa")


def _evento_visao(central, payload: dict) -> None:
    asyncio.run(central.modulo._handle_evento_visao(payload))


def _notificacoes(central, monkeypatch) -> list:
    """Grava as chaves com que o orquestrador foi acordado."""
    registradas = []
    monkeypatch.setattr(central.modulo.orch, "notificar_evento",
                        lambda chave, dados: registradas.append((chave, dados)))
    return registradas


def _leitura_dispenser(slot_id: int, camera: str, tipo: str, **extra) -> dict:
    payload = {
        "tipo":         tipo,
        "camera":       camera,
        "slot_id":      slot_id,
        "os_id":        "OS-CAM",
        "sku_esperado": "SKU-1",
        "confianca":    0.97,
        "ts":           "2026-01-01T00:00:00Z",
    }
    payload.update(extra)
    return payload


@pytest.mark.parametrize("camera,slot_id,chave", [
    ("dispenser_esq", SLOT_ESQ, "camera_dispenser_esq"),
    ("dispenser_dir", SLOT_DIR, "camera_dispenser_dir"),
])
def test_leitura_de_dispenser_pousa_na_camera_que_leu(carregar_central, camera,
                                                      slot_id, chave):
    central = carregar_central()

    _evento_visao(central, _leitura_dispenser(
        slot_id, camera, "leitura_dispenser_ok", sku_lido="SKU-1", match_sku=True))

    visao = central.modulo._estado["visao"]
    assert visao[chave]["ultima_leitura"] == "leitura_dispenser_ok"
    assert visao[chave]["slot_id"] == slot_id
    assert visao[chave]["match_sku"] is True
    # As outras duas seguem intocadas.
    for outra in set(CHAVES_VISAO) - {chave}:
        assert visao[outra]["ultima_leitura"] is None, (
            f"{camera} contaminou {outra}"
        )


def test_leitura_de_mesa_pousa_na_camera_da_balanca(carregar_central):
    central = carregar_central()

    _evento_visao(central, {
        "tipo":                 "leitura_mesa_ok",
        "camera":               "mesa",
        "slot_id":              3,
        "os_id":                "OS-CAM",
        "quantidade_esperada":  10,
        "quantidade_detectada": 10,
        "confianca":            0.95,
        "ts":                   "2026-01-01T00:00:00Z",
    })

    visao = central.modulo._estado["visao"]
    assert visao["camera_mesa"]["ultima_leitura"] == "leitura_mesa_ok"
    assert visao["camera_mesa"]["quantidade_detectada"] == 10
    assert visao["camera_dispenser_esq"]["ultima_leitura"] is None
    assert visao["camera_dispenser_dir"]["ultima_leitura"] is None


def test_as_duas_cameras_de_dispenser_convivem(carregar_central):
    """A leitura de um lado não apaga a do outro — as duas ficam no painel."""
    central = carregar_central()

    _evento_visao(central, _leitura_dispenser(
        SLOT_ESQ, "dispenser_esq", "leitura_dispenser_ok",
        sku_lido="SKU-1", match_sku=True))
    _evento_visao(central, _leitura_dispenser(
        SLOT_DIR, "dispenser_dir", "leitura_dispenser_falha",
        motivo="camera_obstruida"))

    visao = central.modulo._estado["visao"]
    assert visao["camera_dispenser_esq"]["ultima_leitura"] == "leitura_dispenser_ok"
    assert visao["camera_dispenser_esq"]["slot_id"] == SLOT_ESQ
    assert visao["camera_dispenser_dir"]["ultima_leitura"] == "leitura_dispenser_falha"
    assert visao["camera_dispenser_dir"]["slot_id"] == SLOT_DIR


@pytest.mark.parametrize("camera,slot_id", [
    ("dispenser_esq", SLOT_ESQ),
    ("dispenser_dir", SLOT_DIR),
])
def test_divergencia_de_sku_avisa_o_orquestrador_pelos_dois_lados(
        carregar_central, monkeypatch, camera, slot_id):
    """A chave de espera é do SLOT: trocar de câmera não pode mudá-la."""
    central = carregar_central()
    notificadas = _notificacoes(central, monkeypatch)

    _evento_visao(central, _leitura_dispenser(
        slot_id, camera, "leitura_dispenser_divergencia",
        sku_lido="SKU-ERRADO", match_sku=False))

    chaves = [chave for chave, _ in notificadas]
    assert chaves == [f"OS-CAM:visao_dispenser:{slot_id}"]


@pytest.mark.parametrize("camera,slot_id", [
    ("dispenser_esq", SLOT_ESQ),
    ("dispenser_dir", SLOT_DIR),
])
def test_divergencia_de_sku_abre_alarme_com_a_camera_na_fonte(
        carregar_central, camera, slot_id):
    """A fonte identifica a câmera física: é o que aponta a lente a inspecionar."""
    central = carregar_central()

    _evento_visao(central, _leitura_dispenser(
        slot_id, camera, "leitura_dispenser_divergencia",
        sku_lido="SKU-ERRADO", match_sku=False))

    (alarme,) = central.banco.chamadas_de("salvar_alarme")
    fonte, tipo, _ = alarme["args"]
    assert fonte == f"camera_{camera}_{slot_id}"
    assert tipo == "divergencia_sku"


@pytest.mark.parametrize("camera,slot_id", [
    ("dispenser_esq", SLOT_ESQ),
    ("dispenser_dir", SLOT_DIR),
])
def test_historico_grava_qual_camera_leu(carregar_central, camera, slot_id):
    """`visao_leituras.camera` é o que permite auditar uma câmera sozinha."""
    central = carregar_central()

    _evento_visao(central, _leitura_dispenser(
        slot_id, camera, "leitura_dispenser_ok", sku_lido="SKU-1", match_sku=True))

    (leitura,) = central.banco.chamadas_de("salvar_leitura_visao")
    assert leitura["args"][1] == camera


def test_camera_ausente_cai_no_lado_do_slot(carregar_central):
    """Contrato antigo (camera="dispenser", uma fileira só) não some do painel.

    Sem o fallback, a leitura não acharia chave de estado: sumiria do painel e
    do histórico sem erro nenhum no log — o pior modo de falhar.
    """
    central = carregar_central()

    _evento_visao(central, _leitura_dispenser(
        SLOT_DIR, "dispenser", "leitura_dispenser_ok",
        sku_lido="SKU-1", match_sku=True))

    visao = central.modulo._estado["visao"]
    assert visao["camera_dispenser_dir"]["ultima_leitura"] == "leitura_dispenser_ok"
    assert visao["camera_dispenser_esq"]["ultima_leitura"] is None


def test_falha_de_leitura_nomeia_o_lado_na_descricao(carregar_central):
    """Quem lê o alarme precisa saber a qual câmera ir — não só a qual slot."""
    central = carregar_central()

    _evento_visao(central, _leitura_dispenser(
        SLOT_DIR, "dispenser_dir", "leitura_dispenser_falha",
        motivo="camera_obstruida"))

    (alarme,) = central.banco.chamadas_de("salvar_alarme")
    _, tipo, descricao = alarme["args"]
    assert tipo == "falha_leitura_dispenser"
    assert "direita" in descricao


# ── Telemetria da balança: o peso ao vivo chega ao estado e ao banco ─────────
# `docs/PROTOCOLO_SERIAL.md` §5 declara o evento `telemetria` da balança com
# `componente`, `temperatura_c`, `peso_atual_g` e `ts`. O handler gravava só a
# temperatura, e `_estado["peso"]` só mudava em `peso_ok`, `peso_divergencia` e
# `tara_ok`: `peso_atual_g` atravessava a placa, o adapter e o endpoint e
# morria no handler. O peso ao vivo nunca chegava ao dashboard nem ao banco —
# e é o dado que a bancada mais quer ver entre uma pesagem e outra.

def _evento_peso(central, payload: dict) -> None:
    asyncio.run(central.modulo._handle_evento_peso(payload))


def _pesagem_de_slot(central) -> dict:
    """Uma pesagem real, para que haja veredito no estado antes da telemetria."""
    _evento_peso(central, {
        "tipo": "peso_divergencia", "os_id": "OS-42", "slot_id": 3,
        "peso_medido_g": 450.0, "peso_esperado_g": 500.0, "desvio_pct": 10.0,
        "ts": "2026-09-12T10:00:00",
    })
    return dict(central.modulo._estado["peso"])


def _telemetria_balanca(peso_atual_g: float = 123.4, ts: str = "2026-09-12T10:00:15") -> dict:
    return {
        "tipo": "telemetria", "componente": "hx711_balanca_mesa",
        "temperatura_c": 24.7, "peso_atual_g": peso_atual_g, "ts": ts,
    }


def test_peso_atual_e_gravado_como_leitura_de_sensor(carregar_central):
    """No mesmo padrão da temperatura: uma linha em `leituras_sensores`."""
    central = carregar_central()

    _evento_peso(central, _telemetria_balanca(peso_atual_g=123.4))

    leituras = {c["args"][1]: c["args"] for c in central.banco.chamadas_de("salvar_leitura_sensor")}
    assert leituras["temperatura"] == ("hx711_balanca_mesa", "temperatura", 24.7, "°C")
    assert leituras["peso"] == ("hx711_balanca_mesa", "peso", 123.4, "g")


def test_peso_atual_e_publicado_no_estado_em_campo_proprio(carregar_central):
    central = carregar_central()

    _evento_peso(central, _telemetria_balanca(peso_atual_g=123.4, ts="2026-09-12T10:00:15"))

    peso = central.modulo._estado["peso"]
    assert peso["peso_atual_g"] == 123.4
    assert peso["peso_atual_ts"] == "2026-09-12T10:00:15"


def test_telemetria_nao_toca_nos_campos_do_triple_check(carregar_central):
    """O caso que passaria calado: telemetria desfazendo o veredito da pesagem.

    `peso_medido_g`, `peso_esperado_g`, `desvio_pct`, `ultima_leitura`,
    `slot_id` e `ts` são a última PESAGEM de slot — o que o Triple Check
    decidiu e o que o cartão da balança mostra. A leitura contínua tem o seu
    próprio par de campos e não pode encostar nesses: é a regra "fontes de
    verdade" do README aplicada à balança.
    """
    central = carregar_central()
    antes = _pesagem_de_slot(central)

    _evento_peso(central, _telemetria_balanca(peso_atual_g=7.0, ts="2026-09-12T10:00:15"))

    depois = central.modulo._estado["peso"]
    for campo in ("ultima_leitura", "slot_id", "peso_medido_g",
                  "peso_esperado_g", "desvio_pct", "ts"):
        assert depois[campo] == antes[campo], campo
    assert depois["ultima_leitura"] == "peso_divergencia"
    assert depois["peso_atual_g"] == 7.0
    assert depois["peso_atual_ts"] == "2026-09-12T10:00:15"


def test_telemetria_sem_peso_nao_inventa_leitura(carregar_central):
    """Placa antiga que só mande temperatura: nada de linha "peso" com zero."""
    central = carregar_central()

    _evento_peso(central, {"tipo": "telemetria", "componente": "hx711_balanca_mesa",
                           "temperatura_c": 24.7, "ts": "2026-09-12T10:00:15"})

    tipos = [c["args"][1] for c in central.banco.chamadas_de("salvar_leitura_sensor")]
    assert tipos == ["temperatura"]
    assert central.modulo._estado["peso"]["peso_atual_g"] is None


def test_telemetria_da_balanca_continua_segurada_pelo_throttle(carregar_central,
                                                                monkeypatch):
    """Publicar no estado não pode contornar o throttle: telemetria é periódica."""
    central = carregar_central()
    assert "telemetria" in central.modulo._TIPOS_ALTA_FREQUENCIA
    _evento_cnc(central, "movendo")          # consome a janela do throttle
    enviados = _contar_broadcasts(central, monkeypatch)

    for i in range(10):
        _evento_peso(central, _telemetria_balanca(peso_atual_g=100.0 + i))

    assert enviados == []
    assert central.modulo._broadcast_pendente is True


def test_pesagem_de_slot_continua_saindo_na_hora(carregar_central, monkeypatch):
    """Controle: o que NÃO é telemetria não pode ter caído no throttle junto."""
    central = carregar_central()
    _evento_cnc(central, "movendo")
    enviados = _contar_broadcasts(central, monkeypatch)

    _pesagem_de_slot(central)

    assert len(enviados) == 1


# ── Escrita crítica perdida vira alarme, não um warning ───────────────────────
#
# `_db` engolia TODA falha de banco num `logger.warning`. Foi isso que escondeu
# o `medicamento` nulo acima por tempo demais: a linha de dispensa não existia,
# e o único rastro era um warning no meio de centenas de linhas de evento.
#
# A separação não é de gravidade abstrata, é de quem conserta. Telemetria e
# `dispenser_estado` são MEDIÇÕES REPETIDAS — o valor seguinte chega em 15s e
# reescreve a linha. `salvar_dispensa` e `atualizar_item_os` são o rastro
# NOMINAL de quem recebeu o quê: acontecem UMA vez, ninguém os reemite, e a
# linha perdida some do relatório da OS para sempre.

def _explodir(central, monkeypatch, nome: str, erro: str = "1048 Column cannot be null"):
    """Troca uma função de banco por uma que levanta, como o PyMySQL levantaria."""
    def _levanta(*args, **kwargs):
        raise RuntimeError(erro)

    monkeypatch.setattr(central.modulo, nome, _levanta)


def _alarmes_de_persistencia(central) -> list:
    return [c for c in central.banco.chamadas_de("salvar_alarme")
            if c["args"][1] == "persistencia_falhou"]


def test_falha_ao_gravar_a_dispensa_abre_alarme(carregar_central, monkeypatch):
    central = carregar_central()
    central.slot(1).update({"status": "dispensando", "os_id": "OS-1",
                            "medicamento": "Dipirona"})
    _explodir(central, monkeypatch, "salvar_dispensa")

    _evento(central, _dispensa_completa(1))

    (alarme,) = _alarmes_de_persistencia(central)
    fonte, tipo, descricao = alarme["args"]
    assert (fonte, tipo) == ("central", "persistencia_falhou")
    assert "salvar_dispensa" in descricao
    # O payload vai junto: é com ele que alguém reconstrói a linha à mão.
    assert "Dipirona" in descricao


def test_falha_ao_fechar_o_item_da_os_abre_alarme(carregar_central, monkeypatch):
    central = carregar_central()
    central.slot(1).update({"status": "dispensando", "os_id": "OS-1",
                            "medicamento": "Dipirona"})
    _explodir(central, monkeypatch, "atualizar_item_os")

    _evento(central, _dispensa_completa(1))

    (alarme,) = _alarmes_de_persistencia(central)
    assert "atualizar_item_os" in alarme["args"][2]


@pytest.mark.parametrize("escrita", ["salvar_dispenser_estado", "salvar_leitura_sensor"])
def test_falha_de_medicao_repetida_continua_so_no_log(carregar_central, monkeypatch,
                                                      escrita):
    """Controle da linha: alarme para TODA escrita encheria a tela de
    necessidades com um item a cada 15s enquanto o MySQL reinicia."""
    central = carregar_central()
    _explodir(central, monkeypatch, escrita)

    _evento(central, {"tipo": "telemetria", "dispenser_id": 1, "valor_c": 30.0})
    _evento(central, {"tipo": "status", "dispenser_id": 1, "quantidade": 4})

    assert _alarmes_de_persistencia(central) == []


def test_o_alarme_que_tambem_falha_nao_derruba_o_handler(carregar_central,
                                                         monkeypatch):
    """Banco fora derruba as DUAS escritas. O handler é o único caminho de
    volta do adapter ao orquestrador: se ele propagar, o adapter toma 500 e
    retenta um evento que já foi processado."""
    central = carregar_central()
    central.slot(1).update({"status": "dispensando", "os_id": "OS-1"})
    _explodir(central, monkeypatch, "salvar_dispensa")
    _explodir(central, monkeypatch, "salvar_alarme", erro="2003 Can't connect")

    _evento(central, _dispensa_completa(1))          # não pode levantar

    assert central.slot(1)["status"] == "concluido"  # o fluxo andou assim mesmo


def test_a_falha_critica_e_registrada_como_erro(carregar_central, monkeypatch,
                                                caplog):
    """`warning` foi o que escondeu o bug: quem varre log de produção por
    `ERROR` não via nada."""
    central = carregar_central()
    central.slot(1).update({"status": "dispensando", "os_id": "OS-1"})
    _explodir(central, monkeypatch, "salvar_dispensa")

    with caplog.at_level(logging.ERROR, logger=central.modulo.logger.name):
        _evento(central, _dispensa_completa(1))

    assert any("salvar_dispensa" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.ERROR)


# ── A mesa que volta para o HOME não conclui a OS que travou ─────────────────
#
# É o caso NORMAL do Triple Check, não uma borda: a trava dispara entre dois
# ciclos, com a mesa parada. O firmware então faz homing por conta própria — é
# onde o supervisor espera encontrá-la para mexer na bancada — e emite
# `concluido` endereçado ao `trava_os_id`, que é o `os_id` do último `mover`,
# ou seja, a OS que acabou de travar.
#
# O dashboard, o `/estado` e o `/ws` passavam a mostrar CONCLUÍDA a OS que está
# parada esperando alguém liberar. É a tela que a pessoa que vai liberar está
# olhando.

def _travar(central, monkeypatch, os_id: str = "OS-42") -> None:
    """Arma a trava no orquestrador, que é onde ela mora de verdade."""
    monkeypatch.setattr(central.modulo.orch, "_trava_ativa", True)
    monkeypatch.setattr(central.modulo.orch, "_trava_os_id", os_id)


def _com_os_ativa(central, os_id: str = "OS-42") -> None:
    central.modulo._estado["os_ativa"] = {"os_id": os_id, "status": "em_andamento"}


def test_concluido_da_mesa_nao_fecha_os_travada(carregar_central, monkeypatch):
    central = carregar_central()
    _com_os_ativa(central)
    _travar(central, monkeypatch)

    _evento_cnc(central, "concluido")

    assert central.modulo._estado["os_ativa"]["status"] == "em_andamento", (
        "a OS aparece como CONCLUIDA no painel enquanto a trava do Triple Check "
        "está ativa e o supervisor ainda não liberou")


def test_concluido_da_mesa_fecha_a_os_sem_trava(carregar_central):
    """Controle: o caminho normal de fim de ciclo continua fechando a OS."""
    central = carregar_central()
    _com_os_ativa(central)

    _evento_cnc(central, "concluido")

    assert central.modulo._estado["os_ativa"]["status"] == "concluida"


def test_o_evento_de_homing_da_trava_continua_virando_linha(carregar_central,
                                                            monkeypatch):
    """A guarda é só sobre o STATUS DA OS. O `concluido` continua sendo
    transição, então continua indo para `cnc_eventos` e para a posição da mesa
    — é o registro de que ela voltou ao HOME."""
    central = carregar_central()
    _com_os_ativa(central)
    _travar(central, monkeypatch)

    _evento_cnc(central, "concluido")

    (chamada,) = central.banco.chamadas_de("salvar_cnc_evento")
    assert chamada["args"][1] == "concluido"
    assert central.modulo._estado["cnc"]["status"] == "concluido"


# ── OS órfã: o central que morreu no meio do ciclo deixa rastro no banco ─────
#
# O orquestrador é um loop ÚNICO, então no boot não existe OS em execução por
# definição: toda linha `em_andamento` é de um processo que já morreu. Elas não
# somem sozinhas, e `get_ordem_ativa` prefere `em_andamento ORDER BY criado_em
# ASC` — a órfã MAIS ANTIGA vira "a OS ativa" para sempre, no `GET /os/ativa`,
# no app de manutenção e no espelho do painel de bancada. Na bancada isso já
# aconteceu: três ordens paradas desde 11/09.

def _com_orfas(central, monkeypatch, *os_ids: str) -> None:
    monkeypatch.setattr(central.modulo, "fechar_os_orfas", lambda: list(os_ids))


def test_boot_fecha_as_os_orfas_com_um_alarme_por_linha(carregar_central,
                                                        monkeypatch):
    central = carregar_central()
    _com_orfas(central, monkeypatch, "OS-A", "OS-B")

    asyncio.run(central.modulo._fechar_os_orfas_no_boot())

    alarmes = [c["args"] for c in central.banco.chamadas_de("salvar_alarme")]
    assert [tipo for _, tipo, _ in alarmes] == ["os_orfa_no_boot"] * 2
    # Um por linha, e não um agregado: é o `os_id` que leva ao relatório
    # daquela OS e mostra até onde ela chegou.
    assert "OS-A" in alarmes[0][2]
    assert "OS-B" in alarmes[1][2]


def test_boot_sem_orfas_nao_abre_alarme(carregar_central, monkeypatch):
    """Controle: o caso normal é não haver nenhuma."""
    central = carregar_central()
    _com_orfas(central, monkeypatch)

    asyncio.run(central.modulo._fechar_os_orfas_no_boot())

    assert central.banco.chamadas_de("salvar_alarme") == []


def test_banco_fora_no_boot_nao_derruba_o_central(carregar_central, monkeypatch):
    """A reconciliação roda de novo no startup seguinte; derrubar o boot por
    causa dela trocaria um `GET /os/ativa` errado por uma planta parada."""
    central = carregar_central()

    def _levanta():
        raise RuntimeError("2003 Can't connect")

    monkeypatch.setattr(central.modulo, "fechar_os_orfas", _levanta)

    asyncio.run(central.modulo._fechar_os_orfas_no_boot())   # não pode levantar


def test_a_lifespan_chama_a_reconciliacao():
    """A função certa e ninguém a chamando é o mesmo que não existir.

    Os testes acima a chamam direto — é o que permite exercitá-la sem subir o
    FastAPI —, e por isso nenhum deles fica vermelho se a linha sumir da
    `lifespan`. Esta varredura é a que fica.
    """
    arvore = ast.parse((CENTRAL_DIR / "main.py").read_text(encoding="utf-8"))

    for no in ast.walk(arvore):
        if isinstance(no, ast.AsyncFunctionDef) and no.name == "lifespan":
            chamadas = {c.func.id for c in ast.walk(no)
                        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            assert "_fechar_os_orfas_no_boot" in chamadas
            return
    pytest.fail("`lifespan` não foi encontrada em main.py — o extrator quebrou")


# ── Campo torto num evento não pode virar 500 ────────────────────────────────
#
# O payload dos adapters atravessa SEM interpretação — é o contrato —, então um
# campo numérico pode chegar `null`, string ou float. `f"(Δ={det - esp:+d})"`
# exige int: um `null` derrubava o endpoint com 500, o adapter retentava 3× e o
# evento se perdia de vez. A divergência de CONTAGEM é uma das três fontes do
# Triple Check; perdê-la por causa de uma formatação é perder a trava.

@pytest.mark.parametrize("detectada, esperada", [
    (None, 10), (10, None), ("9", 10), (9.0, 10.0), (None, None),
])
def test_divergencia_de_mesa_com_campo_torto_nao_levanta(carregar_central,
                                                         detectada, esperada):
    central = carregar_central()

    asyncio.run(central.modulo._handle_evento_visao({
        "tipo": "leitura_mesa_divergencia", "camera": "mesa", "slot_id": 3,
        "os_id": "OS-1",
        "quantidade_detectada": detectada, "quantidade_esperada": esperada,
    }))

    (alarme,) = [c["args"] for c in central.banco.chamadas_de("salvar_alarme")]
    assert alarme[1] == "divergencia_contagem"


def test_a_divergencia_de_mesa_avisa_o_orquestrador_mesmo_com_campo_torto(
        carregar_central, monkeypatch):
    """O 500 não custava só a linha de alarme: custava a NOTIFICAÇÃO, que é o
    que faz a trava acontecer."""
    central = carregar_central()
    avisos = []
    monkeypatch.setattr(central.modulo.orch, "notificar_evento",
                        lambda chave, payload: avisos.append(chave))

    asyncio.run(central.modulo._handle_evento_visao({
        "tipo": "leitura_mesa_divergencia", "camera": "mesa", "slot_id": 3,
        "os_id": "OS-1", "quantidade_detectada": None, "quantidade_esperada": 10,
    }))

    assert "OS-1:visao_mesa:3" in avisos


def test_o_numero_certo_continua_aparecendo_na_descricao(carregar_central):
    """Controle: tolerar o torto não pode custar a informação do caso normal."""
    central = carregar_central()

    asyncio.run(central.modulo._handle_evento_visao({
        "tipo": "leitura_mesa_divergencia", "camera": "mesa", "slot_id": 3,
        "os_id": "OS-1", "quantidade_detectada": 8, "quantidade_esperada": 10,
    }))

    (alarme,) = [c["args"] for c in central.banco.chamadas_de("salvar_alarme")]
    assert "esperado=10 detectado=8" in alarme[2]
    assert "-2" in alarme[2]


# ── O WebSocket não vaza conexão ─────────────────────────────────────────────

def test_cliente_que_sai_no_meio_do_broadcast_nao_pula_o_seguinte(carregar_central):
    """Há um `await` dentro do laço, e nesse ponto o loop roda outra corrotina.

    Iterar a lista VIVA e remover um item durante a iteração desloca os
    índices: o `for` pula o cliente seguinte, que perde aquele snapshot sem
    erro nenhum. Com o `/ws` chamando `disconnect` no `finally`, isso deixou de
    ser hipótese — a saída de um cliente acontece exatamente durante o `await`
    de outro.
    """
    central = carregar_central()
    manager = central.modulo.WSManager()

    class Cliente:
        def __init__(self, ao_enviar=None):
            self.recebidos = []
            self.ao_enviar = ao_enviar

        async def send_text(self, msg):
            if self.ao_enviar:
                self.ao_enviar()
            self.recebidos.append(msg)

    segundo = Cliente()
    terceiro = Cliente()
    # O primeiro sai da lista enquanto está sendo servido — é o `finally` do
    # handler dele rodando durante este `await`.
    primeiro = Cliente(ao_enviar=lambda: manager.disconnect(primeiro))
    manager.active.extend([primeiro, segundo, terceiro])

    asyncio.run(manager.broadcast({"tipo": "estado"}))

    assert segundo.recebidos, "o cliente seguinte foi PULADO pelo deslocamento"
    assert terceiro.recebidos


def test_toda_saida_do_ws_tira_o_cliente_da_lista(carregar_central):
    """Só `WebSocketDisconnect` era tratada, e ela é UMA das saídas.

    `CancelledError` no shutdown, erro de rede no `send_text` do snapshot, um
    cliente que morre de outro jeito — qualquer uma deixava o WebSocket na
    lista do manager para sempre. A partir daí todo broadcast tentava escrever
    nele, e cada reconexão de um dashboard acrescentava mais um.
    """
    import ast
    import inspect

    arvore = ast.parse(inspect.getsource(central_ws := carregar_central()
                                         .modulo.ws_endpoint))
    corpo = arvore.body[0]
    assert any(isinstance(no, ast.Try) and no.finalbody for no in corpo.body), (
        "`ws_endpoint` não tem `finally` — a desconexão depende de qual exceção "
        "saiu, e só uma delas era tratada")
    assert central_ws is not None
