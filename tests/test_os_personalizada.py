"""
OS personalizada — a validação pura (`central-computer/os_personalizada.py`).

O módulo não importa FastAPI nem `database`; só `os_templates`, que também é
puro. Por isso basta o caminho no `sys.path`, sem o aparato de
`carregar_central`.
"""
import importlib.util
import re
import sys
from datetime import datetime, timezone

import pytest
from conftest import CENTRAL_DIR


@pytest.fixture
def pers(monkeypatch):
    monkeypatch.syspath_prepend(str(CENTRAL_DIR))
    caminho = CENTRAL_DIR / "os_personalizada.py"
    spec = importlib.util.spec_from_file_location("apsen_os_personalizada", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    yield modulo
    sys.modules.pop("os_templates", None)


CATALOGO = {
    f"MED {i}": {"nome": f"MED {i}", "sku": f"SKU-{i:03d}", "categoria": f"cat{i}"}
    for i in range(1, 12)
}


def _itens(n: int) -> list[dict]:
    return [{"medicamento": f"MED {i}", "dispenser_id": i, "quantidade": 3}
            for i in range(1, n + 1)]


def test_constantes_da_decisao(pers):
    assert pers.TEMPLATE_ID == "PERS"
    assert (pers.MAX_ITENS_PERS, pers.QTD_MIN_PERS, pers.QTD_MAX_PERS) == (8, 1, 10)


def test_oito_itens_passam_nove_nao(pers):
    assert pers.validar(_itens(8), CATALOGO, 8) == []
    itens = _itens(8) + [{"medicamento": "MED 9", "dispenser_id": 1, "quantidade": 1}]
    assert any("9 linha(s)" in p for p in pers.validar(itens, CATALOGO, 8))


def test_lista_vazia_recusada(pers):
    assert pers.validar([], CATALOGO, 8) == ["0 linha(s) — a faixa é 1..8."]


def test_teto_segue_num_slots_menor(pers):
    assert any("faixa é 1..4" in p for p in pers.validar(_itens(5), CATALOGO, 4))


@pytest.mark.parametrize("qtd,ok", [(0, False), (1, True), (10, True), (11, False),
                                    (True, False), (2.0, False), ("3", False)])
def test_faixa_de_quantidade(pers, qtd, ok):
    itens = [{"medicamento": "MED 1", "dispenser_id": 1, "quantidade": qtd}]
    problemas = pers.validar(itens, CATALOGO, 8)
    if ok:
        assert problemas == []
    else:
        assert problemas == [f"linha 1: quantidade {qtd!r} fora da faixa 1..10."]


@pytest.mark.parametrize("disp", [0, 9, True, "3", None])
def test_dispenser_fora_da_faixa(pers, disp):
    itens = [{"medicamento": "MED 1", "dispenser_id": disp, "quantidade": 1}]
    assert pers.validar(itens, CATALOGO, 8) == [
        f"linha 1: dispenser {disp!r} fora da faixa D1..D8."]


def test_dispenser_repetido_diz_a_linha(pers):
    itens = [{"medicamento": "MED 1", "dispenser_id": 5, "quantidade": 1},
             {"medicamento": "MED 2", "dispenser_id": 2, "quantidade": 1},
             {"medicamento": "MED 3", "dispenser_id": 5, "quantidade": 1}]
    assert pers.validar(itens, CATALOGO, 8) == ["linha 3: D5 já usado na linha 1."]


def test_medicamento_repetido_diz_a_linha(pers):
    itens = [{"medicamento": "MED 1", "dispenser_id": 1, "quantidade": 1},
             {"medicamento": "MED 1", "dispenser_id": 2, "quantidade": 1}]
    assert pers.validar(itens, CATALOGO, 8) == ["linha 2: 'MED 1' já está na linha 1."]


def test_medicamento_fora_do_catalogo(pers):
    itens = [{"medicamento": "INEXISTENTE", "dispenser_id": 1, "quantidade": 1}]
    assert pers.validar(itens, CATALOGO, 8) == [
        "linha 1: 'INEXISTENTE' não existe no catálogo de medicamentos."]


def test_sem_medicamento_e_item_malformado(pers):
    itens = [{"dispenser_id": 1, "quantidade": 1}, "lixo"]
    assert pers.validar(itens, CATALOGO, 8) == ["linha 1: sem medicamento.",
                                                "linha 2: item malformado."]


def test_itens_que_nao_sao_lista(pers):
    assert pers.validar("x", CATALOGO, 8)


def test_montar_corpo_resolve_do_catalogo(pers):
    itens = [{"medicamento": "MED 4", "dispenser_id": 7, "quantidade": 2,
              "sku": "DO-CLIENTE"}]
    corpo = pers.montar_corpo(itens, CATALOGO)
    assert corpo["template_id"] == "PERS"
    assert corpo["descricao"] == "Ordem personalizada"
    assert corpo["categoria"] == "personalizada"
    assert corpo["medicamentos"] == [{
        "medicamento": "MED 4", "sku": "SKU-004", "categoria": "cat4",
        "quantidade": 2, "dispenser_id": 7,
    }]


def test_os_id_casa_o_formato_de_novo_os_id(pers, os_templates):
    agora = datetime(2026, 10, 21, 14, 30, tzinfo=timezone.utc)
    corpo = pers.montar_corpo(_itens(1), CATALOGO, agora=agora, sufixo="a1b2c3")
    assert corpo["os_id"] == os_templates.novo_os_id("PERS", agora, "a1b2c3")
    assert re.fullmatch(r"PERS-\d{8}T\d{6}-[0-9A-F]{6}", corpo["os_id"])


def test_nenhum_template_padrao_usa_o_id_da_personalizada(pers, os_templates):
    ids = [t["template_id"] for t in os_templates.TEMPLATES]
    assert pers.TEMPLATE_ID not in ids
    assert not any(i.startswith(pers.TEMPLATE_ID + "-") for i in ids)
    assert not any(i.startswith("DEMO-") for i in ids)
