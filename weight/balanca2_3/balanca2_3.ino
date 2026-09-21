// ================================================================
//   BALANÇA 4 PONTOS — HX711 no ESP32
//   + MÓDULO DE CONTAGEM POR PESO COM TOLERÂNCIAS
//   + SAÍDA LEGÍVEL POR MÁQUINA (protocolo serial APSEN)
// ================================================================
//
//   v2.3 = v2.2 + uma segunda VOZ na mesma porta serial.
//
//   Tudo que a 2.2 imprimia para humano continua saindo igual, e todos os
//   comandos de uma letra continuam valendo no Monitor Serial. O que entra
//   são LINHAS EXTRAS, uma por mensagem, começando em '{' — é isso que o
//   `weight-adapter` lê. Linha que não tem '{' é log, e o adapter a ignora;
//   linha que tem é mensagem, e o humano a ignora.
//
//   O formato NÃO foi inventado aqui: ele é o contrato que o resto da célula
//   já fala, escrito em `docs/PROTOCOLO_SERIAL.md` §5 e exercitado sem placa
//   por `tests/fakes/placa_weight.py`. Em uma linha:
//
//     placa → PC   {"cmd":"ping","sub":"weight"}      (quem pinga é a placa)
//     PC   → placa {"resp":"pong","epoch":<unix>}     (e é assim que o relógio acerta)
//     PC   → placa {"cmd":"<nome>","cmd_id":<n>,...}
//     placa → PC   {"resp":"ok","cmd_id":<n>}         ACK = ACEITEI, não = terminei
//     placa → PC   {"evento":{"tipo":"...",...}}      o resultado, depois
//
//   Não há ArduinoJson: as linhas saem por `snprintf` e entram por um scanner
//   de chave (`jsonTexto`/`jsonNumero`). Uma dependência a mais numa placa que
//   já está apertada de RAM não se paga por sete mensagens.
// ================================================================
#include <Preferences.h>
#include <Arduino.h>
#include <time.h>
#include <math.h>

#define FW_VERSION "2.3"

// ======================== CONFIG ========================
const int N = 4;
const int HX_DOUT[N] = {25, 26, 27, 14};
const int HX_SCK [N] = {32, 33, 19, 18};
const int MEDIAN_SAMPLES  = 7;
const int MOVAVG_SAMPLES = 8;

// ======================== TIPOS ========================
struct HX711Channel {
  int   dout_pin;
  int   sck_pin;
  bool  active;
  float calib_factor;
  long  offset_raw;
  float movbuf[MOVAVG_SAMPLES];
  int   movidx;
  bool  movfilled;
  long  last_raw;
  bool  saturated;
};

enum class CountState {
  IDLE, AWAITING_TARA, AWAITING_DEPOSIT, TRANSIENT, COUNTING, DONE
};

enum class CountResult {
  OK, UNDER_TOLERANCE, OVER_TOLERANCE, UNSTABLE, INVALID_WEIGHT, NO_UNIT_WEIGHT
};

struct CountConfig {
  float unit_weight_g;
  float container_tara_g;
  float tolerance_g;
  int   min_items;
  int   max_items;
  int   stabilization_reads;
  float stability_threshold_g;
};

struct CountOutput {
  float   total_weight_g;
  float   net_weight_g;
  float   exact_count;
  int     rounded_count;
  CountResult result;
  bool    within_tolerance;
  unsigned long timestamp;
};

// ======================== GLOBAIS ========================
HX711Channel ch[N] = {
  { 25, 32, true, 406.5217, 0, {}, 0, false, 0, false },
  { 26, 33, true, 399.7391, 0, {}, 0, false, 0, false },
  { 27, 19, true, 400.1739, 0, {}, 0, false, 0, false },
  { 14, 18, false, 404.4348, 0, {}, 0, false, 0, false }
};

Preferences prefs;

enum class ReadPhase { IDLE, READ_CHANNEL, COMPUTE_TOTAL };
ReadPhase phase = ReadPhase::IDLE;
int readChannelIdx = 0;
float channelWeights[N];
float currentTotal = 0.0f;
bool totalReady = false;

CountState   countState       = CountState::IDLE;
CountConfig  countConfig      = {0, 0, 0, 1, 9999, 3, 0.5f};
CountOutput  lastCount       = {0, 0, 0, 0, CountResult::INVALID_WEIGHT, false, 0};
float        transientBuffer[8];
int          transientIdx      = 0;
int          transientStable   = 0;
float        lastStableWeight  = 0.0f;
bool         autoPrint         = false;

// ================================================================
//   SAIDA PARA O PC CENTRAL — estado e declaracoes
// ================================================================
// As implementacoes ficam na secao de mesmo nome, la embaixo. Aqui so as
// assinaturas, porque `saveCountConfig()` e `tareAll()` — definidos logo
// abaixo — ja precisam chamar os emissores.

#define STREAM_INTERVAL_MS      200    // 5 Hz do stream `peso`
#define PING_INTERVAL_MS       3000    // quem inicia o ping e SEMPRE a placa
#define TELEMETRIA_INTERVAL_MS 15000   // periodico, e o adapter filtra repetido

// Teto de UMA linha, o mesmo dos dois lados (`serial_link.MAX_LINHA_BYTES`).
// Linha maior e DESCARTADA pelo adapter sem erro, entao mensagem que nao cabe
// NAO SAI daqui: falhar aponta para a mensagem, truncar apontaria para a placa.
static const size_t MAX_LINHA_BYTES = 1024;

// Tolerancia da balanca na conferencia do Triple Check, em %. E a mesma de
// `tests/fakes/placa_weight.py` e do weight-simulator: divergir aqui faria o
// hardware real e o simulador darem vereditos diferentes sobre a mesma massa.
static const float TOLERANCIA_PCT = 5.0f;

// Quanto o `pesar` espera a mesa parar antes de medir. Depois do ACK, entao o
// relogio que corre do outro lado e o TIMEOUT_PESO do orquestrador (15 s).
static const unsigned long PESAR_ESTAB_TIMEOUT_MS = 3000;

// `os_id` do central e VARCHAR(60) — 64 cobre com folga. O limite sai da
// ORIGEM do dado, nao do tamanho que ele tem hoje: dois disparos do mesmo
// template diferem no FIM da string, e cortar o fim faz dois ids virarem um.
#define MAX_OS_ID_LEN 64

// Buffer de uma linha de saida. `peso_ok` e a maior mensagem do contrato
// (~450 B com todos os campos) e e ela que dimensiona este numero.
static char   jbuf[700];

static bool          streamOn      = true;
static unsigned long lastStreamMs  = 0;
static unsigned long lastPingMs    = 0;
static unsigned long lastTelemMs   = 0;

// Relogio: a placa nao tem RTC nem NTP. O `epoch` do pong e a unica fonte, e
// ate o primeiro pong os carimbos saem em 1970 — visivelmente errado, que e
// melhor que plausivel e errado.
static unsigned long epochBase   = 0;
static unsigned long epochMillis = 0;

// Estabilidade do STREAM. Variaveis PROPRIAS de proposito: `transientBuffer` e
// `transientStable` pertencem a maquina de contagem, e reusa-los faria um
// stream de fundo mexer no resultado de uma contagem em andamento.
static float streamUltimo   = NAN;
static int   streamEstaveis = 0;
static bool  streamEstavel  = false;

// Saturacao: o aviso humano sai a cada leitura (como na 2.2), mas o evento so
// na TRANSICAO nao-saturado -> saturado. Um evento por leitura encheria a
// linha de 115200 baud que os ACKs do adapter estao esperando.
static bool satAnterior[N] = { false, false, false, false };

// Estado da OS em curso (comandos `tara` e `pesar` do APSEN). `taraOsG` e o
// zero LOGICO da mesa — nao mexe no offset do HX711, que e calibracao — e
// `acumuladoOsG` e o quanto ja foi contabilizado, para que cada `pesar`
// devolva o DELTA daquele slot e nao a mesa inteira.
static float taraOsG      = 0.0f;
static float acumuladoOsG = 0.0f;

static long  ultimoCmdId  = 0;

// Quando chegou o ultimo pong. O adapter so responde pong ao NOSSO ping, entao
// silencio longo e o adapter fora do ar — e quando ele volta, o contador de
// `cmd_id` dele volta a nascer em 1. Ver `processarJson`.
static unsigned long ultimoPongMs = 0;
static const unsigned long SESSAO_SILENCIO_MS = 10000;   // > 3 pings sem pong

// A SESSAO do adapter: o epoch de boot do processo do outro lado. Ele a manda
// no pong e em TODO comando, e esta placa so precisa notar que ela MUDOU.
//
// A heuristica do silencio (acima) cobre o adapter que ficou fora por mais de
// 10 s. Ela nao cobre o restart RAPIDO, que e o comum: 2 a 5 s de queda nao
// chegam perto de tres pings perdidos, e nesse caso o contador do adapter
// nascia em 1 com esta placa ainda em `ultimoCmdId = 47`. Todo comando ate 47
// recebia ackOk(repetido) SEM EXECUTAR — e um `pesar` que nao executa e um
// `peso_ok` que nunca chega, com a OS abortando por timeout numa bancada
// intacta.
//
// Zero significa "ainda nao sei": a primeira sessao vista e adotada sem zerar
// nada, senao todo boot da placa descartaria o primeiro comando legitimo.
//
// Identico ao `apsen_serial.h` das outras tres placas, que esta balanca ainda
// nao inclui — `tests/test_balanca_serial.py` confronta as duas copias.
static unsigned long sessaoAdapter = 0;

void setCountState(CountState s);
void emitBoot();
void emitPeso();
void emitCfg();
void emitEstado();
void emitContagem(const CountOutput &out);
void emitTaraBalanca(const char *alvo, float valor_g);
void emitErroBalanca(const char *msg, const char *cmd);
void emitTelemetria();
void emitPing();
void despacharLinha(const String &linha);

// ================================================================
//   NVS
// ================================================================
void saveCalibration() {
  prefs.begin("hx711", false);
  for (int i = 0; i < N; i++) {
    prefs.putFloat(("cal" + String(i)).c_str(), ch[i].calib_factor);
    prefs.putLong(("off" + String(i)).c_str(), ch[i].offset_raw);
    prefs.putBool(("act" + String(i)).c_str(), ch[i].active);
  }
  prefs.end();
  Serial.println(F("Calibracao salva."));
}

void loadCalibration() {
  prefs.begin("hx711", true);
  for (int i = 0; i < N; i++) {
    ch[i].calib_factor = prefs.getFloat(("cal" + String(i)).c_str(), ch[i].calib_factor);
    ch[i].offset_raw   = prefs.getLong(("off" + String(i)).c_str(), 0);
    ch[i].active        = prefs.getBool(("act" + String(i)).c_str(), ch[i].active);
  }
  prefs.end();
  Serial.println(F("Calibracao carregada."));
}

void saveCountConfig() {
  prefs.begin("count", false);
  prefs.putFloat("uw",     countConfig.unit_weight_g);
  prefs.putFloat("tara",   countConfig.container_tara_g);
  prefs.putFloat("tol",    countConfig.tolerance_g);
  prefs.putInt("min",      countConfig.min_items);
  prefs.putInt("max",      countConfig.max_items);
  prefs.putInt("sreads",   countConfig.stabilization_reads);
  prefs.putFloat("sthres", countConfig.stability_threshold_g);
  prefs.end();
  Serial.println(F("Config contagem salva."));
  emitCfg();
}

void loadCountConfig() {
  prefs.begin("count", true);
  countConfig.unit_weight_g         = prefs.getFloat("uw", 0.0f);
  countConfig.container_tara_g      = prefs.getFloat("tara", 0.0f);
  countConfig.tolerance_g           = prefs.getFloat("tol", 0.0f);
  countConfig.min_items             = prefs.getInt("min", 1);
  countConfig.max_items             = prefs.getInt("max", 9999);
  countConfig.stabilization_reads   = prefs.getInt("sreads", 3);
  countConfig.stability_threshold_g = prefs.getFloat("sthres", 0.5f);
  prefs.end();
  Serial.println(F("Config contagem carregada."));
}

// ================================================================
//   HX711 BAIXO NIVEL
// ================================================================
inline bool hx_ready(int i) {
  if (!ch[i].active) return false;
  return digitalRead(ch[i].dout_pin) == LOW;
}

long hx_read_raw(int i) {
  if (!ch[i].active) return 0;
  unsigned long t0 = millis();
  while (!hx_ready(i)) {
    if (!ch[i].active) return 0;
    if (millis() - t0 > 500) return 0;
    delayMicroseconds(10);
  }
  long v = 0;
  portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
  portENTER_CRITICAL(&mux);
  for (int b = 0; b < 24; b++) {
    digitalWrite(ch[i].sck_pin, HIGH);
    delayMicroseconds(1);
    v = (v << 1) | (digitalRead(ch[i].dout_pin) ? 1 : 0);
    digitalWrite(ch[i].sck_pin, LOW);
    delayMicroseconds(1);
  }
  digitalWrite(ch[i].sck_pin, HIGH);
  delayMicroseconds(1);
  digitalWrite(ch[i].sck_pin, LOW);
  delayMicroseconds(1);
  portEXIT_CRITICAL(&mux);
  if (v & 0x800000) v |= ~0xFFFFFF;
  ch[i].last_raw = v;
  ch[i].saturated = (v == 0x7FFFFF || v == (long)0x800000);
  return v;
}

long readRawMedian(int i, int n) {
  if (!ch[i].active) return 0;
  long buf[15];
  for (int k = 0; k < n; k++) buf[k] = hx_read_raw(i);
  for (int a = 1; a < n; a++) {
    long key = buf[a];
    int b = a - 1;
    while (b >= 0 && buf[b] > key) { buf[b + 1] = buf[b]; b--; }
    buf[b + 1] = key;
  }
  return buf[n / 2];
}

float movavg(int i, float x) {
  if (!ch[i].active) return 0.0f;
  ch[i].movbuf[ch[i].movidx] = x;
  ch[i].movidx = (ch[i].movidx + 1) % MOVAVG_SAMPLES;
  if (!ch[i].movfilled && ch[i].movidx == 0) ch[i].movfilled = true;
  int n = ch[i].movfilled ? MOVAVG_SAMPLES : ch[i].movidx;
  float s = 0;
  for (int k = 0; k < n; k++) s += ch[i].movbuf[k];
  return n > 0 ? (s / n) : 0.0f;
}

// ================================================================
//   OPERACOES POR CANAL
// ================================================================
void tareChannel(int i) {
  if (!ch[i].active) return;
  long prev = readRawMedian(i, MEDIAN_SAMPLES);
  int stable = 0;
  for (int attempt = 0; attempt < 10 && stable < 3; attempt++) {
    delay(150);
    long curr = readRawMedian(i, MEDIAN_SAMPLES);
    if (abs(curr - prev) <= 2) stable++; else stable = 0;
    prev = curr;
  }
  ch[i].offset_raw = readRawMedian(i, MEDIAN_SAMPLES);
  Serial.printf("Tara c%d: offset=%ld\n", i, ch[i].offset_raw);
}

void tareAll() {
  for (int i = 0; i < N; i++) if (ch[i].active) tareChannel(i);
  saveCalibration();
  Serial.println(F("TARA OK."));
  emitTaraBalanca("canais", NAN);
}

float readWeight_g_channel(int i) {
  if (!ch[i].active) return 0.0f;
  long raw = readRawMedian(i, MEDIAN_SAMPLES);
  if (ch[i].saturated) {
    Serial.printf("AVISO: Canal %d SATURADO!\n", i);
    if (!satAnterior[i]) {
      satAnterior[i] = true;
      char msg[48];
      snprintf(msg, sizeof msg, "canal %d saturado", i);
      emitErroBalanca(msg, NULL);
    }
  } else {
    satAnterior[i] = false;
  }
  long net = raw - ch[i].offset_raw;
  float g = (float)net / ch[i].calib_factor;
  return movavg(i, g);
}

float readWeight_g_total() {
  float sum = 0;
  for (int i = 0; i < N; i++) sum += readWeight_g_channel(i);
  return sum;
}

// ================================================================
//   HELPERS SERIAL
// ================================================================
void flushSerial() { while (Serial.available()) Serial.read(); }

// Aguarda ENTER do usuario sem descartar o que foi digitado
String readSerialLine(unsigned long timeout_ms = 15000) {
  unsigned long t0 = millis();
  while (!Serial.available() && (millis() - t0 < timeout_ms)) {
    delay(10);
  }
  if (!Serial.available()) return "";
  delay(50);  // garante que todos os bytes chegaram
  String s = Serial.readStringUntil('\n');
  s.trim();
  return s;
}

// ================================================================
//   CALIBRACAO INTERATIVA
// ================================================================
void calibrarCanal(int i) {
  if (i < 0 || i >= N) return;
  if (!ch[i].active) { Serial.printf("Canal %d INATIVO.\n", i); return; }
  Serial.printf("\n=== CALIBRACAO CANAL %d ===\n", i);
  Serial.println(F("1) Retire o peso e digite ENTER:"));
  flushSerial();
  readSerialLine();
  tareChannel(i);
  Serial.println(F("2) Coloque peso conhecido, digite valor (g) + ENTER:"));
  String s = readSerialLine();
  float ref_g = s.toFloat();
  if (ref_g <= 0) { Serial.println(F("Valor invalido.")); return; }
  delay(800);
  long raw = readRawMedian(i, MEDIAN_SAMPLES);
  long net = raw - ch[i].offset_raw;
  if (net <= 0) { Serial.println(F("Leitura nula.")); return; }
  ch[i].calib_factor = (float)net / ref_g;
  saveCalibration();
  Serial.printf("Fator canal %d: %.4f cont/g\n", i, ch[i].calib_factor);
}

// ================================================================
//   MAQUINA DE LEITURA
// ================================================================
void stepReading() {
  switch (phase) {
    case ReadPhase::IDLE:
      readChannelIdx = 0;
      phase = ReadPhase::READ_CHANNEL;
      break;
    case ReadPhase::READ_CHANNEL:
      if (readChannelIdx < N) {
        channelWeights[readChannelIdx] = ch[readChannelIdx].active
          ? readWeight_g_channel(readChannelIdx) : 0.0f;
        readChannelIdx++;
      } else {
        phase = ReadPhase::COMPUTE_TOTAL;
      }
      break;
    case ReadPhase::COMPUTE_TOTAL:
      currentTotal = 0;
      for (int i = 0; i < N; i++) currentTotal += channelWeights[i];
      totalReady = true;
      phase = ReadPhase::IDLE;
      break;
  }
}

// ================================================================
//   IMPRESSAO
// ================================================================
void printChannels() {
  Serial.print(F("Canais [g]: "));
  for (int i = 0; i < N; i++) {
    if (ch[i].active)
      Serial.print(ch[i].saturated ? F("SAT") : String(channelWeights[i], 2));
    else
      Serial.print(F("--"));
    Serial.print(i < N - 1 ? ' ' : '\n');
  }
  Serial.print(F("Total [g]: "));
  Serial.println(currentTotal, 2);
}

void printMap() {
  Serial.println(F("\n=== MAPA DE CANAIS ==="));
  for (int i = 0; i < N; i++) {
    Serial.printf("c%d DOUT=%d SCK=%d [%s] cal=%.4f off=%ld\n",
                  i, ch[i].dout_pin, ch[i].sck_pin,
                  ch[i].active ? "ATIVO" : "INATIVO",
                  ch[i].calib_factor, ch[i].offset_raw);
  }
  Serial.println();
}

// ================================================================
//   MODULO DE CONTAGEM POR PESO
// ================================================================
float calcNetWeight(float total_weight_g, float container_tara_g) {
  return total_weight_g - container_tara_g;
}

float calcExactCount(float net_weight_g, float unit_weight_g) {
  if (unit_weight_g <= 0.0f) return -1.0f;
  return net_weight_g / unit_weight_g;
}

int roundCount(float exact_count, float unit_weight_g, float tolerance_g,
               CountResult &outResult) {
  if (exact_count < 0.0f) {
    outResult = CountResult::NO_UNIT_WEIGHT;
    return 0;
  }
  float fractional = exact_count - floor(exact_count);
  float frac_weight_g = fractional * unit_weight_g;
  int floor_count = (int)floor(exact_count);
  float remaining_weight = unit_weight_g - frac_weight_g;

  if (frac_weight_g <= tolerance_g) {
    if (fractional < 0.5f) {
      outResult = CountResult::OK;
      return floor_count;
    } else {
      outResult = CountResult::OK;
      return floor_count + 1;
    }
  } else if (remaining_weight <= tolerance_g) {
    outResult = CountResult::OK;
    return floor_count + 1;
  } else {
    outResult = (fractional < 0.5f) ? CountResult::UNDER_TOLERANCE
                                    : CountResult::OVER_TOLERANCE;
    return floor_count;
  }
}

bool validateCountRange(int rounded_count, int min_items, int max_items) {
  return (rounded_count >= min_items && rounded_count <= max_items);
}

bool isWeightStable(int stabilized_reads, float threshold_g) {
  return (transientStable >= stabilized_reads);
}

void feedTransientBuffer(float weight) {
  if (transientStable == 0) {
    transientBuffer[transientIdx] = weight;
    transientIdx = (transientIdx + 1) % 8;
    transientStable = 1;
    lastStableWeight = weight;
    return;
  }
  float diff = fabs(weight - lastStableWeight);
  if (diff <= countConfig.stability_threshold_g) {
    transientStable++;
  } else {
    transientStable = 0;
  }
  transientBuffer[transientIdx] = weight;
  transientIdx = (transientIdx + 1) % 8;
  lastStableWeight = weight;
}

void resetTransientBuffer() {
  transientIdx = 0;
  transientStable = 0;
  lastStableWeight = 0.0f;
  for (int i = 0; i < 8; i++) transientBuffer[i] = 0.0f;
}

CountOutput countByWeight(float total_weight_g, const CountConfig &cfg) {
  CountOutput out;
  out.timestamp = millis();
  out.total_weight_g = total_weight_g;

  if (cfg.unit_weight_g <= 0.0f) {
    out.result = CountResult::NO_UNIT_WEIGHT;
    out.within_tolerance = false;
    out.net_weight_g = 0;
    out.exact_count = 0;
    out.rounded_count = 0;
    return out;
  }

  out.net_weight_g = calcNetWeight(total_weight_g, cfg.container_tara_g);

  if (out.net_weight_g <= 0.0f) {
    out.result = CountResult::INVALID_WEIGHT;
    out.within_tolerance = false;
    out.exact_count = 0;
    out.rounded_count = 0;
    return out;
  }

  out.exact_count = calcExactCount(out.net_weight_g, cfg.unit_weight_g);
  out.rounded_count = roundCount(out.exact_count, cfg.unit_weight_g,
                                  cfg.tolerance_g, out.result);
  bool in_range = validateCountRange(out.rounded_count, cfg.min_items, cfg.max_items);
  out.within_tolerance = (out.result == CountResult::OK) && in_range;
  return out;
}

void printCountResult(const CountOutput &out) {
  Serial.println(F("\n=== RESULTADO DA CONTAGEM ==="));
  Serial.printf("Peso total:     %.2f g\n", out.total_weight_g);
  Serial.printf("Peso liquido:   %.2f g\n", out.net_weight_g);
  Serial.printf("Contagem exata:  %.3f\n", out.exact_count);
  Serial.printf("Contagem:        %d\n", out.rounded_count);

  const char* resStr = "?";
  switch (out.result) {
    case CountResult::OK:               resStr = "OK"; break;
    case CountResult::UNDER_TOLERANCE:  resStr = "ABAIXO TOLERANCIA"; break;
    case CountResult::OVER_TOLERANCE:   resStr = "ACIMA TOLERANCIA"; break;
    case CountResult::UNSTABLE:         resStr = "INSTAVEL"; break;
    case CountResult::INVALID_WEIGHT:   resStr = "PESO INVALIDO"; break;
    case CountResult::NO_UNIT_WEIGHT:   resStr = "SEM PESO UNITARIO"; break;
  }
  Serial.printf("Status:          %s\n", resStr);
  Serial.printf("Aceite:          %s\n", out.within_tolerance ? "SIM" : "NAO");
  Serial.println(F("==============================\n"));
}

// ================================================================
//   MAQUINA DE CONTAGEM
// ================================================================
void stepCounting() {
  switch (countState) {
    case CountState::IDLE:
      break;

    case CountState::AWAITING_TARA: {
      float w = readWeight_g_total();
      countConfig.container_tara_g = w;
      saveCountConfig();
      Serial.printf("Tara do recipiente: %.2f g\n", w);
      emitTaraBalanca("recipiente", w);
      resetTransientBuffer();
      setCountState(CountState::AWAITING_DEPOSIT);
      break;
    }

    case CountState::AWAITING_DEPOSIT:
      break;

    case CountState::TRANSIENT: {
      float w = readWeight_g_total();
      feedTransientBuffer(w);
      if (isWeightStable(countConfig.stabilization_reads,
                         countConfig.stability_threshold_g)) {
        Serial.printf("Peso estavel: %.2f g (%d leituras)\n",
                      lastStableWeight, transientStable);
        setCountState(CountState::COUNTING);
      }
      break;
    }

    case CountState::COUNTING: {
      float w = readWeight_g_total();
      lastCount = countByWeight(w, countConfig);
      printCountResult(lastCount);
      emitContagem(lastCount);
      setCountState(CountState::DONE);
      break;
    }

    case CountState::DONE:
      break;
  }
}

// ================================================================
//   TESTES
// ================================================================
int runTests() {
  int passed = 0;
  int total  = 0;
  CountOutput out;

  Serial.println(F("\n===== INICIANDO TESTES ====="));

  CountConfig tc;
  tc.unit_weight_g         = 10.0f;
  tc.container_tara_g      = 50.0f;
  tc.tolerance_g           = 1.5f;
  tc.min_items             = 1;
  tc.max_items             = 1000;
  tc.stabilization_reads   = 3;
  tc.stability_threshold_g = 0.5f;

  auto check = [&](const char* name, CountResult expR, bool expW) {
    total++;
    if (out.result == expR && out.within_tolerance == expW) {
      Serial.printf("  [PASS] %s\n", name); passed++;
    } else {
      Serial.printf("  [FAIL] %s (esp r=%d w=%d, obt r=%d w=%d)\n",
                    name, (int)expR, expW, (int)out.result, out.within_tolerance);
    }
  };

  auto checkCount = [&](const char* name, int exp) {
    total++;
    if (out.rounded_count == exp) { Serial.printf("  [PASS] %s\n", name); passed++; }
    else Serial.printf("  [FAIL] %s (esp=%d, obt=%d)\n", name, exp, out.rounded_count);
  };

  out = countByWeight(100.0f, tc);
  check("T1: 5 exatos", CountResult::OK, true);
  checkCount("T1: count=5", 5);

  out = countByWeight(100.8f, tc);
  check("T2: +0.8g (tol)", CountResult::OK, true);
  checkCount("T2: count=5", 5);

  out = countByWeight(99.3f, tc);
  check("T3: -0.7g (tol)", CountResult::OK, true);
  checkCount("T3: count=5", 5);

  out = countByWeight(103.0f, tc);
  check("T4: 5.3 (fora tol)", CountResult::UNDER_TOLERANCE, false);
  checkCount("T4: count=5", 5);

  out = countByWeight(109.2f, tc);
  check("T5: 5.92 (tol cima)", CountResult::OK, true);
  checkCount("T5: count=6", 6);

  out = countByWeight(40.0f, tc);
  check("T6: peso < tara", CountResult::INVALID_WEIGHT, false);

  CountConfig noUnit = tc; noUnit.unit_weight_g = 0.0f;
  out = countByWeight(100.0f, noUnit);
  check("T7: sem peso unit", CountResult::NO_UNIT_WEIGHT, false);

  CountConfig tight = tc; tight.min_items = 10; tight.max_items = 20;
  out = countByWeight(100.0f, tight);
  total++;
  if (!out.within_tolerance && out.rounded_count == 5) {
    Serial.println(F("  [PASS] T8: fora faixa")); passed++;
  } else Serial.printf("  [FAIL] T8: w=%d c=%d\n", out.within_tolerance, out.rounded_count);

  out = countByWeight(1050.0f, tc);
  check("T9: 100 itens", CountResult::OK, true);
  checkCount("T9: count=100", 100);

  out = countByWeight(60.0f, tc);
  check("T10: 1 item", CountResult::OK, true);
  checkCount("T10: count=1", 1);

  out = countByWeight(100.5f, tc);
  check("T11: 5.05 (margem)", CountResult::OK, true);
  checkCount("T11: count=5", 5);

  resetTransientBuffer();
  feedTransientBuffer(100.0f);
  feedTransientBuffer(100.1f);
  feedTransientBuffer(100.2f);
  total++;
  if (isWeightStable(3, 0.5f)) { Serial.println(F("  [PASS] T12: estavel")); passed++; }
  else Serial.println(F("  [FAIL] T12: deveria estar estavel"));

  resetTransientBuffer();
  feedTransientBuffer(100.0f);
  feedTransientBuffer(105.0f);
  feedTransientBuffer(103.0f);
  total++;
  if (!isWeightStable(3, 0.5f)) { Serial.println(F("  [PASS] T13: instavel")); passed++; }
  else Serial.println(F("  [FAIL] T13: nao deveria estar estavel"));

  Serial.printf("\n===== RESUMO: %d/%d passaram =====\n\n", passed, total);
  return passed;
}

// ================================================================
//   COMANDOS SERIAIS
// ================================================================
void processCommand(String cmd) {

  // --- Balanca ---
  if (cmd == "t") {
    tareAll();
  } else if (cmd == "p") {
    printChannels();
  } else if (cmd == "m") {
    printMap();
  } else if (cmd == "a") {
    autoPrint = !autoPrint;
    Serial.printf("Auto-print: %s\n", autoPrint ? "ON" : "OFF");
  } else if (cmd == "s") {
    saveCalibration();
  } else if (cmd == "l") {
    loadCalibration();
  } else if (cmd.length() == 2 && cmd[0] == 'c' && cmd[1] >= '0' && cmd[1] < '0' + N) {
    calibrarCanal(cmd[1] - '0');
  } else if (cmd.length() == 2 && cmd[0] == '+' && cmd[1] >= '0' && cmd[1] < '0' + N) {
    int i = cmd[1] - '0'; ch[i].active = true; saveCalibration();
    Serial.printf("Canal %d ATIVADO.\n", i);
  } else if (cmd.length() == 2 && cmd[0] == '-' && cmd[1] >= '0' && cmd[1] < '0' + N) {
    int i = cmd[1] - '0'; ch[i].active = false; saveCalibration();
    Serial.printf("Canal %d DESATIVADO.\n", i);

  // --- Contagem: peso unitario (interativo) ---
  } else if (cmd == "u") {
    Serial.println(F("Digite o peso unitario (g) + ENTER:"));
    Serial.flush();
    delay(50);
    while (Serial.available()) Serial.read();  // limpa sujeira
    String s = readSerialLine(15000);
    if (s.length() == 0) {
      Serial.println(F("Timeout."));
      emitErroBalanca("timeout esperando peso unitario", "u");
      return;
    }
    float v = s.toFloat();
    if (v > 0.0f) {
      countConfig.unit_weight_g = v;
      saveCountConfig();
      Serial.printf("Peso unitario: %.4f g (salvo)\n", v);
    } else {
      Serial.printf("Valor invalido. Recebido: '%s'\n", s.c_str());
      Serial.println(F("Digite apenas o numero. Ex: 15 ou 10.5"));
      emitErroBalanca("peso unitario invalido", "u");
    }

  // --- Contagem: peso unitario (direto) ---
  } else if (cmd.length() > 1 && cmd[0] == 'g') {
    float v = cmd.substring(1).toFloat();
    if (v > 0.0f) {
      countConfig.unit_weight_g = v;
      saveCountConfig();
      Serial.printf("Peso unitario: %.4f g (salvo)\n", v);
    } else {
      Serial.println(F("Formato: g<valor>. Ex: g15 ou g10.5"));
      emitErroBalanca("peso unitario invalido", "g");
    }

  // --- Contagem: tara do recipiente ---
  } else if (cmd == "k") {
    setCountState(CountState::AWAITING_TARA);
    Serial.println(F("Coloque o recipiente vazio..."));

  // --- Contagem: depositar e contar ---
  } else if (cmd == "x") {
    if (countConfig.unit_weight_g <= 0) {
      Serial.println(F("ERRO: Configure o peso unitario primeiro ('u' ou 'g<valor>')."));
      emitErroBalanca("configure o peso unitario primeiro", "x");
    } else {
      resetTransientBuffer();
      setCountState(CountState::TRANSIENT);
      Serial.println(F("Deposite os itens. Aguardando estabilizacao..."));
    }

  // --- Contagem: ultimo resultado ---
  } else if (cmd == "n") {
    printCountResult(lastCount);
    emitContagem(lastCount);

  // --- Contagem: testes ---
  } else if (cmd == "T") {
    runTests();

  // --- Contagem: tolerancia ---
  } else if (cmd.length() > 1 && cmd[0] == 'o') {
    float v = cmd.substring(1).toFloat();
    if (v >= 0.0f) {
      countConfig.tolerance_g = v;
      saveCountConfig();
      Serial.printf("Tolerancia: ±%.2f g\n", v);
    } else {
      Serial.println(F("Formato: o<valor>. Ex: o1.5"));
      emitErroBalanca("tolerancia invalida", "o");
    }

  // --- Contagem: faixa min-max ---
  } else if (cmd.length() > 1 && cmd[0] == 'i') {
    String range = cmd.substring(1);
    int dash = range.indexOf('-');
    if (dash > 0) {
      int mn = range.substring(0, dash).toInt();
      int mx = range.substring(dash + 1).toInt();
      if (mn > 0 && mx >= mn) {
        countConfig.min_items = mn;
        countConfig.max_items = mx;
        saveCountConfig();
        Serial.printf("Faixa: %d a %d itens\n", mn, mx);
      } else {
        Serial.println(F("Invalido. Ex: i1-100"));
        emitErroBalanca("faixa invalida", "i");
      }
    } else {
      Serial.println(F("Formato: i<min>-<max>. Ex: i1-100"));
      emitErroBalanca("faixa invalida", "i");
    }

  // --- Contagem: leituras de estabilizacao ---
  } else if (cmd.length() > 1 && cmd[0] == 'r') {
    int v = cmd.substring(1).toInt();
    if (v > 0 && v <= 20) {
      countConfig.stabilization_reads = v;
      saveCountConfig();
      Serial.printf("Leituras estabilizacao: %d\n", v);
    } else {
      Serial.println(F("Formato: r<1-20>. Ex: r3"));
      emitErroBalanca("leituras de estabilizacao invalidas", "r");
    }

  // --- Contagem: limiar de estabilidade ---
  } else if (cmd.length() > 1 && cmd[0] == 'h') {
    float v = cmd.substring(1).toFloat();
    if (v > 0.0f) {
      countConfig.stability_threshold_g = v;
      saveCountConfig();
      Serial.printf("Limiar estabilidade: %.2f g\n", v);
    } else {
      Serial.println(F("Formato: h<valor>. Ex: h0.5"));
      emitErroBalanca("limiar de estabilidade invalido", "h");
    }

  // --- Contagem: imprimir config ---
  } else if (cmd == "C") {
    Serial.println(F("\n=== CONFIG CONTAGEM ==="));
    Serial.printf("Peso unitario:   %.4f g\n", countConfig.unit_weight_g);
    Serial.printf("Tara recipiente: %.2f g\n", countConfig.container_tara_g);
    Serial.printf("Tolerancia:      ±%.2f g\n", countConfig.tolerance_g);
    Serial.printf("Min itens:       %d\n", countConfig.min_items);
    Serial.printf("Max itens:       %d\n", countConfig.max_items);
    Serial.printf("Leituras estab:  %d\n", countConfig.stabilization_reads);
    Serial.printf("Limiar estab:    %.2f g\n", countConfig.stability_threshold_g);
    Serial.println(F("========================\n"));
    emitCfg();

  // --- Stream JSON de peso (ligado por padrao) ---
  } else if (cmd == "j") {
    streamOn = !streamOn;
    Serial.printf("Stream JSON de peso: %s\n", streamOn ? "ON" : "OFF");
    emitCfg();

  // --- Ajuda ---
  } else if (cmd == "?") {
    Serial.println(F("\n=== COMANDOS ==="));
    Serial.println(F("  --- Balanca ---"));
    Serial.println(F("  t = tara canais ativos"));
    Serial.println(F("  p = imprimir canais"));
    Serial.println(F("  m = mapa de canais"));
    Serial.println(F("  a = auto-print on/off"));
    Serial.println(F("  s = salvar calibracao NVS"));
    Serial.println(F("  l = carregar calibracao NVS"));
    Serial.println(F("  c0..c3 = calibrar canal"));
    Serial.println(F("  +0..+3 = ativar canal"));
    Serial.println(F("  -0..-3 = desativar canal"));
    Serial.println(F("  --- Contagem ---"));
    Serial.println(F("  u = peso unitario (interativo)"));
    Serial.println(F("  g<valor> = peso unitario direto (ex: g15)"));
    Serial.println(F("  k = tara do recipiente"));
    Serial.println(F("  o<valor> = tolerancia g (ex: o1.5)"));
    Serial.println(F("  i<min>-<max> = faixa (ex: i1-100)"));
    Serial.println(F("  r<valor> = leituras estabilizacao (ex: r3)"));
    Serial.println(F("  h<valor> = limiar estabilidade g (ex: h0.5)"));
    Serial.println(F("  x = depositar itens e contar"));
    Serial.println(F("  n = ultimo resultado"));
    Serial.println(F("  T = executar testes"));
    Serial.println(F("  C = config atual"));
    Serial.println(F("  j = stream JSON de peso on/off\n"));
  } else {
    Serial.printf("Comando desconhecido: '%s' (use ?)\n", cmd.c_str());
    emitErroBalanca("comando desconhecido", NULL);
  }
}

// ================================================================
//   SAIDA PARA O PC CENTRAL
// ================================================================
//
// Uma mensagem por linha, sempre comecando em '{'. Os `Serial.print` humanos
// da 2.2 continuam todos onde estavam: o que sai daqui sao linhas EXTRAS, e e
// o '{' que separa as duas vozes na mesma porta. O adapter ignora a linha sem
// '{' (para ele e log) e o humano ignora a linha com '{'.
//
// Nada aqui interpreta a regra de negocio da contagem: `countByWeight`,
// `roundCount` e os filtros do HX711 sao os mesmos da 2.2, byte a byte. Esta
// secao so LE o resultado deles e o publica.

// ── Formatacao ───────────────────────────────────────────────────────────────

static void fmtF(char *dest, size_t n, float v, int casas) {
  // NaN e inf nao existem em JSON. Emiti-los como `nan` faria o `json.loads`
  // do adapter descartar a linha INTEIRA — um campo estragado levaria junto os
  // quinze que estavam certos, e o sintoma seria uma pesagem que nunca chegou.
  if (isnan(v) || isinf(v)) { snprintf(dest, n, "null"); return; }
  snprintf(dest, n, "%.*f", casas, v);
}

static void tsAgora(char *dest, size_t n) {
  // A placa nao tem RTC nem NTP: a unica fonte de hora e o `epoch` que vem no
  // pong. Antes do primeiro pong o carimbo sai em 1970 — visivelmente errado,
  // que e o que se quer. Hora plausivel e errada seria pior.
  unsigned long seg = epochBase + (millis() - epochMillis) / 1000UL;
  time_t t = (time_t)seg;
  struct tm g;
  gmtime_r(&t, &g);
  strftime(dest, n, "%Y-%m-%dT%H:%M:%S", &g);
}

static void emitir(int escritos, const char *origem) {
  // Mensagem que nao cabe NAO SAI. O adapter descarta a linha acima do teto
  // sem erro nenhum, e uma linha truncada e JSON invalido: falhar aqui aponta
  // para a mensagem, deixar sair truncada apontaria para a placa.
  if (escritos < 0 || (size_t)escritos >= sizeof(jbuf) ||
      (size_t)escritos > MAX_LINHA_BYTES) {
    Serial.printf("AVISO: mensagem '%s' (%d bytes) passou do teto da linha e NAO saiu.\n",
                  origem, escritos);
    return;
  }
  Serial.println(jbuf);
}

static const char *nomeEstado(CountState s) {
  switch (s) {
    case CountState::IDLE:             return "IDLE";
    case CountState::AWAITING_TARA:    return "AWAITING_TARA";
    case CountState::AWAITING_DEPOSIT: return "AWAITING_DEPOSIT";
    case CountState::TRANSIENT:        return "TRANSIENT";
    case CountState::COUNTING:         return "COUNTING";
    case CountState::DONE:             return "DONE";
  }
  return "DESCONHECIDO";
}

static const char *nomeStatus(CountResult r) {
  switch (r) {
    case CountResult::OK:              return "OK";
    case CountResult::UNDER_TOLERANCE: return "UNDER_TOLERANCE";
    case CountResult::OVER_TOLERANCE:  return "OVER_TOLERANCE";
    case CountResult::UNSTABLE:        return "UNSTABLE";
    case CountResult::INVALID_WEIGHT:  return "INVALID_WEIGHT";
    case CountResult::NO_UNIT_WEIGHT:  return "NO_UNIT_WEIGHT";
  }
  return "DESCONHECIDO";
}

// ── Mensagens de servico ─────────────────────────────────────────────────────

void emitPing() {
  // Quem inicia o ping e SEMPRE a placa: e assim que o adapter descobre qual
  // porta e a da balanca, sem depender de VID/PID (o mesmo conversor
  // USB-serial aparece em placas de fabricantes diferentes, e casar por ele
  // mandaria `dispensar` para a balanca).
  emitir(snprintf(jbuf, sizeof jbuf, "{\"cmd\":\"ping\",\"sub\":\"weight\"}"), "ping");
}

// ── Eventos da BANCADA (ficam no adapter, nao vao ao central) ───────────────

void emitBoot() {
  char ts[24];  tsAgora(ts, sizeof ts);
  char uw[16];  fmtF(uw, sizeof uw, countConfig.unit_weight_g, 2);
  char ativos[48] = "";
  size_t usado = 0;
  for (int i = 0; i < N; i++)
    usado += snprintf(ativos + usado, sizeof(ativos) - usado, "%s%s",
                      i ? "," : "", ch[i].active ? "true" : "false");
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"boot\",\"fw\":\"" FW_VERSION "\","
    "\"canais_ativos\":[%s],\"uw_g\":%s,\"ts\":\"%s\"}}", ativos, uw, ts), "boot");
}

void emitPeso() {
  char ts[24];    tsAgora(ts, sizeof ts);
  char total[16]; fmtF(total, sizeof total, currentTotal, 2);
  char canais[80] = "";
  size_t usado = 0;
  bool sat = false;
  for (int i = 0; i < N; i++) {
    char v[16];
    if (ch[i].active) {
      fmtF(v, sizeof v, channelWeights[i], 2);
      if (ch[i].saturated) sat = true;
    } else {
      // Canal inativo nao tem leitura, e zero seria uma leitura. `null` diz
      // "nao ha canal aqui", que e o que o painel precisa saber.
      snprintf(v, sizeof v, "null");
    }
    usado += snprintf(canais + usado, sizeof(canais) - usado, "%s%s", i ? "," : "", v);
  }
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"peso\",\"total_g\":%s,\"canais_g\":[%s],"
    "\"sat\":%s,\"estavel\":%s,\"ts\":\"%s\"}}",
    total, canais, sat ? "true" : "false", streamEstavel ? "true" : "false", ts),
    "peso");
}

void emitCfg() {
  char ts[24]; tsAgora(ts, sizeof ts);
  char uw[16], tara[16], tol[16], sthres[16];
  fmtF(uw,     sizeof uw,     countConfig.unit_weight_g,         2);
  fmtF(tara,   sizeof tara,   countConfig.container_tara_g,      2);
  fmtF(tol,    sizeof tol,    countConfig.tolerance_g,           2);
  fmtF(sthres, sizeof sthres, countConfig.stability_threshold_g, 2);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"cfg\",\"uw_g\":%s,\"tara_g\":%s,\"tol_g\":%s,"
    "\"min\":%d,\"max\":%d,\"sreads\":%d,\"sthres_g\":%s,\"ts\":\"%s\"}}",
    uw, tara, tol, countConfig.min_items, countConfig.max_items,
    countConfig.stabilization_reads, sthres, ts), "cfg");
}

void emitEstado() {
  char ts[24]; tsAgora(ts, sizeof ts);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"estado\",\"estado\":\"%s\",\"ts\":\"%s\"}}",
    nomeEstado(countState), ts), "estado");
}

void setCountState(CountState s) {
  // Toda troca de estado passa por aqui, e por isso nenhuma escapa do evento.
  // Atribuir `countState` direto e o que faz a tela do operador ficar parada
  // num estado que a maquina ja deixou para tras.
  countState = s;
  emitEstado();
}

void emitContagem(const CountOutput &out) {
  char ts[24];  tsAgora(ts, sizeof ts);
  char total[16], liquido[16], exata[16];
  fmtF(total,   sizeof total,   out.total_weight_g, 2);
  fmtF(liquido, sizeof liquido, out.net_weight_g,   2);
  fmtF(exata,   sizeof exata,   out.exact_count,    3);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"contagem\",\"total_g\":%s,\"liquido_g\":%s,"
    "\"exata\":%s,\"contagem\":%d,\"status\":\"%s\",\"aceite\":%s,\"ts\":\"%s\"}}",
    total, liquido, exata, out.rounded_count, nomeStatus(out.result),
    out.within_tolerance ? "true" : "false", ts), "contagem");
}

void emitTaraBalanca(const char *alvo, float valor_g) {
  // Duas taras, e elas sao coisas diferentes: `canais` zera os offsets do
  // HX711 (calibracao, gravada na NVS) e `recipiente` mede o pote vazio. O
  // `tara_ok` do APSEN e uma TERCEIRA, e por isso tem nome proprio: ela nao
  // toca em hardware nenhum, so move o zero logico da mesa.
  char ts[24]; tsAgora(ts, sizeof ts);
  char v[16];  fmtF(v, sizeof v, valor_g, 2);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"tara_balanca\",\"alvo\":\"%s\",\"valor_g\":%s,"
    "\"ts\":\"%s\"}}", alvo, v, ts), "tara_balanca");
}

void emitErroBalanca(const char *msg, const char *cmd) {
  // `msg` e curta e sem acento de proposito: ela atravessa uma linha UTF-8
  // compartilhada com log, e o que o operador precisa ler e o que aconteceu.
  char ts[24]; tsAgora(ts, sizeof ts);
  char c[40];
  if (cmd) snprintf(c, sizeof c, "\"%s\"", cmd);
  else     snprintf(c, sizeof c, "null");
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"erro_balanca\",\"msg\":\"%s\",\"cmd\":%s,\"ts\":\"%s\"}}",
    msg, c, ts), "erro_balanca");
}

// ── Eventos do APSEN (o adapter encaminha ao central) ───────────────────────

void emitTelemetria() {
  char ts[24]; tsAgora(ts, sizeof ts);
  char temp[16], peso[16];
  fmtF(temp, sizeof temp, temperatureRead(), 2);
  fmtF(peso, sizeof peso, currentTotal, 2);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"telemetria\",\"componente\":\"hx711_balanca_mesa\","
    "\"temperatura_c\":%s,\"peso_atual_g\":%s,\"ts\":\"%s\"}}", temp, peso, ts),
    "telemetria");
}

static void emitTaraOk(const char *os_id, float peso_tara_g) {
  char ts[24]; tsAgora(ts, sizeof ts);
  char v[16];  fmtF(v, sizeof v, peso_tara_g, 2);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"tara_ok\",\"os_id\":\"%s\",\"peso_tara_g\":%s,"
    "\"ts\":\"%s\"}}", os_id, v, ts), "tara_ok");
}

static void emitErroSensor(const char *os_id, int slot_id, const char *descricao) {
  char ts[24]; tsAgora(ts, sizeof ts);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"erro_sensor\",\"os_id\":\"%s\",\"slot_id\":%d,"
    "\"descricao\":\"%s\",\"ts\":\"%s\"}}", os_id, slot_id, descricao, ts),
    "erro_sensor");
}

static void emitPesoOs(const char *os_id, int slot_id, int q_esperada, int q_real,
                       float unitario, float esperado, float medido,
                       float acumulado, float desvio, float desvio_pct,
                       bool dentro, bool injetada) {
  char ts[24]; tsAgora(ts, sizeof ts);
  char un[16], esp[16], med[16], acu[16], dev[16], pct[16], tol[16];
  fmtF(un,  sizeof un,  unitario,   2);
  fmtF(esp, sizeof esp, esperado,   2);
  fmtF(med, sizeof med, medido,     2);
  fmtF(acu, sizeof acu, acumulado,  2);
  fmtF(dev, sizeof dev, desvio,     2);
  fmtF(pct, sizeof pct, desvio_pct, 2);
  fmtF(tol, sizeof tol, TOLERANCIA_PCT, 2);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"%s\",\"os_id\":\"%s\",\"slot_id\":%d,"
    "\"quantidade_esperada\":%d,\"quantidade_real\":%d,\"peso_unitario_g\":%s,"
    "\"peso_esperado_g\":%s,\"peso_medido_g\":%s,\"peso_acumulado_g\":%s,"
    "\"desvio_g\":%s,\"desvio_pct\":%s,\"tolerancia_pct\":%s,"
    "\"dentro_tolerancia\":%s,\"falha_injetada\":%s,\"ts\":\"%s\"}}",
    dentro ? "peso_ok" : "peso_divergencia", os_id, slot_id,
    q_esperada, q_real, un, esp, med, acu, dev, pct, tol,
    dentro ? "true" : "false", injetada ? "true" : "false", ts),
    "peso_ok");
}

// ── Estabilidade do stream ──────────────────────────────────────────────────

static void atualizarEstabilidade() {
  // Comparacao simples com a leitura anterior, usando os MESMOS limiares da
  // config de contagem — mas em variaveis proprias. Reusar `transientBuffer` e
  // `transientStable` faria o stream de fundo alterar o resultado de uma
  // contagem em andamento, que e o oposto do que um stream deve fazer.
  if (isnan(streamUltimo)) {
    streamUltimo   = currentTotal;
    streamEstaveis = 1;
    streamEstavel  = false;
    return;
  }
  if (fabs(currentTotal - streamUltimo) <= countConfig.stability_threshold_g)
    streamEstaveis++;
  else
    streamEstaveis = 0;
  streamUltimo  = currentTotal;
  streamEstavel = (streamEstaveis >= countConfig.stabilization_reads);
}

// ── Entrada: o scanner de JSON ──────────────────────────────────────────────
//
// Sem ArduinoJson: sete comandos nao pagam a dependencia numa placa que ja
// esta apertada de RAM. O scanner acha a CHAVE e le o valor logo depois dela.
// A limitacao e conhecida e esta dentro do contrato: um valor de texto que
// contivesse `"cmd":` enganaria a busca, e nenhum campo deste contrato pode
// conter aspas (`os_id` e alfanumerico com hifen, `injetar_falha` e um
// identificador).

static const char *acharChave(const char *linha, const char *chave) {
  char alvo[40];
  snprintf(alvo, sizeof alvo, "\"%s\"", chave);
  const char *p = strstr(linha, alvo);
  if (!p) return NULL;
  p += strlen(alvo);
  while (*p == ' ') p++;
  if (*p != ':') return NULL;
  p++;
  while (*p == ' ') p++;
  return p;
}

static bool jsonTexto(const char *linha, const char *chave, char *dest, size_t n) {
  const char *p = acharChave(linha, chave);
  if (!p || *p != '"') return false;
  p++;
  size_t i = 0;
  while (*p && *p != '"' && i + 1 < n) {
    if (*p == '\\' && p[1]) p++;   // \" e \\ — o resto do contrato e ASCII
    dest[i++] = *p++;
  }
  dest[i] = '\0';
  return true;
}

static bool jsonBool(const char *linha, const char *chave, bool *out) {
  const char *p = acharChave(linha, chave);
  if (!p) return false;
  if (strncmp(p, "true", 4) == 0)  { *out = true;  return true; }
  if (strncmp(p, "false", 5) == 0) { *out = false; return true; }
  return false;
}

static bool jsonNumero(const char *linha, const char *chave, float *out) {
  const char *p = acharChave(linha, chave);
  if (!p || *p == 'n') return false;   // ausente ou `null`
  char *fim = NULL;
  float v = (float)strtod(p, &fim);
  if (fim == p) return false;
  *out = v;
  return true;
}

// ── ACK ─────────────────────────────────────────────────────────────────────

static void ackOk(long cmd_id, bool repetido) {
  if (repetido)
    emitir(snprintf(jbuf, sizeof jbuf,
      "{\"resp\":\"ok\",\"cmd_id\":%ld,\"repetido\":true}", cmd_id), "ack");
  else
    emitir(snprintf(jbuf, sizeof jbuf,
      "{\"resp\":\"ok\",\"cmd_id\":%ld}", cmd_id), "ack");
}

static void ackErro(long cmd_id, const char *msg) {
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"resp\":\"erro\",\"cmd_id\":%ld,\"msg\":\"%s\"}", cmd_id, msg), "ack");
}

// ── Os comandos que vem do PC ───────────────────────────────────────────────

static void bombearLeitura() {
  stepReading();
  if (totalReady) {
    atualizarEstabilidade();
    totalReady = false;
  }
}

static void cmdTara(const char *linha, long cmd_id) {
  char os_id[MAX_OS_ID_LEN];
  if (!jsonTexto(linha, "os_id", os_id, sizeof os_id)) os_id[0] = '\0';
  ackOk(cmd_id, false);

  // Zero LOGICO da mesa, nao tara de hardware: o offset do HX711 e calibracao
  // e so muda por `t`/`c<n>`, com o operador presente. Aqui o que se move e a
  // referencia da OS — exatamente o que o weight-simulator faz.
  taraOsG      = currentTotal;
  acumuladoOsG = 0.0f;
  Serial.printf("[APSEN] tara da OS %s: mesa em %.2f g\n", os_id, taraOsG);
  emitTaraOk(os_id, taraOsG);
}

static void cmdPesar(const char *linha, long cmd_id) {
  char  os_id[MAX_OS_ID_LEN];
  char  injetar[32];
  float slot = 0, q_esp = 0, q_real = 0, unitario = 0;

  if (!jsonTexto(linha, "os_id", os_id, sizeof os_id)) os_id[0] = '\0';
  if (!jsonNumero(linha, "slot_id", &slot) ||
      !jsonNumero(linha, "quantidade_esperada", &q_esp) ||
      !jsonNumero(linha, "peso_unitario_g", &unitario)) {
    ackErro(cmd_id, "campos ausentes em pesar");
    return;
  }
  // `quantidade_real` ausente cai na esperada, preservando o contrato antigo.
  if (!jsonNumero(linha, "quantidade_real", &q_real)) q_real = q_esp;
  if (!jsonTexto(linha, "injetar_falha", injetar, sizeof injetar)) injetar[0] = '\0';

  ackOk(cmd_id, false);

  // Espera a mesa parar. O ACK ja saiu, entao o relogio que corre agora e o
  // TIMEOUT_PESO do orquestrador (15 s) — nao o `ack_timeout_s` (2 s).
  unsigned long t0 = millis();
  while (!streamEstavel && millis() - t0 < PESAR_ESTAB_TIMEOUT_MS) {
    bombearLeitura();
    delay(5);
  }
  if (!streamEstavel)
    Serial.println(F("[APSEN] mesa nao estabilizou no prazo — medindo assim mesmo."));

  for (int i = 0; i < N; i++) {
    if (ch[i].active && ch[i].saturated) {
      emitErroSensor(os_id, (int)slot, "canal saturado");
      return;
    }
  }
  if (isnan(currentTotal) || isinf(currentTotal)) {
    emitErroSensor(os_id, (int)slot, "leitura invalida");
    return;
  }

  float liquido = currentTotal - taraOsG;
  if (liquido < 0.0f) liquido = 0.0f;
  float medido = liquido - acumuladoOsG;
  if (medido < 0.0f) medido = 0.0f;
  acumuladoOsG = liquido;

  float esperado = q_esp * unitario;

  // A injecao desloca a LEITURA, nunca a massa: a mesa segue com o peso certo
  // e o slot seguinte pesa normal. Tirar peso de verdade faria o one-shot
  // deixar de ser one-shot, e a demonstracao mostraria duas travas onde se
  // armou uma. Ramo isolado, ANTES de qualquer outra conta.
  bool injetada = (strcmp(injetar, "divergencia_peso") == 0);
  if (injetada && esperado > 0.0f) {
    medido -= esperado * (TOLERANCIA_PCT + 3.0f) / 100.0f;
    if (medido < 0.0f) medido = 0.0f;
  } else if (injetar[0] != '\0' && !injetada) {
    // Valor desconhecido e ignorado COM aviso, nunca em silencio: um typo do
    // console nao pode virar uma pesagem diferente da que se pediu.
    Serial.printf("AVISO: injetar_falha '%s' desconhecida — ignorada.\n", injetar);
    emitErroBalanca("injetar_falha desconhecida", "pesar");
  }

  float desvio     = medido - esperado;
  float desvio_pct = (esperado > 0.0f) ? fabs(desvio) / esperado * 100.0f : 0.0f;
  bool  dentro     = (desvio_pct <= TOLERANCIA_PCT);

  emitPesoOs(os_id, (int)slot, (int)q_esp, (int)q_real, unitario,
             esperado, medido, liquido, desvio, desvio_pct, dentro, injetada);
}

static void cmdBancada(const char *cmd, const char *linha, long cmd_id) {
  // Os comandos de bancada existem porque o adapter TOMA a porta: com o
  // transporte serial ligado, ninguem mais abre o Monitor Serial, e sem eles
  // configurar a balanca passaria a exigir parar o adapter. Sao os mesmos
  // efeitos das letras de sempre — `g<valor>`, `k`, `x`, `t`, `C` —, agora com
  // ACK e `cmd_id`, que e o que o transporte do projeto exige.
  if (strcmp(cmd, "peso_unitario") == 0) {
    float v = 0;
    if (!jsonNumero(linha, "valor_g", &v) || v <= 0.0f) {
      ackErro(cmd_id, "valor_g deve ser maior que zero");
      return;
    }
    ackOk(cmd_id, false);
    countConfig.unit_weight_g = v;
    saveCountConfig();                       // emite `cfg`
    Serial.printf("Peso unitario: %.4f g (salvo)\n", v);

  } else if (strcmp(cmd, "tara_recipiente") == 0) {
    ackOk(cmd_id, false);
    setCountState(CountState::AWAITING_TARA);
    Serial.println(F("Coloque o recipiente vazio..."));

  } else if (strcmp(cmd, "tara_canais") == 0) {
    ackOk(cmd_id, false);
    tareAll();                               // emite `tara_balanca`

  } else if (strcmp(cmd, "contar") == 0) {
    if (countConfig.unit_weight_g <= 0.0f) {
      ackErro(cmd_id, "configure o peso unitario primeiro");
      return;
    }
    ackOk(cmd_id, false);
    resetTransientBuffer();
    setCountState(CountState::TRANSIENT);
    Serial.println(F("Deposite os itens. Aguardando estabilizacao..."));

  } else if (strcmp(cmd, "config") == 0) {
    ackOk(cmd_id, false);
    emitCfg();

  } else if (strcmp(cmd, "stream") == 0) {
    bool on = true;                          // ausente = liga
    jsonBool(linha, "on", &on);
    ackOk(cmd_id, false);
    streamOn = on;
    Serial.printf("Stream JSON de peso: %s\n", streamOn ? "ON" : "OFF");
    emitCfg();

  } else {
    ackErro(cmd_id, "comando desconhecido");
    emitErroBalanca("comando desconhecido", cmd);
  }
}

// Adota a sessao que chegou e, se ela for OUTRA, zera o contador de idempotencia.
// Comparacao por DIFERENCA e nao por ordem: relogio do host que ande para tras
// (NTP, fuso, maquina sem RTC) continua sendo uma sessao nova, que e o que
// importa aqui.
static void adotarSessao(const char *linha) {
  float s = 0;
  if (!jsonNumero(linha, "sessao", &s) || s <= 0) return;
  const unsigned long nova = (unsigned long)s;
  if (sessaoAdapter != 0 && nova != sessaoAdapter) {
    ultimoCmdId = 0;
    Serial.println(F("[APSEN] sessao nova do adapter — contador de cmd_id zerado."));
  }
  sessaoAdapter = nova;
}

static void processarJson(const char *linha) {
  char valor[MAX_OS_ID_LEN];

  // O pong e a unica fonte de hora da placa.
  if (jsonTexto(linha, "resp", valor, sizeof valor)) {
    if (strcmp(valor, "pong") == 0) {
      // O `cmd_id` do adapter e monotonico DENTRO de uma sessao dele: um
      // restart do processo o faz nascer em 1 de novo. Sem isto, a placa veria
      // esse 1 como repeticao de um id que ja passou, responderia ACK sem
      // EXECUTAR, e o orquestrador esperaria para sempre por um evento que
      // ninguem ia produzir — OS abortada por timeout com a bancada intacta.
      //
      // E detectavel sem mensagem nova: o adapter so responde pong ao nosso
      // ping, entao silencio de varios pings e ele fora do ar. Abrir a porta
      // nao reinicia a placa (DTR/RTS saem desligados), entao quem tem que
      // notar a volta e este lado.
      if (ultimoPongMs != 0 && millis() - ultimoPongMs > SESSAO_SILENCIO_MS) {
        ultimoCmdId = 0;
        Serial.println(F("[APSEN] adapter voltou — contador de cmd_id zerado."));
      }
      ultimoPongMs = millis();
      // A deteccao EXPLICITA, que nao depende de quanto tempo ele ficou fora.
      adotarSessao(linha);

      float epoch = 0;
      if (jsonNumero(linha, "epoch", &epoch) && epoch > 0) {
        epochBase   = (unsigned long)epoch;
        epochMillis = millis();
      }
    }
    return;
  }

  char cmd[40];
  if (!jsonTexto(linha, "cmd", cmd, sizeof cmd)) return;  // linha de log com '{'

  // ANTES da checagem de idempotencia, e a ordem e o ponto: um comando da
  // sessao nova tem de ser executado, nao respondido como repetido. O pong
  // tambem a carrega, mas o primeiro comando depois do restart pode chegar
  // antes do primeiro pong.
  adotarSessao(linha);

  float id = 0;
  long cmd_id = jsonNumero(linha, "cmd_id", &id) ? (long)id : -1;

  // Idempotencia: o ultimo `cmd_id` executado fica guardado, e um id repetido
  // (ou anterior) responde ACK DE NOVO sem executar. O adapter nao reenvia
  // nada, mas o reenvio pode vir de qualquer origem — um restart do adapter no
  // meio do ciclo, um operador repetindo a acao — e esta e a parte do protocolo
  // que nao da para acrescentar depois sem trocar as duas pontas junto.
  if (cmd_id >= 0 && cmd_id <= ultimoCmdId) {
    ackOk(cmd_id, true);
    return;
  }
  if (cmd_id >= 0) ultimoCmdId = cmd_id;

  if (strcmp(cmd, "tara") == 0)       cmdTara(linha, cmd_id);
  else if (strcmp(cmd, "pesar") == 0) cmdPesar(linha, cmd_id);
  else                                cmdBancada(cmd, linha, cmd_id);
}

void despacharLinha(const String &linha) {
  // O '{' e o unico separador das duas vozes. Comando de uma letra continua
  // indo para `processCommand` sem passar por lugar nenhum novo — e por isso
  // o Monitor Serial da bancada segue funcionando exatamente como na 2.2.
  if (linha.indexOf('{') >= 0) processarJson(linha.c_str());
  else                         processCommand(linha);
}

// ================================================================
//   SETUP
// ================================================================
void setup() {
  Serial.begin(115200);
  delay(300);

  loadCalibration();
  loadCountConfig();

  for (int i = 0; i < N; i++) {
    pinMode(ch[i].sck_pin, OUTPUT);
    pinMode(ch[i].dout_pin, INPUT);
    digitalWrite(ch[i].sck_pin, LOW);
    for (int k = 0; k < MOVAVG_SAMPLES; k++) ch[i].movbuf[k] = 0.0f;
    ch[i].movidx = 0;
    ch[i].movfilled = false;
  }
  delay(500);

  Serial.println(F("\nPressione ENTER para TARA, 'l'+ENTER para carregar NVS."));
  unsigned long t0 = millis();
  while (!Serial.available() && (millis() - t0 < 5000)) delay(10);
  if (Serial.available()) {
    String resp = Serial.readStringUntil('\n'); resp.trim();
    if (resp == "l") { loadCalibration(); Serial.println(F("Offsets carregados.")); }
    else tareAll();
  } else {
    Serial.println(F("\n(Timeout) Tara automatica..."));
    tareAll();
  }

  Serial.println(F("\nBalanca + Contagem pronta. '?' para comandos.\n"));
  printMap();

  if (countConfig.unit_weight_g > 0)
    Serial.printf("Peso unitario: %.4f g. Use 'x' para contar.\n",
                  countConfig.unit_weight_g);
  else
    Serial.println(F("Configure o peso unitario com 'u' ou 'g<valor>'."));

  // O boot e a primeira linha de maquina da porta, e ele diz o que a placa
  // acabou de fazer com a tara. O adapter marca a tara como nao confiavel ao
  // ver um boot que nao pediu: abrir a porta reinicia o ESP32, e um reset com
  // peso na mesa taria o peso junto.
  emitBoot();
  emitCfg();
  emitPing();
}

// ================================================================
//   LOOP
// ================================================================
void loop() {
  static String cmdBuffer = "";
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      cmdBuffer.trim();
      if (cmdBuffer.length() > 0) despacharLinha(cmdBuffer);
      cmdBuffer = "";
    } else {
      cmdBuffer += c;
    }
  }

  stepReading();
  stepCounting();

  if (totalReady) {
    atualizarEstabilidade();
    if (autoPrint) {
      Serial.print(F("Peso total [g]: "));
      Serial.println(currentTotal, 2);
    }
    // O stream e independente do `autoPrint`: um e a voz do humano, o outro a
    // da maquina, e desligar um nunca pode calar o outro.
    if (streamOn && millis() - lastStreamMs >= STREAM_INTERVAL_MS) {
      lastStreamMs = millis();
      emitPeso();
    }
    totalReady = false;
  }

  if (millis() - lastPingMs >= PING_INTERVAL_MS) {
    lastPingMs = millis();
    emitPing();
  }
  if (millis() - lastTelemMs >= TELEMETRIA_INTERVAL_MS) {
    lastTelemMs = millis();
    emitTelemetria();
  }

  delay(10);
}