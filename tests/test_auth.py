"""Verificação de senha: entrada inválida é 401, não 500.

`bcrypt.checkpw` levanta `ValueError` em dois casos que chegam pela rede — hash
malformado no banco e, no bcrypt >= 4, senha acima de 72 bytes, que qualquer um
digita no formulário de login. Sem tratamento a exceção sobe pelo `login()` e o
cliente recebe 500, expondo falha interna onde a resposta certa é credencial
inválida.
"""
import sys
from pathlib import Path

import pytest

CENTRAL_DIR = Path(__file__).resolve().parent.parent / "central-computer"


@pytest.fixture(scope="module")
def auth():
    """Importa `central-computer/auth.py` — sem banco, sem servidor."""
    if str(CENTRAL_DIR) not in sys.path:
        sys.path.insert(0, str(CENTRAL_DIR))
    import auth as modulo
    return modulo


def test_senha_correta_confere(auth):
    """Guarda do caminho feliz: o try/except não pode engolir o sucesso."""
    assert auth.verificar_senha("segredo123", auth.hash_senha("segredo123")) is True


def test_senha_errada_e_falsa(auth):
    assert auth.verificar_senha("errada", auth.hash_senha("segredo123")) is False


@pytest.mark.parametrize("hash_invalido", [
    "",
    "nao-e-um-hash",
    "$999$xx$" + "a" * 20,           # prefixo de algoritmo inexistente
    "$2b$12$" + "!" * 53,            # tamanho certo, alfabeto errado
    "$2b$99$" + "a" * 53,            # custo fora da faixa
    None,                            # coluna NULL
])
def test_hash_malformado_devolve_false_sem_levantar(auth, hash_invalido):
    assert auth.verificar_senha("qualquer", hash_invalido) is False


def test_hash_truncado_nao_derruba_o_processo(auth):
    """O caso que NENHUM try/except pegaria.

    `$2b$12$truncado` faz o backend Rust do bcrypt 4 entrar em pânico
    (`pyo3_runtime.PanicException`, que herda de BaseException). Só a checagem
    de formato antes da chamada evita o 500 — se alguém trocar a validação por
    um try/except "equivalente", este teste quebra.
    """
    assert auth.verificar_senha("qualquer", "$2b$12$truncado") is False


def test_senha_muito_longa_devolve_false_sem_levantar(auth):
    """Mais de 72 bytes vem do formulário de login e não pode virar 500."""
    hash_valido = auth.hash_senha("segredo123")

    assert auth.verificar_senha("x" * 200, hash_valido) is False


def test_senha_longa_multibyte_tambem_e_tratada(auth):
    """72 BYTES, não 72 caracteres: acentuação estoura antes do que parece."""
    hash_valido = auth.hash_senha("segredo123")

    assert auth.verificar_senha("ç" * 100, hash_valido) is False


def test_login_com_hash_corrompido_responde_401(carregar_central, monkeypatch):
    """O efeito visível para o cliente: 401, não 500."""
    from fastapi import HTTPException

    central = carregar_central()
    # `get_usuario` vem duplado pelo BancoFake e devolve None; aqui ele precisa
    # devolver um usuário para o fluxo chegar até a verificação de senha.
    monkeypatch.setattr(central.modulo, "get_usuario", lambda username: {
        "username": "admin", "nome_completo": "Admin",
        "senha_hash": "$2b$12$hash-corrompido", "role": "admin",
    })

    with pytest.raises(HTTPException) as exc:
        central.modulo.login(
            central.modulo.LoginReq(username="admin", senha="x" * 200)
        )

    assert exc.value.status_code == 401
