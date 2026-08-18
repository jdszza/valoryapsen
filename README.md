# APSEN – Sistema de Contagem de Medicamentos

> **Projeto de Engenharia Mecatrônica – 3º Ano**  
> Valory · APSEN Farmacêutica

---

## Visão Geral

Sistema automatizado de contagem e validação de medicamentos. Uma mesa CNC posiciona-se sobre 6 dispensers para coletar os medicamentos de cada Ordem de Saída (OS). Duas câmeras de visão computacional validam o produto carregado (câmera dos dispensers) e a contagem na mesa de coleta (câmera da mesa). Uma célula de carga HX711 instalada sob a mesa CNC valida o peso de cada lote dispensado. Após cada dispensa, um **Triple Check** compara as 3 fontes (contagem do dispenser, câmera da mesa e balança) e trava o sistema em caso de divergência até intervenção do operador. A comunicação é 100% REST/HTTP e WebSocket — **sem MQTT**. MySQL para persistência, Plotly Dash para interface.

---

## Arquitetura (v3.2 — REST/HTTP)

```
Order Generator (gera OS aleatórias por categoria)
  │ POST /api/v1/ordens
  ▼
┌────────────────────────────────────────────────────────────────────┐
│                     Central Computer  :8000                        │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Orquestrador (orchestrator.py)                              │  │
│  │  • Fila de OS (1 por vez)                                    │  │
│  │  • Atribuição de slots por categoria/residual                │  │
│  │  • Rota CNC nearest-neighbor                                 │  │
│  │  • Fluxo: carga → visão dispenser → CNC → dispensa →         │  │
│  │           visão mesa → peso → Triple Check                   │  │
│  │  • Trava de erro: bloqueia até admin liberar                 │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  REST + MySQL + WebSocket /ws                                      │
└──────┬────────────────┬─────────────────┬───────────────┬──────────┘
       │                │                 │               │
  POST comandos    POST comandos     POST comandos   POST comandos
       │                │                 │               │
┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐  ┌────▼────────┐
│  Dispenser  │  │    CNC      │  │   Vision    │  │   Weight    │
│  Adapter    │  │   Adapter   │  │   Adapter   │  │   Adapter   │
│   :8100     │  │    :8101    │  │    :8102    │  │    :8103    │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └────┬────────┘
       │                │                 │               │
┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐  ┌────▼────────┐
│  Dispenser  │  │    CNC      │  │   Vision    │  │   Weight    │
│  Simulator  │  │  Simulator  │  │  Simulator  │  │  Simulator  │
│   :8201     │  │    :8200    │  │    :8202    │  │    :8203    │
│ 6 slots     │  │ firmware    │  │ cam disp +  │  │ HX711 sob   │
│ físicos     │  │ CNC         │  │ cam mesa    │  │ mesa CNC    │
└─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘

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

### Serviços Docker (13 total)

| Serviço               | Porta | Função                                                       |
|-----------------------|-------|--------------------------------------------------------------|
| `mysql`               | 3306  | Banco de dados MySQL 8                                       |
| `central-computer`    | 8000  | Orquestrador, API REST, WebSocket                            |
| `dispenser-adapter`   | 8100  | Bridge HTTP: central ↔ dispenser-simulator                   |
| `cnc-adapter`         | 8101  | Bridge HTTP: central ↔ cnc-simulator                         |
| `vision-adapter`      | 8102  | Bridge HTTP: central ↔ vision-simulator                      |
| `weight-adapter`      | 8103  | Bridge HTTP: central ↔ weight-simulator (HX711)              |
| `dispenser-simulator` | 8201  | Simula 6 dispensers físicos (mecânico, sem CV)               |
| `cnc-simulator`       | 8200  | Simula firmware da mesa CNC                                  |
| `vision-simulator`    | 8202  | Simula câmera dos dispensers e câmera da mesa                |
| `weight-simulator`    | 8203  | Simula célula de carga HX711 instalada sob a mesa CNC        |
| `order-generator`     | —     | Gera OS aleatórias por categoria e envia ao central          |
| `dashboard`           | 8050  | Monitoramento read-only (Plotly Dash)                        |
| `ihm_web`             | 8051  | IHM de manutenção com autenticação JWT (Plotly Dash)         |

---

## Fluxo de uma OS

```
1.  order-generator  →  POST /api/v1/ordens  →  central-computer
2.  central-computer: atribui slots por categoria/residual disponível
3.  central-computer: planeja rota CNC (nearest-neighbor)
4.  central-computer  →  weight-adapter  →  POST /tara  (zera balança para a OS)
5.  central-computer  →  dispenser-adapter  →  POST /executar/carregar  (paralelo por slot)
6.  dispenser-simulator: carrega remédios, reporta "carregado"
7.  central-computer  →  vision-adapter  →  POST /comandos/capturar/dispenser  (paralelo)
    ↳  vision-simulator: lê QR/barcode de cada slot, reporta leitura ao central via evento
    ↳  central: SKU errado → trava imediata + retry após operador corrigir; falha de leitura → alarme não-bloqueante
8.  Para cada slot na rota CNC:
    a. central-computer  →  cnc-adapter  →  POST /executar/mover
    b. cnc-simulator: interpola posição, reporta "posicionado"
    c. central-computer  →  dispenser-adapter  →  POST /executar/dispensar
    d. dispenser-simulator: dispensa mecanicamente, reporta "dispensado" (contagem)
    e. central-computer  →  vision-adapter  →  POST /comandos/capturar/mesa
       ↳  vision-simulator: conta produtos na mesa, reporta resultado ao central via evento
    f. central-computer  →  weight-adapter  →  POST /pesar
       ↳  weight-simulator: lê delta de peso na mesa, reporta desvio em relação ao esperado
    g. Triple Check: compara as 3 fontes (dispenser, câmera mesa, balança)
       • 1 fonte divergente já basta → trava.ativa=True, OS suspensa até
         liberação por admin/supervisor
       • OK → continua para o próximo slot
9.  cnc-adapter  →  POST /executar/homing
10. OS marcada como "concluída" no MySQL
11. Broadcast WebSocket para Dashboard, IHM e Displays
```

---

## Triple Check

Após cada dispensa de slot, o orquestrador valida **3 fontes independentes**:

| Fonte          | O que mede                               | Evento divergente               |
|----------------|------------------------------------------|---------------------------------|
| Dispenser      | Quantidade contada mecanicamente         | `dispensado` → `quantidade_dispensada` ≠ alvo |
| Câmera da mesa | Contagem por visão computacional         | `leitura_mesa_divergencia`      |
| Balança HX711  | Delta de peso (incremento do slot) em g  | `peso_divergencia`              |

Se **qualquer** das 3 fontes divergir além da tolerância, o sistema ativa a **trava de erro**:
- A OS é suspensa (não abortada)
- Um alarme `trava_ativada` (componente `triple_check`) é registrado no banco
- O estado `trava.ativa = True` é transmitido via WebSocket
- O dashboard exibe banner vermelho
- Apenas admin ou supervisor pode liberar via `POST /api/v1/admin/liberar-trava`

O limiar é uma unidade porque os dois erros não custam a mesma coisa: parar uma OS boa custa uma liberação de supervisor, deixar passar uma OS ruim custa medicamento errado — ou na quantidade errada — chegando ao paciente. Se em campo uma fonte específica provar gerar trava sem causa real, o limiar é ajustável por env var no `central-computer`, **sem alterar código**:

| Env var                         | Default | Efeito                                        |
|---------------------------------|---------|-----------------------------------------------|
| `TRIPLE_CHECK_MIN_DIVERGENCIAS` | `1`     | Nº de fontes divergentes que ativa a trava (faixa 1–3; valor fora da faixa cai no default) |

Com limiar acima de 1, a divergência que não trava ainda vira alarme `divergencia_abaixo_do_limiar` no banco — quem elevou o limiar precisa poder auditar o que passou por baixo dele.

**Fonte que não mediu não é fonte que divergiu.** Timeout e falha de leitura da câmera (`leitura_mesa_falha`), ou `erro_sensor` da balança, entram como *fonte indisponível*: são registradas em log, mas não contam para o limiar. Elas não contradizem nada — apenas deixam de confirmar. Contá-las transformaria os ~2% de falha de leitura da câmera em trava por ruído, e trava por ruído é trava desligada em campo. É o mesmo critério já aplicado à câmera do dispenser, onde SKU errado bloqueia e falha de leitura não.

A balança mede o peso **total acumulado** desde a tara. Para validar cada slot individualmente, o sistema calcula o **delta** (incremento desde a última leitura), eliminando o efeito do acúmulo de slots anteriores na mesa. O comando de pesagem carrega duas quantidades — `quantidade_esperada` (alvo da OS, base do peso esperado) e `quantidade_real` (o que o dispenser reportou ter soltado, base do peso que entra na mesa) —, e é da diferença entre as duas que a divergência de peso emerge.

A regra vive em `orchestrator.avaliar_triple_check()`, função pura e testada em `tests/test_orchestrator.py`.

---

## Camada de Visão Computacional

A validação por CV é responsabilidade exclusiva do `vision-adapter` + `vision-simulator`. O `dispenser-simulator` é hardware puro e não faz validação de qualidade.

| Câmera            | Posição          | Valida                          | Outcomes                                                    |
|-------------------|------------------|---------------------------------|-------------------------------------------------------------|
| Câmera Dispenser  | Sobre os slots   | QR/DataMatrix/barcode do produto | `leitura_dispenser_ok`, `falha`, `divergencia` (SKU errado) |
| Câmera Mesa       | Sobre a coleta   | Posição e contagem de unidades  | `leitura_mesa_ok`, `falha`, `divergencia` (contagem errada) |

Câmera dispenser (SKU errado): **bloqueante** — aciona trava imediatamente. Operador remove o medicamento errado, admin libera a trava, sistema re-escaneia o slot. Repete até confirmar SKU correto. Falha de leitura (câmera não conseguiu ler) é não-bloqueante — gera alarme e continua.  
Câmera mesa: **bloqueante via Triple Check** — faz parte da validação de 3 fontes por slot.

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

# 2. Crie o .env (OBRIGATÓRIO — nada sobe sem isto). Ele NÃO é versionado.
cat > .env <<'FIM'
# ── Assinatura dos JWT ───────────────────────────────────────────────────────
# OBRIGATÓRIO. Gere com:
#   python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=

# "dev" tolera SECRET_KEY fraca, só com aviso no log. Qualquer outro valor
# (inclusive vazio → prod) trata segredo fraco como erro e IMPEDE o boot.
APSEN_ENV=prod

# ── MySQL ────────────────────────────────────────────────────────────────────
# OBRIGATÓRIOS: MYSQL_ROOT_PASS e MYSQL_PASS. Lidos na PRIMEIRA subida (criação
# do volume mysql_data); trocar depois exige `docker compose down -v`, que
# apaga os dados.
MYSQL_ROOT_PASS=
MYSQL_USER=apsen
MYSQL_PASS=
MYSQL_DB=apsen_db

# ── Usuários seed da IHM de manutenção ───────────────────────────────────────
# OBRIGATÓRIAS. Criadas só na primeira inicialização do banco. TROQUE AMBAS
# após o primeiro login, pela própria IHM (aba Usuários) — ficam em claro aqui.
SEED_ADMIN_SENHA=
SEED_MANUT_SENHA=

# ── Origens de browser autorizadas a chamar o central ────────────────────────
# Ajuste ao publicar fora da máquina local. `*` não é aceito no central.
CORS_ORIGINS=http://localhost:8050,http://localhost:8051
FIM

# Preencha as CINCO obrigatórias antes de seguir:
#   SECRET_KEY  MYSQL_ROOT_PASS  MYSQL_PASS  SEED_ADMIN_SENHA  SEED_MANUT_SENHA
# As demais já têm default utilizável. O compose recusa subir com qualquer
# obrigatória vazia, citando o nome da variável que falta.

# 3. Build e start
docker compose down -v
docker compose build --no-cache
docker compose up -d

# 3. Acompanhar logs
docker compose logs -f central-computer
docker compose logs -f weight-simulator
docker compose logs -f vision-simulator
docker compose logs -f dispenser-simulator
docker compose logs -f cnc-simulator
```

---

## Interfaces

| Interface   | URL                        | Acesso    |
|-------------|----------------------------|-----------|
| Dashboard   | http://localhost:8050      | Público   |
| IHM Web     | http://localhost:8051      | JWT       |
| API Central | http://localhost:8000      | REST/WS   |
| Docs API    | http://localhost:8000/docs | Swagger   |

**Usuários padrão (IHM):**

| Usuário  | Senha                        | Perfil      |
|----------|------------------------------|-------------|
| `admin`  | `SEED_ADMIN_SENHA` do `.env` | admin       |
| `manut1` | `SEED_MANUT_SENHA` do `.env` | manutencao  |

> **Troque as duas após o primeiro login**, pela própria IHM (aba 👥 Usuários).
> As senhas seed ficam em arquivo, em claro, e são lidas apenas na criação do
> banco — depois disso o `.env` só serve para lembrar quem tem acesso.

### Segredos

Nada de segredo entra no repositório. O `docker-compose.yml` lê tudo do `.env`
(ignorado pelo git) e **falha ao subir** se faltar algo:

| Variável | Para quê |
|----------|----------|
| `SECRET_KEY` | assina os JWT. Gere com `python -c "import secrets; print(secrets.token_hex(32))"` |
| `MYSQL_ROOT_PASS`, `MYSQL_PASS` | credenciais do banco (lidas na criação do volume) |
| `SEED_ADMIN_SENHA`, `SEED_MANUT_SENHA` | senhas iniciais da IHM |
| `APSEN_ENV` | `prod` (default) recusa segredo fraco; `dev` só avisa |

O central **se recusa a subir** com `SECRET_KEY` default, vazia ou com menos de
32 caracteres, a não ser com `APSEN_ENV=dev`. O valor default antigo está no
histórico público deste repositório: com ele qualquer pessoa forja um token
`role=admin`, libera a trava do Triple Check e altera usuários.

---

## Endpoints principais

```
GET  /ping                               → health check
GET  /estado                             → estado completo em memória
GET  /os/ativa                           → OS em execução (ou a próxima da fila)
GET  /os/historico                       → histórico de OS
GET  /medicamentos                       → catálogo (96 medicamentos APSEN)
GET  /dispensers/estado                  → estado dos 6 slots no DB
GET  /alarmes                            → alarmes ativos/resolvidos
WS   /ws                                 → push de estado em tempo real

POST /api/v1/ordens                      → recebe nova OS (order-generator)
                                           409 os_duplicada | 429 fila_cheia |
                                           503 persistencia_indisponivel
GET  /api/v1/fila                        → ocupação da fila (backpressure)
POST /api/v1/eventos/dispenser           → recebe eventos (dispenser-adapter)
POST /api/v1/eventos/cnc                 → recebe eventos (cnc-adapter)
POST /api/v1/eventos/visao               → recebe resultados de CV (vision-adapter)
POST /api/v1/eventos/peso                → recebe leituras de peso (weight-adapter)

POST /auth/login                         → gera JWT
GET  /auth/me                            → usuário autenticado
POST /manutencao/dispensers/{id}/limpar  → limpa slot
GET  /manutencao/alarmes                 → alarmes (autenticado)
PUT  /manutencao/alarmes/{id}/resolver   → resolve alarme
GET  /manutencao/log                     → log de manutenção
POST /api/v1/admin/liberar-trava         → libera trava de erro (admin/supervisor)
GET  /api/v1/visao/historico             → histórico de leituras de CV
GET  /api/v1/relatorio/os/{os_id}?formato=csv|xlsx
                                         → relatório da OS (só header Authorization;
                                           a IHM baixa server-side e entrega ao navegador)
```

---

## Estrutura do repositório

```
valoryapsen/
├── central-computer/       # Orquestrador + API central
│   ├── main.py             # FastAPI + handlers de eventos (dispenser, CNC, visão, peso)
│   ├── orchestrator.py     # Lógica de negócio (fila, atribuição, rota, Triple Check)
│   ├── database.py         # MySQL (10 tabelas, 96 medicamentos)
│   ├── config.py           # Settings via env vars
│   ├── auth.py             # JWT + bcrypt
│   ├── requirements.txt
│   └── Dockerfile
├── dispenser-adapter/      # Bridge HTTP: central ↔ dispenser-simulator (:8100)
├── cnc-adapter/            # Bridge HTTP: central ↔ cnc-simulator (:8101)
├── vision-adapter/         # Bridge HTTP: central ↔ vision-simulator (:8102)
├── weight-adapter/         # Bridge HTTP: central ↔ weight-simulator (:8103)
├── dispenser_simulator/    # Simula 6 dispensers físicos (:8201)
├── cnc_simulator/          # Simula firmware da CNC (:8200)
├── vision-simulator/       # Simula câmera dispenser + câmera mesa (:8202)
├── weight-simulator/       # Simula célula de carga HX711 sob a mesa CNC (:8203)
├── order-generator/        # Gera OS aleatórias por categoria (sem porta)
├── dashboard/              # Plotly Dash read-only :8050
├── ihm_web/                # Plotly Dash IHM manutenção :8051
├── ihm_esp32/              # ⚠️ firmware DEFASADO (ainda MQTT) — ver nota abaixo
├── mysql/init.sql          # Só charset/collation; o schema vem do database.py
├── tests/                  # pytest — sem Docker, sem MySQL (`make test`)
├── docker-compose.yml
└── .gitignore
```

> **`ihm_esp32/` está defasado.** O firmware fala MQTT (`PubSubClient`, tópicos
> `apsen/*`), de antes da migração para REST/HTTP: não há broker no compose e
> nenhum serviço publica tópico, então ele não conversa com o sistema atual.
> Migrá-lo para `GET /estado` + `WS /ws` é trabalho pendente; o código segue
> aqui como referência de layout de tela e pinagem.

---

## Variáveis de ambiente relevantes

| Variável                        | Padrão                          | Serviço            |
|---------------------------------|---------------------------------|--------------------|
| `SECRET_KEY`                    | (obrigatória, vem do `.env`)    | central-computer   |
| `APSEN_ENV`                     | `prod` (`dev` afrouxa o segredo) | central-computer  |
| `CORS_ORIGINS`                  | `http://localhost:8050,http://localhost:8051` | central-computer |
| `AUTH_CACHE_TTL_S`              | `30`s de cache da revalidação   | central-computer   |
| `DISPENSER_ADAPTER_URL`         | `http://dispenser-adapter:8100` | central-computer   |
| `CNC_ADAPTER_URL`               | `http://cnc-adapter:8101`       | central-computer   |
| `VISION_ADAPTER_URL`            | `http://vision-adapter:8102`    | central-computer   |
| `WEIGHT_ADAPTER_URL`            | `http://weight-adapter:8103`    | central-computer   |
| `TIMEOUT_CARREGAMENTO`          | `180`s                          | central-computer   |
| `TIMEOUT_POSICIONAMENTO`        | `120`s                          | central-computer   |
| `TIMEOUT_DISPENSA`              | `120`s                          | central-computer   |
| `TIMEOUT_VISAO_DISPENSER`       | `30`s                           | central-computer   |
| `TIMEOUT_VISAO_MESA`            | `30`s                           | central-computer   |
| `TIMEOUT_PESO`                  | `15`s                           | central-computer   |
| `MAX_FILA_OS`                   | `5` OS esperando (faixa 1–1000) | central-computer   |
| `BROADCAST_MIN_INTERVALO_MS`    | `500`ms entre broadcasts periódicos | central-computer |
| `CNC_AMOSTRAGEM_MOVENDO`        | `0` (não grava trajetória; N = 1 em N) | central-computer |
| `RETENCAO_DIAS`                 | `30` dias de histórico          | central-computer   |
| `EXPURGO_INTERVALO_HORAS`       | `24`h entre expurgos            | central-computer   |
| `T_CARGA_UNID`                  | `0.3`s por unidade              | dispenser-sim      |
| `T_DISPENSA_UNID`               | `0.5`s por unidade              | dispenser-sim      |
| `PROB_ERRO_MECANICO`            | `0.01` (1% falha mecânica)      | dispenser-sim      |
| `PROB_FALHA_LEITURA_DISPENSER`  | `0.02` (2% falha câmera)        | vision-sim         |
| `PROB_DIVERGENCIA_DISPENSER`    | `0.02` (2% SKU errado)          | vision-sim         |
| `PROB_FALHA_LEITURA_MESA`       | `0.02` (2% não detecta)         | vision-sim         |
| `PROB_DIVERGENCIA_MESA`         | `0.02` (2% contagem errada)     | vision-sim         |
| `T_SCAN_DISPENSER`              | `1.5`s                          | vision-sim         |
| `T_SCAN_MESA`                   | `2.0`s                          | vision-sim         |
| `TOLERANCIA_PERC`               | `5.0`%                          | weight-sim         |
| `PROB_ERRO_SENSOR`              | `0.01` (1% falha HX711)         | weight-sim         |
| `T_LEITURA`                     | `1.5`s                          | weight-sim         |
| `RUIDO_G`                       | `2.0`g (ruído gaussiano)        | weight-sim         |
| `VEL_MM_S`                      | `80` mm/s                       | cnc-sim            |
| `INTERVALO_OS`                  | `90`s entre OS                  | order-generator    |
| `ESPERA_FILA_CHEIA`             | `20`s entre consultas à fila    | order-generator    |
| `MAX_ESPERAS_FILA`              | `15` esperas antes de pular o ciclo | order-generator |
| `MIN_MEDS_POR_OS`               | `2`                             | order-generator    |
| `MAX_MEDS_POR_OS`               | `6`                             | order-generator    |

---

## Concorrência e segurança

- `asyncio.get_running_loop()` no lifespan (Python ≥3.10 safe)
- `asyncio.to_thread()` para todo I/O MySQL — event loop nunca bloqueado
- `call_soon_threadsafe()` para notificação de eventos a partir de qualquer thread
- `threading.Lock` por slot no dispenser-simulator (race condition eliminada)
- `threading.Lock` atômico no cnc-simulator para `_em_movimento` (race condition eliminada)
- `threading.Lock` + `_peso_anterior_g` no weight-simulator para leitura delta thread-safe
- Pré-registro de eventos antes de enviar comandos (resposta rápida nunca perdida)
- `copy.deepcopy` sob o lock em todo snapshot de estado — a cópia rasa deixava
  os dicionários aninhados vivos durante a serialização, fora do lock
- JWT + bcrypt para autenticação da IHM
- **Revalidação a cada requisição autenticada**: o token não é a palavra final;
  usuário desativado perde acesso na hora e a `role` vale a do banco, não a do
  token (cache de `AUTH_CACHE_TTL_S`)
- Segredos só no `.env` (não versionado); o central **recusa subir** com
  `SECRET_KEY` default fora de `APSEN_ENV=dev`
- CORS restrito ao dashboard e à IHM (`CORS_ORIGINS`), não `*`
- Healthcheck em todos os serviços de aplicação e `depends_on` por
  `condition: service_healthy` — ordem de start não é prontidão
- Triple Check com trava de erro garante intervenção humana em qualquer divergência (limiar 1, ajustável por `TRIPLE_CHECK_MIN_DIVERGENCIAS`)
