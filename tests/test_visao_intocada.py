# -*- coding: utf-8 -*-
"""O código das duas estações de visão não é editado por este repositório.

`vision/visao/` (as câmeras dos dispensers) e `vision/visao_mesa/` (a câmera da
mesa) vieram prontas de um pacote externo, e foi esse código que se ensaiou: a
estação da mesa rodou de ponta a ponta contra o vision-adapter real, e a dos
dispensers serviu o `/api/estado` com o catálogo vindo de fora. A integração com
o central acontece INTEIRA do lado de cá — no vision-adapter, no orquestrador e
na configuração de bancada (`config/*.json`, o `.env` da estação) —, e o código
delas fica como chegou.

A tentação que a regra existe para barrar é a natural de quem integra:
"melhorar" a visão por dentro. E é a mudança que nenhum outro teste daqui
enxerga — a suíte não importa cv2, e o que foi ensaiado é o código de antes. Um
limiar ajustado em `visao_mesa.py` invalida o ensaio inteiro em silêncio.

Manifesto, e não "o git diz que não mudou": o arquivo pode chegar alterado no
primeiro commit, num merge, ou numa cópia de volta de uma pasta de execução
(`vision/visao_esq/`). O manifesto foi gerado dos arquivos cujo md5 foi
conferido contra o pacote estudado, antes de qualquer outra mudança.

O hash é do conteúdo com `\\r\\n` trocado por `\\n`: o checkout no Windows troca o
fim de linha conforme o `core.autocrlf` de quem clonou, sem ninguém ter mexido
em nada — e um teste que reprova isso ensina a regenerar o manifesto, que é o
contrário do que ele existe para fazer.

Atualizar o pacote externo é decisão, não manutenção: quem a tomar confere o
md5 do pacote novo e só então regenera o dicionário abaixo.
"""
import hashlib
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent

MOTIVO = ("o código da visão não é editado por este repositório — "
          "ver CLAUDE.md, 'A visão real: o adapter traduz, a estação não muda'")

# Pastas varridas em profundidade (`rglob`) e pasta varrida só no nível de cima.
# A raiz de `visao_mesa` é rasa de propósito: abaixo dela estão `.venv/` e
# `dados/`, que não são código desta estação.
PASTAS_RECURSIVAS = (
    "vision/visao/src",
    "vision/visao/tests",
    "vision/visao_mesa/src",
    "vision/visao_mesa/tests",
    "vision/visao_mesa/integracao_apsen",
)
PASTAS_RASAS = (
    "vision/visao_mesa",
)

MANIFESTO = {
    "vision/visao/src/alertas.py":
        "9aaac906e495e604dfee5bb3fbed30d542e587c7d7b5b1da9fd47b788e1d0272",
    "vision/visao/src/api.py":
        "7b24b2e2ec426dbc6b6d3d8ddb7a23bb463c682330e5e8ab3fe3063a20c36784",
    "vision/visao/src/cadastrar_visual.py":
        "0aafc3dafc91965a995229c6fce929a1f4ad5173c49c1f8733754cd0926e4ddd",
    "vision/visao/src/calibrar.py":
        "f8f7432155f4ba3dbb1e5e6d20878879a5cb307eb513e1c507875e8248f92f12",
    "vision/visao/src/calibrar_estoque.py":
        "cd56a6e8fb1c8adf16d21d6034b0bdea315fe9bb8f2cc7e73cfe7762dff94737",
    "vision/visao/src/camera.py":
        "6b67ac55c83e40bfbf9480ef07c5ca933d150c3d8c23d4a21bea0cf3b9ebcce1",
    "vision/visao/src/configuracao.py":
        "b640554d9e58c995f3f8af234365221794a42ffdb4b50d982abf09c1fab560ac",
    "vision/visao/src/contagem.py":
        "4a4f787debb5e10df8a8751ff26e1733708a3ffba2a21d1723218b7c391144b5",
    "vision/visao/src/desenho.py":
        "0d5489063b93d1f5998bd0a684b4332816e46ed6ca8ea6776d8fdab2e527125a",
    "vision/visao/src/detector.py":
        "4b1367870cc31678d9f7242f59e97bc4bf1d911c3916929d27663ff52bc9567f",
    "vision/visao/src/diagnostico.py":
        "3732cb50b30cde47237487c7f24364997d8ccd2adb7892dabce546b279e71798",
    "vision/visao/src/estacao.py":
        "f64c6b55ac87053cb90fd8ce6676e514aa740f758df6c1fb30540caeb27d1e4c",
    "vision/visao/src/eventos.py":
        "fe5dea9b774411675f96d98725873992f9a6f2e212c2bca82b343d6298c1d994",
    "vision/visao/src/fps.py":
        "564ba96b0677693c3e3ddef0d5fea6b1fad8be0376ea3e0dbea3fc4ef177521a",
    "vision/visao/src/fusao.py":
        "740add7648602f5826aaaa53509fcdf82761bb6c4ccf2abc21b542e44e9373c6",
    "vision/visao/src/gerar_qrcodes.py":
        "1443c77a8b632ab892968ec7198293bbb24837612dfa95047adedaa7b1e3ccb1",
    "vision/visao/src/integracao_backend.py":
        "08a7ca45fb4edc36b062196b392b14d30a5d9f6b89ddd552776fbabd719f0f13",
    "vision/visao/src/interface.py":
        "25434e56343e0971b329744d5db0fdc3f9f70b4dbb5ba84cfa9817db204f03e7",
    "vision/visao/src/leitor_qr.py":
        "569d77768e3e5c51fb3edd32eda6a991f2f746c267681aae1d244cea46411469",
    "vision/visao/src/main.py":
        "aac29b27f767d35d674c3c4db4b4cd746be2c6bf3f9ba830c3d3fcbd960b06f7",
    "vision/visao/src/preprocessamento.py":
        "4cf70e90e3d8628028e22a4ffd954638ccc8d908c693269aa22cc08bf35f0e49",
    "vision/visao/src/reconhecimento.py":
        "e45bd0b41932ea5fdd91fd9249fe311846769bf332a697fd0c0b167ce08f5944",
    "vision/visao/src/testar.py":
        "e1a5f10448198501793cf940a172a1192c173320e0d1c62e3083531ad4bc1ac3",
    "vision/visao/tests/benchmark_pose.py":
        "25fe8d065c5b4d739f6352e796073482806d4efc4ad9e4dc1c12ab456223297d",
    "vision/visao/tests/benchmark_robustez.py":
        "55c114ca40a4e28bef7301c97159a7c927782f556de6cbc4b183e7fdf2cade9b",
    "vision/visao/tests/cena_sintetica.py":
        "7890606dc3a5255dde37f4b02de8417b9ce9086ee7d32eea788e55875f13e916",
    "vision/visao/tests/degradacao.py":
        "3c724476ebbb2d3a6b9393e8c942ce381f0b154a3257bdf9598e80cc259baa8b",
    "vision/visao/tests/embalagens.py":
        "d4ddbbac9e88de52e402b7d948d408c9da782371f4db352bcd69b3a5c4749f8f",
    "vision/visao/tests/preview_interface.py":
        "a9214c35bf7ab7721039847a0e0a1c7035db4ce13bf17ffdee7501ac6e4eed66",
    "vision/visao/tests/test_sistema.py":
        "aefa38bd7d9871a1df0238d88cf83e58cfafb4af476d0e82fcc1bddce22748b4",
    "vision/visao/tests/test_v2.py":
        "2661ee582cfeb607f8c55fa23805b8ae57d8a350f0e412a621f93557cca7a7bb",
    "vision/visao/tests/test_v2_helpers.py":
        "5a97ddc86ecca345683af6e12b534e6efe604f70c6e8ab391475f5da255fedce",
    "vision/visao_mesa/camera_finder.py":
        "ace65a359a86aa4177146ccdaa383a8d34e38159ec8ef21bc5aec2e4fed43ce7",
    "vision/visao_mesa/integracao_apsen/__init__.py":
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "vision/visao_mesa/integracao_apsen/cliente.py":
        "1ac2e9e26737e1140629b9dc93e50e4b3bd64a656a3e0b8dff23aa9919f96223",
    "vision/visao_mesa/integracao_apsen/config.py":
        "7563141685d644661a28e5d63ca96c00d1660fd1dff58f2a1b5f025da8731a1e",
    "vision/visao_mesa/integracao_apsen/contagem.py":
        "34ccf5e73aa2c89e6abc79bc7efb741977ee48609b8da3ca9d0f666034f79e65",
    "vision/visao_mesa/integracao_apsen/eventos.py":
        "4410e3e6b06fdf9fd7ce7ae8f59cb8563acada6bc5453380c9211be24146606c",
    "vision/visao_mesa/integracao_apsen/fake_adapter.py":
        "6eebf559542d3a81b9130d5aeab687c1c4ff59847d24ef6c0c118e554e91dfeb",
    "vision/visao_mesa/integracao_apsen/servidor.py":
        "4bc6a02a45e31c402a280973b5901bd983192f8ec1904a5814725cce061d1a6e",
    "vision/visao_mesa/integracao_apsen/testes/__init__.py":
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "vision/visao_mesa/integracao_apsen/testes/test_integracao.py":
        "59feef591fa7f48d69e1fe054f4fcfef904774af2837009fdae2ba35a76e3bef",
    "vision/visao_mesa/src/__init__.py":
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "vision/visao_mesa/src/autoluz.py":
        "70828d3fa0efc077ed97bbc8fdc06e30eb4b506a07f6be911325687bf9c84084",
    "vision/visao_mesa/src/calibrar.py":
        "0fe150bbd194abeacbb73b9c1082b71f9f1134bb3771ca3a49c43b0c021607dd",
    "vision/visao_mesa/src/camera.py":
        "31878cc37ec6b553c566cff9bdd9ac3764e223ec486e0217421f2453673f6305",
    "vision/visao_mesa/src/desenho.py":
        "7e8da7e282520aa26203765f5058fc871123ddd23c874f5a5d1c9c613e016a3f",
    "vision/visao_mesa/src/editor_fundo.py":
        "70d07e597ead1bb96d04c91babf1793a67f212bed517f1f1a35cac667b6573ce",
    "vision/visao_mesa/src/fluxo.py":
        "91589c193ce7dce6447229ac98eb8399529455a3218fc47470dd53cc144de881",
    "vision/visao_mesa/src/main.py":
        "713d673b1568d8529735bdfa2458b20f8fcbd58b5a2a6f0393d69db9ec5ab02d",
    "vision/visao_mesa/src/visao_mesa.py":
        "1bb1e99d82a24daddb2a3e4a4b025eae0886a1cfba6934101910a059bda89dbc",
    "vision/visao_mesa/tests/teste_pipeline.py":
        "58a25976f7518f2285a486e3a7fe958af12bfc55d21553025a9b51a12fbfd4fa",
}


def _sha256(caminho: Path) -> str:
    return hashlib.sha256(caminho.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _no_disco() -> dict[str, str]:
    """Todo `.py` das pastas vigiadas, com o hash de cada um.

    Disco, e não `git ls-files`: o arquivo que ainda não foi commitado é
    justamente o que precisa ser pego antes de entrar.
    """
    achados: dict[str, str] = {}
    for pasta in PASTAS_RECURSIVAS:
        for caminho in (RAIZ_REPO / pasta).rglob("*.py"):
            achados[caminho.relative_to(RAIZ_REPO).as_posix()] = _sha256(caminho)
    for pasta in PASTAS_RASAS:
        for caminho in (RAIZ_REPO / pasta).glob("*.py"):
            achados[caminho.relative_to(RAIZ_REPO).as_posix()] = _sha256(caminho)
    return achados


@pytest.fixture(scope="module")
def no_disco() -> dict[str, str]:
    return _no_disco()


def test_nenhum_arquivo_do_manifesto_sumiu(no_disco):
    sumiram = sorted(set(MANIFESTO) - set(no_disco))
    assert not sumiram, f"{MOTIVO}. Sumiram: {sumiram}"


def test_nenhum_arquivo_do_manifesto_mudou(no_disco):
    mudaram = sorted(caminho for caminho, esperado in MANIFESTO.items()
                     if caminho in no_disco and no_disco[caminho] != esperado)
    assert not mudaram, f"{MOTIVO}. Mudaram: {mudaram}"


def test_nenhum_py_novo_apareceu(no_disco):
    """Arquivo novo é edição também: um `.py` a mais em `src/` muda o que a
    estação importa, e o ensaio foi feito sem ele."""
    novos = sorted(set(no_disco) - set(MANIFESTO))
    assert not novos, f"{MOTIVO}. Apareceram: {novos}"


# ── Guarda da guarda ──────────────────────────────────────────────────────────
#
# Os três testes acima ficam verdes para sempre se a varredura parar de olhar
# para o lugar certo — uma pasta renomeada na lista, um padrão de glob errado.

@pytest.mark.parametrize("pasta", PASTAS_RECURSIVAS + PASTAS_RASAS)
def test_toda_pasta_vigiada_tem_arquivo_no_manifesto(pasta):
    """Pasta da lista sem nenhum arquivo no manifesto é pasta que ninguém vigia."""
    assert any(caminho.startswith(pasta + "/") for caminho in MANIFESTO), pasta


def test_todo_arquivo_do_manifesto_esta_numa_pasta_vigiada():
    """Fora das pastas varridas, o arquivo seria conferido se sumisse ou
    mudasse, mas o `.py` NOVO ao lado dele nunca seria visto."""
    def vigiado(caminho: str) -> bool:
        pai = caminho.rsplit("/", 1)[0]
        return (any(caminho.startswith(p + "/") for p in PASTAS_RECURSIVAS)
                or pai in PASTAS_RASAS)

    assert [c for c in MANIFESTO if not vigiado(c)] == []


def test_o_hash_ignora_o_fim_de_linha_e_mais_nada(tmp_path):
    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    outro = tmp_path / "outro.py"
    lf.write_bytes(b"limiar = 1\nmodo = 'caixa'\n")
    crlf.write_bytes(b"limiar = 1\r\nmodo = 'caixa'\r\n")
    outro.write_bytes(b"limiar = 2\nmodo = 'caixa'\n")

    assert _sha256(lf) == _sha256(crlf)
    assert _sha256(lf) != _sha256(outro)
