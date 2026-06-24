"""
APSEN - Banco de Dados MySQL (PyMySQL)
Substitui a implementação SQLite anterior.
Usa autocommit=True para leituras/escritas simples e transação
explícita somente em criar_os() para geração atômica do OS-XXXX.
"""
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import pymysql
import pymysql.cursors

from auth import hash_senha
from config import settings

logger = logging.getLogger(__name__)


# ── Conexão ────────────────────────────────────────────────────────────────────

def _make_conn(autocommit: bool = True) -> pymysql.Connection:
    return pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASS,
        database=settings.MYSQL_DB,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=autocommit,
    )


@contextmanager
def _conn(autocommit: bool = True):
    """Context manager que garante fechamento da conexão."""
    conn = _make_conn(autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# ── Inicialização ──────────────────────────────────────────────────────────────

def init_db():
    """Aguarda MySQL ficar disponível e cria tabelas + seed de usuários."""
    for attempt in range(30):
        try:
            with _conn() as conn:
                _create_tables(conn)
            logger.info("MySQL conectado e schema verificado.")
            break
        except pymysql.OperationalError as exc:
            logger.warning(f"MySQL indisponível (tentativa {attempt + 1}/30): {exc}")
            if attempt < 29:
                time.sleep(2)
            else:
                raise
    _seed_usuarios()


def _create_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS contagens (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            lote_id    VARCHAR(100) NOT NULL,
            valor      INT NOT NULL,
            velocidade DECIMAL(10,2) DEFAULT 0,
            ts         DATETIME(3) NOT NULL,
            INDEX idx_cont_lote (lote_id, ts)
        ) ENGINE=InnoDB
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS eventos (
            id      INT AUTO_INCREMENT PRIMARY KEY,
            lote_id VARCHAR(100) NOT NULL,
            tipo    VARCHAR(50)  NOT NULL,
            detalhe TEXT,
            ts      DATETIME(3) NOT NULL,
            INDEX idx_evt_lote (lote_id, ts)
        ) ENGINE=InnoDB
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id            INT AUTO_INCREMENT PRIMARY KEY,
            username      VARCHAR(100) NOT NULL UNIQUE,
            senha_hash    TEXT NOT NULL,
            role          VARCHAR(50)  NOT NULL DEFAULT 'operador',
            nome_completo VARCHAR(200) NOT NULL DEFAULT '',
            ativo         TINYINT(1)   NOT NULL DEFAULT 1,
            criado_em     DATETIME(3)  NOT NULL,
            INDEX idx_user_ativo (username, ativo)
        ) ENGINE=InnoDB
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS ordens_servico (
            id            INT AUTO_INCREMENT PRIMARY KEY,
            os_id         VARCHAR(20)  UNIQUE DEFAULT NULL,
            produto       VARCHAR(200) NOT NULL,
            lote_id       VARCHAR(100) NOT NULL,
            meta          INT NOT NULL,
            status        VARCHAR(50)  NOT NULL DEFAULT 'aberto',
            responsavel   VARCHAR(200) NOT NULL DEFAULT '',
            criado_por    VARCHAR(100) NOT NULL,
            criado_em     DATETIME(3)  NOT NULL,
            atualizado_em DATETIME(3)  NOT NULL,
            INDEX idx_os_lote   (lote_id),
            INDEX idx_os_status (status)
        ) ENGINE=InnoDB
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS ocorrencias_os (
            id        INT AUTO_INCREMENT PRIMARY KEY,
            os_id     VARCHAR(20)  NOT NULL,
            tipo      VARCHAR(100) NOT NULL,
            descricao TEXT NOT NULL,
            usuario   VARCHAR(100) NOT NULL DEFAULT '',
            contagem  INT DEFAULT NULL,
            ts        DATETIME(3)  NOT NULL,
            INDEX idx_ocorr_os (os_id, ts)
        ) ENGINE=InnoDB
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS log_manutencao (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            tipo        VARCHAR(50)  NOT NULL,
            descricao   TEXT NOT NULL,
            responsavel VARCHAR(100) NOT NULL,
            componente  VARCHAR(100) DEFAULT '',
            ts          DATETIME(3)  NOT NULL,
            INDEX idx_manut_ts (ts)
        ) ENGINE=InnoDB
        """)


def _seed_usuarios():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM usuarios")
            if cur.fetchone()["n"] > 0:
                return
        ts = _ts()
        seeds = [
            ("admin",     "admin123",  "admin",      "Administrador"),
            ("operador1", "op123",     "operador",   "Operador 1"),
            ("manut1",    "mnt123",    "manutencao", "Técnico Manutenção"),
        ]
        with conn.cursor() as cur:
            for username, senha, role, nome in seeds:
                cur.execute(
                    "INSERT IGNORE INTO usuarios "
                    "(username, senha_hash, role, nome_completo, criado_em) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (username, hash_senha(senha), role, nome, ts),
                )
    logger.info("Usuários seed inseridos.")


# ── Contagens ──────────────────────────────────────────────────────────────────

def salvar_contagem(lote_id: str, valor: int, velocidade: float = 0):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO contagens (lote_id, valor, velocidade, ts) VALUES (%s, %s, %s, %s)",
                (lote_id, valor, velocidade, _ts()),
            )


def salvar_evento(lote_id: str, tipo: str, detalhe: str = ""):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO eventos (lote_id, tipo, detalhe, ts) VALUES (%s, %s, %s, %s)",
                (lote_id, tipo, detalhe, _ts()),
            )


def get_historico(lote_id: str = None, limite: int = 200) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            if lote_id:
                cur.execute(
                    "SELECT * FROM contagens WHERE lote_id=%s ORDER BY ts DESC LIMIT %s",
                    (lote_id, limite),
                )
            else:
                cur.execute(
                    "SELECT * FROM contagens ORDER BY ts DESC LIMIT %s", (limite,)
                )
            rows = cur.fetchall()
    # Converte datetime para string ISO para serialização JSON
    return [_row_to_dict(r) for r in rows]


def get_lote_atual(lote_id: str) -> dict:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT valor, velocidade, ts FROM contagens "
                "WHERE lote_id=%s ORDER BY ts DESC LIMIT 1",
                (lote_id,),
            )
            cont = cur.fetchone()
            cur.execute(
                "SELECT tipo, detalhe, ts FROM eventos "
                "WHERE lote_id=%s ORDER BY ts DESC LIMIT 10",
                (lote_id,),
            )
            evts = cur.fetchall()
    return {
        "lote_id": lote_id,
        "ultima_contagem": _row_to_dict(cont) if cont else None,
        "eventos_recentes": [_row_to_dict(e) for e in evts],
    }


# ── Usuários ───────────────────────────────────────────────────────────────────

def get_usuario(username: str) -> dict | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM usuarios WHERE username=%s AND ativo=1", (username,)
            )
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def listar_usuarios() -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, role, nome_completo, ativo, criado_em "
                "FROM usuarios ORDER BY id"
            )
            rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def criar_usuario(username: str, senha: str, role: str, nome: str) -> dict:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO usuarios (username, senha_hash, role, nome_completo, criado_em) "
                "VALUES (%s, %s, %s, %s, %s)",
                (username, hash_senha(senha), role, nome, _ts()),
            )
    return {"ok": True, "username": username}


def atualizar_usuario(username: str, campos: dict) -> dict:
    if "senha" in campos:
        campos["senha_hash"] = hash_senha(campos.pop("senha"))
    sets = ", ".join(f"{k}=%s" for k in campos)
    vals = list(campos.values()) + [username]
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE usuarios SET {sets} WHERE username=%s", vals)
    return {"ok": True}


# ── Ordens de Serviço ──────────────────────────────────────────────────────────

def criar_os(produto: str, lote_id: str, meta: int, responsavel: str, criado_por: str) -> dict:
    """
    Cria OS com ID atômico usando AUTO_INCREMENT do MySQL:
    1. INSERT sem os_id (NULL) — gera id único via AUTO_INCREMENT
    2. Deriva os_id = "OS-XXXX" a partir de lastrowid
    3. UPDATE seta os_id — dentro da mesma conexão/transação
    """
    ts = _ts()
    # Transação explícita (autocommit=False) para garantir atomicidade
    with _conn(autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ordens_servico "
                    "(produto, lote_id, meta, status, responsavel, criado_por, criado_em, atualizado_em) "
                    "VALUES (%s, %s, %s, 'aberto', %s, %s, %s, %s)",
                    (produto, lote_id, meta, responsavel, criado_por, ts, ts),
                )
                new_id = cur.lastrowid
                os_id = f"OS-{new_id:04d}"
                cur.execute(
                    "UPDATE ordens_servico SET os_id=%s WHERE id=%s",
                    (os_id, new_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    registrar_ocorrencia(os_id, "criacao", f"OS criada por {criado_por}", criado_por)
    return {"os_id": os_id}


def listar_os(status: str = None) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            if status:
                cur.execute(
                    "SELECT * FROM ordens_servico WHERE status=%s "
                    "ORDER BY criado_em DESC",
                    (status,),
                )
            else:
                cur.execute(
                    "SELECT * FROM ordens_servico ORDER BY criado_em DESC"
                )
            rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def get_os(os_id: str) -> dict | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ordens_servico WHERE os_id=%s", (os_id,)
            )
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def atualizar_os(os_id: str, campos: dict, usuario: str) -> dict:
    campos["atualizado_em"] = _ts()
    sets = ", ".join(f"{k}=%s" for k in campos)
    vals = list(campos.values()) + [os_id]
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE ordens_servico SET {sets} WHERE os_id=%s", vals
            )
    if "status" in campos:
        registrar_ocorrencia(
            os_id,
            f"status_{campos['status']}",
            f"Status alterado para '{campos['status']}'",
            usuario,
        )
    return {"ok": True}


# ── Ocorrências ────────────────────────────────────────────────────────────────

def registrar_ocorrencia(
    os_id: str, tipo: str, descricao: str, usuario: str, contagem: int = None
):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ocorrencias_os "
                "(os_id, tipo, descricao, usuario, contagem, ts) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (os_id, tipo, descricao, usuario, contagem, _ts()),
            )


def get_ocorrencias(os_id: str) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ocorrencias_os WHERE os_id=%s ORDER BY ts DESC",
                (os_id,),
            )
            rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


# ── Manutenção ─────────────────────────────────────────────────────────────────

def registrar_manutencao(
    tipo: str, descricao: str, responsavel: str, componente: str = ""
) -> dict:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO log_manutencao "
                "(tipo, descricao, responsavel, componente, ts) "
                "VALUES (%s, %s, %s, %s, %s)",
                (tipo, descricao, responsavel, componente, _ts()),
            )
    return {"ok": True}


def get_log_manutencao(limite: int = 100) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM log_manutencao ORDER BY ts DESC LIMIT %s", (limite,)
            )
            rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def get_alarmes(limite: int = 100) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tipo, detalhe AS descricao, ts FROM eventos "
                "WHERE tipo IN ('alarme','alarm','falha','erro') "
                "ORDER BY ts DESC LIMIT %s",
                (limite,),
            )
            rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


# ── Utilitário ─────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict | None:
    """Converte row do PyMySQL para dict, normalizando datetime para string ISO."""
    if row is None:
        return None
    result = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result
