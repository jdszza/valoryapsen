"""Pool de conexões do central — sem subir MySQL.

Antes havia UMA conexão nova por operação: TCP + handshake + autenticação para
um único INSERT, e fora. O custo não aparece numa query isolada, e o central não
faz queries isoladas — cada evento de adapter são de 1 a 3 escritas, e só a
telemetria dos slots são `NUM_SLOTS` gravações a cada 15s, somadas às da CNC, da
visão e da balança.

O que estes testes travam não é a economia (que um cronômetro em suíte mediria
mal), e sim as REGRAS que fazem reaproveitar conexão ser seguro:

  * conexão devolvida é reaproveitada — senão o pool não existe;
  * conexão parada há muito tempo é verificada antes de voltar a ser usada, e
    substituída se o MySQL já a fechou por `wait_timeout`;
  * conexão que viu exceção NÃO volta ao pool: erro no meio de um statement
    pode deixar resultado por ler no socket, e o sintoma disso é a operação
    SEGUINTE falhar, em outra thread, sem relação visível com a causa;
  * só quem pediu `autocommit=False` paga o ROLLBACK de devolução;
  * o teto é do que fica GUARDADO, não do que pode abrir: pool cheio fecha o
    excedente, pool vazio abre mais em vez de bloquear a thread.

Nenhum teste toca em banco: `_make_conn` é substituído por uma fábrica de
duplos, que é justamente a fronteira sobre a qual o pool foi construído.
"""
import ast
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

RAIZ_REPO   = Path(__file__).resolve().parent.parent
CENTRAL_DIR = RAIZ_REPO / "central-computer"
DATABASE_PY = CENTRAL_DIR / "database.py"


class ConexaoFake:
    """Superfície mínima que o pool usa de uma `pymysql.Connection`."""

    def __init__(self, numero: int):
        self.numero = numero
        self.fechada = False
        self.pings = 0
        self.rollbacks = 0
        self.autocommits: list[bool] = []
        self.ping_explode = False

    def close(self):
        self.fechada = True

    def ping(self, reconnect=True):
        self.pings += 1
        if self.ping_explode:
            raise OSError("MySQL server has gone away")

    def rollback(self):
        self.rollbacks += 1

    def autocommit(self, valor):
        self.autocommits.append(bool(valor))

    def cursor(self):
        raise AssertionError("nenhum teste deste arquivo executa SQL")


class Fabrica:
    """Substitui `_make_conn`: conta aberturas e entrega duplos numerados."""

    def __init__(self):
        self.abertas: list[ConexaoFake] = []

    def __call__(self, autocommit: bool = True) -> ConexaoFake:
        conn = ConexaoFake(len(self.abertas) + 1)
        conn.autocommits.append(bool(autocommit))   # o modo pedido na abertura
        self.abertas.append(conn)
        return conn

    @property
    def total(self) -> int:
        return len(self.abertas)


@pytest.fixture
def db(monkeypatch):
    """`database.py` com `_make_conn` duplado e o pool vazio nas duas pontas.

    O pool é estado de MÓDULO e o módulo fica no cache do `sys.modules` entre
    testes — esvaziá-lo na entrada E na saída é o que impede um duplo de um
    teste de aparecer no seguinte.
    """
    if str(CENTRAL_DIR) not in sys.path:
        sys.path.insert(0, str(CENTRAL_DIR))
    import database as modulo

    modulo.fechar_pool()
    fabrica = Fabrica()
    monkeypatch.setattr(modulo, "_make_conn", fabrica)
    modulo.fabrica = fabrica          # atalho para os testes
    yield modulo
    modulo.fechar_pool()


# ── A economia: a conexão volta e é reusada ───────────────────────────────────

def test_conexao_devolvida_e_reaproveitada(db):
    """A razão de o pool existir: duas operações, um handshake."""
    with db._conn() as primeira:
        pass
    with db._conn() as segunda:
        pass

    assert primeira is segunda
    assert db.fabrica.total == 1
    assert primeira.fechada is False


def test_uso_simultaneo_abre_conexoes_distintas(db):
    """Duas operações ao mesmo tempo não podem compartilhar socket.

    O central fala com o banco por `asyncio.to_thread`, então isto acontece o
    tempo todo: entregar a MESMA conexão a duas threads embaralharia dois
    resultados no mesmo socket.
    """
    with db._conn() as a:
        with db._conn() as b:
            assert a is not b
            assert db.fabrica.total == 2

    # Devolvidas, as duas ficam guardadas: a terceira operação não abre nada.
    with db._conn():
        assert db.fabrica.total == 2


def test_threads_concorrentes_recebem_conexoes_distintas(db):
    """O pool é `queue`, mas o teste é sobre as threads que o central usa."""
    n = 4
    barreira = threading.Barrier(n)
    vistas: list[ConexaoFake] = []
    trava = threading.Lock()

    def trabalhar():
        with db._conn() as conn:
            barreira.wait(timeout=5)      # todas seguram ao mesmo tempo
            with trava:
                vistas.append(conn)

    threads = [threading.Thread(target=trabalhar) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(vistas) == n
    assert len({id(c) for c in vistas}) == n
    assert db.fabrica.total == n
    # E depois de todas devolverem, nada de novo é aberto.
    with db._conn():
        pass
    assert db.fabrica.total == n


# ── Conexão que o MySQL fechou por baixo ──────────────────────────────────────

def test_conexao_parada_ha_pouco_nao_paga_ping(db):
    """O caminho quente é o que o pool existe para acelerar — nada de round
    trip extra nele."""
    with db._conn() as conn:
        pass
    with db._conn():
        pass

    assert conn.pings == 0


def test_conexao_parada_ha_muito_tempo_e_verificada(db):
    """`wait_timeout` do MySQL fecha o socket sem avisar ninguém."""
    with db._conn() as conn:
        pass
    # Envelhece a entrada guardada sem esperar `_PING_APOS_S` de verdade.
    guardada, _ = db._pool.get_nowait()
    db._pool.put_nowait((guardada, time.monotonic() - db._PING_APOS_S - 1))

    with db._conn() as reusada:
        pass

    assert reusada is conn
    assert conn.pings == 1


def test_conexao_morta_e_descartada_e_substituida(db):
    """Ping que falha não pode virar exceção na escrita de telemetria."""
    with db._conn() as morta:
        pass
    morta.ping_explode = True
    guardada, _ = db._pool.get_nowait()
    db._pool.put_nowait((guardada, time.monotonic() - db._PING_APOS_S - 1))

    with db._conn() as nova:
        pass

    assert nova is not morta
    assert morta.fechada is True
    assert db.fabrica.total == 2


# ── Conexão que viu exceção ───────────────────────────────────────────────────

def test_excecao_descarta_a_conexao_em_vez_de_devolve_la(db):
    """Regra 2: o estrago de um socket sujo aparece na operação SEGUINTE."""
    with pytest.raises(RuntimeError):
        with db._conn() as suja:
            raise RuntimeError("1054 Unknown column")

    assert suja.fechada is True
    assert db._pool.qsize() == 0

    with db._conn() as nova:
        pass
    assert nova is not suja
    assert db.fabrica.total == 2


def test_cancelamento_tambem_descarta(db):
    """`CancelledError` herda de `BaseException` — um `except Exception` a
    deixaria passar, e o socket voltaria ao pool no meio de um statement."""
    import asyncio

    with pytest.raises(asyncio.CancelledError):
        with db._conn() as suja:
            raise asyncio.CancelledError()

    assert suja.fechada is True
    assert db._pool.qsize() == 0


# ── Transação aberta não sobrevive ao empréstimo ──────────────────────────────

def test_autocommit_false_faz_rollback_na_devolucao(db):
    """`salvar_ordem` usa `autocommit=False`: transação sua não vaza para o
    próximo que pegar esta conexão."""
    with db._conn(autocommit=False) as conn:
        pass

    assert conn.rollbacks == 1


def test_autocommit_true_nao_paga_rollback(db):
    """Sem transação não há o que desfazer, e o ROLLBACK é um round trip —
    cobrá-lo de todo mundo devolveria boa parte do que o pool economizou."""
    with db._conn() as conn:
        pass

    assert conn.rollbacks == 0


def test_modo_autocommit_e_reajustado_ao_reaproveitar(db):
    """A mesma conexão serve os dois modos; quem pega é quem diz qual quer."""
    with db._conn(autocommit=False):
        pass
    with db._conn(autocommit=True) as conn:
        pass

    assert conn.autocommits[-1] is True


def test_rollback_que_falha_descarta_a_conexao(db):
    """Se nem o ROLLBACK passa, a conexão não está em estado de ser reusada."""
    with db._conn(autocommit=False) as conn:
        conn.rollback = lambda: (_ for _ in ()).throw(OSError("gone away"))

    assert conn.fechada is True
    assert db._pool.qsize() == 0


# ── O teto ────────────────────────────────────────────────────────────────────

def test_teto_limita_o_que_fica_GUARDADO_e_nao_o_que_abre(db, monkeypatch):
    """Pool cheio fecha o excedente; pool vazio abre mais em vez de bloquear.

    Bloquear seria pior que o problema original: uma rajada acima do teto
    congelaria as threads do `to_thread`, e com elas o orquestrador, por causa
    de um número de configuração.
    """
    monkeypatch.setattr(db, "_pool", queue.LifoQueue(maxsize=2))

    with db._conn() as a:
        with db._conn() as b:
            with db._conn() as c:
                assert db.fabrica.total == 3      # pool vazio NÃO bloqueou

    guardadas = [a, b, c]
    assert db._pool.qsize() == 2
    assert sum(1 for conn in guardadas if conn.fechada) == 1


def test_fechar_pool_fecha_o_que_estava_guardado(db):
    """No shutdown não há operação seguinte, e socket aberto faz o MySQL
    segurar o slot até o `wait_timeout`."""
    with db._conn() as a:
        with db._conn() as b:
            pass

    db.fechar_pool()

    assert a.fechada is True and b.fechada is True
    assert db._pool.qsize() == 0


# ── Ninguém escapa do pool ────────────────────────────────────────────────────

_HELPERS_DO_POOL = {"_pegar_conn"}


def test_nenhuma_funcao_de_banco_abre_conexao_por_fora(db):
    """Varredura por AST, no espírito de `test_schema.py`: sem lista manual.

    `_make_conn` continua existindo — é a fábrica que o pool usa. O que não pode
    voltar é uma função de banco chamando-a direto: a conexão nasceria fora do
    pool, não seria devolvida, e o vazamento só apareceria no
    `max_connections` do MySQL horas depois.
    """
    arvore = ast.parse(DATABASE_PY.read_text(encoding="utf-8"))
    infratores = []
    for no in ast.walk(arvore):
        if not isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if no.name in _HELPERS_DO_POOL:
            continue
        for interno in ast.walk(no):
            if (isinstance(interno, ast.Call)
                    and isinstance(interno.func, ast.Name)
                    and interno.func.id == "_make_conn"):
                infratores.append(f"{no.name} (linha {interno.lineno})")

    assert not infratores, (
        "chamada direta a _make_conn fora do pool: " + ", ".join(infratores)
    )


def test_contextmanager_conn_continua_sendo_a_porta_unica(db):
    """40 chamadas de `with _conn()` em `database.py` dependem disso."""
    assert hasattr(db._conn, "__wrapped__"), "_conn deixou de ser contextmanager"


# ── Configuração ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def config():
    if str(CENTRAL_DIR) not in sys.path:
        sys.path.insert(0, str(CENTRAL_DIR))
    import config as modulo
    return modulo


@pytest.mark.parametrize("bruto", ["0", "-1", "65", "muitas", ""])
def test_pool_max_fora_da_faixa_cai_no_default(config, monkeypatch, caplog, bruto):
    """Valor esquisito não pode virar pool de 0 (toda operação abre e fecha) nem
    de 10000 (o central sozinho encosta no `max_connections` do MySQL)."""
    monkeypatch.setenv("MYSQL_POOL_MAX", bruto)

    assert config._mysql_pool_max() == 8


@pytest.mark.parametrize("bruto,esperado", [("1", 1), ("16", 16), ("64", 64)])
def test_pool_max_na_faixa_e_respeitado(config, monkeypatch, bruto, esperado):
    monkeypatch.setenv("MYSQL_POOL_MAX", bruto)

    assert config._mysql_pool_max() == esperado
