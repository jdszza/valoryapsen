# -*- coding: utf-8 -*-
"""O conferidor da estação da mesa: cada regra reprova sozinha.

`vision/conferir_mesa.py` roda antes de a estação subir. Tudo o que ele confere
falharia DEPOIS e calado — contagem no lugar errado com `confiavel=True`, bind na
porta do vision-simulator, eventos para um IP de exemplo, servidor que morre no
import. Os testes usam dicts de exemplo e nunca o `mesa.json` versionado: ele
está errado hoje (modo manual) e só a bancada o corrige.
"""
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
        spec = importlib.util.spec_from_file_location("conferir_mesa",
                                                      VISION / "conferir_mesa.py")
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        yield modulo
    finally:
        sys.path.remove(str(VISION))


MESA_CERTA = {
    "camera": {"indice": 2, "autofoco": False},
    "fundo": {"modo": "caixa", "suavizacao": 0, "frames_validade": 0},
}
ENV_CERTO = {"ADAPTER_URL": "http://127.0.0.1:8102", "PORTA": "8212", "HOST": "0.0.0.0",
             "ACEITAR_INJECAO": "0", "VALIDADE_OS_H": "24", "T_ASSENTAMENTO_S": "1.0"}
PACOTES_CERTOS = {"fastapi", "uvicorn", "requests", "cv2", "cv2.aruco"}


def _conferir(conf, mesa=None, env=ENV_CERTO, pacotes=PACOTES_CERTOS, cameras=None):
    return conf.conferir(mesa or MESA_CERTA, env, pacotes,
                         cameras if cameras is not None else {"esq": "", "dir": ""})


def test_conjunto_certo_passa(conf):
    assert _conferir(conf) == []


def _com(base: dict, secao: str, **campos) -> dict:
    return {**base, secao: {**base[secao], **campos}}


@pytest.mark.parametrize("mesa,nivel,trecho", [
    (_com(MESA_CERTA, "fundo", modo="manual"), "falha", "fundo.modo"),
    (_com(MESA_CERTA, "fundo", suavizacao=0.7), "falha", "suavizacao"),
    (_com(MESA_CERTA, "fundo", frames_validade=60), "falha", "frames_validade"),
    (_com(MESA_CERTA, "camera", autofoco=True), "alerta", "autofoco"),
], ids=["modo-manual", "suavizacao", "frames-validade", "autofoco"])
def test_regras_do_mesa_json(conf, mesa, nivel, trecho):
    (problema,) = _conferir(conf, mesa=mesa)
    assert problema.nivel == nivel and trecho in problema.texto and problema.acao


@pytest.mark.parametrize("env,nivel,trecho", [
    (None, "falha", ".env não existe"),
    ({**ENV_CERTO, "PORTA": "8202"}, "falha", "PORTA"),
    ({k: v for k, v in ENV_CERTO.items() if k != "PORTA"}, "falha", "PORTA"),
    ({**ENV_CERTO, "ADAPTER_URL": "http://192.168.0.10:8102"}, "falha", "ADAPTER_URL"),
    ({**ENV_CERTO, "HOST": "127.0.0.1"}, "falha", "HOST"),
    ({**ENV_CERTO, "ACEITAR_INJECAO": "1"}, "falha", "ACEITAR_INJECAO"),
    ({**ENV_CERTO, "VALIDADE_OS_H": "2"}, "alerta", "VALIDADE_OS_H"),
    ({**ENV_CERTO, "T_ASSENTAMENTO_S": "0,5"}, "alerta", "T_ASSENTAMENTO_S"),
], ids=["sem-env", "porta-do-simulador", "porta-default", "adapter-de-exemplo",
        "host-local", "injecao-ligada", "validade-curta", "assentamento-curto"])
def test_regras_do_env(conf, env, nivel, trecho):
    (problema,) = _conferir(conf, env=env)
    assert problema.nivel == nivel and trecho in problema.texto and problema.acao


@pytest.mark.parametrize("pacotes,trecho", [
    (None, "venv"),
    (PACOTES_CERTOS - {"fastapi", "uvicorn", "requests"}, "fastapi"),
    (PACOTES_CERTOS - {"cv2.aruco"}, "cv2.aruco"),
], ids=["sem-venv", "sem-servidor", "cv2-sem-aruco"])
def test_regras_dos_pacotes(conf, pacotes, trecho):
    (problema,) = _conferir(conf, pacotes=pacotes)
    assert problema.nivel == "falha" and trecho in problema.texto


@pytest.mark.parametrize("cameras,colide", [
    ({"esq": "0", "dir": "2"}, True),       # o índice 2 é o da mesa
    ({"esq": "0", "dir": "1"}, False),
    ({"esq": "", "dir": ""}, False),        # por nome: não dá para comparar
    ({"esq": "logitech", "dir": "2"}, True),
])
def test_camera_da_mesa_nao_pode_ser_a_de_um_dispenser(conf, cameras, colide):
    problemas = _conferir(conf, cameras=cameras)
    assert bool(problemas) is colide
    if colide:
        assert "mesma webcam" in problemas[0].texto


def test_venv_que_nao_executa_e_dito_com_a_causa(conf):
    """Venv copiado de outra máquina aponta para um Python que não existe aqui:
    dizer "faltam pacotes" mandaria instalar pacote num venv morto."""
    (problema,) = conf.conferir(MESA_CERTA, ENV_CERTO, None, {"esq": "", "dir": ""},
                                venv_erro=r"No Python at 'C:\Users\x\python.exe'")
    assert "NÃO executa" in problema.texto and "recrie" in problema.acao


def test_cameras_do_bat_le_os_dois_lados(conf):
    """`conf` só garante `vision/` no sys.path durante o módulo."""
    from conferencia import cameras_do_bat
    texto = "rem x\r\nset CAMERA_ESQ=\r\nset CAMERA_DIR=1\r\n"
    assert cameras_do_bat(texto) == {"esq": "", "dir": "1"}


def test_o_bat_versionado_nao_fixa_camera_por_indice():
    """O default `CAMERA_DIR=1` era o mesmo `camera.indice=1` da mesa."""
    sys.path.insert(0, str(VISION))
    try:
        from conferencia import ler_cameras_do_bat
        assert ler_cameras_do_bat() == {"esq": "", "dir": ""}
    finally:
        sys.path.remove(str(VISION))
