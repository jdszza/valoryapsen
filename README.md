# APSEN – Sistema de Contagem de Medicamentos

> **Projeto de Engenharia Mecatrônica – 3º Ano**  
> Valory · APSEN Farmacêutica

---

## Visão Geral

Sistema que conta e valida medicamentos com o auxílio de uma mesa CNC, 6 dispensers com visão computacional e IA, e integração com o ERP SAP. A arquitetura é baseada em MQTT como backbone de comunicação, com MySQL para persistência e Plotly Dash para interface.

**O Dashboard e a IHM são somente leitura — não controlam nenhum equipamento.**

---

## Arquitetura

```
SAP (ERP)
  │ publica Ordem de Saída (OS)
  ▼
MQTT Broker (Mosquitto)
  ├── Backend FastAPI ──────── MySQL (banco de dados)
  │       │
  │       ├── /estado, /os/*, /alarmes   ← Dashboard (read-only)
  │       └── /manutencao/*, /auth/*     ← IHM Manutenção (JWT)
  │
  ├── Firmware CNC ────────────── mesa movimenta a caixa
  └── Firmware Dispensers ──────── CV + IA validam remédios
```

### Fluxo por Ordem de Saída (OS)

1. **SAP** publica a OS em `apsen/os/nova` com os medicamentos solicitados (baseados nos 96 medicamentos reais APSEN no banco)
2. **IA dos dispensers** define qual slot atende cada medicamento → publica `apsen/ia/atribuicao`
   - Slots são **dinâmicos**: nenhum dispenser é fixo a um medicamento
   - Roteamento prioriza slots com residual do mesmo medicamento; caso contrário, usa slot vazio
3. **Fase de Carregamento**: visão computacional monitora o preenchimento de cada dispenser
   - Se CV detecta erro → publica alarme e a OS para completamente
   - Quando OK → dispenser publica "pronto" em `apsen/dispenser/status`
4. **CNC** recebe a atribuição, calcula a ordem ótima de visita (nearest-neighbor), e aguarda todos os dispensers ficarem prontos
5. **Mesa CNC inicia**: para cada dispenser na ordem calculada:
   - Publica `movendo` com posição interpolada a cada 500ms
   - Chega ao dispenser → publica `posicionado`
   - Dispenser dispensa remédio a remédio com validação IA (95% ok, 5% falha)
   - Cada remédio dispensado gera um evento (`apsen/dispenser/evento`)
   - Dispenser conclui → CNC avança para o próximo
6. **OS concluída**: CNC publica `concluido` e retorna para HOME

---

## Tópicos MQTT

| Tópico | Publicado por | Conteúdo |
|--------|--------------|----------|
| `apsen/os/nova` | SAP | Nova Ordem de Saída |
| `apsen/os/fila` | Dispenser | OS enfileirada (posição na fila) |
| `apsen/os/concluida` | Dispenser | OS finalizada ou abortada |
| `apsen/ia/atribuicao` | Dispenser (IA) | Mapeamento slot → medicamento |
| `apsen/dispenser/carregamento` | Dispenser (CV) | Progresso do carregamento |
| `apsen/dispenser/pronto` | Dispenser | Slot carregado e pronto |
| `apsen/dispenser/evento` | Dispenser | Cada remédio dispensado |
| `apsen/dispenser/status` | Dispenser | Status geral de cada slot |
| `apsen/dispenser/limpar` | IHM | Solicitação de limpeza de slot |
| `apsen/dispenser/limpeza_ok` | Dispenser | Confirmação de limpeza |
| `apsen/dispenser/limpeza_erro` | Dispenser | Limpeza recusada (slot em operação) |
| `apsen/cnc/status` | CNC | Posição XY + status + ciclo |
| `apsen/manut/temperatura` | CNC / Dispensers | Temperatura de componentes |
| `apsen/manut/uso` | CNC | Horas de uso, desgaste |

---

## Serviços

| Serviço | Porta | Descrição |
|---------|-------|-----------|
| `mysql` | 3306 | Banco de dados MySQL 8 |
| `mosquitto` | 1883 / 9001 | Broker MQTT |
| `backend` | 8000 | API FastAPI + WebSocket |
| `dashboard` | 8050 | Monitoramento (sem login) |
| `ihm_web` | 8051 | Manutenção (requer login) |
| `sap_simulator` | — | Simula ERP SAP |
| `cnc_simulator` | — | Simula firmware CNC |
| `dispenser_simulator` | — | Simula 6 dispensers (CV+IA) |

---

## Como Rodar

### Pré-requisitos

- Docker Desktop instalado e rodando
- Git

### 1. Clonar o repositório

```bash
git clone <url-do-repo>
cd valoryapsen
```

### 2. Subir os serviços

```bash
docker compose up -d --build
```

> **Primeira execução**: o banco é inicializado automaticamente com os 96 medicamentos reais APSEN. Aguarde ~30s para os serviços estabilizarem.

### 3. Verificar se está tudo rodando

```bash
docker compose ps
```

Todos os serviços devem estar com status `Up`.

### 4. Acessar

| Serviço | URL |
|---------|-----|
| Dashboard (monitoramento) | http://localhost:8050 |
| IHM de Manutenção | http://localhost:8051 |
| API Backend (Swagger) | http://localhost:8000/docs |
| MySQL Workbench | localhost:3306 — user: `apsen` / pw: `apsen_pass_2024` |

### 5. Login IHM

| Usuário | Senha | Perfil |
|---------|-------|--------|
| `admin` | `admin123` | Administrador |
| `manut1` | `mnt123` | Técnico de Manutenção |

---

## Configuração dos Simuladores

Os simuladores têm parâmetros ajustáveis via variáveis de ambiente no `docker-compose.yml`:

### SAP Simulator

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `INTERVALO_OS` | `90` | Segundos entre ordens de saída |
| `RELOAD_CATALOGO_MIN` | `30` | Recarregar catálogo do DB a cada N minutos |
| `MIN_MEDS_POR_OS` | `2` | Mínimo de medicamentos distintos por OS |
| `MAX_MEDS_POR_OS` | `6` | Máximo de medicamentos por OS |
| `QTD_MIN` | `2` | Unidades mínimas por medicamento |
| `QTD_MAX` | `15` | Unidades máximas por medicamento |

### CNC Simulator

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `VEL_MM_S` | `80` | Velocidade de movimento (mm/s) |
| `INTERVALO` | `0.5` | Segundos entre publicações de posição |
| `TIMEOUT_PRONTO` | `180` | Timeout aguardando dispensers prontos (s) |
| `TIMEOUT_DISP` | `120` | Timeout aguardando dispenser concluir (s) |

### Dispenser Simulator

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `T_CARGA_UNID` | `0.3` | Segundos por unidade no carregamento |
| `T_DISPENSA_UNID` | `0.5` | Segundos por unidade na dispensa |
| `PROB_FALHA_IA` | `0.05` | Probabilidade de falha na validação IA (5%) |
| `PROB_ERRO_CV` | `0.01` | Probabilidade de erro de CV por unidade (1%) |
| `TIMEOUT_CNC_POS` | `240` | Timeout aguardando CNC posicionar (s) |

---

## Rebuild e Manutenção

```bash
# Parar tudo
docker compose down

# Apagar volumes (APAGA O BANCO — use com cuidado)
docker compose down -v

# Rebuild completo (necessário após mudanças no código)
docker compose down -v && docker compose build --no-cache && docker compose up -d

# Ver logs de um serviço
docker compose logs -f backend
docker compose logs -f sap_simulator
docker compose logs -f dispenser_simulator

# Reiniciar um serviço específico
docker compose restart backend
```

---

## Produção

Para substituir os simuladores pelos firmwares reais:

1. **SAP real**: remova o serviço `sap_simulator` do `docker-compose.yml`. O SAP deve publicar diretamente em `apsen/os/nova` no broker MQTT.
2. **CNC real**: remova `cnc_simulator`. O firmware deve publicar em `apsen/cnc/status`.
3. **Dispensers reais**: remova `dispenser_simulator`. Os firmwares devem publicar nos tópicos `apsen/dispenser/*` e `apsen/ia/atribuicao`.

O backend, dashboard e IHM não precisam de nenhuma mudança.

---

## Banco de Dados

Schema `apsen_db` — tabelas principais:

| Tabela | Descrição |
|--------|-----------|
| `medicamentos` | Catálogo com 96 medicamentos reais APSEN (12 categorias) |
| `ordens` | Ordens de Saída recebidas da SAP |
| `os_itens` | Itens de cada OS (por dispenser) |
| `dispensas` | Cada remédio dispensado com validação IA |
| `cnc_eventos` | Eventos de movimento da CNC |
| `leituras_sensores` | Temperatura, desgaste, horas de uso |
| `alarmes` | Alarmes gerados e resoluções |
| `log_manutencao` | Manutenções registradas pelos técnicos |
| `usuarios` | Técnicos autorizados para a IHM (role: admin \| manutencao) |

### Categorias de Medicamentos

| Categoria | Área Terapêutica |
|-----------|-----------------|
| `snc` | Neurologia / Psiquiatria / SNC |
| `otorrino` | Otorrinolaringologia |
| `urologia` | Urologia |
| `gastroenterologia` | Gastroenterologia |
| `lactose` | Intolerância à Lactose |
| `reumatologia` | Reumatologia |
| `ortopedia` | Ortopedia |
| `cardiologia` | Cardiologia |
| `infectologia` | Infectologia |
| `alergia` | Alergia |
| `dermatologia` | Dermatologia |
| `vitaminas` | Vitaminas e Suplementos |

---

## Stack Tecnológico

- **Python 3.12**
- **FastAPI** + Uvicorn (backend REST + WebSocket)
- **Plotly Dash** + dash-bootstrap-components (dashboard + IHM)
- **Paho MQTT** (comunicação MQTT em todos os serviços)
- **MySQL 8** + PyMySQL
- **Eclipse Mosquitto 2** (broker MQTT)
- **Docker Compose** (orquestração)
- **JWT** (autenticação IHM via python-jose)
- **bcrypt** (hash de senhas)
