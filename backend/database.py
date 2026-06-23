"""
APSEN - Banco de Dados SQLite
Tabelas: contagens, eventos, usuarios, ordens_servico, ocorrencias_os, log_manutencao
"""
import sqlite3
from datetime import datetime, timezone
from config import settings
from auth import hash_senha


def _conn():
    c = sqlite3.connect(settings.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS contagens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            lote_id    TEXT    NOT NULL,
            valor      INTEGER NOT NULL,
            velocidade REAL    DEFAULT 0,
            ts         TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS eventos (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            lote_id TEXT NOT NULL,
            tipo    TEXT NOT NULL,
            detalhe TEXT DEFAULT '',
            ts      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usuarios (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    NOT NULL UNIQUE,
            senha_hash    TEXT    NOT NULL,
            role          TEXT    NOT NULL DEFAULT 'operador',
            nome_completo TEXT    NOT NULL DEFAULT '',
            ativo         INTEGER NOT NULL DEFAULT 1,
            criado_em     TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ordens_servico (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            os_id         TEXT    NOT NULL UNIQUE,
            produto       TEXT    NOT NULL,
            lote_id       TEXT    NOT NULL,
            meta          INTEGER NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'aberto',
            responsavel   TEXT    NOT NULL DEFAULT '',
            criado_por    TEXT    NOT NULL,
            criado_em     TEXT    NOT NULL,
            atualizado_em TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ocorrencias_os (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            os_id     TEXT NOT NULL,
            tipo      TEXT NOT NULL,
            descricao TEXT NOT NULL DEFAULT '',
            usuario   TEXT NOT NULL DEFAULT '',
            contagem  INTEGER DEFAULT NULL,
            ts        TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS log_manutencao (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo        TEXT NOT NULL,
            descricao   TEXT NOT NULL,
            responsavel TEXT NOT NULL,
            componente  TEXT DEFAULT '',
            ts          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cont_lote  ON contagens(lote_id, ts);
        CREATE INDEX IF NOT EXISTS idx_evt_lote   ON eventos(lote_id, ts);
        CREATE INDEX IF NOT EXISTS idx_ocorr_os   ON ocorrencias_os(os_id, ts);
        CREATE INDEX IF NOT EXISTS idx_manut_ts   ON log_manutencao(ts);
        """)
    _seed_usuarios()


def _seed_usuarios():
    with _conn() as db:
        count = db.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        if count > 0:
            return
        ts = _ts()
        for username, senha, role, nome in [
            ("admin",     "admin123",  "admin",      "Administrador"),
            ("operador1", "op123",     "operador",   "Operador 1"),
            ("manut1",    "mnt123",    "manutencao", "Técnico Manutenção"),
        ]:
            db.execute(
                "INSERT INTO usuarios (username, senha_hash, role, nome_completo, criado_em) VALUES (?,?,?,?,?)",
                (username, hash_senha(senha), role, nome, ts)
            )


def _ts():
    return datetime.now(timezone.utc).isoformat()


# ── Contagens ──────────────────────────────────────────────────────────────────
def salvar_contagem(lote_id: str, valor: int, velocidade: float = 0):
    with _conn() as db:
        db.execute(
            "INSERT INTO contagens (lote_id, valor, velocidade, ts) VALUES (?,?,?,?)",
            (lote_id, valor, velocidade, _ts())
        )

def salvar_evento(lote_id: str, tipo: str, detalhe: str = ""):
    with _conn() as db:
        db.execute(
            "INSERT INTO eventos (lote_id, tipo, detalhe, ts) VALUES (?,?,?,?)",
            (lote_id, tipo, detalhe, _ts())
        )

def get_historico(lote_id: str = None, limite: int = 200) -> list[dict]:
    with _conn() as db:
        if lote_id:
            rows = db.execute(
                "SELECT * FROM contagens WHERE lote_id=? ORDER BY ts DESC LIMIT ?",
                (lote_id, limite)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM contagens ORDER BY ts DESC LIMIT ?", (limite,)
            ).fetchall()
    return [dict(r) for r in rows]

def get_lote_atual(lote_id: str) -> dict:
    with _conn() as db:
        cont = db.execute(
            "SELECT valor, velocidade, ts FROM contagens WHERE lote_id=? ORDER BY ts DESC LIMIT 1",
            (lote_id,)
        ).fetchone()
        evts = db.execute(
            "SELECT tipo, detalhe, ts FROM eventos WHERE lote_id=? ORDER BY ts DESC LIMIT 10",
            (lote_id,)
        ).fetchall()
    return {
        "lote_id": lote_id,
        "ultima_contagem": dict(cont) if cont else None,
        "eventos_recentes": [dict(e) for e in evts],
    }


# ── Usuários ───────────────────────────────────────────────────────────────────
def get_usuario(username: str) -> dict | None:
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM usuarios WHERE username=? AND ativo=1", (username,)
        ).fetchone()
    return dict(row) if row else None

def listar_usuarios() -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            "SELECT id, username, role, nome_completo, ativo, criado_em FROM usuarios ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]

def criar_usuario(username: str, senha: str, role: str, nome: str) -> dict:
    with _conn() as db:
        db.execute(
            "INSERT INTO usuarios (username, senha_hash, role, nome_completo, criado_em) VALUES (?,?,?,?,?)",
            (username, hash_senha(senha), role, nome, _ts())
        )
    return {"ok": True, "username": username}

def atualizar_usuario(username: str, campos: dict) -> dict:
    if "senha" in campos:
        campos["senha_hash"] = hash_senha(campos.pop("senha"))
    sets = ", ".join(f"{k}=?" for k in campos)
    vals = list(campos.values()) + [username]
    with _conn() as db:
        db.execute(f"UPDATE usuarios SET {sets} WHERE username=?", vals)
    return {"ok": True}


# ── Ordens de Serviço ──────────────────────────────────────────────────────────
def _prox_os_id() -> str:
    # Usa MAX(id) em vez de COUNT(*) para evitar colisão de IDs
    # caso alguma OS seja deletada ou haja criação concorrente
    with _conn() as db:
        n = db.execute("SELECT COALESCE(MAX(id), 0) FROM ordens_servico").fetchone()[0]
    return f"OS-{n + 1:04d}"

def criar_os(produto: str, lote_id: str, meta: int, responsavel: str, criado_por: str) -> dict:
    os_id = _prox_os_id()
    ts = _ts()
    with _conn() as db:
        db.execute(
            """INSERT INTO ordens_servico
               (os_id, produto, lote_id, meta, status, responsavel, criado_por, criado_em, atualizado_em)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (os_id, produto, lote_id, meta, "aberto", responsavel, criado_por, ts, ts)
        )
    registrar_ocorrencia(os_id, "criacao", f"OS criada por {criado_por}", criado_por)
    return {"os_id": os_id}

def listar_os(status: str = None) -> list[dict]:
    with _conn() as db:
        if status:
            rows = db.execute(
                "SELECT * FROM ordens_servico WHERE status=? ORDER BY criado_em DESC", (status,)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM ordens_servico ORDER BY criado_em DESC"
            ).fetchall()
    return [dict(r) for r in rows]

def get_os(os_id: str) -> dict | None:
    with _conn() as db:
        row = db.execute("SELECT * FROM ordens_servico WHERE os_id=?", (os_id,)).fetchone()
    return dict(row) if row else None

def atualizar_os(os_id: str, campos: dict, usuario: str) -> dict:
    campos["atualizado_em"] = _ts()
    sets = ", ".join(f"{k}=?" for k in campos)
    vals = list(campos.values()) + [os_id]
    with _conn() as db:
        db.execute(f"UPDATE ordens_servico SET {sets} WHERE os_id=?", vals)
    if "status" in campos:
        registrar_ocorrencia(os_id, f"status_{campos['status']}",
                             f"Status alterado para '{campos['status']}'", usuario)
    return {"ok": True}


# ── Ocorrências ────────────────────────────────────────────────────────────────
def registrar_ocorrencia(os_id: str, tipo: str, descricao: str,
                          usuario: str, contagem: int = None):
    with _conn() as db:
        db.execute(
            "INSERT INTO ocorrencias_os (os_id, tipo, descricao, usuario, contagem, ts) VALUES (?,?,?,?,?,?)",
            (os_id, tipo, descricao, usuario, contagem, _ts())
        )

def get_ocorrencias(os_id: str) -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM ocorrencias_os WHERE os_id=? ORDER BY ts DESC", (os_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Manutenção ─────────────────────────────────────────────────────────────────
def registrar_manutencao(tipo: str, descricao: str, responsavel: str, componente: str = "") -> dict:
    with _conn() as db:
        db.execute(
            "INSERT INTO log_manutencao (tipo, descricao, responsavel, componente, ts) VALUES (?,?,?,?,?)",
            (tipo, descricao, responsavel, componente, _ts())
        )
    return {"ok": True}

def get_log_manutencao(limite: int = 100) -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM log_manutencao ORDER BY ts DESC LIMIT ?", (limite,)
        ).fetchall()
    return [dict(r) for r in rows]

def get_alarmes(limite: int = 100) -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            """SELECT tipo, detalhe as descricao, ts FROM eventos
               WHERE tipo IN ('alarme','alarm','falha','erro')
               ORDER BY ts DESC LIMIT ?""", (limite,)
        ).fetchall()
    return [dict(r) for r in rows]
