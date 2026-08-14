"""
Testes do dispenser-simulator (6 dispensers físicos).

Regressão principal: o botão "Limpar Dispenser" da IHM parava de funcionar depois
da primeira OS. _do_dispensar() encerrava o slot com status="concluido" e deixava
o os_id preso; _do_limpar() recusava a limpeza tanto por esse status quanto por
`os_id is not None` — e era o único caminho que zerava esses dois campos.
Impasse permanente: o slot só sairia dali sendo limpo, e limpar era o que estava
bloqueado.
"""
import threading

import pytest

# Import determinístico: sem tempo de carga/dispensa e sem falha mecânica.
ENV_DETERMINISTICO = {
    "T_CARGA_UNID":       "0",
    "T_DISPENSA_UNID":    "0",
    "PROB_ERRO_MECANICO": "0",
}

SLOT = 1
MED, SKU, CAT = "Dipirona 500mg", "SKU-DIP-500", "analgesico"


@pytest.fixture
def disp(carregar_simulador):
    return carregar_simulador("dispenser", env=ENV_DETERMINISTICO)


def _carregar(disp, quantidade, os_id):
    disp.modulo._do_carregar(SLOT, MED, SKU, CAT, quantidade, os_id)


def _chamar_com_prazo(func, prazo_s=5.0):
    """Executa func() numa thread e devolve (terminou, resultado).

    A falha aqui é travamento, não exceção — um assert comum ficaria pendurado
    para sempre e levaria a suíte inteira junto. A thread é daemon: se travar,
    ela vaza segurando o lock daquela instância do módulo, mas cada teste carrega
    o simulador do zero, então não contamina os demais.
    """
    resultado = []
    t = threading.Thread(target=lambda: resultado.append(func()), daemon=True)
    t.start()
    t.join(timeout=prazo_s)
    return not t.is_alive(), (resultado[0] if resultado else None)


def test_limpeza_apos_os_concluida_e_aceita(disp):
    """carregar → dispensar → limpar: a limpeza tem que passar."""
    _carregar(disp, 5, "OS-A")
    disp.modulo._do_dispensar(SLOT, "OS-A")
    disp.limpar_eventos()

    disp.modulo._do_limpar(SLOT, "operador")

    assert [e["tipo"] for e in disp.eventos] == ["limpeza_ok"]

    estoque = disp.modulo._estoque[SLOT]
    assert estoque["quantidade"] == 0
    assert estoque["medicamento"] is None
    assert estoque["sku"] is None
    assert estoque["categoria"] is None

    estado = disp.modulo._estado[SLOT]
    assert estado["status"] == "limpo"
    assert estado["os_id"] is None


def test_limpeza_zera_estoque_residual(disp):
    """Slot com sobra de uma OS anterior: é o caso que o botão existe para resolver.

    Aqui o estoque ainda tem conteúdo na hora da limpeza — no teste acima a
    dispensa consome tudo, então os campos já vinham zerados de antes.
    """
    _carregar(disp, 5, "OS-A")            # carrega 5
    _carregar(disp, 2, "OS-B")            # residual suficiente: só reserva 2
    disp.modulo._do_dispensar(SLOT, "OS-B")

    assert disp.modulo._estoque[SLOT]["quantidade"] == 3, "pré-condição: sobrou estoque"
    assert disp.modulo._estoque[SLOT]["medicamento"] == MED
    disp.limpar_eventos()

    disp.modulo._do_limpar(SLOT, "operador")

    evento = disp.eventos[0]
    assert evento["tipo"] == "limpeza_ok"
    assert evento["medicamento_limpo"] == MED

    assert disp.modulo._estoque[SLOT] == {
        "medicamento": None, "sku": None, "categoria": None, "quantidade": 0,
    }


def test_limpeza_de_slot_carregado_mas_nao_dispensado_e_aceita(disp):
    """Slot em "pronto", ainda amarrado a uma OS que nunca dispensou (OS abortada,
    trava do Triple Check, operador desistiu). O estoque está parado ali e precisa
    poder ser removido — antes o guard recusava tanto pelo status quanto pelo os_id.
    """
    _carregar(disp, 5, "OS-A")
    assert disp.modulo._estado[SLOT]["status"] == "pronto", "pré-condição"
    assert disp.modulo._estado[SLOT]["os_id"] == "OS-A", "pré-condição"
    disp.limpar_eventos()

    disp.modulo._do_limpar(SLOT, "operador")

    assert [e["tipo"] for e in disp.eventos] == ["limpeza_ok"]
    assert disp.modulo._estoque[SLOT]["quantidade"] == 0
    assert disp.modulo._estado[SLOT]["os_id"] is None


def test_evento_dispensado_carrega_o_os_id(disp):
    """O central destrava o orquestrador com "{os_id}:dispensado:{slot}".

    Se o reset do slot vazasse para o payload, o os_id viria nulo e a OS travaria
    até o timeout.
    """
    _carregar(disp, 5, "OS-A")
    disp.limpar_eventos()

    disp.modulo._do_dispensar(SLOT, "OS-A")

    dispensados = disp.eventos_do_tipo("dispensado")
    assert len(dispensados) == 1
    assert dispensados[0]["os_id"] == "OS-A"
    assert dispensados[0]["dispenser_id"] == SLOT
    assert dispensados[0]["quantidade_dispensada"] == 5

    # E o slot fica num estado terminal limpo, liberado para a limpeza.
    assert disp.modulo._estado[SLOT]["status"] == "idle"
    assert disp.modulo._estado[SLOT]["os_id"] is None


@pytest.mark.parametrize("status_em_curso", ["carregando", "dispensando"])
def test_limpeza_durante_operacao_fisica_e_recusada(disp, status_em_curso):
    """Operação física em curso continua bloqueando a limpeza.

    O status é posto à mão porque é exatamente ele que o guard consulta; subir uma
    thread real de dispensa só para pegá-la no meio deixaria o teste dependente de
    timing.
    """
    _carregar(disp, 5, "OS-A")
    with disp.modulo._lock:
        disp.modulo._estado[SLOT].update({"status": status_em_curso, "os_id": "OS-A"})
    disp.limpar_eventos()

    disp.modulo._do_limpar(SLOT, "operador")

    evento = disp.eventos[0]
    assert evento["tipo"] == "erro"
    assert evento["codigo_erro"] == "limpeza_em_operacao"
    assert evento["os_id"] == "OS-A"          # lido dentro do lock, junto com o status

    # Nada foi tocado.
    assert disp.modulo._estoque[SLOT]["quantidade"] == 5
    assert disp.modulo._estado[SLOT]["status"] == status_em_curso


def test_falha_mecanica_na_carga_nao_prende_o_slot(carregar_simulador):
    """Carga abortada por falha mecânica deve deixar o slot limpável."""
    disp = carregar_simulador("dispenser", env={**ENV_DETERMINISTICO,
                                                "PROB_ERRO_MECANICO": "1.0"})

    _carregar(disp, 5, "OS-A")

    erros = disp.eventos_do_tipo("erro")
    assert len(erros) == 1
    assert erros[0]["codigo_erro"] == "erro_mecanico_carga"
    assert erros[0]["os_id"] == "OS-A", "o central precisa do os_id para tratar a falha"
    assert disp.modulo._estado[SLOT]["os_id"] is None, "slot não pode ficar preso à OS morta"
    disp.limpar_eventos()

    disp.modulo._do_limpar(SLOT, "operador")

    assert [e["tipo"] for e in disp.eventos] == ["limpeza_ok"]
    assert disp.modulo._estoque[SLOT]["medicamento"] is None


def test_get_status_responde_sem_travar(disp):
    """GET /status não pode adquirir _lock antes de chamar _snapshot_slot().

    _lock é um threading.Lock() (não reentrante) e _snapshot_slot() trava por
    conta própria: segurar o lock antes deixava a request pendurada para sempre.
    Sob uvicorn o endpoint é sync, ou seja, roda no threadpool — cada chamada
    queimava um worker de vez, até o simulador inteiro parar de responder.
    """
    _carregar(disp, 5, "OS-A")

    terminou, resposta = _chamar_com_prazo(disp.modulo.status)

    assert terminou, "GET /status travou (deadlock: _lock adquirido duas vezes)"
    assert [s["dispenser_id"] for s in resposta["slots"]] == [1, 2, 3, 4, 5, 6]

    slot = resposta["slots"][SLOT - 1]
    assert slot["medicamento"] == MED
    assert slot["quantidade"] == 5
    assert slot["status"] == "pronto"


def test_get_status_slot_individual_responde_sem_travar(disp):
    """status_slot() sempre funcionou; fixa o comportamento contra regressão."""
    _carregar(disp, 5, "OS-A")

    terminou, resposta = _chamar_com_prazo(lambda: disp.modulo.status_slot(SLOT))

    assert terminou, "GET /status/{slot_id} travou"
    assert resposta["dispenser_id"] == SLOT
    assert resposta["quantidade"] == 5
