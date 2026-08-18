"""Download de relatório de OS pela IHM de manutenção.

Os botões CSV/XLSX eram âncoras para
`{BACKEND_URL}/api/v1/relatorio/os/{os_id}?formato=csv&token={jwt}`, e isso
errava duas vezes:

  * `BACKEND_URL` é `http://central-computer:8000`, nome DNS da rede Docker
    `apsen-net`. O container resolve; o navegador do operador, não — o
    download não acontecia;
  * o JWT ia na query string, logo no histórico do navegador, no `Referer` e
    no log de acesso do central.

Hoje quem busca o arquivo é o processo da IHM (server-side, dentro da rede) com
o token no header `Authorization`, e os bytes voltam pelo `dcc.Download`. Estes
testes prendem as duas metades: o token não volta para a URL e os bytes chegam
ao componente de download.
"""
import base64

import pytest


BACKEND_INTERNO = "http://central-computer:8000"


@pytest.fixture
def ihm(carregar_ihm):
    return carregar_ihm(env={"BACKEND_URL": BACKEND_INTERNO})


def _preparar_arquivo(ihm, conteudo=b"OS;qtd\nOS-1;10\n", nome="relatorio_OS-1.csv"):
    ihm.requests.status_code = 200
    ihm.requests.content = conteudo
    ihm.requests.headers = {"Content-Disposition": f"attachment; filename={nome}"}


# ── O token vai no header, não na URL ─────────────────────────────────────────

def test_token_vai_no_header_authorization(ihm):
    _preparar_arquivo(ihm)

    ihm.modulo._buscar_relatorio("OS-1", "csv", "jwt-secreto")

    (chamada,) = ihm.chamadas
    assert chamada["headers"]["Authorization"] == "Bearer jwt-secreto"


def test_token_nao_aparece_na_url_nem_nos_params(ihm):
    """O bug de privacidade: JWT no histórico do navegador e no log do central."""
    _preparar_arquivo(ihm)

    ihm.modulo._buscar_relatorio("OS-1", "csv", "jwt-secreto")

    (chamada,) = ihm.chamadas
    assert "jwt-secreto" not in chamada["url"]
    assert "token" not in chamada["url"]
    assert chamada["params"] == {"formato": "csv"}
    assert "token" not in (chamada["params"] or {})


def test_chamada_usa_o_host_interno_server_side(ihm):
    """O hostname da rede Docker é justamente o que só funciona AQUI."""
    _preparar_arquivo(ihm)

    ihm.modulo._buscar_relatorio("OS-1", "csv", "jwt")

    (chamada,) = ihm.chamadas
    assert chamada["url"] == f"{BACKEND_INTERNO}/api/v1/relatorio/os/OS-1"
    assert chamada["metodo"] == "GET"


# ── Os bytes chegam ao dcc.Download ───────────────────────────────────────────

def test_conteudo_e_repassado_ao_componente_de_download(ihm):
    _preparar_arquivo(ihm, conteudo=b"conteudo-do-csv")

    dados, erro = ihm.modulo._buscar_relatorio("OS-1", "csv", "jwt")

    assert erro == ""
    assert dados["base64"] is True
    assert base64.b64decode(dados["content"]) == b"conteudo-do-csv"
    assert dados["filename"] == "relatorio_OS-1.csv"
    assert dados["type"] == "text/csv"


def test_nome_do_arquivo_vem_do_content_disposition(ihm):
    _preparar_arquivo(ihm, nome="relatorio_OS-2024-001.xlsx")

    dados, _ = ihm.modulo._buscar_relatorio("OS-2024-001", "xlsx", "jwt")

    assert dados["filename"] == "relatorio_OS-2024-001.xlsx"
    assert dados["type"].endswith("spreadsheetml.sheet")


def test_sem_content_disposition_o_nome_cai_no_padrao(ihm):
    ihm.requests.status_code = 200
    ihm.requests.content = b"x"
    ihm.requests.headers = {}

    dados, _ = ihm.modulo._buscar_relatorio("OS-9", "xlsx", "jwt")

    assert dados["filename"] == "relatorio_OS-9.xlsx"


# ── Erros ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status, trecho", [
    (401, "Sessão expirada"),
    (404, "não encontrada"),
    (501, "Erro 501"),
])
def test_erro_do_backend_vira_mensagem_e_nao_download(ihm, status, trecho):
    ihm.requests.status_code = status
    ihm.requests.payload = {"detail": "openpyxl não instalado no servidor."}

    dados, erro = ihm.modulo._buscar_relatorio("OS-1", "csv", "jwt")

    assert dados is None
    assert trecho in erro


def test_formato_invalido_nao_chega_a_chamar_o_backend(ihm):
    dados, erro = ihm.modulo._buscar_relatorio("OS-1", "pdf", "jwt")

    assert dados is None
    assert "inválido" in erro
    assert ihm.chamadas == []


# ── O callback em si ──────────────────────────────────────────────────────────

class CtxFake:
    """Duplo do `dash.ctx`: só o `triggered_id` interessa aqui."""

    def __init__(self, triggered_id):
        self.triggered_id = triggered_id


def _clicar(ihm, monkeypatch, os_id="OS-1", formato="csv", token="jwt", n_clicks=None):
    monkeypatch.setattr(ihm.modulo, "ctx", CtxFake(
        {"type": "btn-relatorio", "index": os_id, "formato": formato}
    ))
    return ihm.modulo._baixar_relatorio(n_clicks if n_clicks is not None else [1, 0], token)


def test_clique_no_csv_devolve_o_arquivo_ao_download(ihm, monkeypatch):
    _preparar_arquivo(ihm, conteudo=b"linha1")

    dados, msg = _clicar(ihm, monkeypatch)

    assert base64.b64decode(dados["content"]) == b"linha1"
    assert msg is None                      # limpa mensagem de erro anterior
    (chamada,) = ihm.chamadas
    assert chamada["headers"]["Authorization"] == "Bearer jwt"


def test_clique_no_xlsx_pede_o_formato_certo(ihm, monkeypatch):
    _preparar_arquivo(ihm, nome="relatorio_OS-1.xlsx")

    _clicar(ihm, monkeypatch, formato="xlsx")

    (chamada,) = ihm.chamadas
    assert chamada["params"] == {"formato": "xlsx"}


def test_sem_clique_nao_baixa_nada(ihm, monkeypatch):
    """O callback é pattern-matching: dispara para todas as linhas da tabela."""
    _preparar_arquivo(ihm)

    dados, msg = _clicar(ihm, monkeypatch, n_clicks=[0, 0, None])

    assert dados is ihm.modulo.no_update
    assert msg is ihm.modulo.no_update
    assert ihm.chamadas == []


def test_sessao_sem_token_nao_chama_o_backend(ihm, monkeypatch):
    _preparar_arquivo(ihm)

    dados, msg = _clicar(ihm, monkeypatch, token=None)

    assert dados is ihm.modulo.no_update
    assert msg is not None                  # alerta de sessão expirada
    assert ihm.chamadas == []


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


def test_a_tabela_de_os_renderiza_os_botoes_que_o_callback_espera(ihm):
    """Se os ids divergirem do padrão do Input, o clique não chega ao callback."""
    ihm.requests.payload = [{"os_id": "OS-1", "status": "concluida",
                             "categoria": "Analgésicos", "criado_em": "2026-01-01"}]

    pagina = ihm.modulo._render_ordens("jwt")
    componentes = list(_percorrer(pagina))
    ids = [getattr(c, "id", None) for c in componentes]

    assert {"type": "btn-relatorio", "index": "OS-1", "formato": "csv"} in ids
    assert {"type": "btn-relatorio", "index": "OS-1", "formato": "xlsx"} in ids
    assert "msg-relatorio" in ids
    # O link antigo (host interno + token na URL) não pode voltar.
    hrefs = [getattr(c, "href", "") or "" for c in componentes]
    assert not any(BACKEND_INTERNO in h or "token=" in h for h in hrefs)
