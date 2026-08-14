"""
Testes do weight-simulator (célula de carga HX711 sob a mesa CNC).

Regressão principal: `_do_pesar()` declarava `global` dentro do `with _lock:` sem
incluir `_peso_mesa_g`, que é atribuído com `+=` logo abaixo. Python tratava o
nome como local e TODA pesagem estourava UnboundLocalError dentro da thread —
falha silenciosa: nenhum evento era emitido e o orquestrador só saía por
TIMEOUT_PESO, deixando a balança fora do Triple Check.
"""
import pytest

# Import determinístico: sem espera de estabilização, sem falha de sensor e sem
# ruído gaussiano — assim o peso medido é exatamente qtd × peso_unitario_g.
ENV_DETERMINISTICO = {
    "T_LEITURA":        "0",
    "PROB_ERRO_SENSOR": "0",
    "RUIDO_G":          "0",
    "TOLERANCIA_PERC":  "5.0",
}

TIPOS_DE_PESAGEM = ("peso_ok", "peso_divergencia")


@pytest.fixture
def weight(carregar_simulador):
    return carregar_simulador("weight", env=ENV_DETERMINISTICO)


def test_do_pesar_emite_um_unico_evento_de_pesagem(weight):
    """(a) não levanta exceção e (b) emite exatamente um evento de pesagem."""
    weight.modulo._do_pesar("OS-TESTE", slot_id=1, quantidade_esperada=10,
                            peso_unitario_g=50.0)

    assert len(weight.eventos) == 1, f"eventos emitidos: {weight.eventos}"

    evento = weight.eventos[0]
    assert evento["tipo"] in TIPOS_DE_PESAGEM
    assert evento["slot_id"] == 1
    assert evento["peso_esperado_g"] == pytest.approx(500.0)
    assert evento["peso_medido_g"] == pytest.approx(500.0)
    assert evento["dentro_tolerancia"] is True


def test_peso_medido_e_o_delta_do_slot_nao_o_acumulado(weight):
    """(c) a balança mede o total na mesa; o evento deve reportar o incremento.

    Duas pesagens iguais em sequência: a segunda leitura tem que reportar 500 g
    (o que caiu naquele slot), não 1000 g (o acumulado desde a tara).
    """
    for slot in (1, 2):
        weight.modulo._do_pesar("OS-TESTE", slot_id=slot, quantidade_esperada=10,
                                peso_unitario_g=50.0)

    primeiro, segundo = weight.eventos_do_tipo(*TIPOS_DE_PESAGEM)

    assert primeiro["peso_medido_g"] == pytest.approx(500.0)
    assert primeiro["peso_acumulado_g"] == pytest.approx(500.0)

    assert segundo["peso_medido_g"] == pytest.approx(500.0), (
        "segunda leitura reportou o acumulado em vez do delta do slot"
    )
    assert segundo["peso_acumulado_g"] == pytest.approx(1000.0)
    assert segundo["desvio_g"] == pytest.approx(0.0)
    assert segundo["tipo"] == "peso_ok"


def test_tara_zera_o_delta_para_a_proxima_os(weight):
    """Após a tara, o primeiro slot da nova OS parte do zero (não do acumulado)."""
    weight.modulo._do_pesar("OS-1", slot_id=1, quantidade_esperada=10,
                            peso_unitario_g=50.0)
    weight.modulo._do_tara("OS-2")
    weight.limpar_eventos()

    weight.modulo._do_pesar("OS-2", slot_id=1, quantidade_esperada=4,
                            peso_unitario_g=25.0)

    evento = weight.eventos[0]
    assert evento["tipo"] == "peso_ok"
    assert evento["peso_medido_g"] == pytest.approx(100.0)
    assert evento["peso_acumulado_g"] == pytest.approx(100.0)
