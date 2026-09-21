/*
 * =====================================================================
 *  TELAS TFT DOS 8 DISPENSERS - `sub: "dispenser_tft"`
 * =====================================================================
 *  A SEGUNDA placa do `dispenser-adapter`, numa SEGUNDA porta. Ela nao
 *  aciona mecanismo nenhum: so PINTA. Acionar os 8 mecanismos, desenhar 8
 *  telas e manter a serial nao cabe num ESP so — por isso sao duas placas,
 *  e o dono das duas e o mesmo adapter, que ja ve todo comando que desce e
 *  todo evento que sobe do slot.
 *
 *  O contrato e `docs/PROTOCOLO_SERIAL.md` §6, e ele e curto de proposito:
 *    PC -> placa  {"cmd":"slot",...}           redesenha a tela de um slot
 *    PC -> placa  {"cmd":"estado_celula",...}  ha trava, e de que slot e
 *    placa -> PC  {"evento":{"tipo":"telemetria",...}}  telas vivas, brilho
 *    placa -> PC  {"evento":{"tipo":"erro",...}}        uma tela nao respondeu
 *
 *  NAO ha evento de resultado para `slot` nem para `estado_celula`: eles so
 *  pintam. O ACK ja disse "aceitei", e nao ha "terminei" a esperar. E os dois
 *  eventos que sobem PARAM NO ADAPTER (log e /health) — o central nao tem
 *  endpoint de tela e nao decide nada com eles.
 *
 *  FALHA DE TELA NUNCA MUDA O CAMINHO DO DISPENSER. Esta placa nao recusa
 *  comando e nao atrasa ACK porque um display nao respondeu: responde o ACK,
 *  pinta o que der, e conta o resto no evento `erro`. Tela errada e
 *  cosmetica; dispensa atrasada nao e.
 *
 *  DUAS VOZES NA MESMA PORTA, E O SEPARADOR E O '{'
 *    Igual a placa dos mecanismos e a balanca: linha COM '{' e mensagem,
 *    linha SEM '{' e log. Aqui a voz do humano e so log — esta placa nao tem
 *    terminal de manutencao, porque nao ha nada nela para calibrar.
 *
 *  ARDUINO IDE
 *    Placa: "ESP32 Dev Module"
 *    Compilar: arduino-cli compile --fqbn esp32:esp32:esp32 dispenser/telas_tft
 * =====================================================================
 */

#include <Arduino.h>
#include <stdarg.h>

// A voz de MAQUINA inteira — enquadramento, relogio, scanner de JSON, ACK e
// `cmd_id` — vem daqui, identica a da placa dos mecanismos. Ver o cabecalho do
// arquivo para por que sao duas copias e qual teste as compara.
#define APSEN_SUBSISTEMA "dispenser_tft"
#include "apsen_serial.h"

// ---------------------------------------------------------------------
// HARDWARE DAS TELAS  —  TUDO AQUI ESTA "A CONFIRMAR NA BANCADA"
// ---------------------------------------------------------------------
// O painel ainda nao foi escolhido, e essa escolha e FISICA: modelo, driver,
// tamanho, como as oito telas sao selecionadas (oito CS de SPI, um mux, ou
// I2C com endereco por tela) e onde entra o PWM do brilho. Por isso a camada
// de desenho fica atras de quatro funcoes finas — `telaIniciar`, `telaLimpar`,
// `telaLinha` e `telaMostrar` — e a escolha e UM #define.
//
// O default e TELA_DRIVER_LOG de proposito, e nao um driver concreto:
//   * compila em qualquer maquina, sem biblioteca a instalar. Um default que
//     exigisse TFT_eSPI ou Adafruit_ILI9341 quebraria o build de todo mundo
//     por causa de um display que ninguem escolheu ainda;
//   * e a ferramenta de bancada de verdade: com ele, o Monitor Serial mostra
//     exatamente o que cada tela mostraria. Quando um painel nao acender, e
//     assim que se descobre se o problema e a ligacao ou a mensagem.
// Trocar e uma linha: ponha TELA_DRIVER_ILI9341_SPI e preencha a tabela de
// pinos abaixo.
#define TELA_DRIVER_LOG            0
#define TELA_DRIVER_ILI9341_SPI    1

#ifndef TELA_DRIVER
#define TELA_DRIVER TELA_DRIVER_LOG
#endif

// Oito telas, uma por dispenser. O `dispenser_id` do contrato e 1..8; o indice
// de todo array daqui e 0..7, e a conversao mora em `canalDoSlot`/`slotDoCanal`
// — as MESMAS duas funcoes da placa dos mecanismos, pelo mesmo motivo: um
// off-by-one aqui pinta o medicamento de um slot na tela do vizinho, e quem
// olha a bancada acredita na tela.
#define NUM_TELAS 8

// Quantas linhas de texto cabem numa tela. A CONFIRMAR NA BANCADA junto com o
// modelo: o layout de §6 precisa de cinco campos mais o cabecalho.
#define TELA_LINHAS 6
#define TELA_COLUNAS 20

// A CONFIRMAR NA BANCADA: pinos. Com TELA_DRIVER_LOG nenhum deles e usado.
#define TFT_PIN_SCK     18
#define TFT_PIN_MOSI    23
#define TFT_PIN_DC       2
#define TFT_PIN_RST      4
#define TFT_PIN_BRILHO  -1        // PWM do backlight; -1 = sem controle
// Um CS por tela (o metodo mais provavel com SPI). Mux ou I2C com endereco por
// tela trocam ESTA tabela e o corpo de `telaSelecionar`, e nada mais.
static const int8_t TFT_PIN_CS[NUM_TELAS] = { 5, 13, 14, 15, 16, 17, 21, 22 };

#define BRILHO_PCT_PADRAO 80

// Periodo do `telemetria`. O mesmo 15 s dos outros subsistemas; o adapter
// descarta a repeticao identica (comparacao ignorando `ts`), entao com as oito
// telas de pe ele manda uma e cala.
#define TELEMETRIA_INTERVALO_MS 15000

// Limites que saem da ORIGEM do dado: `medicamentos.nome` e VARCHAR(150),
// `medicamentos.sku` VARCHAR(200) e `categoria` VARCHAR(100) no central.
// (`MAX_OS_ID_LEN` vem do header.)
#define MAX_MED_LEN   152
#define MAX_SKU_LEN   208
#define MAX_CAT_LEN   104
#define MAX_STATUS_LEN 32

// Teto do `trava_resumo`, o mesmo do contrato e do `TRAVA_RESUMO_MAX` do
// adapter. O resumo JA VEM CORTADO do central, e esta placa nao tenta
// completar o texto nem pedir mais: ele sai da CATEGORIA da divergencia
// ("divergencia de peso", "contagem divergente"), nunca do motivo formatado,
// que passa de 240 caracteres. A tela do slot responde uma pergunta so: e este
// slot? O buffer tem o tamanho do contrato, e nao o do texto de hoje.
#define TRAVA_RESUMO_MAX 48

// ---------------------------------------------------------------------
// ESTADO
// ---------------------------------------------------------------------
struct TelaSlot {
  char medicamento[MAX_MED_LEN];
  char sku[MAX_SKU_LEN];
  char categoria[MAX_CAT_LEN];
  char status[MAX_STATUS_LEN];
  char os_id[MAX_OS_ID_LEN];
  int  quantidade_alvo;
  int  quantidade_dispensada;
  int  quantidade_residual;
  bool ok;        // o ultimo redesenho desta tela deu certo?
};

static TelaSlot telas[NUM_TELAS];

// Estado da celula. `travaSlot` = -1 e a trava SEM slot: existe, e nao e de
// ninguem em particular. A CHAVE vem sempre no comando; e ela que diz a cada
// tela se e este slot ou outro.
static bool travaAtiva = false;
static int  travaSlot  = -1;
static char travaResumo[TRAVA_RESUMO_MAX + 1] = "";
static char travaOsId[MAX_OS_ID_LEN] = "";

static uint8_t       brilhoPct = BRILHO_PCT_PADRAO;
static unsigned long ultimaTelemetriaMs = 0;

// Erros de pintura desde o boot — vao no log, e cada um vira um evento `erro`.
static uint32_t errosDePintura = 0;

enum Enfase { ENF_NORMAL, ENF_REALCE, ENF_ESMAECIDO };

// ---------------------------------------------------------------------
// LOG  (a voz do humano; `apsen_serial.h` exige esta assinatura)
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

// ---------------------------------------------------------------------
// Conversao de indice — as MESMAS duas funcoes da placa dos mecanismos
// ---------------------------------------------------------------------
static inline bool    slotValido(int slot_1a8)       { return slot_1a8 >= 1 && slot_1a8 <= NUM_TELAS; }
static inline uint8_t canalDoSlot(int slot_1a8)      { return (uint8_t)(slot_1a8 - 1); }
static inline int     slotDoCanal(uint8_t canal_0a7) { return (int)canal_0a7 + 1; }

// Corta terminando em "...", como o `copy_trunc()` do display de 7". Texto
// cortado e visivelmente cortado; texto cortado em silencio e texto em que
// alguem acredita.
static void recortar(char* dest, size_t n, const char* origem) {
  if (n == 0) return;
  size_t tam = strlen(origem);
  if (tam < n) { memcpy(dest, origem, tam + 1); return; }
  if (n <= 4)  { memcpy(dest, origem, n - 1); dest[n - 1] = '\0'; return; }
  memcpy(dest, origem, n - 4);
  memcpy(dest + n - 4, "...", 4);
}

// =====================================================================
// CAMADA DE DESENHO — quatro funcoes, e a escolha do painel e um #define
// =====================================================================
#if TELA_DRIVER == TELA_DRIVER_ILI9341_SPI
// A troca e este bloco. Ele NAO e compilado por padrao, para que o build nao
// dependa de uma biblioteca escolhida para um display que ainda nao existe.
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>

// Adafruit_GFX + driver, e nao TFT_eSPI, por um motivo pratico: o TFT_eSPI se
// configura por um `User_Setup.h` DENTRO da pasta da biblioteca, fora deste
// repositorio — a ligacao da bancada ficaria gravada num arquivo que nenhum
// commit registra. Aqui os pinos estao na tabela acima, versionados.
static Adafruit_ILI9341* painel[NUM_TELAS];

static void telaIniciar() {
  for (uint8_t i = 0; i < NUM_TELAS; i++) {
    painel[i] = new Adafruit_ILI9341(TFT_PIN_CS[i], TFT_PIN_DC, TFT_PIN_RST);
    painel[i]->begin();
    painel[i]->setRotation(0);
    painel[i]->setTextSize(2);
  }
}

static uint16_t corDaEnfase(Enfase e) {
  switch (e) {
    case ENF_REALCE:     return ILI9341_YELLOW;
    case ENF_ESMAECIDO:  return ILI9341_DARKGREY;
    default:             return ILI9341_WHITE;
  }
}

static bool telaLimpar(uint8_t i, bool alerta) {
  painel[i]->fillScreen(alerta ? ILI9341_RED : ILI9341_BLACK);
  return true;
}

static bool telaLinha(uint8_t i, uint8_t linha, const char* texto, Enfase enfase) {
  painel[i]->setCursor(4, 4 + linha * 22);
  painel[i]->setTextColor(corDaEnfase(enfase));
  painel[i]->print(texto);
  return true;
}

static bool telaMostrar(uint8_t i) { (void)i; return true; }

#else   // TELA_DRIVER_LOG
// Sem painel: a "tela" e o Monitor Serial. Linha sem '{' e log, entao o
// adapter a ignora e as duas vozes seguem convivendo na mesma porta.

static void telaIniciar() {
  logMsg("TELA", "driver = LOG (nenhum painel). O que cada tela mostraria sai aqui.");
  logMsg("TELA", "escolha o painel, troque TELA_DRIVER e preencha a tabela de pinos.");
}

static bool telaLimpar(uint8_t i, bool alerta) {
  Serial.printf("+-- D%d %s\n", slotDoCanal(i), alerta ? "[ALERTA] ----" : "-----------");
  return true;
}

static bool telaLinha(uint8_t i, uint8_t linha, const char* texto, Enfase enfase) {
  const char* marca = (enfase == ENF_REALCE) ? "*" : (enfase == ENF_ESMAECIDO ? "." : " ");
  Serial.printf("| D%d %s %s\n", slotDoCanal(i), marca, texto);
  (void)linha;
  return true;
}

static bool telaMostrar(uint8_t i) {
  Serial.printf("+-- D%d fim\n", slotDoCanal(i));
  return true;
}
#endif

// =====================================================================
// EVENTOS (placa -> adapter). Os dois param no adapter: log e /health.
// =====================================================================
static void emitTelemetriaTelas() {
  uint8_t ok = 0;
  for (uint8_t i = 0; i < NUM_TELAS; i++) if (telas[i].ok) ok++;
  char ts[24]; tsAgora(ts, sizeof ts);
  // Sai inteiro do estado: sem millis(), sem contador que ande sozinho. Duas
  // emissoes seguidas com as oito telas de pe sao byte a byte iguais fora do
  // `ts`, que e o que o filtro de repeticao do adapter compara — se carregasse
  // um uptime, o filtro deixaria de filtrar.
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"telemetria\",\"telas_ok\":%u,\"brilho_pct\":%u,"
    "\"ts\":\"%s\"}}", (unsigned)ok, (unsigned)brilhoPct, ts), "telemetria");
}

static void emitErroTela(int slot_1a8, const char* codigo, const char* descricao) {
  char ts[24]; tsAgora(ts, sizeof ts);
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"evento\":{\"tipo\":\"erro\",\"dispenser_id\":%d,\"codigo_erro\":\"%s\","
    "\"descricao\":\"%s\",\"ts\":\"%s\"}}",
    slot_1a8, codigo, descricao, ts), "erro");
}

// =====================================================================
// PINTURA — a tabela de §6, e nada alem dela
// =====================================================================
//
//  | estado recebido                            | a tela mostra                       |
//  |--------------------------------------------|-------------------------------------|
//  | trava_ativa=false                          | medicamento, SKU, disp/alvo,        |
//  |                                            | residual, status                    |
//  | trava_ativa=true e trava_slot_id == meu id | alerta + AGUARDE SUPERVISOR +       |
//  |                                            | trava_resumo                        |
//  | trava_ativa=true e outro slot              | PARADO - D{n}, conteudo esmaecido   |

static bool pintarNormal(uint8_t canal, const TelaSlot& t) {
  char linha[TELA_COLUNAS + 8];
  char campo[TELA_COLUNAS + 8];
  bool ok = true;

  ok &= telaLimpar(canal, false);

  snprintf(linha, sizeof linha, "D%d  %s", slotDoCanal(canal),
           t.status[0] ? t.status : "idle");
  ok &= telaLinha(canal, 0, linha, ENF_REALCE);

  recortar(campo, sizeof campo, t.medicamento[0] ? t.medicamento : "-");
  ok &= telaLinha(canal, 1, campo, ENF_NORMAL);

  recortar(campo, sizeof campo, t.sku[0] ? t.sku : "-");
  ok &= telaLinha(canal, 2, campo, ENF_ESMAECIDO);

  snprintf(linha, sizeof linha, "%d/%d", t.quantidade_dispensada, t.quantidade_alvo);
  ok &= telaLinha(canal, 3, linha, ENF_NORMAL);

  snprintf(linha, sizeof linha, "resid %d", t.quantidade_residual);
  ok &= telaLinha(canal, 4, linha, ENF_NORMAL);

  ok &= telaMostrar(canal);
  return ok;
}

static bool pintarTravaNesteSlot(uint8_t canal, const TelaSlot& t) {
  char linha[TELA_COLUNAS + 8];
  bool ok = true;

  ok &= telaLimpar(canal, true);

  snprintf(linha, sizeof linha, "D%d  ** TRAVA **", slotDoCanal(canal));
  ok &= telaLinha(canal, 0, linha, ENF_REALCE);
  ok &= telaLinha(canal, 1, "AGUARDE",    ENF_REALCE);
  ok &= telaLinha(canal, 2, "SUPERVISOR", ENF_REALCE);

  // O resumo cabe em 48 caracteres e a tela tem TELA_COLUNAS: ele desce em
  // ate duas linhas. Nada de pedir mais texto ao adapter — o que ele mandou e
  // o que existe.
  char parte[TELA_COLUNAS + 1];
  const char* resto = travaResumo;
  for (uint8_t l = 3; l <= 4; l++) {
    size_t n = strnlen(resto, TELA_COLUNAS);
    memcpy(parte, resto, n);
    parte[n] = '\0';
    ok &= telaLinha(canal, l, parte, ENF_NORMAL);
    resto += n;
    if (*resto == '\0') break;
  }

  ok &= telaMostrar(canal);
  return ok;
}

static bool pintarTravaEmOutroSlot(uint8_t canal, const TelaSlot& t) {
  char linha[TELA_COLUNAS + 8];
  char campo[TELA_COLUNAS + 8];
  bool ok = true;

  ok &= telaLimpar(canal, false);

  snprintf(linha, sizeof linha, "D%d", slotDoCanal(canal));
  ok &= telaLinha(canal, 0, linha, ENF_NORMAL);

  if (travaSlot >= 1) snprintf(linha, sizeof linha, "PARADO - D%d", travaSlot);
  else                snprintf(linha, sizeof linha, "PARADO");
  ok &= telaLinha(canal, 1, linha, ENF_REALCE);

  // Conteudo esmaecido: ele continua la, so nao e o assunto. Apaga-lo faria o
  // operador achar que o slot foi zerado.
  recortar(campo, sizeof campo, t.medicamento[0] ? t.medicamento : "-");
  ok &= telaLinha(canal, 2, campo, ENF_ESMAECIDO);

  snprintf(linha, sizeof linha, "%d/%d", t.quantidade_dispensada, t.quantidade_alvo);
  ok &= telaLinha(canal, 3, linha, ENF_ESMAECIDO);

  ok &= telaMostrar(canal);
  return ok;
}

static void pintarSlot(uint8_t canal) {
  TelaSlot& t = telas[canal];
  bool ok;

  if (!travaAtiva)                            ok = pintarNormal(canal, t);
  else if (travaSlot == slotDoCanal(canal))   ok = pintarTravaNesteSlot(canal, t);
  else                                        ok = pintarTravaEmOutroSlot(canal, t);

  t.ok = ok;
  if (!ok) {
    // Tela que nao respondeu vira evento `erro` — que para no adapter, em log
    // e no /health. Nao muda nada do caminho do dispenser: o ACK ja saiu.
    errosDePintura++;
    logMsg("ERRO", "tela D%d nao respondeu ao redesenho (total=%lu)",
         slotDoCanal(canal), (unsigned long)errosDePintura);
    emitErroTela(slotDoCanal(canal), "tela_sem_resposta",
                 "tela nao respondeu ao redesenho");
  }
}

// =====================================================================
// COMANDOS (§6)
// =====================================================================
static void cmdSlot(const char* linha, long cmd_id) {
  float fslot = 0, falvo = 0, fdisp = 0, fresid = 0;
  char med[MAX_MED_LEN], sku[MAX_SKU_LEN], cat[MAX_CAT_LEN];
  char status[MAX_STATUS_LEN], os_id[MAX_OS_ID_LEN];

  if (!jsonNumero(linha, "dispenser_id", &fslot)) {
    ackErro(cmd_id, "dispenser_id ausente em slot");
    return;
  }
  const int slot = (int)fslot;
  if (!slotValido(slot)) {
    ackErro(cmd_id, "dispenser_id fora de 1..NUM_TELAS");
    return;
  }

  // Campo de texto ausente vira vazio, e numero ausente vira 0: o comando
  // pinta o que veio. Recusar um redesenho por causa de um `categoria` que nao
  // veio deixaria a tela mostrando o estado ANTERIOR — que e o unico desfecho
  // pior do que mostrar um campo vazio.
  if (!jsonTexto(linha, "medicamento", med,    sizeof med))    med[0] = '\0';
  if (!jsonTexto(linha, "sku",         sku,    sizeof sku))    sku[0] = '\0';
  if (!jsonTexto(linha, "categoria",   cat,    sizeof cat))    cat[0] = '\0';
  if (!jsonTexto(linha, "status",      status, sizeof status)) status[0] = '\0';
  if (!jsonTexto(linha, "os_id",       os_id,  sizeof os_id))  os_id[0] = '\0';
  jsonNumero(linha, "quantidade_alvo",       &falvo);
  jsonNumero(linha, "quantidade_dispensada", &fdisp);
  jsonNumero(linha, "quantidade_residual",   &fresid);

  // ACK ANTES de pintar: a placa das telas nunca segura o caminho do
  // dispenser, nem por um redesenho.
  ackOk(cmd_id, false);

  const uint8_t canal = canalDoSlot(slot);
  TelaSlot& t = telas[canal];
  copiarCampo(t.medicamento, sizeof t.medicamento, med);
  copiarCampo(t.sku,         sizeof t.sku,         sku);
  copiarCampo(t.categoria,   sizeof t.categoria,   cat);
  copiarCampo(t.status,      sizeof t.status,      status);
  copiarCampo(t.os_id,       sizeof t.os_id,       os_id);
  t.quantidade_alvo       = (int)falvo;
  t.quantidade_dispensada = (int)fdisp;
  t.quantidade_residual   = (int)fresid;

  pintarSlot(canal);
}

static void cmdEstadoCelula(const char* linha, long cmd_id) {
  bool ativa = false;
  char os_id[MAX_OS_ID_LEN], resumo[TRAVA_RESUMO_MAX + 1];

  if (!jsonBool(linha, "trava_ativa", &ativa)) {
    ackErro(cmd_id, "trava_ativa ausente em estado_celula");
    return;
  }
  // `trava_slot_id` pode vir NULO — trava sem slot —, mas a CHAVE vem sempre:
  // e ela que diz a cada tela se e este slot ou outro. Chave ausente e
  // comando mal formado; chave com `null` e uma trava que nao e de ninguem.
  if (!jsonTemChave(linha, "trava_slot_id")) {
    ackErro(cmd_id, "trava_slot_id ausente em estado_celula");
    return;
  }
  float fslot = 0;
  const bool temSlot = jsonNumero(linha, "trava_slot_id", &fslot);

  if (!jsonTexto(linha, "os_id",        os_id,  sizeof os_id))  os_id[0] = '\0';
  if (!jsonTexto(linha, "trava_resumo", resumo, sizeof resumo)) resumo[0] = '\0';

  ackOk(cmd_id, false);

  travaAtiva = ativa;
  travaSlot  = temSlot ? (int)fslot : -1;
  copiarCampo(travaOsId,   sizeof travaOsId,   os_id);
  copiarCampo(travaResumo, sizeof travaResumo, resumo);

  logMsg("TELA", "estado da celula: trava=%s slot=%d os=%s resumo='%s'",
       travaAtiva ? "SIM" : "nao", travaSlot,
       travaOsId[0] ? travaOsId : "-", travaResumo);

  // Todas as oito redesenham: cada uma precisa saber se e "este slot" ou
  // "outro", e so ela sabe responder isso depois de ver a chave.
  for (uint8_t i = 0; i < NUM_TELAS; i++) pintarSlot(i);
}

// O contrato que `apsen_serial.h` pede do sketch: quais comandos existem.
void executarComando(const char* cmd, const char* linha, long cmd_id) {
  if      (strcmp(cmd, "slot")          == 0) cmdSlot(linha, cmd_id);
  else if (strcmp(cmd, "estado_celula") == 0) cmdEstadoCelula(linha, cmd_id);
  else {
    // Comando fora do contrato recusa com ACK NEGATIVO, nunca em silencio: o
    // adapter o transforma em 502 e quem pediu sabe na hora, em vez de esperar
    // o `ack_timeout_s` inteiro para descobrir o mesmo.
    ackErro(cmd_id, "comando desconhecido");
    logMsg("APSEN", "comando desconhecido: '%s'", cmd);
  }
}

// A linha SEM '{'. Esta placa nao tem terminal de manutencao: nao ha nada nela
// para calibrar, e um terminal so existiria para ser mantido.
void linhaHumana(char* linha) {
  logMsg("INFO", "placa das telas TFT (%d telas). Comando de maquina comeca com "
       "'{'; '%s' e log.", NUM_TELAS, linha);
}

// =====================================================================
void setup() {
  apsenSerialInit();     // buffer de recepcao do tamanho do teto da linha
  Serial.begin(115200);
  delay(1000);           // tempo para o Serial Monitor conectar
  Serial.println();
  Serial.println(F("=== ESP32 INICIOU - Telas TFT dos dispensers ==="));

  for (uint8_t i = 0; i < NUM_TELAS; i++) {
    telas[i].medicamento[0]      = '\0';
    telas[i].sku[0]              = '\0';
    telas[i].categoria[0]        = '\0';
    telas[i].os_id[0]            = '\0';
    telas[i].quantidade_alvo     = 0;
    telas[i].quantidade_dispensada = 0;
    telas[i].quantidade_residual = 0;
    telas[i].ok                  = true;
    strncpy(telas[i].status, "idle", sizeof telas[i].status - 1);
    telas[i].status[sizeof telas[i].status - 1] = '\0';
  }

  telaIniciar();
#if TFT_PIN_BRILHO >= 0
  pinMode(TFT_PIN_BRILHO, OUTPUT);
  analogWrite(TFT_PIN_BRILHO, (int)(255L * BRILHO_PCT_PADRAO / 100));
#endif

  // Pinta o estado inicial: oito telas vazias e sem trava. Tela apagada no
  // boot faria o operador achar que a placa nao subiu.
  for (uint8_t i = 0; i < NUM_TELAS; i++) pintarSlot(i);

  logMsg("BOOT", "pronto. Aguardando `slot` e `estado_celula` do adapter.");

  // A primeira linha de maquina da porta. Quem inicia o ping e SEMPRE a placa:
  // e por ele que o adapter identifica esta porta como a das telas.
  emitPing();
  emitTelemetriaTelas();
}

void loop() {
  serialPoll();
  pingPoll();

  if (millis() - ultimaTelemetriaMs >= TELEMETRIA_INTERVALO_MS) {
    ultimaTelemetriaMs = millis();
    emitTelemetriaTelas();
  }

  delay(2);
}
