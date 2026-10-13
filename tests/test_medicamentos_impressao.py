# -*- coding: utf-8 -*-
"""O medicamentos.json de impressão sai da tabela de etiquetas, e não diverge dela.

`vision/medicamentos_impressao.py` gera o arquivo que o `gerar_qrcodes.py` da
estação imprime. Uma etiqueta impressa com `qr` ou `aruco` diferente do da
tabela é lida na zona como código desconhecido — sem erro em lugar nenhum —, então
o que se cobra aqui é a igualdade campo a campo com a tabela, a aceitação pelo
`Catalogo` DA ESTAÇÃO (o mesmo que o gerador chama) e o número fantasma igual ao
do vision-adapter.
"""
import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ_REPO / "vision" / "medicamentos_impressao.py"
TABELA = RAIZ_REPO / "vision-adapter" / "etiquetas.json"
PONTE = RAIZ_REPO / "vision-adapter" / "ponte_dispensers.py"
CONFIGURACAO_ESTACAO = RAIZ_REPO / "vision" / "visao" / "src" / "configuracao.py"


def _carregar(caminho, nome):
    spec = importlib.util.spec_from_file_location(nome, caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo
    try:
        spec.loader.exec_module(modulo)
    finally:
        sys.modules.pop(nome, None)
    return modulo


@pytest.fixture(scope="module")
def gerador():
    return _carregar(SCRIPT, "medicamentos_impressao")


@pytest.fixture(scope="module")
def ponte():
    return _carregar(PONTE, "ponte_impressao")


@pytest.fixture(scope="module")
def Catalogo():
    nome = "visao_estacao_configuracao_impressao"
    spec = importlib.util.spec_from_file_location(nome, CONFIGURACAO_ESTACAO)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo     # as dataclasses resolvem anotações por aqui
    try:
        spec.loader.exec_module(modulo)
        yield modulo.Catalogo
    finally:
        sys.modules.pop(nome, None)


def _tabela():
    return json.loads(TABELA.read_text(encoding="utf-8"))["etiquetas"]


def _gerar(gerador, tmp_path, *extra):
    saida = tmp_path / "config" / "medicamentos.json"
    codigo = gerador.main([str(saida), *extra])
    return codigo, (json.loads(saida.read_text(encoding="utf-8")) if saida.exists() else None)


def test_o_arquivo_gerado_e_aceito_pelo_catalogo_da_estacao(gerador, tmp_path, Catalogo):
    codigo, dados = _gerar(gerador, tmp_path)
    assert codigo == 0
    # `Catalogo.carregar` é exatamente o que o gerar_qrcodes.py chama.
    catalogo = Catalogo.carregar(tmp_path / "config" / "medicamentos.json")
    assert len(catalogo.medicamentos) == len(_tabela())


def test_qr_nome_e_aruco_sao_os_da_tabela_na_mesma_ordem(gerador, tmp_path):
    _, dados = _gerar(gerador, tmp_path)
    tabela = [(e["qr"], e["nome"], e["aruco"]) for e in _tabela()]
    gerado = [(m["qr"], m["nome"], m["aruco"]) for m in dados["medicamentos"]]
    assert gerado == tabela


def test_o_dispenser_e_o_numero_fantasma_do_adapter(gerador, ponte, tmp_path, caplog):
    """O mesmo número com que o adapter manda o medicamento ao catálogo sem OS."""
    _, dados = _gerar(gerador, tmp_path)
    with caplog.at_level(logging.ERROR):
        etiquetas = ponte.carregar_etiquetas(TABELA)
    do_adapter = {m["qr"]: m["dispenser"]
                  for m in ponte.montar_catalogo({}, etiquetas)["medicamentos"]}
    assert {m["qr"]: m["dispenser"] for m in dados["medicamentos"]} == do_adapter


def test_as_constantes_sao_as_do_adapter(gerador, ponte):
    assert (gerador.ARUCO_MAX, gerador.BASE_FANTASMA) == (ponte.ARUCO_MAX, ponte.BASE_FANTASMA)


def test_as_cores_sao_estaveis_e_validas(gerador, tmp_path):
    _, primeira = _gerar(gerador, tmp_path)
    _, segunda = _gerar(gerador, tmp_path)
    assert primeira == segunda
    assert all(len(m["cor"]) == 3 and all(0 <= c <= 255 for c in m["cor"])
               for m in primeira["medicamentos"])


def test_reimpressao_parcial_nao_muda_o_numero_de_ninguem(gerador, tmp_path, Catalogo):
    codigo, dados = _gerar(gerador, tmp_path, "--qr", "MED-005, MED-021")
    assert codigo == 0
    assert [(m["qr"], m["dispenser"]) for m in dados["medicamentos"]] == \
        [("MED-005", 1005), ("MED-021", 1021)]
    Catalogo.carregar(tmp_path / "config" / "medicamentos.json")


def test_codigo_que_a_tabela_nao_tem_recusa(gerador, tmp_path):
    codigo, dados = _gerar(gerador, tmp_path, "--qr", "MED-999")
    assert codigo == 1 and dados is None


@pytest.mark.parametrize("linha_ruim", [
    {"nome": "ALOIS 20MG", "qr": "MED-001", "aruco": 45},     # qr repetido
    {"nome": "ALOIS 20MG", "qr": "MED-045", "aruco": 1},      # aruco repetido
    {"nome": "ALOIS 20MG", "qr": "MED-045", "aruco": 50},     # fora do DICT_4X4_50
    {"nome": " miosan  5mg ", "qr": "MED-045", "aruco": 45},  # nome repetido
    {"nome": "ALOIS 20MG", "qr": "", "aruco": 45},            # qr vazio
    {"nome": "ALOIS 20MG", "qr": "MED-045", "aruco": "45"},   # aruco não inteiro
], ids=["qr", "aruco", "fora", "nome", "qr-vazio", "aruco-texto"])
def test_linha_que_o_adapter_descartaria_nao_gera_nada(gerador, ponte, tmp_path, caplog,
                                                       linha_ruim):
    """Gerar só as linhas boas imprimiria a ruim como código desconhecido —
    o adapter a descarta, e a etiqueta dela não casa com nada."""
    tabela = tmp_path / "etiquetas.json"
    tabela.write_text(json.dumps({"etiquetas": _tabela() + [linha_ruim]}), encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert len(ponte.carregar_etiquetas(tabela)) == len(_tabela())   # o adapter descarta
    codigo, dados = _gerar(gerador, tmp_path, "--tabela", str(tabela))
    assert codigo == 1 and dados is None
