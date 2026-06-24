"""
APSEN - Banco de Dados MySQL (PyMySQL)
Schema v2.0 — ordens, dispensas, CNC, sensores, manutenção, usuários
"""
import json
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
    conn = _make_conn(autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _row(r) -> dict | None:
    if r is None:
        return None
    out = {}
    for k, v in r.items():
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


def _rows(rs) -> list:
    return [_row(r) for r in rs]


# ── Inicialização ──────────────────────────────────────────────────────────────

def init_db():
    for attempt in range(30):
        try:
            with _conn() as conn:
                _create_tables(conn)
            _seed_usuarios()
            logger.info("MySQL conectado e schema verificado.")
            return
        except pymysql.OperationalError as exc:
            logger.warning(f"MySQL não disponível ({attempt+1}/30): {exc}")
            if attempt < 29:
                time.sleep(2)
            else:
                raise


def _create_tables(conn):
    ddl_list = [
        """CREATE TABLE IF NOT EXISTS ordens (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            os_id        VARCHAR(60)  NOT NULL UNIQUE,
            descricao    VARCHAR(200) NOT NULL DEFAULT '',
            status       VARCHAR(30)  NOT NULL DEFAULT 'aguardando',
            payload_json TEXT         NOT NULL,
            criado_em    DATETIME(3)  NOT NULL,
            concluida_em DATETIME(3)  NULL,
            INDEX idx_os_status (status),
            INDEX idx_os_criado (criado_em)
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS os_itens (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            os_id           VARCHAR(60)  NOT NULL,
            dispenser_id    TINYINT      NOT NULL,
            medicamento     VARCHAR(100) NOT NULL,
            quantidade_alvo INT          NOT NULL,
            quantidade_real INT          NOT NULL DEFAULT 0,
            status          VARCHAR(30)  NOT NULL DEFAULT 'pendente',
            INDEX idx_ositem_os (os_id, dispenser_id)
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS dispensas (
            id                   INT AUTO_INCREMENT PRIMARY KEY,
            os_id                VARCHAR(60)  NOT NULL,
            dispenser_id         TINYINT      NOT NULL,
            medicamento          VARCHAR(100) NOT NULL,
            quantidade_dispensada INT         NOT NULL,
            quantidade_alvo      INT          NOT NULL,
            validado             TINYINT(1)   NOT NULL DEFAULT 1,
            motivo_falha         TEXT         NULL,
            ts                   DATETIME(3)  NOT NULL,
            INDEX idx_disp_os (os_id, dispenser_id, ts),
            INDEX idx_disp_ts (ts)
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS cnc_eventos (
            id             INT AUTO_INCREMENT PRIMARY KEY,
            os_id          VARCHAR(60)  NULL,
            status         VARCHAR(30)  NOT NULL,
            dispenser_alvo TINYINT      NULL,
            posicao_x      DECIMAL(8,3) NULL,
            posicao_y      DECIMAL(8,3) NULL,
            ciclo_atual    INT          NOT NULL DEFAULT 0,
            total_ciclos   INT          NOT NULL DEFAULT 0,
            ts             DATETIME(3)  NOT NULL,
            INDEX idx_cnc_ts (ts),
            INDEX idx_cnc_os (os_id, ts)
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS leituras_sensores (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            componente VARCHAR(100) NOT NULL,
            tipo       VARCHAR(30)  NOT NULL,
            valor      DECIMAL(10,3) NOT NULL,
            unidade    VARCHAR(20)  NOT NULL,
            ts         DATETIME(3)  NOT NULL,
            INDEX idx_sensor_comp (componente, ts),
            INDEX idx_sensor_ts   (ts)
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS alarmes (
            id        INT AUTO_INCREMENT PRIMARY KEY,
            fonte     VARCHAR(50)  NOT NULL,
            tipo      VARCHAR(60)  NOT NULL,
            descricao TEXT         NOT NULL,
            resolvido TINYINT(1)   NOT NULL DEFAULT 0,
            ts        DATETIME(3)  NOT NULL,
            INDEX idx_alarm_resolvido (resolvido, ts),
            INDEX idx_alarm_ts        (ts)
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS log_manutencao (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            tipo       VARCHAR(50)  NOT NULL,
            componente VARCHAR(100) NOT NULL,
            descricao  TEXT         NOT NULL,
            tecnico    VARCHAR(100) NOT NULL,
            ts         DATETIME(3)  NOT NULL,
            INDEX idx_manut_ts (ts)
        ) ENGINE=InnoDB""",

        """CREATE TABLE IF NOT EXISTS usuarios (
            id            INT AUTO_INCREMENT PRIMARY KEY,
            username      VARCHAR(100) NOT NULL UNIQUE,
            senha_hash    TEXT         NOT NULL,
            nome_completo VARCHAR(200) NOT NULL DEFAULT '',
            ativo         TINYINT(1)   NOT NULL DEFAULT 1,
            criado_em     DATETIME(3)  NOT NULL,
            INDEX idx_user (username, ativo)
        ) ENGINE=InnoDB""",
    ]
    with conn.cursor() as cur:
        for ddl in ddl_list:
            cur.execute(ddl)


def _seed_usuarios():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM usuarios")
            if cur.fetchone()["n"] > 0:
                return
        ts = _ts()
        seeds = [
            ("admin",  "admin123", "Administrador"),
            ("manut1", "mnt123",   "Técnico de Manutenção"),
        ]
        with conn.cursor() as cur:
            for username, senha, nome in seeds:
                cur.execute(
                    "INSERT IGNORE INTO usuarios "
                    "(username, senha_hash, nome_completo, criado_em) VALUES (%s,%s,%s,%s)",
                    (username, hash_senha(senha), nome, ts),
                )
    logger.info("Usuários seed inseridos.")


# ── Ordens ─────────────────────────────────────────────────────────────────────

def salvar_ordem(os_id: str, descricao: str, medicamentos: list, payload_raw: dict):
    ts = _ts()
    with _conn(autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT IGNORE INTO ordens "
                    "(os_id, descricao, status, payload_json, criado_em) "
                    "VALUES (%s,%s,'aguardando',%s,%s)",
                    (os_id, descricao, json.dumps(payload_raw), ts),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return  # OS duplicada — ignora
                for item in medicamentos:
                    cur.execute(
                        "INSERT INTO os_itens "
                        "(os_id, dispenser_id, medicamento, quantidade_alvo) "
                        "VALUES (%s,%s,%s,%s)",
                        (os_id, item["dispenser_id"], item["medicamento"], item["quantidade"]),
                    )
            conn.commit()
            logger.info(f"[DB] OS {os_id} salva com {len(medicamentos)} item(ns).")
        except Exception:
            conn.rollback()
            raise


def atualizar_status_ordem(os_id: str, status: str):
    ts = _ts()
    concluida_em = ts if status == "concluida" else None
    with _conn() as conn:
        with conn.cursor() as cur:
            if concluida_em:
                cur.execute(
                    "UPDATE ordens SET status=%s, concluida_em=%s WHERE os_id=%s",
                    (status, concluida_em, os_id),
                )
            else:
                cur.execute(
                    "UPDATE ordens SET status=%s WHERE os_id=%s",
                    (status, os_id),
                )


def atualizar_item_os(os_id: str, dispenser_id: int, quantidade_real: int, status: str):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE os_itens SET quantidade_real=%s, status=%s "
                "WHERE os_id=%s AND dispenser_id=%s",
                (quantidade_real, status, os_id, dispenser_id),
            )


def get_ordem_ativa() -> dict | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ordens "
                "WHERE status IN ('aguardando','em_andamento') "
                "ORDER BY criado_em DESC LIMIT 1"
            )
            ordem = _row(cur.fetchone())
            if not ordem:
                return None
            cur.execute(
                "SELECT * FROM os_itens WHERE os_id=%s ORDER BY dispenser_id",
                (ordem["os_id"],),
            )
            ordem["itens"] = _rows(cur.fetchall())
    return ordem


def get_historico_ordens(limite: int = 20) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT o.*, "
                "  (SELECT COUNT(*) FROM os_itens i WHERE i.os_id=o.os_id) AS total_itens "
                "FROM ordens o ORDER BY o.criado_em DESC LIMIT %s",
                (limite,),
            )
            return _rows(cur.fetchall())


# ── Dispensas ──────────────────────────────────────────────────────────────────

def salvar_dispensa(
    os_id: str, dispenser_id: int, medicamento: str,
    quantidade_dispensada: int, quantidade_alvo: int,
    validado: bool, motivo_falha: str = None,
):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dispensas "
                "(os_id, dispenser_id, medicamento, quantidade_dispensada, "
                " quantidade_alvo, validado, motivo_falha, ts) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (os_id, dispenser_id, medicamento, quantidade_dispensada,
                 quantidade_alvo, 1 if validado else 0, motivo_falha, _ts()),
            )


def get_dispensas(os_id: str, limite: int = 200) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM dispensas WHERE os_id=%s ORDER BY ts DESC LIMIT %s",
                (os_id, limite),
            )
            return _rows(cur.fetchall())


def get_dispensas_recentes(limite: int = 50) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM dispensas ORDER BY ts DESC LIMIT %s", (limite,)
            )
            return _rows(cur.fetchall())


# ── CNC ────────────────────────────────────────────────────────────────────────

def salvar_cnc_evento(
    os_id: str, status: str,
    dispenser_alvo: int = None, posicao_x: float = None, posicao_y: float = None,
    ciclo_atual: int = 0, total_ciclos: int = 0,
):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cnc_eventos "
                "(os_id, status, dispenser_alvo, posicao_x, posicao_y, "
                " ciclo_atual, total_ciclos, ts) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (os_id, status, dispenser_alvo, posicao_x, posicao_y,
                 ciclo_atual, total_ciclos, _ts()),
            )


def get_cnc_recentes(limite: int = 50) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cnc_eventos ORDER BY ts DESC LIMIT %s", (limite,)
            )
            return _rows(cur.fetchall())


# ── Sensores ───────────────────────────────────────────────────────────────────

def salvar_leitura_sensor(componente: str, tipo: str, valor: float, unidade: str):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO leituras_sensores (componente, tipo, valor, unidade, ts) "
                "VALUES (%s,%s,%s,%s,%s)",
                (componente, tipo, valor, unidade, _ts()),
            )


def get_ultimas_leituras() -> list:
    """Retorna a leitura mais recente de cada componente+tipo."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT l1.*
                FROM leituras_sensores l1
                INNER JOIN (
                    SELECT componente, tipo, MAX(ts) AS max_ts
                    FROM leituras_sensores
                    GROUP BY componente, tipo
                ) l2 ON l1.componente=l2.componente AND l1.tipo=l2.tipo AND l1.ts=l2.max_ts
                ORDER BY l1.componente, l1.tipo
            """)
            return _rows(cur.fetchall())


def get_historico_sensor(componente: str, tipo: str, limite: int = 60) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM leituras_sensores "
                "WHERE componente=%s AND tipo=%s ORDER BY ts DESC LIMIT %s",
                (componente, tipo, limite),
            )
            return _rows(cur.fetchall())


# ── Alarmes ────────────────────────────────────────────────────────────────────

def salvar_alarme(fonte: str, tipo: str, descricao: str) -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alarmes (fonte, tipo, descricao, ts) VALUES (%s,%s,%s,%s)",
                (fonte, tipo, descricao, _ts()),
            )
            return cur.lastrowid


def resolver_alarme(alarme_id: int):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE alarmes SET resolvido=1 WHERE id=%s", (alarme_id,)
            )


def get_alarmes(resolvido: bool = False, limite: int = 100) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM alarmes WHERE resolvido=%s ORDER BY ts DESC LIMIT %s",
                (1 if resolvido else 0, limite),
            )
            return _rows(cur.fetchall())


# ── Manutenção ─────────────────────────────────────────────────────────────────

def salvar_manutencao(tipo: str, componente: str, descricao: str, tecnico: str) -> dict:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO log_manutencao "
                "(tipo, componente, descricao, tecnico, ts) VALUES (%s,%s,%s,%s,%s)",
                (tipo, componente, descricao, tecnico, _ts()),
            )
    return {"ok": True}


def get_log_manutencao(limite: int = 100) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM log_manutencao ORDER BY ts DESC LIMIT %s", (limite,)
            )
            return _rows(cur.fetchall())


# ── Usuários ───────────────────────────────────────────────────────────────────

def get_usuario(username: str) -> dict | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM usuarios WHERE username=%s AND ativo=1", (username,)
            )
            return _row(cur.fetchone())
