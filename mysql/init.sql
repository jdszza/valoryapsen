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

-- ── Itens de cada OS (um por medicamento) ───────────────────────────────────
-- dispenser_id é NULL até o sistema de dispensers fazer o roteamento dinâmico
CREATE TABLE IF NOT EXISTS os_itens (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    os_id           VARCHAR(60)  NOT NULL,
    dispenser_id    TINYINT      NULL,     -- atribuído dinamicamente
    medicamento     VARCHAR(100) NOT NULL,
    sku             VARCHAR(150) NULL,
    categoria       VARCHAR(100) NULL,
    quantidade_alvo INT          NOT NULL,
    quantidade_real INT          NOT NULL DEFAULT 0,
    status          VARCHAR(30)  NOT NULL DEFAULT 'pendente',
    INDEX idx_ositem_os  (os_id),
    INDEX idx_ositem_dis (os_id, dispenser_id)
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

-- ── Catálogo de medicamentos APSEN (96 itens, 12 categorias terapêuticas) ─────
-- Fonte: Desafio FIAP - Dimensões e Base FINAL.xlsx / Planilha1
CREATE TABLE IF NOT EXISTS medicamentos (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    nome             VARCHAR(150) NOT NULL UNIQUE,
    sku              VARCHAR(200) NOT NULL,
    categoria        VARCHAR(100) NOT NULL,   -- slug curto (snc, cardiologia, etc.)
    categoria_desc   VARCHAR(200) NOT NULL,   -- descrição completa
    dimensao         VARCHAR(60)  NULL,
    peso_unitario_g  DECIMAL(8,2) NULL,       -- peso unitário da embalagem (gramas)
    INDEX idx_med_categoria (categoria)
) ENGINE=InnoDB;

-- Adiciona coluna em DBs já existentes (idempotente)
ALTER TABLE medicamentos ADD COLUMN IF NOT EXISTS peso_unitario_g DECIMAL(8,2) NULL;

-- ── Histórico de leituras de visão computacional ──────────────────────────────
CREATE TABLE IF NOT EXISTS visao_leituras (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    os_id           VARCHAR(60)  NULL,
    camera          VARCHAR(20)  NOT NULL,  -- "dispenser" | "mesa"
    slot_id         TINYINT      NULL,
    tipo            VARCHAR(50)  NOT NULL,  -- leitura_dispenser_ok, leitura_mesa_divergencia, etc.
    sku_esperado    VARCHAR(200) NULL,
    sku_lido        VARCHAR(200) NULL,
    match_sku       TINYINT(1)   NULL,
    confianca       DECIMAL(5,4) NULL,      -- 0.0 – 1.0
    qtd_esperada    INT          NULL,
    qtd_detectada   INT          NULL,
    motivo          VARCHAR(255) NULL,
    criado_em       DATETIME(3)  NOT NULL,
    INDEX idx_vl_os     (os_id),
    INDEX idx_vl_camera (camera),
    INDEX idx_vl_criado (criado_em)
) ENGINE=InnoDB;

INSERT IGNORE INTO medicamentos (nome, sku, categoria, categoria_desc, dimensao) VALUES
-- ── Neurologia / Psiquiatria / SNC (20 medicamentos) ─────────────────────────
('ALOIS 10MG',           'ALOIS 10MG CX C/7 CP',                      'snc',               'Neurologia / Psiquiatria / SNC',                              '72x25x115mm'),
('ALOIS 20MG',           'ALOIS 20MG C/10 CP',                         'snc',               'Neurologia / Psiquiatria / SNC',                              '72x25x115mm'),
('ALOIS GOTAS',          'ALOIS GOTAS 10MG/ML 15ML',                   'snc',               'Neurologia / Psiquiatria / SNC',                              '47x36x75mm'),
('DONAREN 50MG',         'DONAREN 50MG CX C/5 CP',                     'snc',               'Neurologia / Psiquiatria / SNC',                              '49x20x104mm'),
('DONAREN RETARD 150MG', 'DONAREN RETARD 150MG CX C/5',                'snc',               'Neurologia / Psiquiatria / SNC',                              '46x21x97mm'),
('INSIT 25MG',           'INSIT 25MG C/7 CAPS',                        'snc',               'Neurologia / Psiquiatria / SNC',                              '49x20x104mm'),
('INSIT 50MG',           'INSIT 50MG CX C/7 CAPS',                     'snc',               'Neurologia / Psiquiatria / SNC',                              '49x20x104mm'),
('INSIT 75MG',           'INSIT 75MG CX C/7 CAPS',                     'snc',               'Neurologia / Psiquiatria / SNC',                              '49x20x104mm'),
('INSIT 100MG',          'INSIT 100MG CX C/7 CAPS',                    'snc',               'Neurologia / Psiquiatria / SNC',                              '49x20x104mm'),
('INSIT SOLUCAO ORAL',   'INSIT SOLUCAO ORAL 25MG/ML 18ML',            'snc',               'Neurologia / Psiquiatria / SNC',                              '72x42x115mm'),
('INSERIS XR 150MG',     'INSERIS XR 150MG CX C/10 CP',                'snc',               'Neurologia / Psiquiatria / SNC',                              '53.5x25.5x110mm'),
('INSERIS XR 300MG',     'INSERIS XR 300MG CX C/10 CP',                'snc',               'Neurologia / Psiquiatria / SNC',                              '53.5x25.5x110mm'),
('ATENTAH 10MG',         'ATENTAH 10MG CX C10 CAPS',                   'snc',               'Neurologia / Psiquiatria / SNC',                              '49x20x104mm'),
('ATENTAH 18MG',         'ATENTAH 18MG CX C10 CAPS',                   'snc',               'Neurologia / Psiquiatria / SNC',                              '49x20x104mm'),
('ATENTAH 25MG',         'ATENTAH 25MG CX C/10 CAPS',                  'snc',               'Neurologia / Psiquiatria / SNC',                              '49x20x104mm'),
('ATENTAH 40MG',         'ATENTAH 40MG CX C10 CAPS',                   'snc',               'Neurologia / Psiquiatria / SNC',                              '49x20x104mm'),
('LENIX 50MG',           'LENIX 50MG CX C/2 CP',                       'snc',               'Neurologia / Psiquiatria / SNC',                              '49x20x104mm'),
('PAXORAL 3,5MG',        'PAXORAL 3,5MG CX C/5 CAP',                   'snc',               'Neurologia / Psiquiatria / SNC',                              '50x21x105mm'),
('PAXORAL 7MG',          'PAXORAL 7MG CX C/5 CAP',                     'snc',               'Neurologia / Psiquiatria / SNC',                              '50x21x105mm'),
('COBI-12 1000MCG',      'COBI-12 1000MCG CX C/4 CP SUB',              'snc',               'Neurologia / Psiquiatria / SNC',                              '49x20x104mm'),
-- ── Otorrino / Labirintite / Vertigem (9 medicamentos) ───────────────────────
('LABIRIN 24MG',         'LABIRIN 24MG CX C/15 CP',                    'otorrino',          'Otorrino / Labirintite / Vertigem',                           '49x20x104mm'),
('LABIRIN XR 32MG',      'LABIRIN XR 32MG CX C/5 CP',                  'otorrino',          'Otorrino / Labirintite / Vertigem',                           '55x25x115mm'),
('LABIRIN XR 48MG',      'LABIRIN XR 48MG CX C/5 CP',                  'otorrino',          'Otorrino / Labirintite / Vertigem',                           '55x25x115mm'),
('MECLIN 25MG',          'MECLIN 25MG CX C/5 CP',                      'otorrino',          'Otorrino / Labirintite / Vertigem',                           '49x20x104mm'),
('MECLIN 50MG',          'MECLIN 50MG CX C/5 CP',                      'otorrino',          'Otorrino / Labirintite / Vertigem',                           '49x20x104mm'),
('MECLIN JET 25MG',      'MECLIN JET 25MG CX C/2 CP MAST',             'otorrino',          'Otorrino / Labirintite / Vertigem',                           '49x20x104mm'),
('MECLIN JET 50MG',      'MECLIN JET 50MG CX C/2 CP MAST',             'otorrino',          'Otorrino / Labirintite / Vertigem',                           '49x20x104mm'),
('MECLIN MOVE 25MG',     'MECLIN MOVE 25MG CX C/2 CP',                 'otorrino',          'Otorrino / Labirintite / Vertigem',                           '49x20x104mm'),
('VELUS',                'VELUS CX C/2 CP',                             'otorrino',          'Otorrino / Labirintite / Vertigem',                           '49x20x104mm'),
-- ── Urologia (9 medicamentos) ─────────────────────────────────────────────────
('RETEMIC 5MG',          'RETEMIC 5MG CX C/15 CP',                     'urologia',          'Urologia',                                                    '49x20x104mm'),
('RETEMIC UD 10MG',      'RETEMIC UD 10MG CX C/8 CP',                  'urologia',          'Urologia',                                                    '49x20x104mm'),
('UNOPROST 2MG',         'UNOPROST 2 MG CX C/15 CP',                   'urologia',          'Urologia',                                                    '49x20x104mm'),
('UNOPROST 4MG',         'UNOPROST 4MG CX C/15 CPR',                   'urologia',          'Urologia',                                                    '49x20x104mm'),
('TANDUO',               'TANDUO 0,4MG + 0,5MG CX C/4 CAPS',           'urologia',          'Urologia',                                                    '49x20x104mm'),
('SPASMEX 30MG',         'SPASMEX 30MG CX C/10 CP',                    'urologia',          'Urologia',                                                    '45x20x105mm'),
('TRATURIL',             'TRATURIL 5,631G/8G CX C/1 ENV',              'urologia',          'Urologia',                                                    '95x25x104mm'),
('URO VAXOM',            'URO VAXOM 6 MG CX C/5 CAP',                  'urologia',          'Urologia',                                                    '49x20x104mm'),
('LITOCIT 15MEQ',        'LITOCIT 15MEQ CX C/15CP',                    'urologia',          'Urologia',                                                    '56x54x110mm'),
-- ── Gastroenterologia (8 medicamentos) ───────────────────────────────────────
('DIGELIV 400 SACHES',   'DIGELIV 400 FCC GALU CX C/2 SACHES',        'gastroenterologia', 'Gastroenterologia',                                           '79x25x104mm'),
('DIGELIV 400 CP MAST',  'DIGELIV 400 GALU CX C/2 CP MAST',           'gastroenterologia', 'Gastroenterologia',                                           '49x20x104mm'),
('LONIUM 40MG',          'LONIUM 40MG CX C/5 CP',                      'gastroenterologia', 'Gastroenterologia',                                           '49x20x104mm'),
('INILOK 40MG',          'INILOK 40MG CX C/3 CP',                      'gastroenterologia', 'Gastroenterologia',                                           '49x20x104mm'),
('MAG B',                'MAG B C/2 CP',                                'gastroenterologia', 'Gastroenterologia',                                           '49x20x104mm'),
('MOTILEX',              'MOTILEX CX C/2 CAPS',                         'gastroenterologia', 'Gastroenterologia',                                           '49x20x104mm'),
('MOTILEX HA',           'MOTILEX HA C2 CAPS',                          'gastroenterologia', 'Gastroenterologia',                                           '49x20x104mm'),
('MOTILEX HA+MSM',       'MOTILEX HA+MSM C2 CAPS',                     'gastroenterologia', 'Gastroenterologia',                                           '49x20x104mm'),
-- ── Intolerância à lactose / Flora intestinal / Probióticos (9 medicamentos) ─
('LACTOSIL 4500 SACHES', 'LACTOSIL 4.500 FCC CX C/2 SACHES',          'lactose',           'Intolerância à Lactose / Flora Intestinal / Probióticos',     '84x25x150mm'),
('LACTOSIL 4500 COMP',   'LACTOSIL 4.500 FCC CX C/2 COMPRIMIDOS',     'lactose',           'Intolerância à Lactose / Flora Intestinal / Probióticos',     '49x20x104mm'),
('LACTOSIL 10000 SACHES','LACTOSIL 10.000 FCC CX C/2 SACHES',         'lactose',           'Intolerância à Lactose / Flora Intestinal / Probióticos',     '84x25x150mm'),
('LACTOSIL 10000 COMP',  'LACTOSIL 10.000 FCC CX C/2 COMPRIMIDOS',    'lactose',           'Intolerância à Lactose / Flora Intestinal / Probióticos',     '49x20x104mm'),
('LACTOSIL FLORA',       'LACTOSIL FLORA CX C/2 CAPS',                 'lactose',           'Intolerância à Lactose / Flora Intestinal / Probióticos',     '49x20x104mm'),
('PROBID',               'PROBID CX C/2 CAPS',                          'lactose',           'Intolerância à Lactose / Flora Intestinal / Probióticos',     '49x20x104mm'),
('PROBIANS',             'PROBIANS CX C/2 CAPS',                        'lactose',           'Intolerância à Lactose / Flora Intestinal / Probióticos',     '49x20x104mm'),
('FLORACOL',             'FLORACOL CX C/2 CAPS',                        'lactose',           'Intolerância à Lactose / Flora Intestinal / Probióticos',     '49x20x104mm'),
('MICROBIX',             'MICROBIX CX C/2 CAPS',                        'lactose',           'Intolerância à Lactose / Flora Intestinal / Probióticos',     '45x12x60mm'),
-- ── Reumatologia / Dor / Anti-inflamatórios (7 medicamentos) ─────────────────
('ARPADOL 400MG',        'ARPADOL 400MG CX C/5 CP',                    'reumatologia',      'Reumatologia / Dor / Anti-inflamatórios',                     '47x36x75mm'),
('FLANCOX 500MG',        'FLANCOX 500MG C/2 CP',                       'reumatologia',      'Reumatologia / Dor / Anti-inflamatórios',                     '49x20x104mm'),
('FLANCOX 600MG',        'FLANCOX 600MG CX C/2 CP',                    'reumatologia',      'Reumatologia / Dor / Anti-inflamatórios',                     '49x20x104mm'),
('COLCHIS 0,5MG',        'COLCHIS 0,5MG CX C/15 CP',                   'reumatologia',      'Reumatologia / Dor / Anti-inflamatórios',                     '47x36x75mm'),
('AZULFIN 500MG',        'AZULFIN 500MG CX C/15 CP',                   'reumatologia',      'Reumatologia / Dor / Anti-inflamatórios',                     '55x25x115mm'),
('REUQUINOL 400MG',      'REUQUINOL 400MG CX C/15 CP',                 'reumatologia',      'Reumatologia / Dor / Anti-inflamatórios',                     '55x25x115mm'),
('RAHIME 8MG',           'RAHIME 8MG CX C5 CP',                        'reumatologia',      'Reumatologia / Dor / Anti-inflamatórios',                     '49x20x104mm'),
-- ── Ortopedia / Muscular (8 medicamentos) ────────────────────────────────────
('MIOSAN 5MG',           'MIOSAN 5MG C/2 CP',                          'ortopedia',         'Ortopedia / Muscular',                                        '49x20x104mm'),
('MIOSAN CAF 5/30MG',    'MIOSAN CAF 5/30 MG CX C/2 CP',              'ortopedia',         'Ortopedia / Muscular',                                        '49x20x104mm'),
('MIOSAN CAF 10/60MG',   'MIOSAN CAF 10/60 MG CX C/2 CP',             'ortopedia',         'Ortopedia / Muscular',                                        '49x20x104mm'),
('MIOSAN ODT 5MG',       'MIOSAN ODT 5MG CX C/2 CP',                  'ortopedia',         'Ortopedia / Muscular',                                        '49x20x104mm'),
('MIOSAN ODT 10MG',      'MIOSAN ODT 10MG CX C/2 CP',                 'ortopedia',         'Ortopedia / Muscular',                                        '49x20x104mm'),
('MOTILEX HA ORTOP',     'MOTILEX HA C2 CAPS',                         'ortopedia',         'Ortopedia / Muscular',                                        '49x20x104mm'),
('MOTILEX HA+MSM ORTOP', 'MOTILEX HA+MSM C2 CAPS',                    'ortopedia',         'Ortopedia / Muscular',                                        '49x20x104mm'),
('ADEQUA 1000MG',        'ADEQUA 1000MG CX C/2 CAPS',                  'ortopedia',         'Ortopedia / Muscular',                                        '49x20x104mm'),
-- ── Cardiologia / Vascular (6 medicamentos) ───────────────────────────────────
('ZANIDIP 10MG',         'ZANIDIP 10MG C/5 CP',                        'cardiologia',       'Cardiologia / Vascular',                                      '49x20x104mm'),
('XAFAC 2,5MG',          'XAFAC 2,5MG CX C7 CP',                       'cardiologia',       'Cardiologia / Vascular',                                      '49x20x104mm'),
('XAFAC 10MG',           'XAFAC 10MG CX C5 CP',                        'cardiologia',       'Cardiologia / Vascular',                                      '49x20x104mm'),
('XAFAC 15MG',           'XAFAC 15MG CX C7 CP',                        'cardiologia',       'Cardiologia / Vascular',                                      '49x20x104mm'),
('XAFAC 20MG',           'XAFAC 20MG CX C7 CP',                        'cardiologia',       'Cardiologia / Vascular',                                      '49x20x104mm'),
('DOBEVEN 500MG',        'DOBEVEN 500MG CX C/10 CP',                   'cardiologia',       'Cardiologia / Vascular',                                      '49x20x104mm'),
-- ── Infectologia / Antibióticos (8 medicamentos) ─────────────────────────────
('LEVOXIN 500MG',        'LEVOXIN 500MG CX C/3 CP',                    'infectologia',      'Infectologia / Antibióticos',                                 '47x36x75mm'),
('LEVOXIN 750MG',        'LEVOXIN 750MG CX C/3 CP',                    'infectologia',      'Infectologia / Antibióticos',                                 '47x36x75mm'),
('LECZA XR 500MG',       'LECZA XR 500MG CX C/5 CP',                   'infectologia',      'Infectologia / Antibióticos',                                 '55x25x115mm'),
('LECZA XR 750MG',       'LECZA XR 750MG CX C/5 CP',                   'infectologia',      'Infectologia / Antibióticos',                                 '55x25x115mm'),
('SIL-HP 4MG',           'SIL-HP 4MG CX C/10 CAPS',                    'infectologia',      'Infectologia / Antibióticos',                                 '49x20x104mm'),
('SIL-HP 8MG',           'SIL-HP 8MG CX C/10 CAPS',                    'infectologia',      'Infectologia / Antibióticos',                                 '49x20x104mm'),
('DUEPOLI ER 250MG',     'DUEPOLI ER 250MG CX C/5 CP',                 'infectologia',      'Infectologia / Antibióticos',                                 '55x25x115mm'),
('DUEPOLI ER 500MG',     'DUEPOLI ER 500MG CX C/5 CP',                 'infectologia',      'Infectologia / Antibióticos',                                 '55x25x115mm'),
-- ── Alergia / Imunologia (2 medicamentos) ────────────────────────────────────
('LURATT 20MG',          'LURATT 20MG CX C/5 CP',                      'alergia',           'Alergia / Imunologia',                                        '49x20x104mm'),
('LURATT 40MG',          'LURATT 40MG CX C/5 CP',                      'alergia',           'Alergia / Imunologia',                                        '49x20x104mm'),
-- ── Dermatologia / Capilar (4 medicamentos) ───────────────────────────────────
('FITOSCAR',             'FITOSCAR 10G',                                'dermatologia',      'Dermatologia / Capilar',                                      '47x28x135mm'),
('POSTEC POMADA',        'POSTEC POMADA 5G',                            'dermatologia',      'Dermatologia / Capilar',                                      '47x28x135mm'),
('MOMENT CREME',         'MOMENT 0,025% CREME 25G',                    'dermatologia',      'Dermatologia / Capilar',                                      '47x30x155mm'),
('ENIAGOR SOLUCAO',      'ENIAGOR 50MG/ML SOLUCAO CAPILAR 25ML',       'dermatologia',      'Dermatologia / Capilar',                                      '95x25x104mm'),
-- ── Vitaminas / Nutrição / Suplementação (6 medicamentos) ────────────────────
('DESOL',                'DESOL 200UI/GT CX C/1 FR 2ML',               'vitaminas',         'Vitaminas / Nutrição / Suplementação',                        '47x36x75mm'),
('INPRUV DK 7000UI',     'INPRUV DK 7.000UI CX C/30 CAPS',            'vitaminas',         'Vitaminas / Nutrição / Suplementação',                        '55x25x115mm'),
('INPRUV DK 50000UI',    'INPRUV DK 50.000UI CX C/8 CAPS',            'vitaminas',         'Vitaminas / Nutrição / Suplementação',                        '55x25x115mm'),
('EXTIMA BAUNILHA',      'EXTIMA BAUNILHA 200ML',                       'vitaminas',         'Vitaminas / Nutrição / Suplementação',                        '72x42x115mm'),
('EXTIMA BANANA',        'EXTIMA BANANA 200ML',                         'vitaminas',         'Vitaminas / Nutrição / Suplementação',                        '72x42x115mm'),
('EXTIMA CHOCOLATE',     'EXTIMA CHOCOLATE 200ML',                      'vitaminas',         'Vitaminas / Nutrição / Suplementação',                        '72x42x115mm');

-- ── Pesos estimados por dimensão da embalagem ────────────────────────────────
-- Estimativas realistas baseadas nas dimensões físicas de cada embalagem APSEN.
UPDATE medicamentos SET peso_unitario_g =  15.00 WHERE dimensao = '45x12x60mm';
UPDATE medicamentos SET peso_unitario_g =  45.00 WHERE dimensao = '49x20x104mm';
UPDATE medicamentos SET peso_unitario_g =  50.00 WHERE dimensao = '50x21x105mm';
UPDATE medicamentos SET peso_unitario_g =  60.00 WHERE dimensao = '47x28x135mm';
UPDATE medicamentos SET peso_unitario_g =  70.00 WHERE dimensao = '47x30x155mm';
UPDATE medicamentos SET peso_unitario_g =  55.00 WHERE dimensao = '47x36x75mm';
UPDATE medicamentos SET peso_unitario_g =  60.00 WHERE dimensao = '55x25x115mm';
UPDATE medicamentos SET peso_unitario_g = 120.00 WHERE dimensao = '56x54x110mm';
UPDATE medicamentos SET peso_unitario_g =  75.00 WHERE dimensao = '72x25x115mm';
UPDATE medicamentos SET peso_unitario_g =  75.00 WHERE dimensao = '79x25x104mm';
UPDATE medicamentos SET peso_unitario_g =  95.00 WHERE dimensao = '84x25x150mm';
UPDATE medicamentos SET peso_unitario_g =  85.00 WHERE dimensao = '95x25x104mm';
UPDATE medicamentos SET peso_unitario_g = 100.00 WHERE dimensao = '72x42x115mm';
-- Fallback: embalagens sem dimensão definida recebem 50g
UPDATE medicamentos SET peso_unitario_g = 50.00 WHERE peso_unitario_g IS NULL;

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
