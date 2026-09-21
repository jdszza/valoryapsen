# -*- coding: utf-8 -*-
"""
Modo apresentação e fator de velocidade — os dois controles globais da demo.

Três frentes, e cada uma cobre um modo de a feature falhar:

1. **`MODO_APRESENTACAO` zera o acaso.** Cada simulador é carregado com as
   probabilidades de falha em 1.0 — mais forte do que N execuções a 0.01: se
   nem com probabilidade 1 sai falha, não sai com 1%. O laço de N execuções
   fica por cima disso, para pegar quem mover a decisão para dentro da chamada
   e reler a variável por operação.

2. **`FATOR_VELOCIDADE` escala os tempos — e os timeouts junto.** O erro mais
   provável da feature é desacelerar a célula sem desacelerar o teto de espera:
   a OS aborta por `timeout_carregamento` no meio da apresentação e o log culpa
   o dispenser, que fez exatamente o que se pediu. Os tempos são medidos pelos
   `time.sleep` de verdade (monkeypatch no módulo), não lidos das constantes:
   constante certa com `sleep` de outro valor passaria despercebido.

3. **As CINCO cópias de `_modo_apresentacao`/`_fator_velocidade` concordam.**
   Os simuladores são imagens separadas e não importam módulo do central, então
   o bloco é duplicado de propósito (mesma razão de `os_templates.novo_os_id` ×
   `simulator._novo_os_id`). O que torna a duplicação segura é esta
   comparação-cruzada, no espírito de `test_adapters.py`: cinco cópias divergem
   no primeiro ajuste, e a que fica para trás é a do serviço que ninguém está
   olhando naquele dia.
"""
import importlib.util
import os as _os
import sys
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent

# Probabilidade máxima: toda operação DEVERIA falhar se o modo não valesse.
CERTEZA = "1.0"
# Execuções por caso. Não é o que dá força ao teste (a probabilidade 1.0 é),
# mas pega quem passar a decidir por operação em vez de por configuração.
N_EXEC = 50


def _carregar_config(env=None):
    """`central-computer/config.py` por caminho, com o ambiente aplicado antes.

    Sem `carregar_central`: os defaults de dataclass são avaliados no import do
    módulo, e este teste precisa de uma leitura nova por parametrização. Nome de
    módulo único por chamada para não pegar o cache de sessão, e o ambiente é
    restaurado no fim para não vazar para o teste seguinte.
    """
    caminho = RAIZ_REPO / "central-computer" / "config.py"
    anteriores = {c: _os.environ.get(c) for c in (env or {})}
    for chave, valor in (env or {}).items():
        _os.environ[chave] = valor
    try:
        nome = "apsen_config_%d" % id(env or caminho)
        spec = importlib.util.spec_from_file_location(nome, caminho)
        modulo = importlib.util.module_from_spec(spec)
        sys.modules[nome] = modulo
        try:
            spec.loader.exec_module(modulo)
        finally:
            del sys.modules[nome]
        return modulo
    finally:
        for chave, valor in anteriores.items():
            if valor is None:
                _os.environ.pop(chave, None)
            else:
                _os.environ[chave] = valor


# ── 1. MODO_APRESENTACAO zera o acaso ─────────────────────────────────────────

def test_dispenser_sem_falha_mecanica_no_modo_apresentacao(carregar_simulador):
    sim = carregar_simulador("dispenser", env={
        "MODO_APRESENTACAO":  "1",
        "PROB_ERRO_MECANICO": CERTEZA,
        "T_CARGA_UNID":       "0",
        "T_DISPENSA_UNID":    "0",
    })
    assert sim.modulo.PROB_ERRO_MECANICO == 0.0

    for i in range(N_EXEC):
        sim.modulo._do_carregar(1, "Dipirona", "SKU-1", "analgesico", 3, "OS-%d" % i)
        sim.modulo._do_dispensar(1, "OS-%d" % i)

    assert sim.eventos_do_tipo("erro") == []
    assert len(sim.eventos_do_tipo("carregado")) == N_EXEC
    dispensas = sim.eventos_do_tipo("dispensado")
    assert len(dispensas) == N_EXEC
    # Falha mecânica na DISPENSA não vira evento "erro": ela solta menos
    # unidades do que o alvo. Zerada, todo evento sai completo.
    assert all(e["quantidade_dispensada"] == e["quantidade_alvo"] for e in dispensas)
    assert not any(e["falha_mecanica"] for e in dispensas)


def test_dispenser_falha_mecanica_continua_valendo_sem_o_modo(carregar_simulador):
    """Controle: sem o modo, a MESMA configuração falha.

    Sem este par, um `_do_carregar` que deixasse de sortear por outro motivo
    (refatoração, exceção engolida) faria o teste acima passar sem provar nada.
    """
    sim = carregar_simulador("dispenser", env={
        "MODO_APRESENTACAO":  "0",
        "PROB_ERRO_MECANICO": CERTEZA,
        "T_CARGA_UNID":       "0",
    })
    assert sim.modulo.PROB_ERRO_MECANICO == 1.0

    sim.modulo._do_carregar(1, "Dipirona", "SKU-1", "analgesico", 3, "OS-1")
    erros = sim.eventos_do_tipo("erro")
    assert erros and erros[0]["codigo_erro"] == "erro_mecanico_carga"


def test_vision_tres_cameras_sem_falha_no_modo_apresentacao(carregar_simulador):
    """As TRÊS câmeras, e o override por lado também é vencido pelo modo.

    `PROB_..._DIR` é o escape para simular uma câmera suja em campo. Se ele
    sobrevivesse ao modo apresentação, uma variável esquecida num teste
    reintroduziria exatamente a falha que o modo foi ligado para não ter — e o
    sintoma (divergência de SKU num slot só) é indistinguível de medicamento
    trocado de verdade.
    """
    sim = carregar_simulador("vision", env={
        "MODO_APRESENTACAO":                "1",
        "PROB_FALHA_LEITURA_DISPENSER":     CERTEZA,
        "PROB_DIVERGENCIA_DISPENSER":       CERTEZA,
        "PROB_FALHA_LEITURA_DISPENSER_DIR": CERTEZA,
        "PROB_DIVERGENCIA_DISPENSER_ESQ":   CERTEZA,
        "PROB_FALHA_LEITURA_MESA":          CERTEZA,
        "PROB_DIVERGENCIA_MESA":            CERTEZA,
        "T_SCAN_DISPENSER":                 "0",
        "T_SCAN_MESA":                      "0",
    })
    for cam in sim.modulo.CAMERAS_DISPENSER.values():
        assert (cam.prob_falha, cam.prob_divergencia) == (0.0, 0.0), cam.nome
    assert sim.modulo.CAMERA_MESA.prob_falha == 0.0
    assert sim.modulo.CAMERA_MESA.prob_divergencia == 0.0

    n_slots = sim.modulo.NUM_SLOTS
    for i in range(N_EXEC):
        slot = (i % n_slots) + 1
        sim.modulo._do_capturar_dispenser(slot, "OS-%d" % i, "SKU-1", "Dipirona", 3)
        sim.modulo._do_capturar_mesa(slot, "OS-%d" % i, 3, 0.0, 0.0)

    assert sim.eventos_do_tipo(
        "leitura_dispenser_falha", "leitura_dispenser_divergencia",
        "leitura_mesa_falha", "leitura_mesa_divergencia",
    ) == []
    assert len(sim.eventos_do_tipo("leitura_dispenser_ok")) == N_EXEC
    assert len(sim.eventos_do_tipo("leitura_mesa_ok")) == N_EXEC
    # E as duas câmeras de fileira continuam sendo usadas — o modo desliga o
    # acaso, não a geometria.
    lados = {e["camera"] for e in sim.eventos_do_tipo("leitura_dispenser_ok")}
    assert lados == {sim.modulo.CAM_ESQ, sim.modulo.CAM_DIR}


def test_vision_override_por_lado_continua_valendo_sem_o_modo(carregar_simulador):
    """Controle do override: sem o modo, `_DIR` sobrescreve só a câmera direita."""
    sim = carregar_simulador("vision", env={
        "MODO_APRESENTACAO":                "0",
        "PROB_FALHA_LEITURA_DISPENSER":     "0.0",
        "PROB_FALHA_LEITURA_DISPENSER_DIR": CERTEZA,
        "T_SCAN_DISPENSER":                 "0",
    })
    assert sim.modulo.CAMERAS_DISPENSER[sim.modulo.CAM_ESQ].prob_falha == 0.0
    assert sim.modulo.CAMERAS_DISPENSER[sim.modulo.CAM_DIR].prob_falha == 1.0


def test_weight_sem_falha_e_sem_ruido_no_modo_apresentacao(carregar_simulador):
    """Sensor e ruído: a balança só diverge quando a dispensa realmente erra.

    `RUIDO_G` entra aqui porque é fonte de falha de VERDADE, não enfeite: com
    σ grande contra o peso esperado, a tolerância de 5% vira sorteio e a
    `peso_divergencia` sai sem causa nenhuma. Um σ de 10 000 g é a versão
    decisiva do mesmo argumento da probabilidade 1.0.
    """
    sim = carregar_simulador("weight", env={
        "MODO_APRESENTACAO": "1",
        "PROB_ERRO_SENSOR":  CERTEZA,
        "RUIDO_G":           "10000",
        "T_LEITURA":         "0",
        "T_TARA":            "0",
    })
    assert sim.modulo.PROB_ERRO_SENSOR == 0.0
    assert sim.modulo.RUIDO_G == 0.0

    sim.modulo._do_tara("OS-1")
    for i in range(N_EXEC):
        sim.modulo._do_pesar("OS-1", (i % 8) + 1, 10, 50.0, quantidade_real=10)

    assert sim.eventos_do_tipo("erro_sensor") == []
    assert sim.eventos_do_tipo("peso_divergencia") == []
    assert len(sim.eventos_do_tipo("tara_ok")) == 1
    assert len(sim.eventos_do_tipo("peso_ok")) == N_EXEC


def test_weight_divergencia_real_ainda_sai_no_modo_apresentacao(carregar_simulador):
    """O modo desliga o ACASO, não a detecção.

    Com o ruído zerado, a balança segue divergindo quando a quantidade que caiu
    na mesa não bate com a esperada — que é o caso de falha mecânica de verdade,
    e o que a injeção de falha da demo vai provocar de propósito.
    """
    sim = carregar_simulador("weight", env={
        "MODO_APRESENTACAO": "1",
        "T_LEITURA":         "0",
        "T_TARA":            "0",
    })
    sim.modulo._do_tara("OS-1")
    sim.modulo._do_pesar("OS-1", 1, 10, 50.0, quantidade_real=8)
    assert sim.eventos_do_tipo("peso_divergencia")


def test_weight_ruido_continua_valendo_sem_o_modo(carregar_simulador):
    sim = carregar_simulador("weight", env={
        "MODO_APRESENTACAO": "0",
        "PROB_ERRO_SENSOR":  "0.0",
        "RUIDO_G":           "10000",
        "T_LEITURA":         "0",
        "T_TARA":            "0",
    })
    assert sim.modulo.RUIDO_G == 10000.0
    sim.modulo._do_tara("OS-1")
    for _ in range(10):
        sim.modulo._do_pesar("OS-1", 1, 10, 50.0, quantidade_real=10)
    assert sim.eventos_do_tipo("peso_divergencia")


def test_cnc_expoe_o_modo_mesmo_sem_sortear_falha(carregar_simulador):
    """A CNC não sorteia falha em modo nenhum — e mesmo assim lê a variável.

    O serviço que não sabe dizer em que modo está é o que faz alguém duvidar do
    conjunto na hora da apresentação. O teste cobra a leitura, não um
    comportamento que não existe.
    """
    sim = carregar_simulador("cnc", env={"MODO_APRESENTACAO": "1"})
    assert sim.modulo.MODO_APRESENTACAO is True


# ── 2. FATOR_VELOCIDADE escala tempos e timeouts ──────────────────────────────

def _sleeps(sim, monkeypatch) -> list:
    """Substitui `time.sleep` DO MÓDULO e devolve a lista de durações pedidas."""
    registro: list = []
    monkeypatch.setattr(sim.modulo.time, "sleep", registro.append)
    return registro


@pytest.mark.parametrize("fator", ["0.5", "1.0", "2.0", "4.0"])
def test_dispenser_tempos_efetivos_escalam(carregar_simulador, monkeypatch, fator):
    sim = carregar_simulador("dispenser", env={
        "FATOR_VELOCIDADE":   fator,
        "T_CARGA_UNID":       "0.3",
        "T_DISPENSA_UNID":    "0.5",
        "PROB_ERRO_MECANICO": "0",
    })
    f = float(fator)
    assert sim.modulo.T_CARGA_UNID    == pytest.approx(0.3 * f)
    assert sim.modulo.T_DISPENSA_UNID == pytest.approx(0.5 * f)

    # Medido pelos sleeps de verdade: constante certa com sleep de outro valor
    # (um literal esquecido no caminho) passaria despercebido.
    registro = _sleeps(sim, monkeypatch)
    sim.modulo._do_carregar(1, "Dipirona", "SKU-1", "analgesico", 4, "OS-1")
    assert registro == pytest.approx([0.3 * f] * 4)

    registro.clear()
    sim.modulo._do_dispensar(1, "OS-1")
    assert registro == pytest.approx([0.5 * f] * 4)


@pytest.mark.parametrize("fator", ["0.5", "2.0"])
def test_vision_tempos_efetivos_escalam(carregar_simulador, monkeypatch, fator):
    sim = carregar_simulador("vision", env={
        "FATOR_VELOCIDADE":              fator,
        "T_SCAN_DISPENSER":              "1.5",
        "T_SCAN_MESA":                   "2.0",
        "T_SCAN_DISPENSER_DIR":          "3.0",
        "PROB_FALHA_LEITURA_DISPENSER":  "0",
        "PROB_DIVERGENCIA_DISPENSER":    "0",
        "PROB_FALHA_LEITURA_MESA":       "0",
        "PROB_DIVERGENCIA_MESA":         "0",
    })
    f = float(fator)
    esq  = sim.modulo.CAMERAS_DISPENSER[sim.modulo.CAM_ESQ]
    dire = sim.modulo.CAMERAS_DISPENSER[sim.modulo.CAM_DIR]
    assert esq.t_scan  == pytest.approx(1.5 * f)
    # O override por lado escala junto — não é exceção ao fator global.
    assert dire.t_scan == pytest.approx(3.0 * f)
    assert sim.modulo.CAMERA_MESA.t_scan == pytest.approx(2.0 * f)

    registro = _sleeps(sim, monkeypatch)
    sim.modulo._do_capturar_dispenser(1, "OS-1", "SKU-1", "Dipirona", 3)
    sim.modulo._do_capturar_mesa(1, "OS-1", 3, 0.0, 0.0)
    assert registro == pytest.approx([1.5 * f, 2.0 * f])


@pytest.mark.parametrize("fator", ["0.5", "2.0"])
def test_weight_tempos_efetivos_escalam(carregar_simulador, monkeypatch, fator):
    sim = carregar_simulador("weight", env={
        "FATOR_VELOCIDADE": fator,
        "T_LEITURA":        "1.5",
        "T_TARA":           "0.5",
        "PROB_ERRO_SENSOR": "0",
    })
    f = float(fator)
    registro = _sleeps(sim, monkeypatch)
    sim.modulo._do_tara("OS-1")
    sim.modulo._do_pesar("OS-1", 1, 10, 50.0, quantidade_real=10)
    # O 0.5 da tara era um literal dentro de `_do_tara`; escalar só T_LEITURA
    # deixaria um passo do ciclo fora do compasso do resto da célula.
    assert registro == pytest.approx([0.5 * f, 1.5 * f])


@pytest.mark.parametrize("fator", ["0.5", "1.0", "2.0"])
def test_cnc_velocidade_recebe_o_fator_invertido(carregar_simulador, fator):
    """Fator 2.0 = na metade da velocidade = MENOS mm/s.

    É a única inversão de sinal do sistema, e errá-la faria a CNC acelerar
    justamente quando se pediu calma para narrar o movimento.
    """
    sim = carregar_simulador("cnc", env={"FATOR_VELOCIDADE": fator, "VEL_MM_S": "80"})
    f = float(fator)
    assert sim.modulo.VELOCIDADE_MM_S == pytest.approx(80.0 / f)
    assert sim.modulo.DURACAO_MIN_S   == pytest.approx(1.0 * f)
    # `INTERVALO` é cadência de publicação, não tempo físico: não escala.
    assert sim.modulo.INTERVALO_PUB == pytest.approx(0.5)


def test_cnc_mais_lenta_leva_mais_tempo(carregar_simulador, monkeypatch):
    """Duração do movimento, e não só a constante: 2× mais lento, 2× o tempo."""
    duracoes = {}
    for fator in ("1.0", "2.0"):
        sim = carregar_simulador("cnc", env={
            "FATOR_VELOCIDADE": fator, "VEL_MM_S": "80", "INTERVALO": "0.5",
        })
        with monkeypatch.context() as mp:
            registro: list = []
            mp.setattr(sim.modulo.time, "sleep", registro.append)
            sim.modulo._mover_para(3, 800.0, 0.0, "OS-1", 1, 1)
            duracoes[fator] = sum(registro)

    assert duracoes["2.0"] == pytest.approx(2 * duracoes["1.0"], rel=0.2)


@pytest.mark.parametrize("fator,esperado", [("0.5", 1.0), ("1.0", 1.0),
                                            ("2.0", 2.0), ("4.0", 4.0)])
def test_timeouts_do_orquestrador_acompanham_o_fator(fator, esperado):
    """O erro mais provável da task: célula lenta com teto de espera parado.

    E a assimetria: acelerar NÃO encolhe o timeout. Ele é teto de espera por um
    adapter travado, não parte do ciclo — encolhê-lo não economiza um segundo
    de demo e come a folga do custo FIXO (`_post` retentando 3× com sleep(1),
    MySQL, rede), que não escala com fator nenhum.
    """
    cfg = _carregar_config({"FATOR_VELOCIDADE": fator})
    assert cfg.settings.TIMEOUT_CARREGAMENTO    == pytest.approx(180 * esperado)
    assert cfg.settings.TIMEOUT_POSICIONAMENTO  == pytest.approx(120 * esperado)
    assert cfg.settings.TIMEOUT_DISPENSA        == pytest.approx(120 * esperado)
    assert cfg.settings.TIMEOUT_VISAO_DISPENSER == pytest.approx(30 * esperado)
    assert cfg.settings.TIMEOUT_VISAO_MESA      == pytest.approx(30 * esperado)
    assert cfg.settings.TIMEOUT_PESO            == pytest.approx(15 * esperado)
    assert cfg.settings.TIMEOUT_LIMPEZA         == pytest.approx(60 * esperado)
    # O CRONOGRAMA do ciclo da mesa escala junto, e por uma razão mais direta
    # que a dos timeouts: ele não é teto de espera, é o relógio que decide
    # QUANDO o `dispensar` sai. Desacelerar a célula sem desacelerá-lo faria o
    # comando sair com a mesa ainda em trânsito — comprimido no chão, que é a
    # única forma de o modo apresentação estragar esta feature.
    assert cfg.settings.CNC_TETO_TRAJETO_S      == pytest.approx(2.5 * esperado)
    assert cfg.settings.CNC_MARGEM_CHEGADA_S    == pytest.approx(0.75 * esperado)
    assert cfg.settings.DISPENSA_S_POR_UNIDADE  == pytest.approx(1.0 * esperado)
    assert cfg.settings.DISPENSA_FOLGA_S        == pytest.approx(1.0 * esperado)
    # O fator publicado é o cru — quem só aumenta é a folga do timeout.
    assert cfg.settings.FATOR_VELOCIDADE == pytest.approx(float(fator))


def test_timeout_cobre_o_ciclo_desacelerado():
    """Teste de suficiência: o teto tem que caber o ciclo que ele vigia.

    Carregar o slot mais cheio de um template (15 unidades) a 0.3 s/unidade,
    com o fator aplicado dos dois lados, precisa continuar abaixo do
    `TIMEOUT_CARREGAMENTO`. É a conta que a task pede para não errar, feita em
    vez de argumentada.
    """
    for fator in (0.5, 1.0, 2.0, 4.0, 10.0):
        cfg = _carregar_config({"FATOR_VELOCIDADE": str(fator)})
        carga_s = 15 * 0.3 * fator
        assert carga_s < cfg.settings.TIMEOUT_CARREGAMENTO, fator


def test_estado_do_central_publica_o_modo(carregar_central):
    """O painel precisa dizer qual modo está valendo, ao vivo."""
    central = carregar_central(env={
        "MODO_APRESENTACAO": "1", "FATOR_VELOCIDADE": "2.0",
    })
    assert central.modulo._estado["modo_apresentacao"] is True
    assert central.modulo._estado["fator_velocidade"] == pytest.approx(2.0)


# ── 3. As cinco cópias concordam ──────────────────────────────────────────────

@pytest.fixture
def as_cinco_copias(carregar_simulador):
    """As cinco implementações de `_modo_apresentacao`/`_fator_velocidade`."""
    fontes = {nome: carregar_simulador(nome).modulo
              for nome in ("cnc", "dispenser", "vision", "weight")}
    fontes["central"] = _carregar_config()
    return fontes


BOOLEANOS = [
    ("1", True), ("true", True), ("TRUE", True), ("True", True),
    ("yes", True), ("sim", True), ("on", True), ("  1  ", True),
    ("0", False), ("false", False), ("no", False), ("nao", False),
    ("", False), ("2", False), ("talvez", False), ("off", False),
]


@pytest.mark.parametrize("bruto,esperado", BOOLEANOS)
def test_modo_apresentacao_le_igual_nos_cinco(as_cinco_copias, monkeypatch,
                                              bruto, esperado):
    monkeypatch.setenv("MODO_APRESENTACAO", bruto)
    lidos = {nome: mod._modo_apresentacao() for nome, mod in as_cinco_copias.items()}
    assert set(lidos.values()) == {esperado}, lidos


def test_modo_apresentacao_ausente_e_falso_nos_cinco(as_cinco_copias, monkeypatch):
    monkeypatch.delenv("MODO_APRESENTACAO", raising=False)
    assert not any(mod._modo_apresentacao() for mod in as_cinco_copias.values())


FATORES = [
    ("1.0", 1.0), ("0.5", 0.5), ("2", 2.0), ("0.1", 0.1), ("10", 10.0),
    # Fora da faixa e lixo caem no default, nunca em comportamento silencioso.
    ("0", 1.0), ("-1", 1.0), ("0.05", 1.0), ("10.1", 1.0),
    ("", 1.0), ("rapido", 1.0), ("1,5", 1.0),
]


@pytest.mark.parametrize("bruto,esperado", FATORES)
def test_fator_velocidade_le_igual_nos_cinco(as_cinco_copias, monkeypatch,
                                             bruto, esperado):
    monkeypatch.setenv("FATOR_VELOCIDADE", bruto)
    lidos = {nome: mod._fator_velocidade() for nome, mod in as_cinco_copias.items()}
    assert all(v == pytest.approx(esperado) for v in lidos.values()), lidos


def test_fator_velocidade_ausente_vale_um_nos_cinco(as_cinco_copias, monkeypatch):
    monkeypatch.delenv("FATOR_VELOCIDADE", raising=False)
    for nome, mod in as_cinco_copias.items():
        assert mod._fator_velocidade() == pytest.approx(1.0), nome


def test_todo_servico_declara_os_dois_controles(as_cinco_copias):
    """Serviço que não lê as variáveis é serviço que ignora o modo em silêncio."""
    for nome, mod in as_cinco_copias.items():
        assert hasattr(mod, "_modo_apresentacao"), nome
        assert hasattr(mod, "_fator_velocidade"), nome


def test_todo_servico_da_stack_recebe_as_duas_variaveis():
    """O compose é quem faz o controle ser global — sem ele, não é controle.

    Os quatro simuladores mais o central. Adapters de fora de propósito: eles
    só encaminham comando e evento, e os `TIMEOUT_CMD`/`TIMEOUT_EVENT` deles são
    tetos de HTTP para uma chamada que o simulador responde na hora (a operação
    roda em thread), não tempo simulado.
    """
    import yaml

    compose = yaml.safe_load(
        (RAIZ_REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    esperados = {"central-computer", "cnc-simulator", "dispenser-simulator",
                 "vision-simulator", "weight-simulator"}
    for nome in esperados:
        env = compose["services"][nome].get("environment") or {}
        assert "MODO_APRESENTACAO" in env, nome
        assert "FATOR_VELOCIDADE" in env, nome
        # Interpolado do `.env`, com default: a stack sobe sem as variáveis.
        assert str(env["MODO_APRESENTACAO"]).startswith("${MODO_APRESENTACAO:-")
        assert str(env["FATOR_VELOCIDADE"]).startswith("${FATOR_VELOCIDADE:-")
