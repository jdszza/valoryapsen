/*
 * RECEPTOR - SEGUNDO ESP32 (teste da comunicacao Serial2)
 * ============================================================================
 * Recebe pelo Serial2 as mesmas linhas que o ESP32 da porta envia
 * (ex.: "EVT:PAUSA;UID=04A1B2C3 | Acesso autorizado...") e imprime
 * IDENTICAS no Serial Monitor (115200) deste ESP32.
 *
 * O pino RX2 (GPIO16) fica com PULL-UP INTERNO SEMPRE LIGADO, para que a linha
 * nao flutue (e nao gere lixo) enquanto o outro ESP32 nao esta transmitindo.
 *
 * LIGACOES (cruzadas!)
 *   ESP32 da PORTA  TX2 = GPIO17  ---->  RX2 = GPIO16  deste ESP32
 *   ESP32 da PORTA  RX2 = GPIO16  <----  TX2 = GPIO17  deste ESP32 (opcional)
 *   GND da porta ------------------------ GND deste ESP32   (OBRIGATORIO)
 *
 * Os dois ESP32 devem usar o mesmo baud (115200) e ambos sao 3V3.
 *
 * COMANDOS no Serial Monitor deste ESP32 (um caractere)
 *   X : liga/desliga modo HEX (mostra cada byte recebido, util p/ diagnostico)
 *   F : liga/desliga filtro de ruido (descarta bytes nao imprimiveis)
 *   T : envia uma linha de teste para o ESP32 da porta
 *   S : mostra estatisticas (bytes, linhas, descartados, ultimo recebimento)
 *   H : ajuda
 */

#include "driver/gpio.h"

#define SERIAL2_RX    16
#define SERIAL2_TX    17
#define SERIAL2_BAUD  115200

#define PIN_LED_PLACA 2          // LED azul embutido: pisca a cada linha recebida
const unsigned long PULSO_LED_MS       = 100;
const unsigned long AVISO_SEM_DADOS_MS = 5000;   // avisa se ficar tanto tempo sem receber nada
const unsigned long REFORCO_PULLUP_MS  = 1000;   // reaplica o pull-up periodicamente

static String        linha         = "";
static bool          modoHex       = false;
static bool          filtroRuido   = true;   // descarta bytes nao imprimiveis
static unsigned long totalBytes    = 0;
static unsigned long totalLinhas   = 0;
static unsigned long totalDescart  = 0;
static unsigned long tUltimoByte   = 0;
static unsigned long tUltimoAviso  = 0;
static unsigned long tLedApagar    = 0;
static unsigned long tPullup       = 0;
static bool          ledAceso      = false;
static unsigned long contadorTeste = 0;

// Mantem o pull-up interno ligado no RX2 sem desconectar o pino da UART.
static void pullupRX() {
  gpio_pullup_en((gpio_num_t)SERIAL2_RX);
  gpio_pulldown_dis((gpio_num_t)SERIAL2_RX);
}

static void pulsoLed() {
  digitalWrite(PIN_LED_PLACA, HIGH);
  ledAceso = true;
  tLedApagar = millis() + PULSO_LED_MS;
}

static void atualizarLed() {
  if (ledAceso && (long)(millis() - tLedApagar) >= 0) {
    digitalWrite(PIN_LED_PLACA, LOW);
    ledAceso = false;
  }
}

static void ajuda() {
  Serial.println();
  Serial.println("Comandos: X (hex on/off) | F (filtro de ruido on/off) | T (enviar teste p/ a porta) | S (estatisticas) | H (ajuda)");
}

static void estatisticas() {
  Serial.printf(">> bytes=%lu  linhas=%lu  descartados=%lu  filtro=%s  ",
                totalBytes, totalLinhas, totalDescart, filtroRuido ? "ON" : "OFF");
  if (totalBytes == 0) Serial.println("NADA recebido ainda.");
  else Serial.printf("ultimo byte ha %lu ms\n", millis() - tUltimoByte);
}

static void tratarComandos() {
  while (Serial.available()) {
    char c = toupper(Serial.read());
    if (c == '\r' || c == '\n' || c == ' ') continue;
    switch (c) {
      case 'X':
        modoHex = !modoHex;
        Serial.printf(">> modo HEX %s\n", modoHex ? "LIGADO" : "DESLIGADO");
        break;
      case 'F':
        filtroRuido = !filtroRuido;
        Serial.printf(">> filtro de ruido %s\n", filtroRuido ? "LIGADO" : "DESLIGADO");
        break;
      case 'T':
        contadorTeste++;
        Serial2.printf("TESTE do receptor #%lu\n", contadorTeste);
        Serial.printf(">> enviado teste #%lu para a porta (confira o monitor do outro ESP32)\n", contadorTeste);
        break;
      case 'S': estatisticas(); break;
      case 'H': ajuda(); break;
      default:
        Serial.print(">> Comando desconhecido: "); Serial.println(c);
        ajuda();
        break;
    }
  }
}

static void receberSerial2() {
  while (Serial2.available()) {
    int b = Serial2.read();
    if (b < 0) break;
    totalBytes++;
    tUltimoByte = millis();

    if (modoHex) {
      Serial.printf("%02X ", b);
      if (b == '\n') Serial.println();
    }

    // Filtro: so aceita texto ASCII imprimivel, mais CR e LF
    if (filtroRuido && b != '\n' && b != '\r' && (b < 32 || b > 126)) {
      totalDescart++;
      continue;
    }

    if (b == '\r') continue;                 // ignora CR (println envia \r\n)
    if (b == '\n') {                         // fim de linha: imprime IDENTICA ao original
      if (linha.length()) {
        if (modoHex) Serial.print("  -> ");
        Serial.println(linha);
        totalLinhas++;
        pulsoLed();
        linha = "";
      }
    } else {
      linha += (char)b;
      if (linha.length() > 400) {            // protecao: linha longa demais sem '\n'
        Serial.print("[linha longa sem fim] ");
        Serial.println(linha);
        linha = "";
      }
    }
  }
}

void setup() {
  Serial.begin(115200);
  Serial2.begin(SERIAL2_BAUD, SERIAL_8N1, SERIAL2_RX, SERIAL2_TX);
  pullupRX();                                // RX2 em pull-up constante

  pinMode(PIN_LED_PLACA, OUTPUT);
  digitalWrite(PIN_LED_PLACA, LOW);

  // Descarta qualquer lixo acumulado durante a inicializacao
  delay(100);
  while (Serial2.available()) Serial2.read();

  delay(400);
  Serial.println();
  Serial.println("=== RECEPTOR Serial2 | segundo ESP32 ===");
  Serial.printf("Serial2: RX=GPIO%d (pull-up ON)  TX=GPIO%d  baud=%d\n", SERIAL2_RX, SERIAL2_TX, SERIAL2_BAUD);
  Serial.println("Aguardando mensagens do ESP32 da porta...");
  ajuda();
  tUltimoAviso = millis();
  tPullup = millis();
}

void loop() {
  atualizarLed();
  tratarComandos();
  receberSerial2();

  // Reforca o pull-up de tempos em tempos (garante que continua ligado)
  if (millis() - tPullup > REFORCO_PULLUP_MS) {
    tPullup = millis();
    pullupRX();
  }

  // Diagnostico: avisa periodicamente enquanto nao chega nada
  if (totalBytes == 0 && millis() - tUltimoAviso > AVISO_SEM_DADOS_MS) {
    tUltimoAviso = millis();
    Serial.println("[aviso] Nenhum byte recebido no Serial2 ate agora. "
                   "Confira: TX(17) da porta -> RX(16) daqui, GND comum, baud 115200.");
  }
}
