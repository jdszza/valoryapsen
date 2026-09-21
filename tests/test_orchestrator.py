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
import collections
import itertools
import logging
import time

import pytest

from conftest import NUM_SLOTS, SLOTS_POR_FILEIRA, TEMPLATE_PADRAO


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
    # `strict=True`: as duas fileiras TÊM o mesmo tamanho (NUM_SLOTS é par,
    # e `_num_slots` recusa ímpar). Um zip que truncasse em silêncio faria
    # este teste aprovar uma bancada com uma fileira mais curta que a outra.
    for frente, fundo in zip(esquerda, direita, strict=True):
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


def test_cnc_recebe_a_receita_e_NAO_recebe_coordenada(carregar_orquestrador):
    """Substitui `test_cnc_recebe_a_posicao_do_mapa_do_central`.

    A decisão virou: a mesa executa o roteiro que foi gravado nela, indexado
    pelo DISPENSER, e o comando leva a LETRA da receita. Mandar x/y junto seria
    mandar um número que a placa ignora — e número ignorado que continua sendo
    gravado no banco é pior que nenhum, porque ninguém descobre que ele não
    descreve a máquina.
    """
    orq = carregar_orquestrador()
    alvo = SLOTS_POR_FILEIRA + 1   # primeiro slot da fileira de trás

    asyncio.run(orq.modulo.cmd_mover(alvo, "OS-1", "C", 1, 1))

    (comando,) = orq.adapter.comandos("/comandos/mover")
    assert comando["receita"] == "C"
    assert comando["dispenser_alvo"] == alvo
    assert "posicao_x" not in comando and "posicao_y" not in comando


def test_homing_manda_so_o_os_id(carregar_orquestrador):
    """Substitui `test_homing_tambem_leva_as_coordenadas`.

    O HOME da máquina é o zero que o homing dela estabelece contra os fins de
    curso. Um par de coordenadas vindo do central seria um SEGUNDO home, e os
    dois concordariam só enquanto ninguém mexesse na mesa — depois de uma
    remontagem, o central mandaria a mesa para um ponto que deixou de ser o
    zero, e nada acusaria a diferença.
    """
    orq = carregar_orquestrador()

    asyncio.run(orq.modulo.cmd_homing("OS-1"))

    (comando,) = orq.adapter.comandos("/comandos/homing")
    assert comando == {"os_id": "OS-1"}


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
    # `template_id` é o que diz QUAL das dez ordens padrão esta é, e é dele que
    # o orquestrador tira a letra da receita gravada na mesa. Sem ele a OS é
    # legitimamente abortada com `receita_nao_mapeada` antes do primeiro mover
    # — o caso tem teste próprio; aqui a OS é uma ordem padrão de verdade.
    return {
        "os_id":        os_id,
        "descricao":    "teste",
        "template_id":  TEMPLATE_PADRAO,
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
    #
    # O controle era a chave `dispensado`, e ele descrevia o modelo ANTERIOR: o
    # ciclo da mesa passou a correr por RELÓGIO, então `posicionado` e
    # `dispensado` não são mais aguardados — são COLHIDOS depois do prazo (ver
    # `espiar_evento`/`colher_evento`). Trocado por `peso`, que continua sendo
    # handshake de verdade: sem um controle, "não esperou o scan" ficaria
    # indistinguível de "não esperou nada".
    assert [c for c in aguardadas if "peso" in c], (
        "nenhuma espera sobrou no ciclo — o controle deixou de controlar"
    )
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

    def avisos(self, placa: str = "") -> list[dict]:
        """Os avisos de trava, opcionalmente de UMA placa.

        A trava passou a avisar DUAS placas por transição — as telas TFT e a
        mesa. Sem o filtro, `[True, False]` viraria `[True, True, False, False]`
        e toda asserção sobre a ORDEM das transições passaria a medir também
        quantos destinos existem, que é outra pergunta.
        """
        return [c["json"] for c in self.chamadas
                if c["url"].endswith("/comandos/estado-celula") and placa in c["url"]]


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

    # O MESMO payload vai para as duas placas: as telas mostram de qual slot é
    # a divergência, e a mesa para de andar. Traduzir de um lado seria onde os
    # dois passariam a divergir depois.
    for placa in ("dispenser", "cnc"):
        (aviso,) = telas.avisos(placa)
        assert aviso == {"trava_ativa": True, "trava_slot_id": 3, "os_id": "OS-1",
                         "trava_resumo": "divergência de peso"}, placa
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

    # Por placa, e não no bolo: a ORDEM das transições é o que este teste mede.
    for placa in ("dispenser", "cnc"):
        assert [a["trava_ativa"] for a in telas.avisos(placa)] == [True, False], placa
        assert telas.avisos(placa)[-1] == {"trava_ativa": False, "trava_slot_id": None,
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
    # UMA tentativa POR PLACA. São duas placas desde que a mesa passou a ser
    # avisada, então contar chamadas no bolo deixou de distinguir "avisou os
    # dois destinos" de "retentou o mesmo" — que é o que este teste mede.
    por_url = collections.Counter(c["url"] for c in telas.chamadas)
    assert set(por_url.values()) == {1}, f"o aviso saiu mais de uma vez: {por_url}"
    assert len(por_url) == 2, f"nem todas as placas foram avisadas: {por_url}"


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


# ══════════════════════════════════════════════════════════════════════════════
# A POSIÇÃO É MEDIDA PELA MÁQUINA — o central registra, não dita
# ══════════════════════════════════════════════════════════════════════════════

def test_camera_da_mesa_recebe_a_posicao_MEDIDA(carregar_orquestrador):
    """O evento `posicionado` traz onde a mesa parou, e é isso que vale.

    A placa falsa reporta uma bancada deliberadamente diferente de `POSICOES`
    (ver `_POSICAO_MEDIDA` no conftest). Se este teste passar com os números do
    MODELO, é porque o central voltou a ditar a geometria em vez de registrar a
    medição — e o sintoma em campo seria a câmera olhando para onde o modelo
    diz que o slot está, não para onde a máquina parou.
    """
    orq = carregar_orquestrador()

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-MED")))

    capturas = orq.adapter.comandos("/comandos/capturar/mesa")
    assert capturas, "nenhuma captura de mesa foi pedida"
    for captura in capturas:
        slot = captura["slot_id"]
        assert (captura["posicao_x"], captura["posicao_y"]) == (1000.0 + slot, 2000.0 + slot)
        assert (captura["posicao_x"], captura["posicao_y"]) != orq.modulo.POSICOES[slot]


def test_sem_posicao_no_evento_cai_no_modelo_E_avisa(carregar_orquestrador, caplog):
    """Fallback EXPLÍCITO, num lugar só, e com aviso.

    O que estava aqui antes era `.get("posicao_x", 0.0)`, e 0.0 é a ORIGEM da
    mesa: a câmera seria mandada olhar para o HOME e chamar aquilo de D5 —
    divergência de contagem num slot só, indistinguível de medicamento
    faltando. O modelo é um palpite defensável; o zero silencioso não é.
    """
    orq = carregar_orquestrador()
    orq.adapter.posicao_medida = False

    with caplog.at_level(logging.WARNING):
        asyncio.run(orq.modulo._processar_os(_payload_os("OS-SEM-POS")))

    capturas = orq.adapter.comandos("/comandos/capturar/mesa")
    assert capturas
    for captura in capturas:
        slot = captura["slot_id"]
        assert (captura["posicao_x"], captura["posicao_y"]) == orq.modulo.POSICOES[slot]

    assert any("MODELO" in r.message or "modelo" in r.message.lower()
               for r in caplog.records), "o fallback tem que aparecer no log"


# ══════════════════════════════════════════════════════════════════════════════
# ORDEM SEM RECEITA GRAVADA NA MESA
# ══════════════════════════════════════════════════════════════════════════════

def test_os_fora_das_dez_padrao_aborta_antes_do_primeiro_mover(carregar_orquestrador):
    """Descobrir isso pelo `receita_desconhecida` da placa custaria o ciclo todo.

    A mesa só executa roteiro gravado nela, e o que diz QUAL roteiro é o
    `template_id` da ordem. Uma OS criada fora das dez padrão não tem nenhum —
    e se a checagem viesse depois da atribuição, a OS teria carregado os
    dispensers, descartado resíduo e reservado slots para abortar no primeiro
    ciclo.
    """
    orq = carregar_orquestrador()
    payload = _payload_os("OS-AVULSA")
    payload.pop("template_id")

    asyncio.run(orq.modulo._processar_os(payload))

    assert orq.adapter.comandos("/comandos/mover") == []
    assert orq.adapter.comandos("/comandos/carregar") == []
    assert ("OS-AVULSA", "erro") in _status_gravados(orq)

    motivos = [c["args"][1] for c in orq.banco.chamadas_de("salvar_alarme")]
    assert any("receita_nao_mapeada" in str(m) for m in motivos), motivos


# ══════════════════════════════════════════════════════════════════════════════
# O CICLO POR RELÓGIO — o central agenda, a mesa avisa, ninguém espera o aviso
# ══════════════════════════════════════════════════════════════════════════════
#
# O que mudou de modelo: `posicionado` e `dispensado` deixaram de ser PORTÃO.
# O central calcula quando cada peça acontece e dispara na hora marcada; o
# evento REGISTRA (a posição medida, a contagem do dispenser) e, no máximo,
# CANCELA. Um evento que fizesse o central esperar recriaria o handshake por
# acidente — e é isso que quem mexer aqui depois vai querer fazer.
#
# Os testes abaixo prendem as duas metades: que o cronograma é previsível, e
# que a ausência de confirmação NÃO derruba a OS enquanto um `erro` explícito
# derruba.


class _RelogioFalso:
    """Substitui o prazo por um registro do prazo.

    Cronômetro em suíte é flaky, e "demorou o esperado" depende da máquina. O
    que interessa é QUANTO o orquestrador pediu para dormir, em que ordem — e
    isso é um número exato, não uma medição.

    Mantém a semântica de `_dormir_ou_cancelar`: devolve False (cancelado) se o
    Event já está armado, True se o prazo "venceu". Sem isso, um teste de
    cancelamento passaria por acidente.
    """

    def __init__(self):
        self.prazos: list[float] = []

    async def dormir(self, segundos: float, cancelar) -> bool:
        self.prazos.append(segundos)
        if cancelar.is_set():
            return False
        return True

    @property
    def total(self) -> float:
        return sum(self.prazos)


def _com_relogio_falso(orq, monkeypatch) -> _RelogioFalso:
    relogio = _RelogioFalso()
    monkeypatch.setattr(orq.modulo, "_dormir_ou_cancelar", relogio.dormir)
    return relogio


# ── cronograma_do_ciclo: função pura ─────────────────────────────────────────

@pytest.mark.parametrize("quantidade, dispensa_esperada", [(2, 3.0), (15, 16.0)])
def test_cronograma_do_ciclo_bate_com_o_dwell_gravado_na_mesa(
        carregar_orquestrador, quantidade, dispensa_esperada):
    """A dispensa é a MESMA conta do `WP()` do firmware: (qty + 1) × 1000 ms.

    As duas descrevem o mesmo mecanismo físico — um ciclo de servo por
    comprimido — visto de dois lugares, e têm de ser mudadas juntas. Se o servo
    real for mais lento e só o firmware for ajustado, o central segue cortando a
    dispensa no meio: a OS termina "completa" com menos comprimido no leito, que
    é o pior desfecho possível desta feature.

    Os valores são os de produção, pedidos explicitamente — o conftest zera o
    cronograma para o resto da suíte não dormir de verdade.
    """
    orq = carregar_orquestrador(env={
        "CNC_TETO_TRAJETO_S": "2.5", "CNC_MARGEM_CHEGADA_S": "0.75",
        "DISPENSA_S_POR_UNIDADE": "1.0", "DISPENSA_FOLGA_S": "1.0",
    })

    espera, dispensa = orq.modulo.cronograma_do_ciclo(quantidade)

    assert espera == pytest.approx(3.25)          # 2,5 de trajeto + 0,75 de margem
    assert dispensa == pytest.approx(dispensa_esperada)


def test_o_teto_de_trajeto_cobre_o_pior_percurso_da_celula(carregar_orquestrador):
    """2,5 s não é um número escolhido: é o pior trajeto MAIS folga.

    FEED 750 mm/min = 1000 passos/s; CoreXY max_p = max(|dx+dy|,|dx−dy|) × 80;
    HOME(0,0) → D8(7,18) = 25 × 80 = 2000 passos ≈ 2016 ms.

    E ele é um TETO, não uma cópia da geometria — precisa ser MAIOR que o
    trajeto real, não igual. É isso que o mantém fora da regra do mapa
    duplicado: um limite superior continua verdadeiro depois de alguém regravar
    um waypoint na bancada; uma cópia passaria a mentir.
    """
    orq = carregar_orquestrador(env={"CNC_TETO_TRAJETO_S": "2.5",
                                     "CNC_MARGEM_CHEGADA_S": "0.75"})

    pior_trajeto_s = 2000 / 1000.0            # passos ÷ passos por segundo
    espera, _ = orq.modulo.cronograma_do_ciclo(1)

    assert orq.modulo.settings.CNC_TETO_TRAJETO_S > pior_trajeto_s, (
        "o teto não cobre o pior trajeto: o `dispensar` sairia com a mesa em "
        "trânsito")
    assert espera > pior_trajeto_s


# ── O que não chegou não derruba a OS ────────────────────────────────────────

def test_ciclo_sem_nenhum_evento_nao_aborta_a_os(carregar_orquestrador, monkeypatch):
    """A mesa muda não é a mesa parada.

    Um evento perdido no encaminhamento — o `_post_central` do adapter desiste
    depois de 3 tentativas — chegaria aqui idêntico a "o hardware não fez nada".
    Tratar os dois como iguais aborta OS com a bancada intacta, que é
    exatamente o que o modelo por relógio existe para não fazer. A OS SEGUE, e
    quem decide é o Triple Check no fim, que já compara três fontes.
    """
    orq = carregar_orquestrador()
    # O adapter aceita os comandos e NÃO devolve evento nenhum da mesa nem do
    # dispenser: é o silêncio total do caminho de volta.
    orq.adapter.responder_cnc = False
    orq.adapter.responder_dispenser = False

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-CRON-1")))

    assert _status_gravados(orq) == [("OS-CRON-1", "em_andamento"),
                                     ("OS-CRON-1", "concluida")]


def test_sem_confirmacao_vira_fonte_indisponivel_e_nao_divergencia(carregar_orquestrador):
    """Fonte que não mediu ≠ fonte que divergiu — a regra que sustenta o limiar 1.

    É a pendência indo para a estrutura que a OS JÁ carrega para o Triple Check,
    e não para uma lista paralela que alguém teria de lembrar de consultar.
    Contá-la como divergência faria todo evento perdido virar trava; assumir o
    alvo faria a fonte 1 CONFIRMAR uma contagem que ninguém fez — o Triple Check
    viraria um double check sem que nada dissesse isso.
    """
    orq = carregar_orquestrador()

    veredito = orq.modulo.avaliar_triple_check(
        quantidade_esperada=10, quantidade_dispensada=None,
        resultado_mesa=None, resultado_peso=None,
    )

    assert veredito.divergencias == []
    assert not veredito.travar
    assert any("dispenser" in f for f in veredito.fontes_indisponiveis)


def test_desfecho_de_cada_ciclo_tem_nome(carregar_orquestrador):
    """Os quatro desfechos, e cada um com nome no log e no estado publicado.

    Um ciclo que termina sem nome é um ciclo que ninguém audita depois — e no
    modelo por relógio a maior parte dos desfechos deixou de ser "abortou".
    """
    orq = carregar_orquestrador()
    m = orq.modulo

    disp = lambda q: {"tipo": "dispensado", "quantidade_dispensada": q}
    pos = {"tipo": "posicionado", "posicao_x": 1.0, "posicao_y": 2.0}

    assert m._desfecho_do_ciclo(1, 10, pos, disp(10)) == (m.DESFECHO_COMPLETO, 10)
    assert m._desfecho_do_ciclo(1, 10, pos, disp(8)) == (m.DESFECHO_CURTO, 8)
    assert m._desfecho_do_ciclo(1, 10, pos, None) == (m.DESFECHO_SEM_CONFIRMACAO, None)
    assert m._desfecho_do_ciclo(1, 10, pos, {"tipo": "erro"}) == (m.DESFECHO_ERRO, None)
    assert m._desfecho_do_ciclo(1, 10, {"tipo": "erro"}, disp(10)) == (m.DESFECHO_ERRO, None)


def test_posicionado_atrasado_nao_se_perde(carregar_orquestrador, monkeypatch):
    """O evento que chega DEPOIS de o `dispensar` já ter saído ainda é colhido.

    É o motivo de `espiar_evento` não usar `aguardar_evento(chave, 0)`: aquele
    desregistra a chave no `finally`, e `notificar_evento` DESCARTA evento de
    chave sem ninguém esperando. A espiada do veto acontece justamente enquanto
    o `posicionado` ainda está a caminho — desregistrar ali perderia a posição
    MEDIDA, que é o que a câmera da mesa usa para saber onde olhar.
    """
    orq = carregar_orquestrador()
    entregues: list[tuple] = []

    # A mesa só responde DEPOIS que o dispensar foi enviado: o `posicionado`
    # atravessa o adapter no meio da dispensa, tarde demais para o veto.
    orq.adapter.atrasar_posicionado = True

    post_anterior = orq.modulo._post

    async def _post(url, payload, timeout=10.0):
        ok = await post_anterior(url, payload, timeout)
        if url.endswith("/comandos/capturar/mesa"):
            entregues.append((payload.get("posicao_x"), payload.get("posicao_y")))
        return ok

    monkeypatch.setattr(orq.modulo, "_post", _post)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-CRON-2", _item("Dipirona"))))

    assert entregues, "a câmera da mesa não chegou a ser comandada"
    # A posição que foi para a câmera é a MEDIDA pela placa, não a do modelo.
    medida = orq.adapter.posicao_reportada
    assert entregues[0] == medida, (
        f"a câmera foi mandada para {entregues[0]} e a mesa reportou {medida} — "
        f"o evento atrasado se perdeu e o central caiu no modelo")


# ── O cronograma é previsível ────────────────────────────────────────────────

def test_o_tempo_de_uma_os_de_oito_paradas_e_a_soma_do_cronograma(
        carregar_orquestrador, monkeypatch):
    """Previsível é o ponto: é dele que sai a promessa de que o `dispensar`
    sai com a mesa parada.

    Medido com relógio FALSO — o que se compara é o prazo PEDIDO, não o tempo de
    parede. Cronômetro em suíte é flaky, e "demorou o esperado" mede sobretudo a
    máquina que rodou o teste.
    """
    itens = [_item(f"Med{i}", qtd=2 + i) for i in range(8)]
    orq = carregar_orquestrador(env={
        "CNC_TETO_TRAJETO_S": "2.5", "CNC_MARGEM_CHEGADA_S": "0.75",
        "DISPENSA_S_POR_UNIDADE": "1.0", "DISPENSA_FOLGA_S": "1.0",
    })
    relogio = _com_relogio_falso(orq, monkeypatch)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-CRON-3", *itens)))

    esperado = 0.0
    for item in itens:
        espera, dispensa = orq.modulo.cronograma_do_ciclo(item["quantidade"])
        esperado += espera + dispensa

    assert relogio.total == pytest.approx(esperado), {
        "prazos pedidos": relogio.prazos,
        "soma do cronograma": esperado,
    }
    # Duas pernas por parada: trajeto e dispensa. Uma perna a mais seria uma
    # espera que voltou ao ciclo sem ninguém notar.
    assert len(relogio.prazos) == 2 * len(itens)


# ── O veto: o evento não atrasa, mas cancela ─────────────────────────────────

def test_erro_da_mesa_impede_o_dispensar(carregar_orquestrador):
    """Sem o veto, o relógio despeja medicamento numa mesa que não chegou.

    É a ÚNICA coisa que o evento pode fazer com o cronograma além de registrar:
    cancelá-lo. Ele não pode ATRASÁ-LO — atrasar é o handshake de volta.
    """
    orq = carregar_orquestrador()
    orq.adapter.erro_cnc_em = 1        # a mesa emite `erro` ao receber o mover

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-CRON-4", _item("Dipirona"))))

    assert not orq.adapter.comandos("/comandos/dispensar"), (
        "dispensou com a mesa fora de posição")
    assert _status_gravados(orq)[-1] == ("OS-CRON-4", "erro")


def test_ack_negativo_no_mover_aborta_sem_dispensar(carregar_orquestrador, monkeypatch):
    """O ACK negativo da placa chega aqui como POST que falha (o adapter o
    transforma em 502), e é ele que cancela o agendamento antes da hora.

    No modelo por relógio esta é a única defesa que age ANTES do `dispensar` —
    daí o firmware recusar com ACK negativo em vez de só emitir evento.
    """
    orq = carregar_orquestrador()
    post_anterior = orq.modulo._post

    async def _post(url, payload, timeout=10.0):
        if url.endswith("/comandos/mover"):
            return False           # 502: a placa recusou o comando
        return await post_anterior(url, payload, timeout)

    monkeypatch.setattr(orq.modulo, "_post", _post)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-CRON-5", _item("Dipirona"))))

    assert not orq.adapter.comandos("/comandos/dispensar")
    assert _status_gravados(orq)[-1] == ("OS-CRON-5", "erro")


def test_ausencia_de_posicionado_nao_cancela_o_dispensar(carregar_orquestrador):
    """A diferença entre este modelo e o handshake, num teste.

    Quem mexer aqui depois vai querer "só esperar mais um pouquinho" pelo
    `posicionado`. Um evento perdido no encaminhamento não é a mesa parada, e
    tratar os dois como iguais aborta OS com hardware intacto. Só um `erro`
    EXPLÍCITO veta.
    """
    orq = carregar_orquestrador()
    orq.adapter.responder_cnc = False      # nenhum `posicionado`, e nenhum `erro`

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-CRON-6", _item("Dipirona"))))

    assert orq.adapter.comandos("/comandos/dispensar"), (
        "o dispensar não saiu por falta de um evento que nunca cancelou nada")
    assert _status_gravados(orq)[-1] == ("OS-CRON-6", "concluida")


def test_cancelamento_interrompe_o_prazo_em_curso(carregar_orquestrador):
    """Cancelar só o ciclo SEGUINTE não serve: "o ciclo seguinte" pode ser um
    `dispensar` que já saiu.

    Por isso o prazo é um `asyncio.wait` sobre um Event, e não um `sleep` nu.
    """
    orq = carregar_orquestrador(env={"CNC_TETO_TRAJETO_S": "30"})
    cancelar = asyncio.Event()

    async def _cenario():
        orq.modulo._cancelar_cronograma = cancelar
        # Arma o cancelamento antes do prazo começar: o prazo de 30 s tem de
        # terminar na hora, e não em 30 s.
        orq.modulo.cancelar_cronograma()
        return await orq.modulo._dormir_ou_cancelar(30.0, cancelar)

    inicio = time.monotonic()
    cumpriu = asyncio.run(_cenario())
    decorrido = time.monotonic() - inicio

    assert cumpriu is False, "o prazo foi cumprido apesar do cancelamento"
    assert decorrido < 1.0, f"o cancelamento levou {decorrido:.1f}s para agir"


def test_a_trava_cancela_o_cronograma_antes_de_avisar_qualquer_placa(
        carregar_orquestrador):
    """A ordem é a feature.

    Avisar a mesa custa até TIMEOUT_AVISO_TELAS_S (3 s), e 3 s é tempo de sobra
    para o relógio disparar mais um `dispensar` — que sairia DEPOIS de a trava
    existir, com a mesa já indo para o HOME. Cancelar primeiro fecha essa
    janela inteira por uma linha.
    """
    orq = carregar_orquestrador()
    ordem: list[str] = []
    cancelar = asyncio.Event()

    def _avisar(*args, **kwargs):
        ordem.append("aviso_as_placas")

    async def _cenario():
        orq.modulo._cancelar_cronograma = cancelar
        orq.modulo._agendar_aviso_telas = _avisar
        await orq.modulo._ativar_trava("OS-CRON-7", 3, "motivo", resumo="peso")

    asyncio.run(_cenario())

    assert cancelar.is_set(), "a trava não cancelou o cronograma"
    assert ordem == ["aviso_as_placas"]


# ══════════════════════════════════════════════════════════════════════════════
# A TRAVA CHEGA À MESA — o caminho inteiro, passo a passo
# ══════════════════════════════════════════════════════════════════════════════
#
# Até aqui a trava avisava uma placa só: as telas TFT. A mesa é a peça que está
# fisicamente sobre a bancada onde o supervisor vai mexer — e, sob o ciclo por
# relógio, é também a peça que continua andando sozinha se ninguém a avisar.
#
# A sequência tem seis passos e cada um tem teste próprio abaixo:
#   1. o Triple Check reprova
#   2. `_ativar_trava` cancela o cronograma        (antes de qualquer aviso)
#   3. o aviso sai para os DOIS adapters           (em paralelo)
#   4. a mesa recusa `mover` enquanto travada
#   5. o supervisor libera
#   6. a mesa volta a aceitar, e a OS retoma de onde parou


def _urls_avisadas(orq) -> list[str]:
    return [c["url"] for c in orq.adapter.chamadas
            if c["url"].endswith("/comandos/estado-celula")]


def test_trava_avisa_as_duas_placas(carregar_orquestrador, monkeypatch):
    """Passo 3. As telas mostram DE QUAL slot é a divergência; a mesa PARA.

    Em `gather` e não em sequência: em série, uma placa fora do ar somaria o seu
    TIMEOUT_AVISO_TELAS_S ao prazo da outra, e o aviso à mesa — a que importa,
    porque ela se move — chegaria depois de um timeout inteiro gasto esperando
    uma tela.
    """
    orq = carregar_orquestrador()
    postados: list[tuple] = []

    class _ClienteFake:
        async def post(self, url, json=None, timeout=None):
            postados.append((url, json))

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {"telas": "ok"}
            return _R()

    monkeypatch.setattr(orq.modulo, "_client", _ClienteFake())

    entregue = asyncio.run(orq.modulo._avisar_telas(True, 3, "OS-T", "peso"))

    assert entregue is True
    avisados = [u for u, _ in postados]
    assert any("dispenser" in u for u in avisados), "as telas não foram avisadas"
    assert any("cnc" in u for u in avisados), "a MESA não foi avisada"
    # O mesmo payload para as duas: traduzir de um lado é onde os dois passam a
    # divergir depois.
    corpos = [c for _, c in postados]
    assert corpos[0] == corpos[1]


def test_uma_placa_fora_do_ar_nao_impede_o_aviso_a_outra(carregar_orquestrador,
                                                         monkeypatch):
    """A mesa tem de ser avisada mesmo com as telas mortas, e vice-versa.

    É o motivo do `return_exceptions=True`: isto é cosmético para as telas e
    defensivo para a mesa, e nenhum dos dois pode derrubar o caminho da trava.
    """
    orq = carregar_orquestrador()
    postados: list[str] = []

    class _ClienteFake:
        async def post(self, url, json=None, timeout=None):
            postados.append(url)
            if "dispenser" in url:
                raise ConnectionError("telas fora do ar")

            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {}
            return _R()

    monkeypatch.setattr(orq.modulo, "_client", _ClienteFake())

    entregue = asyncio.run(orq.modulo._avisar_telas(True, 3, "OS-T", "peso"))

    assert entregue is False          # nem tudo chegou, e o retorno diz isso
    assert any("cnc" in u for u in postados), "a mesa ficou sem aviso por causa das telas"


def test_a_trava_do_triple_check_avisa_a_mesa_e_a_libera_depois(carregar_orquestrador,
                                                                monkeypatch):
    """A sequência inteira, de ponta a ponta: passos 1 a 6.

    O Triple Check reprova (o dispenser solta menos que o alvo), a trava é
    ativada, os dois adapters são avisados, o supervisor libera de dentro do
    broadcast — que é o instante exato em que a trava aparece na tela — e a OS
    termina. O aviso de liberação tem de sair DEPOIS do de ativação: uma tela
    que ficasse com o aviso invertido mostraria trava ativa numa célula solta.
    """
    orq = carregar_orquestrador()
    orq.adapter.quantidade_dispensada = 3        # alvo é 10 → Triple Check reprova
    avisos: list[bool] = []
    liberacoes: list[bool] = []

    async def _avisar(trava_ativa, slot_id, os_id, resumo):
        avisos.append(bool(trava_ativa))
        return True

    monkeypatch.setattr(orq.modulo, "_avisar_telas", _avisar)

    def _broadcast_e_liberar():
        if orq.estado["trava"]["ativa"] and not liberacoes:
            liberacoes.append(orq.modulo.liberar_trava("supervisor"))

    monkeypatch.setattr(orq.modulo, "_broadcast_fn", _broadcast_e_liberar)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-T1", _item("Dipirona"))))

    assert liberacoes == [True], "a trava não chegou a ser liberável"
    assert avisos == [True, False], (
        f"a ordem dos avisos às placas saiu {avisos} — a liberação tem de vir "
        f"depois da ativação")
    assert _status_gravados(orq)[-1] == ("OS-T1", "concluida")


def test_a_os_retoma_do_slot_seguinte_depois_da_trava(carregar_orquestrador,
                                                      monkeypatch):
    """Passo 6, e o modo de falhar que ele cobre.

    `_ativar_trava` arma o cancelamento do cronograma para matar qualquer prazo
    em curso. Se ele ficasse armado depois da liberação, TODO prazo dos slots
    restantes venceria na hora, um atrás do outro, e a OS terminaria no meio da
    rota sem uma linha de log dizendo por quê — com a bancada inteira íntegra.
    É `_aguardar_liberacao` que fecha isso, e é por isso que a limpeza mora
    colada na espera.
    """
    itens = [_item("Dipirona"), _item("Amoxicilina"), _item("Omeprazol")]
    orq = carregar_orquestrador()
    orq.adapter.quantidade_dispensada = 3        # toda dispensa reprova
    liberadas = []

    def _broadcast_e_liberar():
        if orq.estado["trava"]["ativa"]:
            liberadas.append(orq.modulo.liberar_trava("supervisor"))

    monkeypatch.setattr(orq.modulo, "_broadcast_fn", _broadcast_e_liberar)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-T2", *itens)))

    movimentos = orq.adapter.comandos("/comandos/mover")
    assert len(movimentos) == len(itens), (
        f"a OS visitou {len(movimentos)} de {len(itens)} slots — o cancelamento "
        f"ficou armado depois da liberação")
    assert _status_gravados(orq)[-1] == ("OS-T2", "concluida")


def test_o_cronograma_da_os_mais_pesada_e_o_que_a_conta_diz(carregar_orquestrador,
                                                            monkeypatch):
    """A OS que usa a célula inteira, com os valores de produção.

    Oito paradas e 52 unidades: 8 × 3,25 s de trajeto mais 52 × 1 s de dispensa
    mais 8 × 1 s de folga = **86,00 s de ciclo CNC**. Esse número é a promessa
    do modelo — é dele que sai "o `dispensar` sai com a mesa parada", porque o
    prazo de cada trajeto cobre o pior percurso da célula com folga.

    Fixá-lo aqui é o que transforma a conta feita uma vez, na verificação da
    frente, em algo que continua sendo conferido. Quem mexer num dos quatro
    números do cronograma vê o total mudar e decide se era isso que queria.

    O relógio é FALSO: o que se compara é o prazo PEDIDO, não o tempo de parede.
    """
    from conftest import CENTRAL_DIR  # noqa: PLC0415
    import importlib.util, sys        # noqa: PLC0415, E401

    spec = importlib.util.spec_from_file_location(
        "apsen_tpl_geral", CENTRAL_DIR / "os_templates.py")
    tpl = importlib.util.module_from_spec(spec)
    sys.modules["apsen_tpl_geral"] = tpl
    spec.loader.exec_module(tpl)
    try:
        alvo = next(t for t in tpl.TEMPLATES if "GERAL" in t["template_id"].upper())
    finally:
        sys.modules.pop("apsen_tpl_geral", None)

    itens = [_item(i["medicamento"], qtd=i["quantidade"]) for i in alvo["itens"]]
    assert len(itens) == 8, "OS-GERAL-01 deixou de usar a célula inteira"
    assert sum(i["quantidade"] for i in itens) == 52

    orq = carregar_orquestrador(env={
        "CNC_TETO_TRAJETO_S": "2.5", "CNC_MARGEM_CHEGADA_S": "0.75",
        "DISPENSA_S_POR_UNIDADE": "1.0", "DISPENSA_FOLGA_S": "1.0",
    })
    relogio = _com_relogio_falso(orq, monkeypatch)

    asyncio.run(orq.modulo._processar_os(
        _payload_os("OS-GERAL-CRON", *itens)))

    assert relogio.total == pytest.approx(86.0), {
        "medido pela suíte": relogio.total,
        "conta": "8 × 3,25 (trajeto) + 52 × 1,0 (dispensa) + 8 × 1,0 (folga)",
    }


# ══════════════════════════════════════════════════════════════════════════════
# O retorno de quem comanda é CONFERIDO — e "não respondeu" ≠ "está certo"
# ══════════════════════════════════════════════════════════════════════════════

def _alarmes(orq) -> list:
    return [c["args"] for c in orq.banco.chamadas_de("salvar_alarme")]


def test_homing_de_fim_de_os_recusado_abre_alarme(carregar_orquestrador):
    """A rota é serpentina, e a serpentina parte de HOME.

    O homing do passo 5 tinha o retorno ignorado: comando recusado deixava a
    mesa parada no último dispenser e a OS fechava como `concluida`, sem nada
    no log. Quem paga é a OS SEGUINTE — ela planeja o ciclo fechado a partir do
    HOME, e a otimalidade da serpentina é a do ciclo fechado.
    """
    orq = carregar_orquestrador()
    orq.adapter.recusar_rotas = {"/comandos/homing"}

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-H1")))

    tipos = [tipo for _, tipo, _ in _alarmes(orq)]
    assert "homing_nao_confirmado" in tipos


def test_homing_recusado_nao_aborta_a_os(carregar_orquestrador):
    """A dispensa já terminou e o Triple Check já opinou: abortar aqui marcaria
    em erro uma OS que entregou tudo certo. O alarme é o desfecho proporcional."""
    orq = carregar_orquestrador()
    orq.adapter.recusar_rotas = {"/comandos/homing"}

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-H2")))

    assert _status_gravados(orq) == [("OS-H2", "em_andamento"), ("OS-H2", "concluida")]


def test_homing_aceito_nao_abre_alarme(carregar_orquestrador):
    """Controle: sem ele, "não abriu alarme" não distingue conferir de não
    conferir — a OS feliz não abre alarme nenhum de qualquer jeito."""
    orq = carregar_orquestrador()

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-H3")))

    assert _alarmes(orq) == []


# ── Re-scan de SKU: silêncio não solta um slot comprovadamente errado ─────────
#
# O slot chegou aqui porque a câmera LEU e acusou SKU errado — é essa medição
# que armou a trava. O re-scan tratava `res is None` como "assumindo
# corrigido": com o vision-adapter fora do ar, bastava liberar a trava e
# esperar o timeout para o medicamento errado seguir para a dispensa.
#
# É a mesma linha que `avaliar_triple_check` traça entre `divergencias` e
# `fontes_indisponiveis`, com o sinal invertido de propósito: lá o silêncio
# deixa a OS seguir porque nada a contradisse; aqui já há contradição
# registrada, e o silêncio não a apaga.

def _supervisor_que_libera(orq, na_primeira_trava=None) -> list:
    """Supervisor que libera toda trava que aparece; devolve os motivos.

    `na_primeira_trava` roda uma vez, no instante em que a PRIMEIRA trava
    aparece — que é o único ponto em que dá para encenar o que acontece com o
    RE-SCAN sem encenar também o scan inicial, que é quem arma a trava.
    """
    motivos: list[str] = []

    def _broadcast_e_liberar():
        if not orq.estado["trava"]["ativa"]:
            return
        if not motivos and na_primeira_trava is not None:
            na_primeira_trava()
        motivos.append(orq.estado["trava"]["motivo"])
        orq.modulo.liberar_trava("supervisor")

    orq.modulo._broadcast_fn = _broadcast_e_liberar
    orq.adapter.capturas_divergentes = 1
    return motivos


def test_re_scan_mudo_mantem_a_trava(carregar_orquestrador, monkeypatch):
    """Duas travas: a do SKU errado e a da câmera que não respondeu ao re-scan."""
    orq = carregar_orquestrador()
    monkeypatch.setattr(orq.modulo.settings, "TIMEOUT_VISAO_DISPENSER", 0.05)
    # O scan inicial acusa SKU errado; o re-scan, aí sim, fica mudo.
    motivos = _supervisor_que_libera(
        orq, na_primeira_trava=lambda: setattr(orq.adapter, "scans_mudos", 1))

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-V1")))

    assert len(motivos) == 2, (
        "o re-scan mudo soltou o slot — um slot com SKU comprovadamente errado "
        "seguiu para a dispensa porque a câmera ficou calada")


def test_a_segunda_trava_diz_que_foi_a_camera(carregar_orquestrador, monkeypatch):
    """O motivo não pode repetir "SKU errado": a saída é outra — olhar a
    estação de visão, não trocar o medicamento do slot. E a pendência é a
    MESMA, então o motivo tem de dizer qual era."""
    orq = carregar_orquestrador()
    monkeypatch.setattr(orq.modulo.settings, "TIMEOUT_VISAO_DISPENSER", 0.05)
    motivos = _supervisor_que_libera(
        orq, na_primeira_trava=lambda: setattr(orq.adapter, "scans_mudos", 1))

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-V2")))

    assert "SKU errado" in motivos[0]
    assert "não respondeu" in motivos[1]
    assert "SKU" in motivos[1]


def test_re_scan_recusado_nao_queima_o_timeout(carregar_orquestrador, monkeypatch):
    """Envio recusado é resolvido na hora como "sem medição" — a mesma regra da
    etapa 3b. Esperar um evento que já se sabe inexistente gasta o relógio do
    orquestrador no exato momento em que o supervisor olha para a tela."""
    orq = carregar_orquestrador()
    # Timeout ALTO de propósito: se o código esperasse por ele, o cronômetro
    # deste teste diria. O que se mede é que a espera não aconteceu.
    monkeypatch.setattr(orq.modulo.settings, "TIMEOUT_VISAO_DISPENSER", 30.0)

    def _recusar_so_o_re_scan():
        orq.adapter.recusar_rotas = {"/comandos/capturar/dispenser"}

    motivos = _supervisor_que_libera(orq, na_primeira_trava=_recusar_so_o_re_scan)
    # Da segunda trava em diante a câmera volta, senão o laço é infinito — que
    # é, aliás, o comportamento correto com a visão fora do ar.
    orq.modulo._broadcast_fn_original = orq.modulo._broadcast_fn

    def _liberar_e_devolver_a_camera():
        orq.modulo._broadcast_fn_original()
        if len(motivos) >= 2:
            orq.adapter.recusar_rotas = set()

    orq.modulo._broadcast_fn = _liberar_e_devolver_a_camera

    inicio = time.monotonic()
    asyncio.run(orq.modulo._processar_os(_payload_os("OS-V3")))
    decorrido = time.monotonic() - inicio

    assert len(motivos) >= 2
    assert decorrido < 10.0, (
        f"{decorrido:.1f}s — o re-scan recusado ficou esperando um evento que "
        f"já se sabia que não viria")


def test_re_scan_que_responde_OK_solta_o_slot(carregar_orquestrador):
    """Controle: o caminho feliz continua funcionando — uma trava só."""
    orq = carregar_orquestrador()
    motivos = _supervisor_que_libera(orq)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-V4")))

    assert len(motivos) == 1
    assert _status_gravados(orq)[-1] == ("OS-V4", "concluida")
