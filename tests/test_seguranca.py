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
"""
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

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
    """Uma query por request derrubaria o throughput da IHM."""
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
    """O atraso do cache não pode valer para o botão "Desativar" da IHM."""
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


# ── CORS ──────────────────────────────────────────────────────────────────────

def test_cors_do_central_nao_e_aberto(carregar_central):
    central = carregar_central()

    origens = central.modulo.settings.CORS_ORIGINS

    assert origens, "CORS_ORIGINS vazio deixaria o central sem nenhuma origem"
    assert "*" not in origens


def test_cors_configuravel_por_env(config, monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://apsen.exemplo, https://ihm.exemplo")

    assert config._origens_cors() == ["https://apsen.exemplo", "https://ihm.exemplo"]


# ── Segredos fora do repositório ──────────────────────────────────────────────

def test_compose_nao_versiona_segredos():
    """Senhas seed e credenciais de banco vêm do .env, não do arquivo commitado."""
    compose = (CENTRAL_DIR.parent / "docker-compose.yml").read_text(encoding="utf-8")

    for segredo in ("Apsen@Admin#2024!", "Apsen@Manut#2024!",
                    "apsen_pass_2024", "apsen_root_2024"):
        assert segredo not in compose, f"segredo ainda versionado no compose: {segredo}"


def test_env_de_exemplo_nao_traz_valores_reais():
    exemplo = (CENTRAL_DIR.parent / ".env.example").read_text(encoding="utf-8")

    for chave in ("SECRET_KEY", "MYSQL_ROOT_PASS", "MYSQL_PASS",
                  "SEED_ADMIN_SENHA", "SEED_MANUT_SENHA"):
        assert f"{chave}=\n" in exemplo, f"{chave} deveria estar vazia no .env.example"


def test_env_real_esta_ignorado_pelo_git():
    gitignore = (CENTRAL_DIR.parent / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore.split()
