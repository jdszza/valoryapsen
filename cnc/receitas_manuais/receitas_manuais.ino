/*
 * =============================================================================
 * NEXT 2K26 — Receitas Manuais (v1)
 * =============================================================================
 * Substitui temporariamente o FluidNC + sender pra tarefas de separação.
 * Ideia: você grava manualmente sequências de posições ("receitas") por slot
 * A a J (10 no total), correspondendo às 10 OSs pré-determinadas. Depois
 * executa cada receita mandando a letra do slot pelo serial.
 *
 * FLUXO TÍPICO DE USO
 * ───────────────────
 * 1. Liga o ESP32 → homing automático → cabeçote no zero máquina
 * 2. Movimenta manualmente até a primeira posição da OS 1:
 *      w / a / s / d          → move Y+/X-/Y-/X+ pelo STEP atual
 *      X+2  / Y-3.5           → jog explícito de N mm
 *      MP 5 -10               → move pra posição absoluta
 * 3. Entra em modo gravação do slot A:
 *      REC A                  → limpa slot A e começa a gravar
 * 4. Confirma que a posição atual é a primeira parada:
 *      MARK                   → adiciona posição atual como waypoint 1
 *                                (usa dwell padrão)
 *      MARK 800               → adiciona com dwell customizado de 800ms
 * 5. Move pra próxima posição, MARK de novo. Repete pra cada parada.
 * 6. Finaliza a receita:
 *      SAVE                   → grava slot A em NVS (persiste no flash)
 * 7. Depois de recorded, executa a receita:
 *      A                      → executa slot A
 *      B, C, ... J            → executa outros slots
 *      EXE                    → executa TODOS os slots ativos em ordem
 *
 * COMANDOS COMPLETOS
 * ──────────────────
 * MOVIMENTO
 *   H                → refaz homing
 *   ?                → status (posição, slot, envelope, modo)
 *   C                → vai ao centro do workspace
 *   MP X Y           → move pra posição absoluta X, Y (soft-limit)
 *   X+N / X-N        → jog X em N mm
 *   Y+N / Y-N        → jog Y em N mm
 *   w a s d          → jog rápido usando STEP atual (WASD like)
 *   STEP N           → define tamanho do jog WASD (mm). Default 1.
 *
 * GRAVAÇÃO DE RECEITA
 *   REC A ... REC J  → entra modo gravação (LIMPA slot antes)
 *   MARK             → adiciona posição atual como waypoint
 *   MARK N           → mesma coisa, mas dwell = N ms (default: DWELL global)
 *   POP              → remove último waypoint gravado (se enganou)
 *   SAVE             → finaliza gravação e persiste em NVS
 *   CANCEL           → aborta gravação sem salvar (mantém slot antigo)
 *   DWELL N          → default dwell time (ms) pros próximos MARKs
 *
 * EXECUÇÃO
 *   A ... J          → executa receita do slot A a J
 *   EXE              → executa A → B → C → ... → J em sequência
 *                       (pula slots vazios)
 *
 * INSPEÇÃO
 *   LIST             → lista status de todos slots (quantos waypoints)
 *   SHOW A           → mostra waypoints do slot A
 *   CLEAR A          → apaga slot A (com confirmação: mandar CLEAR A YES)
 *   CLEAR ALL YES    → apaga TODOS os slots
 *
 * SEGURANÇA
 *   Todo movimento é validado contra envelope [MIN, MAX] em X e Y.
 *   Movimento aborta imediatamente se limit switch dispara (hard-limit).
 *   Comandos de execução (A-J, EXE) refazem homing se ainda não foi feito.
 *
 * PINOUT / ENVELOPE
 *   Motores X (A CoreXY): STEP=23  DIR=22  EN=21
 *   Motores Y (B CoreXY): STEP=25  DIR=33  EN=26
 *   Limits: X=GPIO19, Y=GPIO32 (NC, LOW=triggered, alimentar 3.3V)
 *   Buzzer: GPIO13
 *   Envelope: X ∈ [-6, +8]   Y ∈ [-42, +1.5]  Centro (+4, -20.25)
 * =============================================================================
 */

#include <Arduino.h>
#include <Preferences.h>

// ═════════════════════════════════════════════════════════════════════════════
// AS DUAS VOZES DESTA PORTA
// ═════════════════════════════════════════════════════════════════════════════
// A mesa atende DOIS interlocutores na mesma serial, e o separador é o '{':
//
//   humano   — o terminal inteiro descrito no cabeçalho deste arquivo (H, MP,
//              REC, MARK, SAVE, EXE, WASD...). Continua igual, inclusive o boot
//              barulhento. Nenhuma linha dele contém '{'.
//   máquina  — o `cnc-adapter`, uma linha JSON por mensagem.
//
// Quem corta é `despacharLinha()`, no header, e o corte é feito na linha CRUA —
// ANTES de qualquer transformação. Essa ordem é a feature: um toUpperCase em
// cima de {"cmd":"mover","cmd_id":7} produz {"CMD":"MOVER","CMD_ID":7}, que não
// casa com chave nenhuma. O comando sumiria sem erro e o orquestrador esperaria
// para sempre por um ACK que ninguém ia mandar — OS abortada por timeout com a
// mesa intacta.
//
// Comando sem Enter: se ficar este tempo sem chegar caractere, a linha HUMANA é
// executada assim mesmo (Serial Monitor com "Nenhum final de linha", que é como
// a bancada costuma estar). Vale só para o humano — mensagem de máquina sempre
// termina em \n, e meia mensagem executada seria meio comando com ACK.
#define SERIAL_FIM_LINHA_MS 250

// A voz de MÁQUINA inteira — enquadramento, relógio pelo pong, scanner de JSON,
// ACK e `cmd_id` idempotente — vem daqui, idêntica à das duas placas do
// dispenser. Ver o cabeçalho de `apsen_serial.h` para por que são cópias e qual
// teste as compara.
#define APSEN_SUBSISTEMA "cnc"
#include "apsen_serial.h"

// ═════════════════════════════════════════════════════════════════════════════
// VOCABULÁRIO FECHADO DE `codigo_erro`  (docs/PROTOCOLO_SERIAL.md §4)
// ═════════════════════════════════════════════════════════════════════════════
// Todo evento `erro` desta placa carrega UM destes, e nenhum outro. A lista é
// fechada porque quem lê o outro lado é código: o central agrupa alarme por
// `codigo_erro` e a tela de necessidades manda o técnico à peça certa a partir
// dele. Um código novo inventado na hora não dá erro em lugar nenhum — vira uma
// linha que ninguém sabe ler.
//
// Os cinco primeiros são recusas de `mover`. `homing_falhou` é de `homing`, e
// está aqui embaixo com a separação escrita: uma lista "fechada" que cresce em
// silêncio é pior que uma lista aberta, porque ninguém desconfia dela.
#define ERRO_DISPENSER_INVALIDO "dispenser_invalido"  // fora de 1..NUM_DISPENSERS
#define ERRO_FORA_DO_ENVELOPE   "fora_do_envelope"    // waypoint fora dos limites
#define ERRO_SEM_HOMING         "sem_homing"          // origem nunca estabelecida
#define ERRO_LIMIT_DISPARADO    "limit_disparado"     // fim de curso, antes ou durante
#define ERRO_TRAVADO            "travado"             // trava do Triple Check (task 6)
#define ERRO_HOMING_FALHOU      "homing_falhou"       // `homing` estourou o timeout

// Teto do `trava_resumo` que chega do central, igual ao das telas TFT. O motivo
// formatado passa de 240 caracteres e NAO viaja: a tela da mesa responde uma
// pergunta so — por que ela fugiu —, e acoplar o formato de mensagem do central
// a largura de um terminal criaria um segundo ponto de truncamento para algo
// cosmetico. Ele ja chega cortado; o teto aqui e o buffer que o recebe.
#define TRAVA_RESUMO_MAX 48

// ═════════════════════════════════════════════════════════════════════════════
// CONSTANTES DE HARDWARE (mesmas do calibrador v9)
// ═════════════════════════════════════════════════════════════════════════════
struct MotorPins { uint8_t step, dir, en; };
const MotorPins MOTOR_X = {23, 22, 21};
const MotorPins MOTOR_Y = {25, 33, 26};

const uint8_t LIMIT_X_PIN = 19;
const uint8_t LIMIT_Y_PIN = 32;
const uint8_t BUZZER_PIN  = 13;

const bool LIMIT_ATIVO_ALTO = false;
const uint8_t  N_LEITURAS      = 5;
const uint16_t GAP_LEITURAS_US = 30;

const float STEPS_PER_MM = 80.0f;

// Direções calibradas
const bool HOMING_DIR_X_A_CW = false;
const bool HOMING_DIR_X_B_CW = false;
const bool HOMING_DIR_Y_A_CW = true;
const bool HOMING_DIR_Y_B_CW = false;

#define SINAL_MOTOR_A_POS_DIR (!HOMING_DIR_X_A_CW)
#define SINAL_MOTOR_B_POS_DIR (!HOMING_DIR_X_B_CW)

const bool INVERTER_EIXO_X = false;
const bool INVERTER_EIXO_Y = true;

// Envelope
// X: INVERTER_EIXO_X = false → valores permanecem como medidos originalmente.
// Y: INVERTER_EIXO_Y = true + fix do pos_y_mm() → coord Y agora reflete o
// sentido físico (positivo = pra frente). Os valores originais (−42 e +1.5)
// foram medidos com o tracker invertido, então precisam ser SWAPADOS + com
// sinal trocado pra bater com o novo sistema de coordenadas:
//   backward físico (era +1.5 no tracker antigo)  →  −1.5 no novo
//   forward  físico (era −42  no tracker antigo)  →  +42  no novo
const float LIMITE_MIN_X_MM = -6.00f;
const float LIMITE_MAX_X_MM = +8.00f;
const float LIMITE_MIN_Y_MM = -1.50f;   // extremo backward (perto do switch Y)
const float LIMITE_MAX_Y_MM = +42.00f;  // extremo forward (fim do trilho)

// Centro OPERACIONAL — não é necessariamente o centro geométrico do envelope.
// Serve como "casa" pro comando C. Ideal apontar pra área útil dos dispensers.
// Persiste em NVS; muda com `NOVOCENTRO x y` ou `NOVOCENTRO AQUI`.
//
// Valores iniciais (defaults): centro geométrico + offset X que já tínhamos.
// Depois do primeiro boot, os valores lidos da NVS têm precedência.
float CENTRO_X_MM = (LIMITE_MIN_X_MM + LIMITE_MAX_X_MM) / 2.0f + 3.0f;  // +4.0 default
float CENTRO_Y_MM = (LIMITE_MIN_Y_MM + LIMITE_MAX_Y_MM) / 2.0f;         // -20.25 default

// ═════════════════════════════════════════════════════════════════════════════
// DISPENSERS — posições calibradas manualmente com jog na máquina real
// ═════════════════════════════════════════════════════════════════════════════
// Layout físico (X, Y) em mm, no sistema de coord pós-homing:
//   Coluna esquerda (X = -1)   |   Coluna direita (X = +7)
//     D1 → (-1, 18)             |    D8 → (7, 18)
//     D2 → (-1, 17)             |    D7 → (7, 17)
//     D3 → (-1, 15)             |    D6 → (7, 15)
//     D4 → (-1, 14)             |    D5 → (7, 14)
struct DispenserPos { float x; float y; };
const DispenserPos D1 = { -1.0f, 18.0f };
const DispenserPos D2 = { -1.0f, 17.0f };
const DispenserPos D3 = { -1.0f, 15.0f };
const DispenserPos D4 = { -1.0f, 14.0f };
const DispenserPos D5 = { +7.0f, 14.0f };
const DispenserPos D6 = { +7.0f, 15.0f };
const DispenserPos D7 = { +7.0f, 17.0f };
const DispenserPos D8 = { +7.0f, 18.0f };

// Macro pra montar um waypoint a partir de (Dispenser, quantidade).
// Dwell = (quantidade + 1) × 1000 ms  →  1 segundo a mais que a quantidade
#define WP(d, qty)  { (d).x, (d).y, (uint16_t)(((qty) + 1) * 1000) }

// Quantos dispensers a célula tem. NÃO confundir com `NUM_SLOTS` (10), que é o
// número de SLOTS DE RECEITA na NVS — A a J, uma por ordem padrão. São duas
// contagens diferentes que por acaso vivem na mesma placa.
const uint8_t NUM_DISPENSERS = 8;

// ═════════════════════════════════════════════════════════════════════════════
// O ÚNICO PONTO DE TRADUÇÃO dispenser → coordenada
// ═════════════════════════════════════════════════════════════════════════════
// O comando `mover` traz o DISPENSER, não o par (x, y): a coordenada é medida
// nesta bancada e mora nesta placa. Esta função é a única conversão de número
// para posição em todo o firmware, e é assim de propósito — uma segunda
// tradução em qualquer outro caminho seria um segundo mapa da célula, e os dois
// concordariam só enquanto ninguém recalibrasse um dispenser.
//
// POR QUE NÃO PELA RECEITA. O waypoint gravado por REC/MARK é indexado pela
// ORDEM DE GRAVAÇÃO, e a rota de uma OS é decidida no central em tempo de
// execução (serpentina) e MUDA quando um slot sai da ordem por falta de
// estoque. "Vá ao ponto 3" seria outro dispenser no dia seguinte: a mesa iria
// ao lugar errado, a câmera leria o SKU de quem estava ali, e o sintoma
// chegaria como divergência num slot só — o quadro exato de uma falha mecânica.
// As constantes D1..D8 não têm esse problema: elas são a posição FÍSICA do
// dispenser, e é delas que o próprio WP() das receitas já sai.
//
// Devolve nullptr fora da faixa em vez de saturar na ponta: um `dispenser_alvo`
// de 9 não é "o 8" — é um comando que não se deve executar.
const DispenserPos* dispenser_pos(uint8_t n) {
  static const DispenserPos* const MAPA[NUM_DISPENSERS] = {
    &D1, &D2, &D3, &D4, &D5, &D6, &D7, &D8
  };
  if (n < 1 || n > NUM_DISPENSERS) return nullptr;
  return MAPA[n - 1];
}

// Velocidades — RAMPA TRAPEZOIDAL
// Todo movimento normal usa rampa: acelera até velocidade cruzeiro, mantém,
// depois desacelera até parar. Isso evita perda de passos e desgaste mecânico.
// Homing usa velocidade constante fixa (mais lenta e previsível).
float FEED_MM_MIN     = 750.0f;    // velocidade cruzeiro (mm/min) — ~12.5 mm/s
float ACCEL_MM_S2     = 500.0f;    // aceleração (mm/s²) — trapezóide da rampa
const unsigned int HOMING_STEP_DELAY_US  = 800;   // homing usa passo fixo
const unsigned int DELAY_US_MIN          = 40;    // limite físico do driver
const unsigned int DELAY_US_START_MAX    = 4000;  // meio-período no arranque (v muito baixa)

// Homing
const float        PULLOFF_MM         = 3.0f;
const unsigned long HOMING_TIMEOUT_MS = 60000;
const float ZERO_MAQUINA_X_MM = 0.0f;
const float ZERO_MAQUINA_Y_MM = 0.0f;

// Buzzer
const uint16_t BEEP_FREQ_HZ = 4000;

// ═════════════════════════════════════════════════════════════════════════════
// SLOTS DE RECEITAS
// ═════════════════════════════════════════════════════════════════════════════
const int NUM_SLOTS = 10;                // A a J
const int MAX_PONTOS_POR_SLOT = 20;      // até 20 waypoints por receita

struct Waypoint {
  float x;
  float y;
  uint16_t dwell_ms;   // tempo parado nesta posição pra pegar o medicamento
};

struct Slot {
  Waypoint pontos[MAX_PONTOS_POR_SLOT];
  uint8_t num_pontos;
};

Slot slots[NUM_SLOTS];

// Estado de gravação
int slot_gravando = -1;       // -1 = não está gravando; senão índice 0..9
uint16_t dwell_padrao_ms = 500;
float jog_step_mm = 1.0f;     // step do WASD
bool ja_fez_homing = false;

// A mesa está andando AGORA. Existe porque `sync_move` lê a serial durante o
// movimento: sem ele, um `mover` que chegasse no meio de outro entraria em
// `sync_move` de DENTRO de `sync_move`, e os dois trajetos se intercalariam
// pulso a pulso — a mesa iria para um terceiro lugar que ninguém pediu, e o
// tracker sairia coerente com ela. É a mesma regra que a placa dos mecanismos
// aplica com `canalEmOperacao`.
volatile bool movimento_em_curso = false;

// ── A trava do Triple Check ──────────────────────────────────────────────────
// Chega por `estado_celula`, e a mesa é avisada porque ela é a peça que está
// fisicamente sobre a bancada onde o supervisor vai mexer — e, sob o ciclo por
// relógio do central, é também a peça que continua andando sozinha se ninguém
// lhe disser para parar.
//
// `volatile` porque ela é escrita de dentro do `serialPoll()` que roda NO MEIO
// do laço de `sync_move`, e lida pela condição desse mesmo laço. Sem isso o
// compilador tem todo o direito de manter o valor num registrador e o
// movimento nunca veria a trava chegar.
volatile bool trava_ativa = false;
// Havia um `mover` em curso quando a trava chegou? É o que decide se sai um
// evento `erro` para aquela OS — quem estava esperando a chegada precisa saber
// que ela não vem, em vez de queimar o prazo inteiro.
volatile bool trava_interrompeu_mover = false;
char trava_os_id[MAX_OS_ID_LEN] = "";
int  trava_dispenser = 0;

Preferences prefs;

// ═════════════════════════════════════════════════════════════════════════════
// LOG
// ═════════════════════════════════════════════════════════════════════════════
// O header pede esta função e não a define. Ela é do caminho HUMANO: sai com
// carimbo de uptime e sem '{', para que o adapter a leia como log e não tente
// interpretá-la. É por onde o próprio header reporta linha acima do teto,
// reentrância e volta do adapter.
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

// ═════════════════════════════════════════════════════════════════════════════
// ESTADO DE POSIÇÃO
// ═════════════════════════════════════════════════════════════════════════════
long steps_A = 0;
long steps_B = 0;

// Cinemática CoreXY inversa — mesmo INVERTER_EIXO aplicado no mover_para
// precisa refletir aqui, senão o tracker fica com sinal invertido em relação
// ao sistema de coordenadas do usuário (jog delta esconde isso, mas
// gravação/execução de waypoints absolutos revela).
float pos_x_mm() {
  float x = (steps_A + steps_B) / (2.0f * STEPS_PER_MM);
  return INVERTER_EIXO_X ? -x : x;
}
float pos_y_mm() {
  float y = (steps_A - steps_B) / (2.0f * STEPS_PER_MM);
  return INVERTER_EIXO_Y ? -y : y;
}

char slot_letra(int idx) { return 'A' + idx; }
int  slot_indice(char c) {
  c = toupper(c);
  return (c >= 'A' && c <= 'J') ? (c - 'A') : -1;
}

// ═════════════════════════════════════════════════════════════════════════════
// LIMITS
// ═════════════════════════════════════════════════════════════════════════════
inline int limit_x_raw() { return digitalRead(LIMIT_X_PIN); }
inline int limit_y_raw() { return digitalRead(LIMIT_Y_PIN); }

inline bool limit_triggered_filtrado(uint8_t pin) {
  const int alvo = LIMIT_ATIVO_ALTO ? HIGH : LOW;
  for (uint8_t i = 0; i < N_LEITURAS; i++) {
    if (digitalRead(pin) != alvo) return false;
    delayMicroseconds(GAP_LEITURAS_US);
  }
  return true;
}
inline bool limit_x_triggered() { return limit_triggered_filtrado(LIMIT_X_PIN); }
inline bool limit_y_triggered() { return limit_triggered_filtrado(LIMIT_Y_PIN); }
inline bool qualquer_limit_disparado_rapido() {
  const int alvo = LIMIT_ATIVO_ALTO ? HIGH : LOW;
  return (digitalRead(LIMIT_X_PIN) == alvo) || (digitalRead(LIMIT_Y_PIN) == alvo);
}

bool posicao_dentro_envelope(float x, float y) {
  return x >= LIMITE_MIN_X_MM && x <= LIMITE_MAX_X_MM &&
         y >= LIMITE_MIN_Y_MM && y <= LIMITE_MAX_Y_MM;
}

// ═════════════════════════════════════════════════════════════════════════════
// MOTORES
// ═════════════════════════════════════════════════════════════════════════════
void motor_habilitar(const MotorPins& m, bool on) {
  digitalWrite(m.en, on ? LOW : HIGH);
}
void motor_direcao(const MotorPins& m, bool cw) {
  digitalWrite(m.dir, cw ? HIGH : LOW);
  delayMicroseconds(5);
}

// ═════════════════════════════════════════════════════════════════════════════
// BUZZER
// ═════════════════════════════════════════════════════════════════════════════
void beep_n(uint8_t n, uint16_t ms = 80) {
  for (uint8_t i = 0; i < n; i++) {
    tone(BUZZER_PIN, BEEP_FREQ_HZ, ms);
    delay(ms + 20);
    noTone(BUZZER_PIN);
    delay(80);
  }
}
void beep_longo(uint16_t ms) {
  tone(BUZZER_PIN, BEEP_FREQ_HZ, ms);
  delay(ms + 20);
  noTone(BUZZER_PIN);
}
void beep_alerta() {
  for (int i = 0; i < 3; i++) {
    tone(BUZZER_PIN, 3000, 200);
    delay(250);
    noTone(BUZZER_PIN);
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// SYNC MOVE COREXY — Bresenham + RAMPA TRAPEZOIDAL + hard-limit
// ═════════════════════════════════════════════════════════════════════════════
// Rampa trapezoidal: acelera de 0 até v_max (FEED_MM_MIN), mantém velocidade
// cruzeiro, depois desacelera até 0. Se o movimento for curto demais pra
// atingir v_max, vira triangular (arranque → pico → parada, sem cruise).
//
// Cálculo por pulso:
//   - Aceleração em pulsos/s²:   a = ACCEL_MM_S2 × STEPS_PER_MM
//   - Velocidade máx em pulsos/s: v_max = FEED_MM_MIN/60 × STEPS_PER_MM
//   - Passos pra atingir v_max:  n_ramp = v_max² / (2a)
//   - Meio-período do pulso i:   500000 / v(i)  µs
//   - v(i) durante aceleração:   sqrt(2·a·(i+1))
//   - v(i) durante cruise:       v_max
//   - v(i) durante desaceleração: sqrt(2·a·(passos_restantes))
// ═════════════════════════════════════════════════════════════════════════════

/** Calcula meio-período do pulso i (em µs) pra atingir a rampa alvo. */
inline unsigned int calcular_delay_us(long i, long total, long n_ramp, float accel_pps2, float v_max_pps) {
  float v_pps;
  if (i < n_ramp && i < total - n_ramp) {
    // Fase 1 — acelerando
    v_pps = sqrtf(2.0f * accel_pps2 * (float)(i + 1));
    if (v_pps > v_max_pps) v_pps = v_max_pps;
  } else if (i >= total - n_ramp) {
    // Fase 3 — desacelerando (usa passos restantes)
    long restam = total - i;
    v_pps = sqrtf(2.0f * accel_pps2 * (float)restam);
    if (v_pps > v_max_pps) v_pps = v_max_pps;
  } else {
    // Fase 2 — cruise
    v_pps = v_max_pps;
  }
  if (v_pps < 1.0f) v_pps = 1.0f;   // evita divisão por zero
  float delay = 500000.0f / v_pps;
  if (delay < (float)DELAY_US_MIN) return DELAY_US_MIN;
  if (delay > (float)DELAY_US_START_MAX) return DELAY_US_START_MAX;
  return (unsigned int)delay;
}

bool sync_move(long delta_A, long delta_B, bool respeitar_limits) {
  if (delta_A == 0 && delta_B == 0) return true;

  if (respeitar_limits && qualquer_limit_disparado_rapido()) {
    Serial.println("  ⚠ Limit já triggered — movimento abortado");
    beep_alerta();
    return false;
  }

  bool a_pos = delta_A > 0;
  bool b_pos = delta_B > 0;
  long abs_A = labs(delta_A);
  long abs_B = labs(delta_B);
  long max_p = max(abs_A, abs_B);

  bool a_dir = (a_pos == SINAL_MOTOR_A_POS_DIR);
  bool b_dir = (b_pos == SINAL_MOTOR_B_POS_DIR);
  motor_direcao(MOTOR_X, a_dir);
  motor_direcao(MOTOR_Y, b_dir);

  int8_t inc_A = a_pos ? +1 : -1;
  int8_t inc_B = b_pos ? +1 : -1;

  // Parâmetros de rampa
  float accel_pps2 = ACCEL_MM_S2 * STEPS_PER_MM;
  float v_max_pps  = (FEED_MM_MIN / 60.0f) * STEPS_PER_MM;
  long n_ramp = (long)((v_max_pps * v_max_pps) / (2.0f * accel_pps2));
  if (2 * n_ramp > max_p) n_ramp = max_p / 2;   // triangular se muito curto

  long err_a = 0, err_b = 0;
  movimento_em_curso = true;
  for (long i = 0; i < max_p; i++) {
    // A serial continua sendo LIDA durante o movimento, e não é conforto: o
    // curso mais longo da célula leva ~2 s, e nesse tempo cabem pongs, um
    // `estado_celula` e o que mais o adapter mandar. Sem ler, o buffer de
    // recepção enche e a linha chega PARTIDA — e meia linha é JSON inválido,
    // que some sem erro em lugar nenhum.
    //
    // AQUI, no topo do laço, e nunca entre o digitalWrite(HIGH) e o
    // digitalWrite(LOW): no meio de um pulso, o tempo gasto lendo alarga o
    // pulso que o driver está recebendo. Aqui ele só adia o próximo passo.
    //
    // O header é reentrante (dois jogos de buffer), então a linha lida daqui de
    // dentro não sobrescreve a que está sendo interpretada lá fora. O que ele
    // NÃO pode resolver é um comando que volte a mover a mesa — por isso
    // `movimento_em_curso`, conferido por `cmdMover`.
    serialPoll();

    // A trava é conferida na MESMA iteração barata em que o hard-limit já é
    // conferido — uma leitura de bool por passo. É o que faz o `estado_celula`
    // valer NO MEIO de um movimento, que é justamente quando ele importa: a
    // mesa está a caminho de um slot cuja dispensa o supervisor acabou de
    // reprovar.
    if (trava_ativa) {
      Serial.println("  ⚠ TRAVA do Triple Check — movimento interrompido.");
      movimento_em_curso = false;
      return false;
    }

    if (respeitar_limits && qualquer_limit_disparado_rapido()) {
      Serial.printf("  ⚠ EMERGÊNCIA! Limit disparou no passo %ld/%ld\n", i, max_p);
      beep_alerta();
      movimento_em_curso = false;
      return false;
    }
    err_a += abs_A;
    err_b += abs_B;
    bool pa = err_a >= max_p;
    bool pb = err_b >= max_p;
    if (pa) err_a -= max_p;
    if (pb) err_b -= max_p;

    unsigned int delay_us = calcular_delay_us(i, max_p, n_ramp, accel_pps2, v_max_pps);

    if (pa) digitalWrite(MOTOR_X.step, HIGH);
    if (pb) digitalWrite(MOTOR_Y.step, HIGH);
    delayMicroseconds(delay_us);
    if (pa) digitalWrite(MOTOR_X.step, LOW);
    if (pb) digitalWrite(MOTOR_Y.step, LOW);
    delayMicroseconds(delay_us);

    if (pa) steps_A += inc_A;
    if (pb) steps_B += inc_B;
  }
  movimento_em_curso = false;
  return true;
}

// ═════════════════════════════════════════════════════════════════════════════
// MOVIMENTOS DE ALTO NÍVEL
// ═════════════════════════════════════════════════════════════════════════════
bool mover_para(float x, float y) {
  if (!posicao_dentro_envelope(x, y)) {
    Serial.printf("[SOFT-LIMIT] (%.2f, %.2f) fora do envelope — REJEITADO\n", x, y);
    return false;
  }
  float dx = x - pos_x_mm();
  float dy = y - pos_y_mm();
  if (INVERTER_EIXO_X) dx = -dx;
  if (INVERTER_EIXO_Y) dy = -dy;
  long dA = (long)round((dx + dy) * STEPS_PER_MM);
  long dB = (long)round((dx - dy) * STEPS_PER_MM);
  return sync_move(dA, dB, true);   // usa rampa com FEED_MM_MIN atual
}
bool mover_x(float d) { return mover_para(pos_x_mm() + d, pos_y_mm()); }
bool mover_y(float d) { return mover_para(pos_x_mm(), pos_y_mm() + d); }

// ═════════════════════════════════════════════════════════════════════════════
// HOMING
// ═════════════════════════════════════════════════════════════════════════════
inline void pulso_dois_cru(unsigned int delay_us) {
  digitalWrite(MOTOR_X.step, HIGH); digitalWrite(MOTOR_Y.step, HIGH);
  delayMicroseconds(delay_us);
  digitalWrite(MOTOR_X.step, LOW); digitalWrite(MOTOR_Y.step, LOW);
  delayMicroseconds(delay_us);
}
// Devolve FALSE no timeout, e o retorno não é decoração.
//
// Ela era `void`: um eixo que nunca encontrava o fim de curso apenas voltava, e
// `homing_completo()` seguia zerando `steps_A/steps_B` e marcando
// `ja_fez_homing = true`. A placa passava a afirmar uma origem que nunca foi
// estabelecida, e TODO waypoint absoluto a partir dali saía deslocado pela
// distância que faltou — sem erro em lugar nenhum. O sintoma seria a mesa
// parando ao lado do dispenser, a câmera lendo o SKU do vizinho, e um técnico
// procurando falha mecânica num eixo que só não tinha referência.
bool seek_limit(bool a_cw, bool b_cw, bool (*fn)(), const char* eixo) {
  Serial.printf("[HOMING] Seek %s...\n", eixo);
  motor_direcao(MOTOR_X, a_cw);
  motor_direcao(MOTOR_Y, b_cw);
  unsigned long t0 = millis();
  while (!fn()) {
    if (millis() - t0 > HOMING_TIMEOUT_MS) {
      Serial.printf("[HOMING] TIMEOUT %s\n", eixo);
      return false;
    }
    pulso_dois_cru(HOMING_STEP_DELAY_US);
  }
  Serial.printf("[HOMING] %s TRIGGERED\n", eixo);
  return true;
}
void pulloff(bool a_cw, bool b_cw, float mm) {
  motor_direcao(MOTOR_X, !a_cw);
  motor_direcao(MOTOR_Y, !b_cw);
  long n = (long)(mm * STEPS_PER_MM);
  for (long i = 0; i < n; i++) pulso_dois_cru(HOMING_STEP_DELAY_US);
}
// FALSE quando um dos eixos não achou o fim de curso dentro de
// HOMING_TIMEOUT_MS. Nesse caso NÃO zera o tracker e deixa `ja_fez_homing` em
// false — é o `ja_fez_homing` que barra todo `mover` subsequente.
//
// Não zerar é a parte que importa. Zerar significa "o ponto onde estou agora é
// a origem da máquina", e depois de um timeout esse ponto é onde o eixo travou,
// não onde o switch está. Guardar um zero errado é pior que não ter zero:
// `posicao_dentro_envelope` passaria a aprovar contra um envelope deslocado, e
// o soft-limit deixaria de proteger justamente o eixo que acabou de falhar.
bool homing_completo() {
  Serial.println("\n╔════ HOMING ════╗");
  beep_n(2);
  delay(300);
  bool ok_x = seek_limit(HOMING_DIR_X_A_CW, HOMING_DIR_X_B_CW, limit_x_triggered, "X");
  pulloff(HOMING_DIR_X_A_CW, HOMING_DIR_X_B_CW, PULLOFF_MM);
  delay(300);
  bool ok_y = seek_limit(HOMING_DIR_Y_A_CW, HOMING_DIR_Y_B_CW, limit_y_triggered, "Y");
  pulloff(HOMING_DIR_Y_A_CW, HOMING_DIR_Y_B_CW, PULLOFF_MM);
  delay(300);

  if (!ok_x || !ok_y) {
    ja_fez_homing = false;
    Serial.printf("[HOMING] FALHOU (X=%s Y=%s) — origem NAO estabelecida, "
                  "movimento bloqueado ate um homing completo.\n",
                  ok_x ? "ok" : "timeout", ok_y ? "ok" : "timeout");
    beep_alerta();
    return false;
  }

  steps_A = (long)((ZERO_MAQUINA_X_MM + ZERO_MAQUINA_Y_MM) * STEPS_PER_MM);
  steps_B = (long)((ZERO_MAQUINA_X_MM - ZERO_MAQUINA_Y_MM) * STEPS_PER_MM);
  Serial.printf("[HOMING] Zero em (%.2f, %.2f)\n", pos_x_mm(), pos_y_mm());
  for (int i = 0; i < 3; i++) { beep_longo(200); delay(100); }
  ja_fez_homing = true;
  return true;
}

// ═════════════════════════════════════════════════════════════════════════════
// SLOTS: gravação, execução, listagem
// ═════════════════════════════════════════════════════════════════════════════
void slot_rec_iniciar(int idx) {
  slot_gravando = idx;
  slots[idx].num_pontos = 0;   // apaga waypoints anteriores (só na memória; SAVE persiste)
  Serial.printf("[REC] Slot %c aberto pra gravação. Move → MARK → repete → SAVE ou CANCEL.\n",
                slot_letra(idx));
  beep_n(1);
}

void slot_mark(uint16_t dwell) {
  if (slot_gravando < 0) {
    Serial.println("[MARK] Não estou gravando. Use REC <A-J> primeiro.");
    return;
  }
  Slot& s = slots[slot_gravando];
  if (s.num_pontos >= MAX_PONTOS_POR_SLOT) {
    Serial.printf("[MARK] Slot %c já tem %d waypoints (máximo). Faz SAVE ou POP.\n",
                  slot_letra(slot_gravando), MAX_PONTOS_POR_SLOT);
    return;
  }
  s.pontos[s.num_pontos] = { pos_x_mm(), pos_y_mm(), dwell };
  s.num_pontos++;
  Serial.printf("[MARK] Slot %c ponto %d: (%.2f, %.2f) dwell=%dms\n",
                slot_letra(slot_gravando), s.num_pontos,
                pos_x_mm(), pos_y_mm(), dwell);
  beep_n(1, 40);
}

void slot_pop() {
  if (slot_gravando < 0) {
    Serial.println("[POP] Não estou gravando.");
    return;
  }
  Slot& s = slots[slot_gravando];
  if (s.num_pontos == 0) {
    Serial.println("[POP] Nenhum waypoint pra remover.");
    return;
  }
  s.num_pontos--;
  Serial.printf("[POP] Removido último waypoint. Slot %c agora tem %d pontos.\n",
                slot_letra(slot_gravando), s.num_pontos);
}

void nvs_salvar_slot(int idx) {
  prefs.begin("receitas", false);
  char key[16];
  snprintf(key, sizeof(key), "n_%d", idx);
  prefs.putUChar(key, slots[idx].num_pontos);
  for (int i = 0; i < slots[idx].num_pontos; i++) {
    snprintf(key, sizeof(key), "x_%d_%d", idx, i);
    prefs.putFloat(key, slots[idx].pontos[i].x);
    snprintf(key, sizeof(key), "y_%d_%d", idx, i);
    prefs.putFloat(key, slots[idx].pontos[i].y);
    snprintf(key, sizeof(key), "t_%d_%d", idx, i);
    prefs.putUShort(key, slots[idx].pontos[i].dwell_ms);
  }
  prefs.end();
}

/** Persiste parâmetros globais (FEED, ACCEL, STEP, DWELL, CENTRO) na NVS. */
void salvar_params_nvs() {
  prefs.begin("receitas", false);
  prefs.putFloat("feed",   FEED_MM_MIN);
  prefs.putFloat("accel",  ACCEL_MM_S2);
  prefs.putFloat("step",   jog_step_mm);
  prefs.putUShort("dwell", dwell_padrao_ms);
  prefs.putFloat("cx",     CENTRO_X_MM);
  prefs.putFloat("cy",     CENTRO_Y_MM);
  prefs.end();
}

/** Carrega parâmetros globais da NVS (com defaults). */
void carregar_params_nvs() {
  prefs.begin("receitas", true);
  FEED_MM_MIN      = prefs.getFloat("feed",  FEED_MM_MIN);
  ACCEL_MM_S2      = prefs.getFloat("accel", ACCEL_MM_S2);
  jog_step_mm      = prefs.getFloat("step",  jog_step_mm);
  dwell_padrao_ms  = prefs.getUShort("dwell", dwell_padrao_ms);
  CENTRO_X_MM      = prefs.getFloat("cx",    CENTRO_X_MM);
  CENTRO_Y_MM      = prefs.getFloat("cy",    CENTRO_Y_MM);
  prefs.end();
}

void nvs_carregar_todos() {
  prefs.begin("receitas", true);
  int total_pontos = 0;
  for (int s = 0; s < NUM_SLOTS; s++) {
    char key[16];
    snprintf(key, sizeof(key), "n_%d", s);
    slots[s].num_pontos = prefs.getUChar(key, 0);
    if (slots[s].num_pontos > MAX_PONTOS_POR_SLOT) slots[s].num_pontos = 0;
    for (int i = 0; i < slots[s].num_pontos; i++) {
      snprintf(key, sizeof(key), "x_%d_%d", s, i);
      slots[s].pontos[i].x = prefs.getFloat(key, 0);
      snprintf(key, sizeof(key), "y_%d_%d", s, i);
      slots[s].pontos[i].y = prefs.getFloat(key, 0);
      snprintf(key, sizeof(key), "t_%d_%d", s, i);
      slots[s].pontos[i].dwell_ms = prefs.getUShort(key, dwell_padrao_ms);
    }
    total_pontos += slots[s].num_pontos;
  }
  prefs.end();
  int slots_ativos = 0;
  for (int s = 0; s < NUM_SLOTS; s++) {
    if (slots[s].num_pontos) slots_ativos++;
  }
  Serial.printf("[NVS] Slots carregados. Total de %d waypoints em %d slots ativos.\n",
                total_pontos, slots_ativos);
}

// ═════════════════════════════════════════════════════════════════════════════
// RECEITAS PRÉ-DEFINIDAS (10 OSs do SAP mapeadas pra slots A a J)
// ═════════════════════════════════════════════════════════════════════════════
// Chamado quando:
//   1. Boot detecta NVS zerado (primeira execução do sketch)
//   2. Usuário manda comando LOAD_DEFAULTS YES
//
// Se o usuário editar um slot depois via REC/MARK/SAVE, essa versão fica na
// NVS e prevalece nos próximos boots (não é sobrescrita automaticamente).
//
// Mapping OS → slot:
//   A = OS-URO-01     (2 itens)   Urologia, ronda noturna
//   B = OS-DOR-01     (3 itens)   Reumatologia/Dor, lote matinal
//   C = OS-VITAM-01   (3 itens)   Vitaminas, ambulatorial
//   D = OS-GASTRO-01  (4 itens)   Gastro, leito 118
//   E = OS-SNC-02     (4 itens)   Neuro, leito 207 / reposição
//   F = OS-CARDIO-01  (5 itens)   Cardiologia, leito 302
//   G = OS-SNC-01     (5 itens)   Neuro, leito 204
//   H = OS-LACTO-01   (6 itens)   Intolerância a lactose, kit flora
//   I = OS-INFECTO-01 (6 itens)   Infectologia, leito 415
//   J = OS-GERAL-01   (8 itens)   Carro de emergência, célula cheia
void carregar_receitas_padrao() {
  // ─── A ─ OS-URO-01 ─ Urologia ────────────────────────────────
  slots[0].num_pontos = 2;
  slots[0].pontos[0] = WP(D1, 5);    // RETEMIC 5MG (5)
  slots[0].pontos[1] = WP(D2, 3);    // UNOPROST 2MG (3)

  // ─── B ─ OS-DOR-01 ─ Reumatologia/Dor ────────────────────────
  slots[1].num_pontos = 3;
  slots[1].pontos[0] = WP(D1, 6);    // ARPADOL 400MG (6)
  slots[1].pontos[1] = WP(D2, 8);    // FLANCOX 500MG (8)
  slots[1].pontos[2] = WP(D3, 4);    // COLCHIS 0,5MG (4)

  // ─── C ─ OS-VITAM-01 ─ Vitaminas ─────────────────────────────
  slots[2].num_pontos = 3;
  slots[2].pontos[0] = WP(D1, 4);    // DESOL (4)
  slots[2].pontos[1] = WP(D2, 15);   // INPRUV DK 7000UI (15)
  slots[2].pontos[2] = WP(D3, 3);    // EXTIMA CHOCOLATE (3)

  // ─── D ─ OS-GASTRO-01 ─ Gastro ───────────────────────────────
  slots[3].num_pontos = 4;
  slots[3].pontos[0] = WP(D1, 5);    // LONIUM 40MG (5)
  slots[3].pontos[1] = WP(D2, 3);    // INILOK 40MG (3)
  slots[3].pontos[2] = WP(D3, 2);    // MOTILEX (2)
  slots[3].pontos[3] = WP(D4, 2);    // MAG B (2)

  // ─── E ─ OS-SNC-02 ─ Neuro leito 207 ─────────────────────────
  slots[4].num_pontos = 4;
  slots[4].pontos[0] = WP(D1, 5);    // ALOIS 10MG (5)
  slots[4].pontos[1] = WP(D2, 6);    // INSIT 50MG (6)
  slots[4].pontos[2] = WP(D3, 5);    // PAXORAL 7MG (5)
  slots[4].pontos[3] = WP(D4, 2);    // LENIX 50MG (2)

  // ─── F ─ OS-CARDIO-01 ─ Cardiologia ──────────────────────────
  slots[5].num_pontos = 5;
  slots[5].pontos[0] = WP(D1, 5);    // ZANIDIP 10MG (5)
  slots[5].pontos[1] = WP(D2, 7);    // XAFAC 2,5MG (7)
  slots[5].pontos[2] = WP(D3, 5);    // XAFAC 10MG (5)
  slots[5].pontos[3] = WP(D4, 7);    // XAFAC 20MG (7)
  slots[5].pontos[4] = WP(D5, 10);   // DOBEVEN 500MG (10)

  // ─── G ─ OS-SNC-01 ─ Neuro leito 204 ─────────────────────────
  slots[6].num_pontos = 5;
  slots[6].pontos[0] = WP(D1, 7);    // ALOIS 10MG (7)
  slots[6].pontos[1] = WP(D2, 5);    // DONAREN 50MG (5)
  slots[6].pontos[2] = WP(D3, 7);    // INSIT 50MG (7)
  slots[6].pontos[3] = WP(D4, 10);   // ATENTAH 18MG (10)
  slots[6].pontos[4] = WP(D5, 4);    // COBI-12 1000MCG (4)

  // ─── H ─ OS-LACTO-01 ─ Intolerância a lactose ────────────────
  slots[7].num_pontos = 6;
  slots[7].pontos[0] = WP(D1, 3);    // LACTOSIL 4500 COMP (3)
  slots[7].pontos[1] = WP(D2, 3);    // LACTOSIL 10000 COMP (3)
  slots[7].pontos[2] = WP(D3, 2);    // LACTOSIL FLORA (2)
  slots[7].pontos[3] = WP(D4, 2);    // PROBID (2)
  slots[7].pontos[4] = WP(D5, 2);    // PROBIANS (2)
  slots[7].pontos[5] = WP(D6, 2);    // FLORACOL (2)

  // ─── I ─ OS-INFECTO-01 ─ Infectologia leito 415 ──────────────
  slots[8].num_pontos = 6;
  slots[8].pontos[0] = WP(D1, 3);    // LEVOXIN 500MG (3)
  slots[8].pontos[1] = WP(D2, 3);    // LEVOXIN 750MG (3)
  slots[8].pontos[2] = WP(D3, 5);    // LECZA XR 500MG (5)
  slots[8].pontos[3] = WP(D4, 10);   // SIL-HP 4MG (10)
  slots[8].pontos[4] = WP(D5, 10);   // SIL-HP 8MG (10)
  slots[8].pontos[5] = WP(D6, 5);    // DUEPOLI ER 500MG (5)

  // ─── J ─ OS-GERAL-01 ─ Carro de emergência ───────────────────
  slots[9].num_pontos = 8;
  slots[9].pontos[0] = WP(D1, 7);    // ALOIS 10MG (7)
  slots[9].pontos[1] = WP(D2, 5);    // MECLIN 25MG (5)
  slots[9].pontos[2] = WP(D3, 5);    // RETEMIC 5MG (5)
  slots[9].pontos[3] = WP(D4, 5);    // LONIUM 40MG (5)
  slots[9].pontos[4] = WP(D5, 8);    // FLANCOX 500MG (8)
  slots[9].pontos[5] = WP(D6, 2);    // MIOSAN 5MG (2)
  slots[9].pontos[6] = WP(D7, 5);    // LURATT 20MG (5)
  slots[9].pontos[7] = WP(D8, 15);   // INPRUV DK 7000UI (15)
}

/** Persiste todos os 10 slots na NVS. Usado pelo LOAD_DEFAULTS. */
void nvs_salvar_todos() {
  for (int i = 0; i < NUM_SLOTS; i++) nvs_salvar_slot(i);
}

void slot_save() {
  if (slot_gravando < 0) {
    Serial.println("[SAVE] Não estou gravando.");
    return;
  }
  int idx = slot_gravando;
  nvs_salvar_slot(idx);
  Serial.printf("[SAVE] Slot %c persistido com %d waypoints.\n",
                slot_letra(idx), slots[idx].num_pontos);
  slot_gravando = -1;
  beep_n(2);
}

void slot_cancel() {
  if (slot_gravando < 0) {
    Serial.println("[CANCEL] Não estou gravando.");
    return;
  }
  int idx = slot_gravando;
  Serial.printf("[CANCEL] Gravação do slot %c abortada. Recarregando da NVS...\n",
                slot_letra(idx));
  // Recarrega só esse slot da NVS pra desfazer as mudanças em memória
  prefs.begin("receitas", true);
  char key[16];
  snprintf(key, sizeof(key), "n_%d", idx);
  slots[idx].num_pontos = prefs.getUChar(key, 0);
  for (int i = 0; i < slots[idx].num_pontos; i++) {
    snprintf(key, sizeof(key), "x_%d_%d", idx, i);
    slots[idx].pontos[i].x = prefs.getFloat(key, 0);
    snprintf(key, sizeof(key), "y_%d_%d", idx, i);
    slots[idx].pontos[i].y = prefs.getFloat(key, 0);
    snprintf(key, sizeof(key), "t_%d_%d", idx, i);
    slots[idx].pontos[i].dwell_ms = prefs.getUShort(key, dwell_padrao_ms);
  }
  prefs.end();
  slot_gravando = -1;
}

void slot_clear(int idx) {
  slots[idx].num_pontos = 0;
  nvs_salvar_slot(idx);
  Serial.printf("[CLEAR] Slot %c apagado.\n", slot_letra(idx));
}

bool slot_executar(int idx) {
  Slot& s = slots[idx];
  if (s.num_pontos == 0) {
    Serial.printf("[EXEC] Slot %c está VAZIO — nada a fazer.\n", slot_letra(idx));
    return false;
  }
  if (!ja_fez_homing) {
    Serial.println("[EXEC] Ainda não fez homing — executando homing primeiro...");
    // O retorno é conferido: sem origem, os waypoints da receita são absolutos
    // contra um zero que não existe, e a mesa percorreria a bancada inteira
    // deslocada — com os dwells certos, nos lugares errados.
    if (!homing_completo()) {
      Serial.println("[EXEC] Homing falhou — slot NAO executado.");
      return false;
    }
  }
  Serial.printf("\n▶ [EXEC] Slot %c: %d waypoints\n", slot_letra(idx), s.num_pontos);
  for (int i = 0; i < s.num_pontos; i++) {
    Waypoint& w = s.pontos[i];
    Serial.printf("  [%d/%d] → (%.2f, %.2f) dwell %dms\n",
                  i + 1, s.num_pontos, w.x, w.y, w.dwell_ms);
    if (!mover_para(w.x, w.y)) {
      Serial.printf("  ⚠ Movimento pro waypoint %d FALHOU — abortando slot\n", i + 1);
      beep_alerta();
      return false;
    }
    delay(w.dwell_ms);
  }
  Serial.printf("✓ [EXEC] Slot %c completo.\n\n", slot_letra(idx));
  beep_n(2);
  return true;
}

void slot_executar_todos() {
  Serial.println("\n▶▶ [EXEC ALL] Executando todos os slots ativos em ordem...\n");
  int executados = 0;
  for (int i = 0; i < NUM_SLOTS; i++) {
    if (slots[i].num_pontos > 0) {
      slot_executar(i);
      executados++;
      delay(500);
    }
  }
  Serial.printf("✓✓ [EXEC ALL] Fim. %d slots executados.\n\n", executados);
  beep_longo(500);
}

void slot_list() {
  Serial.println("\n─── SLOTS ───");
  for (int i = 0; i < NUM_SLOTS; i++) {
    char marker = (i == slot_gravando) ? '*' : ' ';
    Serial.printf(" %c %c : %2d waypoint(s)%s\n",
                  marker, slot_letra(i), slots[i].num_pontos,
                  slots[i].num_pontos == 0 ? " (vazio)" : "");
  }
  if (slot_gravando >= 0) {
    Serial.printf("[*] = slot em gravação (%c). Use SAVE ou CANCEL.\n",
                  slot_letra(slot_gravando));
  }
  Serial.println("─────────────\n");
}

void slot_show(int idx) {
  Serial.printf("\n─── SLOT %c ───\n", slot_letra(idx));
  if (slots[idx].num_pontos == 0) {
    Serial.println("(vazio)\n");
    return;
  }
  for (int i = 0; i < slots[idx].num_pontos; i++) {
    Waypoint& w = slots[idx].pontos[i];
    Serial.printf("  %2d: X=%7.2f  Y=%7.2f  dwell=%dms\n",
                  i + 1, w.x, w.y, w.dwell_ms);
  }
  Serial.println("─────────────\n");
}

// ═════════════════════════════════════════════════════════════════════════════
// SERIAL PARSER
// ═════════════════════════════════════════════════════════════════════════════
void imprimir_ajuda() {
  Serial.println("\n─── COMANDOS ───────────────────────────────────────────────");
  Serial.println("MOVIMENTO MANUAL (jog = mover pouco a pouco pra posicionar)");
  Serial.println("  H                  → refaz o homing");
  Serial.println("  ?                  → status atual (posição, params, envelope)");
  Serial.println("  C                  → vai ao centro operacional (configurável)");
  Serial.println("  NOVOCENTRO x y     → define centro operacional em coordenadas");
  Serial.println("  NOVOCENTRO AQUI    → adota a posição atual do cabeçote");
  Serial.println("  NOVOCENTRO PADRAO  → reset ao centro geométrico do envelope");
  Serial.println("  w / a / s / d      → 1 jog em Y+/X-/Y-/X+ pelo STEP atual");
  Serial.println("  wwww / dds / etc.  → sequência: N letras = N jogs sucessivos");
  Serial.println("  X+2 / X-2          → jog X 2 mm (qualquer decimal vale)");
  Serial.println("  Y+3 / Y-3          → jog Y 3 mm");
  Serial.println("  MP x y             → move pra posição absoluta X, Y");
  Serial.println();
  Serial.println("PARÂMETROS (todos persistem em NVS)");
  Serial.println("  FEED N             → velocidade cruzeiro (mm/min). Ex: FEED 750");
  Serial.println("  ACCEL N            → aceleração (mm/s²). Ex: ACCEL 500");
  Serial.println("  STEP N             → distância do jog WASD (mm). Ex: STEP 0.5");
  Serial.println("  DWELL N            → tempo parado nos waypoints (ms). Ex: DWELL 800");
  Serial.println();
  Serial.println("GRAVAÇÃO DE RECEITA (A a J = 10 OSs)");
  Serial.println("  REC A .. REC J     → apaga slot escolhido e inicia gravação");
  Serial.println("  MARK               → grava posição atual como waypoint (dwell padrão)");
  Serial.println("  MARK 800           → grava com tempo parado = 800 ms (customizado)");
  Serial.println("  POP                → remove último waypoint gravado (se errou)");
  Serial.println("  SAVE               → finaliza e persiste na NVS");
  Serial.println("  CANCEL             → aborta sem salvar (mantém receita antiga)");
  Serial.println();
  Serial.println("EXECUÇÃO");
  Serial.println("  A .. J             → executa receita do slot A a J");
  Serial.println("  EXE                → executa todos os slots ativos em ordem");
  Serial.println();
  Serial.println("INSPEÇÃO / LIMPEZA");
  Serial.println("  LIST               → resume todos os slots");
  Serial.println("  SHOW A             → detalha waypoints do slot A");
  Serial.println("  CLEAR A YES        → apaga slot A (confirmação necessária)");
  Serial.println("  CLEAR ALL YES      → apaga todos os slots");
  Serial.println("  LOAD_DEFAULTS YES  → sobrescreve A-J com as 10 OSs do SAP hardcoded");
  Serial.println("────────────────────────────────────────────────────────────\n");
}

void imprimir_status() {
  char grav_str[16];
  if (slot_gravando < 0) strcpy(grav_str, "off");
  else snprintf(grav_str, sizeof(grav_str), "slot %c", slot_letra(slot_gravando));

  Serial.printf("[STATUS] Pos=(%.2f, %.2f)  Homed=%s  Grav=%s\n",
                pos_x_mm(), pos_y_mm(),
                ja_fez_homing ? "SIM" : "nao",
                grav_str);
  Serial.printf("         FEED=%.0f mm/min (%.1f mm/s)  ACCEL=%.0f mm/s²  Step=%.2fmm  TempoParado=%dms\n",
                FEED_MM_MIN, FEED_MM_MIN / 60.0f,
                ACCEL_MM_S2, jog_step_mm, dwell_padrao_ms);
  Serial.printf("         Envelope: X ∈ [%.1f, %.1f]  Y ∈ [%.1f, %.1f]\n",
                LIMITE_MIN_X_MM, LIMITE_MAX_X_MM,
                LIMITE_MIN_Y_MM, LIMITE_MAX_Y_MM);
  Serial.printf("         Centro operacional (comando C): (%.2f, %.2f) mm\n",
                CENTRO_X_MM, CENTRO_Y_MM);
}

// Processa comandos em UPPERCASE
// O PARSER HUMANO. Só `linhaHumana()` o chama, e `linhaHumana()` só recebe
// linha sem '{' — ver `despacharLinha()` no header.
//
// A guarda abaixo protege a invariante no ponto EXATO onde ela pode ser
// violada, que é a única posição que sobrevive a um chamador novo. O corte por
// '{' mora no header, mas o `toUpperCase` mora aqui: quem um dia acrescentar um
// segundo caminho para esta função (um comando de auto-teste no boot, um replay
// de linha guardada) não vai reler o header, e o estrago não daria erro — o
// JSON viraria {"CMD":"MOVER"}, sem chave que case, e a mesa ficaria muda para
// o adapter enquanto responde normalmente ao humano.
void processar_comando(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  if (cmd.indexOf('{') >= 0) {
    logMsg("ERRO", "linha de MAQUINA chegou ao parser humano e NAO foi executada "
                   "- o corte por '{' e de despacharLinha()");
    return;
  }

  String orig = cmd;   // pra WASD, é case-sensitive; verificamos separado
  cmd.toUpperCase();   // MAIÚSCULAS SÓ AQUI — nunca na leitura da serial
  Serial.printf(">>> %s\n", cmd.c_str());

  // WASD — sequência de letras minúsculas executada como jogs sucessivos.
  // Ex: "wwww"  = 4 jogs pra frente (Y+)
  //     "ddss"  = 2 pra direita, depois 2 pra trás
  //     "wdwd"  = escadinha
  // Cada letra dispara um mover_x/mover_y de STEP mm. Aborta se algum sair
  // do envelope ou limit disparar (não emite alarme, só para a sequência).
  bool eh_seq_wasd = orig.length() > 0;
  for (unsigned int i = 0; i < orig.length(); i++) {
    char c = orig.charAt(i);
    if (c != 'w' && c != 'a' && c != 's' && c != 'd') { eh_seq_wasd = false; break; }
  }
  if (eh_seq_wasd) {
    Serial.printf("[WASD] Sequência de %d jog(s) x %.2f mm cada\n",
                  orig.length(), jog_step_mm);
    for (unsigned int i = 0; i < orig.length(); i++) {
      bool ok = true;
      switch (orig.charAt(i)) {
        case 'w': ok = mover_y(+jog_step_mm); break;
        case 'a': ok = mover_x(-jog_step_mm); break;
        case 's': ok = mover_y(-jog_step_mm); break;
        case 'd': ok = mover_x(+jog_step_mm); break;
      }
      if (!ok) {
        Serial.printf("  Sequência abortada no jog %u/%u\n", i + 1, orig.length());
        break;
      }
    }
    imprimir_status();
    return;
  }

  // Comandos simples
  if (cmd == "H") { homing_completo(); return; }
  if (cmd == "?") { imprimir_status(); return; }
  if (cmd == "AJUDA" || cmd == "HELP") { imprimir_ajuda(); return; }

  // LOAD_DEFAULTS YES  → sobrescreve os 10 slots (A-J) com as receitas
  // hardcoded no código e persiste em NVS. Exige a palavra YES pra evitar
  // que o operador apague sem querer as receitas que ele calibrou.
  if (cmd.startsWith("LOAD_DEFAULTS")) {
    if (cmd == "LOAD_DEFAULTS YES") {
      Serial.println("[NVS] Recarregando 10 receitas padrão do código (sobrescreve slots A-J)...");
      carregar_receitas_padrao();
      nvs_salvar_todos();
      Serial.println("[NVS] Feito. Envie SLOTS pra conferir.");
    } else {
      Serial.println("[!] Comando destrutivo. Digite EXATAMENTE: LOAD_DEFAULTS YES");
    }
    return;
  }
  if (cmd == "C" || cmd == "CENTRO") {
    mover_para(CENTRO_X_MM, CENTRO_Y_MM); imprimir_status(); return;
  }

  // NOVOCENTRO x y   → define centro operacional em coordenadas absolutas
  // NOVOCENTRO AQUI  → adota a posição atual do cabeçote
  // NOVOCENTRO PADRAO → volta ao centro geométrico do envelope
  if (cmd.startsWith("NOVOCENTRO")) {
    String rest = cmd.substring(10); rest.trim();
    if (rest == "AQUI") {
      if (!ja_fez_homing) {
        Serial.println("[NOVOCENTRO] Faça homing primeiro (H) — posição atual não confiável.");
        return;
      }
      CENTRO_X_MM = pos_x_mm();
      CENTRO_Y_MM = pos_y_mm();
      salvar_params_nvs();
      Serial.printf("[NOVOCENTRO] Definido como posição atual: (%.2f, %.2f) mm\n",
                    CENTRO_X_MM, CENTRO_Y_MM);
      beep_n(2, 60);
    } else if (rest == "PADRAO") {
      CENTRO_X_MM = (LIMITE_MIN_X_MM + LIMITE_MAX_X_MM) / 2.0f;
      CENTRO_Y_MM = (LIMITE_MIN_Y_MM + LIMITE_MAX_Y_MM) / 2.0f;
      salvar_params_nvs();
      Serial.printf("[NOVOCENTRO] Reset ao centro geométrico: (%.2f, %.2f) mm\n",
                    CENTRO_X_MM, CENTRO_Y_MM);
    } else if (rest.length() > 0) {
      int sep = rest.indexOf(' ');
      if (sep < 0) {
        Serial.println("[NOVOCENTRO] Uso: NOVOCENTRO <x> <y>  |  NOVOCENTRO AQUI  |  NOVOCENTRO PADRAO");
        return;
      }
      float xt = rest.substring(0, sep).toFloat();
      float yt = rest.substring(sep + 1).toFloat();
      if (!posicao_dentro_envelope(xt, yt)) {
        Serial.printf("[NOVOCENTRO] (%.2f, %.2f) fora do envelope — rejeitado\n", xt, yt);
        return;
      }
      CENTRO_X_MM = xt;
      CENTRO_Y_MM = yt;
      salvar_params_nvs();
      Serial.printf("[NOVOCENTRO] Definido: (%.2f, %.2f) mm — persistido em NVS\n",
                    CENTRO_X_MM, CENTRO_Y_MM);
      beep_n(2, 60);
    } else {
      Serial.printf("[NOVOCENTRO] Centro atual: (%.2f, %.2f) mm\n", CENTRO_X_MM, CENTRO_Y_MM);
      Serial.println("             Uso: NOVOCENTRO <x> <y>  |  NOVOCENTRO AQUI  |  NOVOCENTRO PADRAO");
    }
    return;
  }
  if (cmd == "LIST") { slot_list(); return; }
  if (cmd == "EXE") { slot_executar_todos(); return; }
  if (cmd == "MARK") { slot_mark(dwell_padrao_ms); return; }
  if (cmd == "POP")  { slot_pop(); return; }
  if (cmd == "SAVE") { slot_save(); return; }
  if (cmd == "CANCEL") { slot_cancel(); return; }
  if (cmd == "CLEAR ALL YES") {
    for (int i = 0; i < NUM_SLOTS; i++) slot_clear(i);
    Serial.println("[CLEAR ALL] Todos slots apagados.");
    return;
  }

  // MARK N
  if (cmd.startsWith("MARK ")) {
    int n = cmd.substring(5).toInt();
    if (n <= 0 || n > 60000) {
      Serial.println("[MARK] Dwell inválido. Use 1..60000 ms");
    } else {
      slot_mark((uint16_t)n);
    }
    return;
  }

  // DWELL N — tempo parado (em ms) nos waypoints das receitas
  if (cmd.startsWith("DWELL ")) {
    int n = cmd.substring(6).toInt();
    if (n < 0 || n > 60000) {
      Serial.println("[DWELL] Valor inválido. Use 0..60000 ms");
    } else {
      dwell_padrao_ms = n;
      salvar_params_nvs();
      Serial.printf("[DWELL] Novo tempo parado padrão: %d ms\n", n);
    }
    return;
  }

  // STEP N — tamanho do jog manual (WASD)
  if (cmd.startsWith("STEP ")) {
    float v = cmd.substring(5).toFloat();
    if (v <= 0 || v > 50) {
      Serial.println("[STEP] Valor inválido. Use 0.1..50 mm");
    } else {
      jog_step_mm = v;
      salvar_params_nvs();
      Serial.printf("[STEP] Novo jog step: %.2f mm\n", v);
    }
    return;
  }

  // FEED N — velocidade cruzeiro (mm/min)
  if (cmd.startsWith("FEED ")) {
    float v = cmd.substring(5).toFloat();
    if (v < 30 || v > 6000) {
      Serial.println("[FEED] Valor inválido. Use 30..6000 mm/min (0.5..100 mm/s)");
    } else {
      FEED_MM_MIN = v;
      Serial.printf("[FEED] Nova velocidade cruzeiro: %.0f mm/min (%.2f mm/s)\n",
                    v, v / 60.0f);
      salvar_params_nvs();
    }
    return;
  }

  // ACCEL N — aceleração (mm/s²)
  if (cmd.startsWith("ACCEL ")) {
    float v = cmd.substring(6).toFloat();
    if (v < 20 || v > 5000) {
      Serial.println("[ACCEL] Valor inválido. Use 20..5000 mm/s²");
    } else {
      ACCEL_MM_S2 = v;
      Serial.printf("[ACCEL] Nova aceleração: %.0f mm/s²\n", v);
      salvar_params_nvs();
    }
    return;
  }

  // REC X
  if (cmd.startsWith("REC ")) {
    int idx = slot_indice(cmd[4]);
    if (idx < 0) Serial.println("[REC] Slot inválido. Use REC <A-J>");
    else slot_rec_iniciar(idx);
    return;
  }

  // SHOW X
  if (cmd.startsWith("SHOW ")) {
    int idx = slot_indice(cmd[5]);
    if (idx < 0) Serial.println("[SHOW] Slot inválido. Use SHOW <A-J>");
    else slot_show(idx);
    return;
  }

  // CLEAR X [YES]
  if (cmd.startsWith("CLEAR ")) {
    if (cmd.length() < 9 || !cmd.endsWith(" YES")) {
      Serial.println("[CLEAR] Confirmação necessária: 'CLEAR <A-J> YES' ou 'CLEAR ALL YES'");
      return;
    }
    int idx = slot_indice(cmd[6]);
    if (idx < 0) {
      Serial.println("[CLEAR] Slot inválido.");
    } else {
      slot_clear(idx);
    }
    return;
  }

  // MP x y
  if (cmd.startsWith("MP ")) {
    String rest = cmd.substring(3); rest.trim();
    int sep = rest.indexOf(' ');
    if (sep < 0) { Serial.println("[MP] Uso: MP <x> <y>"); return; }
    float xt = rest.substring(0, sep).toFloat();
    float yt = rest.substring(sep + 1).toFloat();
    mover_para(xt, yt);
    imprimir_status();
    return;
  }

  // X±N, Y±N
  if (cmd.length() >= 2 && (cmd[0] == 'X' || cmd[0] == 'Y')) {
    float v = cmd.substring(1).toFloat();
    if (cmd[0] == 'X') mover_x(v); else mover_y(v);
    imprimir_status();
    return;
  }

  // Letra única A..J = executa slot
  if (cmd.length() == 1) {
    int idx = slot_indice(cmd[0]);
    if (idx >= 0) {
      slot_executar(idx);
      return;
    }
  }

  Serial.printf("[ERR] Comando desconhecido: '%s'\n", cmd.c_str());
  imprimir_ajuda();
}

// ═════════════════════════════════════════════════════════════════════════════
// EVENTOS DO CONTRATO (§4)
// ═════════════════════════════════════════════════════════════════════════════
//
// Os nomes de campo são os MESMOS do payload HTTP do `cnc_simulator`, e isso
// não é preguiça: o adapter repassa o evento CRU ao central, sem traduzir nada.
// Um campo renomeado aqui não daria erro em lugar nenhum — daria uma coluna
// vazia no banco.
//
// NADA DE EVENTO PERIÓDICO DURANTE O MOVIMENTO. Não há `movendo`, não há
// `retornando`, não há progresso, e a razão é de TEMPO: uma linha de ~200 B a
// 115200 baud custa ~17 ms, o curso mais longo da célula dura ~2 s, e o central
// dispara o `dispensar` por RELÓGIO. Cada linha emitida no caminho empurra a
// chegada real para depois da hora agendada — ou seja, comprimido caindo com a
// mesa ainda em trânsito. A trajetória ao vivo custaria uma dispensa no chão.

static void emitPosicionado(const char* os_id, int dispenser_alvo,
                            int ciclo_atual, int total_ciclos) {
  char ts[24]; tsAgora(ts, sizeof ts);
  char sx[16]; fmtF(sx, sizeof sx, pos_x_mm(), 2);
  char sy[16]; fmtF(sy, sizeof sy, pos_y_mm(), 2);
  // A posição é lida do tracker DEPOIS do movimento: é a MEDIDA, não o alvo.
  // Reemitir o alvo faria o evento confirmar o que o comando já dizia, e o
  // central passaria a registrar uma posição que ninguém verificou.
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"posicionado\",\"os_id\":\"%s\",\"dispenser_alvo\":%d,"
    "\"posicao_x\":%s,\"posicao_y\":%s,\"ciclo_atual\":%d,\"total_ciclos\":%d,"
    "\"ts\":\"%s\"}}",
    os_id, dispenser_alvo, sx, sy, ciclo_atual, total_ciclos, ts), "posicionado");
}

static void emitConcluido(const char* os_id) {
  char ts[24]; tsAgora(ts, sizeof ts);
  char sx[16]; fmtF(sx, sizeof sx, pos_x_mm(), 2);
  char sy[16]; fmtF(sy, sizeof sy, pos_y_mm(), 2);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"concluido\",\"os_id\":\"%s\",\"posicao_x\":%s,"
    "\"posicao_y\":%s,\"ts\":\"%s\"}}",
    os_id, sx, sy, ts), "concluido");
}

static void emitErroCnc(const char* os_id, int dispenser_alvo,
                        const char* codigo, const char* descricao) {
  // O `erro` desbloqueia quem espera. No modelo por relógio ele é mais que
  // registro: é o VETO — o central o lê antes da hora do `dispensar` e cancela
  // o agendamento, em vez de deixar o ciclo inteiro correr no vazio.
  char ts[24]; tsAgora(ts, sizeof ts);
  char desc[160]; copiarCampo(desc, sizeof desc, descricao);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"erro\",\"os_id\":\"%s\",\"dispenser_alvo\":%d,"
    "\"codigo_erro\":\"%s\",\"descricao\":\"%s\",\"ts\":\"%s\"}}",
    os_id, dispenser_alvo, codigo, desc, ts), "erro");
}

// Recusa: ACK NEGATIVO **e** evento `erro`, sempre os dois.
//
// Os dois porque eles vão para leitores diferentes. O ACK negativo vira 502 no
// adapter e volta ao orquestrador como `cmd_mover` falso — é o que faz o
// cronograma ser cancelado ANTES da hora do `dispensar`. O evento vira alarme e
// linha de histórico, com o `codigo_erro` que manda o técnico à peça certa. Só
// o ACK deixaria a recusa sem rastro; só o evento deixaria o central seguindo o
// relógio até despejar medicamento numa mesa que não chegou.
static void recusarMover(long cmd_id, const char* os_id, int dispenser_alvo,
                         const char* codigo, const char* descricao) {
  ackErro(cmd_id, codigo);
  emitErroCnc(os_id, dispenser_alvo, codigo, descricao);
  logMsg("APSEN", "mover D%d recusado: %s (%s)", dispenser_alvo, codigo, descricao);
}

// ═════════════════════════════════════════════════════════════════════════════
// COMANDOS DE MÁQUINA (§4)
// ═════════════════════════════════════════════════════════════════════════════

static void cmdMover(const char* linha, long cmd_id) {
  float falvo = 0, fciclo = 0, ftotal = 0;
  char os_id[MAX_OS_ID_LEN];
  char receita[8];

  if (!jsonNumero(linha, "dispenser_alvo", &falvo) ||
      !jsonTexto(linha, "os_id", os_id, sizeof os_id)) {
    // Sem `os_id` não há evento a emitir: `erro` é endereçado a uma OS, e um
    // evento sem dono chega ao central como linha órfã. Aqui o ACK negativo é
    // o único aviso possível — e é suficiente, porque quem mandou o comando
    // malformado está do outro lado dele.
    ackErro(cmd_id, "campos ausentes em mover");
    logMsg("APSEN", "mover sem dispenser_alvo ou os_id - recusado");
    return;
  }

  // `receita` é ACEITA e vai para o log; ela NÃO acha a posição (quem acha é
  // `dispenser_pos`). Opcional de propósito: a mesa sabe ir a um dispenser sem
  // saber qual ordem está rodando, e exigi-la transformaria um campo de
  // rastreio em motivo de recusa.
  if (!jsonTexto(linha, "receita", receita, sizeof receita)) receita[0] = '\0';

  const int alvo  = (int)falvo;
  const int ciclo = jsonNumero(linha, "ciclo_atual", &fciclo) ? (int)fciclo : 0;
  const int total = jsonNumero(linha, "total_ciclos", &ftotal) ? (int)ftotal : 0;

  // Um movimento por vez, e a recusa é só no ACK — `movimento_em_curso` é
  // estado do canal, não falha da mesa, e não pertence ao vocabulário de
  // `codigo_erro`. Mesma escolha da placa dos mecanismos com `canalEmOperacao`.
  if (movimento_em_curso) {
    ackErro(cmd_id, "placa ocupada: mesa em movimento");
    logMsg("APSEN", "mover D%d recusado: mesa ja em movimento", alvo);
    return;
  }

  // A trava vem primeiro: com ela ativa a mesa não vai a lugar nenhum, e o
  // motivo é a trava — não a faixa, não o homing. Recusar aqui com ACK negativo
  // é o que faz o central cancelar o agendamento em vez de deixar o ciclo
  // inteiro correr no vazio.
  if (trava_ativa) {
    recusarMover(cmd_id, os_id, alvo, ERRO_TRAVADO,
                 "trava do Triple Check ativa: aguardando liberacao");
    return;
  }

  const DispenserPos* p = dispenser_pos((uint8_t)alvo);
  if (p == nullptr) {
    recusarMover(cmd_id, os_id, alvo, ERRO_DISPENSER_INVALIDO,
                 "dispenser_alvo fora de 1..8");
    return;
  }
  if (!ja_fez_homing) {
    recusarMover(cmd_id, os_id, alvo, ERRO_SEM_HOMING,
                 "origem nunca estabelecida: mande homing antes");
    return;
  }
  if (!posicao_dentro_envelope(p->x, p->y)) {
    // O waypoint calibrado caiu fora dos limites — alguém recalibrou o
    // dispenser ou mexeu no envelope. Recusar aqui, e não deixar o soft-limit
    // de `mover_para` recusar depois, é o que separa este caso do
    // `limit_disparado`: são causas diferentes e mandam o técnico a peças
    // diferentes.
    recusarMover(cmd_id, os_id, alvo, ERRO_FORA_DO_ENVELOPE,
                 "waypoint do dispenser fora do envelope da mesa");
    return;
  }
  if (qualquer_limit_disparado_rapido()) {
    recusarMover(cmd_id, os_id, alvo, ERRO_LIMIT_DISPARADO,
                 "fim de curso ja acionado antes do movimento");
    return;
  }

  // Tudo que dá para saber ANTES já foi conferido: daqui em diante o ACK é
  // positivo, e o que falhar vira evento. A ordem é a feature deste modelo —
  // toda recusa que consegue ser recusa acontece antes do ACK, porque é o ACK
  // negativo que cancela o cronograma a tempo.
  ackOk(cmd_id, false);
  logMsg("APSEN", "mover D%d receita %s ciclo %d/%d OS %s",
         alvo, receita[0] ? receita : "-", ciclo, total, os_id);

  // Guardados para o caso de a trava chegar NO MEIO deste movimento: é com
  // eles que o evento `erro` sai endereçado à OS certa.
  strncpy(trava_os_id, os_id, sizeof trava_os_id - 1);
  trava_os_id[sizeof trava_os_id - 1] = '\0';
  trava_dispenser = alvo;

  if (!mover_para(p->x, p->y)) {
    if (trava_ativa) {
      // Interrompido pela trava, não por falha mecânica. O evento vai com
      // `travado`, e é ele que diz a quem esperava a chegada que ela não vem.
      trava_interrompeu_mover = false;
      emitErroCnc(os_id, alvo, ERRO_TRAVADO,
                  "movimento interrompido pela trava do Triple Check");
      // O retorno e CONFERIDO, como em `cmdHoming` e pela mesma razao que o §4
      // do protocolo registra: `concluido` depois de um homing que falhou
      // afirmaria que a mesa esta no HOME, e ela esta onde o eixo travou. Pior
      // aqui do que la, porque `ja_fez_homing` fica false e TODO `mover`
      // seguinte e recusado com `sem_homing`: o supervisor libera a trava e a
      // OS seguinte aborta com um erro que aponta para o lugar errado.
      if (!homing_completo()) {
        emitErroCnc(os_id, alvo, ERRO_HOMING_FALHOU,
                    "um dos eixos nao achou o fim de curso no prazo");
        return;
      }
      emitConcluido(os_id);
      return;
    }
    // Único caminho de falha DEPOIS do ACK positivo: o fim de curso disparou
    // com a mesa andando. Não há ACK a corrigir — o evento é o aviso, e é ele
    // que cancela o cronograma antes da hora do `dispensar`.
    emitErroCnc(os_id, alvo, ERRO_LIMIT_DISPARADO,
                "fim de curso disparou durante o movimento");
    logMsg("APSEN", "mover D%d abortado por limit em (%.2f, %.2f)",
           alvo, pos_x_mm(), pos_y_mm());
    return;
  }

  emitPosicionado(os_id, alvo, ciclo, total);
}

static void cmdEstadoCelula(const char* linha, long cmd_id) {
  bool ativa = false;
  if (!jsonBool(linha, "trava_ativa", &ativa)) {
    ackErro(cmd_id, "campos ausentes em estado_celula");
    return;
  }

  // ACK PRIMEIRO, e antes de qualquer movimento. Este comando tem de ser aceito
  // sobretudo NO MEIO de um `mover` — é para isso que existe o `serialPoll()`
  // dentro do `sync_move`. Um ACK que só saísse depois do homing chegaria
  // segundos atrasado ao central, que está contando o prazo do aviso.
  ackOk(cmd_id, false);

  char resumo[TRAVA_RESUMO_MAX + 1];
  if (!jsonTexto(linha, "trava_resumo", resumo, sizeof resumo)) resumo[0] = '\0';
  float fslot = 0;
  const int slot = jsonNumero(linha, "trava_slot_id", &fslot) ? (int)fslot : 0;

  // Age só na TRANSIÇÃO. Receber `true` duas vezes é inofensivo: a segunda não
  // refaz o homing. O central reenvia o aviso sempre que a trava muda, sem
  // saber o que esta placa já sabe, então idempotência aqui não é luxo.
  const bool anterior = trava_ativa;
  trava_ativa = ativa;
  if (ativa == anterior) return;

  if (ativa) {
    // Quem estava andando descobre pelo laço de `sync_move`, que confere esta
    // flag a cada passo. Aqui só se registra que havia movimento — o `erro` e
    // o homing saem de lá, de dentro do `mover` interrompido, porque é lá que
    // se sabe qual OS e qual dispenser esperavam a chegada.
    trava_interrompeu_mover = movimento_em_curso;
    beep_alerta();
    Serial.printf("\n⛔ TRAVA DO TRIPLE CHECK — slot %d, OS %s\n   %s\n"
                  "   A mesa nao aceita mover ate a liberacao.\n",
                  slot, jsonTemChave(linha, "os_id") ? "(ver log)" : "-", resumo);
    logMsg("APSEN", "trava ativa (slot %d): %s", slot, resumo);

    if (!trava_interrompeu_mover) {
      // Parada, a mesa vai para o HOME por conta própria: é onde o supervisor
      // espera encontrá-la para mexer na bancada. Com um `mover` em curso, quem
      // faz o homing é o próprio `cmdMover` ao ver que foi interrompido — fazer
      // os dois produziria dois homings encavalados.
      // Mesmo conferimento do caminho acima: sem origem estabelecida, o
      // `concluido` mentiria sobre onde a mesa esta, e quem le o erro no
      // central e quem vai ate a bancada antes de liberar a trava.
      if (!homing_completo()) {
        emitErroCnc(trava_os_id[0] ? trava_os_id : "", trava_dispenser,
                    ERRO_HOMING_FALHOU,
                    "um dos eixos nao achou o fim de curso no prazo");
        return;
      }
      emitConcluido(trava_os_id[0] ? trava_os_id : "");
    }
    return;
  }

  // Liberação: volta a aceitar `mover`, e NADA de homing. A mesa já está no
  // HOME desde a ativação, e um segundo homing custaria segundos no exato
  // momento em que o supervisor acabou de liberar a produção.
  trava_interrompeu_mover = false;
  Serial.println("\n✓ Trava liberada — a mesa volta a aceitar comandos.\n");
  logMsg("APSEN", "trava liberada");
}


static void cmdHoming(const char* linha, long cmd_id) {
  char os_id[MAX_OS_ID_LEN];
  if (!jsonTexto(linha, "os_id", os_id, sizeof os_id)) {
    ackErro(cmd_id, "campos ausentes em homing");
    return;
  }
  if (movimento_em_curso) {
    ackErro(cmd_id, "placa ocupada: mesa em movimento");
    return;
  }

  ackOk(cmd_id, false);

  if (!homing_completo()) {
    // NÃO emite `concluido`. Um `concluido` depois de um homing que estourou o
    // timeout afirmaria que a mesa está no HOME, e ela está onde o eixo travou
    // — com `ja_fez_homing` em false, que é o que barra o próximo `mover`.
    emitErroCnc(os_id, 0, ERRO_HOMING_FALHOU,
                "um dos eixos nao achou o fim de curso no prazo");
    return;
  }
  emitConcluido(os_id);
}

// ═════════════════════════════════════════════════════════════════════════════
// O CONTRATO QUE `apsen_serial.h` PEDE DO SKETCH
// ═════════════════════════════════════════════════════════════════════════════

// Quais comandos de MÁQUINA existem. `ping`/`pong` são mensagens de serviço e o
// header já os trata sozinho, então não aparecem aqui.
//
// Comando fora da lista recusa com ACK NEGATIVO, nunca em silêncio: o adapter o
// transforma em 502 e quem pediu sabe na hora. Calado, o orquestrador queimaria
// o `ack_timeout_s` inteiro para descobrir exatamente a mesma coisa — e, no
// modelo por relógio, esse tempo sai do prazo que ele já tem correndo do outro
// lado.
//
// NENHUM comando de bancada por JSON. FEED, ACCEL, STEP, NOVOCENTRO, REC, MARK
// e SAVE existem SÓ no terminal humano: eles mudam a calibração da máquina, e
// calibração mudada de fora não deixa rastro na bancada onde alguém vai
// procurar por que a mesa passou a parar dois milímetros adiante.
void executarComando(const char* cmd, const char* linha, long cmd_id) {
  if      (strcmp(cmd, "mover")  == 0) cmdMover(linha, cmd_id);
  else if (strcmp(cmd, "homing") == 0) cmdHoming(linha, cmd_id);
  else if (strcmp(cmd, "estado_celula") == 0) cmdEstadoCelula(linha, cmd_id);
  else {
    ackErro(cmd_id, "comando_desconhecido");
    logMsg("APSEN", "comando de maquina fora do contrato: '%s'", cmd);
  }
}

// A linha SEM '{' — o outro contrato que o header pede.
void linhaHumana(char* linha) {
  processar_comando(String(linha));
  // A linha em branco que o `loop()` imprimia depois de cada comando. Ela mora
  // AQUI e não lá porque o `loop()` agora roda a cada poucos milissegundos:
  // deixada no laço, viraria uma linha em branco por iteração. E ela é do
  // humano — despejá-la depois de cada JSON encheria de ruído um canal que já
  // divide banda com os ACKs que o adapter está esperando.
  Serial.println();
}

// ═════════════════════════════════════════════════════════════════════════════
// SETUP
// ═════════════════════════════════════════════════════════════════════════════
void setup() {
  // ANTES do Serial.begin(): o buffer de recepção padrão do ESP32 é de 256 B e
  // a maior linha do contrato vai a 1024. Dimensioná-lo pelo TETO DA LINHA é o
  // que garante que uma mensagem nunca chegue partida — e meia linha é JSON
  // inválido, que some sem erro em lugar nenhum.
  apsenSerialInit();
  Serial.begin(115200);
  delay(500);

  // A PRIMEIRA linha de máquina da porta, e ela sai antes de tudo que demora.
  // Quem inicia o ping é SEMPRE a placa: é por ele que o adapter identifica
  // esta porta como a da mesa, sem depender de VID/PID — o mesmo conversor
  // USB-serial aparece em placas de fabricantes diferentes, e casar por ele
  // mandaria `dispensar` para a balança.
  //
  // Sai aqui, e não só no fim do setup, por causa do relógio de quem procura: o
  // adapter espera ~9,5 s por porta, e o boot desta placa passa disso com
  // folga (beeps, NVS e o homing, que tem timeout de 60 s). Pingar só no fim
  // faria a mesa nunca ser achada na varredura — sem erro em lugar nenhum, e
  // com a placa funcionando perfeitamente para o humano.
  emitPing();
  Serial.println("\n═══════════════════════════════════════════════════════");
  Serial.println("  NEXT 2K26 — Receitas Manuais v1");
  Serial.println("═══════════════════════════════════════════════════════");

  // Pinos motores
  pinMode(MOTOR_X.step, OUTPUT); pinMode(MOTOR_X.dir, OUTPUT); pinMode(MOTOR_X.en, OUTPUT);
  pinMode(MOTOR_Y.step, OUTPUT); pinMode(MOTOR_Y.dir, OUTPUT); pinMode(MOTOR_Y.en, OUTPUT);
  digitalWrite(MOTOR_X.step, LOW); digitalWrite(MOTOR_Y.step, LOW);
  // Pinos limits
  pinMode(LIMIT_X_PIN, INPUT_PULLUP);
  pinMode(LIMIT_Y_PIN, INPUT_PULLUP);
  // Buzzer
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);

  motor_habilitar(MOTOR_X, true);
  motor_habilitar(MOTOR_Y, true);
  delay(500);

  beep_longo(500);
  delay(500);

  // Carrega parâmetros globais (FEED, ACCEL, STEP, DWELL) e slots
  carregar_params_nvs();
  nvs_carregar_todos();

  // Se NVS está zerada (primeiro boot ou wipe), carrega as 10 receitas
  // padrão do SAP direto no código e persiste. Slots gravados pelo usuário
  // via REC/SAVE prevalecem nos boots seguintes.
  int total_slots_ativos = 0;
  for (int i = 0; i < NUM_SLOTS; i++) {
    if (slots[i].num_pontos > 0) total_slots_ativos++;
  }
  if (total_slots_ativos == 0) {
    Serial.println("[NVS] Nenhum slot gravado — carregando 10 receitas padrão do SAP...");
    carregar_receitas_padrao();
    nvs_salvar_todos();
    Serial.println("[NVS] 10 receitas padrão persistidas em NVS (A-J).");
  }

  Serial.printf("[NVS] FEED=%.0f mm/min | ACCEL=%.0f mm/s² | STEP=%.2f mm | DWELL=%d ms\n",
                FEED_MM_MIN, ACCEL_MM_S2, jog_step_mm, dwell_padrao_ms);
  Serial.printf("[NVS] Centro operacional: (%.2f, %.2f) mm\n",
                CENTRO_X_MM, CENTRO_Y_MM);
  slot_list();

  Serial.printf("Estado limits: X=%s  Y=%s\n",
                limit_x_triggered() ? "TRIGGERED" : "livre",
                limit_y_triggered() ? "TRIGGERED" : "livre");
  if (limit_x_triggered() || limit_y_triggered()) {
    Serial.println("[AVISO] Limit já triggered — solte manualmente antes do homing.");
    delay(3000);
  }

  // Homing automático
  homing_completo();

  imprimir_ajuda();
  imprimir_status();
  Serial.println("Pronto pra gravar receitas ou executar. Envie AJUDA se esquecer.\n");

  // O boot inteiro acima é do caminho humano e fica como está — nenhuma dessas
  // linhas contém '{', então o adapter as lê como log. Segundo ping: o homing
  // pode ter consumido a janela de quem estava varrendo as portas.
  emitPing();
}

// ═════════════════════════════════════════════════════════════════════════════
// LOOP
// ═════════════════════════════════════════════════════════════════════════════
void loop() {
  // `Serial.readStringUntil('\n')` saiu, e as duas razões são de máquina:
  //
  //   * ele BLOQUEIA até o timeout do Serial quando a linha não termina. Numa
  //     porta que agora recebe comando com prazo de ACK, um caractere solto do
  //     terminal segurava o laço inteiro — inclusive o `mover` que o
  //     orquestrador está esperando;
  //   * ele aloca uma String no heap por linha lida. Com a telemetria e os
  //     ACKs dividindo o canal, isso é fragmentação contínua numa placa que
  //     precisa ficar meses ligada.
  //
  // `serialPoll()` lê o que chegou e volta na hora, mantém a linha num buffer
  // estático e só entrega quando ela fecha.
  serialPoll();
  pingPoll();   // voz de máquina: quem pinga é a placa
  delay(2);
}
