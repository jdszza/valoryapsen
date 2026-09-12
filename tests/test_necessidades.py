# -*- coding: utf-8 -*-
"""
Tela de necessidades — o que precisa de atenção, priorizado.

Três frentes:

1. **A prioridade é a do IMPACTO.** Trava ativa bloqueia a produção agora;
   desgaste de correia vai bloquear algum dia. Uma lista que os ordenasse pela
   gravidade "de manutenção" deixaria de responder à pergunta que a motivou.
   Os testes encenam todos os tipos juntos e cobram a ordem.

2. **Cada linha leva a uma ABA que existe.** É o que torna a tela um ponto de
   partida e não mais um relatório. Os ids emitidos por `necessidades.py` são
   comparados contra a sidebar de `manut_web/app.py` — aba inexistente é um
   clique que não faz nada, e nada no Python quebraria por causa disso.

3. **Nada pendente é dito com clareza, e banco fora NÃO é nada pendente.**
   "Tudo em dia" com o banco sem responder é a afirmação mais perigosa que esta
   tela pode fazer: alarmes, componentes e OS em erro simplesmente não foram
   lidos.
"""
import importlib.util
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent
CENTRAL_DIR = RAIZ_REPO / "central-computer"
MANUT_APP = RAIZ_REPO / "manut_web" / "app.py"

AGORA = datetime(2026, 9, 11, 16, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def nec():
    """`necessidades.py` por caminho. Puro: sem FastAPI, sem `database`."""
    spec = importlib.util.spec_from_file_location(
        "apsen_necessidades", CENTRAL_DIR / "necessidades.py")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["apsen_necessidades"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


# ── Fatos de mentira, um por fonte ────────────────────────────────────────────

TRAVA = {"ativa": True, "os_id": "OS-7", "slot_id": 3,
         "motivo": "SKU errado no dispenser D3"}
SEM_TRAVA = {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""}


def _alarme(fonte, tipo="leitura_dispenser_falha", ts="2026-09-11T15:50:00",
            descricao="falha de leitura", resolvido=False):
    return {"id": 1, "fonte": fonte, "tipo": tipo, "descricao": descricao,
            "resolvido": resolvido, "ts": ts}


def _leitura(componente, tipo, valor, unidade):
    return {"componente": componente, "tipo": tipo, "valor": valor,
            "unidade": unidade}


def _slot(quantidade=0, medicamento=None, os_id=None, status="idle"):
    return {"status": status, "medicamento": medicamento, "sku": None,
            "categoria": None, "quantidade": quantidade,
            "quantidade_alvo": 0, "quantidade_dispensada": 0,
            "quantidade_residual": quantidade, "os_id": os_id}


def _ordem(os_id, status="erro", quando=None, descricao="teste"):
    return {"os_id": os_id, "status": status, "descricao": descricao,
            "criado_em": (quando or AGORA).isoformat(),
            "concluida_em": (quando or AGORA).isoformat()}


def _categorias(resultado) -> list:
    return [i["categoria"] for i in resultado["itens"]]


# ── 1. Nada pendente ──────────────────────────────────────────────────────────

def test_planta_em_dia_devolve_lista_vazia_e_diz_isso(nec):
    r = nec.montar(trava=SEM_TRAVA, fila={"tamanho": 0, "capacidade": 5},
                   alarmes=[], leituras=[], dispensers={}, os_ativa=None,
                   ordens=[], agora=AGORA)
    assert r["itens"] == []
    assert r["resumo"]["tudo_em_dia"] is True
    assert r["resumo"]["total"] == 0


def test_residuo_sozinho_ja_impede_tudo_em_dia(nec):
    """`tudo_em_dia` é falso com QUALQUER item, inclusive informativo: resíduo
    parado é pendência pequena, mas é pendência."""
    r = nec.montar(dispensers={"3": _slot(6, "Dipirona")}, agora=AGORA)
    assert r["resumo"]["tudo_em_dia"] is False
    assert r["resumo"]["info"] == 1


def test_montar_sem_argumento_nenhum_nao_explode(nec):
    """Todas as fontes são opcionais — a tela é justamente a que se abre quando
    algo já não está normal."""
    assert nec.montar()["itens"] == []


# ── 2. Prioridade ─────────────────────────────────────────────────────────────

def test_trava_vem_antes_de_tudo(nec):
    """Ela é a única que bloqueia a produção AGORA: o loop do orquestrador é
    único, e enquanto ela não for liberada nenhuma OS anda."""
    r = nec.montar(
        trava=TRAVA,
        fila={"tamanho": 5, "capacidade": 5},
        alarmes=[_alarme("camera_mesa")],
        leituras=[_leitura("driver_x", "temperatura", 80.0, "°C")],
        dispensers={"1": _slot(9, "Dipirona")},
        ordens=[_ordem("OS-1")],
        agora=AGORA,
    )
    assert _categorias(r)[0] == "trava"
    assert r["itens"][0]["severidade"] == nec.CRITICO
    assert "D3" in r["itens"][0]["titulo"]
    assert "OS-7" in r["itens"][0]["detalhe"]


def test_ordem_completa_das_categorias(nec):
    """Com uma pendência de cada tipo, a lista sai na ordem de impacto."""
    r = nec.montar(
        trava=TRAVA,
        fila={"tamanho": 5, "capacidade": 5},
        alarmes=[_alarme("camera_mesa")],
        leituras=[_leitura("driver_x", "temperatura", 80.0, "°C")],
        dispensers={"1": _slot(9, "Dipirona")},
        ordens=[_ordem("OS-1")],
        agora=AGORA,
    )
    assert _categorias(r) == list(nec.PRIORIDADE)


def test_prioridade_cobre_toda_categoria_emitida(nec):
    """`PRIORIDADE.index` levanta `ValueError` para categoria desconhecida — a
    tela morreria inteira por causa de um tipo novo de pendência."""
    r = nec.montar(
        trava=TRAVA, fila={"tamanho": 5, "capacidade": 5},
        alarmes=[_alarme("a")], leituras=[_leitura("c", "temperatura", 90, "°C")],
        dispensers={"1": _slot(3, "x")}, ordens=[_ordem("OS-1")], agora=AGORA,
    )
    assert set(_categorias(r)) <= set(nec.PRIORIDADE)
    assert set(nec._ABA_POR_CATEGORIA) == set(nec.PRIORIDADE)


# ── 3. Fila ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tamanho,esperado", [(0, 0), (2, 0), (3, 1), (5, 1)])
def test_fila_so_aparece_acima_do_limiar(nec, tamanho, esperado):
    """Avisar só quando já está recusando é avisar tarde — daí o limiar em 60%,
    e não em 100%."""
    r = nec.montar(fila={"tamanho": tamanho, "capacidade": 5}, agora=AGORA)
    assert len(r["itens"]) == esperado


def test_fila_cheia_e_critica_e_menciona_o_429(nec):
    r = nec.montar(fila={"tamanho": 5, "capacidade": 5}, agora=AGORA)
    (item,) = r["itens"]
    assert item["severidade"] == nec.CRITICO
    assert "429" in item["detalhe"]
    assert "trava" in item["acao"].lower()


def test_fila_acumulando_e_so_atencao(nec):
    r = nec.montar(fila={"tamanho": 4, "capacidade": 5}, agora=AGORA)
    assert r["itens"][0]["severidade"] == nec.ATENCAO


# ── 4. Alarmes ────────────────────────────────────────────────────────────────

def test_alarmes_sao_agrupados_por_fonte(nec):
    """Sem agrupar, uma câmera com problema emite dezenas de linhas iguais e
    empurra para fora da tela a OS em erro que talvez as explique."""
    r = nec.montar(alarmes=[
        _alarme("camera_dispenser_dir_7", ts="2026-09-11T15:00:00"),
        _alarme("camera_dispenser_dir_7", ts="2026-09-11T15:30:00"),
        _alarme("camera_dispenser_dir_7", ts="2026-09-11T15:50:00"),
        _alarme("balanca", ts="2026-09-11T14:00:00"),
    ], agora=AGORA)

    assert len(r["itens"]) == 2
    primeiro = r["itens"][0]
    assert "camera_dispenser_dir_7" in primeiro["titulo"]
    assert "3 alarme" in primeiro["titulo"]
    # A fonte é o agrupamento certo porque é ela que diz ONDE ir.
    assert primeiro["aba"] == "alarmes"


def test_fonte_com_mais_de_um_alarme_e_critica(nec):
    um = nec.montar(alarmes=[_alarme("balanca")], agora=AGORA)["itens"][0]
    dois = nec.montar(alarmes=[_alarme("balanca"), _alarme("balanca")],
                      agora=AGORA)["itens"][0]
    assert um["severidade"] == nec.ATENCAO
    assert dois["severidade"] == nec.CRITICO


def test_fontes_saem_da_mais_recente_para_a_mais_antiga(nec):
    r = nec.montar(alarmes=[
        _alarme("antiga", ts="2026-09-10T08:00:00"),
        _alarme("recente", ts="2026-09-11T15:55:00"),
        _alarme("media", ts="2026-09-11T09:00:00"),
    ], agora=AGORA)
    assert [i["titulo"].split(":")[0] for i in r["itens"]] == [
        "recente", "media", "antiga"]


def test_alarme_resolvido_nao_entra(nec):
    r = nec.montar(alarmes=[_alarme("balanca", resolvido=True)], agora=AGORA)
    assert r["itens"] == []


def test_alarme_sem_fonte_nao_some(nec):
    """Linha sem fonte é rara e é justamente a que ninguém vai procurar — cair
    num balde nomeado é melhor que sumir."""
    r = nec.montar(alarmes=[{"tipo": "x", "descricao": "y", "resolvido": False,
                             "ts": "2026-09-11T15:00:00"}], agora=AGORA)
    assert len(r["itens"]) == 1
    assert "desconhecida" in r["itens"][0]["titulo"]


# ── 5. Componentes ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("valor,esperado", [
    (30.0, None), (49.9, None), (50.0, "atencao"), (64.9, "atencao"),
    (65.0, "critico"), (90.0, "critico"),
])
def test_limiares_de_temperatura(nec, valor, esperado):
    """Os mesmos limiares com que a aba de temperaturas já pinta de vermelho.

    Um limiar próprio aqui faria esta tela listar um componente que a outra
    mostra em verde — e quem visse as duas duvidaria das duas.
    """
    r = nec.montar(leituras=[_leitura("driver_x", "temperatura", valor, "°C")],
                   agora=AGORA)
    if esperado is None:
        assert r["itens"] == []
    else:
        assert r["itens"][0]["severidade"] == esperado
        assert r["itens"][0]["aba"] == "temp"


@pytest.mark.parametrize("valor,esperado", [
    (40.0, None), (60.0, "atencao"), (80.0, "critico"),
])
def test_limiares_de_desgaste(nec, valor, esperado):
    r = nec.montar(leituras=[_leitura("correia_eixo_x", "desgaste", valor, "%")],
                   agora=AGORA)
    if esperado is None:
        assert r["itens"] == []
    else:
        assert r["itens"][0]["severidade"] == esperado
        assert r["itens"][0]["aba"] == "uso"


def test_leitura_sem_unidade_percentual_e_ignorada(nec):
    """`horas_uso` e `ciclos` não têm faixa — listá-los seria ruído permanente."""
    r = nec.montar(leituras=[
        _leitura("cnc_geral", "horas", 4200.0, "h"),
        _leitura("cnc_geral", "ciclos", 51000.0, "ciclos"),
    ], agora=AGORA)
    assert r["itens"] == []


def test_valor_invalido_nao_derruba_a_tela(nec):
    r = nec.montar(leituras=[
        _leitura("quebrado", "temperatura", None, "°C"),
        _leitura("ok", "temperatura", 90.0, "°C"),
    ], agora=AGORA)
    assert len(r["itens"]) == 1
    assert "ok" in r["itens"][0]["titulo"]


def test_componentes_criticos_vem_antes_dos_de_atencao(nec):
    r = nec.montar(leituras=[
        _leitura("morno", "temperatura", 55.0, "°C"),
        _leitura("quente", "temperatura", 85.0, "°C"),
    ], agora=AGORA)
    assert [i["severidade"] for i in r["itens"]] == [nec.CRITICO, nec.ATENCAO]


# ── 6. Resíduo ────────────────────────────────────────────────────────────────

def test_slot_vazio_nao_vira_pendencia(nec):
    r = nec.montar(dispensers={"1": _slot(0)}, agora=AGORA)
    assert r["itens"] == []


def test_slot_com_residuo_sugere_limpeza(nec):
    r = nec.montar(dispensers={"4": _slot(7, "ALOIS 10MG", os_id="OS-3")},
                   agora=AGORA)
    (item,) = r["itens"]
    assert "D4" in item["titulo"] and "7" in item["titulo"]
    assert "ALOIS 10MG" in item["detalhe"]
    assert "OS-3" in item["detalhe"]
    assert item["aba"] == "dispensers"
    assert "limpe" in item["acao"].lower()


def test_slot_da_os_em_execucao_fica_de_fora(nec):
    """Ali o "resíduo" é a carga que está sendo dispensada agora — sugerir
    limpeza seria sugerir abortar a OS."""
    r = nec.montar(dispensers={"2": _slot(10, "Dipirona", os_id="OS-9")},
                   os_ativa={"os_id": "OS-9"}, agora=AGORA)
    assert r["itens"] == []


@pytest.mark.parametrize("status", ["carregando", "dispensando"])
def test_slot_em_operacao_fica_de_fora(nec, status):
    """Mesma distinção que o `_do_limpar` do simulador faz ao recusar limpeza
    de slot em operação."""
    r = nec.montar(dispensers={"2": _slot(10, "Dipirona", status=status)},
                   agora=AGORA)
    assert r["itens"] == []


def test_slots_saem_em_ordem_numerica(nec):
    """Ordenar por string poria D10 entre D1 e D2 — e a lista deixa de casar
    com a bancada, que é como o operador procura."""
    dispensers = {str(i): _slot(3, "X") for i in (10, 2, 1)}
    r = nec.montar(dispensers=dispensers, agora=AGORA)
    assert [i["titulo"][:3] for i in r["itens"]] == ["D1 ", "D2 ", "D10"[:3]]


# ── 7. OS em erro ─────────────────────────────────────────────────────────────

def test_os_em_erro_recente_aparece(nec):
    r = nec.montar(ordens=[_ordem("OS-5", quando=AGORA - timedelta(hours=2))],
                   agora=AGORA)
    (item,) = r["itens"]
    assert "OS-5" in item["titulo"]
    assert item["aba"] == "ordens"


def test_os_em_erro_antiga_fica_de_fora(nec):
    """A janela é curta de propósito: o objetivo é "aconteceu agora, ainda dá
    para investigar", não estatística — para isso existe a aba de ordens."""
    r = nec.montar(ordens=[_ordem("OS-5", quando=AGORA - timedelta(hours=48))],
                   agora=AGORA)
    assert r["itens"] == []


def test_os_concluida_nao_aparece(nec):
    r = nec.montar(ordens=[_ordem("OS-5", status="concluida")], agora=AGORA)
    assert r["itens"] == []


def test_os_em_erro_saem_da_mais_recente_para_a_mais_antiga(nec):
    r = nec.montar(ordens=[
        _ordem("OS-A", quando=AGORA - timedelta(hours=6)),
        _ordem("OS-B", quando=AGORA - timedelta(hours=1)),
    ], agora=AGORA)
    assert [i["chave"] for i in r["itens"]] == ["os_erro:OS-B", "os_erro:OS-A"]


def test_timestamp_estranho_nao_derruba_a_tela(nec):
    r = nec.montar(ordens=[
        {"os_id": "OS-X", "status": "erro", "criado_em": "ontem à tarde"},
        _ordem("OS-OK", quando=AGORA),
    ], agora=AGORA)
    assert [i["chave"] for i in r["itens"]] == ["os_erro:OS-OK"]


# ── 8. Teto por categoria ─────────────────────────────────────────────────────

def test_categoria_longa_e_cortada_com_aviso(nec):
    """Cortar em silêncio mentiria sobre o tamanho do problema; não cortar faria
    vinte alarmes empurrarem as outras categorias para fora da tela."""
    alarmes = [_alarme(f"fonte_{i}", ts=f"2026-09-11T1{i}:00:00")
               for i in range(9)]
    r = nec.montar(alarmes=alarmes, ordens=[_ordem("OS-1")], agora=AGORA)

    do_tipo = [i for i in r["itens"] if i["categoria"] == "alarme"]
    assert len(do_tipo) == nec.MAX_POR_CATEGORIA + 1     # + a linha de resumo
    assert "e mais 3" in do_tipo[-1]["titulo"]
    # E a outra categoria sobreviveu ao corte — que é o ponto.
    assert any(i["categoria"] == "os_erro" for i in r["itens"])


def test_linha_de_excedente_leva_a_uma_aba(nec):
    alarmes = [_alarme(f"f{i}") for i in range(nec.MAX_POR_CATEGORIA + 2)]
    r = nec.montar(alarmes=alarmes, agora=AGORA)
    excedente = [i for i in r["itens"] if i["chave"].endswith(":excedente")]
    assert excedente and excedente[0]["aba"] == "alarmes"


# ── 9. Contrato com a tela ────────────────────────────────────────────────────

def _tudo(nec):
    return nec.montar(
        trava=TRAVA,
        fila={"tamanho": 5, "capacidade": 5},
        alarmes=[_alarme("camera_mesa"), _alarme("balanca")],
        leituras=[_leitura("driver_x", "temperatura", 80.0, "°C"),
                  _leitura("correia_eixo_x", "desgaste", 85.0, "%")],
        dispensers={"1": _slot(4, "Dipirona"), "6": _slot(2, "ALOIS 10MG")},
        ordens=[_ordem("OS-1"), _ordem("OS-2")],
        agora=AGORA,
    )


def test_todo_item_tem_a_forma_que_a_tela_desenha(nec):
    for item in _tudo(nec)["itens"]:
        assert set(item) == {"categoria", "severidade", "titulo", "detalhe",
                             "aba", "acao", "chave"}
        assert item["titulo"] and item["acao"] and item["aba"]
        assert item["severidade"] in (nec.CRITICO, nec.ATENCAO, nec.INFO)


def test_chaves_nao_se_repetem(nec):
    """A lista é redesenhada a cada 5s; chave repetida é linha que se sobrepõe
    a outra sem ninguém perceber."""
    chaves = [i["chave"] for i in _tudo(nec)["itens"]]
    assert len(set(chaves)) == len(chaves)


def test_resumo_conta_por_severidade(nec):
    r = _tudo(nec)
    soma = r["resumo"]["criticos"] + r["resumo"]["atencao"] + r["resumo"]["info"]
    assert soma == r["resumo"]["total"] == len(r["itens"])
    assert r["resumo"]["tudo_em_dia"] is False


# ── 10. As abas existem de verdade ────────────────────────────────────────────

def _abas_da_sidebar() -> set:
    """Ids da sidebar do app de manutenção, lidos do fonte."""
    fonte = MANUT_APP.read_text(encoding="utf-8")
    bloco = fonte.split("_SIDEBAR_LINKS = [", 1)[1].split("]", 1)[0]
    return set(re.findall(r'"([a-z_]+)"\),', bloco))


def test_toda_aba_referenciada_existe_na_sidebar(nec):
    """Aba inexistente é um clique que não faz nada — e nada no Python quebra
    por causa disso, que é o que torna a varredura necessária."""
    sidebar = _abas_da_sidebar()
    assert sidebar, "não consegui ler a sidebar do app de manutenção"
    for item in _tudo(nec)["itens"]:
        assert item["aba"] in sidebar, item
    for categoria, aba in nec._ABA_POR_CATEGORIA.items():
        assert aba in sidebar, categoria


def test_necessidades_e_a_primeira_aba():
    """A tela existe para ser a que se abre. Registrada em terceiro lugar, ela
    seria mais uma aba entre dez — e a navegação que ela poupa continuaria
    sendo feita."""
    fonte = MANUT_APP.read_text(encoding="utf-8")
    bloco = fonte.split("_SIDEBAR_LINKS = [", 1)[1].split("]", 1)[0]
    primeira = re.search(r'"([a-z_]+)"\),', bloco).group(1)
    assert primeira == "necessidades"
    assert 'dcc.Store(id="active-tab", data="necessidades")' in fonte


def test_app_de_manutencao_faz_UMA_chamada_para_a_tela():
    """O ponto do agregado: seis requisições por tique por cliente conectado
    era o custo que ele existe para evitar — é a mesma regra do `_fetch` do
    dashboard."""
    fonte = MANUT_APP.read_text(encoding="utf-8")
    corpo = fonte.split("def _render_necessidades", 1)[1].split("\ndef ", 1)[0]
    assert corpo.count("_api(") == 1, corpo.count("_api(")
    assert "/manutencao/necessidades" in corpo


def test_endpoint_agregado_existe_e_e_autenticado():
    """`/manutencao/*` inteiro passa por `_get_tecnico` — esta rota devolve
    trava, alarmes e histórico de OS, e seria a mais indevida de abrir."""
    fonte = (CENTRAL_DIR / "main.py").read_text(encoding="utf-8")
    bloco = fonte.split('@app.get("/manutencao/necessidades"', 1)[1].split("\n@app.", 1)[0]
    assert "Depends(_get_tecnico)" in bloco
    assert "necessidades.montar(" in bloco


def test_botao_leva_o_id_da_aba_e_nao_a_posicao():
    """A lista muda de tamanho e de ordem a cada render (polling de 5s). Um
    índice posicional mandaria o gestor para a aba errada sempre que uma
    pendência entrasse ou saísse entre o desenho e o clique."""
    fonte = MANUT_APP.read_text(encoding="utf-8")
    assert '"index": aba' in fonte
    corpo = fonte.split("def _ir_para_aba", 1)[1].split("\n@callback", 1)[0]
    assert 'alvo["index"]' in corpo
