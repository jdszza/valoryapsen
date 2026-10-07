# -*- coding: utf-8 -*-
"""O conferidor das estações dos dispensers: cada regra reprova sozinha.

`vision/conferir_dispensers.py` roda antes de cada estação subir. As falhas que
ele pega viram, sem ele, trava falsa (zona do modelo medida em outra bancada),
duas estações julgando a mesma webcam, ou o adapter sem a prova de que a
estação buscou o catálogo da OS corrente.
"""
import copy
import importlib.util
import sys
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent
VISION = RAIZ_REPO / "vision"


@pytest.fixture(scope="module")
def conf():
    sys.path.insert(0, str(VISION))
    try:
        spec = importlib.util.spec_from_file_location("conferir_dispensers",
                                                      VISION / "conferir_dispensers.py")
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        yield modulo
    finally:
        sys.path.remove(str(VISION))


def _zonas(*dispensers, x=60):
    return {"resolucao": [1280, 720],
            "zonas": [{"dispenser": d, "x": x + 10 * d, "y": 130, "largura": 272,
                       "altura": 500} for d in dispensers]}


MODELO = _zonas(1, 2, 3, 4, x=0)


def _parametros(lado, nome="Logitech C920 #1"):
    return {"camera": {"indice": 0, **({"nome": nome} if nome else {})},
            "backend": {"ativo": True, "url": f"http://127.0.0.1:8102/estacoes/{lado}",
                        "intervalo_catalogo": 2.0}}


def _conferir(conf, lado="esq", zonas=None, modelo=MODELO, parametros=None,
              cameras=None, outro=None, mesa=None):
    return conf.conferir(
        lado,
        zonas=zonas if zonas is not None else _zonas(*conf.SLOTS_DO_LADO[lado]),
        zonas_modelo=modelo,
        parametros=parametros if parametros is not None else _parametros(lado),
        cameras_bat=cameras if cameras is not None else {"esq": "", "dir": ""},
        parametros_outro=outro if outro is not None else _parametros(
            conf.OUTRO_LADO[lado], nome="Logitech C920 #2"),
        mesa_json=mesa if mesa is not None else {"camera": {"indice": 2}},
    )


@pytest.mark.parametrize("lado", ["esq", "dir"])
def test_conjunto_certo_passa(conf, lado):
    assert _conferir(conf, lado=lado) == []


def test_zonas_do_modelo_reprovam(conf):
    (problema,) = _conferir(conf, zonas=copy.deepcopy(MODELO))
    assert "idêntico ao do modelo" in problema.texto and "calibrar" in problema.acao


@pytest.mark.parametrize("lado,dispensers", [
    ("dir", (1, 2, 3, 4)),        # a pasta da direita, copiada do modelo e renumerada mal
    ("esq", (1, 2, 3)),
    ("esq", (1, 2, 3, 4, 5)),
])
def test_zonas_com_a_numeracao_errada_reprovam(conf, lado, dispensers):
    problemas = _conferir(conf, lado=lado, zonas=_zonas(*dispensers, x=99))
    assert [p for p in problemas if "zonas numeradas" in p.texto]


def test_zonas_ausentes_reprovam(conf):
    problemas = conf.conferir("esq", None, MODELO, _parametros("esq"),
                              {"esq": "", "dir": ""})
    assert any("zonas.json não existe" in p.texto for p in problemas)


def test_camera_que_ninguem_escolheu_reprova(conf):
    (problema,) = _conferir(conf, parametros=_parametros("esq", nome=None))
    assert "ninguém escolheu a câmera" in problema.texto


def test_camera_por_indice_no_bat_conta_como_escolha(conf):
    assert _conferir(conf, parametros=_parametros("esq", nome=None),
                     cameras={"esq": "0", "dir": ""}) == []


@pytest.mark.parametrize("cameras,outro_nome,trecho", [
    ({"esq": "", "dir": ""}, "Logitech C920 #1", "mesma da estação dir"),
    ({"esq": "1", "dir": "1"}, None, "mesma da estação dir"),
    ({"esq": "2", "dir": ""}, "Logitech C920 #2", "camera.indice da estação da mesa"),
], ids=["mesmo-nome", "mesmo-indice", "indice-da-mesa"])
def test_camera_em_uso_por_outra_estacao_reprova(conf, cameras, outro_nome, trecho):
    problemas = _conferir(conf, cameras=cameras,
                          outro=_parametros("dir", nome=outro_nome))
    assert any(trecho in p.texto for p in problemas), problemas


@pytest.mark.parametrize("backend", [
    {"ativo": True, "url": "http://127.0.0.1:8102", "intervalo_catalogo": 2},
    {"ativo": True, "url": "http://127.0.0.1:8102/estacoes/dir", "intervalo_catalogo": 2},
    {"ativo": False, "url": "http://127.0.0.1:8102/estacoes/esq", "intervalo_catalogo": 2},
    {"ativo": True, "url": "http://127.0.0.1:8102/estacoes/esq", "intervalo_catalogo": 3},
], ids=["url-sem-lado", "url-do-outro-lado", "inativo", "intervalo-diferente"])
def test_backend_reprova(conf, backend):
    parametros = {**_parametros("esq"), "backend": backend}
    (problema,) = _conferir(conf, parametros=parametros)
    assert "backend" in problema.texto
    assert "http://127.0.0.1:8102/estacoes/esq" in problema.acao


def test_toda_falha_diz_o_que_fazer(conf):
    problemas = conf.conferir("dir", _zonas(1, 2, 3, 4), _zonas(1, 2, 3, 4),
                              {"camera": {}, "backend": {}}, {"esq": "", "dir": ""})
    assert len(problemas) >= 3
    assert all(p.acao for p in problemas)


def test_main_recusa_lado_desconhecido(conf, capsys):
    assert conf.main(["x", "meio"]) == 2
