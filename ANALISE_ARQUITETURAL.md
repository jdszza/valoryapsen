# Análise Arquitetural — APSEN Sistema de Contagem
**Data:** 2026-06-24  
**Base:** Código atual + documento SISTEMA APSEN.pdf + fluxograma operacional

---

## 1. Diagnóstico — Problemas Identificados

### 1.1 Inteligência de negócio no lugar errado (crítico)

O problema mais grave do sistema atual: **o Computador Central é passivo e os simuladores é que tomam as decisões**.

**`dispenser_simulator/simulator.py`** contém:
- `_atribuir_slots()` (linha 119) — IA de roteamento: decide qual slot recebe qual medicamento. Isso é IA de Decisão — deveria estar no Computador Central.
- `_processar_os_unica()` (linha 315) — orquestra todo o fluxo de uma OS: carregamento, dispensa, conclusão. Isso é Controle de Fluxo — deveria estar no Computador Central.
- `_processar_fila()` (linha 198) — gerencia a fila FIFO de OS. Isso é Gerenciamento de Ordens — deveria estar no Computador Central.

O dispenser simulator atualmente é o Computador Central disfarçado de hardware.

**`cnc_simulator/simulator.py`** contém:
- `_nearest_neighbor()` (linha 81) — algoritmo de cartonização para otimizar ordem de visita aos dispensers. Isso é otimização de rota — deveria estar no Computador Central.
- Assina `apsen/ia/atribuicao` e sabe quais dispensers têm quais medicamentos. Um firmware CNC real nunca deveria ter esse conhecimento.
- Controla quando "aguardar todos os dispensers prontos" — coordenação de fluxo que pertence ao Computador Central.

**`backend/main.py`** se autodeclara como:
> "Bridge MQTT → MySQL + API REST"
> "O backend NÃO controla nenhum sistema."

Exatamente o problema. O Computador Central deve controlar tudo. Hoje ele apenas escuta e espelha.

---

### 1.2 MQTT como barramento sem contrato formal

- O backend assina `apsen/#` — consome qualquer publicação de qualquer container.
- A coordenação entre módulos é **implícita**: CNC aguarda o tópico `apsen/dispenser/status` com `status="pronto"`. Se o campo vier com typo ou o tópico mudar, o sistema trava silenciosamente.
- Sem validação de schema. Qualquer container pode publicar qualquer coisa em qualquer tópico.
- A substituição por REST/HTTP cria contratos explícitos (body tipado, status codes, timeouts).

---

### 1.3 Acoplamentos indevidos entre simuladores

**`cnc_simulator` assina `apsen/ia/atribuicao`**: o firmware CNC real nunca deveria receber dados de atribuição de IA. Ele só deveria receber o comando "mova para o dispenser X". Ao expor a atribuição completa da OS para o CNC, qualquer mudança na lógica da IA exige atualizar o firmware.

**`dispenser_simulator` assina `apsen/os/nova`** e recebe a OS completa: o firmware do dispenser real nunca receberia uma OS. Ele receberia apenas "carregue este medicamento nesta quantidade". A separação hoje é zero — o simulador age como se fosse o ERP, a IA e o dispenser ao mesmo tempo.

**Comunicação lateral entre simuladores** (sem mediação do Computador Central):
- CNC publica `posicionado` → dispenser lê e dispensa
- Dispenser publica `concluido` → CNC avança para o próximo slot

O Computador Central não participa dessa orquestração. Ele apenas assiste. Isso viola completamente a arquitetura centralizada.

---

### 1.4 Fila de OS duplicada e dessincronizada

| Local | Fila |
|---|---|
| `dispenser_simulator._os_queue` | Fila real — controla o processamento |
| `backend._estado["fila_os"]` | Sombra — derivada de eventos MQTT, frequentemente desincronizada |

A fonte de verdade da fila está no simulador, não no Computador Central. O backend não sabe quantas OS estão na fila — ele tenta inferir ouvindo MQTT.

---

### 1.5 sap_simulator usa dois protocolos simultaneamente

Consulta `/medicamentos` via REST (correto) mas publica a OS via MQTT. Deveria usar apenas REST: um `POST /ordens` para o Computador Central. Simples, rastreável, com resposta de confirmação.

---

## 2. O que Manter (não tocar)

| Arquivo | Por quê manter |
|---|---|
| `backend/database.py` | Camada de persistência completa e funcional. Mover para `central-computer/`. |
| `backend/auth.py` | JWT, criação de token, hash de senha — OK. |
| `backend/main.py` | Endpoints REST, WebSocket manager, estrutura FastAPI — tudo válido. Remover apenas o MQTT client. |
| `dashboard/app.py` | Estrutura Dash OK. Já consome via polling REST/WebSocket do backend. |
| `ihm_web/app.py` | Estrutura OK, autenticação JWT, callbacks — manter. |
| `dispenser_simulator` | Lógica de hardware: timing de carga/dispensa, temperatura, falhas mecânicas, simulação de QR code. |
| `cnc_simulator` | Lógica de movimento: posições XY, interpolação, telemetria de temperatura e desgaste. |
| `sap_simulator` | Lógica de geração de OS: catálogo, intervalos, seleção por categoria. Remover apenas o MQTT. |
| `mysql/init.sql` | Schema do banco OK. |
| `docker-compose.yml` | Base OK — adicionar serviços, remover `mosquitto`. |

---

## 3. Arquitetura Proposta

### 3.1 Mapa de containers (novo docker-compose)

```
┌─────────────────────────────────────────────────────────────┐
│                      apsen-net (bridge)                      │
│                                                              │
│  order-generator ──────────────────────────────────────────► │
│  (era: sap_simulator)  POST /ordens    central-computer      │
│                                        (era: backend)        │
│                                        porta 8000            │
│                                        │                     │
│  cnc-simulator ◄──── cnc-adapter ◄────►│                    │
│  (hardware sim)  HTTP    porta 8101    │                     │
│                                        │                     │
│  dispenser-simulator ◄ dispenser-adapter ◄►│                │
│  (hardware sim)    HTTP    porta 8100  │                     │
│                                        │                     │
│  vision-simulator ◄── vision-adapter ◄►│                    │
│  (futuro)          HTTP    porta 8102  │                     │
│                                        │                     │
│  dashboard  ◄─────── WebSocket /ws ───►│  mysql             │
│  porta 8050          GET /api/*        │  porta 3306        │
│                                        │                     │
│  ihm_web    ◄─────── REST + JWT ──────►│                    │
│  porta 8051                                                  │
└─────────────────────────────────────────────────────────────┘
```

**Sem `mosquitto`.** Toda comunicação via HTTP/WebSocket na rede Docker.

### 3.2 Responsabilidades corrigidas

| Container | Responsabilidade |
|---|---|
| `central-computer` | Toda a inteligência: fila de OS, IA de atribuição de slots, rota CNC, orquestração de carregamento e dispensa, Triple Check, persistência, WebSocket para dashboard |
| `order-generator` | Gerar OS aleatórias e enviar via POST. Sem lógica de negócio. |
| `cnc-adapter` | Receber comandos do central-computer, repassar ao cnc-simulator, receber eventos do cnc-simulator, repassar ao central-computer |
| `dispenser-adapter` | Idem para dispensers |
| `vision-adapter` | Idem para câmeras |
| `cnc-simulator` | Simular apenas hardware: movimentação, posicionamento, temperatura. Sem saber de OS ou IA. |
| `dispenser-simulator` | Simular apenas hardware: timing de carga/dispensa, sensores, falhas mecânicas. Sem saber de OS ou IA. |
| `dashboard` | Exibir estado via WebSocket. Sem regras de negócio. |
| `ihm_web` | Interface de manutenção com JWT. Sem regras de negócio. |

---

## 4. Contratos de Comunicação

### 4.1 order-generator → central-computer

```http
POST http://central-computer:8000/api/v1/ordens
Content-Type: application/json

{
  "os_id": "OS-2024-001",
  "descricao": "Separação lote A",
  "categoria": "Analgésicos",
  "medicamentos": [
    {"medicamento": "Dipirona 500mg", "sku": "DIP500", "categoria": "Analgésicos", "quantidade": 10},
    {"medicamento": "Ibuprofeno 400mg", "sku": "IBU400", "categoria": "Analgésicos", "quantidade": 5}
  ]
}

Response 200:
{"aceita": true, "os_id": "OS-2024-001", "posicao_fila": 1}

Response 409 (OS já existe):
{"erro": "os_duplicada", "os_id": "OS-2024-001"}
```

### 4.2 Eventos: dispenser-adapter → central-computer

```http
POST http://central-computer:8000/api/v1/eventos/dispenser
Content-Type: application/json

// Tipo: status (telemetria periódica)
{
  "tipo": "status",
  "dispenser_id": 1,
  "status": "idle",
  "medicamento": null,
  "quantidade": 0,
  "timestamp": "2024-01-01T00:00:00Z"
}

// Tipo: carregado (dispenser pronto para dispensar)
{
  "tipo": "carregado",
  "dispenser_id": 1,
  "os_id": "OS-2024-001",
  "medicamento": "Dipirona 500mg",
  "quantidade_carregada": 10
}

// Tipo: dispensado (evento por unidade ou por lote)
{
  "tipo": "dispensado",
  "dispenser_id": 1,
  "os_id": "OS-2024-001",
  "quantidade_dispensada": 10,
  "quantidade_residual": 0,
  "validado": true,
  "motivo_falha": null
}

// Tipo: erro
{
  "tipo": "erro",
  "dispenser_id": 1,
  "os_id": "OS-2024-001",
  "codigo_erro": "falha_mecanica",
  "descricao": "Sensor de contagem não respondeu"
}

// Tipo: limpeza_ok
{
  "tipo": "limpeza_ok",
  "dispenser_id": 1,
  "medicamento_removido": "Dipirona 500mg"
}
```

### 4.3 Comandos: central-computer → dispenser-adapter

```http
POST http://dispenser-adapter:8100/comandos/carregar
{
  "dispenser_id": 1,
  "medicamento": "Dipirona 500mg",
  "sku": "DIP500",
  "categoria": "Analgésicos",
  "quantidade": 10,
  "os_id": "OS-2024-001"
}
Response: {"aceito": true, "dispenser_id": 1}

POST http://dispenser-adapter:8100/comandos/dispensar
{
  "dispenser_id": 1,
  "os_id": "OS-2024-001"
}
Response: {"aceito": true}

POST http://dispenser-adapter:8100/comandos/limpar
{
  "dispenser_id": 1,
  "solicitado_por": "admin"
}
Response: {"aceito": true} | {"erro": "em_operacao"}
```

### 4.4 Eventos: cnc-adapter → central-computer

```http
POST http://central-computer:8000/api/v1/eventos/cnc
Content-Type: application/json

// Tipo: movendo
{
  "tipo": "movendo",
  "os_id": "OS-2024-001",
  "dispenser_alvo": 3,
  "posicao_x": 150.0,
  "posicao_y": 0.0,
  "timestamp": "..."
}

// Tipo: posicionado (CNC chegou ao dispenser)
{
  "tipo": "posicionado",
  "os_id": "OS-2024-001",
  "dispenser_alvo": 3,
  "posicao_x": 240.0,
  "posicao_y": 0.0
}

// Tipo: concluido (todos os dispensers visitados)
{
  "tipo": "concluido",
  "os_id": "OS-2024-001",
  "ciclos_realizados": 3
}

// Tipo: erro
{
  "tipo": "erro",
  "os_id": "OS-2024-001",
  "codigo_erro": "limite_eixo_x",
  "descricao": "Eixo X fora do limite operacional"
}
```

### 4.5 Comandos: central-computer → cnc-adapter

```http
POST http://cnc-adapter:8101/comandos/mover
{
  "dispenser_alvo": 3,
  "os_id": "OS-2024-001",
  "posicao_x": 240.0,
  "posicao_y": 0.0
}
Response: {"aceito": true, "eta_segundos": 3.2}

POST http://cnc-adapter:8101/comandos/homing
{}
Response: {"aceito": true}
```

### 4.6 Dashboard e IHM → central-computer

Inalterado — já funciona:
- WebSocket: `ws://central-computer:8000/ws` — stream de estado em tempo real
- REST: `GET /dispensers/estado`, `GET /ordens/ativa`, `GET /historico`, etc.

---

## 5. Fluxo Operacional Corrigido

```
order-generator
  │ POST /api/v1/ordens
  ▼
central-computer recebe OS
  │ persiste no DB, enfileira
  │ roda IA: _atribuir_slots() [migrado do dispenser_simulator]
  │ roda otimização de rota CNC: _planejar_rota() [migrado do cnc_simulator]
  │
  │ para cada slot atribuído:
  │   POST /comandos/carregar → dispenser-adapter → dispenser-simulator
  │   aguarda evento "carregado" de todos os slots
  │
  │ quando todos prontos:
  │   POST /comandos/mover {dispenser_alvo: X} → cnc-adapter → cnc-simulator
  │   aguarda evento "posicionado"
  │
  │   POST /comandos/dispensar {dispenser_id: X} → dispenser-adapter → dispenser-simulator
  │   aguarda evento "dispensado"
  │
  │   (repete para cada dispenser na ordem planejada)
  │
  │ POST /comandos/homing → cnc-adapter
  │ atualiza DB: OS concluída
  │ broadcast WebSocket → dashboard atualiza
  ▼
order-generator pode enviar próxima OS
```

O central-computer é o único que sabe o estado da OS. Adapters são passivos.

---

## 6. Estrutura de Diretórios Proposta

```
valoryapsen/
├── central-computer/          ← era: backend/
│   ├── main.py                   (remover MQTT, adicionar endpoints /api/v1/eventos/*)
│   ├── database.py               (inalterado)
│   ├── auth.py                   (inalterado)
│   ├── config.py                 (remover MQTT_HOST/PORT)
│   ├── orchestrator.py           ← NOVO: lógica de orquestração da OS
│   ├── ia_atribuicao.py          ← NOVO: migrado de dispenser_simulator._atribuir_slots
│   └── Dockerfile
│
├── cnc-adapter/               ← NOVO
│   ├── main.py                   (FastAPI: recebe comandos, repassa ao simulador via HTTP)
│   └── Dockerfile
│
├── dispenser-adapter/         ← NOVO
│   ├── main.py                   (FastAPI: recebe comandos, repassa ao simulador via HTTP)
│   └── Dockerfile
│
├── vision-adapter/            ← NOVO (futuro)
│   ├── main.py
│   └── Dockerfile
│
├── cnc-simulator/             ← simplificado
│   ├── simulator.py              (manter hardware sim, remover IA/fila/atribuição)
│   └── Dockerfile
│
├── dispenser-simulator/       ← simplificado
│   ├── simulator.py              (manter hardware sim, remover _atribuir_slots/_processar_os)
│   └── Dockerfile
│
├── order-generator/           ← era: sap_simulator/
│   ├── simulator.py              (remover MQTT, substituir por POST HTTP)
│   └── Dockerfile
│
├── dashboard/                 ← inalterado
├── ihm_web/                   ← inalterado
├── mysql/                     ← inalterado
└── docker-compose.yml         ← atualizado
```

---

## 7. docker-compose.yml Proposto

```yaml
version: "3.9"

services:

  mysql:
    # inalterado
    image: mysql:8.0
    ...

  central-computer:
    build: ./central-computer
    container_name: apsen-central
    ports:
      - "8000:8000"
    environment:
      MYSQL_HOST: mysql
      CNC_ADAPTER_URL: http://cnc-adapter:8101
      DISPENSER_ADAPTER_URL: http://dispenser-adapter:8100
      VISION_ADAPTER_URL: http://vision-adapter:8102
      SECRET_KEY: apsen-mude-esta-chave
    depends_on:
      mysql:
        condition: service_healthy
    networks:
      - apsen-net

  cnc-adapter:
    build: ./cnc-adapter
    container_name: apsen-cnc-adapter
    ports:
      - "8101:8101"
    environment:
      CENTRAL_URL: http://central-computer:8000
      CNC_SIMULATOR_URL: http://cnc-simulator:8200
    networks:
      - apsen-net

  dispenser-adapter:
    build: ./dispenser-adapter
    container_name: apsen-dispenser-adapter
    ports:
      - "8100:8100"
    environment:
      CENTRAL_URL: http://central-computer:8000
      DISPENSER_SIMULATOR_URL: http://dispenser-simulator:8201
    networks:
      - apsen-net

  cnc-simulator:
    build: ./cnc-simulator
    container_name: apsen-cnc-sim
    ports:
      - "8200:8200"
    environment:
      ADAPTER_URL: http://cnc-adapter:8101
      VEL_MM_S: 80
    networks:
      - apsen-net

  dispenser-simulator:
    build: ./dispenser-simulator
    container_name: apsen-dispenser-sim
    ports:
      - "8201:8201"
    environment:
      ADAPTER_URL: http://dispenser-adapter:8100
      T_CARGA_UNID: 0.3
      T_DISPENSA_UNID: 0.5
      PROB_FALHA: 0.05
    networks:
      - apsen-net

  order-generator:
    build: ./order-generator
    container_name: apsen-order-gen
    environment:
      CENTRAL_URL: http://central-computer:8000
      INTERVALO_OS: 90
    networks:
      - apsen-net

  dashboard:
    build: ./dashboard
    ports:
      - "8050:8050"
    environment:
      BACKEND_URL: http://central-computer:8000
    networks:
      - apsen-net

  ihm_web:
    build: ./ihm_web
    ports:
      - "8051:8051"
    environment:
      BACKEND_URL: http://central-computer:8000
    networks:
      - apsen-net

volumes:
  mysql_data:

networks:
  apsen-net:
    driver: bridge
```

**Sem `mosquitto`.** 10 serviços ao total.

---

## 8. Plano de Migração (3 fases, sem quebrar o que funciona)

### Fase 1 — Criar os adapters (sistema atual continua rodando)

Criar `cnc-adapter/` e `dispenser-adapter/` como FastAPIs mínimas.
Nesta fase, internamente os adapters ainda podem repassar comandos aos simuladores via MQTT (mantém compatibilidade) enquanto já expõem HTTP para o central-computer.
- Testável: o central-computer começa a usar `/api/v1/eventos/*` em vez de ler MQTT.
- Simuladores não mudam ainda.
- Mosquitto permanece.

### Fase 2 — Migrar lógica de negócio para o central-computer

Mover para `central-computer/orchestrator.py`:
- `_atribuir_slots()` do dispenser_simulator
- `_nearest_neighbor()` / `_planejar_rota()` do cnc_simulator
- `_processar_os_unica()` e `_processar_fila()` do dispenser_simulator

Os simuladores param de receber OS e de fazer IA. Passam a apenas executar comandos recebidos dos adapters.

### Fase 3 — Remover MQTT

- Simuladores migram para HTTP: recebem comandos via REST, reportam eventos via POST ao adapter.
- `mosquitto` é removido do docker-compose.
- `order-generator` usa apenas POST HTTP.
- Central-computer remove completamente o cliente MQTT.

---

## 9. Resumo das Correções por Arquivo

| Arquivo | Ação |
|---|---|
| `backend/main.py` | Renomear para `central-computer`. Remover MQTT client. Adicionar endpoints `/api/v1/eventos/cnc` e `/api/v1/eventos/dispenser`. Adicionar lógica de orquestração. |
| `dispenser_simulator/simulator.py` | Remover `_atribuir_slots`, `_processar_os_unica`, `_processar_fila`. Adicionar servidor HTTP (FastAPI/Flask) para receber comandos. |
| `cnc_simulator/simulator.py` | Remover `_nearest_neighbor`, lógica de fila OS, `apsen/ia/atribuicao`. Adicionar servidor HTTP para receber comandos. |
| `sap_simulator/simulator.py` | Remover MQTT. Substituir publicação por `POST /api/v1/ordens`. |
| `docker-compose.yml` | Remover `mosquitto`. Adicionar `cnc-adapter`, `dispenser-adapter`. Renomear serviços. |
| Criar `cnc-adapter/main.py` | FastAPI com `/comandos/mover`, `/comandos/homing`. Repassa ao simulador, recebe eventos e POST para central-computer. |
| Criar `dispenser-adapter/main.py` | FastAPI com `/comandos/carregar`, `/comandos/dispensar`, `/comandos/limpar`. |
| Criar `central-computer/orchestrator.py` | Máquina de estados da OS: fila, atribuição, carregamento, dispensa, conclusão. |
