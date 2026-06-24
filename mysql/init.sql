-- ============================================================
-- APSEN – Sistema de Contagem de Medicamentos
-- Schema MySQL v2.1
-- ============================================================

CREATE DATABASE IF NOT EXISTS apsen_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
USE apsen_db;

-- ── Ordens de Saída (publicadas pela SAP via MQTT) ─────────────────────────
CREATE TABLE IF NOT EXISTS ordens (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    os_id        VARCHAR(60)  NOT NULL UNIQUE,
    descricao    VARCHAR(200) NOT NULL DEFAULT '',
    categoria    VARCHAR(100) NOT NULL DEFAULT '',
    status       VARCHAR(30)  NOT NULL DEFAULT 'aguardando',
    -- aguardando | em_andamento | concluida | erro | cancelada
    payload_json TEXT         NOT NULL,
    criado_em    DATETIME(3)  NOT NULL,
    concluida_em DATETIME(3)  NULL,
    INDEX idx_os_status  (status),
    INDEX idx_os_criado  (criado_em)
) ENGINE=InnoDB;

-- ── Itens de cada OS (um por dispenser envolvido) ──────────────────────────
CREATE TABLE IF NOT EXISTS os_itens (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    os_id           VARCHAR(60)  NOT NULL,
    dispenser_id    TINYINT      NOT NULL,
    medicamento     VARCHAR(100) NOT NULL,
    quantidade_alvo INT          NOT NULL,
    quantidade_real INT          NOT NULL DEFAULT 0,
    status          VARCHAR(30)  NOT NULL DEFAULT 'pendente',
    INDEX idx_ositem_os (os_id, dispenser_id)
) ENGINE=InnoDB;

-- ── Eventos de dispensa ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dispensas (
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
) ENGINE=InnoDB;

-- ── Eventos da CNC ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cnc_eventos (
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
) ENGINE=InnoDB;

-- ── Leituras de sensores ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leituras_sensores (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    componente VARCHAR(100)  NOT NULL,
    tipo       VARCHAR(30)   NOT NULL,
    valor      DECIMAL(10,3) NOT NULL,
    unidade    VARCHAR(20)   NOT NULL,
    ts         DATETIME(3)   NOT NULL,
    INDEX idx_sensor_comp (componente, ts),
    INDEX idx_sensor_ts   (ts)
) ENGINE=InnoDB;

-- ── Alarmes ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alarmes (
    id        INT AUTO_INCREMENT PRIMARY KEY,
    fonte     VARCHAR(50)  NOT NULL,
    tipo      VARCHAR(60)  NOT NULL,
    descricao TEXT         NOT NULL,
    resolvido TINYINT(1)   NOT NULL DEFAULT 0,
    ts        DATETIME(3)  NOT NULL,
    INDEX idx_alarm_resolvido (resolvido, ts),
    INDEX idx_alarm_ts        (ts)
) ENGINE=InnoDB;

-- ── Log de manutenção ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS log_manutencao (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    tipo       VARCHAR(50)  NOT NULL,
    componente VARCHAR(100) NOT NULL,
    descricao  TEXT         NOT NULL,
    tecnico    VARCHAR(100) NOT NULL,
    ts         DATETIME(3)  NOT NULL,
    INDEX idx_manut_ts (ts)
) ENGINE=InnoDB;

-- ── Estado dos dispensers (persistência entre OS) ──────────────────────────
-- Rastreia o medicamento atual e quantidade residual de cada dispenser.
-- Atualizado pelo dispenser simulator e backend.
CREATE TABLE IF NOT EXISTS dispenser_estado (
    dispenser_id      TINYINT      PRIMARY KEY,
    medicamento       VARCHAR(100) NULL,
    categoria         VARCHAR(100) NULL,
    quantidade_atual  INT          NOT NULL DEFAULT 0,
    capacidade        INT          NOT NULL DEFAULT 100,
    ultima_os_id      VARCHAR(60)  NULL,
    atualizado_em     DATETIME(3)  NOT NULL
) ENGINE=InnoDB;

-- ── Usuários (IHM de manutenção) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS usuarios (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(100) NOT NULL UNIQUE,
    senha_hash    TEXT         NOT NULL,
    nome_completo VARCHAR(200) NOT NULL DEFAULT '',
    role          VARCHAR(30)  NOT NULL DEFAULT 'manutencao',
    -- admin | manutencao
    ativo         TINYINT(1)   NOT NULL DEFAULT 1,
    criado_em     DATETIME(3)  NOT NULL,
    INDEX idx_user (username, ativo)
) ENGINE=InnoDB;

-- ── Seed: 6 slots de dispenser — inicialmente VAZIOS ────────────────────────
-- Nenhum slot é fixo para um medicamento específico.
-- O sistema de dispensers (dispenser_simulator) atribui medicamentos
-- dinamicamente conforme as Ordens de Saída chegam do SAP.
INSERT IGNORE INTO dispenser_estado (dispenser_id, medicamento, categoria, quantidade_atual, capacidade, atualizado_em)
VALUES
  (1, NULL, NULL, 0, 100, NOW(3)),
  (2, NULL, NULL, 0, 100, NOW(3)),
  (3, NULL, NULL, 0, 100, NOW(3)),
  (4, NULL, NULL, 0, 100, NOW(3)),
  (5, NULL, NULL, 0, 100, NOW(3)),
  (6, NULL, NULL, 0, 100, NOW(3));
