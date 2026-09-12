"""O perfil "supervisor" existe no central, e é quem libera a trava.

`POST /api/v1/admin/liberar-trava` dependia de `_get_admin`
(`if role != "admin": 403`) e o docstring logo acima afirmava "Exige role
admin ou supervisor". A documentação mentia: não havia role "supervisor" em
lugar nenhum do central. A decisão de operação foi tomada — o supervisor libera
a trava pelo painel de bancada, autenticado por PIN, tanto no display de 7"
quanto na web — e esta é a metade do central:

  1. o portão é `_get_supervisor_ou_admin`, à PARTE de `_get_admin`: a gestão
     de usuários continua só admin, e a role continua vindo do BANCO;
  2. "supervisor" é role válida em `criar_usuario` / `atualizar_usuario`, e
     role desconhecida é 400, não gravada;
  3. o endpoint aceita `em_nome_de`: quando a chamada vem por uma conta de
     serviço, o `liberado_por` gravado vira `"<conta> (em nome de <X>)"`. Sem
     isso toda liberação vinda da bancada apareceria no log com o nome da
     conta de serviço, e o rastro de QUEM liberou — o ponto inteiro de existir
     uma trava — se perderia.
"""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from conftest import CENTRAL_DIR


def _user(role: str, sub: str = "alguem") -> dict:
    return {"sub": sub, "nome": sub, "role": role}


def _capturar_liberacao(central, monkeypatch, ha_trava: bool = True) -> list:
    """Intercepta `orch.liberar_trava` — é o que recebe o `liberado_por`."""
    recebidos = []

    def _liberar(liberado_por):
        recebidos.append(liberado_por)
        return ha_trava

    monkeypatch.setattr(central.modulo.orch, "liberar_trava", _liberar)
    monkeypatch.setattr(central.modulo.orch, "get_trava_estado",
                        lambda: {"ativa": ha_trava, "os_id": "OS-1", "slot_id": 3,
                                 "motivo": "Triple Check FALHOU"})
    return recebidos


def _liberar(central, user: dict, corpo=None) -> dict:
    return asyncio.run(central.modulo.liberar_trava(req=corpo, user=user))


# ── 1. O portão ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["admin", "supervisor"])
def test_admin_e_supervisor_passam_pelo_portao_da_trava(carregar_central, role):
    central = carregar_central()
    assert central.modulo._get_supervisor_ou_admin(_user(role))["role"] == role


@pytest.mark.parametrize("role", ["manutencao", "operador", "", None])
def test_outras_roles_tomam_403_no_portao_da_trava(carregar_central, role):
    central = carregar_central()
    with pytest.raises(HTTPException) as exc:
        central.modulo._get_supervisor_ou_admin(_user(role))
    assert exc.value.status_code == 403


def test_get_admin_continua_recusando_supervisor(carregar_central):
    """O portão é à PARTE: afrouxar `_get_admin` daria ao supervisor a gestão
    de usuários, que é justamente o que ele não deve ter."""
    central = carregar_central()
    with pytest.raises(HTTPException) as exc:
        central.modulo._get_admin(_user("supervisor"))
    assert exc.value.status_code == 403


def test_a_rota_da_trava_declara_o_portao_novo(carregar_central):
    """Os testes acima chamam o portão à mão; este prova que a ROTA o declara."""
    central = carregar_central()
    parametro = inspect.signature(central.modulo.liberar_trava).parameters["user"]
    assert parametro.default.dependency is central.modulo._get_supervisor_ou_admin


@pytest.mark.parametrize("rota", ["listar_usuarios", "criar_novo_usuario",
                                  "editar_usuario", "desativar_usuario",
                                  "ativar_usuario"])
def test_as_rotas_de_gestao_de_usuario_continuam_so_admin(carregar_central, rota):
    central = carregar_central()
    parametro = inspect.signature(getattr(central.modulo, rota)).parameters["user"]
    assert parametro.default.dependency is central.modulo._get_admin


# ── 2. Liberação: supervisor, admin, e quem não pode ─────────────────────────

@pytest.mark.parametrize("role", ["supervisor", "admin"])
def test_supervisor_e_admin_liberam(carregar_central, monkeypatch, role):
    central = carregar_central()
    recebidos = _capturar_liberacao(central, monkeypatch)

    resposta = _liberar(central, _user(role, sub="maria"))

    assert resposta == {"ok": True, "liberado_por": "maria"}
    assert recebidos == ["maria"]
    assert central.modulo._estado["trava"]["ativa"] is False


def test_manutencao_toma_403_antes_de_tocar_na_trava(carregar_central, monkeypatch):
    """O 403 é do portão, não da rota: `_get_supervisor_ou_admin` recusa antes
    de o corpo da rota rodar — a trava nem fica sabendo da tentativa."""
    central = carregar_central()
    recebidos = _capturar_liberacao(central, monkeypatch)

    with pytest.raises(HTTPException) as exc:
        central.modulo._get_supervisor_ou_admin(_user("manutencao"))

    assert exc.value.status_code == 403
    assert recebidos == []


def test_sem_trava_ativa_continua_409(carregar_central, monkeypatch):
    central = carregar_central()
    _capturar_liberacao(central, monkeypatch, ha_trava=False)

    with pytest.raises(HTTPException) as exc:
        _liberar(central, _user("supervisor"))
    assert exc.value.status_code == 409


# ── 3. `em_nome_de` ───────────────────────────────────────────────────────────

def test_em_nome_de_entra_no_liberado_por(carregar_central, monkeypatch):
    central = carregar_central()
    recebidos = _capturar_liberacao(central, monkeypatch)
    corpo = central.modulo.LiberarTravaReq(em_nome_de="Maria Silva")

    resposta = _liberar(central, _user("supervisor", sub="painel-bancada"), corpo)

    assert resposta["liberado_por"] == "painel-bancada (em nome de Maria Silva)"
    assert recebidos == ["painel-bancada (em nome de Maria Silva)"]


def test_em_nome_de_chega_a_trilha_de_manutencao(carregar_central, monkeypatch):
    """A liberação vira linha em `log_manutencao`, com quem liberou no `tecnico`.

    Era só uma linha de log de processo — e é a liberação, não a ativação,
    que diz quem assumiu a responsabilidade pela OS que seguiu.
    """
    central = carregar_central()
    _capturar_liberacao(central, monkeypatch)
    corpo = central.modulo.LiberarTravaReq(em_nome_de="Maria Silva")

    _liberar(central, _user("supervisor", sub="painel-bancada"), corpo)

    (registro,) = central.banco.chamadas_de("salvar_manutencao")
    tipo, componente, descricao, tecnico = registro["args"]
    assert (tipo, componente) == ("trava_liberada", "triple_check")
    assert tecnico == "painel-bancada (em nome de Maria Silva)"
    assert "OS-1" in descricao and "D3" in descricao and "Maria Silva" in descricao


def test_em_nome_de_ausente_mantem_o_comportamento_de_hoje(carregar_central,
                                                            monkeypatch):
    """Sem corpo, com corpo vazio ou com o campo nulo: exatamente o username."""
    central = carregar_central()
    recebidos = _capturar_liberacao(central, monkeypatch)

    for corpo in (None, central.modulo.LiberarTravaReq(),
                  central.modulo.LiberarTravaReq(em_nome_de=None),
                  central.modulo.LiberarTravaReq(em_nome_de="   ")):
        resposta = _liberar(central, _user("admin", sub="admin"), corpo)
        assert resposta["liberado_por"] == "admin"

    assert recebidos == ["admin"] * 4


def test_em_nome_de_e_limitado_a_60_caracteres(carregar_central):
    central = carregar_central()
    longo = "X" * 200

    liberado_por = central.modulo._identificar_liberacao("svc", longo)

    assert liberado_por == "svc (em nome de " + "X" * 60 + ")"
    assert central.modulo.EM_NOME_DE_MAX == 60


def test_em_nome_de_e_sanitizado_antes_de_ir_para_o_log(carregar_central):
    """Quebra de linha viraria uma segunda linha de log que ninguém escreveu."""
    central = carregar_central()
    sujo = "Maria" + chr(10) + "Silva" + chr(9) + chr(0) + " (em nome de admin)" + chr(13) + chr(10) + "  "

    liberado_por = central.modulo._identificar_liberacao("svc", sujo)

    for proibido in (chr(10), chr(13), chr(9), chr(0)):
        assert proibido not in liberado_por
    assert liberado_por == "svc (em nome de Maria Silva (em nome de admin))"


def test_em_nome_de_que_nao_e_texto_e_ignorado(carregar_central):
    central = carregar_central()
    assert central.modulo._identificar_liberacao("svc", 42) == "svc"
    assert central.modulo._identificar_liberacao("svc", ["x"]) == "svc"


# ── 4. Role é vocabulário fechado ─────────────────────────────────────────────

def test_supervisor_e_role_valida_no_banco(carregar_central):
    carregar_central()
    import database
    assert "supervisor" in database.ROLES_VALIDAS
    assert set(database.ROLES_VALIDAS) == {"admin", "supervisor", "manutencao"}


def _gravador(monkeypatch, central, nome: str) -> list:
    """Troca a função de banco por um duplo que responde `ok` e grava os args."""
    chamadas = []

    def _duplo(*args, **kwargs):
        chamadas.append((args, kwargs))
        return {"ok": True, "id": 1, "afetados": 1}

    monkeypatch.setattr(central.modulo, nome, _duplo)
    return chamadas


def test_criar_usuario_com_role_supervisor_grava(carregar_central, monkeypatch):
    central = carregar_central()
    chamadas = _gravador(monkeypatch, central, "criar_usuario")
    req = central.modulo.UsuarioReq(username="sup1", senha="segredo123",
                                    nome_completo="Supervisora", role="supervisor")

    assert central.modulo.criar_novo_usuario(req, _user("admin"))["ok"] is True

    ((args, _),) = chamadas
    assert args[3] == "supervisor"


@pytest.mark.parametrize("role", ["chefe", "Admin", "supervisora", ""])
def test_criar_usuario_com_role_desconhecida_e_400_e_nada_e_gravado(carregar_central,
                                                                     role):
    central = carregar_central()
    req = central.modulo.UsuarioReq(username="x", senha="segredo123",
                                    nome_completo="X", role=role)

    with pytest.raises(HTTPException) as exc:
        central.modulo.criar_novo_usuario(req, _user("admin"))

    assert exc.value.status_code == 400
    assert central.banco.chamadas_de("criar_usuario") == []


def test_editar_usuario_com_role_desconhecida_e_400_e_nada_e_gravado(carregar_central):
    central = carregar_central()
    req = central.modulo.UsuarioUpdateReq(role="chefe")

    with pytest.raises(HTTPException) as exc:
        central.modulo.editar_usuario("tec1", req, _user("admin"))

    assert exc.value.status_code == 400
    assert central.banco.chamadas_de("atualizar_usuario") == []


def test_editar_usuario_sem_mexer_na_role_nao_valida_role(carregar_central,
                                                          monkeypatch):
    """Trocar só a senha não pode ser recusado por uma role que não foi enviada."""
    central = carregar_central()
    chamadas = _gravador(monkeypatch, central, "atualizar_usuario")
    req = central.modulo.UsuarioUpdateReq(nova_senha="outra-senha-123")

    assert central.modulo.editar_usuario("tec1", req, _user("admin"))["ok"] is True

    ((args, _),) = chamadas
    assert args == ("tec1", None, None, "outra-senha-123")


def test_o_banco_tambem_recusa_role_desconhecida_sem_abrir_conexao(carregar_central,
                                                                   monkeypatch):
    """Última linha: quem chamar `database.criar_usuario` direto também esbarra."""
    carregar_central()
    import database

    def _nunca():
        raise AssertionError("abriu conexão para uma role inválida")

    monkeypatch.setattr(database, "_conn", _nunca)
    assert database.criar_usuario("x", "s", "X", "chefe")["ok"] is False
    assert database.atualizar_usuario("x", role="chefe")["ok"] is False


def test_o_seed_nao_ganhou_supervisor(carregar_central):
    """Conta de supervisor é criada por quem opera, com senha própria — nunca
    nasce do seed com senha no `.env`, que fica em claro."""
    fonte = (CENTRAL_DIR / "database.py").read_text(encoding="utf-8")
    bloco = fonte.split("def _seed_usuarios", 1)[1].split("\ndef ", 1)[0]
    assert '"supervisor"' not in bloco
