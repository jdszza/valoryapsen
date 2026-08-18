"""
APSEN - Banco de Dados MySQL (PyMySQL)

Fonte de verdade ÚNICA do schema: ordens, dispensas, CNC, sensores, visão,
manutenção, alarmes, usuários, medicamentos, dispenser_estado. `mysql/init.sql`
não declara nada — ver CLAUDE.md, "Schema do banco".
"""
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pymysql
import pymysql.cursors

from auth import hash_senha
from config import settings

logger = logging.getLogger(__name__)


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


class BancoIndisponivel(RuntimeError):
    """MySQL não aceitou conexão dentro da janela de espera do startup."""


class ConfiguracaoInvalida(RuntimeError):
    """MySQL respondeu e recusou: credencial ou nome de banco errados."""


class SchemaInvalido(RuntimeError):
    """MySQL aceitou a conexão, mas o schema não é o que este módulo espera."""


_TENTATIVAS_CONEXAO      = 30
_ESPERA_ENTRE_TENTATIVAS = 2.0

# Apenas estes erros significam "ainda não dá para conectar" e merecem retry.
# 2002/2003/2006/2013 vêm do cliente (CR_*): socket recusado, servidor sumiu no
# meio do handshake. 1040/1053: servidor de pé, mas ainda subindo ou saturado.
_ERROS_TRANSITORIOS = {2002, 2003, 2006, 2013, 1040, 1053}

# O servidor respondeu e disse não. Esperar não conserta, e o problema não é o
# schema: separar os dois evita mandar quem lê o log caçar tabela errada.
_ERROS_CONFIGURACAO = {1044, 1045, 1049, 1698}

# Todo o resto é schema — 1054 coluna inexistente, 1146 tabela inexistente,
# 1064 sintaxe. PyMySQL entrega esses códigos como OperationalError também, e
# era isso que os escondia atrás de 30 retries de "MySQL não disponível".


def _codigo_erro(exc: Exception) -> int | None:
    codigo = exc.args[0] if exc.args else None
    return codigo if isinstance(codigo, int) else None


def init_db():
    ultimo: Exception | None = None
    for tentativa in range(1, _TENTATIVAS_CONEXAO + 1):
        try:
            with _conn() as conn:
                _create_tables(conn)
            _seed_usuarios()
            _seed_medicamentos()
            logger.info("MySQL conectado e schema verificado. Medicamentos OK.")
            return
        except pymysql.Error as exc:
            codigo = _codigo_erro(exc)
            if codigo in _ERROS_CONFIGURACAO:
                raise ConfiguracaoInvalida(
                    f"MySQL recusou a conexão: {exc}. O servidor está de pé e "
                    "respondendo, então esperar não resolve — confira MYSQL_DB, "
                    "MYSQL_USER e MYSQL_PASS do central-computer."
                ) from exc
            if codigo not in _ERROS_TRANSITORIOS:
                raise SchemaInvalido(
                    f"MySQL respondeu, mas a inicialização do schema falhou: {exc}. "
                    "Isto NÃO é indisponibilidade — o banco provavelmente está numa "
                    "versão anterior do schema. Confira _DDL_TABELAS e "
                    "_COLUNAS_EVOLUTIVAS em central-computer/database.py."
                ) from exc
            ultimo = exc
            logger.warning(
                f"MySQL não disponível ({tentativa}/{_TENTATIVAS_CONEXAO}): {exc}"
            )
            if tentativa < _TENTATIVAS_CONEXAO:
                time.sleep(_ESPERA_ENTRE_TENTATIVAS)

    raise BancoIndisponivel(
        f"MySQL não respondeu após {_TENTATIVAS_CONEXAO} tentativas "
        f"(~{_TENTATIVAS_CONEXAO * _ESPERA_ENTRE_TENTATIVAS:.0f}s). "
        f"Último erro: {ultimo}"
    ) from ultimo


# ── Schema ─────────────────────────────────────────────────────────────────────
# FONTE DE VERDADE ÚNICA do schema (ver CLAUDE.md). `mysql/init.sql` não declara
# mais tabela nenhuma: ele só roda na primeira criação do volume, enquanto isto
# roda em TODO startup do central — inclusive contra um MySQL pré-existente.

_DDL_TABELAS = [
    """CREATE TABLE IF NOT EXISTS ordens (
        id           INT AUTO_INCREMENT PRIMARY KEY,
        os_id        VARCHAR(60)  NOT NULL UNIQUE,
        descricao    VARCHAR(200) NOT NULL DEFAULT '',
        categoria    VARCHAR(100) NOT NULL DEFAULT '',
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
        dispenser_id    TINYINT      NULL,
        medicamento     VARCHAR(100) NOT NULL,
        sku             VARCHAR(150) NULL,
        categoria       VARCHAR(100) NULL,
        quantidade_alvo INT          NOT NULL,
        quantidade_real INT          NOT NULL DEFAULT 0,
        status          VARCHAR(30)  NOT NULL DEFAULT 'pendente',
        INDEX idx_ositem_os  (os_id),
        INDEX idx_ositem_dis (os_id, dispenser_id)
    ) ENGINE=InnoDB""",

    """CREATE TABLE IF NOT EXISTS dispensas (
        id                    INT AUTO_INCREMENT PRIMARY KEY,
        os_id                 VARCHAR(60)  NOT NULL,
        dispenser_id          TINYINT      NOT NULL,
        medicamento           VARCHAR(100) NOT NULL,
        quantidade_dispensada INT          NOT NULL,
        quantidade_alvo       INT          NOT NULL,
        validado              TINYINT(1)   NOT NULL DEFAULT 1,
        motivo_falha          TEXT         NULL,
        ts                    DATETIME(3)  NOT NULL,
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
        componente VARCHAR(100)  NOT NULL,
        tipo       VARCHAR(30)   NOT NULL,
        valor      DECIMAL(10,3) NOT NULL,
        unidade    VARCHAR(20)   NOT NULL,
        ts         DATETIME(3)   NOT NULL,
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

    """CREATE TABLE IF NOT EXISTS dispenser_estado (
        dispenser_id      TINYINT      PRIMARY KEY,
        medicamento       VARCHAR(100) NULL,
        categoria         VARCHAR(100) NULL,
        quantidade_atual  INT          NOT NULL DEFAULT 0,
        capacidade        INT          NOT NULL DEFAULT 100,
        ultima_os_id      VARCHAR(60)  NULL,
        atualizado_em     DATETIME(3)  NOT NULL
    ) ENGINE=InnoDB""",

    """CREATE TABLE IF NOT EXISTS usuarios (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        username      VARCHAR(100) NOT NULL UNIQUE,
        senha_hash    TEXT         NOT NULL,
        nome_completo VARCHAR(200) NOT NULL DEFAULT '',
        role          VARCHAR(30)  NOT NULL DEFAULT 'manutencao',
        ativo         TINYINT(1)   NOT NULL DEFAULT 1,
        criado_em     DATETIME(3)  NOT NULL,
        INDEX idx_user (username, ativo)
    ) ENGINE=InnoDB""",

    """CREATE TABLE IF NOT EXISTS medicamentos (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        nome            VARCHAR(150) NOT NULL UNIQUE,
        sku             VARCHAR(200) NOT NULL,
        categoria       VARCHAR(100) NOT NULL,
        categoria_desc  VARCHAR(200) NOT NULL,
        dimensao        VARCHAR(60)  NULL,
        peso_unitario_g DECIMAL(8,2) NULL,
        INDEX idx_med_categoria (categoria)
    ) ENGINE=InnoDB""",

    """CREATE TABLE IF NOT EXISTS visao_leituras (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        os_id         VARCHAR(60)  NULL,
        camera        VARCHAR(20)  NOT NULL,
        slot_id       TINYINT      NULL,
        tipo          VARCHAR(50)  NOT NULL,
        sku_esperado  VARCHAR(200) NULL,
        sku_lido      VARCHAR(200) NULL,
        match_sku     TINYINT(1)   NULL,
        confianca     DECIMAL(5,4) NULL,
        qtd_esperada  INT          NULL,
        qtd_detectada INT          NULL,
        motivo        VARCHAR(255) NULL,
        criado_em     DATETIME(3)  NOT NULL,
        INDEX idx_vl_os     (os_id),
        INDEX idx_vl_camera (camera),
        INDEX idx_vl_criado (criado_em)
    ) ENGINE=InnoDB""",
]


# Colunas acrescentadas depois que o schema já tinha rodado em algum lugar.
# `CREATE TABLE IF NOT EXISTS` não toca em tabela existente, então um banco de
# versão anterior nunca ganharia a coluna sozinho — daí o ALTER condicional.
_COLUNAS_EVOLUTIVAS: dict[str, dict[str, str]] = {
    "ordens": {
        "categoria": "VARCHAR(100) NOT NULL DEFAULT '' AFTER descricao",
    },
    "os_itens": {
        "sku":       "VARCHAR(150) NULL AFTER medicamento",
        "categoria": "VARCHAR(100) NULL AFTER sku",
    },
    "usuarios": {
        "role": "VARCHAR(30) NOT NULL DEFAULT 'manutencao' AFTER nome_completo",
    },
    "log_manutencao": {
        "tecnico": "VARCHAR(100) NOT NULL DEFAULT 'sistema' AFTER descricao",
    },
    "medicamentos": {
        "categoria_desc":  "VARCHAR(200) NOT NULL DEFAULT '' AFTER categoria",
        "peso_unitario_g": "DECIMAL(8,2) NULL AFTER dimensao",
    },
}

_SLOTS_DISPENSER = 6
_CAPACIDADE_SLOT = 100


def _colunas_existentes(cur) -> dict[str, dict[str, str]]:
    """Mapa `{tabela: {coluna: IS_NULLABLE}}` do banco conectado."""
    cur.execute(
        "SELECT TABLE_NAME AS tabela, COLUMN_NAME AS coluna, IS_NULLABLE AS nulavel "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s",
        (settings.MYSQL_DB,),
    )
    mapa: dict[str, dict[str, str]] = {}
    for linha in cur.fetchall():
        mapa.setdefault(linha["tabela"].lower(), {})[linha["coluna"].lower()] = linha["nulavel"]
    return mapa


def _aplicar_colunas_faltantes(cur) -> None:
    """Repara bancos de versões anteriores, comparando com o information_schema.

    Idempotente: consulta o que existe e só emite ALTER para o que falta — sem
    `try/except: pass` engolindo erro de verdade no caminho.
    """
    existentes = _colunas_existentes(cur)

    for tabela, colunas in _COLUNAS_EVOLUTIVAS.items():
        presentes = existentes.get(tabela, {})
        for coluna, definicao in colunas.items():
            if coluna in presentes:
                continue
            logger.warning(f"[DB] Coluna ausente: {tabela}.{coluna} — aplicando ALTER TABLE.")
            cur.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")

    # `dispenser_id` nasceu NOT NULL; o roteamento dinâmico só define o slot
    # depois que a OS entra em execução, então a coluna precisa aceitar NULL.
    if existentes.get("os_itens", {}).get("dispenser_id") == "NO":
        logger.warning("[DB] os_itens.dispenser_id ainda é NOT NULL — relaxando.")
        cur.execute("ALTER TABLE os_itens MODIFY COLUMN dispenser_id TINYINT NULL")


def _seed_dispenser_estado(cur) -> None:
    """Garante as 6 linhas de slot, todas VAZIAS.

    Nenhum slot é fixo para um medicamento: o orquestrador atribui conforme as
    OS chegam. Só as linhas precisam existir de antemão, porque
    `salvar_dispenser_estado` faz UPDATE, não upsert.
    """
    cur.execute("SELECT COUNT(*) AS n FROM dispenser_estado")
    if cur.fetchone()["n"] >= _SLOTS_DISPENSER:
        return
    ts = _ts()
    for slot in range(1, _SLOTS_DISPENSER + 1):
        cur.execute(
            "INSERT IGNORE INTO dispenser_estado "
            "(dispenser_id, medicamento, categoria, quantidade_atual, capacidade, atualizado_em) "
            "VALUES (%s,NULL,NULL,0,%s,%s)",
            (slot, _CAPACIDADE_SLOT, ts),
        )


def _create_tables(conn):
    with conn.cursor() as cur:
        for ddl in _DDL_TABELAS:
            cur.execute(ddl)
        _aplicar_colunas_faltantes(cur)
        _seed_dispenser_estado(cur)


_MEDICAMENTOS_SEED = [
    ("ALOIS 10MG","ALOIS 10MG CX C/7 CP","snc","Neurologia / Psiquiatria / SNC","72x25x115mm"),
    ("ALOIS 20MG","ALOIS 20MG C/10 CP","snc","Neurologia / Psiquiatria / SNC","72x25x115mm"),
    ("ALOIS GOTAS","ALOIS GOTAS 10MG/ML 15ML","snc","Neurologia / Psiquiatria / SNC","47x36x75mm"),
    ("DONAREN 50MG","DONAREN 50MG CX C/5 CP","snc","Neurologia / Psiquiatria / SNC","49x20x104mm"),
    ("DONAREN RETARD 150MG","DONAREN RETARD 150MG CX C/5","snc","Neurologia / Psiquiatria / SNC","46x21x97mm"),
    ("INSIT 25MG","INSIT 25MG C/7 CAPS","snc","Neurologia / Psiquiatria / SNC","49x20x104mm"),
    ("INSIT 50MG","INSIT 50MG CX C/7 CAPS","snc","Neurologia / Psiquiatria / SNC","49x20x104mm"),
    ("INSIT 75MG","INSIT 75MG CX C/7 CAPS","snc","Neurologia / Psiquiatria / SNC","49x20x104mm"),
    ("INSIT 100MG","INSIT 100MG CX C/7 CAPS","snc","Neurologia / Psiquiatria / SNC","49x20x104mm"),
    ("INSIT SOLUCAO ORAL","INSIT SOLUCAO ORAL 25MG/ML 18ML","snc","Neurologia / Psiquiatria / SNC","72x42x115mm"),
    ("INSERIS XR 150MG","INSERIS XR 150MG CX C/10 CP","snc","Neurologia / Psiquiatria / SNC","53.5x25.5x110mm"),
    ("INSERIS XR 300MG","INSERIS XR 300MG CX C/10 CP","snc","Neurologia / Psiquiatria / SNC","53.5x25.5x110mm"),
    ("ATENTAH 10MG","ATENTAH 10MG CX C10 CAPS","snc","Neurologia / Psiquiatria / SNC","49x20x104mm"),
    ("ATENTAH 18MG","ATENTAH 18MG CX C10 CAPS","snc","Neurologia / Psiquiatria / SNC","49x20x104mm"),
    ("ATENTAH 25MG","ATENTAH 25MG CX C/10 CAPS","snc","Neurologia / Psiquiatria / SNC","49x20x104mm"),
    ("ATENTAH 40MG","ATENTAH 40MG CX C10 CAPS","snc","Neurologia / Psiquiatria / SNC","49x20x104mm"),
    ("LENIX 50MG","LENIX 50MG CX C/2 CP","snc","Neurologia / Psiquiatria / SNC","49x20x104mm"),
    ("PAXORAL 3,5MG","PAXORAL 3,5MG CX C/5 CAP","snc","Neurologia / Psiquiatria / SNC","50x21x105mm"),
    ("PAXORAL 7MG","PAXORAL 7MG CX C/5 CAP","snc","Neurologia / Psiquiatria / SNC","50x21x105mm"),
    ("COBI-12 1000MCG","COBI-12 1000MCG CX C/4 CP SUB","snc","Neurologia / Psiquiatria / SNC","49x20x104mm"),
    ("LABIRIN 24MG","LABIRIN 24MG CX C/15 CP","otorrino","Otorrino / Labirintite / Vertigem","49x20x104mm"),
    ("LABIRIN XR 32MG","LABIRIN XR 32MG CX C/5 CP","otorrino","Otorrino / Labirintite / Vertigem","55x25x115mm"),
    ("LABIRIN XR 48MG","LABIRIN XR 48MG CX C/5 CP","otorrino","Otorrino / Labirintite / Vertigem","55x25x115mm"),
    ("MECLIN 25MG","MECLIN 25MG CX C/5 CP","otorrino","Otorrino / Labirintite / Vertigem","49x20x104mm"),
    ("MECLIN 50MG","MECLIN 50MG CX C/5 CP","otorrino","Otorrino / Labirintite / Vertigem","49x20x104mm"),
    ("MECLIN JET 25MG","MECLIN JET 25MG CX C/2 CP MAST","otorrino","Otorrino / Labirintite / Vertigem","49x20x104mm"),
    ("MECLIN JET 50MG","MECLIN JET 50MG CX C/2 CP MAST","otorrino","Otorrino / Labirintite / Vertigem","49x20x104mm"),
    ("MECLIN MOVE 25MG","MECLIN MOVE 25MG CX C/2 CP","otorrino","Otorrino / Labirintite / Vertigem","49x20x104mm"),
    ("VELUS","VELUS CX C/2 CP","otorrino","Otorrino / Labirintite / Vertigem","49x20x104mm"),
    ("RETEMIC 5MG","RETEMIC 5MG CX C/15 CP","urologia","Urologia","49x20x104mm"),
    ("RETEMIC UD 10MG","RETEMIC UD 10MG CX C/8 CP","urologia","Urologia","49x20x104mm"),
    ("UNOPROST 2MG","UNOPROST 2 MG CX C/15 CP","urologia","Urologia","49x20x104mm"),
    ("UNOPROST 4MG","UNOPROST 4MG CX C/15 CPR","urologia","Urologia","49x20x104mm"),
    ("TANDUO","TANDUO 0,4MG + 0,5MG CX C/4 CAPS","urologia","Urologia","49x20x104mm"),
    ("SPASMEX 30MG","SPASMEX 30MG CX C/10 CP","urologia","Urologia","45x20x105mm"),
    ("TRATURIL","TRATURIL 5,631G/8G CX C/1 ENV","urologia","Urologia","95x25x104mm"),
    ("URO VAXOM","URO VAXOM 6 MG CX C/5 CAP","urologia","Urologia","49x20x104mm"),
    ("LITOCIT 15MEQ","LITOCIT 15MEQ CX C/15CP","urologia","Urologia","56x54x110mm"),
    ("DIGELIV 400 SACHES","DIGELIV 400 FCC GALU CX C/2 SACHES","gastroenterologia","Gastroenterologia","79x25x104mm"),
    ("DIGELIV 400 CP MAST","DIGELIV 400 GALU CX C/2 CP MAST","gastroenterologia","Gastroenterologia","49x20x104mm"),
    ("LONIUM 40MG","LONIUM 40MG CX C/5 CP","gastroenterologia","Gastroenterologia","49x20x104mm"),
    ("INILOK 40MG","INILOK 40MG CX C/3 CP","gastroenterologia","Gastroenterologia","49x20x104mm"),
    ("MAG B","MAG B C/2 CP","gastroenterologia","Gastroenterologia","49x20x104mm"),
    ("MOTILEX","MOTILEX CX C/2 CAPS","gastroenterologia","Gastroenterologia","49x20x104mm"),
    ("MOTILEX HA","MOTILEX HA C2 CAPS","gastroenterologia","Gastroenterologia","49x20x104mm"),
    ("MOTILEX HA+MSM","MOTILEX HA+MSM C2 CAPS","gastroenterologia","Gastroenterologia","49x20x104mm"),
    ("LACTOSIL 4500 SACHES","LACTOSIL 4.500 FCC CX C/2 SACHES","lactose","Intolerancia a Lactose / Flora Intestinal / Probioticos","84x25x150mm"),
    ("LACTOSIL 4500 COMP","LACTOSIL 4.500 FCC CX C/2 COMPRIMIDOS","lactose","Intolerancia a Lactose / Flora Intestinal / Probioticos","49x20x104mm"),
    ("LACTOSIL 10000 SACHES","LACTOSIL 10.000 FCC CX C/2 SACHES","lactose","Intolerancia a Lactose / Flora Intestinal / Probioticos","84x25x150mm"),
    ("LACTOSIL 10000 COMP","LACTOSIL 10.000 FCC CX C/2 COMPRIMIDOS","lactose","Intolerancia a Lactose / Flora Intestinal / Probioticos","49x20x104mm"),
    ("LACTOSIL FLORA","LACTOSIL FLORA CX C/2 CAPS","lactose","Intolerancia a Lactose / Flora Intestinal / Probioticos","49x20x104mm"),
    ("PROBID","PROBID CX C/2 CAPS","lactose","Intolerancia a Lactose / Flora Intestinal / Probioticos","49x20x104mm"),
    ("PROBIANS","PROBIANS CX C/2 CAPS","lactose","Intolerancia a Lactose / Flora Intestinal / Probioticos","49x20x104mm"),
    ("FLORACOL","FLORACOL CX C/2 CAPS","lactose","Intolerancia a Lactose / Flora Intestinal / Probioticos","49x20x104mm"),
    ("MICROBIX","MICROBIX CX C/2 CAPS","lactose","Intolerancia a Lactose / Flora Intestinal / Probioticos","45x12x60mm"),
    ("ARPADOL 400MG","ARPADOL 400MG CX C/5 CP","reumatologia","Reumatologia / Dor / Anti-inflamatorios","47x36x75mm"),
    ("FLANCOX 500MG","FLANCOX 500MG C/2 CP","reumatologia","Reumatologia / Dor / Anti-inflamatorios","49x20x104mm"),
    ("FLANCOX 600MG","FLANCOX 600MG CX C/2 CP","reumatologia","Reumatologia / Dor / Anti-inflamatorios","49x20x104mm"),
    ("COLCHIS 0,5MG","COLCHIS 0,5MG CX C/15 CP","reumatologia","Reumatologia / Dor / Anti-inflamatorios","47x36x75mm"),
    ("AZULFIN 500MG","AZULFIN 500MG CX C/15 CP","reumatologia","Reumatologia / Dor / Anti-inflamatorios","55x25x115mm"),
    ("REUQUINOL 400MG","REUQUINOL 400MG CX C/15 CP","reumatologia","Reumatologia / Dor / Anti-inflamatorios","55x25x115mm"),
    ("RAHIME 8MG","RAHIME 8MG CX C5 CP","reumatologia","Reumatologia / Dor / Anti-inflamatorios","49x20x104mm"),
    ("MIOSAN 5MG","MIOSAN 5MG C/2 CP","ortopedia","Ortopedia / Muscular","49x20x104mm"),
    ("MIOSAN CAF 5/30MG","MIOSAN CAF 5/30 MG CX C/2 CP","ortopedia","Ortopedia / Muscular","49x20x104mm"),
    ("MIOSAN CAF 10/60MG","MIOSAN CAF 10/60 MG CX C/2 CP","ortopedia","Ortopedia / Muscular","49x20x104mm"),
    ("MIOSAN ODT 5MG","MIOSAN ODT 5MG CX C/2 CP","ortopedia","Ortopedia / Muscular","49x20x104mm"),
    ("MIOSAN ODT 10MG","MIOSAN ODT 10MG CX C/2 CP","ortopedia","Ortopedia / Muscular","49x20x104mm"),
    ("MOTILEX HA ORTOP","MOTILEX HA C2 CAPS ORTOP","ortopedia","Ortopedia / Muscular","49x20x104mm"),
    ("MOTILEX HA+MSM ORTOP","MOTILEX HA+MSM C2 CAPS ORTOP","ortopedia","Ortopedia / Muscular","49x20x104mm"),
    ("ADEQUA 1000MG","ADEQUA 1000MG CX C/2 CAPS","ortopedia","Ortopedia / Muscular","49x20x104mm"),
    ("ZANIDIP 10MG","ZANIDIP 10MG C/5 CP","cardiologia","Cardiologia / Vascular","49x20x104mm"),
    ("XAFAC 2,5MG","XAFAC 2,5MG CX C7 CP","cardiologia","Cardiologia / Vascular","49x20x104mm"),
    ("XAFAC 10MG","XAFAC 10MG CX C5 CP","cardiologia","Cardiologia / Vascular","49x20x104mm"),
    ("XAFAC 15MG","XAFAC 15MG CX C7 CP","cardiologia","Cardiologia / Vascular","49x20x104mm"),
    ("XAFAC 20MG","XAFAC 20MG CX C7 CP","cardiologia","Cardiologia / Vascular","49x20x104mm"),
    ("DOBEVEN 500MG","DOBEVEN 500MG CX C/10 CP","cardiologia","Cardiologia / Vascular","49x20x104mm"),
    ("LEVOXIN 500MG","LEVOXIN 500MG CX C/3 CP","infectologia","Infectologia / Antibioticos","47x36x75mm"),
    ("LEVOXIN 750MG","LEVOXIN 750MG CX C/3 CP","infectologia","Infectologia / Antibioticos","47x36x75mm"),
    ("LECZA XR 500MG","LECZA XR 500MG CX C/5 CP","infectologia","Infectologia / Antibioticos","55x25x115mm"),
    ("LECZA XR 750MG","LECZA XR 750MG CX C/5 CP","infectologia","Infectologia / Antibioticos","55x25x115mm"),
    ("SIL-HP 4MG","SIL-HP 4MG CX C/10 CAPS","infectologia","Infectologia / Antibioticos","49x20x104mm"),
    ("SIL-HP 8MG","SIL-HP 8MG CX C/10 CAPS","infectologia","Infectologia / Antibioticos","49x20x104mm"),
    ("DUEPOLI ER 250MG","DUEPOLI ER 250MG CX C/5 CP","infectologia","Infectologia / Antibioticos","55x25x115mm"),
    ("DUEPOLI ER 500MG","DUEPOLI ER 500MG CX C/5 CP","infectologia","Infectologia / Antibioticos","55x25x115mm"),
    ("LURATT 20MG","LURATT 20MG CX C/5 CP","alergia","Alergia / Imunologia","49x20x104mm"),
    ("LURATT 40MG","LURATT 40MG CX C/5 CP","alergia","Alergia / Imunologia","49x20x104mm"),
    ("FITOSCAR","FITOSCAR 10G","dermatologia","Dermatologia / Capilar","47x28x135mm"),
    ("POSTEC POMADA","POSTEC POMADA 5G","dermatologia","Dermatologia / Capilar","47x28x135mm"),
    ("MOMENT CREME","MOMENT 0,025% CREME 25G","dermatologia","Dermatologia / Capilar","47x30x155mm"),
    ("ENIAGOR SOLUCAO","ENIAGOR 50MG/ML SOLUCAO CAPILAR 25ML","dermatologia","Dermatologia / Capilar","95x25x104mm"),
    ("DESOL","DESOL 200UI/GT CX C/1 FR 2ML","vitaminas","Vitaminas / Nutricao / Suplementacao","47x36x75mm"),
    ("INPRUV DK 7000UI","INPRUV DK 7.000UI CX C/30 CAPS","vitaminas","Vitaminas / Nutricao / Suplementacao","55x25x115mm"),
    ("INPRUV DK 50000UI","INPRUV DK 50.000UI CX C/8 CAPS","vitaminas","Vitaminas / Nutricao / Suplementacao","55x25x115mm"),
    ("EXTIMA BAUNILHA","EXTIMA BAUNILHA 200ML","vitaminas","Vitaminas / Nutricao / Suplementacao","72x42x115mm"),
    ("EXTIMA BANANA","EXTIMA BANANA 200ML","vitaminas","Vitaminas / Nutricao / Suplementacao","72x42x115mm"),
    ("EXTIMA CHOCOLATE","EXTIMA CHOCOLATE 200ML","vitaminas","Vitaminas / Nutricao / Suplementacao","72x42x115mm"),
]


_PESO_POR_DIMENSAO: dict[str, float] = {
    "45x12x60mm":   15.0,
    "49x20x104mm":  45.0,
    "50x21x105mm":  50.0,
    "47x28x135mm":  60.0,
    "47x30x155mm":  70.0,
    "47x36x75mm":   55.0,
    "55x25x115mm":  60.0,
    "56x54x110mm": 120.0,
    "72x25x115mm":  75.0,
    "72x42x115mm": 100.0,
    "79x25x104mm":  75.0,
    "84x25x150mm":  95.0,
    "95x25x104mm":  85.0,
}
_PESO_PADRAO = 50.0


def _seed_medicamentos():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM medicamentos")
            count = cur.fetchone()["n"]

        if count < len(_MEDICAMENTOS_SEED):
            inserted = 0
            with conn.cursor() as cur:
                for nome, sku, cat, cat_desc, dim in _MEDICAMENTOS_SEED:
                    cur.execute(
                        "INSERT IGNORE INTO medicamentos "
                        "(nome, sku, categoria, categoria_desc, dimensao) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (nome, sku, cat, cat_desc, dim),
                    )
                    inserted += cur.rowcount
            if inserted:
                logger.info(f"[DB] Seed medicamentos: {inserted} inseridos.")

        # Preenche peso_unitario_g para registros sem peso (idempotente)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM medicamentos WHERE peso_unitario_g IS NULL")
            sem_peso = cur.fetchone()["n"]

        if sem_peso > 0:
            with conn.cursor() as cur:
                for dimensao, peso in _PESO_POR_DIMENSAO.items():
                    cur.execute(
                        "UPDATE medicamentos SET peso_unitario_g=%s "
                        "WHERE dimensao=%s AND peso_unitario_g IS NULL",
                        (peso, dimensao),
                    )
                cur.execute(
                    "UPDATE medicamentos SET peso_unitario_g=%s WHERE peso_unitario_g IS NULL",
                    (_PESO_PADRAO,),
                )
            logger.info(f"[DB] Pesos populados para {sem_peso} medicamento(s).")


def _seed_usuarios():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM usuarios")
            if cur.fetchone()["n"] > 0:
                return
        ts = _ts()
        seeds = [
            ("admin",  settings.SEED_ADMIN_SENHA, "Administrador",         "admin"),
            ("manut1", settings.SEED_MANUT_SENHA, "Tecnico de Manutencao", "manutencao"),
        ]
        with conn.cursor() as cur:
            for username, senha, nome, role in seeds:
                cur.execute(
                    "INSERT IGNORE INTO usuarios "
                    "(username, senha_hash, nome_completo, role, criado_em) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (username, hash_senha(senha), nome, role, ts),
                )
    logger.info("Usuarios seed inseridos.")


# ── Ordens ─────────────────────────────────────────────────────────────────────

def salvar_ordem(os_id: str, descricao: str, medicamentos: list, payload_raw: dict) -> bool:
    """Grava a OS e seus itens. Devolve False se a OS JÁ EXISTIA.

    `ordens.os_id` é UNIQUE e o INSERT IGNORE é a trava de idempotência: no
    reenvio da mesma OS o insert não afeta linha nenhuma (`rowcount == 0`) e a
    função sai sem tocar em `os_itens` — do contrário os itens duplicariam sob
    a ordem original.

    Quem chama PRECISA olhar o retorno. Enfileirar uma OS que não foi inserida
    é processá-la duas vezes; era o que `receber_ordem` fazia, respondendo
    `{"aceita": true}` para um reenvio. O contrato manda 409 (ANALISE
    ARQUITETURAL §4.1).
    """
    ts = _ts()
    categoria = payload_raw.get("categoria", "")
    with _conn(autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT IGNORE INTO ordens "
                    "(os_id, descricao, categoria, status, payload_json, criado_em) "
                    "VALUES (%s,%s,%s,'aguardando',%s,%s)",
                    (os_id, descricao, categoria, json.dumps(payload_raw, ensure_ascii=False), ts),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    logger.info(f"[DB] OS {os_id} já registrada — nada inserido.")
                    return False
                for item in medicamentos:
                    cur.execute(
                        "INSERT INTO os_itens "
                        "(os_id, dispenser_id, medicamento, sku, categoria, quantidade_alvo) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (
                            os_id,
                            item.get("dispenser_id"),
                            item.get("medicamento", ""),
                            item.get("sku"),
                            item.get("categoria"),
                            item.get("quantidade", 0),
                        ),
                    )
            conn.commit()
            logger.info(f"[DB] OS {os_id} salva com {len(medicamentos)} item(ns).")
            return True
        except Exception:
            conn.rollback()
            raise


def atribuir_dispenser_item(os_id: str, medicamento: str, dispenser_id: int):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE os_itens SET dispenser_id=%s "
                "WHERE os_id=%s AND medicamento=%s AND dispenser_id IS NULL LIMIT 1",
                (dispenser_id, os_id, medicamento),
            )


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
                cur.execute("UPDATE ordens SET status=%s WHERE os_id=%s", (status, os_id))


def atualizar_item_os(os_id: str, dispenser_id: int, quantidade_real: int, status: str):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE os_itens SET quantidade_real=%s, status=%s "
                "WHERE os_id=%s AND dispenser_id=%s",
                (quantidade_real, status, os_id, dispenser_id),
            )


def get_ordem_ativa() -> dict | None:
    """A OS em execução — e, se não houver nenhuma, a próxima da fila.

    Quem está rodando é dito pelo STATUS, não pela data: `_processar_os` grava
    'em_andamento' assim que tira a OS da fila. O fallback para 'aguardando'
    existe para a IHM não ficar em branco entre duas OS, e aí a "próxima" é a
    mais ANTIGA — a ordem em que o orquestrador vai puxá-las.

    Uma única query com `criado_em DESC` sobre os dois status — como era antes —
    devolvia, sempre que existisse fila, a OS enfileirada por ÚLTIMO: a que
    ainda nem começou, em vez da que está no meio da execução.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ordens WHERE status='em_andamento' "
                "ORDER BY criado_em ASC LIMIT 1"
            )
            ordem = _row(cur.fetchone())
            if ordem is None:
                cur.execute(
                    "SELECT * FROM ordens WHERE status='aguardando' "
                    "ORDER BY criado_em ASC LIMIT 1"
                )
                ordem = _row(cur.fetchone())
            if not ordem:
                return None
            cur.execute("SELECT * FROM os_itens WHERE os_id=%s ORDER BY dispenser_id", (ordem["os_id"],))
            ordem["itens"] = _rows(cur.fetchall())
    return ordem


def get_historico_ordens(limite: int = 50) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT o.*, (SELECT COUNT(*) FROM os_itens i WHERE i.os_id=o.os_id) AS total_itens "
                "FROM ordens o ORDER BY o.criado_em DESC LIMIT %s", (limite,))
            return _rows(cur.fetchall())


def get_ordem_por_id(os_id: str) -> dict | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ordens WHERE os_id=%s", (os_id,))
            ordem = _row(cur.fetchone())
            if not ordem:
                return None
            cur.execute("SELECT * FROM os_itens WHERE os_id=%s ORDER BY dispenser_id", (os_id,))
            ordem["itens"] = _rows(cur.fetchall())
    return ordem


# ── Dispensas ──────────────────────────────────────────────────────────────────

def salvar_dispensa(os_id, dispenser_id, medicamento, quantidade_dispensada,
                    quantidade_alvo, validado, motivo_falha=None):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dispensas (os_id, dispenser_id, medicamento, quantidade_dispensada, "
                "quantidade_alvo, validado, motivo_falha, ts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (os_id, dispenser_id, medicamento, quantidade_dispensada,
                 quantidade_alvo, 1 if validado else 0, motivo_falha, _ts()),
            )


def get_dispensas(os_id: str, limite: int = 200) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM dispensas WHERE os_id=%s ORDER BY ts DESC LIMIT %s", (os_id, limite))
            return _rows(cur.fetchall())


def get_dispensas_recentes(limite: int = 50) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM dispensas ORDER BY ts DESC LIMIT %s", (limite,))
            return _rows(cur.fetchall())


# ── CNC ────────────────────────────────────────────────────────────────────────

def salvar_cnc_evento(os_id, status, dispenser_alvo=None, posicao_x=None,
                      posicao_y=None, ciclo_atual=0, total_ciclos=0):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cnc_eventos (os_id, status, dispenser_alvo, posicao_x, posicao_y, "
                "ciclo_atual, total_ciclos, ts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (os_id, status, dispenser_alvo, posicao_x, posicao_y, ciclo_atual, total_ciclos, _ts()),
            )


def get_cnc_recentes(limite: int = 50) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM cnc_eventos ORDER BY ts DESC LIMIT %s", (limite,))
            return _rows(cur.fetchall())


# ── Sensores ───────────────────────────────────────────────────────────────────

def salvar_leitura_sensor(componente: str, tipo: str, valor: float, unidade: str):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO leituras_sensores (componente, tipo, valor, unidade, ts) VALUES (%s,%s,%s,%s,%s)",
                (componente, tipo, valor, unidade, _ts()),
            )


_LOTE_EXPURGO = 5000
# SQL literal por tabela, em vez de interpolar o nome: é o que mantém o
# `tests/test_schema.py` capaz de conferir tabela e coluna por AST.
_SQL_EXPURGO = {
    "cnc_eventos":
        f"DELETE FROM cnc_eventos WHERE ts < %s LIMIT {_LOTE_EXPURGO}",
    "leituras_sensores":
        f"DELETE FROM leituras_sensores WHERE ts < %s LIMIT {_LOTE_EXPURGO}",
}


def expurgar_dados_antigos(dias: int) -> dict:
    """Apaga histórico de alta cardinalidade mais velho que `dias`.

    `cnc_eventos` e `leituras_sensores` são as duas tabelas que crescem por
    tempo, não por operação: telemetria de 6 slots a cada 15s, da CNC a cada
    30s, de câmeras e balança a cada 60s. Sem expurgo, o volume só sobe — e o
    banco é um MySQL de container, sem DBA por perto.

    Apaga em lotes de `_LOTE_EXPURGO`: um DELETE único de milhões de linhas
    segura lock e enche o binlog. Devolve o total removido por tabela.
    """
    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    removidos = {}
    for tabela, sql in _SQL_EXPURGO.items():
        total = 0
        while True:
            with _conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (corte,))
                    apagadas = cur.rowcount
            total += apagadas
            if apagadas < _LOTE_EXPURGO:
                break
        removidos[tabela] = total
    if any(removidos.values()):
        logger.info("[DB] Expurgo (> %d dias): %s", dias, removidos)
    return removidos


def get_ultimas_leituras() -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT l1.* FROM leituras_sensores l1
                INNER JOIN (
                    SELECT componente, tipo, MAX(ts) AS max_ts
                    FROM leituras_sensores GROUP BY componente, tipo
                ) l2 ON l1.componente=l2.componente AND l1.tipo=l2.tipo AND l1.ts=l2.max_ts
                ORDER BY l1.componente, l1.tipo
            """)
            return _rows(cur.fetchall())


def get_historico_sensor(componente: str, tipo: str, limite: int = 60) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM leituras_sensores WHERE componente=%s AND tipo=%s ORDER BY ts DESC LIMIT %s",
                (componente, tipo, limite),
            )
            return _rows(cur.fetchall())


# ── Alarmes ────────────────────────────────────────────────────────────────────

def salvar_leitura_visao(
    os_id: str | None, camera: str, slot_id: int | None, tipo: str,
    sku_esperado: str | None = None, sku_lido: str | None = None,
    match_sku: bool | None = None, confianca: float | None = None,
    qtd_esperada: int | None = None, qtd_detectada: int | None = None,
    motivo: str | None = None,
) -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO visao_leituras "
                "(os_id, camera, slot_id, tipo, sku_esperado, sku_lido, match_sku, "
                " confianca, qtd_esperada, qtd_detectada, motivo, criado_em) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (os_id, camera, slot_id, tipo, sku_esperado, sku_lido,
                 1 if match_sku else (0 if match_sku is False else None),
                 confianca, qtd_esperada, qtd_detectada, motivo, _ts()),
            )
            return cur.lastrowid


def get_historico_visao(os_id: str = None, limite: int = 100) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            if os_id:
                cur.execute(
                    "SELECT * FROM visao_leituras WHERE os_id=%s ORDER BY criado_em DESC LIMIT %s",
                    (os_id, limite),
                )
            else:
                cur.execute(
                    "SELECT * FROM visao_leituras ORDER BY criado_em DESC LIMIT %s",
                    (limite,),
                )
            return _rows(cur.fetchall())


def salvar_alarme(fonte: str, tipo: str, descricao: str) -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO alarmes (fonte, tipo, descricao, ts) VALUES (%s,%s,%s,%s)",
                        (fonte, tipo, descricao, _ts()))
            return cur.lastrowid


def resolver_alarme(alarme_id: int):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE alarmes SET resolvido=1 WHERE id=%s", (alarme_id,))


def get_alarmes_por_os(os_id: str, limite: int = 200) -> list:
    """Retorna alarmes cuja descricao menciona a OS (para relatório)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM alarmes WHERE descricao LIKE %s ORDER BY ts ASC LIMIT %s",
                (f"%{os_id}%", limite),
            )
            return _rows(cur.fetchall())


def get_alarmes(resolvido: bool = False, limite: int = 100) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM alarmes WHERE resolvido=%s ORDER BY ts DESC LIMIT %s",
                        (1 if resolvido else 0, limite))
            return _rows(cur.fetchall())


def get_total_alarmes_ativos() -> int:
    """Quantos alarmes estão abertos — o número do badge do dashboard.

    COUNT em vez de `len(get_alarmes())`: o badge só precisa do total, e trazer
    as linhas ainda mentiria por causa do LIMIT (101 alarmes abertos virariam
    100). O índice `idx_alarm_resolvido` cobre a query inteira.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM alarmes WHERE resolvido=0")
            linha = cur.fetchone() or {}
            return int(linha.get("total") or 0)


# ── Manutencao ─────────────────────────────────────────────────────────────────

def salvar_manutencao(tipo: str, componente: str, descricao: str, tecnico: str) -> dict:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO log_manutencao (tipo, componente, descricao, tecnico, ts) VALUES (%s,%s,%s,%s,%s)",
                (tipo, componente, descricao, tecnico, _ts()),
            )
    return {"ok": True}


def get_log_manutencao(limite: int = 100) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM log_manutencao ORDER BY ts DESC LIMIT %s", (limite,))
            return _rows(cur.fetchall())


# ── Dispenser Estado ───────────────────────────────────────────────────────────

def get_dispensers_estado() -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM dispenser_estado ORDER BY dispenser_id")
            return _rows(cur.fetchall())


def get_dispenser_estado(dispenser_id: int) -> dict | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM dispenser_estado WHERE dispenser_id=%s", (dispenser_id,))
            return _row(cur.fetchone())


def salvar_dispenser_estado(dispenser_id, quantidade_atual, os_id=None, medicamento=None, categoria=None):
    ts = _ts()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dispenser_estado SET quantidade_atual=%s, ultima_os_id=%s, "
                "medicamento=%s, categoria=%s, atualizado_em=%s WHERE dispenser_id=%s",
                (quantidade_atual, os_id, medicamento, categoria, ts, dispenser_id),
            )


def limpar_dispenser_estado(dispenser_id: int):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dispenser_estado SET quantidade_atual=0, ultima_os_id=NULL, "
                "medicamento=NULL, categoria=NULL, atualizado_em=%s WHERE dispenser_id=%s",
                (_ts(), dispenser_id),
            )


# ── Usuarios ───────────────────────────────────────────────────────────────────

def get_usuario(username: str) -> dict | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM usuarios WHERE username=%s AND ativo=1", (username,))
            return _row(cur.fetchone())


def get_usuarios() -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, nome_completo, role, ativo, criado_em FROM usuarios ORDER BY criado_em DESC")
            return _rows(cur.fetchall())


def criar_usuario(username: str, senha: str, nome_completo: str, role: str = "manutencao") -> dict:
    with _conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO usuarios (username, senha_hash, nome_completo, role, criado_em) VALUES (%s,%s,%s,%s,%s)",
                    (username, hash_senha(senha), nome_completo, role, _ts()),
                )
                return {"ok": True, "id": cur.lastrowid}
            except pymysql.IntegrityError:
                return {"ok": False, "erro": "username ja existe"}


def atualizar_usuario(username: str, nome_completo: str = None, role: str = None, nova_senha: str = None) -> dict:
    updates, vals = [], []
    if nome_completo is not None:
        updates.append("nome_completo=%s"); vals.append(nome_completo)
    if role is not None:
        updates.append("role=%s"); vals.append(role)
    if nova_senha is not None:
        updates.append("senha_hash=%s"); vals.append(hash_senha(nova_senha))
    if not updates:
        return {"ok": False, "erro": "nada para atualizar"}
    vals.append(username)
    with _conn() as conn:
        with conn.cursor() as cur:
            updates_str = ', '.join(updates)
            cur.execute(f"UPDATE usuarios SET {updates_str} WHERE username=%s", vals)
            return {"ok": True, "afetados": cur.rowcount}


def toggle_usuario_ativo(username: str, ativo: bool) -> dict:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE usuarios SET ativo=%s WHERE username=%s", (1 if ativo else 0, username))
            return {"ok": True, "ativo": ativo}


# ── Medicamentos ───────────────────────────────────────────────────────────────

def listar_medicamentos(categoria: str = None) -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            if categoria:
                cur.execute("SELECT * FROM medicamentos WHERE categoria=%s ORDER BY nome", (categoria,))
            else:
                cur.execute("SELECT * FROM medicamentos ORDER BY categoria, nome")
            return _rows(cur.fetchall())


def get_peso_medicamento(nome: str) -> float:
    """Retorna peso_unitario_g do medicamento. Padrão: 50.0g se não encontrado."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT peso_unitario_g FROM medicamentos WHERE nome=%s LIMIT 1", (nome,)
            )
            row = cur.fetchone()
    if row and row.get("peso_unitario_g") is not None:
        return float(row["peso_unitario_g"])
    return 50.0  # fallback padrão


def listar_categorias() -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT categoria, categoria_desc, COUNT(*) AS total "
                "FROM medicamentos GROUP BY categoria, categoria_desc ORDER BY categoria_desc"
            )
            return _rows(cur.fetchall())
