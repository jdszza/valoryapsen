/*
 * TESTE DE BANCADA DA PORTA NR-12 - NFC (RC522) + FINS DE CURSO + LOGICA DA PORTA
 * ============================================================================
 * Mantem as ferramentas de bancada (teste guiado, nivel dos pinos, versao do
 * RC522) e agora inclui a logica completa do porta_nr12.ino:
 *   - estados da porta (FECHADA / LIBERADA / ABERTA_AUTORIZADA / ABERTA_SEM_AUTORIZACAO)
 *   - tags cadastradas na flash (NVS), modo registro (R / S / C / L)
 *   - mensagens "EVT:..." identicas as do porta_nr12.ino
 *
 * SAIDA DAS MENSAGENS
 *   Toda mensagem sai pelo Serial Monitor (115200) E pelo Serial2 (RX2/TX2)
 *   para o segundo ESP32. A cada mensagem enviada, o LED azul embutido da
 *   placa (GPIO2) acende por ~200 ms.
 *
 * LIGACOES
 *   RC522: SS=23  SCK=22  MOSI=21  MISO=19  RST=15  (3V3!)
 *   ES1 = GPIO4   ES2 = GPIO18   (COM -> GND, INPUT_PULLUP)
 *   Serial2: TX2 = GPIO17 -> RX do outro ESP32
 *            RX2 = GPIO16 <- TX do outro ESP32   (GND comum entre as placas!)
 *   LED embutido = GPIO2 | LED_OK = GPIO25 | Buzzer (opcional) = GPIO26
 *
 * COMANDOS (um caractere)
 *   Porta:    R registrar | S salvar | C cancelar | L listar tags
 *   Bancada:  1 (so ES1) | 2 (so ES2) | A (ambos) | M (liga/desliga log bruto dos ES)
 *             G (teste guiado) | P (nivel dos pinos agora) | Z (zerar contadores)
 *             N (NFC on/off) | V (chip RC522) | H (ajuda)
 *
 * Premissa: porta FECHADA = alavanca APERTADA (ES_NIVEL_FECHADO).
 */

#define MFRC522_SPICLOCK 1000000u   // SPI a 1 MHz, como na porta
#include <SPI.h>
#include <MFRC522.h>
#include <Preferences.h>

// ---------------------------------------------------------------- PINOS
#define PIN_SS        23
#define PIN_RST       15
#define PIN_SCK       22
#define PIN_MISO      19
#define PIN_MOSI      21
#define PIN_ES1       4
#define PIN_ES2       18
#define PIN_LED_OK    25
#define PIN_BUZZER    26    // opcional (o D4 agora e o ES1)
#define PIN_LED_PLACA 2     // LED azul embutido do ESP32 DevKit

#define SERIAL2_RX    16
#define SERIAL2_TX    17
#define SERIAL2_BAUD  115200

// Nivel lido quando a alavanca esta APERTADA (porta fechada). Ajuste com o teste guiado (G).
#define ES_NIVEL_FECHADO LOW

const unsigned long JANELA_LIBERACAO_MS = 10000;  // tempo p/ abrir apos passar a tag
const unsigned long DEBOUNCE_MS         = 30;
const unsigned long PULSO_LED_MS        = 200;    // tempo do LED embutido aceso a cada envio
const int MAX_TAGS                      = 20;

MFRC522 rfid(PIN_SS, PIN_RST);
Preferences prefs;

// Definido aqui no topo (antes de qualquer funcao) para o Arduino gerar os
// prototipos corretamente.
struct EndStop {
  uint8_t pino;
  const char* nome;
  bool fechadoEstavel;
  bool ultimaLeitura;
  unsigned long tMudanca;
};

// ---------------------------------------------------------------- SAIDA (Serial + Serial2 + LED)
static bool          ledPlacaAceso = false;
static unsigned long tLedApagar    = 0;

static void pulsoLedPlaca() {
  digitalWrite(PIN_LED_PLACA, HIGH);
  ledPlacaAceso = true;
  tLedApagar = millis() + PULSO_LED_MS;
}

static void atualizarLedPlaca() {
  if (ledPlacaAceso && (long)(millis() - tLedApagar) >= 0) {
    digitalWrite(PIN_LED_PLACA, LOW);
    ledPlacaAceso = false;
  }
}

// Envia uma linha para o Serial Monitor e para o segundo ESP32; acende o LED da placa.
static void saida(const String& linha) {
  Serial.println(linha);
  Serial2.println(linha);
  pulsoLedPlaca();
}

static void evento(const char* codigo, const char* texto, const String& uid = "") {
  String linha = "EVT:";
  linha += codigo;
  if (uid.length()) { linha += ";UID="; linha += uid; }
  linha += " | ";
  linha += texto;
  saida(linha);
}

static void bip(int n, int ms) {
  for (int i = 0; i < n; i++) {
    digitalWrite(PIN_BUZZER, HIGH); delay(ms);
    digitalWrite(PIN_BUZZER, LOW);  delay(ms);
  }
}

// ---------------------------------------------------------------- TAGS (NVS)
String tags[MAX_TAGS];
int nTags = 0;

static void carregarTags() {
  prefs.begin("tags", true);
  nTags = prefs.getInt("n", 0);
  if (nTags > MAX_TAGS) nTags = MAX_TAGS;
  for (int i = 0; i < nTags; i++) {
    tags[i] = prefs.getString(("u" + String(i)).c_str(), "");
  }
  prefs.end();
}

static bool salvarTag(const String& uid) {
  if (nTags >= MAX_TAGS) return false;
  prefs.begin("tags", false);
  prefs.putString(("u" + String(nTags)).c_str(), uid);
  prefs.putInt("n", nTags + 1);
  prefs.end();
  tags[nTags++] = uid;
  return true;
}

static bool autorizado(const String& uid) {
  for (int i = 0; i < nTags; i++) if (uid.equals(tags[i])) return true;
  return false;
}

static void listarTags() {
  Serial.print("Tags cadastradas ("); Serial.print(nTags);
  Serial.print("/"); Serial.print(MAX_TAGS); Serial.println("):");
  if (nTags == 0) Serial.println("  (nenhuma)");
  for (int i = 0; i < nTags; i++) {
    Serial.print("  "); Serial.print(i + 1); Serial.print(") "); Serial.println(tags[i]);
  }
}

// ---------------------------------------------------------------- ESTADOS
enum Estado { FECHADA, LIBERADA, ABERTA_AUTORIZADA, ABERTA_SEM_AUTORIZACAO };
Estado estado = FECHADA;
bool aguardandoRearme = false;
unsigned long tLiberacao = 0;

enum ModoRegistro { REG_DESLIGADO, REG_AGUARDANDO_TAG, REG_AGUARDANDO_CONFIRMACAO };
ModoRegistro modoReg = REG_DESLIGADO;
String uidPendente = "";

// ---------------------------------------------------------------- FINS DE CURSO (com debounce)
EndStop endStops[] = {
  { PIN_ES1, "ES1(D4)",  true, true, 0 },
  { PIN_ES2, "ES2(D18)", true, true, 0 }
};
const int N_ENDSTOPS = sizeof(endStops) / sizeof(endStops[0]);

static bool lerEndStop(EndStop& e) {
  bool leitura = (digitalRead(e.pino) == ES_NIVEL_FECHADO);
  if (leitura != e.ultimaLeitura) { e.ultimaLeitura = leitura; e.tMudanca = millis(); }
  if (millis() - e.tMudanca > DEBOUNCE_MS) e.fechadoEstavel = leitura;
  return e.fechadoEstavel;
}

// Celula fechada SOMENTE se todos os end-stops estiverem pressionados
static bool lerPortaFechada() {
  bool todosFechados = true;
  for (int i = 0; i < N_ENDSTOPS; i++) {
    if (!lerEndStop(endStops[i])) todosFechados = false;
  }
  return todosFechados;
}

static void iniciarEndStops() {
  for (int i = 0; i < N_ENDSTOPS; i++) pinMode(endStops[i].pino, INPUT_PULLUP);
  delay(20);
  for (int i = 0; i < N_ENDSTOPS; i++) {
    bool f = (digitalRead(endStops[i].pino) == ES_NIVEL_FECHADO);
    endStops[i].fechadoEstavel = endStops[i].ultimaLeitura = f;
  }
}

// Ex.: "   ES1(D4)=FECHADO  ES2(D18)=ABERTO"
static void imprimirSensores() {
  String linha = "   ";
  for (int i = 0; i < N_ENDSTOPS; i++) {
    linha += endStops[i].nome;
    linha += "=";
    linha += endStops[i].fechadoEstavel ? "FECHADO  " : "ABERTO  ";
  }
  saida(linha);
}

// ---------------------------------------------------------------- NFC (nao bloqueante)
static bool nfcAtivo = true;

static void versaoRC522() {
  byte v = rfid.PCD_ReadRegister(MFRC522::VersionReg);
  Serial.printf("RC522 VersionReg = 0x%02X  -> ", v);
  if (v == 0x91 || v == 0x92)      Serial.println("OK, o chip responde no SPI.");
  else if (v == 0x00 || v == 0xFF) Serial.println("SEM RESPOSTA. Confira SS/SCK/MOSI/MISO/RST, GND e a alimentacao em 3V3.");
  else                             Serial.println("valor incomum (clone?). Se ler tag, funciona.");
}

static String lerUID() {
  if (!rfid.PICC_IsNewCardPresent()) return "";
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

// ---------------------------------------------------------------- FERRAMENTAS DE BANCADA
static bool          vigia[2]    = { true, true };
static int           ultimo[2]   = { -1, -1 };
static unsigned long mudancas[2] = { 0, 0 };
static bool          logBruto    = false;   // log bruto das mudancas dos pinos (so no Serial local)
static const uint8_t PINO_ES[2]  = { PIN_ES1, PIN_ES2 };
static const char*   NOME_ES[2]  = { "ES1 (D4, teto)", "ES2 (D18, frente)" };

static void ajuda() {
  Serial.println();
  Serial.println("Porta:   R (registrar) | S (salvar) | C (cancelar) | L (listar tags)");
  Serial.println("Bancada: 1 (so ES1) | 2 (so ES2) | A (ambos) | M (log bruto on/off) | G (teste guiado)");
  Serial.println("         P (nivel dos pinos) | Z (zerar contadores) | N (NFC on/off) | V (chip RC522) | H (ajuda)");
}

// Le o pino por 100 ms. Devolve 0/1 se estavel, -1 se oscilou.
static int lerEstavel(uint8_t pino) {
  int primeiro = digitalRead(pino);
  for (int i = 0; i < 20; i++) {
    delay(5);
    if (digitalRead(pino) != primeiro) return -1;
  }
  return primeiro;
}

static void testeGuiado(int i) {
  const uint8_t pino = PINO_ES[i];
  Serial.printf("\n--- TESTE GUIADO: %s ---\n", NOME_ES[i]);
  Serial.println("1) SOLTE a alavanca (porta ABERTA). Lendo em 3 s...");
  delay(3000);
  int solto = lerEstavel(pino);
  if (solto < 0) {
    Serial.println("   Leitura OSCILANDO com a alavanca solta: fio solto ou mau contato.");
    return;
  }
  Serial.printf("   solta = %s\n", solto ? "HIGH" : "LOW");

  Serial.println("2) APERTE e SEGURE a alavanca (porta FECHADA). Esperando ate 10 s...");
  int apertado = -1;
  unsigned long t0 = millis();
  while (millis() - t0 < 10000) {
    int v = lerEstavel(pino);
    if (v >= 0 && v != solto) { apertado = v; break; }
  }

  if (apertado < 0) {
    Serial.printf("   RESULTADO: o pino NAO mudou (ficou %s o tempo todo).\n", solto ? "HIGH" : "LOW");
    if (solto == 0)
      Serial.println("   Preso em LOW: curto no GND. Veja se C e NC nao estao ligados direto, "
                     "se o fio do pino nao toca o GND, e se a solda nao fez ponte.");
    else
      Serial.println("   Preso em HIGH: o sinal nao chega ao GND. Veja se o C esta no GND, "
                     "se o fio do pino nao esta solto e se a alavanca alcanca o botao.");
    return;
  }

  Serial.printf("   apertada = %s\n", apertado ? "HIGH" : "LOW");
  Serial.printf("   RESULTADO: a ligacao funciona. Contato %s.\n",
                apertado ? "NC (aberto quando apertado)" : "NO (fechado quando apertado)");
  Serial.printf("   Use:  #define ES_NIVEL_FECHADO %s\n", apertado ? "HIGH" : "LOW");
}

static void relatarMudancas() {
  if (!logBruto) return;
  for (int i = 0; i < 2; i++) {
    if (!vigia[i]) continue;
    int v = digitalRead(PINO_ES[i]);
    if (v != ultimo[i]) {
      if (ultimo[i] != -1) mudancas[i]++;
      Serial.printf("[%7lu ms] %s = %s   (mudancas: %lu)\n",
                    millis(), NOME_ES[i], v ? "HIGH" : "LOW", mudancas[i]);
      ultimo[i] = v;
    }
  }
}

static void mostrarNiveis() {
  for (int i = 0; i < 2; i++)
    Serial.printf("%s = %s\n", NOME_ES[i], digitalRead(PINO_ES[i]) ? "HIGH" : "LOW");
}

// ---------------------------------------------------------------- REGISTRO DE TAGS
static void sairModoRegistro() {
  modoReg = REG_DESLIGADO;
  uidPendente = "";
  evento("REG_FIM", "Saiu do modo registro. Operacao normal.");
}

static void tratarTagRegistro(const String& uid) {
  if (modoReg != REG_AGUARDANDO_TAG) return;

  if (autorizado(uid)) {
    evento("REG_JA_EXISTE", "Essa tag ja esta cadastrada.", uid);
    bip(3, 80);
    sairModoRegistro();
    return;
  }

  uidPendente = uid;
  modoReg = REG_AGUARDANDO_CONFIRMACAO;
  Serial.println("--------------------------------------");
  Serial.print  ("Tag detectada: UID = "); Serial.println(uid);
  Serial.println("Envie 'S' para SALVAR ou 'C' para CANCELAR.");
  Serial.println("--------------------------------------");
  bip(1, 120);
}

// ---------------------------------------------------------------- COMANDOS
static void tratarComandos() {
  while (Serial.available()) {
    char c = toupper(Serial.read());
    if (c == '\r' || c == '\n' || c == ' ') continue;

    switch (c) {
      // ---- comandos da porta ----
      case 'R':
        if (modoReg != REG_DESLIGADO) {
          Serial.println(">> Ja esta no modo registro. Envie 'C' para cancelar.");
        } else {
          modoReg = REG_AGUARDANDO_TAG;
          evento("REG_INICIO", "MODO REGISTRO: aproxime a tag do leitor ('C' cancela).");
          bip(1, 60);
        }
        break;

      case 'S':
        if (modoReg == REG_AGUARDANDO_CONFIRMACAO) {
          if (salvarTag(uidPendente)) {
            evento("REG_SALVA", "Tag salva com sucesso.", uidPendente);
            bip(2, 60);
          } else {
            evento("REG_ERRO", "Memoria cheia: limite de tags atingido.", uidPendente);
            bip(3, 80);
          }
          sairModoRegistro();
        } else {
          Serial.println(">> 'S' so funciona apos ler uma tag no modo registro.");
        }
        break;

      case 'C':
        if (modoReg != REG_DESLIGADO) {
          evento("REG_CANCELADO", "Registro cancelado. Nenhuma tag foi salva.", uidPendente);
          sairModoRegistro();
        } else {
          Serial.println(">> Nada para cancelar.");
        }
        break;

      case 'L': listarTags(); break;

      // ---- comandos de bancada ----
      case '1': vigia[0] = true;  vigia[1] = false; ultimo[0] = ultimo[1] = -1; logBruto = true;
                Serial.println(">> log bruto: SO o ES1"); break;
      case '2': vigia[0] = false; vigia[1] = true;  ultimo[0] = ultimo[1] = -1; logBruto = true;
                Serial.println(">> log bruto: SO o ES2"); break;
      case 'A': vigia[0] = vigia[1] = true;         ultimo[0] = ultimo[1] = -1; logBruto = true;
                Serial.println(">> log bruto: os dois"); break;
      case 'M': logBruto = !logBruto; ultimo[0] = ultimo[1] = -1;
                Serial.printf(">> log bruto %s\n", logBruto ? "LIGADO" : "DESLIGADO"); break;
      case 'G': for (int i = 0; i < 2; i++) if (vigia[i]) testeGuiado(i);
                ultimo[0] = ultimo[1] = -1; break;
      case 'P': mostrarNiveis(); break;
      case 'Z': mudancas[0] = mudancas[1] = 0; Serial.println(">> contadores zerados"); break;
      case 'N': nfcAtivo = !nfcAtivo;
                Serial.printf(">> NFC %s\n", nfcAtivo ? "LIGADO" : "DESLIGADO"); break;
      case 'V': versaoRC522(); break;
      case 'H': ajuda(); break;

      default:
        Serial.print(">> Comando desconhecido: "); Serial.println(c);
        ajuda();
        break;
    }
  }
}

// =====================================================================
void setup() {
  Serial.begin(115200);
  Serial2.begin(SERIAL2_BAUD, SERIAL_8N1, SERIAL2_RX, SERIAL2_TX);

  pinMode(PIN_LED_PLACA, OUTPUT); digitalWrite(PIN_LED_PLACA, LOW);
  pinMode(PIN_LED_OK, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  iniciarEndStops();

  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_SS);
  rfid.PCD_Init();
  rfid.PCD_SetAntennaGain(rfid.RxGain_33dB);
  carregarTags();

  delay(500);
  Serial.println();
  Serial.println("=== TESTE DE BANCADA: Porta NR-12 | ESP32 + RC522 + Serial2 ===");
  versaoRC522();
  ajuda();
  listarTags();

  if (lerPortaFechada()) {
    estado = FECHADA;
    evento("BOOT_OK", "Porta fechada. Sistema armado.");
  } else {
    estado = ABERTA_SEM_AUTORIZACAO;
    evento("ALERTA_SEM_AUTORIZACAO", "Porta aberta no boot! Maquina bloqueada.");
  }
  imprimirSensores();
}

void loop() {
  atualizarLedPlaca();
  tratarComandos();
  relatarMudancas();

  bool fechada = lerPortaFechada();   // os 2 end-stops sao monitorados SEMPRE
  String uid = nfcAtivo ? lerUID() : "";

  // No modo registro, a tag lida vai para o cadastro e NAO libera a porta
  if (uid.length() && modoReg != REG_DESLIGADO) {
    tratarTagRegistro(uid);
    uid = "";
  }

  if (uid.length() && !autorizado(uid)) {
    evento("TAG_NEGADA", "Tag nao cadastrada. Envie 'R' para registrar.", uid);
    bip(3, 80);
    uid = "";
  }

  switch (estado) {

    case FECHADA:
      if (!fechada) {                                   // abriu sem tag
        estado = ABERTA_SEM_AUTORIZACAO;
        evento("ALERTA_SEM_AUTORIZACAO", "PORTA ABERTA SEM AUTORIZACAO! Parada de emergencia.");
        imprimirSensores();
        bip(1, 1000);
      } else if (uid.length()) {
        if (aguardandoRearme) {                         // rearme manual apos fechar
          aguardandoRearme = false;
          evento("REARME_OK", "Rearme confirmado. Maquina pode retomar.", uid);
          bip(2, 60);
        } else {                                        // pedido de acesso
          estado = LIBERADA;
          tLiberacao = millis();
          digitalWrite(PIN_LED_OK, HIGH);
          evento("PAUSA", "Acesso autorizado. Maquina em pausa. Pode abrir.", uid);
          bip(1, 120);
        }
      }
      break;

    case LIBERADA:
      if (!fechada) {
        estado = ABERTA_AUTORIZADA;
        evento("ALERTA_PORTA_ABERTA", "ALERTA: MAQUINA PAUSADA - PORTA ABERTA.");
        imprimirSensores();
      } else if (millis() - tLiberacao > JANELA_LIBERACAO_MS) {
        estado = FECHADA;
        digitalWrite(PIN_LED_OK, LOW);
        aguardandoRearme = true;                        // pausou mas ninguem abriu
        evento("LIBERACAO_EXPIRADA", "Porta nao foi aberta. Passe a tag para rearmar.");
      }
      break;

    case ABERTA_AUTORIZADA:
    case ABERTA_SEM_AUTORIZACAO:
      if (fechada) {
        estado = FECHADA;
        digitalWrite(PIN_LED_OK, LOW);
        aguardandoRearme = true;                        // NR-12: fechar NAO religa sozinho
        evento("PORTA_FECHADA", "Porta fechada. Aguardando rearme (passe a tag).");
      }
      break;
  }

  delay(5);
}
