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
# "em_andamento" existia no enum, na IHM e no dashboard, mas NUNCA era gravado:
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
    """O motivo da trava vai para a IHM — precisa dizer o que divergiu e quanto."""
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


def test_ativar_trava_publica_o_motivo_para_a_ihm(carregar_orquestrador):
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
