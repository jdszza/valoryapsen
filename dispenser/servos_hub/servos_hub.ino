/*
 * =====================================================================
 *  CALIBRACAO DE SERVOS - PCB "Servos Hub - MainBoard" REV 1.0
 * =====================================================================
 *  ESP32 DevKit V1 + PCA9685 (8 servos) + 8x TCRT5000 (IR-1..8) + buzzer
 *
 *  - Calibra min/max de cada servo pela Serial e SALVA no ESP32 (NVS/
 *    Preferences, o "EEPROM" do ESP32). Mesmas chaves do sketch antigo
 *    ("servocal", min0..max7), entao o que ja foi salvo continua valendo.
 *  - Tabela CAL_BACKUP no codigo: usada quando o ESP32 nao tem valor salvo
 *    (placa nova, flash apagada, RESET). O comando EXPORT imprime a tabela
 *    pronta para colar aqui.
 *  - Log com horario de tudo: comandos, movimentos, gravacoes, sensores.
 *
 *  DUAS VOZES NA MESMA PORTA, E O SEPARADOR E O '{'
 *    voz do HUMANO  - tudo que a versao de calibracao ja imprimia, e todos os
 *      comandos do terminal (MANUT, SEL, MOVE, MIN/MAX, TEST, EXPORT...).
 *      Linha SEM '{'. Quem calibra na bancada segue calibrando como antes.
 *    voz da MAQUINA - uma linha JSON por mensagem, o contrato de
 *      `docs/PROTOCOLO_SERIAL.md` §2 e §3, lido pelo `dispenser-adapter`.
 *      Linha COM '{'.
 *
 *    O adapter extrai o JSON a partir do primeiro '{' e ignora o que vier
 *    antes, entao log e JSON podem sair GRUDADOS na mesma linha — que e o que
 *    um firmware realmente faz no boot. O humano ignora a linha com '{'.
 *
 *    Em uma linha, o que atravessa a porta:
 *      placa -> PC   {"cmd":"ping","sub":"dispenser"}   (quem pinga e a placa)
 *      PC -> placa   {"resp":"pong","epoch":<unix>}     (e assim o relogio acerta)
 *      PC -> placa   {"cmd":"<nome>","cmd_id":<n>,...}
 *      placa -> PC   {"resp":"ok","cmd_id":<n>}         ACK = ACEITEI, nao = terminei
 *      placa -> PC   {"evento":{"tipo":"...",...}}      o resultado, depois
 *
 *  TERMINAL DE MANUTENCAO
 *    FECHADO (operacao): envia o STATUS periodico (servos, sensores, PCA)
 *      e o unico comando aceito e MANUT.
 *    ABERTO (MANUT): so calibracao. O status periodico PARA; a Serial mostra
 *      apenas a tabela de calibracao e as respostas dos comandos.
 *    MANUTF fecha o terminal e o status periodico volta.
 *
 *  ARDUINO IDE
 *    Placa: "ESP32 Dev Module"
 *    Lib:   Adafruit PWM Servo Driver Library (+ dependencias)
 *    Serial Monitor: 115200 baud, final de linha = "Nova linha"
 *
 *  FLUXO DE CALIBRACAO
 *    MANUT        -> abre o terminal
 *    SEL 0        -> seleciona servo 0
 *    MOVE 20      -> vai para 20 graus   (ou "+", "-", "+5", "-5")
 *    MIN          -> grava a posicao atual como MIN
 *    MOVE 150 ... MAX -> grava como MAX
 *    TEST         -> varre min<->max
 *    (repete p/ os outros) -> SHOW -> EXPORT -> cola no CAL_BACKUP
 *    MANUTF       -> fecha o terminal
 * =====================================================================
 */

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>
#include <Preferences.h>
#include <stdarg.h>

// ---------------------------------------------------------------------
// MODO AO ABRIR O TERMINAL (MANUT). Troca depois com CAL / NORMAL.
// true  -> calibracao    false -> normal (respeita limites calibrados)
// ---------------------------------------------------------------------
#define MODO_CALIBRACAO_INICIAL true

// Intervalo do status automatico com o terminal FECHADO (ms). 0 = desligado.
// Durante o MANUT o status automatico nao e enviado.
// Muda em tempo real com "STATUS <segundos>" (dentro do MANUT).
#define STATUS_INTERVALO_MS 2000

// Comando sem Enter: se ficar este tempo sem chegar caractere, executa
// mesmo assim (Serial Monitor com "Nenhum final de linha").
#define SERIAL_FIM_LINHA_MS 250

// A voz de MAQUINA inteira — enquadramento, relogio, scanner de JSON, ACK e
// `cmd_id` — vem daqui, identica a da placa das telas. Ver o cabecalho do
// arquivo para por que sao duas copias e qual teste as compara.
#define APSEN_SUBSISTEMA "dispenser"
#include "apsen_serial.h"

// Ao ligar, leva cada servo para o seu MIN calibrado (um por vez, para
// nao dar pico de corrente). false = nao move nada no boot.
#define MOVER_PARA_MIN_NO_BOOT true

// ---------------------------------------------------------------------
// BACKUP DA CALIBRACAO  (cole aqui a saida do comando EXPORT)
// ---------------------------------------------------------------------
struct ServoCal {
  int angMin;
  int angMax;
};

// Oito servos e oito sensores, e o pareamento e por INDICE: o servo `i` e o
// IR `i + 1` sao o mesmo slot da bancada. Por isso os dois numeros nao podem
// divergir — o `static_assert` logo abaixo de NUM_IR recusa a compilacao se
// alguem mexer em um so. Este arquivo ja teve NUM_SERVOS=9 contra NUM_IR=8, e
// o preco foi leitura fora do array em tres lugares (a tabela, o TESTALL e o
// status), que no ESP32 nao estoura: devolve lixo, e o lixo aparecia como um
// "IR-9 DET" fantasma.
#define NUM_SERVOS 8

const ServoCal CAL_BACKUP[NUM_SERVOS] = {
  {   0, 180 },  // servo 0  (IR-1)
  {   0, 180 },  // servo 1  (IR-2)
  {   0, 180 },  // servo 2  (IR-3)
  {   0, 180 },  // servo 3  (IR-4)
  {   0, 180 },  // servo 4  (IR-5)
  {   0, 180 },  // servo 5  (IR-6)
  {   0, 180 },  // servo 6  (IR-7)
  {   0, 180 },  // servo 7  (IR-8)
};

// ---------------------------------------------------------------------
// HARDWARE DA PCB
// ---------------------------------------------------------------------
#define PCA_ADDR   0x40
// A CONFIRMAR NA BANCADA: o esquematico da REV 1.0 mostra D21/D22 como NC, e
// estas duas constantes dizem GPIO23/GPIO22. Quem decide e o multimetro.
// Enquanto a duvida durar, ela mora AQUI e em lugar nenhum mais: as mensagens
// do boot imprimem ESTES valores com %d, nunca um par escrito a mao. Mensagem
// de erro que aponta para o pino errado manda quem le procurar o problema no
// lugar errado — e e o log do boot que a pessoa vai ler primeiro.
#define PCA_SDA    23
#define PCA_SCL    22
#define SERVO_FREQ 50

#define SERVO_PULSE_MIN 102   // ticks @50Hz -> ~0.5 ms = 0 graus
#define SERVO_PULSE_MAX 512   // ticks @50Hz -> ~2.5 ms = 180 graus

#define BUZZER_PIN   15
#define BUZZER_ATIVO 1        // 1 = ativo (liga/desliga) | 0 = passivo (tom)
#define BUZZER_FREQ  2700

#define NUM_IR 8
const uint8_t IR_PINS[NUM_IR] = { 13, 12, 14, 27, 26, 25, 33, 32 }; // IR-1..IR-8
#define IR_ATIVO_LOW   1      // reflexao puxa o sinal para GND
#define IR_DEBOUNCE_MS 20

// Servo `i` e IR `i + 1` sao o MESMO slot. Dois lacos deste arquivo percorrem
// os dois conjuntos ao mesmo tempo (`mostrarTabela` e `varrer`), e e este
// static_assert que os torna legais: sem ele, mexer em um dos dois numeros
// volta a ler fora do array em silencio.
static_assert(NUM_SERVOS == NUM_IR,
              "um servo por sensor: NUM_SERVOS e NUM_IR tem de ser iguais");

#define TEST_PASSO_GRAUS 5
#define TEST_PASSO_MS    100

#define NVS_NAMESPACE "servocal"

// ---------------------------------------------------------------------
Adafruit_PWMServoDriver pwm(PCA_ADDR);
Preferences prefs;

enum Origem { ORIG_BACKUP, ORIG_NVS };

ServoCal cal[NUM_SERVOS];
Origem   calOrigem[NUM_SERVOS];
int      angAtual[NUM_SERVOS];      // -1 = desconhecido
int      servoSel     = 0;
bool     modoCal      = false;   // definido ao abrir o MANUT
bool     pcaOk        = false;

bool          irEstado[NUM_IR];
bool          irLeitura[NUM_IR];
unsigned long irMudouEm[NUM_IR];
uint32_t      irContagem[NUM_IR];
bool          irLog = false;   // deteccoes dos sensores durante o MANUT (IRLOG)

bool          manut            = false;   // terminal de manutencao aberto?
unsigned long statusIntervalo  = STATUS_INTERVALO_MS;
unsigned long ultimoStatus     = 0;

// =====================================================================
// SAIDA DE MAQUINA - o que e DESTA placa
// =====================================================================
// O nucleo do protocolo mora em `apsen_serial.h`. Aqui fica so o que os oito
// mecanismos acrescentam a ele.

// Periodo COMPLETO dos periodicos: cada slot emite `telemetria` + `status` uma
// vez a cada este intervalo. Os oito sao espacados dentro dele (ver
// `periodicosPoll`), nao emitidos juntos. 15 s e o mesmo do
// `dispenser_simulator` e o que §3 do protocolo descreve.
#define TELEMETRIA_INTERVALO_MS 15000

// Limites que saem da ORIGEM do dado, nunca do tamanho que ele tem hoje:
// `medicamentos.nome` e VARCHAR(150), `medicamentos.sku` VARCHAR(200) e
// `categoria` VARCHAR(100) no central. (`MAX_OS_ID_LEN` vem do header.)
#define MAX_MED_LEN   152
#define MAX_SKU_LEN   208
#define MAX_CAT_LEN   104

static unsigned long ultimoPeriodicoMs = 0;
static uint8_t       proximoPeriodico  = 0;   // proximo slot a reportar

// ---------------------------------------------------------------------
// OPERACAO (comandos do PC) - estado dos oito slots
// ---------------------------------------------------------------------
// O `dispenser_id` do contrato e 1..8; o canal do PCA9685 e o indice de todo
// array deste sketch sao 0..7. A conversao mora em `canalDoSlot`/`slotDoCanal`
// e em lugar NENHUM mais: um off-by-one aqui dispensa do dispenser VIZINHO, e
// o sintoma chega na camera como divergencia de SKU — indistinguivel de um
// medicamento realmente trocado.

enum EstadoSlot {
  SLOT_IDLE, SLOT_CARREGANDO, SLOT_PRONTO, SLOT_DISPENSANDO, SLOT_LIMPO, SLOT_ERRO
};

struct Slot {
  char       medicamento[MAX_MED_LEN];
  char       sku[MAX_SKU_LEN];
  char       categoria[MAX_CAT_LEN];
  char       os_id[MAX_OS_ID_LEN];
  int        quantidade;       // estoque do slot
  int        qtd_alvo;         // o que a OS pediu
  int        qtd_dispensada;   // o que o IR contou nesta dispensa
  EstadoSlot estado;
};

// A carga vive em RAM e NAO na NVS, de proposito: ela nao sobrevive a queda de
// energia de verdade — quem abre a gaveta e tira o comprimido e uma pessoa —,
// e gravar daria ao central um estoque que a bancada talvez nao tenha mais.
// Quem mede estoque nesta celula e a estacao de visao.
static Slot slots[NUM_SERVOS];

// Slot em operacao AGORA (0..7), ou -1. So a dispensa bloqueia: `carregar` e
// `limpar` sao registro em RAM, sem mecanismo, e terminam no mesmo instante.
static int canalEmOperacao = -1;

// Uma unidade por ciclo de servo, e o ciclo e min -> max -> min.
//
// A CONFIRMAR NA BANCADA: os dois numeros abaixo saem de MEDIDA, nao de gosto.
// DISPENSA_SERVO_MS e quanto o braco leva de um extremo ao outro com carga;
// DISPENSA_TIMEOUT_UNIDADE_MS e o prazo para o pulso do IR daquele slot chegar,
// contado do INICIO do ciclo. Comecam em 250 ms e 1500 ms, que e folga larga
// sobre um servo de hobby; o roteiro de PRIMEIRO_ENSAIO.md manda medir o par
// real e apertar. Curto demais e falha mecanica onde nao houve; longo demais
// gasta o TIMEOUT_DISPENSA do orquestrador, que corre do outro lado.
#define DISPENSA_SERVO_MS            250
#define DISPENSA_TIMEOUT_UNIDADE_MS 1500

// O UNICO valor de `injetar_falha` que esta placa reconhece. E o mesmo de
// `central-computer/injecao.TIPO_FALHA_MECANICA` e de
// `dispenser_simulator.INJECAO_FALHA_MECANICA`, e a igualdade importa porque
// uma string divergente nao quebra nada VISIVELMENTE: a placa ignora, o
// console diz "armado", e o gatilho simplesmente nunca dispara.
#define INJECAO_FALHA_MECANICA "falha_mecanica_dispenser"


// =====================================================================
// LOG
// =====================================================================
void logMsg(const char* tag, const char* fmt, ...) {
  char buf[192];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);

  unsigned long ms = millis();
  Serial.printf("[%02lu:%02lu:%02lu.%03lu] %-5s %s\n",
                ms / 3600000UL, (ms / 60000UL) % 60, (ms / 1000UL) % 60, ms % 1000,
                tag, buf);
}

// =====================================================================
// SENSORES IR (polling - suficiente para calibrar/testar)
// =====================================================================
bool lerIR(uint8_t i) {
  bool v = digitalRead(IR_PINS[i]);
  return IR_ATIVO_LOW ? !v : v;
}

void irInit() {
  for (uint8_t i = 0; i < NUM_IR; i++) {
    pinMode(IR_PINS[i], INPUT);   // pull-up de 10k ja esta na placa do sensor
    irEstado[i]   = lerIR(i);
    irLeitura[i]  = irEstado[i];
    irMudouEm[i]  = 0;
    irContagem[i] = 0;
  }
}

void irPoll() {
  unsigned long agora = millis();
  for (uint8_t i = 0; i < NUM_IR; i++) {
    bool r = lerIR(i);
    if (r != irLeitura[i]) {
      irLeitura[i] = r;
      irMudouEm[i] = agora;
    } else if (r != irEstado[i] && agora - irMudouEm[i] >= IR_DEBOUNCE_MS) {
      irEstado[i] = r;
      if (r) irContagem[i]++;
      // Fora do MANUT a deteccao faz parte do status; dentro, so com IRLOG
      if (manut ? irLog : statusIntervalo > 0) {
        logMsg("IR", "IR-%d (GPIO%d) %s  total=%lu", i + 1, IR_PINS[i],
             r ? "DETECTOU" : "livre", (unsigned long)irContagem[i]);
      }
    }
  }
}

// delay que continua lendo os sensores
void esperar(unsigned long ms) {
  unsigned long t0 = millis();
  while (millis() - t0 < ms) {
    irPoll();
    delay(2);
  }
}

// =====================================================================
// BUZZER
// =====================================================================
void buzzer(bool on) {
#if BUZZER_ATIVO
  digitalWrite(BUZZER_PIN, on ? HIGH : LOW);
#else
  if (on) tone(BUZZER_PIN, BUZZER_FREQ);
  else    noTone(BUZZER_PIN);
#endif
}

void bip(uint8_t vezes = 1, uint16_t dur = 80) {
  for (uint8_t i = 0; i < vezes; i++) {
    buzzer(true);
    esperar(dur);
    buzzer(false);
    if (i + 1 < vezes) esperar(80);
  }
}

// =====================================================================
// SERVOS
// =====================================================================
int anguloParaPulso(int ang) {
  ang = constrain(ang, 0, 180);
  return map(ang, 0, 180, SERVO_PULSE_MIN, SERVO_PULSE_MAX);
}

int pulsoParaMicros(int ticks) {
  return (int)((ticks * 20000L) / 4096L);
}

void moverRaw(uint8_t ch, int ang) {
  ang = constrain(ang, 0, 180);
  pwm.setPWM(ch, 0, anguloParaPulso(ang));
  angAtual[ch] = ang;
}

void moverCalibrado(uint8_t ch, int ang) {
  moverRaw(ch, constrain(ang, cal[ch].angMin, cal[ch].angMax));
}

// =====================================================================
// CALIBRACAO (NVS + BACKUP)
// =====================================================================
bool calValida(int mn, int mx) {
  return mn >= 0 && mx <= 180 && mn < mx;
}

void carregarCalibracao() {
  prefs.begin(NVS_NAMESPACE, true);
  for (int i = 0; i < NUM_SERVOS; i++) {
    char kMin[8], kMax[8];
    snprintf(kMin, sizeof(kMin), "min%d", i);
    snprintf(kMax, sizeof(kMax), "max%d", i);

    bool temNvs = prefs.isKey(kMin) && prefs.isKey(kMax);
    int mn = temNvs ? prefs.getInt(kMin, 0) : 0;
    int mx = temNvs ? prefs.getInt(kMax, 180) : 180;

    if (temNvs && calValida(mn, mx)) {
      cal[i].angMin = mn;
      cal[i].angMax = mx;
      calOrigem[i]  = ORIG_NVS;
      logMsg("LOAD", "servo %d: min=%d max=%d (ESP32)", i, mn, mx);
    } else {
      if (temNvs) {
        logMsg("AVISO", "servo %d: valor salvo invalido (min=%d max=%d)", i, mn, mx);
      }
      cal[i]       = CAL_BACKUP[i];
      calOrigem[i] = ORIG_BACKUP;
      logMsg("LOAD", "servo %d: min=%d max=%d (BACKUP do codigo)",
           i, cal[i].angMin, cal[i].angMax);
    }
  }
  prefs.end();
}

bool salvarCalibracao(uint8_t ch) {
  char kMin[9], kMax[9];
  snprintf(kMin, sizeof(kMin), "min%d", ch);
  snprintf(kMax, sizeof(kMax), "max%d", ch);

  prefs.begin(NVS_NAMESPACE, false);
  bool ok = prefs.putInt(kMin, cal[ch].angMin) > 0;
  ok      = (prefs.putInt(kMax, cal[ch].angMax) > 0) && ok;
  prefs.end();

  if (ok) {
    calOrigem[ch] = ORIG_NVS;
    logMsg("SAVE", "servo %d: min=%d max=%d gravado no ESP32",
         ch, cal[ch].angMin, cal[ch].angMax);
    bip(1, 60);
  } else {
    logMsg("ERRO", "servo %d: falha ao gravar no ESP32", ch);
    bip(3, 150);
  }
  return ok;
}

void apagarCalibracao(uint8_t ch) {
  char kMin[9], kMax[9];
  snprintf(kMin, sizeof(kMin), "min%d", ch);
  snprintf(kMax, sizeof(kMax), "max%d", ch);

  prefs.begin(NVS_NAMESPACE, false);
  prefs.remove(kMin);
  prefs.remove(kMax);
  prefs.end();

  cal[ch]       = CAL_BACKUP[ch];
  calOrigem[ch] = ORIG_BACKUP;
  logMsg("RESET", "servo %d: apagado do ESP32, voltou ao BACKUP (min=%d max=%d)",
       ch, cal[ch].angMin, cal[ch].angMax);
}

// =====================================================================
// SAIDAS DE TEXTO
// =====================================================================
void mostrarTabela() {
  Serial.println();
  Serial.println(F("+-------+-----+-----+-------------+--------+-------+------------+"));
  Serial.println(F("| servo | min | max | pulso (us)  | origem | atual | sensor     |"));
  Serial.println(F("+-------+-----+-----+-------------+--------+-------+------------+"));
  // Percorre os servos e mostra, na mesma linha, o sensor do mesmo slot. Ler
  // `irEstado[i]` aqui so e legal porque NUM_SERVOS == NUM_IR, e quem garante
  // isso e o static_assert la em cima — nao a sorte.
  for (int i = 0; i < NUM_SERVOS; i++) {
    char atual[9];
    if (angAtual[i] < 0) snprintf(atual, sizeof(atual), "  ?");
    else                 snprintf(atual, sizeof(atual), "%3d", angAtual[i]);

    Serial.printf("| %c %d   | %3d | %3d | %4d..%4d  | %-6s |  %s  | IR-%d %-5s |\n",
                  (i == servoSel && modoCal) ? '>' : ' ', i,
                  cal[i].angMin, cal[i].angMax,
                  pulsoParaMicros(anguloParaPulso(cal[i].angMin)),
                  pulsoParaMicros(anguloParaPulso(cal[i].angMax)),
                  calOrigem[i] == ORIG_NVS ? "ESP32" : "BACKUP",
                  atual,
                  i + 1, irEstado[i] ? "DET" : "livre");
  }
  Serial.println(F("+-------+-----+-----+-------------+--------+-------+------------+"));
  Serial.printf("Modo: %s | PCA9685: %s | deteccoes IR no MANUT: %s\n\n",
                modoCal ? "CALIBRACAO" : "NORMAL",
                pcaOk ? "OK" : "NAO ENCONTRADO",
                irLog ? "ligado" : "desligado");
}

void exportarBackup() {
  Serial.println();
  Serial.println(F("// ---- EXPORT: copie e cole no lugar do CAL_BACKUP ----"));
  Serial.println(F("const ServoCal CAL_BACKUP[NUM_SERVOS] = {"));
  for (int i = 0; i < NUM_SERVOS; i++) {
    Serial.printf("  { %3d, %3d },  // servo %d  (IR-%d)%s\n",
                  cal[i].angMin, cal[i].angMax, i, i + 1,
                  calOrigem[i] == ORIG_NVS ? "" : "  <- nao calibrado");
  }
  Serial.println(F("};"));
  Serial.println(F("// -------------------------------------------------------"));
  Serial.println();
  logMsg("INFO", "tabela exportada");
}

void mostrarSensores() {
  Serial.print(F("Sensores: "));
  for (uint8_t i = 0; i < NUM_IR; i++) {
    Serial.printf("IR-%d=%s(%lu) ", i + 1, irEstado[i] ? "DET" : ".",
                  (unsigned long)irContagem[i]);
  }
  Serial.println();
}

void mostrarAjuda() {
  Serial.println();
  Serial.println(F("=== CALIBRACAO SERVOS - Servos Hub REV 1.0 ==="));
  Serial.println(F("Terminal:"));
  Serial.println(F("  MANUT / MANUTF   abre / fecha o terminal de manutencao"));
  Serial.println(F("  STATUS           mostra o status dos servos/sensores agora"));
  Serial.println(F("  STATUS <s>       intervalo do status fora do MANUT (0 = desliga)"));
  Serial.println(F("Calibracao (modo CAL):"));
  Serial.println(F("  SEL <n>          seleciona servo 0-7 e vai para o meio da faixa"));
  Serial.println(F("  MOVE <ang>       move o selecionado (0-180, sem limite)"));
  Serial.println(F("  + / - / +5 / -5  ajuste fino a partir da posicao atual"));
  Serial.println(F("  MIN / MAX        grava a posicao ATUAL como min / max"));
  Serial.println(F("  MIN <a> / MAX <b> grava o valor digitado"));
  Serial.println(F("  MIN <a> MAX <b>  grava os dois de uma vez"));
  Serial.println(F("  SET <min> <max>  define e grava min/max"));
  Serial.println(F("  GOMIN / GOMAX    vai para o min / max salvo"));
  Serial.println(F("  TEST [ciclos]    varre min<->max do selecionado"));
  Serial.println(F("  TESTALL          varre todos, um por vez"));
  Serial.println(F("  RESET / RESETALL apaga do ESP32 e volta ao BACKUP do codigo"));
  Serial.println(F("Normal (modo NORMAL):"));
  Serial.println(F("  S <n> <ang>      move servo n respeitando a calibracao"));
  Serial.println(F("Sempre:"));
  Serial.println(F("  SHOW             tabela de calibracao"));
  Serial.println(F("  EXPORT           imprime o CAL_BACKUP para colar no codigo"));
  Serial.println(F("  SENS             estado e contagem dos sensores IR"));
  Serial.println(F("  IRLOG            mostra/oculta deteccoes dos sensores no MANUT"));
  Serial.println(F("  BIP              testa o buzzer"));
  Serial.println(F("  CAL / NORMAL     troca o modo"));
  Serial.println(F("  HELP             este menu"));
  Serial.printf("Modo atual: %s | servo selecionado: %d\n\n",
                modoCal ? "CALIBRACAO" : "NORMAL", servoSel);
}

// =====================================================================
// STATUS PERIODICO + TERMINAL DE MANUTENCAO
// =====================================================================
void enviarStatus() {
  char servos[80] = "";
  size_t n = 0;
  for (int i = 0; i < NUM_SERVOS && n < sizeof(servos); i++) {
    if (angAtual[i] < 0) n += snprintf(servos + n, sizeof(servos) - n, "S%d:? ", i);
    else                 n += snprintf(servos + n, sizeof(servos) - n, "S%d:%d ", i, angAtual[i]);
  }

  char ir[NUM_IR + 1];
  for (uint8_t i = 0; i < NUM_IR; i++) ir[i] = irEstado[i] ? 'X' : '.';
  ir[NUM_IR] = '\0';

  logMsg("STAT", "%s| IR %s | PCA %s", servos, ir, pcaOk ? "OK" : "FALHA");
}

void abrirManut() {
  if (manut) { logMsg("MANUT", "terminal ja esta aberto"); return; }
  manut        = true;
  modoCal      = MODO_CALIBRACAO_INICIAL;
  ultimoStatus = millis();
  logMsg("MANUT", "=== TERMINAL DE MANUTENCAO ABERTO (modo %s) ===",
       modoCal ? "CALIBRACAO" : "NORMAL");
  logMsg("MANUT", "status automatico pausado ate o MANUTF");
  mostrarTabela();
  mostrarAjuda();
  bip(2, 60);
}

void fecharManut() {
  // Servo que ficou fora da faixa calibrada durante a calibracao volta para dentro
  for (int i = 0; i < NUM_SERVOS; i++) {
    if (angAtual[i] < 0) continue;
    int dentro = constrain(angAtual[i], cal[i].angMin, cal[i].angMax);
    if (dentro != angAtual[i]) {
      logMsg("MANUT", "servo %d estava em %d (fora de %d..%d) -> %d",
           i, angAtual[i], cal[i].angMin, cal[i].angMax, dentro);
      moverRaw(i, dentro);
    }
  }
  logMsg("MANUT", "=== TERMINAL DE MANUTENCAO FECHADO (digite MANUT para abrir) ===");
  manut   = false;
  modoCal = false;
  if (statusIntervalo > 0) {
    logMsg("MANUT", "status automatico a cada %lu s", statusIntervalo / 1000UL);
  }
  ultimoStatus = millis();
  enviarStatus();
  bip(1, 150);
}

void statusPoll() {
  if (manut || statusIntervalo == 0) return;   // no MANUT: so calibracao
  unsigned long agora = millis();
  if (agora - ultimoStatus >= statusIntervalo) {
    ultimoStatus = agora;
    enviarStatus();
  }
}

// =====================================================================
// TESTES
// =====================================================================
// `ch` indexa os DOIS conjuntos: o servo que se move e o sensor que conta.
// Ver o static_assert de NUM_SERVOS == NUM_IR.
void varrer(uint8_t ch, int ciclos) {
  int mn = cal[ch].angMin, mx = cal[ch].angMax;
  logMsg("TEST", "servo %d: %d ciclo(s) entre %d e %d", ch, ciclos, mn, mx);

  uint32_t irAntes = irContagem[ch];
  for (int c = 0; c < ciclos; c++) {
    for (int a = mn; a <= mx; a += TEST_PASSO_GRAUS) { moverRaw(ch, a); esperar(TEST_PASSO_MS); }
    moverRaw(ch, mx); esperar(300);
    for (int a = mx; a >= mn; a -= TEST_PASSO_GRAUS) { moverRaw(ch, a); esperar(TEST_PASSO_MS); }
    moverRaw(ch, mn); esperar(300);
    if (Serial.available()) {             // qualquer tecla interrompe
      while (Serial.available()) Serial.read();
      logMsg("TEST", "interrompido pelo usuario");
      break;
    }
  }
  logMsg("TEST", "servo %d: fim (parou no min=%d) | IR-%d detectou %lu vez(es)",
       ch, mn, ch + 1, (unsigned long)(irContagem[ch] - irAntes));
}

// =====================================================================
// COMANDOS
// =====================================================================
bool exigeCal() {
  if (modoCal) return true;
  logMsg("ERRO", "comando so no modo CAL (digite CAL)");
  return false;
}

// "MIN60MAX130" -> "MIN 60 MAX 130" (separa letra de numero)
void normalizarLinha(const char* in, char* out, size_t tam) {
  size_t j = 0;
  for (size_t i = 0; in[i] != '\0' && j + 2 < tam; i++) {
    char c = in[i];
    if (i > 0 && j > 0 && out[j - 1] != ' ') {
      bool trocaLN = isalpha((unsigned char)in[i - 1]) && isdigit((unsigned char)c);
      bool trocaNL = isdigit((unsigned char)in[i - 1]) && isalpha((unsigned char)c);
      if (trocaLN || trocaNL) out[j++] = ' ';
    }
    out[j++] = c;
  }
  out[j] = '\0';
}

// Aceita:  MIN | MAX | MIN <ang> | MAX <ang> | MIN <a> MAX <b> | MAX <b> MIN <a>
// Sem numero = usa a posicao atual do servo. So grava se TUDO estiver valido.
void comandoMinMax(const char* linha) {
  char copia[48];
  strncpy(copia, linha, sizeof(copia) - 1);
  copia[sizeof(copia) - 1] = '\0';

  const int minAntes = cal[servoSel].angMin;
  const int maxAntes = cal[servoSel].angMax;
  int  novoMin = minAntes, novoMax = maxAntes;
  bool temMin = false, temMax = false;
  bool minDaPosicao = false, maxDaPosicao = false;

  char* tok = strtok(copia, " ");
  while (tok) {
    bool ehMin = !strcmp(tok, "MIN");
    bool ehMax = !strcmp(tok, "MAX");
    if (!ehMin && !ehMax) {
      logMsg("ERRO", "nao entendi '%s' - nada foi gravado", tok);
      logMsg("ERRO", "use: MIN | MAX | MIN <ang> | MAX <ang> | MIN <a> MAX <b>");
      bip(3, 150);
      return;
    }
    if ((ehMin && temMin) || (ehMax && temMax)) {
      logMsg("ERRO", "%s repetido - nada foi gravado", tok);
      bip(3, 150);
      return;
    }

    char* prox = strtok(NULL, " ");
    int  valor;
    bool daPosicao = false;
    if (prox && isdigit((unsigned char)prox[0])) {
      valor = atoi(prox);
      if (valor > 180) {
        logMsg("ERRO", "%s %d fora de 0..180 - nada foi gravado", tok, valor);
        bip(3, 150);
        return;
      }
      prox = strtok(NULL, " ");
    } else {
      if (angAtual[servoSel] < 0) {
        logMsg("ERRO", "%s sem valor usa a posicao atual, que e desconhecida (use MOVE antes)", tok);
        return;
      }
      valor     = angAtual[servoSel];
      daPosicao = true;
    }

    if (ehMin) { novoMin = valor; temMin = true; minDaPosicao = daPosicao; }
    else       { novoMax = valor; temMax = true; maxDaPosicao = daPosicao; }
    tok = prox;
  }

  if (!calValida(novoMin, novoMax)) {
    logMsg("ERRO", "servo %d: min=%d max=%d invalido (min precisa ser menor que max) - nada foi gravado",
           servoSel, novoMin, novoMax);
    bip(3, 150);
    return;
  }

  if (temMin) logMsg("CAL", "servo %d: MIN %d -> %d (%s)", servoSel, minAntes, novoMin,
                     minDaPosicao ? "posicao atual" : "digitado");
  if (temMax) logMsg("CAL", "servo %d: MAX %d -> %d (%s)", servoSel, maxAntes, novoMax,
                     maxDaPosicao ? "posicao atual" : "digitado");

  cal[servoSel].angMin = novoMin;
  cal[servoSel].angMax = novoMax;
  salvarCalibracao(servoSel);
  logMsg("CAL", "confira com GOMIN / GOMAX / TEST");
}

void processar(char* linhaBruta) {
  char linha[64];
  normalizarLinha(linhaBruta, linha, sizeof(linha));

  char cmd[16] = { 0 };
  int a = 0, b = 0;
  int lidos = sscanf(linha, "%15s %d %d", cmd, &a, &b);
  int nArgs = lidos - 1;

  // Terminal fechado: so aceita MANUT
  if (!manut) {
    if (!strcmp(cmd, "MANUT")) abrirManut();
    else Serial.println(F("Terminal fechado. Digite MANUT para abrir."));
    return;
  }

  logMsg("CMD", "> %s", linha);

  if (!strcmp(cmd, "MANUTF")) { fecharManut(); return; }
  if (!strcmp(cmd, "MANUT"))  { abrirManut();  return; }

  // Ajuste fino: "+", "-", "+5", "-10"
  if (linha[0] == '+' || linha[0] == '-') {
    if (!exigeCal()) return;
    int passo = (linha[1] != '\0') ? abs(atoi(linha + 1)) : 1;
    if (passo == 0) passo = 1;
    int base  = angAtual[servoSel] < 0 ? 90 : angAtual[servoSel];
    int novo  = constrain(base + (linha[0] == '+' ? passo : -passo), 0, 180);
    moverRaw(servoSel, novo);
    logMsg("MOVE", "servo %d -> %d graus (pulso %d us)", servoSel, novo,
         pulsoParaMicros(anguloParaPulso(novo)));
    return;
  }

  if (!strcmp(cmd, "SEL")) {
    if (!exigeCal()) return;
    if (nArgs < 1 || a < 0 || a >= NUM_SERVOS) { logMsg("ERRO", "uso: SEL <0-7>"); return; }
    servoSel = a;
    int meio = (cal[a].angMin + cal[a].angMax) / 2;
    moverRaw(a, meio);
    logMsg("SEL", "servo %d selecionado -> meio da faixa (%d graus) | min=%d max=%d [%s]",
         a, meio, cal[a].angMin, cal[a].angMax,
         calOrigem[a] == ORIG_NVS ? "ESP32" : "BACKUP");
  }
  else if (!strcmp(cmd, "MOVE") || !strcmp(cmd, "M")) {
    if (!exigeCal()) return;
    if (nArgs < 1) { logMsg("ERRO", "uso: MOVE <0-180>"); return; }
    int ang = constrain(a, 0, 180);
    moverRaw(servoSel, ang);
    logMsg("MOVE", "servo %d -> %d graus (pulso %d us)", servoSel, ang,
         pulsoParaMicros(anguloParaPulso(ang)));
  }
  else if (!strcmp(cmd, "MIN") || !strcmp(cmd, "MAX")) {
    if (!exigeCal()) return;
    comandoMinMax(linha);
  }
  else if (!strcmp(cmd, "SET")) {
    if (!exigeCal()) return;
    if (nArgs < 2) { logMsg("ERRO", "uso: SET <min> <max>"); return; }
    int mn = constrain(a, 0, 180), mx = constrain(b, 0, 180);
    if (!calValida(mn, mx)) {
      logMsg("ERRO", "min (%d) precisa ser menor que max (%d)", mn, mx);
      bip(3, 150);
      return;
    }
    cal[servoSel].angMin = mn;
    cal[servoSel].angMax = mx;
    salvarCalibracao(servoSel);
  }
  else if (!strcmp(cmd, "GOMIN") || !strcmp(cmd, "GOMAX")) {
    if (!exigeCal()) return;
    int ang = !strcmp(cmd, "GOMIN") ? cal[servoSel].angMin : cal[servoSel].angMax;
    moverRaw(servoSel, ang);
    logMsg("MOVE", "servo %d -> %s = %d graus", servoSel, cmd + 2, ang);
  }
  else if (!strcmp(cmd, "TEST")) {
    if (!exigeCal()) return;
    varrer(servoSel, (nArgs >= 1 && a > 0) ? a : 1);
  }
  else if (!strcmp(cmd, "TESTALL")) {
    if (!exigeCal()) return;
    for (int i = 0; i < NUM_SERVOS; i++) varrer(i, 1);
    logMsg("TEST", "TESTALL concluido");
    bip(2, 80);
  }
  else if (!strcmp(cmd, "RESET")) {
    if (!exigeCal()) return;
    apagarCalibracao(servoSel);
  }
  else if (!strcmp(cmd, "RESETALL")) {
    if (!exigeCal()) return;
    for (int i = 0; i < NUM_SERVOS; i++) apagarCalibracao(i);
    bip(2, 150);
  }
  else if (!strcmp(cmd, "S")) {
    if (nArgs < 2 || a < 0 || a >= NUM_SERVOS) { logMsg("ERRO", "uso: S <servo 0-7> <angulo>"); return; }
    moverCalibrado(a, b);
    logMsg("MOVE", "servo %d: pedido %d -> foi para %d (limites %d..%d)",
         a, b, angAtual[a], cal[a].angMin, cal[a].angMax);
  }
  else if (!strcmp(cmd, "SHOW"))   mostrarTabela();
  else if (!strcmp(cmd, "EXPORT")) exportarBackup();
  else if (!strcmp(cmd, "SENS"))   mostrarSensores();
  else if (!strcmp(cmd, "IRLOG")) {
    irLog = !irLog;
    logMsg("INFO", "deteccoes dos sensores no MANUT: %s", irLog ? "LIGADO" : "DESLIGADO");
  }
  else if (!strcmp(cmd, "BIP"))    bip(1, 200);
  else if (!strcmp(cmd, "STATUS")) {
    if (nArgs < 1) { enviarStatus(); return; }
    if (a < 0) a = 0;
    statusIntervalo = (unsigned long)a * 1000UL;
    ultimoStatus    = millis();
    if (a == 0) logMsg("INFO", "status automatico DESLIGADO (vale fora do MANUT)");
    else        logMsg("INFO", "status automatico a cada %d s (volta ao sair com MANUTF)", a);
  }
  else if (!strcmp(cmd, "CAL")) {
    modoCal = true;
    logMsg("MODO", "CALIBRACAO (servo selecionado: %d)", servoSel);
  }
  else if (!strcmp(cmd, "NORMAL")) {
    modoCal = false;
    logMsg("MODO", "NORMAL (movimentos limitados pela calibracao)");
  }
  else if (!strcmp(cmd, "HELP") || !strcmp(cmd, "?")) mostrarAjuda();
  else {
    logMsg("ERRO", "comando desconhecido: %s (digite HELP)", cmd);
  }
}

// =====================================================================
// SAIDA DE MAQUINA (protocolo serial APSEN)
// =====================================================================
//
// Uma mensagem por linha, sempre comecando em '{'. Os `Serial.print` e o
// `logMsg` humanos continuam todos onde estavam: o que sai daqui sao linhas
// EXTRAS, e e o '{' que separa as duas vozes na mesma porta.
//
// Nada aqui interpreta a mecanica: `moverCalibrado`, `irPoll` e a calibracao
// sao os mesmos de antes. Esta secao LE o resultado deles e o publica.

// -- Despacho dos comandos do PC --------------------------------------------

// -- Slots: conversao de indice, texto seguro e espera que nao para o mundo --

// A conversao 1..8 <-> 0..7 mora AQUI e em lugar nenhum mais. Ver o bloco de
// estado dos slots, la em cima.
static inline bool    slotValido(int slot_1a8)     { return slot_1a8 >= 1 && slot_1a8 <= NUM_SERVOS; }
static inline uint8_t canalDoSlot(int slot_1a8)    { return (uint8_t)(slot_1a8 - 1); }
static inline int     slotDoCanal(uint8_t canal_0a7) { return (int)canal_0a7 + 1; }

static const char* nomeEstadoSlot(EstadoSlot e) {
  // Vocabulario FECHADO, e e o mesmo do `dispenser_simulator`: o central soma
  // por igualdade de status, entao uma grafia nova aqui nao da erro — da um
  // slot que nao e contado em lugar nenhum.
  switch (e) {
    case SLOT_IDLE:        return "idle";
    case SLOT_CARREGANDO:  return "carregando";
    case SLOT_PRONTO:      return "pronto";
    case SLOT_DISPENSANDO: return "dispensando";
    case SLOT_LIMPO:       return "limpo";
    case SLOT_ERRO:        return "erro";
  }
  return "desconhecido";
}

static void esperarLendoSerial(unsigned long ms) {
  // O laco de dispensa e a unica parte deste firmware que demora segundos, e
  // durante ele duas coisas nao podem parar: a contagem do IR (e ela que conta
  // a unidade) e a leitura da serial. Sem a segunda, o pong do adapter — que
  // chega a cada ping nosso, de 3 em 3 s — enche o buffer de recepcao e a
  // linha seguinte chega partida.
  unsigned long t0 = millis();
  do {
    irPoll();
    serialPoll();
    delay(2);
  } while (millis() - t0 < ms);
}

// -- Eventos do contrato (§3) ------------------------------------------------
//
// Os nomes de campo sao os MESMOS do payload HTTP do `dispenser_simulator`, e
// isso nao e preguica: o adapter repassa o evento CRU ao central, sem traduzir
// nada. Um campo renomeado aqui nao daria erro em lugar nenhum — daria uma
// coluna vazia no banco do central.

static void emitCarregado(int slot_1a8, const Slot& sl, const char* os_id,
                          int quantidade_total, bool via_residual) {
  char ts[24]; tsAgora(ts, sizeof ts);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"carregado\",\"dispenser_id\":%d,\"os_id\":\"%s\","
    "\"medicamento\":\"%s\",\"sku\":\"%s\",\"categoria\":\"%s\","
    "\"quantidade_total\":%d,\"quantidade_residual\":%d,\"via_residual\":%s,"
    "\"ts\":\"%s\"}}",
    slot_1a8, os_id, sl.medicamento, sl.sku, sl.categoria,
    quantidade_total, quantidade_total, via_residual ? "true" : "false", ts),
    "carregado");
}

static void emitDispensado(int slot_1a8, const char* os_id, const char* medicamento,
                           int dispensada, int alvo, int residual, bool injetada) {
  char ts[24]; tsAgora(ts, sizeof ts);
  bool falha = dispensada < alvo;
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"dispensado\",\"dispenser_id\":%d,\"os_id\":\"%s\","
    "\"medicamento\":\"%s\",\"quantidade_dispensada\":%d,\"quantidade_alvo\":%d,"
    "\"falha_mecanica\":%s,\"motivo_falha\":%s,\"quantidade_residual\":%d,"
    "\"falha_injetada\":%s,\"ts\":\"%s\"}}",
    slot_1a8, os_id, medicamento, dispensada, alvo,
    falha ? "true" : "false",
    falha ? "\"falha_mecanica\"" : "null",
    residual, injetada ? "true" : "false", ts), "dispensado");
}

static void emitLimpezaOk(int slot_1a8, const char* medicamento_limpo,
                          const char* solicitado_por) {
  // `limpeza_ok` NAO carrega `os_id`, e isso e contrato, nao esquecimento:
  // limpeza e operacao de SLOT, e a chave de espera do orquestrador e
  // `limpeza:{dispenser_id}`, sem prefixo de OS.
  char ts[24]; tsAgora(ts, sizeof ts);
  char med[40];
  if (medicamento_limpo && medicamento_limpo[0] != '\0')
    snprintf(med, sizeof med, "\"%.*s\"", (int)sizeof(med) - 4, medicamento_limpo);
  else
    snprintf(med, sizeof med, "null");
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"limpeza_ok\",\"dispenser_id\":%d,"
    "\"medicamento_limpo\":%s,\"solicitado_por\":\"%s\",\"ts\":\"%s\"}}",
    slot_1a8, med, solicitado_por, ts), "limpeza_ok");
}

static void emitStatusSlot(uint8_t canal_0a7) {
  // Snapshot de UM slot, com os mesmos campos do `_snapshot_slot` do
  // simulador. Sai na TRANSICAO (aqui) e em laco periodico (ver statusApsenPoll):
  // transicao nunca e filtrada pelo adapter, e o periodico e — quando identico
  // ao anterior a menos do `ts`. Por isso este payload sai INTEIRO do estado,
  // sem nada de millis() nem de sorteio: dois periodicos seguidos sem mudanca
  // sao byte a byte iguais fora do carimbo, que e o que faz o filtro funcionar.
  //
  // Buffers estaticos, e nao de pilha: sao ~530 B somados, esta funcao nunca
  // cede o controle no meio (nao chama serialPoll), e a placa e de uma linha
  // de execucao so.
  static char qmed[MAX_MED_LEN + 4], qsku[MAX_SKU_LEN + 4];
  static char qcat[MAX_CAT_LEN + 4], qos[MAX_OS_ID_LEN + 4];

  const Slot& sl = slots[canal_0a7];
  char ts[24]; tsAgora(ts, sizeof ts);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"status\",\"dispenser_id\":%d,\"medicamento\":%s,"
    "\"sku\":%s,\"categoria\":%s,\"quantidade\":%d,\"status\":\"%s\","
    "\"os_id\":%s,\"qtd_alvo\":%d,\"qtd_dispensada\":%d,\"ts\":\"%s\"}}",
    slotDoCanal(canal_0a7),
    citarOuNull(qmed, sizeof qmed, sl.medicamento),
    citarOuNull(qsku, sizeof qsku, sl.sku),
    citarOuNull(qcat, sizeof qcat, sl.categoria),
    sl.quantidade, nomeEstadoSlot(sl.estado),
    citarOuNull(qos, sizeof qos, sl.os_id),
    sl.qtd_alvo, sl.qtd_dispensada, ts), "status");
}

static void emitErroSlot(int slot_1a8, const char* os_id, const char* codigo,
                         const char* descricao) {
  // O `erro` desbloqueia quem espera: o central o repassa para as chaves
  // `{os_id}:carregado:{slot}` e `{os_id}:dispensado:{slot}`. Ficar mudo faria
  // o orquestrador queimar o TIMEOUT inteiro para saber o que a placa ja sabe.
  char ts[24]; tsAgora(ts, sizeof ts);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"erro\",\"dispenser_id\":%d,\"os_id\":\"%s\","
    "\"codigo_erro\":\"%s\",\"descricao\":\"%s\",\"ts\":\"%s\"}}",
    slot_1a8, os_id, codigo, descricao, ts), "erro");
}

// -- A mecanica: uma unidade por ciclo, confirmada pelo IR --------------------

static bool soltarUmaUnidade(uint8_t canal_0a7) {
  const uint32_t antes = irContagem[canal_0a7];
  const unsigned long t0 = millis();

  // `moverCalibrado`, NUNCA `moverRaw`: fora do MANUT o movimento respeita a
  // calibracao, e e ela que impede o servo de bater no fim de curso no meio de
  // uma OS. O ciclo sempre TERMINA no min, inclusive quando o pulso nao vem.
  moverCalibrado(canal_0a7, cal[canal_0a7].angMax);
  esperarLendoSerial(DISPENSA_SERVO_MS);
  moverCalibrado(canal_0a7, cal[canal_0a7].angMin);
  esperarLendoSerial(DISPENSA_SERVO_MS);

  // O pulso pode ter chegado durante a ida ou durante a volta; o que conta e a
  // contagem daquele sensor ter mudado dentro do prazo, contado do INICIO do
  // ciclo. `irPoll` ja faz o debounce, entao um comprimido nao e contado duas
  // vezes por tremida do sensor.
  while (irContagem[canal_0a7] == antes) {
    if (millis() - t0 >= DISPENSA_TIMEOUT_UNIDADE_MS) return false;
    esperarLendoSerial(4);
  }
  return true;
}

// -- Os tres comandos do contrato --------------------------------------------

static void cmdCarregar(const char* linha, long cmd_id) {
  float fslot = 0, fqtd = 0;
  char os_id[MAX_OS_ID_LEN], med[MAX_MED_LEN], sku[MAX_SKU_LEN], cat[MAX_CAT_LEN];

  // Campo obrigatorio ausente e comando INUTILIZAVEL: nao ha slot a que
  // prender um evento, entao a recusa vai no ACK — que o adapter transforma em
  // 502 na hora. Valor fora de faixa e outra coisa: ali o comando esta bem
  // formado, o ACK e positivo, e a falha vira evento (que desbloqueia quem
  // espera). Os dois casos existem, e confundi-los deixa alguem esperando.
  if (!jsonNumero(linha, "dispenser_id", &fslot) ||
      !jsonNumero(linha, "quantidade", &fqtd) ||
      !jsonTexto(linha, "os_id", os_id, sizeof os_id) ||
      !jsonTexto(linha, "medicamento", med, sizeof med) ||
      !jsonTexto(linha, "sku", sku, sizeof sku) ||
      !jsonTexto(linha, "categoria", cat, sizeof cat)) {
    ackErro(cmd_id, "campos ausentes em carregar");
    return;
  }

  const int slot = (int)fslot;
  const int quantidade = (int)fqtd;
  ackOk(cmd_id, false);

  if (!slotValido(slot)) {
    emitErroSlot(slot, os_id, "slot_invalido", "dispenser_id fora de 1..NUM_SERVOS");
    return;
  }
  const uint8_t canal = canalDoSlot(slot);

  if (canal == canalEmOperacao) {
    emitErroSlot(slot, os_id, "slot_em_operacao",
                 "carga recusada: o slot esta dispensando");
    return;
  }

  Slot& sl = slots[canal];
  const int estoque_atual = sl.quantidade;

  // Slot vazio (ou zerado) assume o medicamento novo; slot com residual do
  // MESMO item mantem o que ja esta la. Quem decide trocar de medicamento e o
  // `atribuir_slots` do central, e ele manda limpar antes.
  if (sl.medicamento[0] == '\0' || estoque_atual == 0) {
    copiarCampo(sl.medicamento, sizeof sl.medicamento, med);
    copiarCampo(sl.sku,         sizeof sl.sku,         sku);
    copiarCampo(sl.categoria,   sizeof sl.categoria,   cat);
  }
  copiarCampo(sl.os_id, sizeof sl.os_id, os_id);
  sl.qtd_alvo       = quantidade;
  sl.qtd_dispensada = 0;

  if (estoque_atual >= quantidade) {
    // Residual suficiente: nao ha o que carregar. O central conta este caso
    // como carga VALIDA — e ele que devolve o slot ao pool sem descartar
    // estoque bom.
    sl.estado = SLOT_PRONTO;
    logMsg("APSEN", "D%d residual suficiente: %d >= %d ('%s')",
         slot, estoque_atual, quantidade, sl.medicamento);
    emitStatusSlot(canal);
    emitCarregado(slot, sl, os_id, estoque_atual, true);
    return;
  }

  // Nesta celula NAO ha mecanismo de carga: quem enche o cartucho e uma
  // pessoa, na bancada. O firmware REGISTRA a carga que o central declarou e
  // confirma na hora — nao ha nada a esperar, e fingir um tempo de carga so
  // atrasaria a OS. A conferencia de que as unidades estao mesmo la e da
  // estacao de visao e da balanca; e para isso que o Triple Check existe.
  sl.estado = SLOT_CARREGANDO;
  emitStatusSlot(canal);

  sl.quantidade = quantidade;
  sl.estado     = SLOT_PRONTO;
  logMsg("APSEN", "D%d carregado: %d un. de '%s' (residual anterior=%d)",
       slot, quantidade, sl.medicamento, estoque_atual);
  emitStatusSlot(canal);
  emitCarregado(slot, sl, os_id, quantidade, false);
}

static void cmdDispensar(const char* linha, long cmd_id) {
  float fslot = 0;
  char os_id[MAX_OS_ID_LEN];
  char injetar[48];

  if (!jsonNumero(linha, "dispenser_id", &fslot) ||
      !jsonTexto(linha, "os_id", os_id, sizeof os_id)) {
    ackErro(cmd_id, "campos ausentes em dispensar");
    return;
  }
  if (!jsonTexto(linha, "injetar_falha", injetar, sizeof injetar)) injetar[0] = '\0';

  const int slot = (int)fslot;

  // Uma dispensa por vez, e a recusa e no ACK. A placa tem UM laco: aceitar a
  // segunda faria dois servos se moverem de dentro do mesmo fluxo, e o pulso
  // de um slot seria contado no prazo do outro. O ACK negativo vira 502 no
  // adapter, que e a resposta certa a "agora nao".
  if (canalEmOperacao >= 0) {
    ackErro(cmd_id, "placa ocupada: ja esta dispensando");
    logMsg("APSEN", "dispensar D%d recusado: D%d em operacao",
         slot, slotDoCanal((uint8_t)canalEmOperacao));
    return;
  }

  ackOk(cmd_id, false);   // ACK = aceitei. O resultado vem no evento, depois.

  if (!slotValido(slot)) {
    emitErroSlot(slot, os_id, "slot_invalido", "dispenser_id fora de 1..NUM_SERVOS");
    return;
  }
  const uint8_t canal = canalDoSlot(slot);
  Slot& sl = slots[canal];

  if (!pcaOk) {
    emitErroSlot(slot, os_id, "pca_sem_resposta",
                 "PCA9685 nao responde: nenhum servo se move");
    return;
  }
  if (sl.quantidade <= 0 || sl.qtd_alvo <= 0) {
    emitErroSlot(slot, os_id, "slot_vazio",
                 "dispensar pedido para slot sem carga");
    return;
  }

  const int alvo = sl.qtd_alvo;

  // Injecao de falha: ramo ISOLADO, ANTES de qualquer outra decisao, e por
  // isso ela funciona com o modo apresentacao ligado — o modo desliga o acaso,
  // nao a capacidade de mostrar o Triple Check. UMA unidade a menos, e o
  // numero nao e de gosto: as ordens padrao vao ate 15 unidades por slot, e
  // 1/15 = 6,7% ja passa da tolerancia de 5% da balanca. Ou seja, a menor
  // falha possivel ja e detectavel em QUALQUER template.
  int  a_soltar = alvo;
  bool injetada = false;
  if (strcmp(injetar, INJECAO_FALHA_MECANICA) == 0) {
    if (alvo > 1) { a_soltar = alvo - 1; injetada = true; }
    logMsg("AVISO", "D%d FALHA MECANICA INJETADA (demonstracao): vai soltar %d de %d",
         slot, a_soltar, alvo);
  } else if (injetar[0] != '\0') {
    // Valor desconhecido e ignorado COM aviso, nunca em silencio: um typo do
    // console nao pode virar dispensa diferente da que se pediu.
    logMsg("AVISO", "D%d injetar_falha '%s' desconhecida - ignorada", slot, injetar);
  }

  canalEmOperacao   = (int)canal;
  sl.estado         = SLOT_DISPENSANDO;
  sl.qtd_dispensada = 0;
  emitStatusSlot(canal);
  logMsg("APSEN", "D%d dispensando %d x '%s' (os=%s)", slot, alvo, sl.medicamento, os_id);

  int  contadas = 0;
  bool interrompeu = false;
  for (int i = 1; i <= a_soltar; i++) {
    if (!soltarUmaUnidade(canal)) {
      // Pulso que nao vem no prazo INTERROMPE: nao tenta o mesmo comprimido de
      // novo (dose dobrada se ele tiver caido sem o sensor ver) e nao segue
      // para o proximo (se o mecanismo atolou, insistir agrava). Quem decide o
      // que fazer com o deficit e o Triple Check, com tres fontes.
      logMsg("ERRO", "D%d unidade %d de %d: IR-%d nao pulsou em %d ms - dispensa interrompida",
           slot, i, a_soltar, slot, DISPENSA_TIMEOUT_UNIDADE_MS);
      interrompeu = true;
      break;
    }
    contadas++;
    sl.qtd_dispensada = contadas;
  }

  // O servo ja terminou o ciclo no min; esta linha e a garantia explicita de
  // que ele esta la em TODA saida, inclusive na interrompida.
  moverCalibrado(canal, cal[canal].angMin);

  // O residual desconta o que o IR CONTOU, nao o alvo. E a unica diferenca de
  // valor entre este firmware e o `dispenser_simulator`, e ela e deliberada:
  // o simulador desconta o alvo porque nao tem sensor: aqui, descontar o alvo
  // depois de uma interrupcao na unidade 3 de 10 diria ao central que o slot
  // esta vazio com sete comprimidos dentro dele. Numero gravado no historico
  // tem de ter vindo de uma medicao.
  const int residual = (sl.quantidade > contadas) ? (sl.quantidade - contadas) : 0;
  sl.quantidade = residual;

  // O nome sai do slot ANTES de o slot ser esvaziado, e a ORDEM e o ponto: a
  // limpeza logo abaixo roda justamente quando residual == 0, que e o caso de
  // SUCESSO. Copiando depois, `emitDispensado` saia com medicamento vazio toda
  // vez que a dispensa dava CERTO — e `dispensas.medicamento` e VARCHAR(100)
  // NOT NULL no central: a dispensa completa era exatamente a que nao virava
  // linha no banco, sem erro em lugar nenhum alem de um warning.
  char med_evento[MAX_MED_LEN];
  copiarCampo(med_evento, sizeof med_evento, sl.medicamento);

  if (residual == 0) {
    sl.medicamento[0] = '\0';
    sl.sku[0]         = '\0';
    sl.categoria[0]   = '\0';
  }

  // Estado terminal limpo: a dispensa acabou e o slot nao pertence mais a OS.
  // Sem soltar o os_id aqui, `limpar` nunca mais aceitaria este slot.
  sl.estado  = interrompeu ? SLOT_ERRO : SLOT_IDLE;
  sl.os_id[0] = '\0';
  sl.qtd_alvo = 0;
  canalEmOperacao = -1;

  logMsg("APSEN", "D%d dispensado %d/%d | residual=%d%s",
       slot, contadas, alvo, residual, injetada ? " (injetada)" : "");
  emitStatusSlot(canal);
  emitDispensado(slot, os_id, med_evento, contadas, alvo, residual, injetada);
}

static void cmdLimpar(const char* linha, long cmd_id) {
  float fslot = 0;
  char por[64];

  if (!jsonNumero(linha, "dispenser_id", &fslot) ||
      !jsonTexto(linha, "solicitado_por", por, sizeof por)) {
    ackErro(cmd_id, "campos ausentes em limpar");
    return;
  }

  const int slot = (int)fslot;
  ackOk(cmd_id, false);

  if (!slotValido(slot)) {
    emitErroSlot(slot, "", "slot_invalido", "dispenser_id fora de 1..NUM_SERVOS");
    return;
  }
  const uint8_t canal = canalDoSlot(slot);
  Slot& sl = slots[canal];

  if (canal == canalEmOperacao || sl.estado == SLOT_DISPENSANDO ||
      sl.estado == SLOT_CARREGANDO) {
    // O 409 que o central ja sabe tratar: limpar um slot com peca se movendo
    // e o unico caso em que a limpeza e recusada. "pronto", "erro" e "limpo"
    // sao estados parados, e limpar slot com estoque encalhado e exatamente o
    // proposito do botao do app de manutencao.
    emitErroSlot(slot, sl.os_id, "limpeza_em_operacao",
                 "dispenser em operacao");
    return;
  }

  char anterior[MAX_MED_LEN];
  copiarCampo(anterior, sizeof anterior, sl.medicamento);

  // Nesta celula tambem nao ha mecanismo de descarte: limpar e o operador
  // retirando o residual. O firmware registra que o slot esta vazio, que e o
  // que o central precisa para devolve-lo ao pool.
  sl.medicamento[0] = '\0';
  sl.sku[0]         = '\0';
  sl.categoria[0]   = '\0';
  sl.os_id[0]       = '\0';
  sl.quantidade     = 0;
  sl.qtd_alvo       = 0;
  sl.qtd_dispensada = 0;
  sl.estado         = SLOT_LIMPO;

  logMsg("APSEN", "D%d limpo (anterior: %s) | por: %s",
       slot, anterior[0] ? anterior : "vazio", por);
  emitStatusSlot(canal);
  emitLimpezaOk(slot, anterior, por);
}

// -- Periodicos: `telemetria` e `status` -------------------------------------

static void emitTelemetria(uint8_t canal_0a7) {
  // ESTA PLACA NAO TEM SENSOR DE TEMPERATURA, e por isso ela nao emite uma.
  //
  // O `dispenser_simulator` emite temperatura porque e simulador: ele INVENTA
  // o numero. O firmware nao pode, porque o que sai daqui e gravado em
  // `leituras_sensores` e lido depois como se tivesse sido medido.
  //
  // A leitura interna do ESP32 (`temperatureRead()`) foi considerada e
  // recusada: ela e UMA temperatura, a do SoC, e sairia oito vezes com oito
  // rotulos diferentes (`dispenser_1`..`dispenser_8`) — oito leituras iguais
  // atribuidas a oito mecanismos que a placa nem toca. Pior que nao medir e
  // medir a coisa errada com o nome da certa: o app de manutencao pinta
  // temperatura de vermelho a 65 graus, e ele estaria pintando o chip.
  //
  // O que esta placa MEDE de verdade, por slot, e o sensor IR: quantas
  // unidades passaram por ele desde o boot. E uso acumulado do mecanismo — o
  // numero de que a manutencao preventiva precisa — e vem de uma medicao.
  // Os NOMES de campo sao os do contrato (§3); `valor_c` nasceu Celsius e
  // continua sendo a chave que o central le, e quem diz o que o numero e sao
  // `tipo_leitura` e `unidade`. Nenhuma tela se confunde: tanto
  // `necessidades.itens_componentes` quanto a aba de temperaturas do app de
  // manutencao so olham para `tipo == "temperatura"`.
  const int slot = slotDoCanal(canal_0a7);
  char ts[24]; tsAgora(ts, sizeof ts);
  char v[16];  fmtF(v, sizeof v, (float)irContagem[canal_0a7], 0);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"telemetria\",\"dispenser_id\":%d,"
    "\"componente\":\"dispenser_%d\",\"tipo_leitura\":\"pulsos_ir\","
    "\"valor_c\":%s,\"unidade\":\"un\",\"ts\":\"%s\"}}",
    slot, slot, v, ts), "telemetria");
}

static void periodicosPoll() {
  // UM slot por vez, espacado, e nao os dezesseis de uma vez.
  //
  // Telemetria periodica nao pode competir com o caminho critico (§2): oito
  // `telemetria` mais oito `status` somam ~3 kB, que a 115200 baud sao ~280 ms
  // de linha ocupada — bem em cima do ACK que o adapter esta esperando. Um
  // slot a cada TELEMETRIA_INTERVALO_MS/NUM_SERVOS da a mesma informacao, no
  // mesmo intervalo por slot, sem rajada.
  //
  // E nada periodico sai durante uma dispensa: ali o canal e do ciclo em
  // andamento. As transicoes daquele slot continuam saindo na hora, porque
  // transicao nunca e periodico.
  if (canalEmOperacao >= 0) return;

  const unsigned long passo = TELEMETRIA_INTERVALO_MS / NUM_SERVOS;
  if (millis() - ultimoPeriodicoMs < passo) return;
  ultimoPeriodicoMs = millis();

  const uint8_t canal = proximoPeriodico;
  proximoPeriodico = (uint8_t)((proximoPeriodico + 1) % NUM_SERVOS);

  // Os dois saem inteiros do estado: sem millis(), sem sorteio, sem contador
  // que ande sozinho. Duas emissoes seguidas sem nada mudar sao byte a byte
  // iguais fora do `ts` — que e exatamente o que o filtro de repeticao do
  // adapter compara (`serial_link`, tipos `telemetria`/`status`/`movendo`/
  // `retornando`, ignorando `ts`). Se um deles carregasse um uptime, o filtro
  // deixaria de filtrar e o central gravaria 5.760 linhas por slot por dia.
  emitTelemetria(canal);
  emitStatusSlot(canal);
}

// O contrato que `apsen_serial.h` pede do sketch: quais comandos existem.
void executarComando(const char* cmd, const char* linha, long cmd_id) {
  if      (strcmp(cmd, "carregar")  == 0) cmdCarregar(linha, cmd_id);
  else if (strcmp(cmd, "dispensar") == 0) cmdDispensar(linha, cmd_id);
  else if (strcmp(cmd, "limpar")    == 0) cmdLimpar(linha, cmd_id);
  else {
    // Comando fora do contrato recusa com ACK NEGATIVO, nunca em silencio: o
    // adapter o transforma em 502 e quem pediu sabe na hora, em vez de esperar
    // o `ack_timeout_s` inteiro para descobrir o mesmo.
    ackErro(cmd_id, "comando desconhecido");
    logMsg("APSEN", "comando desconhecido: '%s'", cmd);
  }
}

// A linha SEM '{' — o contrato que `apsen_serial.h` pede do sketch.
void linhaHumana(char* linha) {
  // Com uma dispensa em curso, a voz do humano espera. Nao e zelo: o laco de
  // dispensa le a serial para nao perder comando, e um TESTALL ou um SEL
  // digitados nesse instante moveriam servos de DENTRO do laco que ja esta
  // movendo um — dois movimentos intercalados, e o pulso de um slot contado no
  // prazo do outro. A voz de maquina continua passando, porque ela so responde
  // ACK e acerta o relogio; nenhum comando dela move nada enquanto ha slot em
  // operacao (ver cmdDispensar e cmdCarregar).
  if (canalEmOperacao >= 0) {
    Serial.printf("Ocupado: dispensando D%d. Aguarde o fim do ciclo.\n",
                  slotDoCanal((uint8_t)canalEmOperacao));
    return;
  }

  // Maiusculas SO aqui: `processar()` compara com "MANUT", "SEL", "MOVE"...
  // Fazer isto na leitura, como o sketch fazia antes da voz de maquina,
  // transformava {"cmd":"dispensar"} em {"CMD":"DISPENSAR"} — que nao casa com
  // chave nenhuma, e o comando do adapter morreria sem ACK.
  for (char* q = linha; *q; q++) *q = toupper((unsigned char)*q);
  processar(linha);
}

// =====================================================================
void setup() {
  apsenSerialInit();     // buffer de recepcao do tamanho do teto da linha
  Serial.begin(115200);
  delay(1000);   // tempo para o Serial Monitor conectar
  Serial.println();
  Serial.println(F("=== ESP32 INICIOU - Servos Hub (calibracao) ==="));

  pinMode(BUZZER_PIN, OUTPUT);
  buzzer(false);

  for (int i = 0; i < NUM_SERVOS; i++) angAtual[i] = -1;

  // Slots comecam vazios: a carga vive em RAM e nao sobrevive a queda de
  // energia — e nao deve, porque quem tira comprimido do cartucho e uma
  // pessoa. Slot que o central achava carregado sera recarregado pela OS
  // seguinte, e a etapa 1b manda limpar antes se o medicamento mudou.
  for (int i = 0; i < NUM_SERVOS; i++) {
    slots[i].medicamento[0] = '\0';
    slots[i].sku[0]         = '\0';
    slots[i].categoria[0]   = '\0';
    slots[i].os_id[0]       = '\0';
    slots[i].quantidade     = 0;
    slots[i].qtd_alvo       = 0;
    slots[i].qtd_dispensada = 0;
    slots[i].estado         = SLOT_IDLE;
  }

  logMsg("BOOT", "Servos Hub REV 1.0 - calibracao de servos");

  Wire.begin(PCA_SDA, PCA_SCL);
  Wire.beginTransmission(PCA_ADDR);
  pcaOk = (Wire.endTransmission() == 0);
  if (pcaOk) {
    logMsg("BOOT", "PCA9685 OK em 0x%02X (SDA=%d SCL=%d)", PCA_ADDR, PCA_SDA, PCA_SCL);
  } else {
    logMsg("ERRO", "PCA9685 NAO responde em 0x%02X (SDA=%d SCL=%d)", PCA_ADDR, PCA_SDA, PCA_SCL);
    logMsg("ERRO", "verifique a ligacao GPIO%d->SDA e GPIO%d->SCL na PCB e a alimentacao",
         PCA_SDA, PCA_SCL);
  }
  pwm.begin();
  pwm.setPWMFreq(SERVO_FREQ);
  delay(10);

  irInit();
  logMsg("BOOT", "sensores IR-1..8 nos GPIO 13,12,14,27,26,25,33,32");

  carregarCalibracao();

  if (MOVER_PARA_MIN_NO_BOOT) {
    for (int i = 0; i < NUM_SERVOS; i++) {
      // Servo sem calibracao propria carrega o min do CAL_BACKUP, que e 0 grau
      // — e 0 grau pode estar ALEM do fim de curso mecanico do dispenser. Este
      // e o unico movimento do boot capaz de forcar a mecanica, e ele cairia
      // justamente na placa nova ou recem-apagada: a que ninguem conferiu
      // ainda. Nao calibrado fica parado, e o log diz o que fazer.
      if (calOrigem[i] != ORIG_NVS) {
        logMsg("AVISO", "servo %d nao foi movido no boot: min=%d vem do BACKUP, "
             "nao da calibracao (abra MANUT e calibre com SEL %d)",
             i, cal[i].angMin, i);
        continue;
      }
      moverRaw(i, cal[i].angMin);
      logMsg("BOOT", "servo %d -> min (%d graus)", i, cal[i].angMin);
      esperar(150);   // escalonado: evita pico de corrente na fonte
    }
  } else {
    logMsg("BOOT", "servos nao foram movidos (MOVER_PARA_MIN_NO_BOOT=false)");
  }

  modoCal = false;
  logMsg("BOOT", "pronto. Terminal FECHADO - digite MANUT para calibrar");
  ultimoStatus = millis();
  enviarStatus();

  if (pcaOk) bip(1, 100);
  else       bip(4, 300);

  // A primeira linha de maquina da porta. Quem inicia o ping e SEMPRE a placa:
  // e por ele que o adapter identifica esta porta como a dos dispensers.
  emitPing();
}

void loop() {
  serialPoll();
  irPoll();
  statusPoll();      // STAT humano, so com o terminal fechado
  pingPoll();        // voz de maquina: quem pinga e a placa
  periodicosPoll();  // voz de maquina: `telemetria` + `status`, um slot por vez
  delay(2);
}
