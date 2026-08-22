"""A faixa de slots é a mesma nos quatro simuladores — e ela cresceu.

A célula passou de 6 para 8 dispensers, em duas fileiras frente a frente. O
número aparecia hardcoded em cinco serviços (`range(1, 7)` e a mensagem
"deve ser 1-6"), cada um com sua cópia: bastava um deles ficar para trás para
que D7 e D8 existissem no central e fossem recusados com 400 pelo equipamento
— falha que só apareceria em produção, na primeira OS que sorteasse um slot da
ponta, e cujo sintoma (`erro_carregamento` num slot só) não aponta para a causa.

Por isso o teste é PARAMETRIZADO por simulador em vez de escrito quatro vezes:
um simulador novo entra na tabela `VALIDACOES` e passa a ser cobrado pela mesma
regra. E as três asserções são as três que importam:

  * o último slot da fileira de trás é aceito (a célula toda é utilizável);
  * o penúltimo também (não é um "+1" solto na validação);
  * `NUM_SLOTS + 1` é recusado com 400 (a faixa continua sendo uma faixa —
     slot inexistente não pode virar comando enviado a um motor que não existe).

`NUM_SLOTS` vem do conftest, que é quem fixa a env var para a sessão inteira:
repetir o literal 8 aqui reintroduziria, no teste, exatamente o acoplamento que
a mudança removeu do código.
"""
import pytest
from fastapi import HTTPException

from conftest import NUM_SLOTS

# Ambiente determinístico: nenhum simulador deve dormir nem sortear falha ao
# aceitar o comando — o que está em teste é a validação de faixa, não o ciclo.
ENV_RAPIDO = {
    "T_CARGA_UNID": "0", "T_DISPENSA_UNID": "0", "PROB_ERRO_MECANICO": "0",
    "T_SCAN_DISPENSER": "0", "T_SCAN_MESA": "0",
    "PROB_FALHA_LEITURA_DISPENSER": "0", "PROB_DIVERGENCIA_DISPENSER": "0",
    "PROB_FALHA_LEITURA_MESA": "0", "PROB_DIVERGENCIA_MESA": "0",
    "T_LEITURA": "0", "PROB_ERRO_SENSOR": "0", "RUIDO_G": "0",
    "INTERVALO": "0", "VEL_MM_S": "100000",
}


def _dispenser_carregar(sim, slot_id):
    req = sim.modulo.CarregarReq(dispenser_id=slot_id, medicamento="Dipirona",
                                 sku="SKU-1", categoria="geral", quantidade=1,
                                 os_id="OS-SLOT")
    return sim.modulo.executar_carregar(req)


def _dispenser_limpar(sim, slot_id):
    return sim.modulo.executar_limpar(
        sim.modulo.LimparReq(dispenser_id=slot_id, solicitado_por="teste")
    )


def _dispenser_status(sim, slot_id):
    return sim.modulo.status_slot(slot_id)


def _vision_dispenser(sim, slot_id):
    return sim.modulo.executar_capturar_dispenser(
        sim.modulo.CapturarDispenserReq(slot_id=slot_id, os_id="OS-SLOT",
                                        sku_esperado="SKU-1",
                                        medicamento_esperado="Dipirona",
                                        quantidade_esperada=1)
    )


def _vision_mesa(sim, slot_id):
    return sim.modulo.executar_capturar_mesa(
        sim.modulo.CapturarMesaReq(slot_id=slot_id, os_id="OS-SLOT",
                                   quantidade_esperada=1,
                                   posicao_x=0.0, posicao_y=0.0)
    )


def _weight_pesar(sim, slot_id):
    return sim.modulo.executar_pesar(
        sim.modulo.PesarReq(os_id="OS-SLOT", slot_id=slot_id,
                            quantidade_esperada=1, quantidade_real=1,
                            peso_unitario_g=50.0)
    )


def _cnc_mover(sim, slot_id):
    return sim.modulo.executar_mover(
        sim.modulo.MoverReq(dispenser_alvo=slot_id, os_id="OS-SLOT",
                            posicao_x=0.0, posicao_y=0.0,
                            ciclo_atual=1, total_ciclos=1)
    )


# (id do caso, simulador, função que manda o comando para um slot)
VALIDACOES = [
    ("dispenser/carregar", "dispenser", _dispenser_carregar),
    ("dispenser/limpar",   "dispenser", _dispenser_limpar),
    ("dispenser/status",   "dispenser", _dispenser_status),
    ("vision/dispenser",   "vision",    _vision_dispenser),
    ("vision/mesa",        "vision",    _vision_mesa),
    ("weight/pesar",       "weight",    _weight_pesar),
    ("cnc/mover",          "cnc",       _cnc_mover),
]

PARAMS = pytest.mark.parametrize(
    "simulador,comandar",
    [(sim, fn) for _, sim, fn in VALIDACOES],
    ids=[caso for caso, _, _ in VALIDACOES],
)


@PARAMS
@pytest.mark.parametrize("slot_id", [NUM_SLOTS - 1, NUM_SLOTS],
                         ids=["penultimo_slot", "ultimo_slot"])
def test_slot_da_fileira_de_tras_e_aceito(carregar_simulador, simulador,
                                          comandar, slot_id):
    """Os slots novos (D7 e D8 na célula de 8) valem como qualquer outro."""
    sim = carregar_simulador(simulador, env=ENV_RAPIDO)

    resposta = comandar(sim, slot_id)

    assert resposta is not None


@PARAMS
def test_slot_fora_da_celula_e_recusado_com_400(carregar_simulador, simulador,
                                                comandar):
    """Um slot além da célula é erro de quem chamou, não comando a executar."""
    sim = carregar_simulador(simulador, env=ENV_RAPIDO)

    with pytest.raises(HTTPException) as erro:
        comandar(sim, NUM_SLOTS + 1)

    assert erro.value.status_code == 400


@PARAMS
def test_slot_zero_e_recusado_com_400(carregar_simulador, simulador, comandar):
    """A faixa tem as duas pontas: id começa em 1, e 0 não é 'o primeiro'."""
    sim = carregar_simulador(simulador, env=ENV_RAPIDO)

    with pytest.raises(HTTPException) as erro:
        comandar(sim, 0)

    assert erro.value.status_code == 400


@PARAMS
def test_mensagem_de_recusa_cita_a_faixa_real(carregar_simulador, simulador,
                                              comandar):
    """A mensagem é derivada de NUM_SLOTS — "1-6" fixo mentia sobre a célula.

    Quem lê o log de um 400 usa a mensagem para saber qual faixa vale; um
    literal desatualizado manda procurar o problema no lugar errado.
    """
    sim = carregar_simulador(simulador, env=ENV_RAPIDO)

    with pytest.raises(HTTPException) as erro:
        comandar(sim, NUM_SLOTS + 1)

    assert f"1-{NUM_SLOTS}" in str(erro.value.detail)


def test_dispenser_expoe_um_slot_por_posicao_da_celula(carregar_simulador):
    """O estoque interno do simulador nasce com a célula inteira."""
    sim = carregar_simulador("dispenser", env=ENV_RAPIDO)

    assert sim.modulo.NUM_SLOTS == NUM_SLOTS
    assert sorted(sim.modulo._estoque) == list(range(1, NUM_SLOTS + 1))


def test_cnc_nao_guarda_mapa_de_posicoes(carregar_simulador):
    """O mapa é do central: cópia no simulador é a duplicação que saiu daqui.

    O simulador recebe `posicao_x`/`posicao_y` em cada comando de movimento e
    valida apenas a faixa do id. Se um `POSICOES` reaparecer aqui, volta o par
    de dicionários mantidos à mão em serviços diferentes.
    """
    sim = carregar_simulador("cnc", env=ENV_RAPIDO)

    assert not hasattr(sim.modulo, "POSICOES")
