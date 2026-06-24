-- ============================================================
-- APSEN – Sistema de Contagem de Medicamentos
-- Schema MySQL v2.0
-- ============================================================

CREATE DATABASE IF NOT EXISTS apsen_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
USE apsen_db;

-- ── Ordens de Saída (publicadas pela SAP via MQTT) ─────────────────────────
-- Cada OS especifica quais medicamentos devem ir para a caixa
CREATE TABLE IF NOT EXISTS ordens (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    os_id        VARCHAR(60)  NOT NULL UNIQUE,
    descricao    VARCHAR(200) NOT NULL DEFAULT '',
    status       VARCHAR(30)  NOT NULL DEFAULT 'aguardando',
    -- aguardando | em_andamento | concluida | erro
    payload_json TEXT         NOT NULL,  -- JSON original da OS
    criado_em    DATETIME(3)  NOT NULL,
    concluida_em DATETIME(3)  NULL,
    INDEX idx_os_status   (status),
    INDEX idx_os_criado   (criado_em)
) ENGINE=InnoDB;

-- ── Itens de cada OS (um por dispenser envolvido) ──────────────────────────
CREATE TABLE IF NOT EXISTS os_itens (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    os_id           VARCHAR(60) NOT NULL,
    dispenser_id    TINYINT     NOT NULL,   -- 1 a 6
    medicamento     VARCHAR(100) NOT NULL,
    quantidade_alvo INT          NOT NULL,
    quantidade_real INT          NOT NULL DEFAULT 0,
    status          VARCHAR(30)  NOT NULL DEFAULT 'pendente',
    -- pendente | em_andamento | concluido | erro
    INDEX idx_ositem_os (os_id, dispenser_id)
) ENGINE=InnoDB;

-- ── Eventos de dispensa (cada remédio empurrado para a caixa) ──────────────
CREATE TABLE IF NOT EXISTS dispensas (
    id                   INT AUTO_INCREMENT PRIMARY KEY,
    os_id                VARCHAR(60)  NOT NULL,
    dispenser_id         TINYINT      NOT NULL,
    medicamento          VARCHAR(100) NOT NULL,
    quantidade_dispensada INT         NOT NULL,  -- acumulado no momento
    quantidade_alvo      INT          NOT NULL,
    validado             TINYINT(1)   NOT NULL DEFAULT 1,  -- 1=ok, 0=falha IA
    motivo_falha         TEXT         NULL,
    ts                   DATETIME(3)  NOT NULL,
    INDEX idx_disp_os  (os_id, dispenser_id, ts),
    INDEX idx_disp_ts  (ts)
) ENGINE=InnoDB;

-- ── Eventos da CNC (movimentos e posições) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS cnc_eventos (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    os_id          VARCHAR(60)  NULL,
    status         VARCHAR(30)  NOT NULL,
    -- idle | movendo | posicionado | aguardando | concluido | erro
    dispenser_alvo TINYINT      NULL,
    posicao_x      DECIMAL(8,3) NULL,
    posicao_y      DECIMAL(8,3) NULL,
    ciclo_atual    INT          NOT NULL DEFAULT 0,
    total_ciclos   INT          NOT NULL DEFAULT 0,
    ts             DATETIME(3)  NOT NULL,
    INDEX idx_cnc_ts    (ts),
    INDEX idx_cnc_os    (os_id, ts)
) ENGINE=InnoDB;

-- ── Leituras de sensores (temperatura, desgaste, horas) ───────────────────
CREATE TABLE IF NOT EXISTS leituras_sensores (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    componente VARCHAR(100) NOT NULL,
    -- ex: motor_eixo_x, driver_y, placa_cnc, sensor_visao_3, motor_dispenser_2
    tipo       VARCHAR(30)  NOT NULL,
    -- temperatura | desgaste | horas_uso | ciclos
    valor      DECIMAL(10,3) NOT NULL,
    unidade    VARCHAR(20)  NOT NULL,
    -- °C | % | h | ciclos
    ts         DATETIME(3)  NOT NULL,
    INDEX idx_sensor_comp (componente, ts),
    INDEX idx_sensor_ts   (ts)
) ENGINE=InnoDB;

-- ── Alarmes e falhas (CNC, dispensers, sistema) ────────────────────────────
CREATE TABLE IF NOT EXISTS alarmes (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    fonte      VARCHAR(50)  NOT NULL,
    -- cnc | dispenser_1..6 | sistema
    tipo       VARCHAR(60)  NOT NULL,
    descricao  TEXT         NOT NULL,
    resolvido  TINYINT(1)   NOT NULL DEFAULT 0,
    ts         DATETIME(3)  NOT NULL,
    INDEX idx_alarm_resolvido (resolvido, ts),
    INDEX idx_alarm_ts        (ts)
) ENGINE=InnoDB;

-- ── Log de manutenção (registrado pelo técnico via IHM) ───────────────────
CREATE TABLE IF NOT EXISTS log_manutencao (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    tipo       VARCHAR(50)  NOT NULL,
    -- preventiva | corretiva | preditiva | inspecao
    componente VARCHAR(100) NOT NULL,
    descricao  TEXT         NOT NULL,
    tecnico    VARCHAR(100) NOT NULL,
    ts         DATETIME(3)  NOT NULL,
    INDEX idx_manut_comp (componente, ts),
    INDEX idx_manut_ts   (ts)
) ENGINE=InnoDB;

-- ── Usuários (somente IHM de manutenção) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS usuarios (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(100) NOT NULL UNIQUE,
    senha_hash    TEXT         NOT NULL,
    nome_completo VARCHAR(200) NOT NULL DEFAULT '',
    ativo         TINYINT(1)   NOT NULL DEFAULT 1,
    criado_em     DATETIME(3)  NOT NULL,
    INDEX idx_user (username, ativo)
) ENGINE=InnoDB;
