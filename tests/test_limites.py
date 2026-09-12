"""O `?limite=` das rotas de histórico tem teto e tem piso.

O parâmetro ia CRU para o `LIMIT %s` das queries. Dois estragos, e só o primeiro
era visível:

  * `?limite=-1` vira `LIMIT -1`, que é erro de SINTAXE no MySQL (1064) — uma
    rota de leitura respondendo 500 porque alguém digitou um número negativo;
  * `?limite=99999999` é aceito e varre a tabela inteira. Em `dispensas` e
    `cnc_eventos`, que são as de maior cardinalidade, uma requisição sozinha
    ocupa uma conexão do pool e a memória do processo por bastante tempo.

`/api/v1/visao/historico` era a única com clamp, e um só (`> 500`). Hoje as nove
rotas com `limite` passam pelo mesmo `_limite`, e o último teste deste arquivo
varre `main.py` por AST para que a décima não nasça sem ele.
"""
import ast
import asyncio
from pathlib import Path

import pytest

CENTRAL_DIR = Path(__file__).resolve().parent.parent / "central-computer"
MAIN_PY     = CENTRAL_DIR / "main.py"

TECNICO = {"sub": "tec1", "nome": "Técnico 1", "role": "manutencao"}


# ── O helper ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("entrada,esperado", [
    (-1,        1),      # o que virava 1064 no MySQL
    (0,         1),      # LIMIT 0 é válido e devolve nada — ninguém pede isso
    (1,         1),
    (50,        50),
    (500,       500),
    (501,       500),
    (99_999_999, 500),   # a varredura de tabela
])
def test_limite_encaixa_na_faixa(carregar_central, entrada, esperado):
    central = carregar_central()

    assert central.modulo._limite(entrada) == esperado


@pytest.mark.parametrize("lixo", [None, "abc", "", [3]])
def test_limite_nao_numerico_nao_levanta(carregar_central, lixo):
    """O FastAPI já converte o query param, mas o helper é chamado de dentro do
    processo também — e um `TypeError` aqui seria 500 pelo outro caminho."""
    central = carregar_central()

    assert central.modulo._limite(lixo) == 1


def test_teto_e_um_numero_so(carregar_central):
    """`visao_historico` tinha o `> 500` escrito à mão. Dois tetos divergem no
    primeiro ajuste, e o que fica para trás é o que ninguém está olhando."""
    central = carregar_central()

    assert central.modulo._LIMITE_MAX == 500
    assert "if limite > 500" not in MAIN_PY.read_text(encoding="utf-8")


# ── As quatro rotas do relato original, mais as que herdaram o helper ────────

def _limite_recebido(central, fn: str, posicao: int = -1):
    """Valor que chegou à função de banco, seja por posição ou por `limite=`."""
    chamada = central.banco.chamadas_de(fn)[-1]
    if "limite" in chamada["kwargs"]:
        return chamada["kwargs"]["limite"]
    return chamada["args"][posicao]


@pytest.mark.parametrize("bruto,esperado", [(-1, 1), (0, 1), (99_999_999, 500)])
def test_os_historico_clampa(carregar_central, bruto, esperado):
    central = carregar_central()

    central.modulo.os_historico(limite=bruto)

    assert _limite_recebido(central, "get_historico_ordens") == esperado


@pytest.mark.parametrize("bruto,esperado", [(-1, 1), (0, 1), (99_999_999, 500)])
def test_dispensas_recentes_clampa(carregar_central, bruto, esperado):
    central = carregar_central()

    central.modulo.dispensas(limite=bruto)

    assert _limite_recebido(central, "get_dispensas_recentes") == esperado


@pytest.mark.parametrize("bruto,esperado", [(-1, 1), (0, 1), (99_999_999, 500)])
def test_dispensas_por_os_clampa(carregar_central, bruto, esperado):
    """O ramo com `os_id` é outro `cur.execute` — e era o mesmo buraco."""
    central = carregar_central()

    central.modulo.dispensas(os_id="OS-1", limite=bruto)

    assert _limite_recebido(central, "get_dispensas") == esperado


@pytest.mark.parametrize("bruto,esperado", [(-1, 1), (0, 1), (99_999_999, 500)])
def test_cnc_historico_clampa(carregar_central, bruto, esperado):
    central = carregar_central()

    central.modulo.cnc_historico(limite=bruto)

    assert _limite_recebido(central, "get_cnc_recentes") == esperado


@pytest.mark.parametrize("bruto,esperado", [(-1, 1), (0, 1), (99_999_999, 500)])
def test_alarmes_clampa(carregar_central, bruto, esperado):
    central = carregar_central()

    central.modulo.alarmes(limite=bruto)

    assert _limite_recebido(central, "get_alarmes") == esperado


@pytest.mark.parametrize("bruto,esperado", [(-1, 1), (0, 1), (99_999_999, 500)])
def test_visao_historico_continua_clampando(carregar_central, monkeypatch,
                                            bruto, esperado):
    """A única que já tinha clamp — e que perdeu o número escrito à mão.

    O `if limite > 500` de antes deixava passar negativo: a rota "com clamp"
    também respondia 500 a `?limite=-1`.
    """
    central = carregar_central()
    recebidos = []
    # O duplo do `BancoFake` devolve None, e esta rota faz `len()` no retorno.
    monkeypatch.setattr(central.modulo, "get_historico_visao",
                        lambda os_id, limite: recebidos.append(limite) or [])

    asyncio.run(central.modulo.visao_historico(limite=bruto))

    assert recebidos == [esperado]


@pytest.mark.parametrize("bruto,esperado", [(-1, 1), (0, 1), (99_999_999, 500)])
def test_manut_log_clampa(carregar_central, bruto, esperado):
    """Exigir JWT não protege o MySQL do `LIMIT -1`: o 1064 é o mesmo."""
    central = carregar_central()

    central.modulo.manut_log(limite=bruto, user=TECNICO)

    assert _limite_recebido(central, "get_log_manutencao") == esperado


@pytest.mark.parametrize("bruto,esperado", [(-1, 1), (0, 1), (99_999_999, 500)])
def test_manut_alarmes_clampa(carregar_central, bruto, esperado):
    central = carregar_central()

    central.modulo.manut_alarmes(limite=bruto, user=TECNICO)

    assert _limite_recebido(central, "get_alarmes") == esperado


@pytest.mark.parametrize("bruto,esperado", [(-1, 1), (0, 1), (99_999_999, 500)])
def test_manut_sensor_hist_clampa(carregar_central, bruto, esperado):
    central = carregar_central()

    central.modulo.manut_sensor_hist("dispenser_1", limite=bruto, user=TECNICO)

    assert _limite_recebido(central, "get_historico_sensor") == esperado


def test_log_eventos_nao_devolve_recorte_invertido(carregar_central):
    """Não vai a banco, mas `lista[:-1]` devolve "todos menos o último" — um
    recorte que ninguém pediu e que some com um evento sem dizer nada."""
    central = carregar_central()
    for i in range(5):
        central.modulo._log("teste", f"evento {i}")

    assert len(central.modulo.log_eventos(limite=-1)) == 1
    assert len(central.modulo.log_eventos(limite=3)) == 3


def test_limite_valido_passa_intacto(carregar_central):
    """O clamp não pode virar um teto que ninguém pediu no caso comum."""
    central = carregar_central()

    central.modulo.os_historico(limite=200)

    assert _limite_recebido(central, "get_historico_ordens") == 200


# ── A décima rota ─────────────────────────────────────────────────────────────

def test_toda_rota_com_limite_passa_pelo_helper():
    """Varredura por AST: sem lista manual para alguém esquecer de atualizar.

    A regra é simples de checar e difícil de burlar por engano — função que
    declara um parâmetro `limite` precisa chamar `_limite` em algum lugar do
    corpo. Uma rota nova que mande o parâmetro cru para o banco cai aqui, e não
    em produção com um 500 numa rota de leitura.
    """
    arvore = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    faltando = []
    for no in ast.walk(arvore):
        if not isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nomes = {a.arg for a in no.args.args} | {a.arg for a in no.args.kwonlyargs}
        if "limite" not in nomes:
            continue
        usa_helper = any(
            isinstance(i, ast.Call) and isinstance(i.func, ast.Name)
            and i.func.id == "_limite"
            for i in ast.walk(no)
        )
        if not usa_helper:
            faltando.append(f"{no.name} (linha {no.lineno})")

    assert not faltando, (
        "rota com `limite` que não passa por _limite(): " + ", ".join(faltando)
    )
