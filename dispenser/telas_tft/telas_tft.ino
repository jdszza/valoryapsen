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
 *  A TELA MOSTRA A CAIXA DO MEDICAMENTO
 *    O hardware e o da bancada: 8 ST7735 de 128x160, ligacao e inicializacao
 *    vindas do `referencia/TFT.ino` (o sketch de teste que ja funcionava), sem
 *    mudar um pino. Cada slot com medicamento carregado mostra a IMAGEM dele,
 *    de `imagens.h`. O `slot` traz o NOME; quem o traduz para o numero da
 *    imagem e `catalogo_imagens.h`, ao lado das imagens — o protocolo e o
 *    adapter nao mudaram para isso.
 *
 *  DUAS VOZES NA MESMA PORTA, E O SEPARADOR E O '{'
 *    Igual a placa dos mecanismos e a balanca: linha COM '{' e mensagem,
 *    linha SEM '{' e log. A voz do humano aqui so tem dois comandos de
 *    bancada, para conferir a ordem das imagens sem PC: `lista` e
 *    `img <slot> <n>`.
 *
 *  ARDUINO IDE
 *    Placa: "ESP32 Dev Module"
 *    Partition Scheme: "Huge APP (3MB No OTA/1MB SPIFFS)"  <-- OBRIGATORIO
 *      As 39 imagens somam 1,6 MB (39 x 128 x 160 x 2 bytes), e o esquema
 *      default tem 1,25 MB de app: o build falha com "Sketch too big". O
 *      `referencia/TFT.ino` cabia no default porque usava so 8 imagens, e o
 *      linker descartava as outras 31.
 *    Bibliotecas: Adafruit GFX Library, Adafruit ST7735 and ST7789 Library
 *    Compilar:
 *      arduino-cli compile --fqbn esp32:esp32:esp32:PartitionScheme=huge_app dispenser/telas_tft
 * =====================================================================
 */

#include <Arduino.h>
#include <stdarg.h>

// A voz de MAQUINA inteira — enquadramento, relogio, scanner de JSON, ACK e
// `cmd_id` — vem daqui, identica a da placa dos mecanismos. Ver o cabecalho do
// arquivo para por que sao duas copias e qual teste as compara.
#define APSEN_SUBSISTEMA "dispenser_tft"
#include "apsen_serial.h"

#include "catalogo_imagens.h"

// ---------------------------------------------------------------------
// DRIVER DAS TELAS
// ---------------------------------------------------------------------
// O default e o painel REAL da bancada (ST7735). TELA_DRIVER_LOG continua
// existindo como ferramenta: compila sem biblioteca nenhuma e mostra no Monitor
// Serial o que cada tela desenharia — quando um painel nao acender, e assim que
// se descobre se o problema e a ligacao ou a mensagem.
#define TELA_DRIVER_LOG     0
#define TELA_DRIVER_ST7735  1

#ifndef TELA_DRIVER
#define TELA_DRIVER TELA_DRIVER_ST7735
#endif

// Oito telas, uma por dispenser. O `dispenser_id` do contrato e 1..8; o indice
// de todo array daqui e 0..7, e a conversao mora em `canalDoSlot`/`slotDoCanal`
// — as MESMAS duas funcoes da placa dos mecanismos, pelo mesmo motivo: um
// off-by-one aqui pinta o medicamento de um slot na tela do vizinho, e quem
// olha a bancada acredita na tela.
#define NUM_TELAS 8

// PINOUT — o do `referencia/TFT.ino`, que e o que esta soldado na bancada.
// SPI por hardware (VSPI): SCK = GPIO18, MOSI = GPIO23. DC e RST sao
// compartilhados pelas oito; cada tela tem o seu CS.
#define TFT_PIN_DC      4
#define TFT_PIN_RST    22         // reset COMPARTILHADO: pulsado UMA vez no boot
#define TFT_PIN_BRILHO -1         // backlight direto no 3V3; -1 = sem controle
#define TFT_SPI_HZ     27000000
// A ordem do array E a ordem dos slots: CS_PINS[0] e a tela do D1.
// GPIO12 e pino de strapping (tensao da flash): nada pode puxa-lo para HIGH
// durante o reset. O setup so o leva a HIGH depois do boot, como o TFT.ino.
static const uint8_t TFT_PIN_CS[NUM_TELAS] = { 12, 13, 14, 25, 26, 27, 32, 33 };

// Sem pino de backlight nao ha brilho a ajustar; a telemetria reporta o que a
// tela efetivamente tem.
#if TFT_PIN_BRILHO >= 0
#define BRILHO_PCT_PADRAO 80
#else
#define BRILHO_PCT_PADRAO 100
#endif

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

// Tudo que uma tela desenhou, numa string. Redesenho com a mesma assinatura e
// pulado: empurrar 40 KB de imagem pela SPI a cada transicao que nao muda a
// tela (um `dispensando` de um slot cuja caixa ja esta la) e piscar a tela a
// toa. Cabe medicamento + resumo + os numeros.
#define ASSINATURA_MAX (MAX_MED_LEN + TRAVA_RESUMO_MAX + MAX_STATUS_LEN + 48)

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
  int  imagem;    // indice em NOMES_IMAGEM; -1 = sem imagem (vazio ou desconhecido)
  bool ok;        // o ultimo redesenho desta tela deu certo?
  char desenhado[ASSINATURA_MAX];   // assinatura do que esta na tela agora
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

enum Enfase { ENF_NORMAL, ENF_REALCE, ENF_ESMAECIDO, ENF_ALERTA };

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

// =====================================================================
// NOME -> NUMERO DA IMAGEM
// =====================================================================
// So ASCII: os nomes do catalogo sao todos maiusculos e sem acento, e a
// comparacao nao precisa ser mais esperta do que isso — ver o cabecalho de
// `catalogo_imagens.h` sobre por que "parecido" nao serve.
static inline char maiuscula(char c) { return (c >= 'a' && c <= 'z') ? (char)(c - 32) : c; }
static inline bool espaco(char c)    { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; }

// Maiusculas, sem espaco nas pontas, espacos repetidos viram um.
static void normalizarNome(char* dest, size_t n, const char* origem) {
  size_t j = 0;
  bool pendente = false;
  while (*origem && espaco(*origem)) origem++;
  for (; *origem && j + 1 < n; origem++) {
    if (espaco(*origem)) { pendente = true; continue; }
    if (pendente && j + 2 < n) dest[j++] = ' ';
    pendente = false;
    dest[j++] = maiuscula(*origem);
  }
  dest[j] = '\0';
}

// -1 = sem imagem. Nome vazio tambem e -1, e nao e erro: e o slot vazio.
static int indiceDaImagem(const char* nome) {
  char alvo[MAX_MED_LEN], candidato[MAX_MED_LEN];
  normalizarNome(alvo, sizeof alvo, nome);
  if (alvo[0] == '\0') return -1;
  for (int i = 0; i < NUM_IMAGENS; i++) {
    normalizarNome(candidato, sizeof candidato, NOMES_IMAGEM[i]);
    if (strcmp(alvo, candidato) == 0) return i;
  }
  return -1;
}

// =====================================================================
// CAMADA DE DESENHO
// =====================================================================
// Seis funcoes finas, e a escolha do driver e um #define. Todas devolvem bool:
// e o que vira o evento `erro` quando uma tela nao responde.
//   telaIniciar  telaImagem  telaFundo  telaTexto  telaFaixa  telaMostrar

#if TELA_DRIVER == TELA_DRIVER_ST7735
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>
#include "imagens.h"

// A tabela de ponteiros acompanha `catalogo_imagens.h` posicao a posicao:
// IMAGENS[i] e a caixa de NOMES_IMAGEM[i]. Se alguem acrescentar uma imagem e
// esquecer o nome (ou o contrario), o build para aqui — antes de a tela de um
// slot mostrar a caixa do vizinho na lista.
static const uint16_t* const IMAGENS[] = {
  img0,  img1,  img2,  img3,  img4,  img5,  img6,  img7,  img8,  img9,
  img10, img11, img12, img13, img14, img15, img16, img17, img18, img19,
  img20, img21, img22, img23, img24, img25, img26, img27, img28, img29,
  img30, img31, img32, img33, img34, img35, img36, img37, img38,
};
static_assert((int)(sizeof(IMAGENS) / sizeof(IMAGENS[0])) == NUM_IMAGENS,
              "imagens.h e catalogo_imagens.h tem tamanhos diferentes");

#define COR_CINZA 0x7BEF

// Adafruit_GFX + driver, e nao TFT_eSPI: o TFT_eSPI se configura por um
// `User_Setup.h` DENTRO da pasta da biblioteca, fora deste repositorio — a
// ligacao da bancada ficaria gravada num arquivo que nenhum commit registra.
static Adafruit_ST7735* painel[NUM_TELAS];

static void telaIniciar() {
  // Todos os CS em HIGH = nenhuma tela selecionada. Precisa vir ANTES do
  // reset: com um CS flutuando, a tela dele leria a inicializacao das outras.
  for (uint8_t i = 0; i < NUM_TELAS; i++) {
    pinMode(TFT_PIN_CS[i], OUTPUT);
    digitalWrite(TFT_PIN_CS[i], HIGH);
  }

  // Reset compartilhado: pulsa UMA vez para todas. Por isso o construtor
  // recebe -1 no RST — se cada `initR` pulsasse o reset, inicializar a tela 2
  // apagaria a tela 1.
  pinMode(TFT_PIN_RST, OUTPUT);
  digitalWrite(TFT_PIN_RST, LOW);  delay(20);
  digitalWrite(TFT_PIN_RST, HIGH); delay(150);

  for (uint8_t i = 0; i < NUM_TELAS; i++) {
    painel[i] = new Adafruit_ST7735(TFT_PIN_CS[i], TFT_PIN_DC, -1);
    painel[i]->initR(INITR_BLACKTAB);
    painel[i]->setSPISpeed(TFT_SPI_HZ);
    painel[i]->setRotation(0);
    painel[i]->setTextWrap(false);
  }
}

static uint16_t corDaEnfase(Enfase e) {
  switch (e) {
    case ENF_REALCE:     return ST77XX_YELLOW;
    case ENF_ESMAECIDO:  return COR_CINZA;
    case ENF_ALERTA:     return ST77XX_RED;
    default:             return ST77XX_WHITE;
  }
}

static bool telaImagem(uint8_t i, int imagem) {
  if (imagem < 0 || imagem >= NUM_IMAGENS) return false;
  painel[i]->drawRGBBitmap(0, 0, IMAGENS[imagem], IMAGEM_LARGURA, IMAGEM_ALTURA);
  return true;
}

static bool telaFundo(uint8_t i, bool alerta) {
  painel[i]->fillScreen(alerta ? ST77XX_RED : ST77XX_BLACK);
  return true;
}

// Texto centralizado na linha `y` (pixels). Fonte padrao: 6x8 por tamanho.
static bool telaTexto(uint8_t i, int16_t y, const char* texto, Enfase enfase, uint8_t tam) {
  int16_t largura = (int16_t)strlen(texto) * 6 * tam;
  int16_t x = (IMAGEM_LARGURA - largura) / 2;
  if (x < 0) x = 0;
  painel[i]->setTextSize(tam);
  painel[i]->setTextColor(corDaEnfase(enfase));
  painel[i]->setCursor(x, y);
  painel[i]->print(texto);
  return true;
}

// Faixa de 18 px por cima da imagem, no topo ou no rodape.
static bool telaFaixa(uint8_t i, bool topo, const char* texto, Enfase enfase) {
  const int16_t alt = 18;
  const int16_t y = topo ? 0 : IMAGEM_ALTURA - alt;
  painel[i]->fillRect(0, y, IMAGEM_LARGURA, alt,
                      enfase == ENF_ALERTA ? ST77XX_RED : ST77XX_BLACK);
  return telaTexto(i, y + 5, texto,
                   enfase == ENF_ALERTA ? ENF_NORMAL : enfase, 1);
}

// O ST7735 nao tem linha de leitura (so MOSI): nao ha como a placa saber que
// um painel nao acendeu. `true` aqui e "a SPI aceitou", nao "o painel mostrou"
// — quem confere e o olho na bancada, com `img <slot> <n>`.
static bool telaMostrar(uint8_t i) { (void)i; return true; }

#else   // TELA_DRIVER_LOG
// Sem painel: a "tela" e o Monitor Serial. Linha sem '{' e log, entao o
// adapter a ignora e as duas vozes seguem convivendo na mesma porta.

static void telaIniciar() {
  logMsg("TELA", "driver = LOG (nenhum painel). O que cada tela mostraria sai aqui.");
}

static bool telaImagem(uint8_t i, int imagem) {
  if (imagem < 0 || imagem >= NUM_IMAGENS) return false;
  Serial.printf("+-- D%d [imagem %d: %s]\n", slotDoCanal(i), imagem, NOMES_IMAGEM[imagem]);
  return true;
}

static bool telaFundo(uint8_t i, bool alerta) {
  Serial.printf("+-- D%d %s\n", slotDoCanal(i), alerta ? "[ALERTA] ----" : "-----------");
  return true;
}

static bool telaTexto(uint8_t i, int16_t y, const char* texto, Enfase enfase, uint8_t tam) {
  const char* marca = (enfase == ENF_REALCE) ? "*" : (enfase == ENF_ESMAECIDO ? "." : " ");
  Serial.printf("| D%d %s %s\n", slotDoCanal(i), marca, texto);
  (void)y; (void)tam;
  return true;
}

static bool telaFaixa(uint8_t i, bool topo, const char* texto, Enfase enfase) {
  Serial.printf("| D%d [faixa %s] %s\n", slotDoCanal(i), topo ? "topo" : "rodape", texto);
  (void)enfase;
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
//  | estado recebido                            | a tela mostra                        |
//  |--------------------------------------------|--------------------------------------|
//  | trava_ativa=false, medicamento com imagem  | a caixa do medicamento (tela cheia); |
//  |                                            | faixa vermelha "D{n} ERRO" se erro   |
//  | trava_ativa=false, sem imagem              | TEXTO: D{n}, status, nome, disp/alvo |
//  | trava_ativa=true e trava_slot_id == meu id | fundo vermelho + AGUARDE SUPERVISOR  |
//  |                                            | + trava_resumo                       |
//  | trava_ativa=true e outro slot              | a mesma tela, com a faixa            |
//  |                                            | "PARADO - D{n}" no topo              |
//
// "Sem imagem" e dois casos, e os dois TEM que parecer diferentes de uma
// caixa: slot vazio ("VAZIO") e medicamento fora de `catalogo_imagens.h` (o
// nome escrito). Caixa generica para um nome desconhecido seria uma tela
// afirmando um medicamento que ninguem conferiu.

#define TEXTO_COLUNAS 21     // 128 px / 6 px por caractere, tamanho 1

// Ate `linhas` linhas de TEXTO_COLUNAS a partir de `y`, quebrando por tamanho.
static bool textoEmLinhas(uint8_t canal, int16_t y, const char* texto,
                          uint8_t linhas, Enfase enfase) {
  char parte[TEXTO_COLUNAS + 1];
  bool ok = true;
  const char* resto = texto;
  for (uint8_t l = 0; l < linhas && *resto; l++) {
    size_t n = strnlen(resto, TEXTO_COLUNAS);
    memcpy(parte, resto, n);
    parte[n] = '\0';
    ok &= telaTexto(canal, y + l * 10, parte, enfase, 1);
    resto += n;
  }
  return ok;
}

static bool statusDeErro(const TelaSlot& t) { return strcmp(t.status, "erro") == 0; }

static bool pintarConteudo(uint8_t canal, const TelaSlot& t, bool esmaecido) {
  char linha[TEXTO_COLUNAS + 8];
  bool ok = true;
  const int slot = slotDoCanal(canal);

  if (t.imagem >= 0) {
    ok &= telaImagem(canal, t.imagem);
    if (statusDeErro(t)) {
      snprintf(linha, sizeof linha, "D%d ERRO", slot);
      ok &= telaFaixa(canal, false, linha, ENF_ALERTA);
    }
    return ok;
  }

  const Enfase base = esmaecido ? ENF_ESMAECIDO : ENF_NORMAL;
  ok &= telaFundo(canal, false);
  snprintf(linha, sizeof linha, "D%d", slot);
  ok &= telaTexto(canal, 26, linha, esmaecido ? ENF_ESMAECIDO : ENF_REALCE, 3);
  ok &= telaTexto(canal, 58, t.status[0] ? t.status : "idle",
                  statusDeErro(t) ? ENF_ALERTA : base, 1);

  if (t.medicamento[0] == '\0') {
    ok &= telaTexto(canal, 84, "VAZIO", base, 2);
    return ok;
  }
  // Medicamento sem imagem: o nome escrito, e o aviso de que nao ha caixa.
  ok &= textoEmLinhas(canal, 78, t.medicamento, 3, base);
  snprintf(linha, sizeof linha, "%d/%d", t.quantidade_dispensada, t.quantidade_alvo);
  ok &= telaTexto(canal, 114, linha, base, 2);
  ok &= telaTexto(canal, 142, "(sem imagem)", ENF_ESMAECIDO, 1);
  return ok;
}

static bool pintarTravaNesteSlot(uint8_t canal) {
  char linha[TEXTO_COLUNAS + 8];
  bool ok = true;

  ok &= telaFundo(canal, true);
  snprintf(linha, sizeof linha, "D%d", slotDoCanal(canal));
  ok &= telaTexto(canal, 8,  linha,        ENF_NORMAL, 3);
  ok &= telaTexto(canal, 40, "TRAVA",      ENF_REALCE, 2);
  ok &= telaTexto(canal, 66, "AGUARDE",    ENF_NORMAL, 2);
  ok &= telaTexto(canal, 86, "SUPERVISOR", ENF_NORMAL, 2);
  // O resumo cabe em 48 caracteres e desce em ate tres linhas. Nada de pedir
  // mais texto ao adapter — o que ele mandou e o que existe.
  ok &= textoEmLinhas(canal, 116, travaResumo, 3, ENF_NORMAL);
  return ok;
}

static bool pintarTravaEmOutroSlot(uint8_t canal, const TelaSlot& t) {
  char linha[TEXTO_COLUNAS + 8];
  // O conteudo continua la, so nao e o assunto: apaga-lo faria o operador
  // achar que o slot foi zerado. A faixa diz onde esta o problema.
  bool ok = pintarConteudo(canal, t, true);
  if (travaSlot >= 1) snprintf(linha, sizeof linha, "PARADO - D%d", travaSlot);
  else                snprintf(linha, sizeof linha, "PARADO");
  ok &= telaFaixa(canal, true, linha, ENF_REALCE);
  return ok;
}

// Tudo que determina o desenho, numa string so. Igual a do ultimo desenho =
// nada a fazer.
static void assinaturaDe(uint8_t canal, char* dest, size_t n) {
  const TelaSlot& t = telas[canal];
  const int slot = slotDoCanal(canal);
  if (travaAtiva && travaSlot == slot) {
    snprintf(dest, n, "T|%s", travaResumo);
  } else if (t.imagem >= 0) {
    // Com caixa na tela, os numeros nao aparecem: mudar so a quantidade nao
    // redesenha 40 KB de imagem.
    snprintf(dest, n, "I|%d|%d|%d", t.imagem, statusDeErro(t) ? 1 : 0,
             travaAtiva ? travaSlot : -2);
  } else {
    snprintf(dest, n, "X|%s|%s|%d/%d|%d", t.status, t.medicamento,
             t.quantidade_dispensada, t.quantidade_alvo, travaAtiva ? travaSlot : -2);
  }
}

static void pintarSlot(uint8_t canal) {
  TelaSlot& t = telas[canal];
  char assinatura[ASSINATURA_MAX];
  assinaturaDe(canal, assinatura, sizeof assinatura);
  if (strcmp(assinatura, t.desenhado) == 0) return;

  bool ok;
  if (!travaAtiva)                            ok = pintarConteudo(canal, t, false);
  else if (travaSlot == slotDoCanal(canal))   ok = pintarTravaNesteSlot(canal);
  else                                        ok = pintarTravaEmOutroSlot(canal, t);
  ok &= telaMostrar(canal);

  t.ok = ok;
  if (ok) {
    copiarCampo(t.desenhado, sizeof t.desenhado, assinatura);
  } else {
    // Assinatura apagada: a proxima transicao tenta de novo, em vez de achar
    // que a tela ja mostra o que nunca chegou a mostrar.
    t.desenhado[0] = '\0';
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

  // A traducao nome -> numero acontece UMA vez, na chegada do nome.
  t.imagem = indiceDaImagem(t.medicamento);
  if (t.imagem < 0 && t.medicamento[0])
    logMsg("TELA", "D%d: '%s' nao esta em catalogo_imagens.h — tela em modo texto",
           slot, t.medicamento);

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

  // Todas as oito avaliam: cada uma precisa saber se e "este slot" ou
  // "outro", e so ela sabe responder isso depois de ver a chave. A assinatura
  // poupa as que nao mudaram.
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

// =====================================================================
// A VOZ DO HUMANO — so para conferir as imagens na bancada
// =====================================================================
//   lista            imprime numero -> nome de todas as imagens
//   img <slot> <n>   desenha a imagem n na tela do slot (1..8)
//
// E o teste do `referencia/TFT.ino` sem regravar a placa: "a imagem 5 e mesmo
// o DESOL?" se responde olhando. O desenho de bancada NAO entra na assinatura
// — o proximo `slot` daquele dispenser redesenha o estado verdadeiro.
static bool palavraIgual(const char* a, const char* b) {
  while (*a && *b) { if (maiuscula(*a++) != maiuscula(*b++)) return false; }
  return *a == '\0' && (*b == '\0' || espaco(*b));
}

void linhaHumana(char* linha) {
  while (espaco(*linha)) linha++;

  if (palavraIgual("lista", linha)) {
    for (int i = 0; i < NUM_IMAGENS; i++) logMsg("IMG", "%2d  %s", i, NOMES_IMAGEM[i]);
    return;
  }
  int slot = 0, imagem = -1;
  if (palavraIgual("img", linha) && sscanf(linha + 3, "%d %d", &slot, &imagem) == 2) {
    if (!slotValido(slot) || imagem < 0 || imagem >= NUM_IMAGENS) {
      logMsg("IMG", "uso: img <1..%d> <0..%d>", NUM_TELAS, NUM_IMAGENS - 1);
      return;
    }
    const uint8_t canal = canalDoSlot(slot);
    telaImagem(canal, imagem);
    telas[canal].desenhado[0] = '\0';
    logMsg("IMG", "D%d <- imagem %d (%s)", slot, imagem, NOMES_IMAGEM[imagem]);
    return;
  }
  logMsg("INFO", "placa das telas TFT (%d telas, %d imagens). Comando de maquina "
       "comeca com '{'; de bancada: 'lista', 'img <slot> <n>'.", NUM_TELAS, NUM_IMAGENS);
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
    telas[i].desenhado[0]        = '\0';
    telas[i].quantidade_alvo     = 0;
    telas[i].quantidade_dispensada = 0;
    telas[i].quantidade_residual = 0;
    telas[i].imagem              = -1;
    telas[i].ok                  = true;
    strncpy(telas[i].status, "idle", sizeof telas[i].status - 1);
    telas[i].status[sizeof telas[i].status - 1] = '\0';
  }

  telaIniciar();
#if TFT_PIN_BRILHO >= 0
  pinMode(TFT_PIN_BRILHO, OUTPUT);
  analogWrite(TFT_PIN_BRILHO, (int)(255L * BRILHO_PCT_PADRAO / 100));
#endif

  // Pinta o estado inicial: oito telas "VAZIO" e sem trava. Tela apagada no
  // boot faria o operador achar que a placa nao subiu.
  for (uint8_t i = 0; i < NUM_TELAS; i++) pintarSlot(i);

  logMsg("BOOT", "pronto: %d telas, %d imagens. Aguardando `slot` e `estado_celula`.",
       NUM_TELAS, NUM_IMAGENS);

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
