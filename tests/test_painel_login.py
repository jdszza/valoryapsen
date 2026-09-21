# -*- coding: utf-8 -*-
"""A porta de entrada do painel: freio, destino e CSRF.

`tests/test_painel_seguranca.py` cobre as quatro superfícies que estavam
ABERTAS (a rota que publicava PINs, o bloco `/api/*` sem token, o console do
Werkzeug na rede, a chave de sessão com default público). Este arquivo cobre as
três que estavam FRACAS, e as três compartilham o mesmo modo de falhar: o painel
funciona perfeitamente, e nada no log diz que a proteção não existe.

1. **`/login` sem freio de força bruta.** PIN de 4 dígitos são 10 mil
   candidatos, os nomes do seed estão no README, e não havia limite de
   tentativas. O central já freava o console e o `POST /auth/login`; a bancada,
   que é onde o PIN de fato está, não freava nada. E deixou de ser teórico: os
   três operadores da bancada estavam com o PIN do seed, incluindo o
   **Supervisor**, que é o perfil que libera a trava do Triple Check.
2. **Open redirect no `next=`.** `redirect(request.args.get("next"))` aceitava
   qualquer coisa: um link para `/login?next=https://…` fazia da página de login
   LEGÍTIMA do painel o trampolim para uma cópia dela.
3. **Nenhum CSRF e nenhum `SameSite`.** `/trava/liberar`,
   `/ordens/<id>/excluir`, `/admin/historico/limpar` e
   `/medicamentos/<id>/excluir` dependiam só do cookie — e a primeira delas
   escreve no computador central.
"""
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent
TEMPLATES = RAIZ_REPO / "painel_operador" / "backend" / "templates"


@pytest.fixture
def painel(carregar_painel):
    return carregar_painel()


# ══════════════════════════════════════════════════════════════════════════════
# 1. O freio de força bruta — a mesma regra do `console.py` do central
# ══════════════════════════════════════════════════════════════════════════════
#
# A chave é IP **e** nome, e isso é escolha. Só o IP puniria o turno inteiro por
# causa de um operador que erra o PIN — a bancada fala por um NAT só, e atrás de
# proxy todos caem no mesmo `remote_addr`. Só o nome deixaria qualquer um trancar
# a conta alheia de fora, que é negação de serviço disfarçada de proteção. O par
# **não** cobre varredura de muitos nomes a partir de um IP; o que ele resolve é
# adivinhar o PIN de uma conta conhecida, que é o caso real aqui.

def _tentar(painel, nome: str = "Administrador", pin: str = "9999"):
    return painel.cliente.post("/login", data={"nome": nome, "pin": pin})


def test_tentativas_demais_no_mesmo_nome_viram_429(painel):
    for _ in range(painel.modulo.LOGIN_MAX_TENTATIVAS):
        assert _tentar(painel).status_code == 200      # a tela de erro normal

    resposta = _tentar(painel)

    assert resposta.status_code == 429
    # O header é o que um cliente automático entende; a tela diz o mesmo.
    assert int(resposta.headers["Retry-After"]) >= 1


def test_o_freio_nao_tranca_o_operador_do_lado(painel):
    """Balde por `IP|nome`: o nome errado de um não pode trancar o outro."""
    for _ in range(painel.modulo.LOGIN_MAX_TENTATIVAS + 1):
        _tentar(painel, nome="Administrador")

    assert _tentar(painel, nome="Supervisor").status_code == 200


def test_o_freio_corre_antes_de_conferir_o_hash(painel, monkeypatch):
    """Conferir hash custa ~300 ms DE PROPÓSITO (ver `gerar_pin_hash`), e esse
    custo é de quem defende. Depois do freio, a tentativa bloqueada não paga."""
    chamadas = []
    monkeypatch.setattr(painel.modulo, "conferir_pin",
                        lambda h, p: chamadas.append(p) or False)

    for _ in range(painel.modulo.LOGIN_MAX_TENTATIVAS + 3):
        _tentar(painel)

    assert len(chamadas) == painel.modulo.LOGIN_MAX_TENTATIVAS


def test_pin_certo_zera_o_contador(painel):
    """Quem sabe o PIN não é varredura — e o balde some junto."""
    origem = "1.2.3.4|administrador"
    for _ in range(painel.modulo.LOGIN_MAX_TENTATIVAS):
        painel.modulo.login_registrar_falha(origem)
    assert painel.modulo.login_bloqueado(origem) > 0

    painel.modulo.login_limpar_falhas(origem)

    assert painel.modulo.login_bloqueado(origem) == 0


def test_a_janela_expira_sozinha(painel):
    """Sem relógio de parede: as duas funções aceitam o instante."""
    mod = painel.modulo
    origem = "1.2.3.4|admin"
    for _ in range(mod.LOGIN_MAX_TENTATIVAS):
        mod.login_registrar_falha(origem, agora=0.0)

    assert mod.login_bloqueado(origem, agora=1.0) > 0
    assert mod.login_bloqueado(origem, agora=mod.LOGIN_JANELA_S + 1) == 0


def test_o_balde_que_esvazia_e_removido(painel):
    """A chave é escolhida por quem tenta: um nome novo a cada tentativa
    deixaria uma entrada permanente por tentativa, num processo que não
    reinicia. O custo da varredura é o que mantém o número pequeno."""
    mod = painel.modulo
    for i in range(50):
        mod.login_registrar_falha(f"1.2.3.4|nome{i}", agora=0.0)
    assert len(mod._login_tentativas) == 50

    mod.login_registrar_falha("1.2.3.4|novo", agora=mod.LOGIN_JANELA_S + 1)

    assert list(mod._login_tentativas) == ["1.2.3.4|novo"]


def test_os_numeros_sao_os_do_central(painel):
    """Cópia e não import — o painel roda fora do Docker e não compartilha
    pacote com o central. Dois freios com janelas diferentes para o mesmo
    problema é o que faz alguém "consertar" um copiando o hábito do outro.

    Lido por AST e não por import: `console.py` importa `config`, que lê o
    ambiente do central inteiro. O que se quer aqui são dois números.
    """
    import ast

    from conftest import CENTRAL_DIR

    arvore = ast.parse((CENTRAL_DIR / "console.py").read_text(encoding="utf-8"))
    do_central = {
        no.targets[0].id: ast.literal_eval(no.value)
        for no in arvore.body
        if isinstance(no, ast.Assign) and isinstance(no.targets[0], ast.Name)
        and no.targets[0].id in ("MAX_TENTATIVAS", "JANELA_TENTATIVAS_S")
    }
    assert len(do_central) == 2, f"o extrator não achou os dois: {do_central}"

    assert painel.modulo.LOGIN_MAX_TENTATIVAS == do_central["MAX_TENTATIVAS"]
    assert painel.modulo.LOGIN_JANELA_S == do_central["JANELA_TENTATIVAS_S"]


# ══════════════════════════════════════════════════════════════════════════════
# 2. O `next=` do login não manda o operador para fora
# ══════════════════════════════════════════════════════════════════════════════

BARRA_INVERTIDA = chr(92)

FORA = [
    "https://exemplo.invalido/x",
    "//exemplo.invalido/x",                      # esquema relativo: sai daqui
    "/" + BARRA_INVERTIDA + "exemplo.invalido",  # browsers normalizam para /
    "http:/exemplo.invalido",
    "javascript:alert(1)",
    "",
    None,
]


@pytest.mark.parametrize("destino", FORA)
def test_next_de_fora_cai_no_padrao(painel, destino):
    assert painel.modulo.destino_seguro(destino, "/dashboard") == "/dashboard"


@pytest.mark.parametrize("destino", ["/ordens", "/ordens?status=Pendente",
                                     "/medicamentos#lote-3"])
def test_caminho_do_proprio_painel_e_respeitado(painel, destino):
    """Controle: recusar tudo transformaria o `next=` em enfeite, e quem
    voltasse de uma sessão expirada perderia a página em que estava."""
    assert painel.modulo.destino_seguro(destino, "/dashboard") == destino


def test_a_rota_de_login_usa_o_helper(painel):
    """A função certa e a rota não a chamando é o mesmo que não existir."""
    import ast

    fonte = (RAIZ_REPO / "painel_operador" / "backend" / "app.py"
             ).read_text(encoding="utf-8")
    for no in ast.walk(ast.parse(fonte)):
        if isinstance(no, ast.FunctionDef) and no.name == "login":
            chamadas = {c.func.id for c in ast.walk(no)
                        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            assert "destino_seguro" in chamadas
            return
    pytest.fail("a rota `login` não foi encontrada — o extrator quebrou")


# ══════════════════════════════════════════════════════════════════════════════
# 3. CSRF e o cookie de sessão
# ══════════════════════════════════════════════════════════════════════════════
#
# `SameSite=Lax` sozinho já resolveria nos browsers atuais. O token existe
# porque a suposição "todo browser da bancada é atual" é justamente a que
# ninguém confere: o painel roda no Windows do mini PC, e um atalho num
# navegador antigo não dá erro nenhum — só deixa de proteger.

ROTAS_DESTRUTIVAS = [
    "/trava/liberar",
    "/ordens/1/excluir",
    "/medicamentos/1/excluir",
    "/admin/historico/limpar",
]

FORMULARIOS = [
    ("dashboard.html", "web_liberar_trava"),
    ("ordens.html", "excluir_ordem"),
    ("medicamentos.html", "excluir_medicamento"),
    ("admin_historico.html", "admin_limpar_historico"),
]


def test_o_cookie_de_sessao_e_lax_e_httponly(painel):
    """A metade que não custa nada: o browser deixa de mandar o cookie em POST
    de outra origem, e a navegação normal segue igual. `Strict` quebraria voltar
    ao painel por um link externo, que é como o atalho da bancada abre."""
    assert painel.modulo.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert painel.modulo.app.config["SESSION_COOKIE_HTTPONLY"] is True


def test_secure_desligado_por_padrao_e_ligavel(carregar_painel, monkeypatch):
    """Ligado numa bancada em http, o flag faria o browser DESCARTAR o cookie e
    ninguém conseguiria logar — com "o login não funciona" e nada no log."""
    assert carregar_painel().modulo.app.config["SESSION_COOKIE_SECURE"] is False

    monkeypatch.setenv("APSEN_COOKIE_SECURE", "1")
    assert carregar_painel().modulo.app.config["SESSION_COOKIE_SECURE"] is True


@pytest.mark.parametrize("rota", ROTAS_DESTRUTIVAS)
def test_post_sem_token_e_recusado(painel, rota):
    painel.logar()

    assert painel.cliente.post(rota).status_code == 400


@pytest.mark.parametrize("rota", ROTAS_DESTRUTIVAS)
def test_post_com_token_de_outra_sessao_e_recusado(painel, rota):
    """O token é derivado da SESSÃO: o de outro operador não serve."""
    painel.logar()
    meu = painel.token_csrf()
    with painel.cliente.session_transaction() as sessao:
        sessao["op_id"] = 99
        sessao["op_nome"] = "Outro"

    resposta = painel.cliente.post(rota, data={painel.modulo.CSRF_CAMPO: meu})

    assert resposta.status_code == 400


def test_sem_sessao_o_csrf_nao_atropela_o_login(painel):
    """`login_required` roda ANTES: 400 aqui mandaria quem só perdeu a sessão
    caçar um problema que não existe."""
    resposta = painel.cliente.post("/trava/liberar")

    assert resposta.status_code == 302
    assert "/login" in resposta.headers["Location"]


@pytest.mark.parametrize("rota", ROTAS_DESTRUTIVAS)
def test_post_com_token_passa_do_portao(painel, rota):
    """Controle: sem ele, "400 em tudo" seria indistinguível de um CSRF que
    recusa até o formulário legítimo."""
    painel.logar()

    assert painel.post_form(rota).status_code != 400


@pytest.mark.parametrize("arquivo, rota", FORMULARIOS)
def test_o_formulario_da_tela_carrega_o_campo(arquivo, rota):
    """A metade que falta no servidor é o form que deixou de passar o token —
    e o sintoma dela é um 400 na cara do operador no meio do turno."""
    html = (TEMPLATES / arquivo).read_text(encoding="utf-8")

    assert rota in html, f"{arquivo} deixou de apontar para {rota}"
    assert "campo_csrf()" in html


def test_toda_rota_marcada_no_codigo_esta_coberta_aqui(painel):
    """Rota nova com `@csrf_protegido` e sem cobertura aqui não vale.

    A varredura é do `url_map`, como a do `api_token_required`: o marcador sobe
    pela pilha de decorators porque `functools.wraps` copia o `__dict__`.
    """
    app = painel.modulo.app
    protegidas = {
        regra.rule for regra in app.url_map.iter_rules()
        if getattr(app.view_functions[regra.endpoint], "_csrf_protegido", False)
    }
    cobertas = {r.replace("/1/", "/<int:id>/") for r in ROTAS_DESTRUTIVAS}

    assert protegidas == cobertas


# ══════════════════════════════════════════════════════════════════════════════
# 4. PIN de fábrica não sobrevive ao primeiro acesso
# ══════════════════════════════════════════════════════════════════════════════
#
# O hash sempre esteve certo; o problema era o SEGREDO. Os PINs do seed estão
# neste repositório, e na bancada os TRÊS operadores ainda estavam com eles —
# incluindo o **Supervisor**, que é o perfil que libera a trava do Triple Check.
# Somado a um `/login` sem freio, eram 4 dígitos com o valor publicado e
# tentativas ilimitadas.

def _pin_provisorio(painel, op_id: int = 1) -> bool:
    conn = painel.conexao()
    try:
        return bool(conn.execute(
            "SELECT pin_provisorio FROM operadores WHERE id=?", (op_id,)
        ).fetchone()["pin_provisorio"])
    finally:
        conn.close()


def test_o_seed_nasce_marcado_como_provisorio(painel):
    conn = painel.conexao()
    try:
        marcados = conn.execute(
            "SELECT COUNT(*) c FROM operadores WHERE pin_provisorio=1"
        ).fetchone()["c"]
        total = conn.execute("SELECT COUNT(*) c FROM operadores").fetchone()["c"]
    finally:
        conn.close()

    assert marcados == total > 0


def test_login_com_pin_de_fabrica_cai_na_troca(painel):
    resposta = painel.cliente.post("/login",
                                   data={"nome": "Administrador", "pin": "1234"})

    assert resposta.status_code == 302
    assert "/trocar-pin" in resposta.headers["Location"]


def test_sessao_ja_aberta_tambem_e_obrigada(painel):
    """O redirect no login sozinho não bastaria: quem já tem sessão — de antes
    desta versão, ou porque digitou a URL direto — continuaria navegando com o
    PIN publicado."""
    painel.logar(pin_provisorio=True)

    resposta = painel.cliente.get("/ordens")

    assert resposta.status_code == 302
    assert "/trocar-pin" in resposta.headers["Location"]


def test_a_propria_tela_de_troca_nao_entra_no_laco(painel):
    """Sem a lista de rotas livres, o guarda mandaria a tela de troca de volta
    para si mesma — redirect infinito na primeira entrada de todo mundo."""
    painel.logar(pin_provisorio=True)

    assert painel.cliente.get("/trocar-pin").status_code == 200
    assert painel.cliente.get("/logout").status_code in (200, 302)


def test_o_bloco_api_nao_e_afetado(painel):
    """`/api/*` autentica por token de MÁQUINA: o PIN de um operador não diz
    nada sobre ele, e o display pararia por causa de uma troca pendente na web."""
    painel.logar(pin_provisorio=True)

    assert painel.api("get", "/api/resumo").status_code == 200


def _trocar(painel, atual="1234", novo="4821", confirma=None):
    return painel.cliente.post("/trocar-pin", data={
        "pin_atual": atual, "pin_novo": novo,
        "pin_confirma": confirma if confirma is not None else novo,
    })


def test_a_troca_limpa_a_marca_e_solta_o_painel(painel):
    painel.logar(pin_provisorio=True)

    resposta = _trocar(painel)

    assert resposta.status_code == 302
    assert not _pin_provisorio(painel)
    assert painel.cliente.get("/ordens").status_code == 200


@pytest.mark.parametrize("atual, novo, confirma, motivo", [
    ("9999", "4821", "4821", "PIN atual errado"),
    ("1234", "482", "482", "menos de 4 dígitos"),
    ("1234", "abcd", "abcd", "não numérico"),
    ("1234", "4821", "4822", "confirmação diferente"),
    ("1234", "1234", "1234", "trocar pelo mesmo"),
    ("1234", "0001", "0001", "outro PIN de fábrica"),
])
def test_troca_recusada_mantem_a_marca(painel, atual, novo, confirma, motivo):
    """Os dois últimos são os jeitos de "trocar" sem trocar nada — e os dois
    sairiam daqui com `pin_provisorio=0` sobre um valor publicado."""
    painel.logar(pin_provisorio=True)

    resposta = _trocar(painel, atual, novo, confirma)

    assert resposta.status_code == 200, motivo
    assert _pin_provisorio(painel), motivo


def test_o_pin_novo_passa_a_valer_no_login(painel):
    """Controle: a troca precisa ter gravado um hash que o login reconheça."""
    painel.logar(pin_provisorio=True)
    _trocar(painel, novo="4821")
    painel.cliente.get("/logout")

    assert painel.cliente.post(
        "/login", data={"nome": "Administrador", "pin": "4821"}
    ).status_code == 302
    # `logout` entre os dois: com sessão aberta, `/login` redireciona para o
    # dashboard sem olhar o PIN, e o segundo assert mediria isso.
    painel.cliente.get("/logout")

    assert painel.cliente.post(
        "/login", data={"nome": "Administrador", "pin": "1234"}
    ).status_code == 200      # a tela de erro


def test_o_display_e_avisado_mas_nao_bloqueado(painel):
    """A troca acontece na web, onde há teclado. Bloquear o display deixaria a
    bancada sem operador até alguém achar um computador — e o PIN de fábrica
    continua sendo o de fábrica nos dois lugares."""
    conn = painel.conexao()
    try:
        resultado = painel.modulo._validar_pin_data(conn, "1234")
    finally:
        conn.close()

    assert resultado["ok"] is True
    assert resultado["pin_provisorio"] is True
