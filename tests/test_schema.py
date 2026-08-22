"""Guarda do schema do banco — sem subir MySQL.

O schema já esteve declarado em dois lugares que divergiram: `mysql/init.sql`
(só roda na primeira criação do volume) e `_create_tables()` (roda em todo
startup). O resultado foi um central que não subia: `_seed_medicamentos`
consultava `medicamentos.peso_unitario_g`, coluna que só existia no init.sql,
e o 1054 resultante era classificado como "MySQL não disponível" — 30 retries
e queda.

Estes testes travam as três consequências disso:

  * `mysql/init.sql` não pode voltar a declarar schema (fonte de verdade única);
  * tudo que `database.py` consulta precisa existir no DDL — este é o teste que
    pega drift futuro sozinho, sem ninguém lembrar de atualizar uma lista;
  * o reparo de bancos antigos (ALTER condicional) e o seed dos slots precisam
    rodar de verdade, então rodam contra um cursor duplo.
"""
import ast
import re
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import NUM_SLOTS

RAIZ_REPO   = Path(__file__).resolve().parent.parent
CENTRAL_DIR = RAIZ_REPO / "central-computer"
DATABASE_PY = CENTRAL_DIR / "database.py"
INIT_SQL    = RAIZ_REPO / "mysql" / "init.sql"


# ── Leitura estática de database.py ────────────────────────────────────────────

def _constante(nome: str):
    """Valor literal de uma constante de módulo, sem importar nada."""
    arvore = ast.parse(DATABASE_PY.read_text(encoding="utf-8"))
    for no in arvore.body:
        alvos = no.targets if isinstance(no, ast.Assign) else (
            [no.target] if isinstance(no, ast.AnnAssign) else []
        )
        if any(isinstance(a, ast.Name) and a.id == nome for a in alvos):
            return ast.literal_eval(no.value)
    raise AssertionError(f"{nome} não encontrado em {DATABASE_PY.name}")


_RE_CREATE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?\s*\((.*)\)\s*ENGINE",
    re.I | re.S,
)
# Linhas de definição que não são coluna.
_NAO_COLUNA = {"index", "key", "primary", "unique", "constraint", "foreign",
               "fulltext", "spatial", "check"}


def _parse_ddl(ddl: str) -> tuple[str, set[str]]:
    achado = _RE_CREATE.search(ddl)
    assert achado, f"DDL não reconhecido: {ddl[:70]!r}"
    colunas = set()
    for linha in achado.group(2).splitlines():
        linha = linha.strip().rstrip(",")
        if not linha:
            continue
        primeiro = linha.split()[0].strip("`").lower()
        if primeiro not in _NAO_COLUNA:
            colunas.add(primeiro)
    return achado.group(1).lower(), colunas


SCHEMA: dict[str, set[str]] = dict(_parse_ddl(d) for d in _constante("_DDL_TABELAS"))
COLUNAS_EVOLUTIVAS: dict[str, dict[str, str]] = _constante("_COLUNAS_EVOLUTIVAS")


# ── Extração das queries de database.py ────────────────────────────────────────

_RE_VERBO = re.compile(r"^(SELECT|INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


def _sqls() -> list[str]:
    """Toda string de `database.py` que começa com um verbo DML, normalizada."""
    arvore = ast.parse(DATABASE_PY.read_text(encoding="utf-8"))
    achados = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Constant) and isinstance(no.value, str):
            bruto = no.value
        elif isinstance(no, ast.JoinedStr):
            # f-string: só as partes literais. O miolo interpolado é montado de
            # fragmentos (`"role=%s"`) que não formam SQL sozinhos.
            bruto = "".join(
                p.value for p in no.values
                if isinstance(p, ast.Constant) and isinstance(p.value, str)
            )
        else:
            continue
        texto = " ".join(bruto.split())
        if _RE_VERBO.match(texto):
            achados.append(texto)
    return achados


_PALAVRAS_SQL = {
    "select", "from", "where", "and", "or", "not", "null", "is", "in", "like",
    "as", "on", "inner", "left", "right", "outer", "join", "group", "by",
    "order", "limit", "offset", "asc", "desc", "insert", "into", "ignore",
    "values", "update", "set", "delete", "distinct", "between", "exists",
    "count", "max", "min", "sum", "avg", "now", "if", "case", "when", "then",
    "else", "end", "true", "false",
}

_RE_TABELA    = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+`?(\w+)`?", re.I)
_RE_ALIAS_AS  = re.compile(r"\bAS\s+`?(\w+)`?", re.I)
_RE_ALIAS_TAB = re.compile(r"\b(?:FROM|JOIN)\s+\w+\s+(\w+)\b", re.I)


def _normalizar(sql: str) -> str:
    sql = sql.replace("%s", "?")        # placeholder não é identificador
    return re.sub(r"'[^']*'", "?", sql)  # literais idem


def _identificadores(trecho: str) -> set[str]:
    trecho = re.sub(r"\b[A-Za-z_]\w*\s*\.", "", trecho)  # descarta prefixo de alias
    return {m.lower() for m in re.findall(r"[A-Za-z_]\w*", trecho)} - _PALAVRAS_SQL


def _colunas_referenciadas(sql: str) -> set[str]:
    """Identificadores que só podem ser coluna, pela posição que ocupam."""
    colunas: set[str] = set()

    for lista in re.findall(r"\bINTO\s+\w+\s*\(([^)]*)\)", sql, re.I):
        colunas |= _identificadores(lista)

    for corpo in re.findall(r"\bSET\s+(.*?)(?=\bWHERE\b|$)", sql, re.I):
        colunas |= {m.lower() for m in re.findall(r"(\w+)\s*=", corpo)}

    colunas |= {m.lower() for m in re.findall(r"(\w+)\s*(?:=|<>|!=|<=|>=|<|>)", sql)}
    colunas |= {m.lower() for m in re.findall(r"(\w+)\s+(?:IS|IN|LIKE|BETWEEN)\b", sql, re.I)}

    for lista in re.findall(r"\bSELECT\s+(.*?)\s+FROM\b", sql, re.I):
        colunas |= _identificadores(lista)

    for lista in re.findall(r"\b(?:ORDER|GROUP)\s+BY\s+(.*?)(?=\bLIMIT\b|\)|$)", sql, re.I):
        colunas |= _identificadores(lista)

    return colunas - _PALAVRAS_SQL


def _queries_da_aplicacao() -> list[str]:
    # `information_schema` é catálogo do MySQL, não schema da aplicação.
    return [_normalizar(s) for s in _sqls() if "information_schema" not in s.lower()]


# ── init.sql não pode voltar a declarar schema ─────────────────────────────────

def test_init_sql_nao_declara_schema():
    """`init.sql` só roda na criação do volume — DDL ali diverge silenciosamente."""
    sql = INIT_SQL.read_text(encoding="utf-8")
    sem_comentarios = "\n".join(
        l for l in sql.splitlines() if not l.strip().startswith("--")
    )
    proibidos = re.findall(
        r"\b(CREATE\s+TABLE|ALTER\s+TABLE|INSERT\s+(?:IGNORE\s+)?INTO|UPDATE\s+\w+\s+SET)\b",
        sem_comentarios, re.I,
    )
    assert not proibidos, (
        f"mysql/init.sql voltou a declarar schema: {proibidos}. "
        "A fonte de verdade é _DDL_TABELAS em central-computer/database.py."
    )


# ── O DDL cobre tudo que o código usa ──────────────────────────────────────────

def test_toda_tabela_consultada_existe_no_ddl():
    faltando = {}
    for sql in _queries_da_aplicacao():
        for tabela in {t.lower() for t in _RE_TABELA.findall(sql)}:
            if tabela not in SCHEMA:
                faltando.setdefault(tabela, sql)
    assert not faltando, (
        "Tabelas consultadas por database.py e ausentes de _DDL_TABELAS: "
        + "; ".join(f"{t} (em {q[:70]}...)" for t, q in faltando.items())
    )


def test_toda_coluna_consultada_existe_no_ddl():
    """Pega drift automaticamente: query nova em coluna inexistente quebra aqui."""
    erros = []
    for sql in _queries_da_aplicacao():
        tabelas = {t.lower() for t in _RE_TABELA.findall(sql)}
        if not tabelas <= set(SCHEMA):
            continue  # coberto por test_toda_tabela_consultada_existe_no_ddl
        # Aliases (`AS total_itens`, `FROM ordens o`) não são coluna de tabela.
        permitidas = set().union(*(SCHEMA[t] for t in tabelas)) if tabelas else set()
        permitidas |= {a.lower() for a in _RE_ALIAS_AS.findall(sql)}
        permitidas |= {a.lower() for a in _RE_ALIAS_TAB.findall(sql)}

        ausentes = _colunas_referenciadas(sql) - permitidas
        if ausentes:
            erros.append(f"{sorted(ausentes)} em {sql[:90]}")
    assert not erros, "Colunas consultadas e ausentes do DDL:\n  " + "\n  ".join(erros)


def test_colunas_evolutivas_batem_com_o_ddl():
    """O ALTER de reparo e o CREATE precisam falar da mesma coluna."""
    for tabela, colunas in COLUNAS_EVOLUTIVAS.items():
        assert tabela in SCHEMA, f"_COLUNAS_EVOLUTIVAS cita tabela inexistente: {tabela}"
        ausentes = set(colunas) - SCHEMA[tabela]
        assert not ausentes, (
            f"{tabela}: {sorted(ausentes)} é reparado por ALTER mas não existe no "
            "CREATE TABLE — banco novo nasceria sem a coluna."
        )


def test_objetos_que_ja_divergiram():
    """Regressão nominal do bug que derrubava o central."""
    assert "visao_leituras" in SCHEMA
    assert "peso_unitario_g" in SCHEMA["medicamentos"]
    assert "peso_unitario_g" in COLUNAS_EVOLUTIVAS["medicamentos"]


# ── Comportamento do reparo e do seed ──────────────────────────────────────────

@pytest.fixture(scope="module")
def database():
    """Importa `central-computer/database.py` de verdade.

    O import não abre conexão: o módulo só conecta dentro de cada função.
    """
    if str(CENTRAL_DIR) not in sys.path:
        sys.path.insert(0, str(CENTRAL_DIR))
    import database as modulo
    return modulo


class CursorFake:
    """Cursor mínimo: grava os SQLs e responde ao que `_create_tables` pergunta.

    `slots` é a lista de ids que a tabela `dispenser_estado` JÁ tem — não uma
    contagem. O seed passou a comparar por id justamente porque a contagem não
    distingue "6 linhas numa célula de 6" de "6 linhas numa célula de 8".
    """

    def __init__(self, colunas: list[dict], slots: list[int] | None = None):
        self.executados: list[str] = []
        self._colunas = colunas
        self._slots = list(slots or [])
        self._resultado: list[dict] = []

    def execute(self, sql, args=None):
        self.executados.append(" ".join(sql.split()))
        if "information_schema" in sql:
            self._resultado = list(self._colunas)
        elif "FROM dispenser_estado" in sql:
            self._resultado = [{"dispenser_id": s} for s in self._slots]
        else:
            self._resultado = []

    def fetchall(self):
        return self._resultado

    def fetchone(self):
        return self._resultado[0] if self._resultado else None

    def sqls(self, prefixo: str) -> list[str]:
        return [s for s in self.executados if s.upper().startswith(prefixo.upper())]


def _information_schema(**nulavel_por_coluna: str) -> list[dict]:
    """Linhas de information_schema para um banco com o schema completo."""
    return [
        {"tabela": tabela, "coluna": coluna,
         "nulavel": nulavel_por_coluna.get(coluna, "YES")}
        for tabela, colunas in SCHEMA.items()
        for coluna in colunas
    ]


def test_schema_completo_nao_gera_alter(database):
    cur = CursorFake(_information_schema())
    database._aplicar_colunas_faltantes(cur)
    assert cur.sqls("ALTER") == []


def test_coluna_ausente_vira_alter(database):
    """Banco de versão anterior: `CREATE TABLE IF NOT EXISTS` não o repararia."""
    antigo = [l for l in _information_schema() if l["coluna"] != "peso_unitario_g"]
    cur = CursorFake(antigo)
    database._aplicar_colunas_faltantes(cur)
    assert cur.sqls("ALTER") == [
        "ALTER TABLE medicamentos ADD COLUMN peso_unitario_g DECIMAL(8,2) NULL AFTER dimensao"
    ]


def test_dispenser_id_not_null_e_relaxado(database):
    cur = CursorFake(_information_schema(dispenser_id="NO"))
    database._aplicar_colunas_faltantes(cur)
    assert cur.sqls("ALTER") == [
        "ALTER TABLE os_itens MODIFY COLUMN dispenser_id TINYINT NULL"
    ]


def test_seed_cria_uma_linha_por_slot_da_celula(database):
    """Banco vazio: uma linha para cada slot, nem mais nem menos."""
    cur = CursorFake([], slots=[])
    database._seed_dispenser_estado(cur)
    inserts = cur.sqls("INSERT")
    assert len(inserts) == NUM_SLOTS == database._SLOTS_DISPENSER
    assert all("dispenser_estado" in s for s in inserts)


def test_seed_e_idempotente(database):
    cur = CursorFake([], slots=list(range(1, NUM_SLOTS + 1)))
    database._seed_dispenser_estado(cur)
    assert cur.sqls("INSERT") == []


def test_seed_completa_banco_de_celula_menor(database):
    """A célula cresceu de 6 para 8: o banco de 6 linhas ganha D7 e D8.

    `CREATE TABLE IF NOT EXISTS` não repara tabela existente, e o critério
    antigo (`COUNT(*) >= _SLOTS_DISPENSER`) também não repararia esta: 6 linhas
    já bastavam para ele desistir. Só o INSERT do que falta acerta o banco
    antigo — o mesmo raciocínio de `_aplicar_colunas_faltantes`.
    """
    cur = CursorFake([], slots=[1, 2, 3, 4, 5, 6])
    database._seed_dispenser_estado(cur)
    assert len(cur.sqls("INSERT")) == max(0, NUM_SLOTS - 6)


def test_seed_repoe_linha_apagada_no_meio(database):
    """Buraco no meio volta na subida seguinte — slot sem linha não é gravado."""
    ids = [s for s in range(1, NUM_SLOTS + 1) if s != 2]
    cur = CursorFake([], slots=ids)
    database._seed_dispenser_estado(cur)
    assert len(cur.sqls("INSERT")) == 1


# ── init_db distingue indisponibilidade de erro de schema ──────────────────────

def _conn_que_levanta(erro):
    @contextmanager
    def _fake(*args, **kwargs):
        raise erro
        yield  # pragma: no cover — mantém a função geradora

    return _fake


def test_erro_de_schema_falha_rapido(database, monkeypatch):
    """1054 não é indisponibilidade: retentar 30x esconderia a causa."""
    erro = database.pymysql.OperationalError(1054, "Unknown column 'peso_unitario_g'")
    monkeypatch.setattr(database, "_conn", _conn_que_levanta(erro))
    monkeypatch.setattr(database.time, "sleep",
                        lambda _: pytest.fail("não deveria retentar erro de schema"))

    with pytest.raises(database.SchemaInvalido) as exc:
        database.init_db()
    assert "peso_unitario_g" in str(exc.value)


@pytest.mark.parametrize("codigo, mensagem", [
    (1045, "Access denied for user 'apsen'"),
    (1049, "Unknown database 'apsen_db'"),
])
def test_credencial_e_banco_errados_nao_viram_erro_de_schema(
    database, monkeypatch, codigo, mensagem
):
    """O servidor respondeu e recusou: nem indisponibilidade, nem schema."""
    erro = database.pymysql.OperationalError(codigo, mensagem)
    monkeypatch.setattr(database, "_conn", _conn_que_levanta(erro))
    monkeypatch.setattr(database.time, "sleep",
                        lambda _: pytest.fail("não deveria retentar erro de configuração"))

    with pytest.raises(database.ConfiguracaoInvalida) as exc:
        database.init_db()
    assert "MYSQL_USER" in str(exc.value)


def test_conexao_recusada_retenta_e_desiste(database, monkeypatch):
    erro = database.pymysql.OperationalError(2003, "Can't connect to MySQL server")
    esperas = []
    monkeypatch.setattr(database, "_conn", _conn_que_levanta(erro))
    monkeypatch.setattr(database, "_TENTATIVAS_CONEXAO", 3)
    monkeypatch.setattr(database.time, "sleep", esperas.append)

    with pytest.raises(database.BancoIndisponivel):
        database.init_db()
    assert len(esperas) == 2  # espera entre tentativas, não depois da última
