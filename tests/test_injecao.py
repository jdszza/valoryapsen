# -*- coding: utf-8 -*-
"""
Injeção de falha sob demanda — o gatilho armado no console, consumido uma vez.

Quatro frentes:

1. **O módulo `injecao` sozinho** — armar, desarmar, validar, e o one-shot sob
   concorrência. `consumir` é check-and-pop: se fosse ler-decidir-apagar, os
   comandos dos oito slots saindo em `gather` poderiam ler o mesmo gatilho e a
   falha sairia em dois lugares.

2. **O orquestrador injeta no comando certo** — e SÓ nele. O gatilho de peso no
   D3 não pode viajar no comando de captura, nem no comando do D4.

3. **Cada simulador honra o campo, com as probabilidades ZERADAS.** É o
   requisito que separa esta task da 6: o modo apresentação desliga o acaso, a
   injeção liga o que se escolheu. Se os testes rodassem com as probabilidades
   default, "saiu divergência" não provaria de onde ela veio.

4. **Os nomes dos tipos batem nas quatro cópias.** O catálogo do central, os
   três simuladores e o duplo de adapter da suíte escrevem as mesmas strings.
   Uma divergência aqui não quebra nada visivelmente: vira gatilho armado que
   nunca dispara, que é o pior modo de falhar para uma feature cuja razão de
   existir é não depender de sorte.
"""
import asyncio
import importlib.util
import sys
import threading
from pathlib import Path

import pytest
from conftest import TEMPLATE_PADRAO

RAIZ_REPO = Path(__file__).resolve().parent.parent
CENTRAL_DIR = RAIZ_REPO / "central-computer"


@pytest.fixture
def inj(carregar_orquestrador):
    """Módulo `injecao.py` tal como o orquestrador o enxerga, já zerado."""
    orq = carregar_orquestrador()
    orq.injecao.resetar()
    return orq.injecao


def _item(med: str, qtd: int = 10) -> dict:
    return {"medicamento": med, "sku": f"{med[:3].upper()}-1", "categoria": "geral",
            "quantidade": qtd}


def _payload_os(os_id: str, *itens) -> dict:
    # `template_id` é o que diz QUAL das dez ordens padrão esta é, e é dele que
    # o orquestrador tira a letra da receita gravada na mesa. Sem ele a OS é
    # legitimamente abortada com `receita_nao_mapeada` antes do primeiro mover
    # — o caso tem teste próprio; aqui a OS é uma ordem padrão de verdade.
    return {"os_id": os_id, "descricao": "teste",
            "template_id": TEMPLATE_PADRAO,
            "medicamentos": list(itens) or [_item("Dipirona")]}


def _status_gravados(orq) -> list:
    return [c["args"] for c in orq.banco.chamadas_de("atualizar_status_ordem")]


def _liberar_trava_automaticamente(orq, uma_vez: bool = False) -> list:
    """Faz o papel do supervisor: libera a trava assim que ela aparece.

    Quase todo tipo injetável é BLOQUEANTE de propósito — é o Triple Check
    fazendo o que deve. Sem alguém do outro lado, `_processar_os` fica parado em
    `evento_lib.wait()` para sempre e o teste vira um travamento da suíte, não
    uma falha. O clique acontece de dentro do broadcast, que é o instante exato
    em que a trava aparece na tela (ver os testes de corrida em
    `test_orchestrator.py`).

    Devolve a lista de liberações, para o teste cobrar QUANTAS vezes travou.
    """
    liberacoes: list = []

    def _broadcast_e_liberar():
        if orq.estado["trava"]["ativa"] and not (uma_vez and liberacoes):
            liberacoes.append(orq.modulo.liberar_trava("supervisor"))

    orq.modulo._broadcast_fn = _broadcast_e_liberar
    return liberacoes


# ── 1. O módulo sozinho ───────────────────────────────────────────────────────

def test_nada_armado_por_padrao(inj):
    """Estado de boot é não injetar nada — como a pausa do gerador."""
    assert inj.armada() is None
    assert inj.consumir(1, "/comandos/dispensar") is None


def test_armar_e_consumir_uma_unica_vez(inj):
    inj.armar(inj.TIPO_DIVERGENCIA_PESO, 3)
    assert inj.armada()["tipo"] == inj.TIPO_DIVERGENCIA_PESO

    assert inj.consumir(3, "/comandos/pesar") == inj.TIPO_DIVERGENCIA_PESO
    # One-shot: a segunda pesagem do mesmo slot já é normal.
    assert inj.consumir(3, "/comandos/pesar") is None
    assert inj.armada() is None


def test_consumo_exige_o_slot_certo(inj):
    inj.armar(inj.TIPO_DIVERGENCIA_PESO, 3)
    assert inj.consumir(4, "/comandos/pesar") is None
    # E o gatilho continua armado, esperando o D3.
    assert inj.armada()["slot_id"] == 3


def test_consumo_exige_o_comando_certo(inj):
    """Peso não sai pela câmera. O mapa vem de `TIPOS`, não de lista à parte."""
    inj.armar(inj.TIPO_DIVERGENCIA_PESO, 3)
    assert inj.consumir(3, "/comandos/capturar/dispenser") is None
    assert inj.consumir(3, "/comandos/capturar/mesa") is None
    assert inj.consumir(3, "/comandos/carregar") is None
    assert inj.consumir(3, "/comandos/pesar") == inj.TIPO_DIVERGENCIA_PESO


def test_armar_substitui_o_anterior(inj):
    """Um gatilho de cada vez: dois armados tornariam a trava ambígua."""
    inj.armar(inj.TIPO_DIVERGENCIA_PESO, 3)
    inj.armar(inj.TIPO_SKU_DISPENSER, 5)
    assert inj.armada()["tipo"] == inj.TIPO_SKU_DISPENSER
    assert inj.armada()["slot_id"] == 5
    assert inj.consumir(3, "/comandos/pesar") is None


def test_desarmar_devolve_o_que_estava_e_zera(inj):
    inj.armar(inj.TIPO_SKU_DISPENSER, 2)
    anterior = inj.desarmar()
    assert anterior["tipo"] == inj.TIPO_SKU_DISPENSER
    assert inj.armada() is None
    assert inj.desarmar() is None


@pytest.mark.parametrize("tipo,slot", [
    ("tipo_que_nao_existe", 1),
    ("sku_dispenser", 0),
    ("sku_dispenser", 99),
    ("sku_dispenser", "D3"),
])
def test_pedido_invalido_levanta_injecao_invalida(inj, tipo, slot):
    """422 na rota, não 500: o console manda o que o operador digitou."""
    with pytest.raises(inj.InjecaoInvalida):
        inj.armar(tipo, slot)
    assert inj.armada() is None


def test_one_shot_sobrevive_a_oito_consumidores_simultaneos(inj):
    """O cenário real: os comandos dos 8 slots saem em `gather`.

    `consumir` faz check-and-pop sob lock. Ler, decidir e apagar em passos
    separados deixaria dois slots verem o mesmo gatilho armado — e a falha
    sairia em dois lugares, que é a surpresa que esta feature existe para
    eliminar. Threads, e não corrotinas, porque é o lock que está sendo
    testado.
    """
    inj.armar(inj.TIPO_FALHA_MECANICA, 4)
    vencedores: list = []
    largada = threading.Event()

    def _tentar():
        largada.wait()
        if inj.consumir(4, "/comandos/dispensar"):
            vencedores.append(threading.current_thread().name)

    threads = [threading.Thread(target=_tentar, name=f"t{i}") for i in range(8)]
    for t in threads:
        t.start()
    largada.set()
    for t in threads:
        t.join()

    assert len(vencedores) == 1, vencedores


def test_catalogo_descreve_todos_os_tipos(inj):
    """O console monta o seletor daqui — campo faltando é opção sem rótulo."""
    catalogo = inj.catalogo()
    assert {c["tipo"] for c in catalogo} == set(inj.TIPOS)
    for entrada in catalogo:
        for campo in ("rotulo", "efeito", "comando", "simulador", "evento",
                      "bloqueante"):
            assert entrada.get(campo) not in (None, ""), (entrada["tipo"], campo)


def test_os_cinco_tipos_pedidos_existem(inj):
    """A lista da task, contra o catálogo — nem a mais, nem a menos."""
    assert set(inj.TIPOS) == {
        "sku_dispenser",             # SKU errado na câmera de dispenser
        "falha_leitura_dispenser",   # falha de leitura (alarme não-bloqueante)
        "divergencia_mesa",          # divergência de contagem na câmera da balança
        "divergencia_peso",          # divergência de peso na balança
        "falha_mecanica_dispenser",  # dispensa com quantidade a menos
    }
    # A não-bloqueante é justamente a falha de leitura — é o que a distingue de
    # uma divergência, aqui e no `avaliar_triple_check`.
    assert inj.TIPOS["falha_leitura_dispenser"]["bloqueante"] is False


# ── 2. O orquestrador injeta no comando certo ─────────────────────────────────

def test_comando_normal_nao_carrega_o_campo(carregar_orquestrador):
    """Sem gatilho, o corpo sai byte a byte como saía antes da feature."""
    orq = carregar_orquestrador()
    orq.injecao.resetar()

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-1")))

    for comando in orq.adapter.chamadas:
        assert "injetar_falha" not in comando["payload"], comando["url"]


def test_gatilho_viaja_no_comando_de_pesagem_do_slot_armado(carregar_orquestrador):
    orq = carregar_orquestrador()
    orq.injecao.resetar()
    orq.injecao.armar("divergencia_peso", 1)
    _liberar_trava_automaticamente(orq)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-2")))

    (pesagem,) = orq.adapter.comandos("/comandos/pesar")
    assert pesagem["injetar_falha"] == "divergencia_peso"
    # E em nenhum outro comando.
    outros = [c for c in orq.adapter.chamadas
              if not c["url"].endswith("/comandos/pesar")]
    assert all("injetar_falha" not in c["payload"] for c in outros)


def test_gatilho_de_outro_slot_nao_e_consumido(carregar_orquestrador):
    """Duas OS de um slot só, gatilho armado no D2: nada sai e nada se perde."""
    orq = carregar_orquestrador()
    orq.injecao.resetar()
    orq.injecao.armar("falha_mecanica_dispenser", 2)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-3")))

    (dispensa,) = orq.adapter.comandos("/comandos/dispensar")
    assert dispensa["dispenser_id"] == 1
    assert "injetar_falha" not in dispensa
    assert orq.injecao.armada()["slot_id"] == 2, "gatilho foi perdido sem disparar"


def test_gatilho_consumido_e_publicado_no_estado(carregar_orquestrador):
    """A tela do console tem que ver o gatilho aparecer e sumir."""
    orq = carregar_orquestrador()
    orq.injecao.resetar()
    orq.injecao.armar("divergencia_peso", 1)
    orq.modulo._publicar_injecao()
    assert orq.estado["falha_armada"]["tipo"] == "divergencia_peso"
    _liberar_trava_automaticamente(orq)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-4")))

    assert orq.estado["falha_armada"] is None


def test_falha_mecanica_injetada_trava_a_os(carregar_orquestrador):
    """O caso mais completo: falha física de verdade, vista pela balança.

    Uma unidade a menos de 10 é 10% de desvio contra uma tolerância de 5% —
    a balança diverge sozinha e, com o limiar padrão de 1, a OS trava. É o que
    torna este o tipo mais útil de armar para explicar o Triple Check.
    """
    orq = carregar_orquestrador()
    orq.injecao.resetar()
    orq.injecao.armar("falha_mecanica_dispenser", 1)
    liberacoes = _liberar_trava_automaticamente(orq, uma_vez=True)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-5")))

    assert liberacoes == [True], "a falha injetada não travou a OS"
    (dispensa,) = orq.adapter.comandos("/comandos/dispensar")
    assert dispensa["injetar_falha"] == "falha_mecanica_dispenser"
    # Saída em status terminal, como toda saída de `_processar_os`.
    assert _status_gravados(orq)[-1][1] in ("concluida", "erro")


def test_sku_injetado_trava_e_o_rescan_volta_limpo(carregar_orquestrador):
    """One-shot no cenário que mais importa: o laço de re-scan tem que fechar.

    Se o gatilho não se desarmasse ao ser consumido, o re-scan depois da
    liberação voltaria divergente de novo e a OS ficaria travando para sempre —
    a trava viraria um laço infinito na frente da banca.
    """
    orq = carregar_orquestrador()
    orq.injecao.resetar()
    orq.injecao.armar("sku_dispenser", 1)
    liberacoes = _liberar_trava_automaticamente(orq)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-6")))

    assert liberacoes == [True], "travou zero vezes, ou travou mais de uma"
    scans = orq.adapter.comandos("/comandos/capturar/dispenser")
    assert len(scans) == 2, "esperado scan inicial + um re-scan"
    assert scans[0]["injetar_falha"] == "sku_dispenser"
    assert "injetar_falha" not in scans[1], "o re-scan reinjetou a falha"
    assert _status_gravados(orq)[-1] == ("OS-6", "concluida")


def test_falha_de_leitura_injetada_nao_trava(carregar_orquestrador):
    """Fonte que deixou de confirmar ≠ fonte que contradisse.

    É a mesma regra que o Triple Check já aplica; o tipo não-bloqueante existe
    para mostrar o OUTRO lado dela na apresentação.
    """
    orq = carregar_orquestrador()
    orq.injecao.resetar()
    orq.injecao.armar("falha_leitura_dispenser", 1)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-7")))

    assert orq.estado["trava"]["ativa"] is False
    assert _status_gravados(orq)[-1] == ("OS-7", "concluida")
    assert len(orq.adapter.comandos("/comandos/capturar/dispenser")) == 1


def test_divergencia_de_mesa_injetada_trava(carregar_orquestrador):
    orq = carregar_orquestrador()
    orq.injecao.resetar()
    orq.injecao.armar("divergencia_mesa", 1)
    liberacoes = _liberar_trava_automaticamente(orq, uma_vez=True)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-8")))

    assert liberacoes == [True]
    (mesa,) = orq.adapter.comandos("/comandos/capturar/mesa")
    assert mesa["injetar_falha"] == "divergencia_mesa"


def test_a_os_seguinte_volta_ao_normal(carregar_orquestrador):
    """Depois do tiro, a planta é a de sempre — sem resíduo de gatilho."""
    orq = carregar_orquestrador()
    orq.injecao.resetar()
    orq.injecao.armar("divergencia_peso", 1)
    _liberar_trava_automaticamente(orq)

    asyncio.run(orq.modulo._processar_os(_payload_os("OS-9")))
    n_antes = len(orq.adapter.chamadas)
    asyncio.run(orq.modulo._processar_os(_payload_os("OS-10")))

    segunda = orq.adapter.chamadas[n_antes:]
    assert segunda, "a segunda OS não rodou"
    assert all("injetar_falha" not in c["payload"] for c in segunda)
    assert _status_gravados(orq)[-1] == ("OS-10", "concluida")


# ── 3. Os simuladores honram o campo, COM as probabilidades zeradas ───────────
#
# Todo carregamento abaixo liga `MODO_APRESENTACAO`. É o requisito explícito da
# task e também o que dá força ao teste: com as probabilidades default, "saiu
# divergência" não provaria de onde ela veio.

MODO_DEMO = {"MODO_APRESENTACAO": "1"}


def test_simulador_dispenser_solta_uma_a_menos(carregar_simulador):
    sim = carregar_simulador("dispenser", env={
        **MODO_DEMO, "T_CARGA_UNID": "0", "T_DISPENSA_UNID": "0",
    })
    sim.modulo._do_carregar(1, "Dipirona", "SKU-1", "analgesico", 10, "OS-1")
    sim.modulo._do_dispensar(1, "OS-1", injetar_falha="falha_mecanica_dispenser")

    (evento,) = sim.eventos_do_tipo("dispensado")
    assert evento["quantidade_alvo"] == 10
    assert evento["quantidade_dispensada"] == 9
    assert evento["falha_mecanica"] is True
    assert evento["falha_injetada"] is True


def test_simulador_dispenser_sem_injecao_dispensa_tudo(carregar_simulador):
    """Controle: no modo apresentação, sem gatilho, nada falha."""
    sim = carregar_simulador("dispenser", env={
        **MODO_DEMO, "T_CARGA_UNID": "0", "T_DISPENSA_UNID": "0",
    })
    sim.modulo._do_carregar(1, "Dipirona", "SKU-1", "analgesico", 10, "OS-1")
    sim.modulo._do_dispensar(1, "OS-1")

    (evento,) = sim.eventos_do_tipo("dispensado")
    assert evento["quantidade_dispensada"] == 10
    assert evento["falha_injetada"] is False


def test_simulador_dispenser_ignora_injecao_desconhecida(carregar_simulador):
    """Typo do console não pode virar dispensa silenciosamente diferente."""
    sim = carregar_simulador("dispenser", env={
        **MODO_DEMO, "T_CARGA_UNID": "0", "T_DISPENSA_UNID": "0",
    })
    sim.modulo._do_carregar(1, "Dipirona", "SKU-1", "analgesico", 10, "OS-1")
    sim.modulo._do_dispensar(1, "OS-1", injetar_falha="divergencia_peso")

    (evento,) = sim.eventos_do_tipo("dispensado")
    assert evento["quantidade_dispensada"] == 10
    assert evento["falha_injetada"] is False


def test_simulador_vision_injeta_sku_errado(carregar_simulador):
    sim = carregar_simulador("vision", env={**MODO_DEMO, "T_SCAN_DISPENSER": "0"})
    sim.modulo._do_capturar_dispenser(1, "OS-1", "SKU-1", "Dipirona", 10,
                                      injetar_falha="sku_dispenser")

    (evento,) = sim.eventos_do_tipo("leitura_dispenser_divergencia")
    assert evento["sku_esperado"] == "SKU-1"
    assert evento["sku_lido"] == sim.modulo.SKU_INJETADO
    assert evento["match_sku"] is False
    assert evento["falha_injetada"] is True
    # A câmera certa continua sendo escolhida pelo slot — a injeção não mexe
    # na geometria da bancada.
    assert evento["camera"] == sim.modulo.camera_do_slot(1)


def test_simulador_vision_injeta_falha_de_leitura(carregar_simulador):
    sim = carregar_simulador("vision", env={**MODO_DEMO, "T_SCAN_DISPENSER": "0"})
    sim.modulo._do_capturar_dispenser(5, "OS-1", "SKU-1", "Dipirona", 10,
                                      injetar_falha="falha_leitura_dispenser")

    (evento,) = sim.eventos_do_tipo("leitura_dispenser_falha")
    assert evento["motivo"] == "injecao_demonstracao"
    assert evento["falha_injetada"] is True
    assert evento["camera"] == sim.modulo.camera_do_slot(5)


def test_simulador_vision_injeta_divergencia_de_mesa(carregar_simulador):
    sim = carregar_simulador("vision", env={**MODO_DEMO, "T_SCAN_MESA": "0"})
    sim.modulo._do_capturar_mesa(3, "OS-1", 10, 0.0, 0.0,
                                 injetar_falha="divergencia_mesa")

    (evento,) = sim.eventos_do_tipo("leitura_mesa_divergencia")
    # UMA a menos, sempre: é o quadro de um produto que não saiu. Contar a mais
    # seria a câmera vendo o que não existe — possível, mas confuso de explicar.
    assert evento["quantidade_detectada"] == 9
    assert evento["delta"] == -1
    assert evento["falha_injetada"] is True


def test_simulador_vision_sem_injecao_le_certo(carregar_simulador):
    sim = carregar_simulador("vision", env={
        **MODO_DEMO, "T_SCAN_DISPENSER": "0", "T_SCAN_MESA": "0",
    })
    sim.modulo._do_capturar_dispenser(1, "OS-1", "SKU-1", "Dipirona", 10)
    sim.modulo._do_capturar_mesa(1, "OS-1", 10, 0.0, 0.0)

    assert len(sim.eventos_do_tipo("leitura_dispenser_ok")) == 1
    assert len(sim.eventos_do_tipo("leitura_mesa_ok")) == 1


def test_simulador_vision_ignora_injecao_desconhecida(carregar_simulador):
    sim = carregar_simulador("vision", env={**MODO_DEMO, "T_SCAN_DISPENSER": "0"})
    sim.modulo._do_capturar_dispenser(1, "OS-1", "SKU-1", "Dipirona", 10,
                                      injetar_falha="divergencia_peso")
    assert sim.eventos_do_tipo("leitura_dispenser_ok")


def test_simulador_weight_injeta_divergencia_de_peso(carregar_simulador):
    sim = carregar_simulador("weight", env={
        **MODO_DEMO, "T_LEITURA": "0", "T_TARA": "0",
    })
    sim.modulo._do_tara("OS-1")
    sim.modulo._do_pesar("OS-1", 1, 10, 50.0, quantidade_real=10,
                         injetar_falha="divergencia_peso")

    (evento,) = sim.eventos_do_tipo("peso_divergencia")
    assert evento["dentro_tolerancia"] is False
    assert evento["desvio_pct"] > evento["tolerancia_pct"]
    assert evento["falha_injetada"] is True


def test_injecao_de_peso_nao_contamina_o_slot_seguinte(carregar_simulador):
    """A injeção desloca a LEITURA, não a massa na mesa.

    A balança mede o acumulado desde a tara e valida cada slot pelo DELTA. Se a
    injeção tirasse peso de verdade da mesa, o delta do slot SEGUINTE nasceria
    inflado e divergiria também — o one-shot deixaria de ser one-shot sem
    ninguém notar, e a demo mostraria duas travas onde se armou uma.
    """
    sim = carregar_simulador("weight", env={
        **MODO_DEMO, "T_LEITURA": "0", "T_TARA": "0",
    })
    sim.modulo._do_tara("OS-1")
    sim.modulo._do_pesar("OS-1", 1, 10, 50.0, quantidade_real=10,
                         injetar_falha="divergencia_peso")
    sim.modulo._do_pesar("OS-1", 2, 10, 50.0, quantidade_real=10)

    assert len(sim.eventos_do_tipo("peso_divergencia")) == 1
    (ok,) = sim.eventos_do_tipo("peso_ok")
    assert ok["slot_id"] == 2
    assert ok["peso_medido_g"] == pytest.approx(500.0)
    assert ok["falha_injetada"] is False


def test_simulador_weight_ignora_injecao_desconhecida(carregar_simulador):
    sim = carregar_simulador("weight", env={
        **MODO_DEMO, "T_LEITURA": "0", "T_TARA": "0",
    })
    sim.modulo._do_tara("OS-1")
    sim.modulo._do_pesar("OS-1", 1, 10, 50.0, quantidade_real=10,
                         injetar_falha="sku_dispenser")
    assert sim.eventos_do_tipo("peso_ok")


# ── 4. Os nomes batem nas quatro cópias ───────────────────────────────────────

def _carregar_injecao():
    """`injecao.py` por caminho, sem depender de outra fixture."""
    caminho = CENTRAL_DIR / "injecao.py"
    if str(CENTRAL_DIR) not in sys.path:
        sys.path.insert(0, str(CENTRAL_DIR))
    spec = importlib.util.spec_from_file_location("apsen_injecao_nomes", caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["apsen_injecao_nomes"] = modulo
    try:
        spec.loader.exec_module(modulo)
    finally:
        del sys.modules["apsen_injecao_nomes"]
    return modulo


def test_constantes_dos_simuladores_batem_com_o_catalogo(carregar_simulador):
    """Cada simulador reconhece EXATAMENTE os tipos que o catálogo diz que ele
    executa — nem string diferente, nem tipo a mais, nem a menos.

    String divergente não quebra nada visivelmente: o simulador ignora, o
    central acha que injetou, e o gatilho armado simplesmente não dispara.
    """
    catalogo = _carregar_injecao().TIPOS

    # Constante do simulador → tipo do catálogo que ela deveria valer.
    constantes = {
        "dispenser": {"INJECAO_FALHA_MECANICA": "falha_mecanica_dispenser"},
        "vision": {
            "INJECAO_SKU_DISPENSER":      "sku_dispenser",
            "INJECAO_FALHA_LEITURA_DISP": "falha_leitura_dispenser",
            "INJECAO_DIVERGENCIA_MESA":   "divergencia_mesa",
        },
        "weight": {"INJECAO_DIVERGENCIA_PESO": "divergencia_peso"},
    }

    for nome_sim, mapa in constantes.items():
        modulo = carregar_simulador(nome_sim).modulo
        for constante, tipo in mapa.items():
            assert getattr(modulo, constante) == tipo, (nome_sim, constante)
            assert catalogo[tipo]["simulador"] == nome_sim, tipo

        # Nem tipo a menos: tudo que o catálogo atribui a este simulador tem
        # que ter constante aqui.
        do_catalogo = {t for t, meta in catalogo.items()
                       if meta["simulador"] == nome_sim}
        assert do_catalogo == set(mapa.values()), nome_sim


def test_duplo_de_adapter_da_suite_usa_as_mesmas_strings():
    """A quarta cópia: o `AdapterFake` do conftest.

    Ele reproduz a semântica de injeção para que os testes de OS completa
    exercitem o efeito, e não só o envio. Uma string errada ali faria esses
    testes verificarem um caminho que a planta real não percorre.
    """
    catalogo = _carregar_injecao().TIPOS
    fonte = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
    for tipo in catalogo:
        assert f'"{tipo}"' in fonte, tipo


def test_todo_tipo_aponta_para_um_comando_que_o_orquestrador_manda():
    """Catálogo e orquestrador: o comando do tipo tem que existir de verdade.

    Um tipo apontando para uma rota que ninguém chama é gatilho que nunca
    dispara — a mesma família de bug das strings divergentes, por outro lado.
    """
    modulo = _carregar_injecao()
    fonte = (CENTRAL_DIR / "orchestrator.py").read_text(encoding="utf-8")
    for comando in modulo.TIPOS_POR_COMANDO:
        assert f'_injecao_para("{comando}"' in fonte, comando
        assert f'+ "{comando}"' in fonte, comando
