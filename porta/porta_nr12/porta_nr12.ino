/*
 * =====================================================================
 *  PORTA NR-12 (prototipo academico) - ESP32 + RC522 + 2 fins de curso
 *  - `sub: "porta"`
 * =====================================================================
 *  Duas portas fisicas: ES1 = TETO (abastecer remedio), ES2 = FRENTE (a
 *  caixa sobre a balanca). A celula so esta FECHADA com as DUAS fechadas.
 *
 *  SEM FECHADURA ELETRICA. Cada porta e presa por um IMA simples e o fim de
 *  curso diz se ela esta fechada: quem puxa abre, a qualquer momento. O que
 *  esta placa controla nao e a abertura, e a AUTORIZACAO dela.
 *
 *  QUEM AUTORIZA E O CENTRAL. Esta placa valida a TAG (lista local na NVS),
 *  mas nao se autoriza sozinha: ela pede acesso (`pedido_acesso`), o central
 *  PARA a celula inteira e so entao manda `porta_liberar`, que acende o LED de
 *  "pode abrir". Aberta com o LED aceso, a abertura e AUTORIZADA; aberta antes
 *  (ou sem tag), e SEM autorizacao — alarme, e a celula para do mesmo jeito.
 *  Sem resposta do central em ESPERA_CENTRAL_MS, o LED nao acende. O contrato
 *  e `docs/PROTOCOLO_SERIAL.md` §9.
 *
 *  UMA USB, DUAS VOZES (a mesma regra das outras placas)
 *    Esta placa tem USB PROPRIA no mini PC, e o dono dela e o `porta-adapter`
 *    (HTTP 8104). Na mesma USB andam:
 *      - a voz de MAQUINA: linha com chave. Todo evento do contrato, todo ACK
 *        e todo comando do central. Lida SO pelo `serialPoll()` do header;
 *      - a voz de BANCADA: linha sem chave. Os comandos de um caractere
 *        R/S/C/L/E chegam por `linhaHumana()`, e os textos para humano saem por
 *        `logMsg`/`Serial.print` - nenhum deles com chave de abertura, que
 *        viraria linha de maquina do outro lado.
 *    O adapter nunca manda linha sem chave. E no Windows a COM abre em UM
 *    programa por vez: para cadastrar tag pelo Monitor Serial, PARE o
 *    porta-adapter antes.
 *
 *  LIGACOES (ESP32 NodeMCU / DevKit V1) - as do RC522 e dos fins de curso sao
 *  as VALIDADAS na bancada, sem mudar um pino.
 *    RC522 -> ESP32  (alimentar SEMPRE em 3V3)
 *      SDA/SS -> D23    SCK  -> D22
 *      MOSI   -> D21    MISO -> D19
 *      RST    -> D15    3.3V -> 3V3   GND -> GND
 *    Fins de curso (micro switch)
 *      ES1 (TETO):   COM -> GND    NO -> D4  (INPUT_PULLUP)
 *      ES2 (FRENTE): COM -> GND    NO -> D2  (INPUT_PULLUP + resistor 10k
 *                    entre D2 e 3V3, porque o LED da placa fica no D2 e puxa
 *                    o sinal para baixo)
 *      Fechado = alavanca pressionada = LOW
 *      Fio rompido = HIGH = lido como "aberto" (falha segura)
 *    Fechadura: NENHUMA. Cada porta e presa por um ima simples; o fim de
 *      curso e o unico sensor dela.
 *    USB -> mini PC (a COM fixada no Windows vai em PORTA_SERIAL_URL)
 *    LED "pode abrir" -> D25 (aceso enquanto a abertura esta AUTORIZADA)
 *
 *  COMANDOS DE BANCADA (Monitor Serial da USB, 115200, com o porta-adapter
 *  PARADO). Uma letra por linha; maiuscula ou minuscula, espacos ignorados.
 *    R -> entra no modo REGISTRO (aproxime a tag a cadastrar)
 *    S -> salva a tag lida (dentro do modo registro)
 *    C -> cancela o registro
 *    L -> lista as tags cadastradas
 *    E -> diagnostico dos fins de curso (nivel bruto de cada pino)
 *
 *  As tags ficam na NVS e sobrevivem ao desligamento. Tags GENERICAS: o
 *  historico registra porta, UID e horario, nao a pessoa.
 *
 *  Bibliotecas: "MFRC522" (miguelbalboa). "Preferences" vem com o core ESP32.
 *  Compilar:
 *    arduino-cli compile --fqbn esp32:esp32:esp32 porta/porta_nr12
 *
 *  PARADA SO POR SOFTWARE (decisao de prototipo), e porta SEM trava. Em producao, uma auditoria
 *  NR-12 exige o fim de curso cortando fisicamente a energia dos motores (rele
 *  de seguranca). Ver porta/README.md.
 * =====================================================================
 */

#define MFRC522_SPICLOCK 1000000u   // SPI a 1 MHz: mais tolerante a jumpers longos
#include <SPI.h>
#include <MFRC522.h>
#include <Preferences.h>
#include <stdarg.h>

// A voz de MAQUINA inteira - enquadramento, relogio, scanner de JSON, ACK e
// `cmd_id` - vem do header, identico ao das outras tres placas, e pela mesma
// porta delas: a USB.
#define APSEN_SUBSISTEMA "porta"
#include "apsen_serial.h"

// ---------------------------------------------------------------------
// PINOS
// ---------------------------------------------------------------------
#define PIN_SS       23   // pinagem VALIDADA na bancada
#define PIN_RST      15
#define PIN_SCK      22
#define PIN_MISO     19
#define PIN_MOSI     21
#define PIN_ENDSTOP1 4    // ES1 = TETO
// ES2 = FRENTE. Constante UNICA de proposito: se a gravacao pela USB falhar,
// mude para 33 (D2 e pino de boot) e tire o resistor de 10k.
#define PIN_ENDSTOP2 2
// "Pode abrir": aceso SO em LIBERADA. Sem fechadura eletrica, e ele que diz a
// quem passou a tag que a maquina ja parou.
#define PIN_LED_OK   25
// GPIO16/17 ficaram LIVRES: eram a UART com a placa das telas, que saiu.

// ---------------------------------------------------------------------
// TEMPOS
// ---------------------------------------------------------------------
// Janela padrao da autorizacao, se o `porta_liberar` nao trouxer `janela_ms`.
const unsigned long JANELA_LIBERACAO_MS = 10000;
// O que se aceita de `janela_ms`: o central e quem decide, mas um numero
// absurdo vindo do fio nao pode deixar a abertura autorizada por uma hora.
const unsigned long JANELA_MIN_MS = 1000;
const unsigned long JANELA_MAX_MS = 60000;
// Quanto se espera a resposta do central a um `pedido_acesso`. TEM DE SER
// MAIOR que `PORTA_PARADA_MAX_S` do central (teto de 4 s) mais o trajeto: o
// central so autoriza depois de a maquina confirmar a parada, e uma espera
// menor faria a porta desistir de um pedido que o central ia aceitar. E um
// LIMITE SUPERIOR, como o `CNC_TETO_TRAJETO_S`, e nao um numero a manter igual.
const unsigned long ESPERA_CENTRAL_MS = 5000;
// O heartbeat. O porta-adapter declara o link perdido depois de
// `PORTA_HEARTBEAT_MAX_S` (2,5 s) sem ele: cinco heartbeats.
const unsigned long HEARTBEAT_MS = 500;
const unsigned long DEBOUNCE_MS  = 30;
const int MAX_TAGS               = 20;

// UID: ate 10 bytes no ISO 14443, ou seja 20 digitos hex.
#define MAX_UID_LEN 24
#define MAX_MOTIVO_LEN 64

MFRC522 rfid(PIN_SS, PIN_RST);
Preferences prefs;

// ---------- lista de tags autorizadas (carregada da flash) ----------
String tags[MAX_TAGS];
int nTags = 0;

// ---------- estados da porta ----------
// AGUARDANDO_CENTRAL e o estado novo: a tag e valida, o pedido saiu, e a
// abertura NAO esta autorizada ate o central dizer que a maquina parou.
enum Estado { FECHADA, AGUARDANDO_CENTRAL, LIBERADA, ABERTA_AUTORIZADA, ABERTA_SEM_AUTORIZACAO };
Estado estado = FECHADA;
bool aguardandoRearme = false;
unsigned long tPedido = 0;      // quando o pedido_acesso saiu
unsigned long tLiberacao = 0;   // quando a abertura foi autorizada
unsigned long janelaMs = JANELA_LIBERACAO_MS;
unsigned long ultimoHeartbeatMs = 0;

// ---------- estados do modo registro ----------
enum ModoRegistro { REG_DESLIGADO, REG_AGUARDANDO_TAG, REG_AGUARDANDO_CONFIRMACAO };
ModoRegistro modoReg = REG_DESLIGADO;
String uidPendente = "";

// ---------------------------------------------------------------------
// LOG  (a voz do humano, na USB; `apsen_serial.h` exige esta assinatura)
// ---------------------------------------------------------------------
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

// ---------- END-STOP 1 (D4, TETO) - com debounce ----------
// Pressionado = LOW (COM no GND, NO no pino, INPUT_PULLUP)
bool es1Fechado = true;
bool es1UltimaLeitura = true;
unsigned long es1TMudanca = 0;

bool lerEndStop1() {
  bool leitura = (digitalRead(PIN_ENDSTOP1) == LOW);
  if (leitura != es1UltimaLeitura) { es1UltimaLeitura = leitura; es1TMudanca = millis(); }
  if (millis() - es1TMudanca > DEBOUNCE_MS) es1Fechado = leitura;
  return es1Fechado;
}

// ---------- END-STOP 2 (D2, FRENTE) - com debounce (copia exata do 1) ----------
bool es2Fechado = true;
bool es2UltimaLeitura = true;
unsigned long es2TMudanca = 0;

bool lerEndStop2() {
  bool leitura = (digitalRead(PIN_ENDSTOP2) == LOW);
  if (leitura != es2UltimaLeitura) { es2UltimaLeitura = leitura; es2TMudanca = millis(); }
  if (millis() - es2TMudanca > DEBOUNCE_MS) es2Fechado = leitura;
  return es2Fechado;
}

void iniciarEndStops() {
  pinMode(PIN_ENDSTOP1, INPUT_PULLUP);
  pinMode(PIN_ENDSTOP2, INPUT_PULLUP);
  delay(20);
  es1Fechado = es1UltimaLeitura = (digitalRead(PIN_ENDSTOP1) == LOW);
  es2Fechado = es2UltimaLeitura = (digitalRead(PIN_ENDSTOP2) == LOW);
}

// Mostra qual sensor abriu, ex.: "   ES1(D4)=FECHADO  ES2(D2)=ABERTO"
void imprimirSensores() {
  Serial.print("   ES1(D4,teto)="); Serial.print(es1Fechado ? "FECHADO" : "ABERTO");
  Serial.print("  ES2(D2,frente)=");  Serial.println(es2Fechado ? "FECHADO" : "ABERTO");
}

// Diagnostico (comando 'E'): nivel eletrico bruto de cada pino
void diagnosticoEndStops() {
  int n1 = digitalRead(PIN_ENDSTOP1);
  int n2 = digitalRead(PIN_ENDSTOP2);
  Serial.println("--- Diagnostico end-stops (pressionado deve dar LOW) ---");
  Serial.print("  ES1(D4,teto): pino="); Serial.print(n1 == HIGH ? "HIGH" : "LOW ");
  Serial.print("  -> "); Serial.println(n1 == LOW ? "FECHADO" : "ABERTO");
  Serial.print("  ES2(D2,frente): pino="); Serial.print(n2 == HIGH ? "HIGH" : "LOW ");
  Serial.print("  -> "); Serial.println(n2 == LOW ? "FECHADO" : "ABERTO");
}

// ---------- NFC (nao bloqueante) ----------
String lerUID() {
  if (!rfid.PICC_IsNewCardPresent()) return "";

  // Ate 3 tentativas: se a primeira leitura falhar, "acorda" a tag e tenta de novo
  bool ok = false;
  for (int tentativa = 0; tentativa < 3 && !ok; tentativa++) {
    if (tentativa > 0) {
      byte atqa[2]; byte tam = sizeof(atqa);
      delay(5);
      if (rfid.PICC_WakeupA(atqa, &tam) != MFRC522::STATUS_OK) continue;
    }
    ok = (rfid.PICC_Select(&rfid.uid, 0) == MFRC522::STATUS_OK);
  }
  if (!ok) return "";

  // Hex MAIUSCULO por formatacao, o mesmo texto que o sketch de bancada
  // imprimia - e as tags ja gravadas na NVS estao nesse formato.
  String uid;
  char par[3];
  for (byte i = 0; i < rfid.uid.size; i++) {
    snprintf(par, sizeof par, "%02X", rfid.uid.uidByte[i]);
    uid += par;
  }
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  return uid;
}

// ---------- armazenamento das tags (NVS) ----------
void carregarTags() {
  prefs.begin("tags", true);                 // somente leitura
  nTags = prefs.getInt("n", 0);
  if (nTags > MAX_TAGS) nTags = MAX_TAGS;
  for (int i = 0; i < nTags; i++) {
    tags[i] = prefs.getString(("u" + String(i)).c_str(), "");
  }
  prefs.end();
}

bool salvarTag(const String& uid) {
  if (nTags >= MAX_TAGS) return false;
  prefs.begin("tags", false);                // leitura/escrita
  prefs.putString(("u" + String(nTags)).c_str(), uid);
  prefs.putInt("n", nTags + 1);
  prefs.end();
  tags[nTags++] = uid;
  return true;
}

bool autorizado(const String& uid) {
  for (int i = 0; i < nTags; i++) if (uid.equals(tags[i])) return true;
  return false;
}

void listarTags() {
  Serial.print("Tags cadastradas ("); Serial.print(nTags);
  Serial.print("/"); Serial.print(MAX_TAGS); Serial.println("):");
  if (nTags == 0) Serial.println("  (nenhuma)");
  for (int i = 0; i < nTags; i++) {
    Serial.print("  "); Serial.print(i + 1); Serial.print(") "); Serial.println(tags[i]);
  }
}

// ---------------------------------------------------------------------
// LED "PODE ABRIR"
// ---------------------------------------------------------------------
// Nao ha fechadura: o LED e o unico sinal de que a abertura esta autorizada.
// Ele segue o ESTADO, e nao um flag a parte — aceso so em LIBERADA, conferido
// a cada volta do laco. Um caminho novo que esquecesse de apaga-lo diria a
// alguem para abrir com a maquina andando.
void atualizarLed() {
  digitalWrite(PIN_LED_OK, estado == LIBERADA ? HIGH : LOW);
}

// =====================================================================
// EVENTOS (placa -> adapter, pela USB). `docs/PROTOCOLO_SERIAL.md` §9.
// =====================================================================
// Todo evento leva "origem":"porta". Com a USB propria ele e redundante - a
// COM ja diz de quem e a linha -, e fica: o central filtra por ele, e tira-lo
// mudaria firmware e rota juntos por ganho nenhum.

static const char* nomeEstado(Estado e) {
  // Vocabulario FECHADO: o central e o painel comparam por igualdade.
  switch (e) {
    case FECHADA:                return "FECHADA";
    case AGUARDANDO_CENTRAL:     return "AGUARDANDO_CENTRAL";
    case LIBERADA:               return "LIBERADA";
    case ABERTA_AUTORIZADA:      return "ABERTA_AUTORIZADA";
    case ABERTA_SEM_AUTORIZACAO: return "ABERTA_SEM_AUTORIZACAO";
  }
  return "ABERTA_SEM_AUTORIZACAO";   // desconhecido e tratado como o pior caso
}

static const char* nomePorta(bool fechada) { return fechada ? "fechada" : "aberta"; }

static void emitEstado() {
  char ts[24]; tsAgora(ts, sizeof ts);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"origem\":\"porta\",\"tipo\":\"estado\",\"teto\":\"%s\","
    "\"frente\":\"%s\",\"estado\":\"%s\",\"aguardando_rearme\":%s,"
    "\"ts\":\"%s\"}}",
    nomePorta(es1Fechado), nomePorta(es2Fechado), nomeEstado(estado),
    aguardandoRearme ? "true" : "false", ts),
    "estado");
  ultimoHeartbeatMs = millis();
}

// pedido_acesso, tag_negada e rearme: os tres levam so o UID.
static void emitComUid(const char* tipo, const String& uid) {
  char ts[24]; tsAgora(ts, sizeof ts);
  char u[MAX_UID_LEN + 1];
  copiarCampo(u, sizeof u, uid.c_str());
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"origem\":\"porta\",\"tipo\":\"%s\",\"uid\":\"%s\",\"ts\":\"%s\"}}",
    tipo, u, ts), tipo);
}

static void emitLiberada(unsigned long janela) {
  char ts[24]; tsAgora(ts, sizeof ts);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"origem\":\"porta\",\"tipo\":\"liberada\",\"janela_ms\":%lu,"
    "\"ts\":\"%s\"}}", janela, ts), "liberada");
}

static void emitLiberacaoExpirada(const char* motivo) {
  char ts[24]; tsAgora(ts, sizeof ts);
  char m[MAX_MOTIVO_LEN];
  copiarCampo(m, sizeof m, motivo);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"origem\":\"porta\",\"tipo\":\"liberacao_expirada\",\"motivo\":\"%s\","
    "\"ts\":\"%s\"}}", m, ts), "liberacao_expirada");
}

static void emitPortaAberta(const char* porta, bool autorizada) {
  char ts[24]; tsAgora(ts, sizeof ts);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"origem\":\"porta\",\"tipo\":\"porta_aberta\",\"porta\":\"%s\","
    "\"autorizada\":%s,\"ts\":\"%s\"}}",
    porta, autorizada ? "true" : "false", ts), "porta_aberta");
}

static void emitPortaFechada() {
  char ts[24]; tsAgora(ts, sizeof ts);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"origem\":\"porta\",\"tipo\":\"porta_fechada\",\"ts\":\"%s\"}}", ts),
    "porta_fechada");
}

static void emitBoot() {
  char ts[24]; tsAgora(ts, sizeof ts);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"origem\":\"porta\",\"tipo\":\"boot\",\"teto\":\"%s\",\"frente\":\"%s\","
    "\"ts\":\"%s\"}}", nomePorta(es1Fechado), nomePorta(es2Fechado), ts), "boot");
}

// Toda mudanca de estado passa por aqui: o `estado` sai NA HORA, alem do
// heartbeat. Quem decide parar a maquina e o central, e 500 ms de atraso numa
// porta que acabou de abrir e meio segundo de CNC andando com alguem dentro.
static void mudarEstado(Estado novo) {
  if (novo != estado)
    logMsg("PORTA", "%s -> %s", nomeEstado(estado), nomeEstado(novo));
  estado = novo;
  atualizarLed();
  emitEstado();
}

// =====================================================================
// COMANDOS DO CENTRAL (pela USB, atraves do porta-adapter)
// =====================================================================

static void cmdPortaLiberar(const char* linha, long cmd_id) {
  // So existe liberacao para um pedido PENDENTE. Autorizar uma porta que nao
  // pediu nada seria acender o "pode abrir" por um comando atrasado - um
  // `porta_liberar` que chegou depois de esta placa ter desistido do pedido.
  if (estado != AGUARDANDO_CENTRAL) {
    ackErro(cmd_id, "sem_pedido_pendente");
    logMsg("PORTA", "porta_liberar recusado: estado %s", nomeEstado(estado));
    return;
  }
  float fjanela = 0;
  unsigned long janela = JANELA_LIBERACAO_MS;
  if (jsonNumero(linha, "janela_ms", &fjanela) && fjanela > 0) janela = (unsigned long)fjanela;
  if (janela < JANELA_MIN_MS) janela = JANELA_MIN_MS;
  if (janela > JANELA_MAX_MS) janela = JANELA_MAX_MS;

  ackOk(cmd_id, false);
  janelaMs   = janela;
  tLiberacao = millis();
  logMsg("PORTA", "LIBERADA pelo central por %lu ms - pode abrir.", janela);
  emitLiberada(janela);
  mudarEstado(LIBERADA);       // acende o LED "pode abrir"
}

static void cmdPortaNegar(const char* linha, long cmd_id) {
  char motivo[MAX_MOTIVO_LEN];
  if (!jsonTexto(linha, "motivo", motivo, sizeof motivo) || !motivo[0])
    strncpy(motivo, "negado_pelo_central", sizeof motivo);
  motivo[sizeof motivo - 1] = '\0';

  if (estado != AGUARDANDO_CENTRAL && estado != LIBERADA) {
    ackErro(cmd_id, "sem_pedido_pendente");
    return;
  }
  ackOk(cmd_id, false);
  // Volta a FECHADA SEM rearme pendente: o acesso nao aconteceu, entao nao ha
  // o que rearmar. A proxima tag e um pedido novo.
  logMsg("PORTA", "acesso NEGADO pelo central: %s", motivo);
  mudarEstado(FECHADA);
  emitLiberacaoExpirada(motivo);
}

static void cmdPortaEstado(long cmd_id) {
  ackOk(cmd_id, false);
  emitEstado();
}

// O contrato que `apsen_serial.h` pede do sketch: quais comandos existem.
void executarComando(const char* cmd, const char* linha, long cmd_id) {
  if      (strcmp(cmd, "porta_liberar") == 0) cmdPortaLiberar(linha, cmd_id);
  else if (strcmp(cmd, "porta_negar")   == 0) cmdPortaNegar(linha, cmd_id);
  else if (strcmp(cmd, "porta_estado")  == 0) cmdPortaEstado(cmd_id);
  else {
    // Fora do contrato: ACK NEGATIVO, nunca silencio.
    ackErro(cmd_id, "comando desconhecido");
    logMsg("APSEN", "comando desconhecido: '%s'", cmd);
  }
}

// =====================================================================
// A VOZ DO HUMANO - a linha SEM chave, na mesma USB
// =====================================================================
static inline char maiuscula(char c) { return (c >= 'a' && c <= 'z') ? (char)(c - 32) : c; }

void sairModoRegistro() {
  modoReg = REG_DESLIGADO;
  uidPendente = "";
  logMsg("REG", "Saiu do modo registro. Operacao normal.");
}

static void usoBancada() {
  Serial.println(">> Use: R (registrar) | S (salvar) | C (cancelar) | L (listar) | E (end-stops)");
}

// Um comando de bancada, ja em maiuscula. So `linhaHumana` chama isto: ler a
// USB por conta propria seria um segundo leitor da MESMA linha que o
// `serialPoll()` do header le, e cada um ficaria com metade dos bytes de um
// JSON - as duas metades sumiriam em silencio.
static void comandoBancada(char c) {
  switch (c) {
    case 'R':
      if (modoReg != REG_DESLIGADO) {
        Serial.println(">> Ja esta no modo registro. Envie 'C' para cancelar.");
      } else {
        modoReg = REG_AGUARDANDO_TAG;
        logMsg("REG", "MODO REGISTRO: aproxime a tag do leitor ('C' cancela).");
      }
      break;

    case 'S':
      if (modoReg == REG_AGUARDANDO_CONFIRMACAO) {
        if (salvarTag(uidPendente)) {
          logMsg("REG", "Tag %s salva com sucesso.", uidPendente.c_str());
        } else {
          logMsg("REG", "Memoria cheia: limite de tags atingido (%s nao salva).",
                 uidPendente.c_str());
        }
        sairModoRegistro();
      } else {
        Serial.println(">> 'S' so funciona apos ler uma tag no modo registro.");
      }
      break;

    case 'C':
      if (modoReg != REG_DESLIGADO) {
        logMsg("REG", "Registro cancelado. Nenhuma tag foi salva.");
        sairModoRegistro();
      } else {
        Serial.println(">> Nada para cancelar.");
      }
      break;

    case 'L':
      listarTags();
      break;

    case 'E':
      diagnosticoEndStops();
      break;

    default:
      Serial.print(">> Comando desconhecido: "); Serial.println(c);
      usoBancada();
      break;
  }
}

// A linha SEM chave, que o header separa da voz de maquina na linha CRUA (ver
// `despacharLinha`). Uma letra por linha: espaco, tab, \r e \n nao contam, e a
// letra vale em maiuscula ou minuscula - "R", "s" e " L " sao os comandos que
// parecem. Mais de uma letra NAO vira uma sequencia de comandos: um "RS"
// digitado de uma vez entraria no modo registro e tentaria salvar uma tag que
// ninguem leu.
void linhaHumana(char* linha) {
  char letra = 0;
  int letras = 0;
  for (const char* p = linha; *p; p++) {
    if (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') continue;
    letra = maiuscula(*p);
    letras++;
  }
  if (letras == 0) return;          // linha em branco: nada a fazer
  if (letras > 1) {
    Serial.print(">> Uma letra por linha. Recebido: "); Serial.println(linha);
    usoBancada();
    return;
  }
  comandoBancada(letra);
}

// Tag lida durante o modo registro: vai para o cadastro e NAO abre nada.
void tratarTagRegistro(const String& uid) {
  if (modoReg != REG_AGUARDANDO_TAG) return;   // ja tem uma tag pendente: ignora outras

  if (autorizado(uid)) {
    logMsg("REG", "A tag %s ja esta cadastrada.", uid.c_str());
    sairModoRegistro();
    return;
  }

  uidPendente = uid;
  modoReg = REG_AGUARDANDO_CONFIRMACAO;
  Serial.println("--------------------------------------");
  Serial.print  ("Tag detectada: UID = "); Serial.println(uid);
  Serial.println("Envie 'S' para SALVAR ou 'C' para CANCELAR.");
  Serial.println("--------------------------------------");
}

// =====================================================================
// A MAQUINA DE ESTADOS
// =====================================================================

// As portas que ABRIRAM nesta volta do laco, uma a uma. `porta_aberta` sai
// UMA vez por porta, na transicao - inclusive a segunda, quando ela abre com
// a primeira ja aberta.
static void tratarAberturas(bool tetoAntes, bool frenteAntes) {
  const bool abriuTeto   = tetoAntes   && !es1Fechado;
  const bool abriuFrente = frenteAntes && !es2Fechado;
  if (!abriuTeto && !abriuFrente) return;

  // Autorizada so se a abertura veio de uma liberacao do central (ou e a
  // segunda porta de uma abertura que ja era autorizada). Em qualquer outro
  // estado - inclusive com rearme pendente - e abertura SEM autorizacao.
  const bool autorizada = (estado == LIBERADA || estado == ABERTA_AUTORIZADA);
  if (abriuTeto)   emitPortaAberta("teto", autorizada);
  if (abriuFrente) emitPortaAberta("frente", autorizada);

  if (autorizada) {
    logMsg("PORTA", "ALERTA: MAQUINA PAUSADA - PORTA ABERTA.");
    mudarEstado(ABERTA_AUTORIZADA);     // apaga o LED: a autorizacao foi usada
  } else {
    logMsg("PORTA", "PORTA ABERTA SEM AUTORIZACAO! Parada de emergencia.");
    mudarEstado(ABERTA_SEM_AUTORIZACAO);
  }
  imprimirSensores();
}

static void tratarTag(const String& uid) {
  if (!autorizado(uid)) {
    logMsg("PORTA", "Tag %s nao cadastrada. Envie 'R' na USB para registrar.", uid.c_str());
    emitComUid("tag_negada", uid);
    return;
  }
  if (estado != FECHADA) {
    // Tag no meio de um pedido, de uma liberacao ou com porta aberta: nao ha
    // decisao a pedir. Logada, para a bancada entender por que nada mudou.
    logMsg("PORTA", "Tag %s ignorada no estado %s.", uid.c_str(), nomeEstado(estado));
    return;
  }
  if (aguardandoRearme) {
    // Rearme SO por tag, e so com as duas portas fechadas (FECHADA garante).
    // Depois dele a proxima tag ja e um pedido normal.
    aguardandoRearme = false;
    logMsg("PORTA", "Rearme por tag %s. O central decide se a maquina retoma.", uid.c_str());
    emitComUid("rearme", uid);
    mudarEstado(FECHADA);
    return;
  }
  // Pedido de acesso: a porta NAO abre aqui. Quem abre e o central, depois de
  // parar a maquina.
  tPedido = millis();
  logMsg("PORTA", "Pedido de acesso (tag %s) - aguardando o central parar a maquina.",
         uid.c_str());
  emitComUid("pedido_acesso", uid);
  mudarEstado(AGUARDANDO_CENTRAL);
}

static void tratarPrazos(bool fechada) {
  if (estado == AGUARDANDO_CENTRAL && millis() - tPedido > ESPERA_CENTRAL_MS) {
    // Sem resposta do central, a porta NAO abre. Volta a FECHADA sem rearme:
    // o acesso nao aconteceu.
    logMsg("PORTA", "Central nao respondeu em %lu ms - pedido descartado, abertura NAO autorizada.",
           ESPERA_CENTRAL_MS);
    mudarEstado(FECHADA);
    emitLiberacaoExpirada("central_nao_respondeu");
    return;
  }
  if (estado == LIBERADA && fechada && millis() - tLiberacao > janelaMs) {
    // A maquina ja parou ao receber o pedido; so retoma com tag.
    aguardandoRearme = true;
    logMsg("PORTA", "Porta nao foi aberta. Passe a tag para rearmar.");
    mudarEstado(FECHADA);
    emitLiberacaoExpirada("janela_expirou");
    return;
  }
  if ((estado == ABERTA_AUTORIZADA || estado == ABERTA_SEM_AUTORIZACAO) && fechada) {
    // NR-12: fechar NAO religa sozinho. As DUAS fechadas, e o rearme e por tag.
    aguardandoRearme = true;
    logMsg("PORTA", "Porta fechada. Aguardando rearme (passe a tag).");
    emitPortaFechada();
    mudarEstado(FECHADA);
  }
}

// =====================================================================
void setup() {
  // O LED "pode abrir" nasce APAGADO, antes de qualquer outra linha: um boot
  // (queda de energia, reset) nunca pode dizer a alguem que pode abrir.
  pinMode(PIN_LED_OK, OUTPUT);
  digitalWrite(PIN_LED_OK, LOW);

  // Buffer de recepcao da USB do tamanho do teto da linha, ANTES do begin.
  apsenSerialInit();
  Serial.begin(115200);

  iniciarEndStops();
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_SS);
  rfid.PCD_Init();
  rfid.PCD_SetAntennaGain(rfid.RxGain_33dB);  // ganho padrao (o maximo amplifica ruido)
  carregarTags();

  delay(50);
  Serial.println();
  Serial.println("=== Porta NR-12 | ESP32 + RC522 ===");
  Serial.println("Bancada (esta USB, com o porta-adapter parado):");
  usoBancada();
  Serial.println("Maquina: linhas JSON nesta mesma USB, com o porta-adapter.");
  listarTags();

  // A primeira linha de maquina: o ping. Quem o responde e o porta-adapter.
  emitPing();
  emitBoot();
  if (es1Fechado && es2Fechado) {
    estado = FECHADA;
    logMsg("BOOT", "Porta fechada. Sistema armado.");
  } else {
    estado = ABERTA_SEM_AUTORIZACAO;
    logMsg("BOOT", "Porta aberta no boot! Maquina bloqueada.");
  }
  imprimirSensores();
  emitEstado();
}

void loop() {
  serialPoll();     // as duas vozes da USB, pelo header (maquina e bancada)
  pingPoll();

  // Os 2 fins de curso sao monitorados SEMPRE, mesmo no modo registro.
  const bool tetoAntes = es1Fechado, frenteAntes = es2Fechado;
  lerEndStop1();
  lerEndStop2();
  const bool fechada = es1Fechado && es2Fechado;
  tratarAberturas(tetoAntes, frenteAntes);

  String uid = lerUID();
  if (uid.length() && modoReg != REG_DESLIGADO) {
    tratarTagRegistro(uid);   // no modo registro a tag vai para o cadastro
    uid = "";
  }
  if (uid.length()) tratarTag(uid);

  tratarPrazos(fechada);
  atualizarLed();

  if (millis() - ultimoHeartbeatMs >= HEARTBEAT_MS) emitEstado();
  delay(2);
}
