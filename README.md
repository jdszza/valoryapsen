# APSEN — Sistema de Contagem de Medicamentos

> **Projeto de Engenharia Mecatrônica — 3º ano** · Valory · APSEN Farmacêutica

Célula automatizada que monta Ordens de Saída (OS) de medicamento e valida cada
dispensa por **três fontes independentes**. Roda hoje inteira em Docker, com os
sensores e atuadores simulados; os três firmwares que vão substituir os
simuladores já têm contrato e transporte prontos do lado Python.

| Onde está o quê | |
|---|---|
| **Como o sistema funciona e como rodá-lo** | este arquivo |
| **Por que cada decisão foi tomada** (e as armadilhas) | `CLAUDE.md` |
| **O que falta fazer** | [`TASKS.md`](TASKS.md) |
| **Contrato serial adapter ↔ firmware** | [`docs/PROTOCOLO_SERIAL.md`](docs/PROTOCOLO_SERIAL.md) |
| **Painel de bancada e display de 7"** | [`docs/BANCADA.md`](docs/BANCADA.md) |

---

## Índice

- [Visão geral](#visão-geral)
- [Arquitetura](#arquitetura)
- [A célula por dentro](#a-célula-por-dentro) — layout, câmeras, Triple Check, fluxo
- [As 10 ordens padrão](#as-10-ordens-padrão)
- [Subir a stack](#subir-a-stack)
- [Operar](#operar) — console, modo apresentação, injeção, reset, pré-voo
- [Roteiro de demonstração](#roteiro-de-demonstração)
- [Testes](#testes)
- [Painel de bancada](#painel-de-bancada)
- [Interfaces e segredos](#interfaces-e-segredos)
- [Endpoints](#endpoints)
- [Estrutura do repositório](#estrutura-do-repositório)
- [Variáveis de ambiente](#variáveis-de-ambiente)
- [Concorrência e segurança](#concorrência-e-segurança)

---

## Visão geral

Uma mesa CNC percorre o corredor entre **duas fileiras de 4 dispensers**, frente
a frente, coletando os medicamentos de cada OS. **Três câmeras** validam a
célula: uma sobre cada fileira (lendo o QR/DataMatrix do produto carregado em
cada slot) e uma sobre a mesa de coleta — a **câmera da balança** —, que conta
visualmente as unidades dispensadas. Uma célula de carga HX711 sob a mesa pesa
cada lote.

Depois de cada dispensa, o **Triple Check** compara as três fontes (contagem do
dispenser, câmera da mesa, balança). **Uma divergência já trava o sistema** até
um supervisor liberar.

Comunicação 100% REST/HTTP + WebSocket — **sem MQTT**. MySQL para persistência,
Plotly Dash para as interfaces.

### Duas metades, que sobem separado

| Metade | O que é | Como sobe | Portas |
|---|---|---|---|
| **Célula** | central, 4 adapters, 4 simuladores, ERP, dashboard, manutenção | `docker compose up` | 8000, 8050, 8051 |
| **Painel de bancada** | Flask + SQLite + display ESP32 por USB serial | `python app.py` na máquina da bancada | 5000 |

O painel **não** está no compose de propósito: ele é dono de uma porta serial, e
container Linux não enxerga a COM do host. Ver [`docs/BANCADA.md`](docs/BANCADA.md).

---

## Arquitetura

```
ERP Simulator (dispara 1 das 10 ordens padrão, com os_id novo)
  │ POST /api/v1/ordens
  ▼
┌────────────────────────────────────────────────────────────────────┐
│                     Central Computer  :8000                        │
│   Orquestrador (orchestrator.py)                                   │
│   • fila de OS (1 por vez, teto MAX_FILA_OS)                       │
│   • atribuição de slots por categoria / residual                   │
│   • rota CNC em serpentina (duas fileiras)                         │
│   • carga → visão dispenser → CNC → dispensa → visão mesa →        │
│     peso → Triple Check                                            │
│   • trava de erro: bloqueia até admin liberar                      │
│   REST + MySQL + WebSocket /ws + console /console                  │
└──────┬────────────────┬─────────────────┬───────────────┬──────────┘
       │                │                 │               │
┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
│  Dispenser  │  │    CNC      │  │   Vision    │  │   Weight    │
│  Adapter    │  │   Adapter   │  │   Adapter   │  │   Adapter   │
│   :8100     │  │    :8101    │  │    :8102    │  │    :8103    │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │ http | serial  │ http | serial   │ http          │ http | serial
┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
│  Dispenser  │  │    CNC      │  │   Vision    │  │   Weight    │
│  Simulator  │  │  Simulator  │  │  Simulator  │  │  Simulator  │
│   :8201     │  │    :8200    │  │    :8202    │  │    :8203    │
│  8 slots    │  │ mesa CNC    │  │ 3 câmeras   │  │ HX711       │
└─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘

Dashboard  :8050  ←─ polling /estado ──────── Central
Manutenção :8051  ←─ JWT + REST + WS ──────── Central
MySQL      :3306  ←─ pymysql (pool) ───────── Central
Painel     :5000  ─ GET (espelho de mão única) → Central
                   └─ USB serial → display ESP32 7"
```

### Serviços Docker (13)

| Serviço | Porta | Função |
|---|---|---|
| `mysql` | 3306 | MySQL 8 — 11 tabelas, 96 medicamentos no seed |
| `central-computer` | 8000 | Orquestrador, API REST, WebSocket, console |
| `dispenser-adapter` | 8100 | Bridge central ↔ dispensers |
| `cnc-adapter` | 8101 | Bridge central ↔ mesa CNC |
| `vision-adapter` | 8102 | Bridge central ↔ câmeras |
| `weight-adapter` | 8103 | Bridge central ↔ balança HX711 |
| `dispenser-simulator` | 8201 | Simula os 8 dispensers (mecânico, sem CV) |
| `cnc-simulator` | 8200 | Simula o firmware da mesa |
| `vision-simulator` | 8202 | Simula as 3 câmeras |
| `weight-simulator` | 8203 | Simula a célula de carga sob a mesa |
| `erp-simulator` | — | O ERP do hospital: dispara 1 das 10 ordens por ciclo |
| `dashboard` | 8050 | Monitoramento read-only (Dash) |
| `manut_web` | 8051 | Manutenção e operação, com JWT (Dash) |

**Ordem de subida.** Todos os serviços de aplicação têm healthcheck (menos o
`erp-simulator`, que é worker de laço sem porta) e as dependências usam
`condition: service_healthy`. O grafo é
`mysql → central → adapters → erp-simulator`, com os simuladores entrando ao
lado do central. O central **não** depende dos adapters — seria ciclo, e ele
tolera adapter fora do ar.

### Os adapters têm DOIS transportes

Cada adapter fala com a ponta de baixo por HTTP (o simulador) **ou** por serial
(o firmware), e quem escolhe é uma env var. O default é `http`:

| Variável | Default | |
|---|---|---|
| `<SUB>_TRANSPORTE` | `http` | `http` = simulador, `serial` = firmware |
| `<SUB>_SERIAL_URL` | vazia | vazia = varre as portas e detecta pelo ping da placa |
| `<SUB>_SERIAL_BAUD` | `115200` | |
| `<SUB>_ACK_TIMEOUT_S` | `2` | prazo do ACK, **não** da conclusão |

`<SUB>` é `DISPENSER`, `CNC` ou `WEIGHT`. O `vision-adapter` não entra: a visão
continua por HTTP.

A **perna de cima não muda**: mesmos endpoints `/comandos/*`, mesmos modelos,
mesmo payload de evento indo ao central sem interpretação. Por isso trocar o
simulador por firmware não toca em `central-computer/`. Contrato completo em
[`docs/PROTOCOLO_SERIAL.md`](docs/PROTOCOLO_SERIAL.md); o transporte é o
`serial_link.py` (cópia idêntica nos três adapters, com teste que cobra a
igualdade).

Em Linux a porta entra no container pelo `devices:` do compose — o bloco está
escrito e **comentado** nos três serviços. Em Windows o Docker Desktop não
repassa COM: o caminho é uma ponte RFC2217 no host e
`<SUB>_SERIAL_URL=rfc2217://host:porta`.

### Fontes de verdade

Cada campo do estado tem **um** dono, e a linha entre os dois é o que impede a
telemetria de desfazer o fluxo:

| Dono | Manda em | Campos |
|---|---|---|
| **Simulador** (ou firmware) | hardware e estoque | `medicamento`, `sku`, `categoria`, `quantidade` |
| **Orquestrador** | fluxo da OS | `status` da etapa, `os_id` em execução |

O evento `status` do dispenser é **telemetria periódica** (a cada 15 s, para
todos os slots) e só pode tocar na primeira linha da tabela. Quando ele
encostava no fluxo, desfazia o reset de fim de OS a cada ciclo. Quem move o
fluxo são os eventos de **transição**: `carregado`, `dispensado`, `limpeza_ok`,
`erro`.

O mesmo vale para os dois lados do status de uma OS: `_estado["os_ativa"]`
(memória, para o WebSocket) e `ordens.status` (banco, para quem consulta por
HTTP) contam a mesma história para públicos diferentes, e **os dois precisam ser
escritos** — toda saída de `_processar_os` fecha em status terminal
(`concluida`, `erro` ou `cancelada`).

---

## A célula por dentro

### Layout físico

```
             x=0     x=120   x=240   x=360
              │       │       │       │
   fileira    D1      D2      D3      D4        y = -150 mm
   esquerda   ▔▔      ▔▔      ▔▔      ▔▔
  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   y =    0 mm  ← corredor da CNC
   ●HOME      ▁▁      ▁▁      ▁▁      ▁▁
   (-120,0)   D5      D6      D7      D8        y = +150 mm
   fileira direita
```

Os pares frente a frente são D1↔D5, D2↔D6, D3↔D7 e D4↔D8 — consequência da
numeração: o slot `i` e o slot `i + NUM_SLOTS/2` compartilham o X.

**A geometria tem um dono só.** Passo (120 mm), afastamento (150 mm), HOME e
`NUM_SLOTS` são constantes no topo de `orchestrator.py`, e `POSICOES` é derivado
delas. O `cnc-simulator` não guarda cópia: recebe `posicao_x`/`posicao_y` em
cada comando e só valida a faixa do id.

**Rota em serpentina** — desce uma fileira em X crescente, volta pela outra em X
decrescente. O ciclo é fechado (HOME → slots → HOME) e todos os pontos estão na
borda de um polígono convexo, então a ordem do contorno *é* o trajeto ótimo:
medido por força bruta nos 255 subconjuntos de 8 slots, acerta o ótimo em
255/255, contra um nearest-neighbor 4,1% pior em média e 40,4% no pior caso.

### As três câmeras

| Câmera | `camera` | Cobre | Valida | Bloqueia? |
|---|---|---|---|---|
| Fileira esquerda | `dispenser_esq` | D1–D4 | SKU do produto carregado | **sim**, se o SKU não bate |
| Fileira direita | `dispenser_dir` | D5–D8 | SKU do produto carregado | **sim**, se o SKU não bate |
| Mesa (**a da balança**) | `mesa` | mesa de coleta | contagem das unidades | **sim**, via Triple Check |

As duas de dispenser emitem os **mesmos tipos de evento**; o que separa uma da
outra é o campo `camera`. Quem escolhe a câmera é o simulador, derivando o lado
do próprio `slot_id` — o central manda só o slot. Falha de leitura (a câmera não
conseguiu ler) é **não-bloqueante**: gera alarme e a OS continua.

`PROB_FALHA_LEITURA_DISPENSER` e `PROB_DIVERGENCIA_DISPENSER` valem para as
duas; o sufixo `_ESQ`/`_DIR` sobrescreve uma delas (para simular uma lente
suja). A da mesa é uma só.

### Triple Check

| Fonte | O que mede | Evento divergente |
|---|---|---|
| Dispenser | quantidade contada mecanicamente | `dispensado` com `quantidade_dispensada` ≠ alvo |
| Câmera da mesa | contagem por visão | `leitura_mesa_divergencia` |
| Balança HX711 | delta de peso do slot, em g | `peso_divergencia` |

**Uma divergência já trava.** A OS é suspensa (não abortada), um alarme
`trava_ativada` vai ao banco, `trava.ativa` sai pelo WebSocket e só admin ou
supervisor libera. O limiar é 1 porque os dois erros não custam o mesmo: parar
uma OS boa custa uma liberação; deixar passar uma ruim custa medicamento errado
no leito. Ajustável por `TRIPLE_CHECK_MIN_DIVERGENCIAS` (1–3) — acima de 1, o
que passa por baixo vira o alarme `divergencia_abaixo_do_limiar`.

**Fonte que não mediu ≠ fonte que divergiu.** Timeout, `leitura_mesa_falha` e
`erro_sensor` entram como *fonte indisponível* e não contam para o limiar: elas
não contradizem nada, apenas deixam de confirmar. Contá-las transformaria os ~2%
de falha de leitura em trava por ruído — e trava por ruído é trava desligada em
campo.

> **Operar este sistema exige supervisor disponível.** Com
> `PROB_ERRO_MECANICO=0.01`, uma OS que usa a célula inteira move ~70 unidades:
> medido em execução contínua, **2 de 5 OS travaram**. O orquestrador é um loop
> único — enquanto a trava não sai, nenhuma outra OS anda e a fila enche por
> trás. Isso é a regra funcionando, não defeito.

### Fluxo de uma OS

```
1.  ERP dispara 1 das 10 ordens padrão → POST /api/v1/ordens
2.  Central atribui slots (categoria / residual aproveitável)
3.  Central planeja a rota em serpentina
4.  tara da balança (zera para esta OS)
5.  carregar TODOS os slots (comandos em paralelo)
6.  escanear TODOS os slots com a câmera da fileira (em paralelo)
    ↳ SKU errado → trava + re-scan depois da liberação
    ↳ falha de leitura → alarme, segue
7.  para cada slot na rota, SEQUENCIALMENTE (a mesa é uma só):
      mover CNC → dispensar → câmera da mesa → pesar → Triple Check
8.  homing
9.  OS marcada como concluída no MySQL
10. broadcast WebSocket para dashboard, manutenção e console
```

Os passos 5 e 6 saem em `gather` porque são de SLOTS diferentes; o passo 7 é
sequencial porque a mesa é única — paralelizar ali mandaria a CNC a dois lugares
ao mesmo tempo.

---

## As 10 ordens padrão

As OS **não são sorteadas item a item**. Existem dez ordens fixas em
`central-computer/os_templates.py`; o acaso que sobrou é **qual delas** dispara.
Para quem observa a planta o comportamento continua imprevisível; para quem
apresenta, o conteúdo de cada OS é conhecido de antemão.

| Template | Ordem | Itens | Perfil |
|---|---|---|---|
| `OS-URO-01` | Urologia — ronda noturna | 2 | a mais curta |
| `OS-DOR-01` | Reumatologia / Dor — lote matinal | 3 | |
| `OS-VITAM-01` | Vitaminas — suplementação ambulatorial | 3 | |
| `OS-GASTRO-01` | Gastroenterologia — leito 118 | 4 | meia célula |
| `OS-SNC-02` | Neurologia — leito 207 (reposição) | 4 | reaproveita residual da SNC-01 |
| `OS-CARDIO-01` | Cardiologia — leito 302 | 5 | |
| `OS-SNC-01` | Neurologia — leito 204 | 5 | |
| `OS-LACTO-01` | Intolerância a lactose — kit flora | 6 | |
| `OS-INFECTO-01` | Infectologia — esquema antibiótico | 6 | |
| `OS-GERAL-01` | Carro de emergência — célula cheia | 8 | rota completa |

Os 39 medicamentos citados são itens reais do catálogo. Seis aparecem em mais de
uma ordem, então disparar `OS-SNC-01` e logo depois `OS-SNC-02` reencontra slots
já carregados e o passo 1 do `atribuir_slots` os usa sem limpar.

**Template fixo, `os_id` único a cada disparo** —
`{template_id}-{AAAAMMDDTHHMMSS}-{6 hex}`, por exemplo
`OS-SNC-01-20260814T221104-A3F291`. `ordens.os_id` é UNIQUE e o central recusa
reenvio com 409; sem o sufixo, a segunda vez que uma ordem padrão fosse
disparada seria rejeitada.

Editar as ordens é mexer só na lista `TEMPLATES` (nome do medicamento como está
no catálogo, quantidade entre 2 e 15). A validação roda no import, no boot do
central e no boot do gerador; `GET /api/v1/ordens/templates` serve as dez com o
diagnóstico junto.

---

## Subir a stack

### Pré-requisitos

- Docker Engine ≥ 24 e Docker Compose v2 (`docker compose version`, sem hífen)
- ~2 GB de RAM
- Python 3.10+ (só para os testes e para o painel de bancada)

### Quickstart

```bash
git clone <url-do-repo> && cd valoryapsen
```

Crie o `.env` (modelo abaixo) com as **cinco obrigatórias**, e então:

```bash
docker compose up -d --build
```

A primeira vez leva alguns minutos (build + seed do MySQL). A resposta rápida
para "subiu certo?" é o **pré-voo**: http://localhost:8000/console/prevoo.

### O `.env` da raiz

O compose usa `${VAR:?...}` nas obrigatórias: faltando alguma, ele **recusa
subir** e diz qual. Nenhum segredo tem default.

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # gere a SECRET_KEY
```

```bash
cat > .env <<'FIM'
# ── OBRIGATÓRIAS ─────────────────────────────────────────────────────────────
SECRET_KEY=                 # 64 hex; assina os JWT
MYSQL_ROOT_PASS=
MYSQL_PASS=
SEED_ADMIN_SENHA=           # senha inicial do admin no app de manutenção
SEED_MANUT_SENHA=           # senha inicial do técnico

# ── Banco ────────────────────────────────────────────────────────────────────
# Credenciais lidas UMA vez, na criação do volume mysql_data. Trocar depois
# exige `docker compose down -v` — que apaga os dados.
MYSQL_USER=apsen
MYSQL_DB=apsen_db

# "dev" tolera SECRET_KEY fraca (só avisa). Qualquer outro valor, inclusive
# vazio, IMPEDE o boot com segredo fraco.
APSEN_ENV=prod

# ── Célula ───────────────────────────────────────────────────────────────────
NUM_SLOTS=8

# Origens de browser autorizadas. `*` não é aceito no central.
CORS_ORIGINS=http://localhost:8050,http://localhost:8051

# ── Demonstração (opcionais) ─────────────────────────────────────────────────
MODO_APRESENTACAO=0         # 1 zera TODA falha aleatória dos simuladores
FATOR_VELOCIDADE=1.0        # 0.5 = dobro da velocidade, 2.0 = metade

# ── Console de operação (opcional) ───────────────────────────────────────────
# Vazia = console DESABILITADO (toda rota /console* responde 503). Senha
# PRÓPRIA, independente do login do app de manutenção. Sem default embutido.
CONSOLE_SENHA=
CONSOLE_SESSAO_HORAS=8
FIM
```

### Esperar ficar saudável

```bash
docker compose ps                       # STATUS deve ler "healthy" em todos
docker compose logs -f central-computer
```

Está pronto quando aparecer:

```
MySQL conectado e schema verificado. Medicamentos OK.
[TEMPLATES] 10 ordens padrão válidas contra o catálogo.
Computador Central APSEN v3.1 iniciado.
```

### Atalhos do Makefile

`make help` lista todos.

| Comando | O que faz |
|---|---|
| `make up` | `docker compose up -d` (recusa se faltar o `.env`) |
| `make build` | build incremental |
| `make rebuild` | **`down -v`** + build sem cache + `up -d` — ⚠️ **APAGA O BANCO** |
| `make down` | derruba containers e rede, **mantém** o banco |
| `make restart` | recria os containers, mantém o banco |
| `make ps` / `make logs` | estado / log agregado |
| `make log-central`, `make log-erp`, … | log de um serviço |
| `make shell-central` / `make shell-mysql` | shell no container |
| `make config` | valida o compose sem subir |
| `make test` / `make lint` | suíte / pyflakes |

> **`make rebuild` não é um `make build` mais forte.** Ele começa com
> `docker compose down -v`, e o `-v` remove o volume `mysql_data` — catálogo,
> usuários, ordens e histórico vão junto. Para reconstruir sem perder o banco:
> `docker compose build --no-cache && docker compose up -d`.

### Rebuild: o que recriar depois de mudar o quê

A pergunta que decide é **se o banco pode ser perdido**.

| Mudou | Comando | Banco |
|---|---|---|
| Só código Python | `docker compose up -d --build` | preservado |
| Um serviço só | `docker compose up -d --build central-computer` | preservado |
| `requirements.txt` | `docker compose build --no-cache <serviço>` + `up -d` | preservado |
| `.env` (variável de runtime) | `docker compose up -d` | preservado |
| `.env` (credencial do MySQL) | `docker compose down -v` + `up -d --build` | **APAGADO** |
| Schema de versão antiga quebrado | `docker compose down -v` + `up -d --build` | **APAGADO** |
| Nome ou `container_name` | `docker compose down` + `up -d --build` | preservado |

`console.html`, `console_login.html` e `console_prevoo.html` são lidos do disco a
cada requisição: editá-los vale com `docker compose restart central-computer`.

### Derrubar

```bash
docker compose stop     # pausa; mantém banco e containers
docker compose down     # remove containers e rede, MANTÉM o banco
docker compose down -v  # remove tudo, inclusive o banco
```

### Problemas comuns

| Sintoma | Causa | O que fazer |
|---|---|---|
| `compose: variable is not set` | falta variável no `.env` | a mensagem diz qual — o `:?` é intencional |
| central sai no boot com `ConfiguracaoInsegura` | `SECRET_KEY` fraca fora de `APSEN_ENV=dev` | gerar chave de 64 hex |
| central em restart loop, log `SchemaInvalido` | banco de versão anterior | `docker compose down -v` e subir de novo |
| `MySQL não disponível (n/30)` | MySQL ainda subindo | normal no primeiro boot; ~30 s |
| dashboard em branco | central ainda não `healthy` | `docker compose ps` |
| `/console` responde 503 | `CONSOLE_SENHA` vazia | definir no `.env` e reiniciar o central |
| OS recusada com 429 | fila no teto (`MAX_FILA_OS=5`) | ver `/api/v1/trava` — quase sempre há trava ativa |
| container órfão depois de renomear serviço | `container_name` mudou | `docker compose down` antes, ou `up -d --remove-orphans` |

---

## Operar

### Console de operação — http://localhost:8000/console

A mesa de onde se escolhe **qual** das dez ordens entra e **quando**. Existe para
a hora da apresentação, quando o sorteio do ERP — que é o que dá naturalidade à
planta rodando sozinha — passa a atrapalhar quem precisa mostrar um caso
específico.

| Ação | Detalhe |
|---|---|
| Listar as 10 ordens | nome, categoria, itens, quantos slots ocupa |
| Disparar qualquer uma | um clique, sem confirmação — `os_id` novo a cada disparo |
| Pausar / retomar o automático | assume o controle sem competir com o ERP |
| Estado ao vivo | OS ativa, fila, trava (com motivo e slot) e os 8 dispensers |
| Liberar a trava | **com confirmação** |
| **Armar uma falha** | tipo + slot; dispara uma vez e se desarma sozinha |
| **Resetar a planta** | **com confirmação** |
| **Semear histórico** | **com confirmação** — dado de DEMONSTRAÇÃO |
| **Pré-voo** | tela à parte, em `/console/prevoo` |

**Acesso.** Rota discreta (fora do Swagger), senha `CONSOLE_SENHA` do `.env` —
própria do console, independente do JWT do app de manutenção. Sessão em cookie
`HttpOnly` assinado por HMAC, válida por `CONSOLE_SESSAO_HORAS` e restrita ao
path `/console`; trocar a senha invalida na hora as sessões abertas. Cinco erros
em um minuto bloqueiam a origem por um minuto.

> **Sem `CONSOLE_SENHA` o console não existe**: toda rota `/console*` responde
> **503** com a instrução de definir a variável. Não há senha default embutida —
> ela estaria versionada aqui e abriria o disparo de OS para quem lesse o
> repositório.

O disparo manual **é** o `POST /api/v1/ordens`: o console monta o corpo com
`os_templates.instanciar` e chama a mesma função do endpoint, então 409, 429 e
503 chegam à tela porque é o mesmo código respondendo.

**A pausa é um flag que o ERP consulta** (`GET /api/v1/gerador`), não um
`docker stop`: o container segue de pé e volta a produzir no instante em que o
console despausa. Não é persistida — restart do central retoma o automático.
Pausar **não** bloqueia o disparo manual.

### Modo apresentação

Dois controles globais no `.env`, válidos para o central e os quatro simuladores
de uma vez. São env vars: `docker compose up -d` basta, sem rebuild.

```bash
MODO_APRESENTACAO=1     # planta determinística: nenhuma falha aleatória
FATOR_VELOCIDADE=2.0    # metade da velocidade, para narrar cada etapa
```

`MODO_APRESENTACAO=1` zera **probabilidade de falha**, não aleatoriedade em
geral: erro mecânico, falhas e divergências das 3 câmeras, erro do HX711 e o
`RUIDO_G` da balança vão a zero; confiança da leitura, jitter de temperatura e
horas de uso continuam variando (é textura, não falha). O que ele **não**
desliga é a detecção — a balança segue divergindo quando a quantidade que caiu
na mesa não bate com a esperada.

`FATOR_VELOCIDADE` multiplica todo `T_*`, **divide** `VEL_MM_S` (velocidade é o
inverso de tempo) e escala os `TIMEOUT_*` do central por `max(1, fator)` —
desacelerar sem isso abortaria a OS por `timeout_carregamento` culpando o
dispenser, que fez exatamente o que se pediu. `INTERVALO_OS` do ERP não escala:
é a cadência com que se *quer* ordens.

### Injeção de falha sob demanda

Sem ela, o Triple Check só aparece quando o sorteio colabora. No console você
arma **um** gatilho (tipo + slot); ele dispara na próxima ocorrência aplicável e
se desarma sozinho.

| Tipo | Efeito | Trava? |
|---|---|---|
| `sku_dispenser` | a câmera da fileira lê um SKU que não bate | **sim** — e entra no laço de re-scan |
| `falha_leitura_dispenser` | a câmera não consegue ler | não — alarme, a OS continua |
| `divergencia_mesa` | a câmera da balança conta uma a menos | **sim** |
| `divergencia_peso` | o HX711 mede fora da tolerância | **sim** |
| `falha_mecanica_dispenser` | o dispenser solta uma unidade a menos | **sim**, pela balança |

O último é o mais completo de mostrar: a falha é FÍSICA e a balança a detecta
sozinha — uma unidade de 15 já é 6,7% contra 5% de tolerância. A trava nasce de
um erro real, não de um sensor mentindo.

**Funciona com `MODO_APRESENTACAO` ligado** — o modo desliga o acaso, a injeção
liga o que você escolheu; são controles independentes. Nada persiste, e o log
diz de forma inequívoca quando a falha foi provocada (o evento carrega
`falha_injetada: true`).

### Reset rápido

O botão **Resetar** devolve a bancada ao estado de boot em segundos, sem
`down -v`: esvazia a fila (as OS que esperavam são **canceladas** no banco),
libera a trava, desarma a injeção, manda `cmd_limpar` nos 8 slots e **espera
confirmação**, zera a balança e leva a CNC para HOME. Apagar o histórico é opção
à parte, e o default é **preservar**. Catálogo, usuários e log de manutenção
nunca são apagados.

> **Recusa (409) enquanto houver OS em execução.** Resetar por cima mandaria
> limpar um slot que está dispensando, zeraria a balança no meio de uma pesagem
> e abortaria a OS por uma divergência que ninguém provocou. Com a trava ativa,
> libere-a primeiro — o botão está ao lado.

### Histórico de demonstração

> ⚠️ **Dado fabricado.** Nada disto aconteceu na planta.

O botão **Semear** cria N ordens concluídas nos últimos D dias, com dispensas,
alarmes já resolvidos e curvas de temperatura — para o dashboard não abrir vazio
numa instalação nova. Tudo coerente: as OS saem das dez ordens padrão, o SKU vem
do catálogo real, os horários caem no turno e a duração cresce com o número de
slots. **Todo `os_id` semeado começa com `DEMO-`**, então dado de demonstração e
operação real nunca se confundem. Nunca roda sozinho — só pela rota do console.

### Pré-voo — http://localhost:8000/console/prevoo

A tela que se lê cinco minutos antes de apresentar. Responde "pronto" ou "N itens
impedem a apresentação" em cima, e a lista item a item abaixo.

| Grupo | Itens |
|---|---|
| Serviços | os 13 do compose — 11 sondados em `/ping`, o central por dentro, o ERP como informação |
| Banco | MySQL conectado, schema completo, catálogo populado, os 8 slots |
| Ordens | as 10 ordens válidas contra o catálogo; ocupação da fila |
| Célula | trava, posição da CNC, OS em execução, alarmes abertos |
| Modo | apresentação ou realista, fator de velocidade, falha armada, clientes WS |

**Todo item vermelho ou amarelo diz o que fazer** — é o que separa a tela de um
relatório. **Amarelo não derruba o veredito**: modo apresentação ligado ou CNC
fora de HOME são informações, não impedimentos. Responde em ~2 s mesmo com
serviço morto (as sondas correm em paralelo), que é justamente quando ela mais
importa.

### Tela de necessidades — primeira aba de :8051

A lista do que precisa de alguém agora, em ordem de **impacto**:

| # | O quê | Por quê aqui |
|---|---|---|
| 1 | trava do Triple Check | bloqueia a produção **agora** — o loop é único |
| 2 | fila no teto | a planta já está recusando ordens com 429 |
| 3 | alarmes abertos, agrupados por fonte | já falhou, ninguém fechou |
| 4 | componentes acima do limiar | vai falhar, ainda não falhou |
| 5 | dispensers com resíduo parado | estoque imobilizado |
| 6 | OS em erro nas últimas horas | já acabou — é diagnóstico |

Cada linha tem um botão que leva à aba onde se resolve. Os limiares (65 °C, 80%
de desgaste) são os mesmos com que as outras abas já pintam de vermelho. Com o
banco fora, a tela **avisa que a lista está incompleta** em vez de dizer que está
tudo em dia.

---

## Roteiro de demonstração

**1. Está vivo?**

```bash
curl http://localhost:8000/ping
curl http://localhost:8000/estado        # cnc, 8 dispensers, visão, peso, trava
curl http://localhost:8000/api/v1/fila   # {"tamanho":0,"capacidade":5,...}
```

**2. Uma OS de ponta a ponta.** O ERP posta sozinho a cada 90 s. Para não
esperar, abra o `/console`, escolha uma das 10 e dispare, com o dashboard aberto
em outra aba. Por curl:

```bash
curl -X POST http://localhost:8000/api/v1/ordens \
  -H "Content-Type: application/json" \
  -d '{"os_id":"OS-TESTE-001","descricao":"Teste manual","categoria":"snc",
       "medicamentos":[{"medicamento":"ALOIS 10MG","sku":"ALOIS 10MG CX C/7 CP","categoria":"snc","quantidade":5}]}'
```

Contrato: `200` aceita · `409` `os_duplicada` · `429` `fila_cheia` · `503`
`persistencia_indisponivel`.

```bash
docker compose logs -f central-computer | grep ORCH
curl http://localhost:8000/os/OS-TESTE-001
```

**3. A trava do Triple Check.** É o comportamento mais importante de mostrar.
Arme a falha no console (não espere o sorteio) e acompanhe:

```bash
curl http://localhost:8000/api/v1/trava     # {"ativa":true,"os_id":...,"motivo":...}
```

Liberar, pelos dois portões — o botão do console (com confirmação) ou o JWT:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","senha":"<SEED_ADMIN_SENHA>"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['token'])")

curl -X POST http://localhost:8000/api/v1/admin/liberar-trava \
  -H "Authorization: Bearer $TOKEN"
```

A OS retoma de onde parou.

---

## Testes

Não precisam de Docker, MySQL, uvicorn nem porta serial: importam os módulos por
caminho e substituem as bordas.

```bash
python -m pip install -r tests/requirements-dev.txt
python -m pytest tests/ -q
```

Vale conhecer os que vigiam invariantes em vez de comportamento — são eles que
pegam a regressão que nenhum outro pega:

| Teste | O que ele impede |
|---|---|
| `test_schema.py` | query referenciando tabela/coluna que o DDL não tem (varre por AST) |
| `test_db_pool.py` | função de banco voltando a abrir conexão fora do pool |
| `test_compose.py` | healthcheck removido, ciclo de dependência, porta duplicada, `devices:` ativo |
| `test_orchestrator.py` | banco chamado no event loop; serpentina pior que a permutação ótima |
| `test_adapters.py` | os quatro `_post_central` divergindo na política de retry |
| `test_serial_link.py` | serial bloqueando o event loop; as 3 cópias de `serial_link.py` divergindo |
| `test_protocolo_serial.py` | contrato do display divergindo entre C++, backend e simulador |
| `test_protocolo_placas.py` | contrato serial divergindo entre `docs/`, adapters e placas falsas |
| `test_seguranca.py` | rota conferindo JWT sem revalidar no banco |
| `test_limites.py` | `?limite=` cru indo para o `LIMIT` do MySQL |

---

## Painel de bancada

`painel_operador/` — a tela de 7" com touch que o **operador de chão de fábrica**
usa, ao lado da célula, mais uma interface web. Lista as ordens, mostra o estoque
de cada dispenser, autentica por PIN, registra histórico e abre desvios.

**O espelho é de mão única.** O painel **lê** do central (`/os/historico`,
`/os/{os_id}`, `/dispensers/estado`, sem autenticação) e **nunca** comanda a
célula. O caminho de escrita óbvio seria `PUT /ordens/{os_id}/status`, e ele é
exatamente o errado: só grava a coluna do banco, sem falar com o orquestrador —
um botão "Iniciar" ali mudaria a linha enquanto a célula faz outra coisa. Um
espelho que escreve é um espelho que mente.

Ele roda **fora do Docker**, porque é dono de uma porta USB, e
`PAINEL_CENTRAL=0` desliga a integração inteira: a bancada precisa funcionar com
o central desligado — em feira, em treinamento e no dia em que o Docker não sobe.

```bash
cd painel_operador/backend
python app.py          # :5000
```

Instalação, gravação do display ESP32-S3, protocolo serial, PIN, banco SQLite e
as armadilhas conhecidas estão em **[`docs/BANCADA.md`](docs/BANCADA.md)**.

---

## Interfaces e segredos

| Interface | URL | Acesso |
|---|---|---|
| Dashboard | http://localhost:8050 | público |
| Manutenção e operação | http://localhost:8051 | JWT |
| Console de operação | http://localhost:8000/console | `CONSOLE_SENHA` |
| Pré-voo | http://localhost:8000/console/prevoo | `CONSOLE_SENHA` |
| API + Swagger | http://localhost:8000/docs | REST/WS |
| Painel de bancada | http://localhost:5000 | PIN (hash) |
| API do painel | http://localhost:5000/api/* | `X-API-Token` |

**Usuários seed do app de manutenção:** `admin` (`SEED_ADMIN_SENHA`) e `manut1`
(`SEED_MANUT_SENHA`). Criados só na primeira inicialização do banco. **Troque as
duas após o primeiro login**, pela aba Usuários — elas ficam em claro no `.env`.

### Segredos

Nada de segredo entra no repositório. O compose lê tudo do `.env` (ignorado pelo
git) e **falha ao subir** se faltar algo.

| Variável | Para quê |
|---|---|
| `SECRET_KEY` | assina os JWT. 64 hex |
| `MYSQL_ROOT_PASS`, `MYSQL_PASS` | banco (lidas na criação do volume) |
| `SEED_ADMIN_SENHA`, `SEED_MANUT_SENHA` | senhas iniciais do app de manutenção |
| `APSEN_ENV` | `prod` (default) recusa segredo fraco; `dev` só avisa |
| `CONSOLE_SENHA` | console de operação. **Opcional** — vazia = console desabilitado (503) |
| `APSEN_SECRET` | sessão do painel de bancada. **Sem default** — sem ela o painel não sobe |
| `APSEN_API_TOKEN` | header `X-API-Token` das rotas `/api/*` do painel. **Sem default** — ausente, elas respondem 503 e o resto do painel sobe |

O central **se recusa a subir** com `SECRET_KEY` default, vazia ou com menos de
32 caracteres, a não ser com `APSEN_ENV=dev`. O valor default antigo está no
histórico público deste repositório: com ele qualquer pessoa forja um token
`role=admin` e libera a trava do Triple Check.

---

## Endpoints

```
GET  /ping                               health check (é o que o compose usa)
GET  /health                             diagnóstico: upstreams e, nos adapters,
                                         o estado da porta serial
GET  /estado                             estado completo em memória
GET  /os/ativa | /os/historico | /os/{id}
GET  /medicamentos                       catálogo (96 medicamentos)
GET  /dispensers/estado                  os 8 slots, no banco
GET  /alarmes                            alarmes ativos/resolvidos
WS   /ws                                 push de estado em tempo real

POST /api/v1/ordens                      nova OS — 409 os_duplicada
                                         | 429 fila_cheia | 503 persistencia_indisponivel
GET  /api/v1/fila                        ocupação da fila (backpressure)
GET  /api/v1/gerador                     flag de pausa do ERP
GET  /api/v1/ordens/templates            as 10 ordens + diagnóstico
GET  /api/v1/trava                       estado da trava
POST /api/v1/eventos/{dispenser|cnc|visao|peso}    eventos vindos dos adapters

POST /auth/login | GET /auth/me
POST /api/v1/admin/liberar-trava         admin/supervisor
GET  /manutencao/necessidades            lista priorizada (agregado)
GET  /manutencao/alarmes | PUT /manutencao/alarmes/{id}/resolver
POST /manutencao/dispensers/{id}/limpar
GET  /manutencao/log
GET  /api/v1/visao/historico
GET  /api/v1/relatorio/os/{os_id}?formato=csv|xlsx
```

### Contrato de entrada de uma OS

`POST /api/v1/ordens` é a única porta de entrada de ordem — o disparo manual do
console chama a mesma função. A regra é **nenhuma OS entra na fila sem linha no
banco**: a fila é o que dispensa medicamento, o banco é o que registra o que foi
dispensado, e aceitar uma sem a outra produz os dois piores resultados do
sistema.

| Status | Corpo | Quando | O gerador retenta? |
|---|---|---|---|
| `200` | `{"aceita": true, "posicao_fila": N}` | aceita e enfileirada | — |
| `409` | `{"erro": "os_duplicada", "os_id": ...}` | `os_id` já registrado (`INSERT IGNORE` não inseriu) | **não** — reenviar daria 409 para sempre, e reprocessar seria dose dobrada no leito |
| `429` | `{"erro": "fila_cheia", "os_id": ...}` | `MAX_FILA_OS` OS já esperando. **Nada é persistido** — a recusa vem ANTES do INSERT | **não** — a próxima OS já nasce com `os_id` novo |
| `503` | `{"erro": "persistencia_indisponivel", "os_id": ...}` | banco fora do ar; a OS **não** entra na fila | **não** — encheria o log enquanto o banco está fora |

Os corpos seguem a forma `{"erro": ..., "os_id": ...}` e por isso são
`JSONResponse`: `HTTPException` embrulharia tudo em `detail`. A declaração
executável está no `responses=` do endpoint, em `central-computer/main.py`, e sai
no Swagger; `tests/test_api_ordens.py` prende os quatro casos.

> Entre **não dispensar** e **dispensar sem rastro**, um sistema de medicação
> escolhe não dispensar. Com o banco fora, a OS dispensada não teria linha em
> `ordens`/`os_itens`: `atualizar_item_os` e `atualizar_status_ordem` não
> achariam o que atualizar e o relatório sairia vazio.

O console tem rotas próprias, **fora do Swagger** e atrás do cookie de sessão:

```
GET  /console | /console/login | /console/prevoo
POST /console/login | /console/logout
POST /console/api/disparar               chama o MESMO receber_ordem do POST /api/v1/ordens
POST /console/api/gerador                pausa/retoma o ERP
POST /console/api/liberar-trava
GET  /console/api/injecao                tipos + gatilho armado
POST /console/api/injecao | /console/api/injecao/desarmar
POST /console/api/reset                  409 com OS em execução
POST /console/api/seed                   histórico de DEMONSTRAÇÃO
GET  /console/api/prevoo                 o relatório item a item
```

---

## Estrutura do repositório

```
valoryapsen/
├── README.md               # este arquivo
├── CLAUDE.md               # decisões de arquitetura e armadilhas
├── TASKS.md                # backlog
├── docs/
│   ├── PROTOCOLO_SERIAL.md # contrato adapter ↔ firmware (uma linha JSON por mensagem)
│   └── BANCADA.md          # painel de bancada + display ESP32
├── central-computer/
│   ├── main.py             # FastAPI, handlers de evento, rotas do console
│   ├── orchestrator.py     # fila, atribuição de slots, rota, Triple Check, trava
│   ├── database.py         # MySQL — DDL, pool, seeds (fonte única do schema)
│   ├── os_templates.py     # as 10 ordens padrão
│   ├── console.py          # senha, sessão HMAC, flag de pausa
│   ├── injecao.py          # gatilho de falha (um por vez, check-and-pop)
│   ├── necessidades.py     # a lista priorizada (pura)
│   ├── prevoo.py           # o relatório de pré-voo (puro)
│   ├── seed_demo.py        # histórico fabricado (puro)
│   ├── config.py  auth.py  console*.html
├── dispenser-adapter/      # :8100 — main.py + serial_link.py
├── cnc-adapter/            # :8101 — main.py + serial_link.py
├── weight-adapter/         # :8103 — main.py + serial_link.py
├── vision-adapter/         # :8102 — só HTTP
├── dispenser_simulator/    # :8201 — os 8 dispensers
├── cnc_simulator/          # :8200 — a mesa
├── vision-simulator/       # :8202 — as 3 câmeras
├── weight-simulator/       # :8203 — o HX711
├── erp-simulator/          # o ERP que emite as OS (sem porta)
├── dashboard/              # Dash read-only :8050
├── manut_web/              # Dash manutenção e operação :8051
├── painel_operador/        # FORA do Docker — backend Flask + firmware do display
├── mysql/init.sql          # só charset/collation; o schema vem do database.py
├── tests/                  # pytest — sem Docker, sem MySQL, sem porta física
│   └── fakes/              # placas falsas: o firmware, por socket://
├── docker-compose.yml  Makefile  .gitignore
```

**O schema tem uma fonte só, e é o `database.py`.** O `init.sql` roda uma vez na
criação do volume; o `init_db()` roda em todo startup. Objeto que existisse só no
`init.sql` sumiria num `down` sem `-v` — e o código seguiria consultando o que
não existe.

---

## Variáveis de ambiente

> `MODO_APRESENTACAO` e `FATOR_VELOCIDADE` **mandam por cima**: com o modo
> ligado, toda probabilidade desta tabela vale zero, inclusive os overrides
> `_ESQ`/`_DIR` e o `RUIDO_G`.

### Central e simuladores (`.env` + `docker-compose.yml`)

| Variável | Padrão | Serviço |
|---|---|---|
| `MODO_APRESENTACAO` | `0` — `1` zera TODA falha aleatória | central + 4 simuladores |
| `FATOR_VELOCIDADE` | `1.0` (faixa 0.1–10) | central + 4 simuladores |
| `NUM_SLOTS` | `8` (par, 2 fileiras) | todos |
| `SECRET_KEY` / `APSEN_ENV` | obrigatória / `prod` | central |
| `CORS_ORIGINS` | `http://localhost:8050,http://localhost:8051` | central |
| `AUTH_CACHE_TTL_S` | `30` s de cache da revalidação de token | central |
| `MYSQL_POOL_MAX` | `8` conexões guardadas (1–64) | central |
| `CONSOLE_SENHA` / `CONSOLE_SESSAO_HORAS` | vazia (desabilita) / `8` h | central |
| `TRIPLE_CHECK_MIN_DIVERGENCIAS` | `1` (1–3) | central |
| `MAX_FILA_OS` | `5` OS esperando | central |
| `TIMEOUT_CARREGAMENTO` / `_POSICIONAMENTO` / `_DISPENSA` | `180` / `120` / `120` s | central |
| `TIMEOUT_VISAO_DISPENSER` / `_VISAO_MESA` / `_PESO` | `30` / `30` / `15` s | central |
| `BROADCAST_MIN_INTERVALO_MS` | `500` ms entre broadcasts periódicos | central |
| `CNC_AMOSTRAGEM_MOVENDO` | `0` (não grava trajetória; N = 1 em N) | central |
| `RETENCAO_DIAS` / `EXPURGO_INTERVALO_HORAS` | `30` d / `24` h | central |
| `<SUB>_TRANSPORTE` | `http` (`serial` = firmware) | dispenser/cnc/weight-adapter |
| `<SUB>_SERIAL_URL` / `_BAUD` / `_ACK_TIMEOUT_S` | vazia / `115200` / `2` s | dispenser/cnc/weight-adapter |
| `T_CARGA_UNID` / `T_DISPENSA_UNID` | `0.3` / `0.5` s por unidade | dispenser-sim |
| `PROB_ERRO_MECANICO` | `0.01` | dispenser-sim |
| `PROB_FALHA_LEITURA_DISPENSER` / `PROB_DIVERGENCIA_DISPENSER` | `0.02` — as duas fileiras | vision-sim |
| `T_SCAN_DISPENSER` | `1.5` s — as duas fileiras | vision-sim |
| `..._ESQ` / `..._DIR` | herdam o valor acima; sobrescrevem UMA câmera | vision-sim |
| `PROB_FALHA_LEITURA_MESA` / `PROB_DIVERGENCIA_MESA` / `T_SCAN_MESA` | `0.02` / `0.02` / `2.0` s | vision-sim |
| `TOLERANCIA_PERC` | `5.0` % | weight-sim |
| `PROB_ERRO_SENSOR` / `T_LEITURA` / `T_TARA` / `RUIDO_G` | `0.01` / `1.5` s / `0.5` s / `2.0` g | weight-sim |
| `VEL_MM_S` / `HOME_X` / `HOME_Y` | `80` mm/s / `-120` / `0` | cnc-sim |
| `INTERVALO_OS` | `90` s entre OS | erp-simulator |
| `ESPERA_FILA_CHEIA` / `MAX_ESPERAS_FILA` | `20` s / `15` esperas | erp-simulator |
| `RELOAD_CATALOGO_MIN` / `ESPERA_PAUSA` | `30` min / `10` s | erp-simulator |

### Painel de bancada (fora do Docker)

| Variável | Padrão | Para quê |
|---|---|---|
| `CENTRAL_URL` | `http://localhost:8000` | onde o central atende (a porta **publicada** — não o nome DNS do Docker) |
| `CENTRAL_TIMEOUT_S` / `CENTRAL_SYNC_S` | `3` / `5` s | teto de cada requisição / intervalo do espelho |
| `PAINEL_CENTRAL` | `1` | `0` desliga a integração inteira |
| `APSEN_DB` | `backend/apsen.db` | caminho do banco |
| `APSEN_SECRET` | **sem default** | chave de sessão; ausente ou fraca, o painel não sobe |
| `APSEN_API_TOKEN` | **sem default** | token das rotas `/api/*`; ausente, elas respondem 503 |
| `APSEN_DEBUG` | `0` | `1` liga o Werkzeug e **força o bind em 127.0.0.1** |
| `APSEN_HOST` / `APSEN_PORTA` | `0.0.0.0` / `5000` | |
| `APSEN_RELOADER` | `0` | deixe desligado com o display conectado |

---

## Concorrência e segurança

**Event loop nunca bloqueado.** Todo I/O de MySQL passa por `asyncio.to_thread`,
e o serial dos adapters roda numa thread leitora dedicada por porta — as duas
regras têm varredura por AST na suíte. Snapshot de estado é `copy.deepcopy` sob
o lock: a cópia rasa deixava os dicionários aninhados vivos durante a
serialização, fora do lock.

**Autenticação.** JWT + bcrypt, com **revalidação no banco a cada requisição**
(cache de `AUTH_CACHE_TTL_S`): o token não é a palavra final, usuário desativado
perde acesso na hora e a `role` que vale é a do banco. `SECRET_KEY` default
impede o boot. CORS é lista explícita, não `*`. `?limite=` passa por clamp antes
do `LIMIT`.

**Console e painel.** O console tem senha própria e sessão HMAC `HttpOnly`, e
fica inteiro desabilitado sem `CONSOLE_SENHA` — sem senha default embutida. No
painel de bancada, `APSEN_SECRET` sem default (o processo recusa subir), rotas
`/api/*` atrás de `X-API-Token`, PIN de operador em hash e conferido pelo
backend (o display não guarda credencial, nem no cartão SD), e `debug` só com
`APSEN_DEBUG=1` — e aí escutando apenas em `127.0.0.1`.

**Infra.** Healthcheck em todos os serviços de aplicação, `depends_on` por
`condition: service_healthy` (ordem de start não é prontidão), pool de conexões
MySQL com verificação de conexão ociosa, e retry com o **mesmo critério** nos
dois sentidos da ponte: retenta 5xx, 408 e 429; recusa determinística (409, 422)
não é retentada.

**Segurança operacional.** Triple Check com trava garante intervenção humana em
qualquer divergência; toda saída do ciclo de uma OS fecha em status terminal e
devolve os slots ao pool; OS não entra na fila sem linha no banco (entre não
dispensar e dispensar sem rastro, um sistema de medicação escolhe não
dispensar).
