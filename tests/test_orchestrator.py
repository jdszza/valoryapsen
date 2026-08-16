"""
Testes do orquestrador: atribuição de slots e abort de OS.

O que está em teste é o caminho que levava o sistema à parada total. Cada OS
abortada deixava o estoque físico no dispenser — o abort só resetava a memória
do central — e `atribuir_slots` só aceitava slot ocupado quando o medicamento
era o mesmo. Com 96 medicamentos no catálogo, o slot com resíduo órfão saía de
circulação para sempre; depois de ~6 abortos toda OS nova era rejeitada com
"sem_slot" e o sistema parava sozinho.

As duas metades da correção:

  - `atribuir_slots` ganhou um passo 3 — slot ocupado por OUTRO medicamento é
    aceito e marcado com `precisa_limpeza`. A função continua PURA: ela marca,
    quem limpa é o `_processar_os`;
  - `_abortar_os` recebe as atribuições e manda limpar cada slot que a OS
    chegou a reservar, devolvendo o slot ao pool.
"""
import asyncio

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────────

def _dispensers(**slots) -> dict:
    """Snapshot de estado dos 6 slots; os não citados ficam vazios.

    Uso: `_dispensers(**{"1": ("Dipirona", 5)})` → D1 com resíduo, resto livre.
    """
    estado = {
        str(i): {"medicamento": None, "sku": None, "categoria": None, "quantidade": 0}
        for i in range(1, 7)
    }
    for slot_id, (med, qtd) in slots.items():
        estado[slot_id].update({"medicamento": med, "quantidade": qtd})
    return estado


def _todos_ocupados() -> dict:
    """Os 6 slots com medicamentos diferentes e resíduo — o cenário do impasse."""
    return _dispensers(**{
        "1": ("Ibuprofeno",  7),
        "2": ("Amoxicilina", 3),
        "3": ("Losartana",   9),
        "4": ("Omeprazol",   5),
        "5": ("Metformina",  2),
        "6": ("Captopril",   8),
    })


def _item(med: str, qtd: int = 10) -> dict:
    return {"medicamento": med, "sku": f"{med[:3].upper()}-1", "categoria": "geral",
            "quantidade": qtd}


# ── atribuir_slots: função pura ────────────────────────────────────────────────

def test_reaproveita_slot_com_residual_do_mesmo_medicamento(carregar_orquestrador):
    """Passo 1: resíduo do mesmo item é reaproveitado, sem limpeza."""
    orq = carregar_orquestrador()

    (atrib,) = orq.modulo.atribuir_slots(
        [_item("Dipirona")], _dispensers(**{"4": ("Dipirona", 6)})
    )

    assert atrib["dispenser_id"] == 4
    assert atrib["precisa_limpeza"] is False


def test_prefere_slot_livre_a_slot_ocupado(carregar_orquestrador):
    """Passo 2: com slot vazio disponível, ninguém precisa ser esvaziado."""
    orq = carregar_orquestrador()
    estado = _dispensers(**{
        "1": ("Ibuprofeno",  7),
        "2": ("Amoxicilina", 3),
        "3": ("Losartana",   0),   # quantidade zero = livre, mesmo com nome preso
    })

    (atrib,) = orq.modulo.atribuir_slots([_item("Dipirona")], estado)

    assert atrib["dispenser_id"] == 3
    assert atrib["precisa_limpeza"] is False


def test_slot_ocupado_por_outro_medicamento_e_aceito_com_limpeza(carregar_orquestrador):
    """Passo 3: sem nenhum slot livre, a OS não é mais rejeitada.

    Antes desta regra, `atribuir_slots` devolvia None aqui e o `_processar_os`
    fechava a OS com alarme "sem_slot".
    """
    orq = carregar_orquestrador()

    atribuicoes = orq.modulo.atribuir_slots([_item("Dipirona")], _todos_ocupados())

    assert atribuicoes is not None
    (atrib,) = atribuicoes
    assert atrib["medicamento"] == "Dipirona"
    assert atrib["precisa_limpeza"] is True


def test_limpeza_sacrifica_o_slot_de_menor_residual(carregar_orquestrador):
    """Entre slots ocupados, descarta-se o que tem menos estoque (D5, com 2)."""
    orq = carregar_orquestrador()

    (atrib,) = orq.modulo.atribuir_slots([_item("Dipirona")], _todos_ocupados())

    assert atrib["dispenser_id"] == 5


def test_slots_a_limpar_nao_se_repetem_entre_itens(carregar_orquestrador):
    """Dois itens na mesma OS não podem cair no mesmo slot sacrificado."""
    orq = carregar_orquestrador()

    atribuicoes = orq.modulo.atribuir_slots(
        [_item("Dipirona"), _item("Paracetamol")], _todos_ocupados()
    )

    ids = [a["dispenser_id"] for a in atribuicoes]
    assert len(set(ids)) == 2
    assert all(a["precisa_limpeza"] for a in atribuicoes)


def test_sem_slot_para_todos_os_itens_devolve_none(carregar_orquestrador):
    """Mais itens que slots continua sendo o único caso de rejeição."""
    orq = carregar_orquestrador()

    atribuicoes = orq.modulo.atribuir_slots(
        [_item(f"Med{i}") for i in range(7)], _todos_ocupados()
    )

    assert atribuicoes is None


def test_atribuir_slots_nao_toca_no_estado_recebido(carregar_orquestrador):
    """A função é pura: nenhum comando enviado, nenhum dict alterado."""
    orq = carregar_orquestrador()
    estado = _todos_ocupados()
    antes = {k: dict(v) for k, v in estado.items()}

    orq.modulo.atribuir_slots([_item("Dipirona")], estado)

    assert estado == antes
    assert orq.adapter.chamadas == []


# ── _abortar_os: devolve os slots ao pool ──────────────────────────────────────

def _atribuicoes(*slots) -> list:
    return [
        {"dispenser_id": s, "medicamento": f"Med{s}", "sku": f"SKU-{s}",
         "categoria": "geral", "quantidade": 10, "precisa_limpeza": False}
        for s in slots
    ]


def _ocupar(orq, *slots) -> None:
    """Coloca a OS em andamento com os slots já reservados para ela."""
    orq.estado["os_ativa"] = {"os_id": "OS-42", "status": "em_andamento"}
    orq.estado["atribuicao_ia"] = _atribuicoes(*slots)
    for s in slots:
        orq.slot(s).update({
            "status": "aguardando_carga", "os_id": "OS-42",
            "medicamento": f"Med{s}", "quantidade": 10,
        })


def test_abort_manda_limpar_cada_slot_atribuido(carregar_orquestrador):
    """O estoque órfão é descartado — é o que devolve o slot ao pool."""
    orq = carregar_orquestrador()
    _ocupar(orq, 2, 5)

    asyncio.run(orq.modulo._abortar_os("OS-42", "erro_cnc", _atribuicoes(2, 5)))

    limpezas = orq.adapter.comandos("/comandos/limpar")
    assert [c["dispenser_id"] for c in limpezas] == [2, 5]
    assert all("OS-42" in c["solicitado_por"] for c in limpezas)


def test_abort_devolve_o_estado_em_memoria_para_idle(carregar_orquestrador):
    orq = carregar_orquestrador()
    _ocupar(orq, 2, 5)

    asyncio.run(orq.modulo._abortar_os("OS-42", "erro_cnc", _atribuicoes(2, 5)))

    assert orq.estado["os_ativa"] is None
    assert orq.estado["atribuicao_ia"] == []
    for slot_id in range(1, 7):
        assert orq.slot(slot_id)["status"] == "idle"
        assert orq.slot(slot_id)["os_id"] is None


def test_abort_registra_o_motivo_original(carregar_orquestrador):
    """A OS vai para "erro" com o alarme do motivo que a derrubou."""
    orq = carregar_orquestrador()
    _ocupar(orq, 3)

    asyncio.run(orq.modulo._abortar_os("OS-42", "erro_dispenser", _atribuicoes(3)))

    (ordem,) = orq.banco.chamadas_de("atualizar_status_ordem")
    assert ordem["args"] == ("OS-42", "erro")
    (alarme,) = orq.banco.chamadas_de("salvar_alarme")
    assert alarme["args"][1] == "erro_dispenser"


def test_abort_sem_atribuicoes_nao_manda_limpar_nada(carregar_orquestrador):
    """Abort antes de qualquer reserva de slot não mexe no hardware."""
    orq = carregar_orquestrador()

    asyncio.run(orq.modulo._abortar_os("OS-42", "erro_limpeza_previa"))

    assert orq.adapter.comandos("/comandos/limpar") == []


@pytest.mark.parametrize("modo_falha", ["adapter_fora", "sem_confirmacao"])
def test_limpeza_nao_confirmada_vira_alarme_sem_mascarar_o_abort(
    carregar_orquestrador, monkeypatch, modo_falha
):
    """Adapter fora do ar ou equipamento mudo: os dois viram alarme próprio.

    O motivo original do abort continua registrado — o alarme da limpeza é um
    segundo registro, não uma substituição.
    """
    orq = carregar_orquestrador()
    _ocupar(orq, 4)
    if modo_falha == "adapter_fora":
        orq.adapter.aceita = False
    else:
        orq.adapter.confirma_limpeza = False
        monkeypatch.setattr(orq.modulo.settings, "TIMEOUT_LIMPEZA", 0.01)

    asyncio.run(orq.modulo._abortar_os("OS-42", "erro_carregamento", _atribuicoes(4)))

    codigos = [c["args"][1] for c in orq.banco.chamadas_de("salvar_alarme")]
    assert codigos == ["erro_carregamento", "limpeza_pos_abort_falhou"]
    # Mesmo sem confirmação, o slot volta a idle: a memória não pode ficar presa
    # numa OS morta. O alarme é que sinaliza o resíduo possivelmente encalhado.
    assert orq.slot(4)["status"] == "idle"


def test_liberar_slot_confirma_pelo_evento_do_dispenser(carregar_orquestrador):
    """O orquestrador só considera o slot livre com o "limpeza_ok" na mão."""
    orq = carregar_orquestrador()

    assert asyncio.run(orq.modulo._liberar_slot(1, "teste")) is True
    assert orq.modulo._pending_events == {}   # nada vazou para o próximo ciclo


def test_liberar_slot_falha_quando_a_limpeza_e_recusada(carregar_orquestrador,
                                                        monkeypatch):
    """Recusa do simulador (`limpeza_em_operacao`) chega como evento de erro."""
    orq = carregar_orquestrador()
    orq.adapter.confirma_limpeza = False
    monkeypatch.setattr(orq.modulo.settings, "TIMEOUT_LIMPEZA", 5)

    async def _cenario():
        tarefa = asyncio.ensure_future(orq.modulo._liberar_slot(1, "teste"))
        await asyncio.sleep(0)   # deixa o comando sair antes da recusa chegar
        orq.modulo.notificar_evento(
            "limpeza:1", {"tipo": "erro", "codigo_erro": "limpeza_em_operacao"}
        )
        return await tarefa

    assert asyncio.run(_cenario()) is False
