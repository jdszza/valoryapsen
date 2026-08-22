"""
Vision simulator: são TRÊS câmeras, e o lado sai do slot — não do comando.

A célula tem uma câmera por fileira de dispensers (D1–D4 à esquerda, D5–D8 à
direita) e uma sobre a mesa de coleta, onde fica a balança HX711. O tipo do
evento continua sendo o mesmo nas duas câmeras de dispenser
(`leitura_dispenser_*`): quem separa esquerda de direita é o campo `camera`.

O que estes testes protegem:

  * a câmera é DERIVADA do slot_id. Se o adapter (ou o central) pudesse
    escolher, um dia pediria a leitura de D7 para a câmera da esquerda — e o
    sintoma seria uma divergência de SKU num slot só, indistinguível de um
    medicamento realmente trocado;
  * a partição esquerda/direita é a MESMA da geometria do orquestrador. Duas
    metades calculadas em serviços diferentes precisariam concordar, e a
    discordância não daria erro: daria leitura atribuída à câmera errada;
  * o override por câmera (`..._ESQ` / `..._DIR`) atinge só a câmera do sufixo.
    É o que permite simular uma câmera suja em campo sem transformar a célula
    inteira num equipamento ruim.
"""
import pytest

from conftest import NUM_SLOTS, SLOTS_POR_FILEIRA

# Sem sono e sem sorteio: o que está em teste é o roteamento por câmera, não o
# ciclo óptico. Toda captura cai no happy path e emite exatamente um evento.
ENV_DETERMINISTICO = {
    "T_SCAN_DISPENSER":             "0",
    "T_SCAN_MESA":                  "0",
    "PROB_FALHA_LEITURA_DISPENSER": "0",
    "PROB_DIVERGENCIA_DISPENSER":   "0",
    "PROB_FALHA_LEITURA_MESA":      "0",
    "PROB_DIVERGENCIA_MESA":        "0",
}

SLOTS_ESQUERDA = list(range(1, SLOTS_POR_FILEIRA + 1))
SLOTS_DIREITA  = list(range(SLOTS_POR_FILEIRA + 1, NUM_SLOTS + 1))


@pytest.fixture
def visao(carregar_simulador):
    return carregar_simulador("vision", env=ENV_DETERMINISTICO)


def _capturar_dispenser(sim, slot_id: int) -> dict:
    sim.modulo._do_capturar_dispenser(slot_id, "OS-CAM", "SKU-1", "Dipirona", 5)
    (evento,) = sim.eventos
    return evento


# ── Qual câmera olhou ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("slot_id", SLOTS_ESQUERDA)
def test_slot_da_fileira_esquerda_e_lido_pela_camera_esquerda(visao, slot_id):
    evento = _capturar_dispenser(visao, slot_id)

    assert evento["camera"] == "dispenser_esq"
    assert evento["tipo"] == "leitura_dispenser_ok"
    assert evento["slot_id"] == slot_id


@pytest.mark.parametrize("slot_id", SLOTS_DIREITA)
def test_slot_da_fileira_direita_e_lido_pela_camera_direita(visao, slot_id):
    evento = _capturar_dispenser(visao, slot_id)

    assert evento["camera"] == "dispenser_dir"
    assert evento["tipo"] == "leitura_dispenser_ok"
    assert evento["slot_id"] == slot_id


def test_captura_de_mesa_sai_pela_camera_da_balanca(visao):
    visao.modulo._do_capturar_mesa(3, "OS-CAM", 5, 120.0, -150.0)

    (evento,) = visao.eventos
    assert evento["camera"] == "mesa"
    assert evento["tipo"] == "leitura_mesa_ok"


def test_o_tipo_do_evento_nao_distingue_os_lados(visao):
    """Esquerda e direita emitem o MESMO tipo — o orquestrador não olha o lado.

    Se o lado entrasse no tipo (`leitura_dispenser_esq_ok`), a espera do
    orquestrador e o Triple Check passariam a depender da geometria da bancada
    para reconhecer um resultado que já é inequívoco sem ela.
    """
    esquerda = _capturar_dispenser(visao, SLOTS_ESQUERDA[0])
    visao.limpar_eventos()
    direita  = _capturar_dispenser(visao, SLOTS_DIREITA[0])

    assert esquerda["tipo"] == direita["tipo"] == "leitura_dispenser_ok"
    assert esquerda["camera"] != direita["camera"]


# ── A partição é a da geometria, não uma cópia ────────────────────────────────

def test_lado_da_camera_acompanha_o_y_do_slot(visao, carregar_orquestrador):
    """A câmera de um slot cai do mesmo lado do corredor em que ele está.

    `POSICOES` é a fonte da geometria (y < 0 na fileira esquerda, y > 0 na
    direita). Divergir daqui não daria erro: daria leitura de D7 arquivada como
    se a câmera da esquerda a tivesse feito.
    """
    posicoes = carregar_orquestrador().modulo.POSICOES

    for slot_id, (_, y) in posicoes.items():
        lado_geometrico = "dispenser_esq" if y < 0 else "dispenser_dir"
        assert visao.modulo.camera_do_slot(slot_id) == lado_geometrico, (
            f"slot {slot_id} está em y={y} mas foi para "
            f"{visao.modulo.camera_do_slot(slot_id)}"
        )


def test_as_duas_cameras_cobrem_a_celula_inteira(visao):
    cobertos = {visao.modulo.camera_do_slot(s) for s in range(1, NUM_SLOTS + 1)}

    assert cobertos == {"dispenser_esq", "dispenser_dir"}


# ── Configuração por câmera ───────────────────────────────────────────────────

def test_override_por_camera_atinge_so_o_lado_do_sufixo(carregar_simulador):
    """Uma câmera suja em campo não deve exigir degradar a outra."""
    sim = carregar_simulador("vision", env={
        **ENV_DETERMINISTICO,
        "PROB_FALHA_LEITURA_DISPENSER_DIR": "1",
    })

    esquerda = _capturar_dispenser(sim, SLOTS_ESQUERDA[0])
    sim.limpar_eventos()
    direita  = _capturar_dispenser(sim, SLOTS_DIREITA[0])

    assert esquerda["tipo"] == "leitura_dispenser_ok"
    assert direita["tipo"] == "leitura_dispenser_falha"
    assert direita["camera"] == "dispenser_dir"


def test_sem_sufixo_o_valor_compartilhado_vale_para_as_duas(carregar_simulador):
    sim = carregar_simulador("vision", env={
        **ENV_DETERMINISTICO,
        "PROB_DIVERGENCIA_DISPENSER": "1",
    })

    esquerda = _capturar_dispenser(sim, SLOTS_ESQUERDA[0])
    sim.limpar_eventos()
    direita  = _capturar_dispenser(sim, SLOTS_DIREITA[0])

    assert esquerda["tipo"] == "leitura_dispenser_divergencia"
    assert direita["tipo"] == "leitura_dispenser_divergencia"


def test_camera_da_mesa_nao_herda_override_de_dispenser(carregar_simulador):
    sim = carregar_simulador("vision", env={
        **ENV_DETERMINISTICO,
        "PROB_FALHA_LEITURA_DISPENSER": "1",
    })

    sim.modulo._do_capturar_mesa(1, "OS-CAM", 5, 0.0, -150.0)

    (evento,) = sim.eventos
    assert evento["tipo"] == "leitura_mesa_ok"


# ── Telemetria e status ───────────────────────────────────────────────────────

def test_telemetria_reporta_as_tres_cameras(visao):
    componentes = {c for c, _, _ in visao.modulo.COMPONENTES_TELEMETRIA}

    assert {"camera_dispenser_esq", "camera_dispenser_dir",
            "camera_mesa"} <= componentes


def test_status_lista_as_tres_cameras_com_a_cobertura(visao):
    cameras = visao.modulo.status()["cameras"]

    assert [c["camera"] for c in cameras] == [
        "dispenser_esq", "dispenser_dir", "mesa",
    ]
    assert cameras[0]["cobertura"] == f"D1-D{SLOTS_POR_FILEIRA}"
    assert cameras[1]["cobertura"] == f"D{SLOTS_POR_FILEIRA + 1}-D{NUM_SLOTS}"


def test_endpoint_de_captura_devolve_a_camera_escolhida(visao):
    req = visao.modulo.CapturarDispenserReq(
        slot_id=SLOTS_DIREITA[0], os_id="OS-CAM", sku_esperado="SKU-1",
        medicamento_esperado="Dipirona", quantidade_esperada=1,
    )

    resposta = visao.modulo.executar_capturar_dispenser(req)

    assert resposta["camera"] == "dispenser_dir"
