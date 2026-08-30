"""Download de relatório de OS pelo app de manutenção e operação.

Os botões CSV/XLSX eram âncoras para
`{BACKEND_URL}/api/v1/relatorio/os/{os_id}?formato=csv&token={jwt}`, e isso
errava duas vezes:

  * `BACKEND_URL` é `http://central-computer:8000`, nome DNS da rede Docker
    `apsen-net`. O container resolve; o navegador do operador, não — o
    download não acontecia;
  * o JWT ia na query string, logo no histórico do navegador, no `Referer` e
    no log de acesso do central.

Hoje quem busca o arquivo é o processo do app de manutenção (server-side, dentro
da rede) com
o token no header `Authorization`, e os bytes voltam pelo `dcc.Download`. Estes
testes prendem as duas metades: o token não volta para a URL e os bytes chegam
ao componente de download.
"""
import base64

import pytest


BACKEND_INTERNO = "http://central-computer:8000"


@pytest.fixture
def manut(carregar_manut):
    return carregar_manut(env={"BACKEND_URL": BACKEND_INTERNO})


def _preparar_arquivo(manut, conteudo=b"OS;qtd\nOS-1;10\n", nome="relatorio_OS-1.csv"):
    manut.requests.status_code = 200
    manut.requests.content = conteudo
    manut.requests.headers = {"Content-Disposition": f"attachment; filename={nome}"}


# ── O token vai no header, não na URL ─────────────────────────────────────────

def test_token_vai_no_header_authorization(manut):
    _preparar_arquivo(manut)

    manut.modulo._buscar_relatorio("OS-1", "csv", "jwt-secreto")

    (chamada,) = manut.chamadas
    assert chamada["headers"]["Authorization"] == "Bearer jwt-secreto"


def test_token_nao_aparece_na_url_nem_nos_params(manut):
    """O bug de privacidade: JWT no histórico do navegador e no log do central."""
    _preparar_arquivo(manut)

    manut.modulo._buscar_relatorio("OS-1", "csv", "jwt-secreto")

    (chamada,) = manut.chamadas
    assert "jwt-secreto" not in chamada["url"]
    assert "token" not in chamada["url"]
    assert chamada["params"] == {"formato": "csv"}
    assert "token" not in (chamada["params"] or {})


def test_chamada_usa_o_host_interno_server_side(manut):
    """O hostname da rede Docker é justamente o que só funciona AQUI."""
    _preparar_arquivo(manut)

    manut.modulo._buscar_relatorio("OS-1", "csv", "jwt")

    (chamada,) = manut.chamadas
    assert chamada["url"] == f"{BACKEND_INTERNO}/api/v1/relatorio/os/OS-1"
    assert chamada["metodo"] == "GET"


# ── Os bytes chegam ao dcc.Download ───────────────────────────────────────────

def test_conteudo_e_repassado_ao_componente_de_download(manut):
    _preparar_arquivo(manut, conteudo=b"conteudo-do-csv")

    dados, erro = manut.modulo._buscar_relatorio("OS-1", "csv", "jwt")

    assert erro == ""
    assert dados["base64"] is True
    assert base64.b64decode(dados["content"]) == b"conteudo-do-csv"
    assert dados["filename"] == "relatorio_OS-1.csv"
    assert dados["type"] == "text/csv"


def test_nome_do_arquivo_vem_do_content_disposition(manut):
    _preparar_arquivo(manut, nome="relatorio_OS-2024-001.xlsx")

    dados, _ = manut.modulo._buscar_relatorio("OS-2024-001", "xlsx", "jwt")

    assert dados["filename"] == "relatorio_OS-2024-001.xlsx"
    assert dados["type"].endswith("spreadsheetml.sheet")


def test_sem_content_disposition_o_nome_cai_no_padrao(manut):
    manut.requests.status_code = 200
    manut.requests.content = b"x"
    manut.requests.headers = {}

    dados, _ = manut.modulo._buscar_relatorio("OS-9", "xlsx", "jwt")

    assert dados["filename"] == "relatorio_OS-9.xlsx"


# ── Erros ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status, trecho", [
    (401, "Sessão expirada"),
    (404, "não encontrada"),
    (501, "Erro 501"),
])
def test_erro_do_backend_vira_mensagem_e_nao_download(manut, status, trecho):
    manut.requests.status_code = status
    manut.requests.payload = {"detail": "openpyxl não instalado no servidor."}

    dados, erro = manut.modulo._buscar_relatorio("OS-1", "csv", "jwt")

    assert dados is None
    assert trecho in erro


def test_formato_invalido_nao_chega_a_chamar_o_backend(manut):
    dados, erro = manut.modulo._buscar_relatorio("OS-1", "pdf", "jwt")

    assert dados is None
    assert "inválido" in erro
    assert manut.chamadas == []


# ── O callback em si ──────────────────────────────────────────────────────────

class CtxFake:
    """Duplo do `dash.ctx`: só o `triggered_id` interessa aqui."""

    def __init__(self, triggered_id):
        self.triggered_id = triggered_id


def _clicar(manut, monkeypatch, os_id="OS-1", formato="csv", token="jwt", n_clicks=None):
    monkeypatch.setattr(manut.modulo, "ctx", CtxFake(
        {"type": "btn-relatorio", "index": os_id, "formato": formato}
    ))
    return manut.modulo._baixar_relatorio(n_clicks if n_clicks is not None else [1, 0], token)


def test_clique_no_csv_devolve_o_arquivo_ao_download(manut, monkeypatch):
    _preparar_arquivo(manut, conteudo=b"linha1")

    dados, msg = _clicar(manut, monkeypatch)

    assert base64.b64decode(dados["content"]) == b"linha1"
    assert msg is None                      # limpa mensagem de erro anterior
    (chamada,) = manut.chamadas
    assert chamada["headers"]["Authorization"] == "Bearer jwt"


def test_clique_no_xlsx_pede_o_formato_certo(manut, monkeypatch):
    _preparar_arquivo(manut, nome="relatorio_OS-1.xlsx")

    _clicar(manut, monkeypatch, formato="xlsx")

    (chamada,) = manut.chamadas
    assert chamada["params"] == {"formato": "xlsx"}


def test_sem_clique_nao_baixa_nada(manut, monkeypatch):
    """O callback é pattern-matching: dispara para todas as linhas da tabela."""
    _preparar_arquivo(manut)

    dados, msg = _clicar(manut, monkeypatch, n_clicks=[0, 0, None])

    assert dados is manut.modulo.no_update
    assert msg is manut.modulo.no_update
    assert manut.chamadas == []


def test_sessao_sem_token_nao_chama_o_backend(manut, monkeypatch):
    _preparar_arquivo(manut)

    dados, msg = _clicar(manut, monkeypatch, token=None)

    assert dados is manut.modulo.no_update
    assert msg is not None                  # alerta de sessão expirada
    assert manut.chamadas == []


# ── Os botões que disparam o callback ─────────────────────────────────────────

def _percorrer(no):
    """Componentes da árvore, em profundidade."""
    yield no
    filhos = getattr(no, "children", None)
    if isinstance(filhos, (list, tuple)):
        for filho in filhos:
            yield from _percorrer(filho)
    elif filhos is not None:
        yield from _percorrer(filhos)


def test_a_tabela_de_os_renderiza_os_botoes_que_o_callback_espera(manut):
    """Se os ids divergirem do padrão do Input, o clique não chega ao callback."""
    manut.requests.payload = [{"os_id": "OS-1", "status": "concluida",
                             "categoria": "Analgésicos", "criado_em": "2026-01-01"}]

    pagina = manut.modulo._render_ordens("jwt")
    componentes = list(_percorrer(pagina))
    ids = [getattr(c, "id", None) for c in componentes]

    assert {"type": "btn-relatorio", "index": "OS-1", "formato": "csv"} in ids
    assert {"type": "btn-relatorio", "index": "OS-1", "formato": "xlsx"} in ids
    assert "msg-relatorio" in ids
    # O link antigo (host interno + token na URL) não pode voltar.
    hrefs = [getattr(c, "href", "") or "" for c in componentes]
    assert not any(BACKEND_INTERNO in h or "token=" in h for h in hrefs)
