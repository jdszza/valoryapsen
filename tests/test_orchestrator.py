"""
Testes do orquestrador: atribuição de slots, abort de OS e Triple Check.

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
import itertools
import time

import pytest

from conftest import NUM_SLOTS, SLOTS_POR_FILEIRA


# ── Helpers ────────────────────────────────────────────────────────────────────

def _dispensers(**slots) -> dict:
    """Snapshot de estado de todos os slots; os não citados ficam vazios.

    Uso: `_dispensers(**{"1": ("Dipirona", 5)})` → D1 com resíduo, resto livre.
    """
    estado = {
        str(i): {"medicamento": None, "sku": None, "categoria": None, "quantidade": 0}
        for i in range(1, NUM_SLOTS + 1)
    }
    for slot_id, (med, qtd) in slots.items():
        estado[slot_id].update({"medicamento": med, "quantidade": qtd})
    return estado


# Slot que `_todos_ocupados` deixa com o MENOR resíduo — o que a política de
# descarte deve sacrificar. Fica na fileira da direita (D5 numa célula de 8)
# de propósito: a escolha é por estoque, não por posição.
SLOT_MENOR_RESIDUO = SLOTS_POR_FILEIRA + 1
RESIDUO_MENOR = 2
RESIDUO_PADRAO = 9


def _todos_ocupados() -> dict:
    """Todo slot com um medicamento diferente e resíduo — o cenário do impasse."""
    return _dispensers(**{
        str(i): (
            f"Ocupante{i}",
            RESIDUO_MENOR if i == SLOT_MENOR_RESIDUO else RESIDUO_PADRAO,
        )
        for i in range(1, NUM_SLOTS + 1)
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
    """Entre slots ocupados, descarta-se o que tem menos estoque."""
    orq = carregar_orquestrador()

    (atrib,) = orq.modulo.atribuir_slots([_item("Dipirona")], _todos_ocupados())

    assert atrib["dispenser_id"] == SLOT_MENOR_RESIDUO


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
        [_item(f"Med{i}") for i in range(NUM_SLOTS + 1)], _todos_ocupados()
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


# ── Geometria e rota: duas fileiras frente a frente ────────────────────────────
#
# Enquanto a célula era uma fileira só, o Y de todo slot era 0 e qualquer
# ordenação devolvia a mesma linha reta — a rota não tinha o que errar. Com as
# duas fileiras, escolher a ordem passa a valer distância de verdade, e é aqui
# que se cobra a escolha (serpentina; ver `planejar_rota`).

def _slots_de_cada_lado() -> tuple[list[int], list[int]]:
    esquerda = list(range(1, SLOTS_POR_FILEIRA + 1))
    direita  = list(range(SLOTS_POR_FILEIRA + 1, NUM_SLOTS + 1))
    return esquerda, direita


def test_posicoes_formam_duas_fileiras_frente_a_frente(carregar_orquestrador):
    """Pares D1↔D5, D2↔D6…: mesmo X, Y espelhado."""
    orq = carregar_orquestrador()
    pos = orq.modulo.POSICOES
    afastamento = orq.modulo.AFASTAMENTO_Y_MM

    assert len(pos) == NUM_SLOTS
    esquerda, direita = _slots_de_cada_lado()
    assert all(pos[d][1] == -afastamento for d in esquerda)
    assert all(pos[d][1] == +afastamento for d in direita)
    for frente, fundo in zip(esquerda, direita):
        assert pos[frente][0] == pos[fundo][0], f"D{frente} e D{fundo} não são um par"


def test_home_fica_no_corredor_antes_do_primeiro_par(carregar_orquestrador):
    """HOME no eixo do corredor (y=0) e fora da faixa dos dispensers."""
    orq = carregar_orquestrador()
    home_x, home_y = orq.modulo.HOME

    assert home_y == 0.0
    assert home_x < min(x for x, _ in orq.modulo.POSICOES.values())


def test_rota_devolve_todos_os_slots_pedidos_sem_repeticao(carregar_orquestrador):
    """Slot que sai da rota é slot que a OS não dispensa."""
    orq = carregar_orquestrador()
    esquerda, direita = _slots_de_cada_lado()
    pedidos = esquerda[:2] + direita[:2] + esquerda[-1:]

    rota = orq.modulo.planejar_rota(pedidos, orq.modulo.HOME)

    assert sorted(rota) == sorted(set(pedidos))
    assert len(rota) == len(set(rota))


def test_rota_nao_e_pior_que_a_ordem_de_entrada(carregar_orquestrador):
    """Reordenar só se justifica se encurtar o trajeto — em TODO subconjunto.

    Força bruta sobre todos os subconjuntos não-vazios da célula, não sobre um
    caso escolhido a dedo: é o que pegaria uma heurística que melhora a média e
    piora algum caso específico.
    """
    orq = carregar_orquestrador()
    home = orq.modulo.HOME

    for tamanho in range(1, NUM_SLOTS + 1):
        for entrada in itertools.combinations(range(1, NUM_SLOTS + 1), tamanho):
            rota = orq.modulo.planejar_rota(list(entrada), home)
            assert (orq.modulo.distancia_rota(rota, home)
                    <= orq.modulo.distancia_rota(list(entrada), home) + 1e-9), (
                f"rota {rota} é mais longa que a ordem de entrada {list(entrada)}"
            )


def test_rota_e_a_mais_curta_possivel_a_partir_de_home(carregar_orquestrador):
    """A serpentina não é só melhor que a entrada — é o ótimo do ciclo fechado.

    O ciclo real é HOME → slots → HOME. Com HOME e as duas fileiras na borda de
    um mesmo polígono convexo, o tour ótimo é a ordem do contorno; a serpentina
    a reproduz. Conferido contra a permutação ótima de cada subconjunto.
    """
    orq = carregar_orquestrador()
    home = orq.modulo.HOME

    for tamanho in range(1, NUM_SLOTS + 1):
        for entrada in itertools.combinations(range(1, NUM_SLOTS + 1), tamanho):
            melhor = min(orq.modulo.distancia_rota(list(p), home)
                         for p in itertools.permutations(entrada))
            escolhida = orq.modulo.distancia_rota(
                orq.modulo.planejar_rota(list(entrada), home), home
            )
            assert escolhida == pytest.approx(melhor), (
                f"subconjunto {list(entrada)}: {escolhida:.1f}mm contra {melhor:.1f}mm"
            )


def test_rota_completa_desce_um_lado_e_volta_pelo_outro(carregar_orquestrador):
    """A forma da serpentina, explícita: sem cruzar o corredor no meio."""
    orq = carregar_orquestrador()
    esquerda, direita = _slots_de_cada_lado()

    rota = orq.modulo.planejar_rota(list(range(1, NUM_SLOTS + 1)), orq.modulo.HOME)

    assert rota == esquerda + list(reversed(direita))


def test_rota_independe_da_ordem_em_que_os_slots_chegam(carregar_orquestrador):
    """Mesma OS, mesma rota: a ordem das atribuições não pode mudar o trajeto."""
    orq = carregar_orquestrador()
    esquerda, direita = _slots_de_cada_lado()
    pedidos = [direita[-1], esquerda[0], direita[0], esquerda[-1]]

    rota = orq.modulo.planejar_rota(pedidos, orq.modulo.HOME)

    assert rota == orq.modulo.planejar_rota(sorted(pedidos), orq.modulo.HOME)


def test_rota_de_um_lado_so_nao_cruza_o_corredor(carregar_orquestrador):
    """OS inteira de um lado só: nenhuma travessia, ida em x crescente."""
    orq = carregar_orquestrador()
    esquerda, _ = _slots_de_cada_lado()

    rota = orq.modulo.planejar_rota(list(reversed(esquerda)), orq.modulo.HOME)

    assert rota == esquerda


def test_cnc_recebe_a_posicao_do_mapa_do_central(carregar_orquestrador):
    """O comando carrega x/y — é o que dispensa a cópia do mapa no simulador."""
    orq = carregar_orquestrador()
    alvo = SLOTS_POR_FILEIRA + 1   # primeiro slot da fileira de trás

    asyncio.run(orq.modulo.cmd_mover(alvo, "OS-1", 1, 1))

    (comando,) = orq.adapter.comandos("/comandos/mover")
    assert (comando["posicao_x"], comando["posicao_y"]) == orq.modulo.POSICOES[alvo]


def test_homing_tambem_leva_as_coordenadas(carregar_orquestrador):
    """Mesma razão do mover: o HOME é do central, não do simulador."""
    orq = carregar_orquestrador()

    asyncio.run(orq.modulo.cmd_homing("OS-1"))

    (comando,) = orq.adapter.comandos("/comandos/homing")
    assert (comando["posicao_x"], comando["posicao_y"]) == orq.modulo.HOME


# ── atribuir_slots com a célula inteira ────────────────────────────────────────

def test_usa_os_slots_das_duas_fileiras(carregar_orquestrador):
    """Uma OS que enche a célula ocupa os dois lados, sem repetir slot."""
    orq = carregar_orquestrador()

    atribuicoes = orq.modulo.atribuir_slots(
        [_item(f"Med{i}") for i in range(NUM_SLOTS)], _dispensers()
    )

    ids = [a["dispenser_id"] for a in atribuicoes]
    assert sorted(ids) == list(range(1, NUM_SLOTS + 1))
    assert not any(a["precisa_limpeza"] for a in atribuicoes)


def test_residual_da_fileira_de_tras_e_reaproveitado(carregar_orquestrador):
    """Passo 1 não conhece geometria: o resíduo vale onde quer que esteja."""
    orq = carregar_orquestrador()
    alvo = NUM_SLOTS   # último slot da fileira da direita

    (atrib,) = orq.modulo.atribuir_slots(
        [_item("Dipirona")], _dispensers(**{str(alvo): ("Dipirona", 6)})
    )

    assert atrib["dispenser_id"] == alvo
    assert atrib["precisa_limpeza"] is False


def test_celula_cheia_sacrifica_um_slot_por_item(carregar_orquestrador):
    """Todos ocupados: cada item leva um slot distinto, todos marcados."""
    orq = carregar_orquestrador()

    atribuicoes = orq.modulo.atribuir_slots(
        [_item(f"Novo{i}") for i in range(NUM_SLOTS)], _todos_ocupados()
    )

    ids = [a["dispenser_id"] for a in atribuicoes]
    assert sorted(ids) == list(range(1, NUM_SLOTS + 1))
    assert all(a["precisa_limpeza"] for a in atribuicoes)


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
    for slot_id in range(1, NUM_SLOTS + 1):
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


# ── Status da OS no banco: quem está de fato em execução ───────────────────────
#
# "em_andamento" existia no enum, no app de manutenção e no dashboard, mas NUNCA
# era gravado:
# o orquestrador só o escrevia no dicionário em memória. Com todas as OS do
# banco paradas em "aguardando", o `ORDER BY criado_em DESC` de
# `get_ordem_ativa` devolvia a última OS ENFILEIRADA — então o GET /os/ativa
# mostrava a OS errada sempre que houvesse fila.
#
# O outro lado da mesma regra: toda saída de `_processar_os` tem que deixar um
# status terminal. Gravar "em_andamento" sem fechá-lo troca o sintoma antigo
# por um pior — OS eternamente em execução para quem consulta o banco.

def _payload_os(os_id: str, *itens) -> dict:
    return {
        "os_id":        os_id,
        "descricao":    "teste",
        "medicamentos": list(itens) or [_item("Dipirona")],
    }


def _status_gravados(orq) -> list:
    return [c["args"] for c in orq.banco.chamadas_de("atualizar_status_ordem")]


def test_inicio_do_processamento_grava_em_andamento(carregar_orquestrador):
    """É o que distingue a OS em execução das que ainda esperam na fila."""
    orq = carregar_orquestrador()

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-7")))

    assert _status_gravados(orq)[0] == ("OS-7", "em_andamento")


def test_os_completa_termina_em_concluida(carregar_orquestrador):
    orq = carregar_orquestrador()

    asyncio.run(orq.modulo._processar_os(
        _payload_os("OS-7", _item("Dipirona"), _item("Paracetamol"))
    ))

    assert _status_gravados(orq) == [("OS-7", "em_andamento"), ("OS-7", "concluida")]
    assert orq.adapter.comandos("/comandos/homing")   # a OS chegou mesmo ao fim
    assert orq.estado["os_ativa"] is None


def test_abort_no_meio_da_os_termina_em_erro(carregar_orquestrador, monkeypatch):
    """Adapter fora do ar no carregamento: a OS fecha em erro, não em aberto."""
    orq = carregar_orquestrador()
    monkeypatch.setattr(orq.modulo.settings, "TIMEOUT_CARREGAMENTO", 0.01)
    orq.adapter.aceita = False

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-8")))

    assert _status_gravados(orq) == [("OS-8", "em_andamento"), ("OS-8", "erro")]
    assert orq.estado["os_ativa"] is None


def test_os_rejeitada_por_falta_de_slot_termina_em_erro(carregar_orquestrador):
    """A rejeição é uma saída como as outras — e a mais fácil de esquecer.

    Um item a mais do que a célula tem slots: `atribuir_slots` devolve None e a
    OS nunca chega a reservar slot nenhum, então nada há para limpar no
    hardware.
    """
    orq = carregar_orquestrador()

    asyncio.run(orq.modulo._processar_os(
        _payload_os("OS-9", *[_item(f"Med{i}") for i in range(NUM_SLOTS + 1)])
    ))

    assert _status_gravados(orq) == [("OS-9", "em_andamento"), ("OS-9", "erro")]
    assert [c["args"][1] for c in orq.banco.chamadas_de("salvar_alarme")] == ["sem_slot"]
    assert orq.adapter.comandos("/comandos/limpar") == []
    assert orq.estado["os_ativa"] is None


# ── avaliar_triple_check: a regra das 3 fontes ─────────────────────────────────
#
# A regra era `n_div >= 2`: uma fonte solitária acusando erro virava alarme e a
# OS seguia para o paciente — o Triple Check operava como double check, e o
# README ainda descrevia a regra conservadora. Agora 1 divergência trava
# (`TRIPLE_CHECK_MIN_DIVERGENCIAS`, default 1).
#
# A decisão foi extraída para função de módulo — era uma closure dentro de
# `_processar_os`, alcançável só depois de encenar carga, CNC, visão e pesagem.

QTD_ALVO = 10

MESA_OK          = {"tipo": "leitura_mesa_ok", "quantidade_detectada": QTD_ALVO}
MESA_DIVERGENTE  = {"tipo": "leitura_mesa_divergencia", "quantidade_detectada": 8}
MESA_FALHA       = {"tipo": "leitura_mesa_falha"}

PESO_OK          = {"tipo": "peso_ok", "desvio_pct": 0.4}
PESO_DIVERGENTE  = {"tipo": "peso_divergencia", "desvio_pct": 20.0}
PESO_ERRO_SENSOR = {"tipo": "erro_sensor"}


def _avaliar(orq, dispensado=QTD_ALVO, mesa=MESA_OK, peso=PESO_OK, limiar=None):
    return orq.modulo.avaliar_triple_check(
        quantidade_esperada=QTD_ALVO,
        quantidade_dispensada=dispensado,
        resultado_mesa=mesa,
        resultado_peso=peso,
        min_divergencias=limiar,
    )


@pytest.mark.parametrize("dispensado, mesa, peso, n_esperado", [
    # 0 divergências — as 3 fontes concordam com o alvo
    (QTD_ALVO, MESA_OK,         PESO_OK,         0),
    # 1 divergência — cada fonte sozinha
    (8,        MESA_OK,         PESO_OK,         1),
    (QTD_ALVO, MESA_DIVERGENTE, PESO_OK,         1),
    (QTD_ALVO, MESA_OK,         PESO_DIVERGENTE, 1),
    # 2 divergências
    (8,        MESA_DIVERGENTE, PESO_OK,         2),
    (8,        MESA_OK,         PESO_DIVERGENTE, 2),
    # 3 divergências — o caso real de falha mecânica, agora que a balança enxerga
    (8,        MESA_DIVERGENTE, PESO_DIVERGENTE, 3),
])
def test_uma_divergencia_ja_trava(carregar_orquestrador, dispensado, mesa, peso,
                                  n_esperado):
    """Default conservador: qualquer fonte que contradiga o alvo suspende a OS."""
    orq = carregar_orquestrador()

    veredito = _avaliar(orq, dispensado=dispensado, mesa=mesa, peso=peso)

    assert len(veredito.divergencias) == n_esperado
    assert veredito.travar is (n_esperado >= 1)
    assert veredito.limiar == 1


def test_limiar_default_vem_da_configuracao(carregar_orquestrador, monkeypatch):
    """Sem `min_divergencias`, a função lê TRIPLE_CHECK_MIN_DIVERGENCIAS."""
    orq = carregar_orquestrador()
    monkeypatch.setattr(orq.modulo.settings, "TRIPLE_CHECK_MIN_DIVERGENCIAS", 2)

    veredito = _avaliar(orq, dispensado=8)

    assert veredito.limiar == 2
    assert veredito.divergencias  # a divergência continua registrada…
    assert veredito.travar is False  # …mas não trava com o limiar elevado


@pytest.mark.parametrize("limiar, travar_esperado", [(1, True), (2, True), (3, False)])
def test_limiar_configurado_desloca_a_decisao(carregar_orquestrador, limiar,
                                              travar_esperado):
    """Duas fontes divergentes: trava com limiar 1 e 2, não com 3."""
    orq = carregar_orquestrador()

    veredito = _avaliar(orq, dispensado=8, mesa=MESA_DIVERGENTE, limiar=limiar)

    assert len(veredito.divergencias) == 2
    assert veredito.travar is travar_esperado


@pytest.mark.parametrize("mesa, peso, indisponiveis", [
    (MESA_FALHA, PESO_OK,          1),   # câmera não conseguiu ler
    (None,       PESO_OK,          1),   # timeout da câmera
    (MESA_OK,    PESO_ERRO_SENSOR, 1),   # HX711 fora do ar
    (MESA_OK,    None,             1),   # timeout da balança
    (None,       None,             2),   # só o dispenser respondeu
])
def test_fonte_que_nao_mediu_nao_e_fonte_que_divergiu(carregar_orquestrador, mesa,
                                                       peso, indisponiveis):
    """Falha de leitura e timeout não contradizem nada — não travam sozinhos.

    É o que torna o limiar 1 sustentável: os ~2% de falha de leitura da câmera
    viravam trava por ruído, e trava por ruído é trava desligada em campo.
    """
    orq = carregar_orquestrador()

    veredito = _avaliar(orq, mesa=mesa, peso=peso)

    assert veredito.divergencias == []
    assert veredito.travar is False
    assert len(veredito.fontes_indisponiveis) == indisponiveis


def test_fonte_indisponivel_nao_esconde_divergencia_das_outras(carregar_orquestrador):
    """Câmera cega + dispensa parcial: a fonte que mediu ainda trava a OS."""
    orq = carregar_orquestrador()

    veredito = _avaliar(orq, dispensado=8, mesa=MESA_FALHA, peso=PESO_DIVERGENTE)

    assert len(veredito.divergencias) == 2
    assert veredito.fontes_indisponiveis == ["câmera_mesa: falha de leitura"]
    assert veredito.travar is True


def test_causas_nomeiam_a_fonte_e_os_numeros(carregar_orquestrador):
    """O motivo da trava vai para o painel — precisa dizer o que divergiu e quanto."""
    orq = carregar_orquestrador()

    veredito = _avaliar(orq, dispensado=8, mesa=MESA_DIVERGENTE, peso=PESO_DIVERGENTE)

    dispenser, camera, balanca = veredito.divergencias
    assert "dispensou 8 de 10" in dispenser
    assert "detectou 8 de 10" in camera
    assert "20.0%" in balanca


def test_avaliar_triple_check_e_pura(carregar_orquestrador):
    """Nenhum comando enviado, nenhum dict de entrada alterado."""
    orq = carregar_orquestrador()
    mesa = dict(MESA_DIVERGENTE)
    peso = dict(PESO_DIVERGENTE)

    _avaliar(orq, dispensado=8, mesa=mesa, peso=peso)

    assert mesa == MESA_DIVERGENTE
    assert peso == PESO_DIVERGENTE
    assert orq.adapter.chamadas == []


# ── Robustez: banco fora do ar não pode derrubar a OS ─────────────────────────

def test_falha_no_peso_unitario_nao_derruba_a_os(carregar_orquestrador, monkeypatch):
    """O lookup de peso é paralelo, e `gather` propaga a primeira exceção.

    Sem `return_exceptions=True`, um erro de banco em UM medicamento matava a
    OS inteira — sendo que o peso unitário já tem fallback (50 g). Perder
    precisão da balança em um item é aceitável; perder a OS por isso, não.
    """
    orq = carregar_orquestrador()

    def _explode(nome):
        raise RuntimeError(f"MySQL fora do ar ao buscar {nome}")

    monkeypatch.setattr(orq.modulo, "get_peso_medicamento", _explode)

    asyncio.run(orq.modulo._processar_os(
        _payload_os("OS-9", _item("Dipirona"), _item("Paracetamol"))
    ))

    assert _status_gravados(orq) == [("OS-9", "em_andamento"), ("OS-9", "concluida")]
    pesagens = orq.adapter.comandos("/comandos/pesar")
    assert len(pesagens) == 2
    assert all(p["peso_unitario_g"] == orq.modulo.PESO_UNITARIO_PADRAO_G
               for p in pesagens)


def test_peso_do_catalogo_e_usado_quando_o_banco_responde(carregar_orquestrador,
                                                          monkeypatch):
    """Guarda do caminho feliz: o fallback não pode virar o padrão."""
    orq = carregar_orquestrador()
    monkeypatch.setattr(orq.modulo, "get_peso_medicamento", lambda nome: 12.5)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-9")))

    (pesagem,) = orq.adapter.comandos("/comandos/pesar")
    assert pesagem["peso_unitario_g"] == 12.5


# ── Trava: estado interno antes da publicação ─────────────────────────────────

def test_trava_ja_e_liberavel_quando_aparece_no_dashboard(carregar_orquestrador,
                                                          monkeypatch):
    """A janela de corrida: `_estado["trava"]` publicado antes de `_trava_ativa`.

    O supervisor via a trava na tela e clicava em "Liberar"; `liberar_trava`
    caía no `if not _trava_ativa: return False` e a API respondia 409 "nenhuma
    trava ativa" com a trava bem visível. Aqui o clique acontece no instante
    exato do broadcast — o pior momento possível.
    """
    liberacoes = []

    orq = carregar_orquestrador()

    def _broadcast_e_clicar():
        if orq.estado["trava"]["ativa"]:
            liberacoes.append(orq.modulo.liberar_trava("supervisor"))

    monkeypatch.setattr(orq.modulo, "_broadcast_fn", _broadcast_e_clicar)

    asyncio.run(orq.modulo._ativar_trava("OS-1", 3, "SKU errado em D3"))

    assert liberacoes == [True], "trava apareceu na tela antes de ser liberável"


def test_trava_de_sku_errado_e_liberavel_no_instante_do_broadcast(carregar_orquestrador,
                                                                  monkeypatch):
    """O cenário completo, pelo caminho onde a corrida vivia.

    O bloco de SKU errado publicava `_estado["trava"]` e chamava o broadcast
    ANTES de `_ativar_trava`. O clique em "Liberar" que chegasse nesse intervalo
    era recusado com 409 e a OS ficava parada esperando um evento que já tinha
    sido pedido.
    """
    orq = carregar_orquestrador()
    orq.adapter.capturas_divergentes = 1      # o 1º scan volta com SKU errado
    liberacoes = []

    def _broadcast_e_clicar():
        if orq.estado["trava"]["ativa"] and not liberacoes:
            liberacoes.append(orq.modulo.liberar_trava("supervisor"))

    monkeypatch.setattr(orq.modulo, "_broadcast_fn", _broadcast_e_clicar)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-10")))

    assert liberacoes == [True], "trava visível na tela mas ainda não liberável"
    assert _status_gravados(orq)[-1] == ("OS-10", "concluida")
    assert orq.estado["trava"]["ativa"] is False


def test_ativar_trava_publica_o_motivo_para_o_painel(carregar_orquestrador):
    """A publicação mudou de lugar (foi para dentro de `_ativar_trava`), não sumiu."""
    orq = carregar_orquestrador()

    asyncio.run(orq.modulo._ativar_trava("OS-1", 3, "SKU errado em D3"))

    assert orq.estado["trava"] == {
        "ativa": True, "os_id": "OS-1", "slot_id": 3, "motivo": "SKU errado em D3",
    }
    assert orq.modulo.get_trava_estado()["ativa"] is True


# ── Nenhuma query síncrona dentro do event loop ───────────────────────────────

def test_orquestrador_nao_chama_o_banco_no_event_loop():
    """Toda função de `database` tem que passar por `asyncio.to_thread`.

    Uma query síncrona no event loop congela o central INTEIRO enquanto o MySQL
    responde: WebSocket, endpoints e o próprio orquestrador. Varredura por AST
    em vez de lista manual — chamada nova entra na conta sozinha.
    """
    import ast
    from pathlib import Path

    arquivo = Path(__file__).resolve().parent.parent / "central-computer" / "orchestrator.py"
    arvore = ast.parse(arquivo.read_text(encoding="utf-8"))

    do_banco = {
        alias.name
        for no in ast.walk(arvore)
        if isinstance(no, ast.ImportFrom) and no.module == "database"
        for alias in no.names
    }
    assert do_banco, "nenhum import de `database` encontrado — teste desatualizado"

    em_thread, diretas = set(), []
    for no in ast.walk(arvore):
        if not isinstance(no, ast.Call):
            continue
        nome = getattr(no.func, "attr", None) or getattr(no.func, "id", None)
        if nome == "to_thread":
            em_thread |= {a.lineno for a in no.args if isinstance(a, ast.Name)}
        if isinstance(no.func, ast.Name) and no.func.id in do_banco:
            diretas.append((no.func.id, no.lineno))

    sincronas = [f"{nome}() na linha {linha}"
                 for nome, linha in diretas if linha not in em_thread]
    assert not sincronas, (
        "chamadas de banco fora de asyncio.to_thread (bloqueiam o event loop): "
        + "; ".join(sincronas)
    )


# ══════════════════════════════════════════════════════════════════════════════
# Envio de comandos: "em paralelo" era só o comentário
# ══════════════════════════════════════════════════════════════════════════════
#
# Os comandos de carregamento e de scan saíam num laço SEQUENCIAL com `await`,
# sob um comentário que dizia "em paralelo". Isoladamente isso seria só lento;
# o que o torna um bug é o relógio do lado de lá.
#
# `_post` retenta 3x com `sleep(1)` e timeout de 10s — até ~32s por comando com
# o adapter fora do ar. Com a célula cheia (8 slots), o comando do ÚLTIMO slot
# só sairia ~4 min depois do primeiro, enquanto o `TIMEOUT_CARREGAMENTO` (180s)
# do PRIMEIRO já corre desde que o evento foi registrado. A OS abortava por
# "timeout de carregamento" de um dispenser que nunca tinha sido chamado — e o
# log apontava para o slot errado, porque o slot que estourou não é o lento.
#
# Os dois testes abaixo medem a concorrência de verdade (quantos comandos ficam
# em voo ao mesmo tempo), e não o tempo de parede: cronômetro em suíte de teste
# é flaky em máquina carregada, e o que está em jogo aqui é estrutural.


class _ContadorDeVoo:
    """Envolve `_post` contando quantos comandos ficam em voo simultaneamente.

    Cada chamada cede o controle ao event loop (`sleep(0)`) antes de responder:
    com `gather`, todas as corrotinas chegam ao `sleep` antes de a primeira
    voltar, e o pico bate no número de slots. Em laço sequencial o pico é 1,
    sempre — é exatamente essa a diferença que o teste precisa enxergar.
    """

    def __init__(self, adapter, recusa_sufixo: str | None = None):
        self.adapter = adapter
        self.recusa_sufixo = recusa_sufixo
        self.em_voo = 0
        self.pico_por_sufixo: dict[str, int] = {}

    async def post(self, url: str, payload: dict, timeout: float = 10.0) -> bool:
        sufixo = "/" + url.rsplit("/comandos/", 1)[-1]
        self.em_voo += 1
        self.pico_por_sufixo[sufixo] = max(self.pico_por_sufixo.get(sufixo, 0),
                                           self.em_voo)
        try:
            await asyncio.sleep(0)
            if self.recusa_sufixo and url.endswith(self.recusa_sufixo):
                self.adapter.chamadas.append({"url": url, "payload": payload})
                return False
            return await self.adapter.post(url, payload, timeout)
        finally:
            self.em_voo -= 1


def _os_da_celula_cheia(os_id: str = "OS-PAR") -> dict:
    """OS que usa TODOS os slots — é onde o laço sequencial dói."""
    return _payload_os(os_id, *[_item(f"Med{i}") for i in range(1, NUM_SLOTS + 1)])


def test_carregamento_sai_com_todos_os_comandos_em_voo(carregar_orquestrador,
                                                       monkeypatch):
    """Um `await` por slot, em fila, é o que faz o primeiro slot estourar."""
    orq = carregar_orquestrador()
    contador = _ContadorDeVoo(orq.adapter)
    monkeypatch.setattr(orq.modulo, "_post", contador.post)

    asyncio.run(orq.modulo._processar_os(_os_da_celula_cheia()))

    assert contador.pico_por_sufixo["/carregar"] == NUM_SLOTS


def test_scan_de_visao_sai_com_todos_os_comandos_em_voo(carregar_orquestrador,
                                                        monkeypatch):
    """O scan tem o mesmo laço e o mesmo relógio — e o mesmo conserto."""
    orq = carregar_orquestrador()
    contador = _ContadorDeVoo(orq.adapter)
    monkeypatch.setattr(orq.modulo, "_post", contador.post)

    asyncio.run(orq.modulo._processar_os(_os_da_celula_cheia()))

    assert contador.pico_por_sufixo["/capturar/dispenser"] == NUM_SLOTS


def test_a_cnc_continua_sequencial(carregar_orquestrador, monkeypatch):
    """O paralelismo é do carregamento, NÃO do ciclo de dispensa.

    A mesa é uma só: paralelizar `mover` mandaria a CNC para dois slots ao
    mesmo tempo. Este teste existe para que um "otimizar igual ao de cima"
    futuro não passe calado.
    """
    orq = carregar_orquestrador()
    contador = _ContadorDeVoo(orq.adapter)
    monkeypatch.setattr(orq.modulo, "_post", contador.post)

    asyncio.run(orq.modulo._processar_os(_os_da_celula_cheia()))

    for sufixo in ("/mover", "/dispensar", "/pesar", "/capturar/mesa"):
        assert contador.pico_por_sufixo[sufixo] == 1, sufixo


def test_envio_de_carregamento_recusado_aborta_sem_esperar_o_timeout(
    carregar_orquestrador
):
    """`ok=False` já é a resposta: esperar 180s por ela não acrescenta nada.

    O timeout fica no valor de produção de propósito — se o abort dependesse
    dele, o teste levaria três minutos em vez de falhar.
    """
    orq = carregar_orquestrador()
    assert orq.modulo.settings.TIMEOUT_CARREGAMENTO >= 60   # o valor real, não um encurtado
    orq.adapter.aceita = False

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-11", _item("Dipirona"))))

    assert _status_gravados(orq) == [("OS-11", "em_andamento"), ("OS-11", "erro")]
    assert [c["args"][1] for c in orq.banco.chamadas_de("salvar_alarme")][0] == \
        "erro_envio_carregamento"
    # Nenhum evento da OS morta sobrou para confundir a próxima.
    assert orq.modulo._pending_events == {}


def test_scan_recusado_nao_aborta_a_os_mas_tambem_nao_espera(carregar_orquestrador,
                                                             monkeypatch):
    """Câmera é fonte que deixou de confirmar, não fonte que contradisse.

    Por isso o envio recusado aqui NÃO derruba a OS — a regra é a mesma de
    `leitura_dispenser_falha`. O que muda é a espera: sem o comando na planta o
    evento nunca chega, então aguardá-lo seria queimar TIMEOUT_VISAO_DISPENSER
    inteiro por um resultado que já se sabe inexistente.

    A ausência da espera é afirmada pelas CHAVES aguardadas, e não pelo relógio:
    cronômetro em suíte de teste é flaky, e "demorou menos" não diz qual espera
    sumiu.
    """
    orq = carregar_orquestrador()
    assert orq.modulo.settings.TIMEOUT_VISAO_DISPENSER >= 10
    contador = _ContadorDeVoo(orq.adapter, recusa_sufixo="/capturar/dispenser")
    monkeypatch.setattr(orq.modulo, "_post", contador.post)

    aguardadas: list[str] = []
    aguardar_real = orq.modulo.aguardar_evento

    async def _espiar(chave, timeout):
        aguardadas.append(chave)
        return await aguardar_real(chave, timeout)

    monkeypatch.setattr(orq.modulo, "aguardar_evento", _espiar)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-12", _item("Dipirona"))))

    assert _status_gravados(orq) == [("OS-12", "em_andamento"), ("OS-12", "concluida")]
    assert not [c for c in aguardadas if "visao_dispenser" in c], (
        "esperou um scan que o vision-adapter recusou enviar"
    )
    # O restante do ciclo segue aguardando normalmente — a exceção é o scan.
    assert [c for c in aguardadas if "dispensado" in c]
    assert orq.modulo._pending_events == {}


# ══════════════════════════════════════════════════════════════════════════════
# _abortar_os toca só nos slots DESTA OS
# ══════════════════════════════════════════════════════════════════════════════
#
# O abort varria `_estado["dispensers"]` inteiro, zerando `status` e `os_id` de
# TODO slot — inclusive os que guardam resíduo de outra OS. O caminho de
# sucesso sempre iterou sobre `atribuicoes`; era o caminho de erro que
# generalizava.
#
# `os_id` num slot é quem diz de quem é o medicamento parado ali. Apagado, o
# resíduo vira órfão sem dono aparente: o painel mostra o slot como idle e o
# `atribuir_slots` seguinte o toma por reaproveitável sem passar pela limpeza
# que o `precisa_limpeza` obrigaria.
#
# Os testes antigos não pegavam isso porque encenavam uma OS por vez: os slots
# que o abort não devia tocar já estavam idle, e "não mexeu" era
# indistinguível de "mexeu para o mesmo valor". Aqui há SEMPRE um slot de outra
# OS na bancada.

def _slot_de_outra_os(orq, slot_id: int, os_id: str = "OS-VIZINHA") -> None:
    """Resíduo que pertence a outra OS — o que o abort não pode reivindicar."""
    orq.slot(slot_id).update({
        "status": "carregado", "os_id": os_id,
        "medicamento": "Vizinho", "quantidade": 7,
    })


def test_abort_nao_mexe_em_slot_de_outra_os(carregar_orquestrador):
    orq = carregar_orquestrador()
    _ocupar(orq, 2, 5)
    _slot_de_outra_os(orq, 8)

    asyncio.run(orq.modulo._abortar_os("OS-42", "erro_cnc", _atribuicoes(2, 5)))

    assert orq.slot(8)["os_id"] == "OS-VIZINHA"
    assert orq.slot(8)["status"] == "carregado"
    assert orq.slot(8)["medicamento"] == "Vizinho"


def test_abort_nao_manda_limpar_slot_de_outra_os(carregar_orquestrador):
    """A limpeza já era por atribuição; o reset da memória é que não era.

    Vale afirmar os dois juntos: um abort que descarta o estoque certo mas
    apaga o dono do slot errado produz a MESMA divergência entre bancada e
    painel, só que pelo lado da memória.
    """
    orq = carregar_orquestrador()
    _ocupar(orq, 2, 5)
    _slot_de_outra_os(orq, 8)

    asyncio.run(orq.modulo._abortar_os("OS-42", "erro_cnc", _atribuicoes(2, 5)))

    limpos = [c["dispenser_id"] for c in orq.adapter.comandos("/comandos/limpar")]
    assert limpos == [2, 5]


def test_abort_e_sucesso_resetam_exatamente_o_mesmo_conjunto(carregar_orquestrador):
    """As duas saídas da OS deixam a bancada no mesmo estado.

    Se divergirem, a diferença aparece só na OS seguinte — e como um 409 de
    limpeza, que não aponta para o abort que o causou.
    """
    def _bancada_depois(fechamento) -> dict:
        orq = carregar_orquestrador()
        _slot_de_outra_os(orq, NUM_SLOTS)
        asyncio.run(fechamento(orq))
        return {
            str(i): (orq.slot(i)["status"], orq.slot(i)["os_id"])
            for i in range(1, NUM_SLOTS + 1)
        }

    async def _sucesso(orq):
        await orq.modulo._processar_os(_payload_os("OS-13", _item("Dipirona")))

    async def _abort(orq):
        _ocupar(orq, 1)
        await orq.modulo._abortar_os("OS-13", "erro_cnc", _atribuicoes(1))

    assert _bancada_depois(_abort) == _bancada_depois(_sucesso)


# ══════════════════════════════════════════════════════════════════════════════
# Exceção não tratada é um abort — não meio abort
# ══════════════════════════════════════════════════════════════════════════════
#
# O `except` do `loop_orquestrador` gravava "erro" e zerava `os_ativa`, e parava
# aí. Três rastros ficavam para trás, e nenhum deles dá erro no momento:
#
#   * o medicamento carregado segue FISICAMENTE no dispenser — ninguém mandou
#     limpar;
#   * os slots guardam o `os_id` da OS morta;
#   * as chaves `{os_id}:...` seguem em `_pending_events`.
#
# O sintoma nasce na OS SEGUINTE, longe daqui: a etapa 1b manda limpar o slot
# que ninguém liberou e toma 409. `_abortar_os` já faz as três coisas certas —
# a correção é chamá-lo.

def _rodar_pelo_loop(orq, *etapas) -> None:
    """Roda N OS pelo `loop_orquestrador`, que em produção é infinito.

    O caminho importa: é o `loop_orquestrador` que tem o `except`, e chamar
    `_processar_os` direto (como o resto do arquivo faz) passaria ao lado
    justamente do bloco em teste.

    Tudo num `asyncio.run()` só, e não um por OS: `_os_queue` é global do
    módulo e prende seus `Event` internos ao primeiro loop que os tocar — um
    segundo `asyncio.run()` sobre a mesma fila estoura em "bound to a different
    event loop", que é falha do aparato de teste e não do código.

    Cada etapa é um payload de OS ou um callable, executado entre as OS (é
    assim que o teste da OS seguinte devolve a CNC ao normal).
    """
    async def _cenario():
        tarefa = asyncio.ensure_future(orq.modulo.loop_orquestrador())
        try:
            for etapa in etapas:
                if callable(etapa):
                    etapa()
                    continue
                assert await orq.modulo.enfileirar_os(etapa)
                await orq.modulo._os_queue.join()
        finally:
            tarefa.cancel()
            try:
                await tarefa
            except asyncio.CancelledError:
                pass

    asyncio.run(_cenario())


@pytest.fixture
def os_que_explode(carregar_orquestrador, monkeypatch):
    """OS que estoura DEPOIS do carregamento — é onde há estoque a perder.

    `cmd_mover` é o primeiro comando do ciclo da CNC (etapa 4): quando ele
    levanta, os dispensers já receberam a carga e os slots já estão marcados
    com o `os_id`. Explodir antes disso testaria o caso fácil, em que não há
    nada preso na bancada.
    """
    orq = carregar_orquestrador()

    async def _explode(*_a, **_kw):
        raise RuntimeError("falha inesperada no adapter da CNC")

    orq.cmd_mover_real = orq.modulo.cmd_mover      # para a OS seguinte voltar ao normal
    monkeypatch.setattr(orq.modulo, "cmd_mover", _explode)
    return orq


def test_excecao_nao_tratada_descarta_o_estoque_dos_slots(os_que_explode):
    """Sem a limpeza, o slot sai de circulação e ninguém fica sabendo."""
    orq = os_que_explode

    _rodar_pelo_loop(orq, _payload_os("OS-BOOM", _item("Dipirona"),
                                             _item("Paracetamol")))

    limpos = sorted(c["dispenser_id"]
                    for c in orq.adapter.comandos("/comandos/limpar"))
    assert limpos == [1, 2]


def test_excecao_nao_tratada_limpa_os_eventos_da_os_morta(os_que_explode):
    """Chave pendente de OS morta é notificação cruzada esperando acontecer."""
    orq = os_que_explode

    _rodar_pelo_loop(orq, _payload_os("OS-BOOM", _item("Dipirona")))

    assert orq.modulo._pending_events == {}


def test_excecao_nao_tratada_solta_o_os_id_dos_slots(os_que_explode):
    orq = os_que_explode

    _rodar_pelo_loop(orq, _payload_os("OS-BOOM", _item("Dipirona")))

    assert orq.estado["os_ativa"] is None
    assert orq.estado["atribuicao_ia"] == []
    assert orq.slot(1)["os_id"] is None
    assert orq.slot(1)["status"] == "idle"


def test_excecao_nao_tratada_fecha_a_os_em_erro_com_alarme(os_que_explode):
    """O status terminal já era gravado; o alarme nomeando a causa, não."""
    orq = os_que_explode

    _rodar_pelo_loop(orq, _payload_os("OS-BOOM", _item("Dipirona")))

    assert _status_gravados(orq)[-1] == ("OS-BOOM", "erro")
    assert "excecao_nao_tratada" in [
        c["args"][1] for c in orq.banco.chamadas_de("salvar_alarme")
    ]


def test_a_os_seguinte_comeca_com_a_bancada_limpa(os_que_explode, monkeypatch):
    """O sintoma real: os rastros só doem na OS SEGUINTE, longe da exceção.

    O que a OS seguinte encontrava no `except` antigo era um slot ainda marcado
    com o `os_id` da OS morta e as chaves `{os_id}:...` vivas em
    `_pending_events`. A verificação acontece ENTRE as duas OS, e não depois das
    duas: a segunda OS completa não distingue os dois mundos — ela conclui do
    mesmo jeito, e é justamente essa a razão de o bug ser silencioso.

    O loop também tem que sobreviver à exceção: uma OS que o derrubasse pararia
    a planta inteira, porque o orquestrador é um loop único.
    """
    orq = os_que_explode
    entre: dict = {}

    def _conferir_e_consertar_a_cnc():
        entre["eventos"] = dict(orq.modulo._pending_events)
        entre["slot_os_id"] = orq.slot(1)["os_id"]
        entre["slot_status"] = orq.slot(1)["status"]
        orq.banco.limpar_chamadas()
        orq.adapter.chamadas.clear()
        monkeypatch.setattr(orq.modulo, "cmd_mover", orq.cmd_mover_real)

    _rodar_pelo_loop(
        orq,
        _payload_os("OS-BOOM", _item("Dipirona")),
        _conferir_e_consertar_a_cnc,
        _payload_os("OS-DEPOIS", _item("Dipirona")),
    )

    assert entre["eventos"] == {}, "eventos da OS morta sobreviveram ao except"
    assert entre["slot_os_id"] is None
    assert entre["slot_status"] == "idle"
    assert _status_gravados(orq) == [("OS-DEPOIS", "em_andamento"),
                                     ("OS-DEPOIS", "concluida")]


# ══════════════════════════════════════════════════════════════════════════════
# `_post`: retry só onde tentar de novo pode dar outro resultado
# ══════════════════════════════════════════════════════════════════════════════
#
# `_post` retentava QUALQUER status >= 300 — três tentativas, `sleep(1)` entre
# elas. Para falha de rede e 5xx isso é exatamente o certo. Para 409 e 422 é
# desperdício puro: a mesma requisição vai receber a mesma resposta, e o
# preço são 2s por recusa.
#
# O caso concreto é o 409 `limpeza_em_operacao` do dispenser-simulator, e ele
# aparece justamente onde dói: a etapa 3 do `_processar_os` dispara os comandos
# de todos os slots em `gather`, e cada recusa determinística segurava um slot
# por 2s a mais enquanto o `TIMEOUT_CARREGAMENTO` do PRIMEIRO já corria — a
# mesma forma do bug que "Comando a todos os slots sai em `gather`" registra.

class _RespostaHTTP:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = ""


class _ClienteHTTPFake:
    """Duplo do `httpx.AsyncClient` que o orquestrador guarda em `_client`.

    Cada item de `respostas` é um status (`int`) ou uma exceção a levantar;
    esgotada a lista, o último item se repete.
    """

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    async def post(self, url, json=None, timeout=None):
        self.chamadas.append({"url": url, "json": json})
        item = (self.respostas[len(self.chamadas) - 1]
                if len(self.chamadas) <= len(self.respostas)
                else self.respostas[-1])
        if isinstance(item, BaseException):
            raise item
        return _RespostaHTTP(item)


def _postar(orq, monkeypatch, respostas):
    """Roda o `_post` DE VERDADE (não o duplo do adapter) sobre um cliente fake."""
    cliente = _ClienteHTTPFake(respostas)
    monkeypatch.setattr(orq.modulo, "_client", cliente)

    async def _sem_espera(_s):
        return None

    monkeypatch.setattr(orq.modulo.asyncio, "sleep", _sem_espera)
    ok = asyncio.run(orq.post_real("http://adapter/comandos/limpar", {"dispenser_id": 1}))
    return ok, cliente


@pytest.mark.parametrize("status", [400, 404, 405, 409, 422])
def test_recusa_deterministica_nao_e_retentada(carregar_orquestrador, monkeypatch,
                                               status):
    """O 409 de limpeza em curso continuará sendo 409 nos próximos 2 segundos.

    A linha é o 500, e não uma lista de códigos: 5xx inteiro é "o lado de lá
    falhou", inclusive um 501 de rota que o adapter não implementa. Retentar
    esse é barato e a lista curta é o que mantém a regra igual dos dois lados
    da ponte.
    """
    orq = carregar_orquestrador()

    ok, cliente = _postar(orq, monkeypatch, [status])

    assert ok is False
    assert len(cliente.chamadas) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408, 429])
def test_falha_transitoria_continua_sendo_retentada(carregar_orquestrador,
                                                    monkeypatch, status):
    """O que o retry existe para cobrir: adapter subindo, saturado ou caído."""
    orq = carregar_orquestrador()

    ok, cliente = _postar(orq, monkeypatch, [status, status, 200])

    assert ok is True
    assert len(cliente.chamadas) == 3


def test_falha_de_rede_continua_sendo_retentada(carregar_orquestrador, monkeypatch):
    """`connection refused` no primeiro ciclo depois de um `up` é o motivo de o
    retry existir — ver CLAUDE.md, "A ordem de subida é do compose"."""
    orq = carregar_orquestrador()

    ok, cliente = _postar(orq, monkeypatch,
                          [ConnectionError("connection refused"), 200])

    assert ok is True
    assert len(cliente.chamadas) == 2


def test_teto_de_tentativas_continua_valendo(carregar_orquestrador, monkeypatch):
    orq = carregar_orquestrador()

    ok, cliente = _postar(orq, monkeypatch, [TimeoutError("timed out")])

    assert ok is False
    assert len(cliente.chamadas) == orq.modulo._TENTATIVAS_POST


def test_sucesso_de_primeira_nao_dorme(carregar_orquestrador, monkeypatch):
    orq = carregar_orquestrador()
    dormidas = []

    async def _contar(segundos):
        dormidas.append(segundos)

    cliente = _ClienteHTTPFake([200])
    monkeypatch.setattr(orq.modulo, "_client", cliente)
    monkeypatch.setattr(orq.modulo.asyncio, "sleep", _contar)

    ok = asyncio.run(orq.post_real("http://adapter/comandos/dispensar", {}))

    assert ok is True
    assert dormidas == []


def test_recusa_deterministica_tambem_nao_dorme(carregar_orquestrador, monkeypatch):
    """O ganho medido: a recusa sai na hora, e não depois de 2s de `sleep`.

    O teste conta as ESPERAS, não o tempo de parede: cronômetro em suíte é
    flaky, e "demorou menos" não diz qual espera sumiu.
    """
    orq = carregar_orquestrador()
    dormidas = []

    async def _contar(segundos):
        dormidas.append(segundos)

    cliente = _ClienteHTTPFake([409])
    monkeypatch.setattr(orq.modulo, "_client", cliente)
    monkeypatch.setattr(orq.modulo.asyncio, "sleep", _contar)

    asyncio.run(orq.post_real("http://adapter/comandos/limpar", {}))

    assert dormidas == []


def test_criterio_e_uma_funcao_nomeada(carregar_orquestrador):
    """A regra mora em `_vale_retentar`, e não num `if` dentro do laço: é o que
    permite comparar o critério com o dos adapters (`tests/test_adapters.py`)
    em vez de confiar que as duas cópias dizem a mesma coisa."""
    orq = carregar_orquestrador()

    assert orq.modulo._vale_retentar(503) is True
    assert orq.modulo._vale_retentar(409) is False


# ══════════════════════════════════════════════════════════════════════════════
# O central avisa a célula que travou — as 8 telas TFT, pelo dispenser-adapter
# ══════════════════════════════════════════════════════════════════════════════
#
# `POST /comandos/estado-celula` sai ao ativar E ao liberar a trava (e no reset),
# com `trava_resumo` derivado da CATEGORIA da divergência — nunca do texto do
# motivo. UMA vez, com timeout curto, sem `_post`: uma placa de telas fora do ar
# não pode acrescentar ~32 s ao caminho da trava. E falha aqui nunca muda o
# fluxo: não aborta, não entra no veredito, não atrasa o broadcast.

class _RespostaTelas:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def json(self):
        return {"ok": True, "telas": "ok"}


class _TelasFake:
    """Duplo do `_client` do orquestrador só para o aviso às telas."""

    def __init__(self, status: int = 200, atraso: float = 0.0, explode: bool = False):
        self.status = status
        self.atraso = atraso
        self.explode = explode
        self.chamadas: list[dict] = []

    async def post(self, url, json=None, timeout=None):
        self.chamadas.append({"url": url, "json": json, "timeout": timeout})
        if self.explode:
            raise ConnectionError("connection refused")
        if self.atraso:
            await asyncio.sleep(self.atraso)
        return _RespostaTelas(self.status)

    def avisos(self) -> list[dict]:
        return [c["json"] for c in self.chamadas if c["url"].endswith("/comandos/estado-celula")]


def _com_telas(orq, **kw) -> _TelasFake:
    telas = _TelasFake(**kw)
    orq.modulo._client = telas
    return telas


async def _deixar_o_aviso_sair():
    """O aviso é agendado, não esperado: um tique do loop basta para ele sair."""
    await asyncio.sleep(0.05)


# `MESA_DIVERGENTE` e `PESO_DIVERGENTE` são os do bloco do Triple Check, acima.


def test_ativar_trava_avisa_as_telas_com_slot_e_resumo(carregar_orquestrador):
    orq = carregar_orquestrador()
    telas = _com_telas(orq)

    async def _cenario():
        await orq.modulo._ativar_trava("OS-1", 3, "Triple Check FALHOU (1/3 ...) — D3: ...",
                                       resumo="divergência de peso")
        await _deixar_o_aviso_sair()

    asyncio.run(_cenario())

    (aviso,) = telas.avisos()
    assert aviso == {"trava_ativa": True, "trava_slot_id": 3, "os_id": "OS-1",
                     "trava_resumo": "divergência de peso"}
    assert telas.chamadas[0]["timeout"] == orq.modulo.TIMEOUT_AVISO_TELAS_S


def test_liberar_trava_avisa_as_telas_que_a_trava_saiu(carregar_orquestrador):
    orq = carregar_orquestrador()
    telas = _com_telas(orq)

    async def _cenario():
        await orq.modulo._ativar_trava("OS-1", 3, "motivo", resumo="SKU errado")
        await _deixar_o_aviso_sair()
        assert orq.modulo.liberar_trava("supervisor") is True
        await _deixar_o_aviso_sair()

    asyncio.run(_cenario())

    assert [a["trava_ativa"] for a in telas.avisos()] == [True, False]
    assert telas.avisos()[-1] == {"trava_ativa": False, "trava_slot_id": None,
                                  "os_id": "", "trava_resumo": ""}


def test_reset_da_planta_avisa_as_telas(carregar_orquestrador):
    orq = carregar_orquestrador()
    telas = _com_telas(orq)

    async def _cenario():
        await orq.modulo._ativar_trava("OS-1", 3, "motivo", resumo="SKU errado")
        await _deixar_o_aviso_sair()
        await orq.modulo.resetar_planta()

    asyncio.run(_cenario())

    assert telas.avisos()[-1]["trava_ativa"] is False
    assert orq.modulo.get_trava_estado()["ativa"] is False


# ── O resumo vem da categoria, não do texto ──────────────────────────────────

def test_veredito_carrega_a_categoria_de_cada_divergencia(carregar_orquestrador):
    orq = carregar_orquestrador()
    veredito = orq.modulo.avaliar_triple_check(10, 9, MESA_DIVERGENTE, PESO_DIVERGENTE,
                                               min_divergencias=1)
    assert veredito.categorias == ("dispenser divergente", "contagem divergente",
                                   "divergência de peso")
    assert len(veredito.categorias) == len(veredito.divergencias)


def test_resumo_da_trava_e_a_primeira_categoria_no_teto_de_48(carregar_orquestrador):
    """Inclusive no veredito com 3 divergências, cujo motivo formatado passa
    de 100 caracteres: o resumo não é derivado dele."""
    orq = carregar_orquestrador()
    veredito = orq.modulo.avaliar_triple_check(10, 9, MESA_DIVERGENTE, PESO_DIVERGENTE,
                                               min_divergencias=1)
    motivo = "; ".join(veredito.divergencias)
    assert len(motivo) > 48

    resumo = orq.modulo.resumo_da_trava(veredito)

    assert resumo == "dispenser divergente"
    assert len(resumo) <= 48 == orq.modulo.TRAVA_RESUMO_MAX
    assert resumo not in motivo          # categoria, não recorte do texto

    so_balanca = orq.modulo.avaliar_triple_check(10, 10, None, PESO_DIVERGENTE,
                                                 min_divergencias=1)
    assert orq.modulo.resumo_da_trava(so_balanca) == "divergência de peso"


def test_o_aviso_nunca_passa_de_48_mesmo_com_resumo_maior(carregar_orquestrador):
    orq = carregar_orquestrador()
    telas = _com_telas(orq)

    async def _cenario():
        await orq.modulo._ativar_trava("OS-1", 3, "motivo", resumo="x" * 200)
        await _deixar_o_aviso_sair()

    asyncio.run(_cenario())
    assert len(telas.avisos()[0]["trava_resumo"]) == 48


def test_a_os_travada_pelo_triple_check_manda_a_categoria_as_telas(carregar_orquestrador,
                                                                     monkeypatch):
    orq = carregar_orquestrador()
    telas = _com_telas(orq)
    orq.adapter.quantidade_dispensada = 9          # dispenser divergente → trava

    def _liberar_quando_aparecer():
        if orq.estado["trava"]["ativa"]:
            orq.modulo.liberar_trava("supervisor")

    monkeypatch.setattr(orq.modulo, "_broadcast_fn", _liberar_quando_aparecer)

    async def _cenario():
        await orq.modulo._processar_os(_payload_os("OS-T", _item("Dipirona", 10)))
        await _deixar_o_aviso_sair()

    asyncio.run(_cenario())

    ativacoes = [a for a in telas.avisos() if a["trava_ativa"]]
    assert ativacoes and ativacoes[0]["trava_resumo"] == "dispenser divergente"
    assert ativacoes[0]["trava_slot_id"] == 1
    assert telas.avisos()[-1]["trava_ativa"] is False


# ── Adapter fora do ar não atrasa nem derruba a ativação ────────────────────

@pytest.mark.parametrize("telas_kw", [dict(explode=True), dict(atraso=2.0), dict(status=503)])
def test_adapter_fora_do_ar_nao_atrasa_nem_derruba_a_ativacao(carregar_orquestrador,
                                                              telas_kw):
    """A ativação tem que continuar na casa de milissegundos — o aviso corre em
    paralelo, e uma exceção dele nunca chega ao orquestrador."""
    orq = carregar_orquestrador()
    telas = _com_telas(orq, **telas_kw)
    duracao = {}

    async def _cenario():
        inicio = time.monotonic()
        await orq.modulo._ativar_trava("OS-1", 3, "motivo", resumo="SKU errado")
        duracao["ativacao"] = time.monotonic() - inicio
        await _deixar_o_aviso_sair()

    asyncio.run(_cenario())

    assert duracao["ativacao"] < 0.2, f"ativação levou {duracao['ativacao']:.3f}s"
    assert orq.modulo.get_trava_estado()["ativa"] is True
    assert orq.estado["trava"]["ativa"] is True
    assert len(telas.chamadas) == 1, "o aviso saiu mais de uma vez — retentou"


def test_o_aviso_nao_usa_o_post_com_retry(carregar_orquestrador):
    """`_post` retenta 3× com sleep(1) e timeout de 10 s: ~32 s a mais no
    caminho da trava com a placa fora. O aviso tem timeout próprio e curto."""
    import inspect

    orq = carregar_orquestrador()
    fonte = inspect.getsource(orq.modulo._avisar_telas)
    assert "_post(" not in fonte
    assert orq.modulo.TIMEOUT_AVISO_TELAS_S < 10.0


def test_o_triple_check_decide_igual_com_e_sem_o_aviso(carregar_orquestrador, monkeypatch):
    """O veredito é puro e o aviso é cosmético: a OS termina do mesmo jeito com
    o adapter respondendo, explodindo, ou sem cliente nenhum."""
    def _rodar(telas_kw):
        orq = carregar_orquestrador()
        if telas_kw is not None:
            _com_telas(orq, **telas_kw)
        orq.adapter.quantidade_dispensada = 9

        def _liberar_quando_aparecer():
            if orq.estado["trava"]["ativa"]:
                orq.modulo.liberar_trava("supervisor")

        monkeypatch.setattr(orq.modulo, "_broadcast_fn", _liberar_quando_aparecer)
        asyncio.run(orq.modulo._processar_os(_payload_os("OS-T", _item("Dipirona", 10))))
        veredito = orq.modulo.avaliar_triple_check(10, 9, None, None, min_divergencias=1)
        return _status_gravados(orq), veredito.travar, veredito.divergencias

    sem_cliente = _rodar(None)
    respondendo = _rodar(dict())
    explodindo = _rodar(dict(explode=True))

    assert sem_cliente == respondendo == explodindo
    assert sem_cliente[0][-1] == ("OS-T", "concluida")
    assert sem_cliente[1] is True
