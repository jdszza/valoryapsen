"""A trava do Triple Check chega ao painel de bancada — e o supervisor a libera.

O painel espelha o central de mão única, e `central_client.py` só tem GET.
A liberação da trava é a ÚNICA exceção, e ela vive sozinha em
`central_comandos.py`: autentica com uma conta de serviço, manda `em_nome_de`
com o nome do supervisor que digitou o PIN, e nunca levanta. O que estes
testes prendem:

  1. quem pode: perfil com `trava_liberar` (Supervisor e Admin). PCP, PCM e
     Operador tomam recusa NA ROTA, não só botão escondido;
  2. sem `PAINEL_CENTRAL_USER` a liberação aparece desligada, com a mensagem
     dizendo qual variável definir — e o processo sobe;
  3. central fora do ar não derruba a thread: nem a leitura da trava, nem a
     escrita;
  4. a mutação que importa: `central_client.py` continua sem post/put, e a
     única escrita do painel no central está em `central_comandos.py`.
"""
import pytest

TRAVA_ATIVA = {
    "ativa": True, "os_id": "OS-INFECTO-01-20260909T143012-A1B2C3", "slot_id": 3,
    "motivo": "Triple Check FALHOU (1/3 fontes divergentes, limiar=1) — D3: balança: desvio=10.0%",
}
SEM_TRAVA = {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""}

CONTA = {"PAINEL_CENTRAL_USER": "painel-bancada", "PAINEL_CENTRAL_SENHA": "segredo-servico"}


@pytest.fixture
def painel(carregar_painel):
    """Painel com a conta de serviço configurada e o central respondendo."""
    painel = carregar_painel(trava_central=TRAVA_ATIVA, env=CONTA)
    painel.modulo.central_comandos.esquecer_sessao()
    # O `requests` do módulo é o duplo do conftest: o login devolve um token.
    painel.modulo.central_comandos.requests.payload = {"token": "jwt-de-teste"}
    return painel


def _posts(painel) -> list:
    return [c for c in painel.modulo.central_comandos.requests.chamadas
            if c["metodo"] == "POST"]


def _liberacoes(painel) -> list:
    return [c for c in _posts(painel) if c["url"].endswith("/api/v1/admin/liberar-trava")]


def _historico(painel) -> list:
    conn = painel.conexao()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM historico WHERE acao LIKE 'Liberar trava%' ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()


# ── 1. Quem pode ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("perfil", ["Supervisor", "Admin"])
def test_supervisor_e_admin_liberam_em_nome_de_quem_logou(painel, perfil):
    painel.logar(perfil)
    with painel.cliente.session_transaction() as sessao:
        sessao["op_nome"] = "Maria Silva"

    resposta = painel.post_form("/trava/liberar", follow_redirects=False)

    assert resposta.status_code == 302
    (chamada,) = _liberacoes(painel)
    assert chamada["json"] == {"em_nome_de": "Maria Silva"}
    assert chamada["headers"]["Authorization"] == "Bearer jwt-de-teste"
    (registro,) = _historico(painel)
    assert registro["operador"] == "Maria Silva"
    assert registro["perfil"] == perfil
    assert registro["acao"] == "Liberar trava"


@pytest.mark.parametrize("perfil", ["PCP", "PCM", "Operador"])
def test_outros_perfis_tomam_403_na_rota_e_nada_sai_para_o_central(painel, perfil):
    """Botão escondido não é proteção: a rota confere a permissão no servidor."""
    painel.logar(perfil)

    resposta = painel.post_form("/trava/liberar")

    assert resposta.status_code == 403
    assert _liberacoes(painel) == []
    assert _historico(painel) == []


def test_sem_sessao_a_rota_manda_para_o_login(painel):
    resposta = painel.post_form("/trava/liberar", follow_redirects=False)
    assert resposta.status_code == 302
    assert "/login" in resposta.headers["Location"]
    assert _liberacoes(painel) == []


def test_a_permissao_e_so_de_supervisor_e_admin(painel):
    permissoes = painel.modulo.PERMISSOES
    assert "Supervisor" in painel.modulo.PERFIS
    assert {p for p, perms in permissoes.items() if perms.get("trava_liberar")} == {"Admin", "Supervisor"}
    # Todo perfil declara a permissão — ausente cairia em False por acidente,
    # e um perfil novo copiado de outro herdaria o silêncio.
    for perfil, perms in permissoes.items():
        assert "trava_liberar" in perms, perfil


def test_supervisor_entra_na_web_e_ve_o_dashboard(painel):
    """Diferente do Operador, que só usa o display."""
    conn = painel.conexao()
    try:
        conn.execute(
            "INSERT INTO operadores (nome, pin_hash, perfil, ativo, data_criacao) VALUES (?,?,?,1,?)",
            ("Sup Teste", painel.modulo.gerar_pin_hash("9876"), "Supervisor", "2026-01-01"),
        )
        conn.commit()
    finally:
        conn.close()

    resposta = painel.cliente.post("/login", data={"nome": "Sup Teste", "pin": "9876"},
                                   follow_redirects=False)
    assert resposta.status_code == 302
    with painel.cliente.session_transaction() as sessao:
        assert sessao["perfil"] == "Supervisor"


# ── 2. A tela ─────────────────────────────────────────────────────────────────

def test_dashboard_mostra_a_faixa_com_motivo_os_e_slot(painel):
    painel.modulo.sincronizar_trava_central()
    painel.logar("PCP")

    html = painel.cliente.get("/").get_data(as_text=True)

    assert 'id="trava-banner"' in html
    assert TRAVA_ATIVA["os_id"] in html
    assert "D3" in html
    assert "balança: desvio=10.0%" in html


def test_botao_liberar_so_aparece_para_quem_pode(painel):
    """Botão que o backend vai recusar não é botão desabilitado — é ausente."""
    painel.modulo.sincronizar_trava_central()

    painel.logar("Supervisor")
    com = painel.cliente.get("/").get_data(as_text=True)
    assert 'id="btn-liberar-trava"' in com
    assert "/trava/liberar" in com

    painel.logar("PCP")
    sem = painel.cliente.get("/").get_data(as_text=True)
    assert 'id="trava-banner"' in sem            # vê a trava…
    assert 'id="btn-liberar-trava"' not in sem   # …mas não o botão
    assert "/trava/liberar" not in sem


def test_sem_trava_nao_ha_faixa(carregar_painel):
    painel = carregar_painel(trava_central=SEM_TRAVA, env=CONTA)
    painel.modulo.sincronizar_trava_central()
    painel.logar("Supervisor")

    html = painel.cliente.get("/").get_data(as_text=True)

    assert 'id="trava-banner"' not in html


# ── 3. Sem conta de serviço: desligado, com mensagem, e o processo sobe ──────

def test_sem_conta_de_servico_a_liberacao_aparece_desligada(carregar_painel):
    painel = carregar_painel(trava_central=TRAVA_ATIVA,
                             env={"PAINEL_CENTRAL_USER": None, "PAINEL_CENTRAL_SENHA": None})
    comandos = painel.modulo.central_comandos
    assert comandos.habilitado() is False

    ok, msg = comandos.liberar_trava("Maria")
    assert ok is False
    assert "PAINEL_CENTRAL_USER" in msg and "PAINEL_CENTRAL_SENHA" in msg
    assert _liberacoes(painel) == []

    # Na tela: a faixa continua (a trava existe), o botão dá lugar à frase.
    painel.modulo.sincronizar_trava_central()
    painel.logar("Supervisor")
    html = painel.cliente.get("/").get_data(as_text=True)
    assert 'id="trava-banner"' in html
    assert 'id="btn-liberar-trava"' not in html
    assert "PAINEL_CENTRAL_USER" in html


def test_sem_conta_de_servico_a_rota_recusa_com_a_mesma_mensagem(carregar_painel):
    painel = carregar_painel(trava_central=TRAVA_ATIVA,
                             env={"PAINEL_CENTRAL_USER": None, "PAINEL_CENTRAL_SENHA": None})
    painel.logar("Admin")

    resposta = painel.post_form("/trava/liberar", follow_redirects=True)

    assert resposta.status_code == 200
    assert "PAINEL_CENTRAL_USER" in resposta.get_data(as_text=True)
    (registro,) = _historico(painel)
    assert registro["acao"] == "Liberar trava (recusada)"


def test_com_integracao_desligada_a_liberacao_tambem_esta(carregar_painel):
    """`PAINEL_CENTRAL=0` desliga tudo que fala com o central — inclusive isto."""
    painel = carregar_painel(env={**CONTA, "PAINEL_CENTRAL": "0"})
    comandos = painel.modulo.central_comandos
    assert comandos.habilitado() is False
    ok, msg = comandos.liberar_trava("Maria")
    assert ok is False and "PAINEL_CENTRAL=0" in msg


# ── 4. Central fora do ar não derruba nada ───────────────────────────────────

def test_central_fora_do_ar_nao_derruba_a_leitura_da_trava(painel):
    painel.modulo.sincronizar_trava_central()
    assert painel.modulo.trava_atual()["ativa"] is True

    painel.central.fora_do_ar = True
    estado, mudou = painel.modulo.sincronizar_trava_central()

    assert (estado, mudou) == (None, False)
    # O último estado conhecido fica, marcado como não confirmado.
    atual = painel.modulo.trava_atual()
    assert atual["ativa"] is True and atual["fonte_ok"] is False


def test_central_fora_do_ar_nao_derruba_a_escrita(painel, monkeypatch):
    comandos = painel.modulo.central_comandos

    def _explode(*args, **kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(comandos.requests, "post", _explode)

    ok, msg = comandos.liberar_trava("Maria")

    assert ok is False
    assert "indispon" in msg


def test_o_laco_do_espelho_sobrevive_a_excecao_na_trava(painel, monkeypatch):
    """A thread de sincronização é uma só; a trava não pode derrubá-la."""
    def _explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(painel.modulo.central_client, "trava_estado", _explode)
    with pytest.raises(RuntimeError):
        painel.modulo.sincronizar_trava_central()
    # Quem protege é o `try/except` de `central_sync_worker`; aqui provamos que
    # a chamada está DENTRO dele lendo o fonte do laço.
    import inspect
    fonte = inspect.getsource(painel.modulo.central_sync_worker)
    bloco_try = fonte.split("try:", 1)[1].split("except", 1)[0]
    assert "sincronizar_trava_central(" in bloco_try


# ── 5. O espelho guarda o anterior e só acusa mudança de verdade ─────────────

def test_a_primeira_leitura_e_uma_mudanca_e_a_repeticao_nao(painel):
    estado, mudou = painel.modulo.sincronizar_trava_central()
    assert mudou is True and estado["ativa"] is True

    _, mudou = painel.modulo.sincronizar_trava_central()
    assert mudou is False

    painel.central.trava = SEM_TRAVA
    estado, mudou = painel.modulo.sincronizar_trava_central()
    assert mudou is True and estado["ativa"] is False
    assert painel.modulo._trava_espelho["anterior"]["ativa"] is True


def test_a_trava_e_lida_no_mesmo_ciclo_das_ordens(painel):
    """Uma leitura a mais por ciclo, não uma thread a mais."""
    import inspect
    fonte = inspect.getsource(painel.modulo.central_sync_worker)
    assert "sincronizar_ordens_central(conn)" in fonte
    assert "sincronizar_trava_central(avisar_display=True)" in fonte


# ── 6. A sessão na conta de serviço ──────────────────────────────────────────

def test_o_jwt_e_cacheado_entre_liberacoes(painel):
    comandos = painel.modulo.central_comandos
    assert comandos.liberar_trava("A")[0] is True
    assert comandos.liberar_trava("B")[0] is True

    logins = [c for c in _posts(painel) if c["url"].endswith("/auth/login")]
    assert len(logins) == 1
    assert logins[0]["json"] == {"username": "painel-bancada", "senha": "segredo-servico"}
    assert len(_liberacoes(painel)) == 2


def test_401_refaz_o_login_uma_vez_e_repete(painel, monkeypatch):
    comandos = painel.modulo.central_comandos
    from conftest import RespostaFake
    sequencia = []

    def _post(url, json=None, **kwargs):
        sequencia.append(url.rsplit("/", 1)[-1])
        if url.endswith("/auth/login"):
            return RespostaFake(200, {"token": f"jwt-{len(sequencia)}"})
        # A primeira liberação toma 401 (token vencido); a segunda passa.
        return RespostaFake(401 if sequencia.count("liberar-trava") == 1 else 200, {})

    monkeypatch.setattr(comandos.requests, "post", _post)

    ok, _ = comandos.liberar_trava("Maria")

    assert ok is True
    assert sequencia == ["login", "liberar-trava", "login", "liberar-trava"]


@pytest.mark.parametrize("status, trecho", [(403, "role supervisor"), (409, "nenhuma trava"),
                                            (500, "HTTP 500")])
def test_recusas_do_central_viram_mensagem_e_nao_excecao(painel, status, trecho):
    comandos = painel.modulo.central_comandos
    comandos._jwt()                                  # login com 200
    comandos.requests.status_code = status

    ok, msg = comandos.liberar_trava("Maria")

    assert ok is False
    assert trecho in msg


# ── 7. A mutação que importa ──────────────────────────────────────────────────

def test_central_client_continua_sem_escrita(painel):
    fonte = open(painel.modulo.central_client.__file__, encoding="utf-8").read()
    for verbo in ("requests.post", "requests.put", "requests.patch", "requests.delete"):
        assert verbo not in fonte
    assert "def trava_estado" in fonte       # a LEITURA da trava mora lá


def test_a_unica_escrita_do_painel_no_central_esta_em_central_comandos(painel):
    """Varre o backend inteiro: um `requests.post` para o central fora de
    `central_comandos.py` é uma segunda porta de escrita sem nome."""
    from pathlib import Path
    pasta = Path(painel.modulo.central_comandos.__file__).parent
    for arquivo in pasta.glob("*.py"):
        if arquivo.name == "central_comandos.py":
            continue
        assert "liberar-trava" not in arquivo.read_text(encoding="utf-8"), arquivo.name
    docstring = " ".join(painel.modulo.central_comandos.__doc__.split())
    assert "mão única" in docstring and "única exceção" in docstring
