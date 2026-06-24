# APSEN – Sistema de Contagem Inteligente

> Projeto de Engenharia Mecatrônica – 3º Ano  
> Integração: IHM (ESP32) + Dashboard Online (Plotly Dash) + Backend (FastAPI) via MQTT

---

## Arquitetura

```
┌─────────────────┐          ┌───────────────────┐          ┌──────────────────┐
│  ESP32-8048S070 │  MQTT    │  Mosquitto Broker  │  MQTT    │  FastAPI Backend │
│  7" 800x480 GT911│◄──────► │   :1883  :9001     │ ◄──────► │  + SQLite        │
└─────────────────┘  WiFi    └───────────────────┘          └────────┬─────────┘
                                                                      │ WebSocket
                                                             ┌────────▼─────────┐
                                                             │  Plotly Dash      │
                                                             │  Dashboard :8050  │
                                                             └──────────────────┘
```

**Fluxo de dados:**
1. ESP32 lê o sensor e publica contagem via MQTT a cada 1 segundo
2. Backend assina MQTT, persiste no SQLite e transmite via WebSocket
3. Dashboard recebe via WebSocket e atualiza em tempo real
4. Dashboard pode enviar comandos (novo lote, reset) → backend → MQTT → ESP32

---

## Tópicos MQTT

| Tópico              | Direção         | Payload (JSON)                                              |
|---------------------|-----------------|-------------------------------------------------------------|
| `apsen/contagem`    | ESP32 → Todos   | `{"valor": 1234, "velocidade": 42.5}`                      |
| `apsen/status`      | ESP32 → Todos   | `{"status": "running", "alarme": null}`                    |
| `apsen/lote`        | ESP32 → Todos   | `{"lote_id": "LOTE-001", "meta": 5000}`                    |
| `apsen/cmd/lote`    | Dash → ESP32    | `{"lote_id": "LOTE-002", "meta": 3000}`                    |
| `apsen/cmd/reset`   | Dash → ESP32    | `{"reset": true}`                                          |
| `apsen/cmd/status`  | Dash → ESP32    | `{"cmd": "start"}` / `"pause"` / `"stop"`                  |

**Status possíveis:** `idle` | `running` | `paused` | `alarm`

---

## Iniciar o sistema (PC / servidor)

### Pré-requisito: Docker + Docker Compose instalados

```bash
# Na raiz do projeto
docker compose up --build

# Serviços disponíveis:
#   Dashboard  → http://localhost:8050
#   Backend API → http://localhost:8000
#   MQTT Broker → mqtt://localhost:1883
```

### API REST do Backend

| Endpoint              | Método | Descrição                              |
|-----------------------|--------|----------------------------------------|
| `/estado`             | GET    | Estado atual (contagem, status, lote)  |
| `/historico`          | GET    | Histórico de contagem (`?lote_id=X`)   |
| `/lote`               | GET    | Info do lote ativo                     |
| `/cmd/lote`           | POST   | Novo lote (`?lote_id=X&meta=5000`)     |
| `/cmd/reset`          | POST   | Reseta contagem                        |
| `/cmd/status`         | POST   | Envia comando (`?status=start`)        |
| `/ws`                 | WS     | WebSocket – estado em tempo real       |

---

## Configurar o ESP32-8048S070

### Hardware: Sunton ESP32-8048S070C
- Display 7" IPS 800×480 — interface **RGB paralelo 16-bit** (≠ SPI)
- Touch capacitivo **GT911** via I2C
- ESP32-S3-WROOM-N16R8 (16 MB Flash, 8 MB PSRAM OPI)

### Pinagem (já configurada no .ino)

| Função         | GPIO |
|----------------|------|
| Backlight      | 2    |
| DE             | 40   |
| VSYNC          | 41   |
| HSYNC          | 39   |
| PCLK           | 42   |
| R[0-4]         | 45, 48, 47, 21, 14 |
| G[0-5]         | 5, 6, 7, 15, 16, 4 |
| B[0-4]         | 8, 3, 46, 9, 1 |
| Touch SDA      | 19   |
| Touch SCL      | 20   |
| Touch INT      | 38   |

### 1. Instalar bibliotecas no Arduino IDE

| Biblioteca        | Autor              | Como instalar                          |
|-------------------|--------------------|----------------------------------------|
| `Arduino_GFX_Library` | moononournation | Library Manager: "Arduino GFX"     |
| `TAMC_GT911`      | TAMCTech           | Library Manager: "TAMC GT911"          |
| `PubSubClient`    | Nick O'Leary       | Library Manager: "PubSubClient"        |
| `ArduinoJson`     | Benoit Blanchon    | Library Manager: "ArduinoJson"         |

### 2. Configurar a placa no Arduino IDE

`Tools` → `Board` → `ESP32 Arduino` → **"ESP32S3 Dev Module"**

| Opção              | Valor                              |
|--------------------|------------------------------------|
| PSRAM              | **OPI PSRAM**                      |
| Flash Size         | **16MB (128Mb)**                   |
| Partition Scheme   | 16M Flash (3MB APP/9.9MB FATFS)    |
| Upload Speed       | 921600                             |
| USB CDC On Boot    | Enabled (para Serial.print)        |

### 3. Editar `ihm_esp32.ino`
```cpp
const char* WIFI_SSID     = "SEU_WIFI";
const char* WIFI_PASSWORD = "SUA_SENHA";
const char* MQTT_SERVER   = "192.168.1.100";  // IP do servidor na mesma rede
```

### 4. Integrar o sensor de contagem
Procure o bloco marcado com `INTEGRE SEU SENSOR AQUI` e implemente a leitura:
- **Sensor IR / feixe de luz:** detecta borda de descida no pino digital
- **Encoder incremental:** `attachInterrupt` na borda FALLING
- **Câmera + IA:** atribua o resultado do modelo a `est.contagem`

### 5. Calibrar timing do display (se necessário)
Se a imagem aparecer com cores erradas ou deslocada, ajuste os parâmetros
`hfp/hpw/hbp` e `vfp/vpw/vbp` no construtor `Arduino_RPi_DPI_RGBPanel`.
Valores típicos para o painel 7" deste módulo já estão configurados.

---

## Integração futura com CNC

Quando o firmware CNC estiver pronto, basta:
1. Conectá-lo ao broker MQTT como um novo publisher
2. Criar novos tópicos: ex. `apsen/cnc/posicao`, `apsen/cnc/status`
3. Assinar no backend e no dashboard — nenhuma alteração na arquitetura

---

## Estrutura de arquivos

```
APSEN - VALORY/
├── backend/
│   ├── main.py          # FastAPI + MQTT bridge + WebSocket
│   ├── database.py      # SQLite (contagens, eventos)
│   ├── config.py        # Configurações por env var
│   ├── requirements.txt
│   └── Dockerfile
├── dashboard/
│   ├── app.py           # Plotly Dash (layout + callbacks)
│   ├── requirements.txt
│   └── Dockerfile
├── ihm_esp32/
│   └── ihm_esp32.ino    # Firmware ESP32 IHM
├── mosquitto/
│   └── mosquitto.conf   # Config do broker
├── docker-compose.yml
└── SISTEMA.md           # Este arquivo
```

---

## Credenciais de Acesso

**Dashboard** → `http://localhost:8050`  
**IHM Web** → `http://localhost:8051`

| Usuário    | Senha      | Role        | Acesso                                      |
|------------|------------|-------------|---------------------------------------------|
| `admin`    | `admin123` | Admin       | Dashboard + IHM completo                    |
| `operador1`| `op123`    | Operador    | Dashboard + IHM (operação e OS)             |
| `manut1`   | `mnt123`   | Manutenção  | Dashboard + IHM (operação, OS e manutenção) |

> **Atenção:** troque as senhas antes de colocar em produção. Acesse a página de Usuários na IHM (login como `admin`) ou use o endpoint `PUT /usuarios/{username}` no backend.

---

## Próximos Passos

### Funcionalidades
- **Gerador automático de OS** — inspirado em `sap_integration.py`: ao iniciar um novo lote (via MQTT `apsen/lote` ou comando manual), o sistema gera automaticamente uma OS com os dados do lote, produto e meta. Opcionalmente envia um payload JSON para um ERP externo (SAP ou sistema da APSEN) via `POST /api/sap/registro`, com campos: `lote`, `produto`, `meta`, `centro`, `timestamp`. Implementar como módulo `erp_integration.py` no backend, chamado dentro do handler do tópico `apsen/lote`.
- **Edição e encerramento de OS** — a rota `PUT /ordens/{os_id}` já existe no backend, mas a IHM não tem botão para mudar status (aberto → em andamento → concluído). Adicionar ações na tabela de OS.
- **Edição/desativação de usuários** — a página de Usuários só cria. Adicionar botões de editar role e desativar conta.

### Hardware e integração
- **Integrar sensor de contagem no ESP32** — o bloco `INTEGRE SEU SENSOR AQUI` no firmware está vazio. Implementar leitura real (sensor IR, encoder incremental ou câmera com IA) e publicar em `apsen/contagem`.
- **Integração com firmware CNC** — quando pronto, criar tópicos `apsen/cnc/*`, assinar no backend e exibir painel dedicado no dashboard.

### Infraestrutura
- **Teste completo com Docker** — subir `docker compose up --build` e validar fluxo ponta a ponta: ESP32 → MQTT → backend → dashboard e IHM.
- **Segurança para produção** — gerar `SECRET_KEY` forte no `docker-compose.yml`, habilitar HTTPS/WSS, validar comportamento do ESP32 em WiFi industrial (reconexão automática, interferências).
