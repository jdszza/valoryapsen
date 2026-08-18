/*
  ⚠️  FIRMWARE DEFASADO — NÃO REFLETE A ARQUITETURA ATUAL DO SISTEMA
  ==================================================================
  Este sketch fala MQTT (PubSubClient, tópicos `apsen/*`), e o APSEN migrou
  inteiramente para REST/HTTP + WebSocket: não existe mais broker no
  docker-compose.yml, e nenhum serviço publica ou assina tópico algum. Ou seja,
  compilado e gravado hoje, este firmware não conversa com coisa nenhuma.

  Migrar para `GET /estado` + `WS /ws` do central-computer é trabalho
  PENDENTE — ver a nota no CLAUDE.md. O arquivo segue no repositório como
  referência do layout de tela e do mapeamento de pinos do hardware.

  APSEN - IHM ESP32-8048S070
  ===========================
  Hardware: Sunton ESP32-8048S070C
    - ESP32-S3-WROOM-N16R8 (16MB Flash, 8MB PSRAM)
    - Display: 7.0" IPS 800x480, interface RGB paralelo 16-bit
    - Touch: GT911 (capacitivo, I2C)
    - WiFi + Bluetooth integrado

  Bibliotecas (Arduino Library Manager ou GitHub):
    - Arduino_GFX_Library  (moononournation)  → display RGB
    - TAMC_GT911           (TAMCTech)         → touch capacitivo
    - PubSubClient         (Nick O'Leary)     → MQTT
    - ArduinoJson          (Benoit Blanchon)  → JSON

  Board: "ESP32S3 Dev Module"
    - PSRAM: "OPI PSRAM"
    - Flash Size: "16MB (128Mb)"
    - Partition Scheme: "16M Flash (3MB APP/9.9MB FATFS)"
    - Upload Speed: 921600

  Tópicos MQTT:
    Publica:
      apsen/contagem   → {"valor": <int>, "velocidade": <float>}
      apsen/status     → {"status": "running"|"paused"|"idle"|"alarm", "alarme": <str|null>}
      apsen/lote       → {"lote_id": <str>, "meta": <int>}
    Assina:
      apsen/cmd/lote   ← {"lote_id": <str>, "meta": <int>}
      apsen/cmd/reset  ← {"reset": true}
      apsen/cmd/status ← {"cmd": "start"|"pause"|"stop"}
*/

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Arduino_GFX_Library.h>
#include <TAMC_GT911.h>

// ── Configurações de rede ──────────────────────────────────────────────────────
const char* WIFI_SSID      = "SEU_WIFI";
const char* WIFI_PASSWORD  = "SUA_SENHA";
const char* MQTT_SERVER    = "192.168.1.100";  // IP do servidor na rede local
const int   MQTT_PORT      = 1883;
const char* MQTT_CLIENT_ID = "apsen-ihm-01";

// ── Tópicos MQTT ───────────────────────────────────────────────────────────────
#define TOPIC_CONTAGEM   "apsen/contagem"
#define TOPIC_STATUS     "apsen/status"
#define TOPIC_LOTE       "apsen/lote"
#define TOPIC_CMD_LOTE   "apsen/cmd/lote"
#define TOPIC_CMD_RESET  "apsen/cmd/reset"
#define TOPIC_CMD_STAT   "apsen/cmd/status"

// ── Pinagem ESP32-8048S070 ─────────────────────────────────────────────────────
// LCD RGB 16-bit
#define LCD_BL    2    // Backlight PWM
#define LCD_DE    40
#define LCD_VSYNC 41
#define LCD_HSYNC 39
#define LCD_PCLK  42
// RGB Red
#define LCD_R0 45
#define LCD_R1 48
#define LCD_R2 47
#define LCD_R3 21
#define LCD_R4 14
// RGB Green
#define LCD_G0 5
#define LCD_G1 6
#define LCD_G2 7
#define LCD_G3 15
#define LCD_G4 16
#define LCD_G5 4
// RGB Blue
#define LCD_B0 8
#define LCD_B1 3
#define LCD_B2 46
#define LCD_B3 9
#define LCD_B4 1

// Touch GT911 I2C
#define TOUCH_SDA  19
#define TOUCH_SCL  20
#define TOUCH_INT  38
#define TOUCH_RST  -1
#define TOUCH_W    800
#define TOUCH_H    480

// ── Driver RGB + GFX ──────────────────────────────────────────────────────────
Arduino_ESP32RGBPanel *bus = new Arduino_ESP32RGBPanel(
  LCD_DE, LCD_VSYNC, LCD_HSYNC, LCD_PCLK,
  LCD_R0, LCD_R1, LCD_R2, LCD_R3, LCD_R4,
  LCD_G0, LCD_G1, LCD_G2, LCD_G3, LCD_G4, LCD_G5,
  LCD_B0, LCD_B1, LCD_B2, LCD_B3, LCD_B4,
  1 /* hsync_pol */, 10 /* hfp */, 8 /* hpw */, 50 /* hbp */,
  1 /* vsync_pol */, 10 /* vfp */, 8 /* vpw */, 20 /* vbp */
);

Arduino_RPi_DPI_RGBPanel *gfx = new Arduino_RPi_DPI_RGBPanel(
  bus,
  800 /* width  */, 0, 8, 4, 8,
  480 /* height */, 0, 8, 4, 8,
  1 /* pclk_active_neg */, 14000000 /* pclk Hz */
);

// ── Touch GT911 ───────────────────────────────────────────────────────────────
TAMC_GT911 touch(TOUCH_SDA, TOUCH_SCL, TOUCH_INT, TOUCH_RST, TOUCH_W, TOUCH_H);

// ── Estado do sistema ──────────────────────────────────────────────────────────
struct Estado {
  long  contagem   = 0;
  long  meta       = 1000;
  char  lote_id[24] = "LOTE-001";
  char  status[16]  = "idle";
  char  alarme[80]  = "";
  float velocidade  = 0.0f;
  bool  dirty       = true;
} est;

// ── MQTT + WiFi ────────────────────────────────────────────────────────────────
WiFiClient   wifiCli;
PubSubClient mqtt(wifiCli);

// ── Timers ─────────────────────────────────────────────────────────────────────
unsigned long tPublish = 0;
unsigned long tDraw    = 0;
const unsigned long IV_PUBLISH = 1000;
const unsigned long IV_DRAW    = 100;

// ── Paleta de cores (RGB565) ──────────────────────────────────────────────────
#define C_BG      0x0000  // Preto
#define C_PANEL   0x0841  // Cinza escuro ~#101820
#define C_AZUL    0x0198  // Azul APSEN ~#003087
#define C_VERDE   0x07E0  // Verde
#define C_VERMELHO 0xF800 // Vermelho
#define C_AMARELO 0xFFE0  // Amarelo
#define C_BRANCO  0xFFFF
#define C_CINZA   0x7BEF
#define C_CINZAESC 0x39E7

// ── Helpers de texto centralizado ─────────────────────────────────────────────
void textoCentro(const char* txt, int16_t x, int16_t y, int16_t w,
                 uint8_t size, uint16_t cor) {
  gfx->setTextSize(size);
  gfx->setTextColor(cor);
  int16_t bx, by; uint16_t bw, bh;
  // Estimativa de largura (6 px por char * size)
  int16_t tw = strlen(txt) * 6 * size;
  gfx->setCursor(x + (w - tw) / 2, y);
  gfx->print(txt);
}

// ── Conexão WiFi ──────────────────────────────────────────────────────────────
void conectarWiFi() {
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  gfx->fillScreen(C_BG);
  gfx->setTextColor(C_BRANCO);
  gfx->setTextSize(3);
  gfx->setCursor(200, 220);
  gfx->print("Conectando WiFi...");
  while (WiFi.status() != WL_CONNECTED) delay(500);
  gfx->fillScreen(C_BG);
}

// ── MQTT callback ──────────────────────────────────────────────────────────────
void onMqttMsg(char* topic, byte* payload, unsigned int len) {
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, payload, len) != DeserializationError::Ok) return;

  if (strcmp(topic, TOPIC_CMD_LOTE) == 0) {
    strlcpy(est.lote_id, doc["lote_id"] | est.lote_id, sizeof(est.lote_id));
    est.meta = doc["meta"] | est.meta;
    est.contagem = 0;
    est.dirty = true;
    publicarLote();
  }
  else if (strcmp(topic, TOPIC_CMD_RESET) == 0) {
    if (doc["reset"].as<bool>()) { est.contagem = 0; est.dirty = true; }
  }
  else if (strcmp(topic, TOPIC_CMD_STAT) == 0) {
    const char* cmd = doc["cmd"];
    if (!cmd) return;
    if      (strcmp(cmd, "start") == 0) strlcpy(est.status, "running", sizeof(est.status));
    else if (strcmp(cmd, "pause") == 0) strlcpy(est.status, "paused",  sizeof(est.status));
    else if (strcmp(cmd, "stop")  == 0) strlcpy(est.status, "idle",    sizeof(est.status));
    est.dirty = true;
  }
}

void conectarMQTT() {
  while (!mqtt.connected()) {
    if (mqtt.connect(MQTT_CLIENT_ID)) {
      mqtt.subscribe(TOPIC_CMD_LOTE);
      mqtt.subscribe(TOPIC_CMD_RESET);
      mqtt.subscribe(TOPIC_CMD_STAT);
    } else {
      delay(2000);
    }
  }
}

// ── Publicações MQTT ───────────────────────────────────────────────────────────
void publicarContagem() {
  StaticJsonDocument<128> doc;
  doc["valor"]      = est.contagem;
  doc["velocidade"] = est.velocidade;
  char buf[128]; serializeJson(doc, buf);
  mqtt.publish(TOPIC_CONTAGEM, buf, true);
}

void publicarStatus() {
  StaticJsonDocument<128> doc;
  doc["status"] = est.status;
  doc["alarme"] = strlen(est.alarme) ? est.alarme : (const char*)nullptr;
  char buf[128]; serializeJson(doc, buf);
  mqtt.publish(TOPIC_STATUS, buf, true);
}

void publicarLote() {
  StaticJsonDocument<128> doc;
  doc["lote_id"] = est.lote_id;
  doc["meta"]    = est.meta;
  char buf[128]; serializeJson(doc, buf);
  mqtt.publish(TOPIC_LOTE, buf, true);
}

// ── UI: Desenho da tela ────────────────────────────────────────────────────────
/*
  Layout 800×480:
  ┌─────────────────────────────────────────────────────┐  y=0..59   Header
  ├──────────────────────┬──────────────────────────────┤  y=60
  │   CONTAGEM (grande)  │   Status Badge               │  y=60..179
  │   sub: un. contadas  │   Lote + Meta                │
  ├──────────────────────┴──────────────────────────────┤  y=180
  │   Barra de progresso                                 │  y=180..239
  ├─────────────────────────────────────────────────────┤  y=240
  │   [BTN PAUSAR/INICIAR]   [BTN RESET]   info WiFi    │  y=240..299
  ├─────────────────────────────────────────────────────┤  y=300
  │   Alarme / mensagens                                 │  y=300..360
  └─────────────────────────────────────────────────────┘
*/

void drawHeader() {
  gfx->fillRect(0, 0, 800, 60, C_AZUL);
  gfx->setTextColor(C_BRANCO);
  gfx->setTextSize(4);
  gfx->setCursor(20, 12);
  gfx->print("APSEN");
  gfx->setTextSize(2);
  gfx->setCursor(160, 20);
  gfx->print("Sistema de Contagem Inteligente");
}

uint16_t corStatus() {
  if (strcmp(est.status, "running") == 0) return C_VERDE;
  if (strcmp(est.status, "paused")  == 0) return C_AMARELO;
  if (strcmp(est.status, "alarm")   == 0) return C_VERMELHO;
  return C_CINZAESC;
}

void drawStatus() {
  // Badge de status (canto direito)
  uint16_t cor = corStatus();
  gfx->fillRoundRect(560, 70, 220, 50, 10, cor);
  uint16_t tc = (cor == C_AMARELO) ? C_BG : C_BRANCO;
  const char* label =
    strcmp(est.status, "running") == 0 ? "EM PRODUCAO" :
    strcmp(est.status, "paused")  == 0 ? "PAUSADO"     :
    strcmp(est.status, "alarm")   == 0 ? "ALARME"      : "AGUARDANDO";
  textoCentro(label, 560, 84, 220, 2, tc);
}

void drawContagem() {
  gfx->fillRect(0, 60, 550, 120, C_BG);
  // Número grande
  char buf[16];
  ltoa(est.contagem, buf, 10);
  gfx->setTextColor(C_BRANCO);
  gfx->setTextSize(8);
  int tw = strlen(buf) * 6 * 8;
  gfx->setCursor(max(10, (int)(270 - tw / 2)), 70);
  gfx->print(buf);
  // Subtítulo
  gfx->setTextSize(2);
  gfx->setTextColor(C_CINZA);
  gfx->setCursor(160, 155);
  gfx->print("unidades contadas");
}

void drawInfoLote() {
  gfx->fillRect(560, 125, 230, 55, C_BG);
  char buf[40];
  gfx->setTextColor(C_CINZA);
  gfx->setTextSize(2);

  gfx->setCursor(565, 130);
  gfx->print("Lote: ");
  gfx->setTextColor(C_BRANCO);
  gfx->print(est.lote_id);

  gfx->setTextColor(C_CINZA);
  gfx->setCursor(565, 155);
  gfx->print("Meta: ");
  gfx->setTextColor(C_BRANCO);
  snprintf(buf, sizeof(buf), "%ld", est.meta);
  gfx->print(buf);
}

void drawBarra() {
  const int BX = 20, BY = 185, BW = 760, BH = 36;
  float prog = (float)est.contagem / max(est.meta, 1L);
  if (prog > 1.0f) prog = 1.0f;
  int fill = (int)(BW * prog);

  gfx->fillRoundRect(BX, BY, BW, BH, 8, C_CINZAESC);
  if (fill > 0) gfx->fillRoundRect(BX, BY, fill, BH, 8, C_VERDE);

  // Percentual sobre a barra
  char buf[16];
  snprintf(buf, sizeof(buf), "%.1f%%", prog * 100.0f);
  textoCentro(buf, BX, BY + 9, BW, 2, C_BRANCO);

  // Texto abaixo
  gfx->setTextColor(C_CINZA);
  gfx->setTextSize(2);
  snprintf(buf, sizeof(buf), "%ld / %ld un", est.contagem, est.meta);
  gfx->setCursor(BX, BY + BH + 6);
  gfx->print(buf);

  // Velocidade
  snprintf(buf, sizeof(buf), "%.1f un/min", est.velocidade);
  gfx->setCursor(BX + 400, BY + BH + 6);
  gfx->print(buf);
}

// Botões de controle
struct Btn { int16_t x, y, w, h; const char* label; uint16_t cor; };
Btn btnPausa = {20,  248, 220, 50, "PAUSAR",   C_AMARELO};
Btn btnStart = {20,  248, 220, 50, "INICIAR",  C_VERDE};
Btn btnReset = {260, 248, 220, 50, "RESETAR",  C_VERMELHO};

void drawBotoes() {
  gfx->fillRect(0, 240, 800, 70, C_BG);

  // Pausar / Iniciar (alterna)
  bool running = strcmp(est.status, "running") == 0;
  Btn& bp = running ? btnPausa : btnStart;
  gfx->fillRoundRect(bp.x, bp.y, bp.w, bp.h, 10, bp.cor);
  uint16_t tc = (bp.cor == C_AMARELO) ? C_BG : C_BRANCO;
  textoCentro(bp.label, bp.x, bp.y + 16, bp.w, 2, tc);

  // Reset
  gfx->fillRoundRect(btnReset.x, btnReset.y, btnReset.w, btnReset.h, 10, btnReset.cor);
  textoCentro(btnReset.label, btnReset.x, btnReset.y + 16, btnReset.w, 2, C_BRANCO);

  // Indicadores de conectividade
  bool wifiOk = WiFi.status() == WL_CONNECTED;
  bool mqttOk = mqtt.connected();
  gfx->setTextSize(2);

  gfx->setTextColor(wifiOk ? C_VERDE : C_VERMELHO);
  gfx->setCursor(520, 260);
  gfx->print(wifiOk ? "WiFi OK" : "WiFi ERR");

  gfx->setTextColor(mqttOk ? C_VERDE : C_VERMELHO);
  gfx->setCursor(650, 260);
  gfx->print(mqttOk ? "MQTT OK" : "MQTT ERR");
}

void drawAlarme() {
  gfx->fillRect(0, 320, 800, 60, C_BG);
  if (strlen(est.alarme) == 0) return;
  gfx->fillRoundRect(10, 324, 780, 50, 10, 0x8400); // vermelho escuro
  gfx->setTextColor(C_BRANCO);
  gfx->setTextSize(2);
  gfx->setCursor(30, 340);
  gfx->print("ALARME: ");
  gfx->print(est.alarme);
}

void redraw() {
  drawHeader();
  drawStatus();
  drawContagem();
  drawInfoLote();
  drawBarra();
  drawBotoes();
  drawAlarme();
  est.dirty = false;
}

// ── Touch ──────────────────────────────────────────────────────────────────────
bool dentroBtn(Btn& b, int16_t tx, int16_t ty) {
  return tx >= b.x && tx <= b.x + b.w && ty >= b.y && ty <= b.y + b.h;
}

unsigned long tUltimoToque = 0;
void verificarTouch() {
  touch.read();
  if (!touch.isTouched) return;
  if (millis() - tUltimoToque < 300) return;  // debounce 300ms
  tUltimoToque = millis();

  int16_t tx = touch.points[0].x;
  int16_t ty = touch.points[0].y;

  // Pausar / Iniciar
  if (dentroBtn(btnPausa, tx, ty)) {
    bool running = strcmp(est.status, "running") == 0;
    strlcpy(est.status, running ? "paused" : "running", sizeof(est.status));
    est.dirty = true;
    publicarStatus();
  }
  // Reset
  else if (dentroBtn(btnReset, tx, ty)) {
    est.contagem = 0;
    est.dirty = true;
    mqtt.publish(TOPIC_CMD_RESET, "{\"reset\":true}");
  }
}

// ── Setup ──────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);

  // Backlight
  pinMode(LCD_BL, OUTPUT);
  digitalWrite(LCD_BL, HIGH);

  // Display
  gfx->begin();
  gfx->fillScreen(C_BG);
  gfx->setTextWrap(false);

  // Touch GT911
  Wire.begin(TOUCH_SDA, TOUCH_SCL);
  touch.begin();
  touch.setRotation(ROTATION_NORMAL);

  // Tela de boot
  gfx->setTextColor(C_BRANCO);
  gfx->setTextSize(4);
  gfx->setCursor(250, 200);
  gfx->print("APSEN");
  gfx->setTextSize(2);
  gfx->setCursor(220, 260);
  gfx->print("Sistema de Contagem");
  delay(1000);

  // WiFi
  conectarWiFi();

  // MQTT
  mqtt.setServer(MQTT_SERVER, MQTT_PORT);
  mqtt.setCallback(onMqttMsg);
  mqtt.setBufferSize(512);
  conectarMQTT();

  publicarLote();
  publicarStatus();

  redraw();
}

// ── Loop ───────────────────────────────────────────────────────────────────────
void loop() {
  // Mantém conexões
  if (WiFi.status() != WL_CONNECTED) conectarWiFi();
  if (!mqtt.connected()) conectarMQTT();
  mqtt.loop();

  // Touch
  verificarTouch();

  unsigned long agora = millis();

  // Publicação periódica
  if (agora - tPublish >= IV_PUBLISH) {
    tPublish = agora;

    // ─────────────────────────────────────────────────────────────────────
    // INTEGRE SEU SENSOR AQUI
    // Exemplos:
    //
    // Sensor IR / feixe de luz (pino digital, borda de descida = 1 item):
    //   static bool lastSensor = HIGH;
    //   bool cur = digitalRead(PIN_SENSOR);
    //   if (lastSensor == HIGH && cur == LOW) est.contagem++;
    //   lastSensor = cur;
    //
    // Encoder incremental (interrupção):
    //   attachInterrupt(PIN_ENC, []{ est.contagem++; }, FALLING);
    //   (remova o attachInterrupt do loop, coloque no setup)
    //
    // Resultado de IA (câmera / modelo):
    //   est.contagem = resultadoDoModelo;
    // ─────────────────────────────────────────────────────────────────────

    // Verifica lote completo
    if (est.contagem >= est.meta && strcmp(est.status, "running") == 0) {
      strlcpy(est.status, "idle", sizeof(est.status));
      snprintf(est.alarme, sizeof(est.alarme), "Lote %s completo!", est.lote_id);
      est.dirty = true;
    }

    publicarContagem();
    publicarStatus();
  }

  // Redraw
  if (agora - tDraw >= IV_DRAW || est.dirty) {
    tDraw = agora;
    if (est.dirty) redraw();
  }
}
