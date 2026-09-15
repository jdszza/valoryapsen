// ================================================================
//   BALANÇA 4 PONTOS — HX711 no ESP32
//   + MÓDULO DE CONTAGEM POR PESO COM TOLERÂNCIAS
// ================================================================
#include <Preferences.h>
#include <Arduino.h>

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
}

float readWeight_g_channel(int i) {
  if (!ch[i].active) return 0.0f;
  long raw = readRawMedian(i, MEDIAN_SAMPLES);
  if (ch[i].saturated) Serial.printf("AVISO: Canal %d SATURADO!\n", i);
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
      resetTransientBuffer();
      countState = CountState::AWAITING_DEPOSIT;
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
        countState = CountState::COUNTING;
      }
      break;
    }

    case CountState::COUNTING: {
      float w = readWeight_g_total();
      lastCount = countByWeight(w, countConfig);
      printCountResult(lastCount);
      countState = CountState::DONE;
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
    }

  // --- Contagem: tara do recipiente ---
  } else if (cmd == "k") {
    countState = CountState::AWAITING_TARA;
    Serial.println(F("Coloque o recipiente vazio..."));

  // --- Contagem: depositar e contar ---
  } else if (cmd == "x") {
    if (countConfig.unit_weight_g <= 0) {
      Serial.println(F("ERRO: Configure o peso unitario primeiro ('u' ou 'g<valor>')."));
    } else {
      resetTransientBuffer();
      countState = CountState::TRANSIENT;
      Serial.println(F("Deposite os itens. Aguardando estabilizacao..."));
    }

  // --- Contagem: ultimo resultado ---
  } else if (cmd == "n") {
    printCountResult(lastCount);

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
    } else Serial.println(F("Formato: o<valor>. Ex: o1.5"));

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
      } else Serial.println(F("Invalido. Ex: i1-100"));
    } else Serial.println(F("Formato: i<min>-<max>. Ex: i1-100"));

  // --- Contagem: leituras de estabilizacao ---
  } else if (cmd.length() > 1 && cmd[0] == 'r') {
    int v = cmd.substring(1).toInt();
    if (v > 0 && v <= 20) {
      countConfig.stabilization_reads = v;
      saveCountConfig();
      Serial.printf("Leituras estabilizacao: %d\n", v);
    } else Serial.println(F("Formato: r<1-20>. Ex: r3"));

  // --- Contagem: limiar de estabilidade ---
  } else if (cmd.length() > 1 && cmd[0] == 'h') {
    float v = cmd.substring(1).toFloat();
    if (v > 0.0f) {
      countConfig.stability_threshold_g = v;
      saveCountConfig();
      Serial.printf("Limiar estabilidade: %.2f g\n", v);
    } else Serial.println(F("Formato: h<valor>. Ex: h0.5"));

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
    Serial.println(F("  C = config atual\n"));
  } else {
    Serial.printf("Comando desconhecido: '%s' (use ?)\n", cmd.c_str());
  }
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
      if (cmdBuffer.length() > 0) processCommand(cmdBuffer);
      cmdBuffer = "";
    } else {
      cmdBuffer += c;
    }
  }

  stepReading();
  stepCounting();

  if (totalReady) {
    if (autoPrint) {
      Serial.print(F("Peso total [g]: "));
      Serial.println(currentTotal, 2);
    }
    totalReady = false;
  }

  delay(10);
}