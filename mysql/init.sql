-- APSEN - Schema MySQL
-- Executado automaticamente pelo container mysql na primeira inicialização.
-- Conectar via MySQL Workbench: host=localhost, porta=3306, user=apsen, senha=apsen_pass_2024

CREATE DATABASE IF NOT EXISTS apsen_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE apsen_db;

-- ── Contagens da máquina ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS contagens (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    lote_id    VARCHAR(100) NOT NULL,
    valor      INT NOT NULL,
    velocidade DECIMAL(10,2) DEFAULT 0,
    ts         DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_cont_lote (lote_id, ts)
) ENGINE=InnoDB;

-- ── Eventos MQTT (status, alarmes, lote_concluido) ─────────────────────────
CREATE TABLE IF NOT EXISTS eventos (
    id      INT AUTO_INCREMENT PRIMARY KEY,
    lote_id VARCHAR(100) NOT NULL,
    tipo    VARCHAR(50)  NOT NULL,
    detalhe TEXT,
    ts      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_evt_lote (lote_id, ts)
) ENGINE=InnoDB;

-- ── Usuários ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS usuarios (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(100) NOT NULL UNIQUE,
    senha_hash    TEXT NOT NULL,
    role          VARCHAR(50)  NOT NULL DEFAULT 'operador',
    nome_completo VARCHAR(200) NOT NULL DEFAULT '',
    ativo         TINYINT(1)   NOT NULL DEFAULT 1,
    criado_em     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_user_ativo (username, ativo)
) ENGINE=InnoDB;

-- ── Ordens de Serviço ───────────────────────────────────────────────────────
-- os_id é gerado no Python após o AUTO_INCREMENT do id, portanto
-- inicialmente NULL e atualizado imediatamente em transação.
CREATE TABLE IF NOT EXISTS ordens_servico (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    os_id         VARCHAR(20)  UNIQUE DEFAULT NULL,
    produto       VARCHAR(200) NOT NULL,
    lote_id       VARCHAR(100) NOT NULL,
    meta          INT NOT NULL,
    status        VARCHAR(50)  NOT NULL DEFAULT 'aberto',
    responsavel   VARCHAR(200) NOT NULL DEFAULT '',
    criado_por    VARCHAR(100) NOT NULL,
    criado_em     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    atualizado_em DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_os_lote   (lote_id),
    INDEX idx_os_status (status)
) ENGINE=InnoDB;

-- ── Ocorrências por OS ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ocorrencias_os (
    id        INT AUTO_INCREMENT PRIMARY KEY,
    os_id     VARCHAR(20)  NOT NULL,
    tipo      VARCHAR(100) NOT NULL,
    descricao TEXT NOT NULL,
    usuario   VARCHAR(100) NOT NULL DEFAULT '',
    contagem  INT DEFAULT NULL,
    ts        DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_ocorr_os (os_id, ts)
) ENGINE=InnoDB;

-- ── Log de manutenção ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS log_manutencao (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    tipo        VARCHAR(50)  NOT NULL,
    descricao   TEXT NOT NULL,
    responsavel VARCHAR(100) NOT NULL,
    componente  VARCHAR(100) DEFAULT '',
    ts          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_manut_ts (ts)
) ENGINE=InnoDB;
