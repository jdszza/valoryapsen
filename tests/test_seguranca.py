"""Controle de acesso do central.

Três furos que permitiam burlar o perfil de usuário:

  * `SECRET_KEY` com o valor default — que está VERSIONADO neste repositório.
    Quem o tem assina um JWT `role=admin`, libera a trava do Triple Check e
    mexe em usuários. O código só emitia um warning no boot;
  * `_get_tecnico` apenas decodificava o token. Técnico desativado seguia com
    acesso total até o token expirar (8h), e role rebaixada continuava valendo
    pelo mesmo tempo — o token é assinado, não dá para "editar" o que já foi
    emitido;
  * CORS `*` num serviço autenticado: qualquer página aberta no browser do
    técnico podia disparar requisição em nome dele.

E dois que sobreviveram à primeira rodada, porque estavam fora do caminho que
ela varreu:

  * `GET /api/v1/relatorio/os/{os_id}` decodificava o token por conta própria,
    sem passar por `_get_tecnico`. Era a ÚNICA rota autenticada assim — e,
    justamente, a que exporta o histórico de dispensação nominal da OS. Técnico
    desativado perdia o app de manutenção inteiro e seguia baixando relatório
    por até 8h;
  * `POST /auth/login` não tinha freio de força bruta, embora o console — que
    protege menos — tivesse. É a rota que emite o JWT de `admin`, o username do
    seed está no README e tentar não custava nada a quem tentasse.
"""
import ast
import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from conftest import requisicao

CENTRAL_DIR = Path(__file__).resolve().parent.parent / "central-computer"


@pytest.fixture(scope="module")
def config():
    if str(CENTRAL_DIR) not in sys.path:
        sys.path.insert(0, str(CENTRAL_DIR))
    import config as modulo
    return modulo


# ── SECRET_KEY ────────────────────────────────────────────────────────────────

def test_secret_key_default_impede_o_boot(config):
    """Warning não protege ninguém: em produção o boot tem que falhar."""
    with pytest.raises(config.ConfiguracaoInsegura) as exc:
        config.validar_secret_key(config._DEFAULT_SECRET_KEY, "prod")

    assert "default" in str(exc.value)
    assert "secrets.token_hex" in str(exc.value)      # a receita vai no erro


@pytest.mark.parametrize("chave", ["", "curta-demais", "x" * 31])
def test_secret_key_vazia_ou_curta_impede_o_boot(config, chave):
    with pytest.raises(config.ConfiguracaoInsegura):
        config.validar_secret_key(chave, "prod")


def test_ambiente_dev_tolera_com_aviso(config, caplog):
    """A saída de escape é explícita: `APSEN_ENV=dev`, não o silêncio."""
    config.validar_secret_key(config._DEFAULT_SECRET_KEY, "dev")   # não levanta

    assert any("APSEN_ENV=dev" in r.getMessage() for r in caplog.records)


def test_chave_forte_passa_em_qualquer_ambiente(config):
    config.validar_secret_key("a" * 64, "prod")
    config.validar_secret_key("a" * 64, "dev")


def test_valor_default_continua_sendo_o_que_o_compose_nao_usa(config):
    """Se alguém trocar o default, o teste acima perde o sentido — ancora aqui."""
    compose = (CENTRAL_DIR.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert config._DEFAULT_SECRET_KEY not in compose, (
        "docker-compose.yml voltou a fixar a SECRET_KEY default"
    )


# ── Revalidação do usuário a cada requisição ──────────────────────────────────

def _credenciais(central, token: str):
    from fastapi.security import HTTPAuthorizationCredentials
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _usuario_no_banco(central, monkeypatch, **campos):
    """Instala a resposta de `get_usuario` (que já filtra `ativo=1`)."""
    usuario = {"username": "tec1", "nome_completo": "Técnico 1",
               "role": "manutencao", "ativo": 1, **campos}
    monkeypatch.setattr(central.modulo, "get_usuario",
                        lambda username: usuario if usuario else None)
    return usuario


def test_token_de_usuario_ativo_e_aceito(carregar_central, monkeypatch):
    central = carregar_central()
    _usuario_no_banco(central, monkeypatch)
    token = central.modulo.criar_token("tec1", "Técnico 1", "manutencao")

    user = central.modulo._get_tecnico(_credenciais(central, token))

    assert user["sub"] == "tec1"
    assert user["role"] == "manutencao"


def test_token_valido_de_usuario_desativado_e_rejeitado(carregar_central, monkeypatch):
    """O furo: o token continuava valendo por até 8h depois da desativação."""
    central = carregar_central()
    token = central.modulo.criar_token("tec1", "Técnico 1", "manutencao")
    # `get_usuario` filtra `ativo=1` — desativado some da consulta.
    monkeypatch.setattr(central.modulo, "get_usuario", lambda username: None)

    with pytest.raises(HTTPException) as exc:
        central.modulo._get_tecnico(_credenciais(central, token))

    assert exc.value.status_code == 401
    assert "inativo" in exc.value.detail


def test_role_vem_do_banco_e_nao_do_token(carregar_central, monkeypatch):
    """Token forjado (ou emitido antes do rebaixamento) não vira admin."""
    central = carregar_central()
    _usuario_no_banco(central, monkeypatch, role="manutencao")
    # Token diz admin — assinado com a chave certa, mas o banco discorda.
    token = central.modulo.criar_token("tec1", "Técnico 1", "admin")

    user = central.modulo._get_tecnico(_credenciais(central, token))
    assert user["role"] == "manutencao"

    with pytest.raises(HTTPException) as exc:
        central.modulo._get_admin(user)
    assert exc.value.status_code == 403


def test_admin_de_verdade_passa(carregar_central, monkeypatch):
    central = carregar_central()
    _usuario_no_banco(central, monkeypatch, role="admin")
    token = central.modulo.criar_token("tec1", "Técnico 1", "manutencao")

    user = central.modulo._get_tecnico(_credenciais(central, token))

    assert central.modulo._get_admin(user)["role"] == "admin"


def test_banco_fora_do_ar_nega_acesso(carregar_central, monkeypatch):
    """Falha de revalidação é 401, nunca "deixa passar por precaução"."""
    central = carregar_central()

    def _explode(username):
        raise RuntimeError("MySQL server has gone away")

    monkeypatch.setattr(central.modulo, "get_usuario", _explode)
    token = central.modulo.criar_token("tec1", "Técnico 1", "admin")

    with pytest.raises(HTTPException) as exc:
        central.modulo._get_tecnico(_credenciais(central, token))
    assert exc.value.status_code == 401


def test_revalidacao_usa_cache_curto(carregar_central, monkeypatch):
    """Uma query por request derrubaria o throughput do app de manutenção."""
    central = carregar_central()
    consultas = []

    def _contar(username):
        consultas.append(username)
        return {"username": username, "role": "manutencao", "ativo": 1}

    monkeypatch.setattr(central.modulo, "get_usuario", _contar)
    token = central.modulo.criar_token("tec1", "Técnico 1", "manutencao")

    for _ in range(10):
        central.modulo._get_tecnico(_credenciais(central, token))

    assert len(consultas) == 1


def test_desativar_usuario_invalida_o_cache_na_hora(carregar_central, monkeypatch):
    """O atraso do cache não pode valer para o botão "Desativar" do painel."""
    central = carregar_central()
    ativo = {"valor": True}
    monkeypatch.setattr(
        central.modulo, "get_usuario",
        lambda username: {"username": username, "role": "manutencao", "ativo": 1}
        if ativo["valor"] else None,
    )
    monkeypatch.setattr(central.modulo, "toggle_usuario_ativo",
                        lambda username, valor: {"ok": True})
    token = central.modulo.criar_token("tec1", "Técnico 1", "manutencao")
    central.modulo._get_tecnico(_credenciais(central, token))      # aquece o cache

    ativo["valor"] = False
    central.modulo.desativar_usuario("tec1", {"sub": "admin", "role": "admin"})

    with pytest.raises(HTTPException) as exc:
        central.modulo._get_tecnico(_credenciais(central, token))
    assert exc.value.status_code == 401


# ── O relatório passou a usar a mesma porta que o resto ───────────────────────

def _relatorio(central, token):
    """Chama o endpoint como o FastAPI chamaria: a dependência resolvida à mão.

    `Depends(_get_tecnico)` só é resolvido pelo framework; aqui o teste faz o
    mesmo passo, que é exatamente o que prova que a rota depende dele.
    """
    user = central.modulo._get_tecnico(_credenciais(central, token))
    return asyncio.run(central.modulo.relatorio_os("OS-1", "csv", user))


def test_relatorio_recusa_token_de_usuario_desativado(carregar_central, monkeypatch):
    """O furo: a rota tinha `decodificar_token` próprio e ignorava o banco."""
    central = carregar_central()
    token = central.modulo.criar_token("tec1", "Técnico 1", "manutencao")
    monkeypatch.setattr(central.modulo, "get_usuario", lambda username: None)

    with pytest.raises(HTTPException) as exc:
        _relatorio(central, token)

    assert exc.value.status_code == 401
    assert central.banco.chamadas_de("get_ordem_por_id") == []


def test_relatorio_sem_token_recusa(carregar_central):
    central = carregar_central()

    with pytest.raises(HTTPException) as exc:
        central.modulo._get_tecnico(None)

    assert exc.value.status_code == 401


def test_relatorio_de_tecnico_ativo_busca_os(carregar_central, monkeypatch):
    """O outro lado: fechar a porta não pode fechá-la para quem tem a chave."""
    central = carregar_central()
    _usuario_no_banco(central, monkeypatch)
    monkeypatch.setattr(central.modulo, "get_ordem_por_id", lambda os_id: None)
    token = central.modulo.criar_token("tec1", "Técnico 1", "manutencao")

    with pytest.raises(HTTPException) as exc:
        _relatorio(central, token)

    # 404 e não 401: a autenticação passou e a OS é que não existe.
    assert exc.value.status_code == 404


def test_relatorio_declara_a_dependencia_de_tecnico(carregar_central):
    """Os testes acima resolvem a dependência à mão; este prova que a ROTA a
    declara — sem isto, todos eles passariam com o endpoint aberto."""
    import inspect

    central = carregar_central()
    parametro = inspect.signature(central.modulo.relatorio_os).parameters["user"]

    assert parametro.default.dependency is central.modulo._get_tecnico


def test_nenhuma_rota_confere_token_por_conta_propria():
    """Varredura por AST, e não só do relatório: a regra é do central inteiro.

    `decodificar_token` é a metade criptográfica da autenticação — ela prova
    que o token foi assinado com a chave certa, e é só isso que ela prova. A
    outra metade (o usuário existe, está ativo, e a role é a do BANCO) mora em
    `_get_tecnico`. Qualquer rota que chame a primeira sem passar pela segunda
    volta a ter o furo, e o teste encontra a próxima sozinho.
    """
    arvore = ast.parse((CENTRAL_DIR / "main.py").read_text(encoding="utf-8"))
    infratoras = []
    for no in ast.walk(arvore):
        if not isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if no.name == "_get_tecnico":          # a dona legítima da chamada
            continue
        if any(isinstance(i, ast.Call) and isinstance(i.func, ast.Name)
               and i.func.id == "decodificar_token" for i in ast.walk(no)):
            infratoras.append(f"{no.name} (linha {no.lineno})")

    assert not infratoras, (
        "rota conferindo token sem _get_tecnico: " + ", ".join(infratoras)
    )


# ── Freio de força bruta no login ─────────────────────────────────────────────

def _usuario_com_senha(central, monkeypatch, senha="segredo123"):
    import auth
    usuario = {"username": "admin", "nome_completo": "Admin",
               "senha_hash": auth.hash_senha(senha), "role": "admin", "ativo": 1}
    monkeypatch.setattr(central.modulo, "get_usuario",
                        lambda username: usuario if username == "admin" else None)
    return senha


def _login(central, username, senha, ip="10.0.0.1"):
    return central.modulo.login(
        central.modulo.LoginReq(username=username, senha=senha), requisicao(ip)
    )


@pytest.fixture(autouse=True)
def _freio_limpo():
    """O contador é estado de módulo, compartilhado com o console."""
    if str(CENTRAL_DIR) not in sys.path:
        sys.path.insert(0, str(CENTRAL_DIR))
    import console
    console._tentativas.clear()
    yield
    console._tentativas.clear()


def test_rajada_de_senhas_erradas_e_bloqueada(carregar_central, monkeypatch):
    """Sem freio, a rota que emite o JWT de admin aceitava palpite sem limite."""
    central = carregar_central()
    _usuario_com_senha(central, monkeypatch)
    import console

    for _ in range(console.MAX_TENTATIVAS):
        with pytest.raises(HTTPException) as exc:
            _login(central, "admin", "chute")
        assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        _login(central, "admin", "chute")
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers


def test_bloqueio_vale_inclusive_para_a_senha_certa(carregar_central, monkeypatch):
    """Senão bastaria intercalar palpites com um acerto para nunca ser freado —
    e quem tem a senha certa não precisa de seis tentativas por minuto."""
    central = carregar_central()
    senha = _usuario_com_senha(central, monkeypatch)
    import console

    for _ in range(console.MAX_TENTATIVAS):
        with pytest.raises(HTTPException):
            _login(central, "admin", "chute")

    with pytest.raises(HTTPException) as exc:
        _login(central, "admin", senha)
    assert exc.value.status_code == 429


def test_login_correto_zera_o_contador(carregar_central, monkeypatch):
    """Quem erra três vezes e acerta na quarta não pode ficar a uma tentativa
    do bloqueio pelo resto da janela."""
    central = carregar_central()
    senha = _usuario_com_senha(central, monkeypatch)
    import console

    for _ in range(console.MAX_TENTATIVAS - 1):
        with pytest.raises(HTTPException):
            _login(central, "admin", "chute")

    assert _login(central, "admin", senha)["role"] == "admin"

    # E a janela recomeça do zero.
    for _ in range(console.MAX_TENTATIVAS - 1):
        with pytest.raises(HTTPException) as exc:
            _login(central, "admin", "chute")
        assert exc.value.status_code == 401


def test_freio_e_por_IP_E_username(carregar_central, monkeypatch):
    """Só o IP puniria o turno inteiro por causa de um técnico que erra a senha
    — a bancada fala com o central por um NAT só. Só o username deixaria
    qualquer um trancar a conta alheia de fora."""
    central = carregar_central()
    _usuario_com_senha(central, monkeypatch)
    import console

    for _ in range(console.MAX_TENTATIVAS):
        with pytest.raises(HTTPException):
            _login(central, "admin", "chute", ip="10.0.0.1")

    # Mesmo IP, outro usuário: balde próprio.
    with pytest.raises(HTTPException) as exc:
        _login(central, "tec1", "chute", ip="10.0.0.1")
    assert exc.value.status_code == 401

    # Mesmo usuário, outro IP: balde próprio também.
    with pytest.raises(HTTPException) as exc:
        _login(central, "admin", "chute", ip="10.0.0.2")
    assert exc.value.status_code == 401


def test_freio_do_login_nao_gasta_as_tentativas_do_console(carregar_central,
                                                           monkeypatch):
    """Os dois dividem o dicionário; o prefixo da chave é o que os separa.

    Sem ele, errar a senha do console cinco vezes trancaria o login da API — e
    o operador descobriria isso no pior momento possível.
    """
    central = carregar_central()
    _usuario_com_senha(central, monkeypatch)
    import console

    for _ in range(console.MAX_TENTATIVAS * 2):
        console.registrar_falha("10.0.0.1")      # a chave que o console usa

    with pytest.raises(HTTPException) as exc:
        _login(central, "admin", "chute", ip="10.0.0.1")
    assert exc.value.status_code == 401          # 401, não 429


def test_usuario_inexistente_tambem_conta(carregar_central, monkeypatch):
    """Varredura de usuário é o passo ANTES de varrer senha — e o `get_usuario`
    devolvendo None era a resposta mais barata que o central dava."""
    central = carregar_central()
    _usuario_com_senha(central, monkeypatch)
    import console

    for _ in range(console.MAX_TENTATIVAS):
        with pytest.raises(HTTPException):
            _login(central, "ninguem", "chute")

    with pytest.raises(HTTPException) as exc:
        _login(central, "ninguem", "chute")
    assert exc.value.status_code == 429


# ── CORS ──────────────────────────────────────────────────────────────────────

def test_cors_do_central_nao_e_aberto(carregar_central):
    central = carregar_central()

    origens = central.modulo.settings.CORS_ORIGINS

    assert origens, "CORS_ORIGINS vazio deixaria o central sem nenhuma origem"
    assert "*" not in origens


def test_cors_configuravel_por_env(config, monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://apsen.exemplo, https://manut.exemplo")

    assert config._origens_cors() == ["https://apsen.exemplo", "https://manut.exemplo"]


# ── Segredos fora do repositório ──────────────────────────────────────────────

def test_compose_nao_versiona_segredos():
    """Senhas seed e credenciais de banco vêm do .env, não do arquivo commitado."""
    compose = (CENTRAL_DIR.parent / "docker-compose.yml").read_text(encoding="utf-8")

    for segredo in ("Apsen@Admin#2024!", "Apsen@Manut#2024!",
                    "apsen_pass_2024", "apsen_root_2024"):
        assert segredo not in compose, f"segredo ainda versionado no compose: {segredo}"


# Havia aqui um `test_env_de_exemplo_nao_traz_valores_reais`, que exigia as 5
# chaves de segredo VAZIAS num `.env.example` versionado. O arquivo foi removido
# do repositório: sem template commitado, não existe o risco de alguém preencher
# o exemplo com valores reais e commitar — o teste perdeu o objeto. O modelo do
# `.env` passou a viver no README ("Build e deploy"), que não é lido por
# nenhum teste.


def test_env_real_esta_ignorado_pelo_git():
    gitignore = (CENTRAL_DIR.parent / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore.split()
