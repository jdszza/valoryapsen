# APSEN – Sistema de Contagem de Medicamentos

> **Projeto de Engenharia Mecatrônica – 3º Ano**  
> Valory · APSEN Farmacêutica

---

## Visão Geral

Sistema automatizado de contagem e validação de medicamentos. Uma mesa CNC posiciona-se sobre 6 dispensers para coletar os medicamentos de cada Ordem de Saída (OS). Duas câmeras de visão computacional validam o produto carregado (câmera dos dispensers) e a contagem na mesa de coleta (câmera da mesa). A comunicação é 100% REST/HTTP e WebSocket — **sem MQTT**. MySQL para persistência, Plotly Dash para interface.

---

## Arquitetura (v3.1 — REST/HTTP)

```
Order Generator (gera OS aleatórias por categoria)
  │ POST /api/v1/ordens
  ▼
┌──────────────────────────────────────────────────────────────┐
│                   Central Computer  :8000                    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  Orquestrador (orchestrator.py)                     │    │
│  │  • Fila de OS                                       │    │
│  │  • Atribuição de slots por categoria/residual       │    │
│  │  • Rota CNC nearest-neighbor                        │    │
│  │  • Coordena: carga → visão dispenser → CNC → dispensa → visão mesa │
│  └─────────────────────────────────────────────────────┘    │
│  REST + MySQL + WebSocket /ws                                │
└──────┬────────────────┬────────────────┬────────────────────┘
       │                │                │
  POST comandos    POST comandos    POST comandos
       │                │                │
┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
│  Dispenser  │  │    CNC      │  │   Vision    │
│  Adapter    │  │   Adapter   │  │   Adapter   │
│   :8100     │  │    :8101    │  │    :8102    │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │
┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
│  Dispenser  │  │    CNC      │  │   Vision    │
│  Simulator  │  │  Simulator  │  │  Simulator  │
│   :8201     │  │    :8200    │  │    :8202    │
│ 6 slots     │  │ firmware    │  │ cam disp +  │
│ físicos     │  │ CNC         │  │ cam mesa    │
└─────────────┘  └─────────────┘  └─────────────┘

Dashboard  :8050  ←─ GET /estado (polling) ──── Central Computer
IHM Web    :8051  ←─ JWT + REST + WS ────────── Central Computer
Displays          ←─ WebSocket /ws ──────────── Central Computer
MySQL      :3306  ←─ pymysql (sync) ─────────── Central Computer
```

### Displays dos dispensers

Os displays físicos de cada dispenser são **consumidores passivos** do WebSocket do Central Computer. Não possuem lógica de negócio, não enviam eventos e não participam do fluxo operacional. Simplesmente exibem o produto associado ao slot.

O Central emite o evento abaixo sempre que um produto é atribuído a um dispenser:

```json
{
  "event": "produto_alterado",
  "dispenser": 5,
  "produto": "Dipirona 500mg"
}
```

---

### Serviços Docker (11 total)

| Serviço               | Porta | Função                                                |
|-----------------------|-------|-------------------------------------------------------|
| `mysql`               | 3306  | Banco de dados MySQL 8                                |
| `central-computer`    | 8000  | Orquestrador, API REST, WebSocket                     |
| `dispenser-adapter`   | 8100  | Bridge HTTP: central ↔ dispenser-simulator            |
| `cnc-adapter`         | 8101  | Bridge HTTP: central ↔ cnc-simulator                  |
| `vision-adapter`      | 8102  | Bridge HTTP: central ↔ vision-simulator               |
| `dispenser-simulator` | 8201  | Simula 6 dispensers físicos (mecânico, sem CV)        |
| `cnc-simulator`       | 8200  | Simula firmware da mesa CNC                           |
| `vision-simulator`    | 8202  | Simula câmera dos dispensers e câmera da mesa         |
| `order-generator`     | —     | Gera OS aleatórias por categoria e envia ao central   |
| `dashboard`           | 8050  | Monitoramento read-only (Plotly Dash)                 |
| `ihm_web`             | 8051  | IHM de manutenção com autenticação JWT (Plotly Dash)  |

---

## Fluxo de uma OS

```
1.  order-generator  →  POST /api/v1/ordens  →  central-computer
2.  central-computer: atribui slots por categoria/residual disponível
3.  central-computer: planeja rota CNC (nearest-neighbor)
4.  central-computer  →  dispenser-adapter  →  POST /executar/carregar  (paralelo)
5.  dispenser-simulator: carrega remédios, reporta "carregado"
6.  central-computer  →  vision-adapter  →  POST /comandos/capturar/dispenser  (paralelo)
    ↳  vision-simulator: lê QR/barcode de cada slot, reporta leitura ao central via evento
    ↳  central: registra alarme se SKU divergente — não aborta OS (não-bloqueante)
7.  Para cada slot na rota CNC:
    a. central-computer  →  cnc-adapter  →  POST /executar/mover
    b. cnc-simulator: interpola posição, reporta "posicionado"
    c. central-computer  →  dispenser-adapter  →  POST /executar/dispensar
    d. dispenser-simulator: dispensa mecanicamente, reporta "dispensado"
    e. central-computer  →  vision-adapter  →  POST /comandos/capturar/mesa
       ↳  vision-simulator: conta produtos na mesa, reporta resultado ao central via evento
       ↳  central: registra alarme se contagem divergente — não aborta OS (não-bloqueante)
8.  cnc-adapter  →  POST /executar/homing
9.  OS marcada como "concluída" no MySQL
10. Broadcast WebSocket para Dashboard, IHM e Displays
```

---

## Camada de Visão Computacional

A validação por CV é responsabilidade exclusiva do `vision-adapter` + `vision-simulator`. O `dispenser-simulator` é hardware puro e não faz validação de qualidade.

| Câmera            | Posição          | Valida                         | Outcomes                                                    |
|-------------------|------------------|--------------------------------|-------------------------------------------------------------|
| Câmera Dispenser  | Sobre os slots   | QR/DataMatrix/barcode do produto| `leitura_dispenser_ok`, `falha`, `divergencia` (SKU errado) |
| Câmera Mesa       | Sobre a coleta   | Posição e contagem de unidades | `leitura_mesa_ok`, `falha`, `divergencia` (contagem errada) |

Ambas são **não-bloqueantes**: divergências geram alarmes no sistema mas não abortam a OS. O bloqueio por Triple Check é trabalho futuro.

Probabilidades configuráveis por env var (padrão 2% cada):
- `PROB_FALHA_LEITURA_DISPENSER` — câmera do dispenser não consegue ler
- `PROB_DIVERGENCIA_DISPENSER` — leu mas SKU errado
- `PROB_FALHA_LEITURA_MESA` — câmera da mesa não detecta produto
- `PROB_DIVERGENCIA_MESA` — detecta mas conta errado

---

## Pré-requisitos

- Docker Engine ≥ 24
- Docker Compose v2
- ~2 GB RAM

---

## Como rodar

```bash
# 1. Clone o repositório
git clone https://github.com/seu-usuario/valoryapsen.git
cd valoryapsen

# 2. (Primeira vez) Remover diretórios legados MQTT
bash cleanup_phase3.sh

# 3. Build e start
docker compose down -v
docker compose build --no-cache
docker compose up -d

# 4. Acompanhar logs
docker compose logs -f central-computer
docker compose logs -f vision-simulator
docker compose logs -f dispenser-simulator
docker compose logs -f cnc-simulator
```

---

## Interfaces

| Interface   | URL                     | Acesso    |
|-------------|-------------------------|-----------|
| Dashboard   | http://localhost:8050   | Público   |
| IHM Web     | http://localhost:8051   | JWT       |
| API Central | http://localhost:8000   | REST/WS   |
| Docs API    | http://localhost:8000/docs | Swagger |

**Usuários padrão (IHM):**

| Usuário   | Senha        | Perfil      |
|-----------|--------------|-------------|
| `admin`   | `apsen2024`  | admin       |
| `tecnico` | `manut2024`  | manutencao  |

---

## Endpoints principais

```
GET  /ping                               → health check
GET  /estado                             → estado completo em memória
GET  /os/ativa                           → OS em execução
GET  /os/historico                       → histórico de OS
GET  /medicamentos                       → catálogo (96 medicamentos APSEN)
GET  /dispensers/estado                  → estado dos 6 slots no DB
GET  /alarmes                            → alarmes ativos/resolvidos
WS   /ws                                 → push de estado em tempo real

POST /api/v1/ordens                      → recebe nova OS (order-generator)
POST /api/v1/eventos/dispenser           → recebe eventos (dispenser-adapter)
POST /api/v1/eventos/cnc                 → recebe eventos (cnc-adapter)
POST /api/v1/eventos/visao               → recebe resultados de CV (vision-adapter)

POST /auth/login                         → gera JWT
GET  /auth/me                            → usuário autenticado
POST /manutencao/dispensers/{id}/limpar  → limpa slot
GET  /manutencao/alarmes                 → alarmes (autenticado)
PUT  /manutencao/alarmes/{id}/resolver   → resolve alarme
GET  /manutencao/log                     → log de manutenção
```

---

## Estrutura do repositório

```
valoryapsen/
├── central-computer/       # Orquestrador + API central
│   ├── main.py             # FastAPI + handlers de eventos (dispenser, CNC, visão)
│   ├── orchestrator.py     # Lógica de negócio (fila, atribuição, rota, visão)
│   ├── database.py         # MySQL (10 tabelas, 96 medicamentos)
│   ├── config.py           # Settings via env vars
│   ├── auth.py             # JWT + bcrypt
│   ├── requirements.txt
│   └── Dockerfile
├── dispenser-adapter/      # Bridge HTTP: central ↔ dispenser-simulator (:8100)
├── cnc-adapter/            # Bridge HTTP: central ↔ cnc-simulator (:8101)
├── vision-adapter/         # Bridge HTTP: central ↔ vision-simulator (:8102)
├── dispenser_simulator/    # Simula 6 dispensers físicos (:8201)
├── cnc_simulator/          # Simula firmware da CNC (:8200)
├── vision-simulator/       # Simula câmera dispenser + câmera mesa (:8202)
├── order-generator/        # Gera OS aleatórias por categoria (sem porta)
├── dashboard/              # Plotly Dash read-only :8050
├── ihm_web/                # Plotly Dash IHM manutenção :8051
├── mysql/init.sql          # Schema MySQL (criado pelo database.py)
├── docker-compose.yml
├── cleanup_phase3.sh       # Remove diretórios legados MQTT
└── .gitignore
```

---

## Variáveis de ambiente relevantes

| Variável                       | Padrão                          | Serviço            |
|--------------------------------|---------------------------------|--------------------|
| `DISPENSER_ADAPTER_URL`        | `http://dispenser-adapter:8100` | central-computer   |
| `CNC_ADAPTER_URL`              | `http://cnc-adapter:8101`       | central-computer   |
| `VISION_ADAPTER_URL`           | `http://vision-adapter:8102`    | central-computer   |
| `TIMEOUT_CARREGAMENTO`         | `180`s                          | central-computer   |
| `TIMEOUT_POSICIONAMENTO`       | `120`s                          | central-computer   |
| `TIMEOUT_DISPENSA`             | `120`s                          | central-computer   |
| `TIMEOUT_VISAO_DISPENSER`      | `30`s                           | central-computer   |
| `TIMEOUT_VISAO_MESA`           | `30`s                           | central-computer   |
| `T_CARGA_UNID`                 | `0.3`s por unidade              | dispenser-sim      |
| `T_DISPENSA_UNID`              | `0.5`s por unidade              | dispenser-sim      |
| `PROB_ERRO_MECANICO`           | `0.01` (1% falha mecânica)      | dispenser-sim      |
| `PROB_FALHA_LEITURA_DISPENSER` | `0.02` (2% falha câmera)        | vision-sim         |
| `PROB_DIVERGENCIA_DISPENSER`   | `0.02` (2% SKU errado)          | vision-sim         |
| `PROB_FALHA_LEITURA_MESA`      | `0.02` (2% não detecta)         | vision-sim         |
| `PROB_DIVERGENCIA_MESA`        | `0.02` (2% contagem errada)     | vision-sim         |
| `T_SCAN_DISPENSER`             | `1.5`s                          | vision-sim         |
| `T_SCAN_MESA`                  | `2.0`s                          | vision-sim         |
| `VEL_MM_S`                     | `80` mm/s                       | cnc-sim            |
| `INTERVALO_OS`                 | `90`s entre OS                  | order-generator    |
| `MIN_MEDS_POR_OS`              | `2`                             | order-generator    |
| `MAX_MEDS_POR_OS`              | `6`                             | order-generator    |

---

## Concorrência e segurança

- `asyncio.get_running_loop()` no lifespan (Python ≥3.10 safe)
- `asyncio.to_thread()` para todo I/O MySQL — event loop nunca bloqueado
- `call_soon_threadsafe()` para notificação de eventos a partir de qualquer thread
- `threading.Lock` por slot no dispenser-simulator (race condition eliminada)
- `threading.Lock` atômico no cnc-simulator para `_em_movimento` (race condition eliminada)
- Pré-registro de eventos antes de enviar comandos (resposta rápida nunca perdida)
- JWT + bcrypt para autenticação da IHM
