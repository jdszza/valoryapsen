// ================================================================
//   BALANÇA 4 PONTOS — HX711 no ESP32
//   + MÓDULO DE CONTAGEM POR PESO COM TOLERÂNCIAS
//   + SAÍDA LEGÍVEL POR MÁQUINA (protocolo serial APSEN)
//   + ILUMINAÇÃO DOURADA (LEDs endereçáveis no GPIO 15)
// ================================================================
//
//   v2.5 = v2.4 com a MEDIÇÃO reescrita. O protocolo serial (todas as linhas
//   '{...}', campos e nomes de evento) e os comandos de uma letra continuam
//   os mesmos. O que mudou, e por quê:
//
//   1) Os 4 HX711 são lidos EM PARALELO. Na 2.4 cada canal era lido em
//      sequência com mediana de 7 amostras: a 10 SPS são ~0,7 s por canal e
//      ~2,8 s por total, e os quatro cantos eram medidos em INSTANTES
//      DIFERENTES — com o objeto chegando no meio, o total misturava antes e
//      depois. Agora um "quadro" é uma conversão nova de cada canal, colhida
//      assim que fica pronta, e o loop nunca fica parado esperando.
//
//   2) Filtro sem atraso escondido. A média móvel de 8 sobre leituras de 2,8 s
//      levava ~20 s para convergir, e a estabilidade comparava só duas
//      leituras seguidas da média — a mesa parecia "estável" enquanto o valor
//      ainda subia. Agora: mediana de 3 por canal (tira pico isolado) e média
//      da maior sequência RECENTE de quadros cujo total varia no máximo o
//      limiar `h`. Degrau real encurta a média na hora; mesa parada acumula até
//      40 quadros. Estável = essa sequência tem >= `r` quadros e >= 400 ms.
//
//   3) Leitura com erro não vira peso. Na 2.4 um HX711 que não respondia em
//      500 ms devolvia 0, e esse 0 entrava na média como leitura válida
//      (= -offset, centenas de gramas de erro). A saturação NEGATIVA nunca era
//      detectada (comparação depois da extensão de sinal). Agora canal mudo ou
//      saturado é sinalizado (erro_balanca / erro_sensor) e não mede.
//
//   4) Calibração da MESA, não de cada canto isolado. Numa mesa rígida o peso
//      se divide entre as 4 células, então `c<n>` (peso "em cima" de uma
//      célula) erra o fator daquela célula. O novo `K` mede o mesmo peso em
//      várias posições e resolve os 4 fatores por mínimos quadrados — o total
//      fica certo em qualquer ponto da mesa. `E` ajusta só a escala geral e
//      `R` mede o ruído real para escolher `h` e saber se a contagem é segura.
//
//   5) Contagem que espera o que foi pedido. Na 2.4 `k` capturava a tara
//      imediatamente (antes de o pote estar na mesa) e `x` aceitava a primeira
//      "estabilidade" (muitas vezes a mesa ainda vazia). Agora `k` espera a
//      mesa parar com algo em cima e `x` espera chegar pelo menos meio item.
//      A contagem arredonda para o inteiro MAIS PRÓXIMO; fora da tolerância
//      o status e o aceite continuam dizendo isso.
//
//   6) `tara` e `pesar` do APSEN esperam quadros NOVOS e estáveis depois do
//      comando, em vez de usar um "estável" que podia ser de antes da descarga.
//
//     Célula 1 (c0): SCK 19 / DT 27
//     Célula 2 (c1): SCK 33 / DT 26
//     Célula 3 (c2): SCK 18 / DT 14
//     Célula 4 (c3): SCK 32 / DT 25
//
//   A fita roda numa tarefa própria no core 0, independente da medição.
//   Requer a biblioteca "Adafruit NeoPixel".
//
//   v2.3 = v2.2 + uma segunda VOZ na mesma porta serial.
//
//   Tudo que a 2.2 imprimia para humano continua saindo, e todos os
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
#include <Adafruit_NeoPixel.h>

#define FW_VERSION "2.5"

// ======================== CONFIG ========================
const int N = 4;
//                        cel1 cel2 cel3 cel4
const int HX_DOUT[N] = {  27,  26,  14,  25 };
const int HX_SCK [N] = {  19,  33,  18,  32 };

// ======================== LEDS ========================
#define LED_PIN        15
#define LED_COUNT      82
// Teto global de brilho (0-255). Muitos LEDs em dourado cheio passam de 3 A;
// 160 mantem o efeito vivo sem exigir uma fonte enorme.
#define LED_BRILHO_MAX 160
// Dourado "base" e o tom mais claro que o brilho que percorre a fita atinge.
static const uint8_t OURO_R = 255, OURO_G = 140, OURO_B = 10;
static const uint8_t LUZ_R  = 255, LUZ_G  = 215, LUZ_B  = 110;

Adafruit_NeoPixel fita(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

// ======================== AQUISICAO ========================
#define HIST_MAX          40     // quadros guardados: teto da media (4 s a 10 SPS)
#define ESTAB_MIN_MS     400     // estavel exige pelo menos isto de mesa parada
#define CANAL_MUDO_MS    600     // a 10 SPS o HX711 entrega a cada 100 ms
#define QUADRO_TIMEOUT_MS 1000   // leitura bloqueante (tara/calibracao)
#define TARA_QUADROS      30     // quadros medios na tara dos canais
#define CAL_QUADROS       30     // quadros medios por posicao na calibracao
#define CAL_MAX_POS        8     // posicoes na calibracao completa
#define CAL_ASSENTAR_MS 1500     // espera o peso parar de balancar antes de medir

// ======================== CONTAGEM ========================
#define TARA_REC_ESPERA_MS  10000  // `k` sem nada na mesa: aceita zero depois disto
#define CONTAGEM_TIMEOUT_MS 30000  // `k` e `x` desistem depois disto

// ======================== TIPOS ========================
struct HX711Channel {
  int   dout_pin;
  int   sck_pin;
  bool  active;
  float calib_factor;   // contagens por grama
  long  offset_raw;
  long  last_raw;
  bool  saturated;
};

// Qualidade de uma medicao parada (tara/calibracao). Separa tres coisas que
// um pico-a-pico so misturava:
//   ruido     — quanto cada quadro oscila (eletrico, normal, cai na media);
//   picos     — quadros isolados absurdos (descartados, nao entram na media);
//   tendencia — a media da 1a metade difere da 2a: ISSO e mesa se mexendo.
struct Qualidade {
  float ruidoG;          // desvio do total por quadro (robusto: 1,4826 x MAD)
  float tendenciaG;      // |media 2a metade - media 1a metade| do total
  float limiteTendG;     // tendencia acima disto = movimento real
  int   picos;           // quadros descartados
  int   usados;          // quadros que entraram na media
  float ruidoCanalG[N];  // desvio por canal, para apontar o canal ruidoso
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
  { HX_DOUT[0], HX_SCK[0], true, 406.5217, 0, 0, false },  // celula 1
  { HX_DOUT[1], HX_SCK[1], true, 399.7391, 0, 0, false },  // celula 2
  { HX_DOUT[2], HX_SCK[2], true, 400.1739, 0, 0, false },  // celula 3
  { HX_DOUT[3], HX_SCK[3], true, 404.4348, 0, 0, false }   // celula 4
};

Preferences prefs;

float channelWeights[N];
float currentTotal = 0.0f;
bool  totalReady   = false;

CountState   countState       = CountState::IDLE;
CountConfig  countConfig      = {0, 0, 0, 1, 9999, 3, 0.5f};
CountOutput  lastCount        = {0, 0, 0, 0, CountResult::INVALID_WEIGHT, false, 0};
bool         autoPrint        = false;

// Diagnostico de deriva (comando D): uma linha por segundo com cada canto.
// Modo manutencao (`manut` ... `manutf`): para stream, ping, telemetria,
// contagem e comandos do PC; so a calibracao por celula funciona.
static bool          modoManut     = false;
static bool          emitirEmManut = false;   // libera UMA linha JSON em manut

// Modo console: uma PESSOA esta no Monitor Serial (chegou linha sem '{' — o
// adapter so manda JSON). As linhas de maquina {...} deixam de sair para nao
// poluir a tela. Voltam sozinhas quando chega JSON (o adapter esta ai), com
// o comando `json`, ou apos 5 min sem nada digitado — senao o adapter, que
// descobre a porta pelo ping, nunca acharia a balanca.
#define CONSOLE_TIMEOUT_MS 300000UL
static bool          modoConsole     = false;
static unsigned long consoleUltimoMs = 0;

static bool          logDeriva    = false;
static unsigned long logDerivaT0  = 0;
static unsigned long logDerivaUlt = 0;

// Fita ligada/desligada (comando L). Lida pela tarefa do core 0.
static volatile bool ledsLigados  = true;

// Quando e em qual quadro o estado atual comecou: `k` e `x` so aceitam
// medicao feita DEPOIS do pedido.
static unsigned long estadoDesdeMs  = 0;
static uint32_t      estadoDesdeSeq = 0;

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

// Quanto `tara` e `pesar` esperam a mesa parar antes de medir. Depois do ACK,
// entao o relogio que corre do outro lado e o TIMEOUT_PESO do orquestrador
// (15 s). 5 s cobre a mesa balancando depois de uma descarga.
static const unsigned long PESAR_ESTAB_TIMEOUT_MS = 5000;

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

// Estabilidade da mesa, calculada a cada quadro pela aquisicao. E so
// observacao: contagem, stream e APSEN leem, ninguem escreve.
static bool  streamEstavel  = false;

// Saturacao e canal mudo: o aviso sai na TRANSICAO, nao a cada leitura. Um
// evento por leitura encheria a linha de 115200 baud que os ACKs do adapter
// estao esperando.
static bool satAnterior[N] = { false, false, false, false };
static bool canalMudo[N]   = { false, false, false, false };

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
void entrarConsole();
void sairConsole(bool avisar);

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
}

void loadCalibration() {
  prefs.begin("hx711", true);
  for (int i = 0; i < N; i++) {
    float f = prefs.getFloat(("cal" + String(i)).c_str(), ch[i].calib_factor);
    // Fator zero/negativo na NVS e lixo (dividiria por zero ou inverteria o
    // canal): fica o padrao do codigo e o operador e avisado.
    if (f > 1.0f) ch[i].calib_factor = f;
    else Serial.printf("AVISO: fator invalido na NVS para c%d (%.4f) — usando %.4f.\n",
                       i, f, ch[i].calib_factor);
    ch[i].offset_raw   = prefs.getLong(("off" + String(i)).c_str(), 0);
    ch[i].active       = prefs.getBool(("act" + String(i)).c_str(), ch[i].active);
  }
  prefs.end();
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
}

// ================================================================
//   HX711 BAIXO NIVEL
// ================================================================
// Um mux so para todos: a secao critica existe para nenhuma interrupcao
// esticar o pulso de SCK acima de 60 us (o HX711 entende isso como
// power-down e a leitura sai deslocada).
static portMUX_TYPE hxMux = portMUX_INITIALIZER_UNLOCKED;

inline bool hx_ready(int i) {
  return digitalRead(ch[i].dout_pin) == LOW;
}

// So chamar com `hx_ready(i)` verdadeiro. Nao espera nada: quem decide
// quando ler e a aquisicao, que nunca para o loop.
long hx_ler(int i) {
  long v = 0;
  portENTER_CRITICAL(&hxMux);
  for (int b = 0; b < 24; b++) {
    digitalWrite(ch[i].sck_pin, HIGH);
    delayMicroseconds(1);
    v = (v << 1) | (digitalRead(ch[i].dout_pin) ? 1 : 0);
    digitalWrite(ch[i].sck_pin, LOW);
    delayMicroseconds(1);
  }
  // 25o pulso: proxima conversao no canal A, ganho 128.
  digitalWrite(ch[i].sck_pin, HIGH);
  delayMicroseconds(1);
  digitalWrite(ch[i].sck_pin, LOW);
  delayMicroseconds(1);
  portEXIT_CRITICAL(&hxMux);
  if (v & 0x800000) v |= ~0xFFFFFFL;
  ch[i].last_raw = v;
  // Os dois extremos do conversor. Na 2.4 o negativo era comparado contra
  // +0x800000 DEPOIS da extensao de sinal e por isso nunca batia.
  ch[i].saturated = (v >= 0x7FFFFFL || v <= -0x800000L);
  return v;
}

// ================================================================
//   AQUISICAO E FILTRO
// ================================================================
//
// Um QUADRO = uma conversao nova de cada canal ativo. Os quatro HX711
// convertem sozinhos; cada um e lido assim que avisa que tem dado, e o quadro
// fecha quando todos entregaram. Assim os quatro cantos sao do MESMO
// instante (dentro de um periodo de conversao).
//
// Filtro, em duas etapas:
//   1) mediana de 3 por canal: tira um pico isolado e atrasa um degrau real
//      em no maximo um quadro;
//   2) media da maior CAUDA de quadros recentes cujo total nao varia mais que
//      o limiar `h`. Objeto chegou -> a cauda encolhe para 1 e o valor
//      acompanha na hora; mesa parada -> a cauda cresce ate HIST_MAX e o
//      ruido cai com a raiz do numero de quadros.
// Estavel = cauda com pelo menos `r` quadros (minimo 3) E 400 ms.

static long          pendRaw[N];
static bool          pendOk[N];
static unsigned long ultAmostraMs[N];

static long  med3[N][3];
static int   med3n[N];

static float         histG[HIST_MAX][N];   // gramas por canal, ja sem pico
static float         histTot[HIST_MAX];
static unsigned long histMs[HIST_MAX];
static int           histN   = 0;
static int           histIdx = 0;          // proxima posicao a escrever

static uint32_t quadroSeq = 0;             // quadros fechados desde o boot
static int      caudaLen  = 0;             // quadros na media atual

// Ruido do total (desvio por quadro) medido na ultima tara ou `R`.
static float ruidoTotalG = 0.0f;

// Limiar usado de fato na estabilidade: o `h` configurado, mas nunca menor
// que 4x o ruido real da mesa. Sem isso, uma mesa com 0,4 g de ruido e
// h = 0,5 nunca ficava "estavel" — e `k`, `x` e `pesar` esperavam a toa.
static float limiarEfetivo() {
  return fmaxf(countConfig.stability_threshold_g, 4.0f * ruidoTotalG);
}

static int janelaEstab() {
  int w = countConfig.stabilization_reads;
  if (w < 3) w = 3;
  if (w > HIST_MAX) w = HIST_MAX;
  return w;
}

// Estavel E com pelo menos uma janela de quadros medidos depois de `seq`.
static bool estavelDesde(uint32_t seq) {
  return streamEstavel && (quadroSeq - seq) >= (uint32_t)janelaEstab();
}

static long mediana3(long a, long b, long c) {
  if (a > b) { long t = a; a = b; b = t; }
  if (b > c) b = c;
  return a > b ? a : b;
}

// Comprimento da maior cauda (os quadros MAIS RECENTES) cujo total varia no
// maximo `limiar`. `tot` vai do mais antigo ao mais novo. Funcao pura: e ela
// que os testes T12-T14 exercitam.
int caudaEstavel(const float *tot, int n, float limiar) {
  if (n <= 0) return 0;
  float mn = tot[n - 1], mx = tot[n - 1];
  int len = 1;
  for (int k = n - 2; k >= 0; k--) {
    float v = tot[k];
    float nmn = v < mn ? v : mn;
    float nmx = v > mx ? v : mx;
    if (nmx - nmn > limiar) break;
    mn = nmn; mx = nmx; len++;
  }
  return len;
}

// Esquece tudo que foi medido. Obrigatorio depois de mudar offset, fator ou
// canais ativos: misturar quadros de antes e depois daria um peso que nao e
// nem um nem outro.
void reiniciarFiltro() {
  unsigned long agora = millis();
  histN = 0; histIdx = 0; caudaLen = 0;
  streamEstavel = false;
  for (int i = 0; i < N; i++) {
    pendOk[i] = false;
    med3n[i] = 0;
    ultAmostraMs[i] = agora;
    canalMudo[i] = false;
  }
}

static void verificarSaturacao(int i) {
  if (ch[i].saturated) {
    if (!satAnterior[i]) {
      satAnterior[i] = true;
      Serial.printf("AVISO: Canal %d SATURADO!\n", i);
      char msg[48];
      snprintf(msg, sizeof msg, "canal %d saturado", i);
      emitErroBalanca(msg, NULL);
    }
  } else {
    satAnterior[i] = false;
  }
}

// O motivo pelo qual a mesa NAO pode medir agora, ou NULL se pode.
static const char *problemaSensor() {
  for (int i = 0; i < N; i++) {
    if (!ch[i].active) continue;
    if (canalMudo[i])      return "canal sem resposta";
    if (ch[i].saturated)   return "canal saturado";
  }
  return NULL;
}

static void processarQuadro() {
  float g[N];
  float tot = 0.0f;
  for (int i = 0; i < N; i++) {
    pendOk[i] = false;
    if (!ch[i].active) { g[i] = 0.0f; continue; }
    long *h = med3[i];
    h[0] = h[1]; h[1] = h[2]; h[2] = pendRaw[i];
    if (med3n[i] < 3) med3n[i]++;
    long m = (med3n[i] < 3) ? pendRaw[i] : mediana3(h[0], h[1], h[2]);
    g[i] = (float)(m - ch[i].offset_raw) / ch[i].calib_factor;
    tot += g[i];
  }

  unsigned long agora = millis();
  for (int i = 0; i < N; i++) histG[histIdx][i] = g[i];
  histTot[histIdx] = tot;
  histMs[histIdx]  = agora;
  histIdx = (histIdx + 1) % HIST_MAX;
  if (histN < HIST_MAX) histN++;
  quadroSeq++;

  // Copia linear (antigo -> novo) para a funcao pura da cauda.
  float linear[HIST_MAX];
  int antigo = (histIdx - histN + HIST_MAX) % HIST_MAX;
  for (int k = 0; k < histN; k++) linear[k] = histTot[(antigo + k) % HIST_MAX];
  caudaLen = caudaEstavel(linear, histN, limiarEfetivo());

  double soma[N] = {0};
  unsigned long inicioMs = agora;
  for (int k = 0; k < caudaLen; k++) {
    int p = (histIdx - 1 - k + HIST_MAX) % HIST_MAX;
    for (int i = 0; i < N; i++) soma[i] += histG[p][i];
    inicioMs = histMs[p];
  }
  currentTotal = 0.0f;
  for (int i = 0; i < N; i++) {
    channelWeights[i] = ch[i].active ? (float)(soma[i] / caudaLen) : 0.0f;
    currentTotal += channelWeights[i];
  }

  streamEstavel = (problemaSensor() == NULL) &&
                  caudaLen >= janelaEstab() &&
                  (agora - inicioMs) >= ESTAB_MIN_MS;
  totalReady = true;
}

// Chamado o tempo todo pelo loop. Nunca espera: so le quem ja esta pronto.
void bombearAquisicao() {
  unsigned long agora = millis();
  bool algumAtivo = false;
  bool completo   = true;
  for (int i = 0; i < N; i++) {
    if (!ch[i].active) continue;
    algumAtivo = true;
    if (hx_ready(i)) {
      // Se ja havia amostra pendente, a mais nova substitui: o quadro fica
      // sempre com o dado mais fresco de cada canal.
      pendRaw[i] = hx_ler(i);
      pendOk[i]  = true;
      ultAmostraMs[i] = agora;
      verificarSaturacao(i);
      if (canalMudo[i]) {
        canalMudo[i] = false;
        Serial.printf("Canal %d voltou a responder.\n", i);
      }
    } else if (!canalMudo[i] && agora - ultAmostraMs[i] > CANAL_MUDO_MS) {
      canalMudo[i] = true;
      streamEstavel = false;
      Serial.printf("AVISO: Canal %d SEM RESPOSTA (verifique DT/SCK/alimentacao).\n", i);
      char msg[48];
      snprintf(msg, sizeof msg, "canal %d sem resposta", i);
      emitErroBalanca(msg, NULL);
    }
    if (!pendOk[i]) completo = false;
  }
  if (algumAtivo && completo) processarQuadro();
}

// Leitura BLOQUEANTE de um quadro bruto, para tara e calibracao (que ja sao
// interativas e param o loop de qualquer jeito).
static bool lerQuadroBloqueante(long raw[N]) {
  bool tem[N];
  for (int i = 0; i < N; i++) tem[i] = !ch[i].active;
  unsigned long t0 = millis();
  for (;;) {
    bool todos = true;
    for (int i = 0; i < N; i++) {
      if (tem[i]) continue;
      if (hx_ready(i)) { raw[i] = hx_ler(i); tem[i] = true; }
      else todos = false;
    }
    if (todos) return true;
    if (millis() - t0 > QUADRO_TIMEOUT_MS) {
      for (int i = 0; i < N; i++)
        if (!tem[i]) Serial.printf("ERRO: canal %d nao respondeu.\n", i);
      return false;
    }
    delay(1);
  }
}

#define MEDIA_MAX_QUADROS 40

static void ordenar(float *v, int n) {
  for (int a = 1; a < n; a++) {
    float key = v[a];
    int b = a - 1;
    while (b >= 0 && v[b] > key) { v[b + 1] = v[b]; b--; }
    v[b + 1] = key;
  }
}

// Media robusta de `n` quadros brutos (contagens) por canal.
static bool mediaBruta(int n, double media[N], Qualidade *q) {
  if (n > MEDIA_MAX_QUADROS) n = MEDIA_MAX_QUADROS;
  static long buf[MEDIA_MAX_QUADROS][N];
  float tot[MEDIA_MAX_QUADROS];
  long raw[N];
  // Descarta 2 quadros: a conversao que estava esperando pode ser anterior
  // ao pedido.
  for (int k = 0; k < 2; k++) if (!lerQuadroBloqueante(raw)) return false;
  bool sat = false;
  for (int k = 0; k < n; k++) {
    if (!lerQuadroBloqueante(raw)) return false;
    tot[k] = 0.0f;
    for (int i = 0; i < N; i++) {
      buf[k][i] = raw[i];
      if (!ch[i].active) continue;
      tot[k] += (float)(raw[i] - ch[i].offset_raw) / ch[i].calib_factor;
      if (ch[i].saturated) sat = true;
    }
  }
  if (sat) {
    Serial.println(F("ERRO: canal saturado durante a medicao."));
    return false;
  }

  // Mediana e MAD do total: nao se deixam levar por um pico isolado.
  float ord[MEDIA_MAX_QUADROS];
  for (int k = 0; k < n; k++) ord[k] = tot[k];
  ordenar(ord, n);
  float med = ord[n / 2];
  for (int k = 0; k < n; k++) ord[k] = fabsf(tot[k] - med);
  ordenar(ord, n);
  float sigma = 1.4826f * ord[n / 2];
  float corte = fmaxf(5.0f * sigma, 0.05f);

  double soma[N] = {0}, soma2[N] = {0};
  double meia[2] = {0, 0};
  int    nMeia[2] = {0, 0};
  int usados = 0;
  for (int k = 0; k < n; k++) {
    if (fabsf(tot[k] - med) > corte) continue;
    usados++;
    for (int i = 0; i < N; i++) {
      if (!ch[i].active) continue;
      soma[i] += buf[k][i];
      double g = (double)(buf[k][i] - ch[i].offset_raw) / ch[i].calib_factor;
      soma2[i] += g * g;
    }
    int h = (k < n / 2) ? 0 : 1;
    meia[h] += tot[k];
    nMeia[h]++;
  }
  if (usados < n / 2) {
    Serial.printf("ERRO: %d de %d quadros eram picos — leitura instavel/mau contato.\n",
                  n - usados, n);
    return false;
  }
  for (int i = 0; i < N; i++) media[i] = ch[i].active ? soma[i] / usados : 0.0;

  if (q) {
    q->ruidoG = sigma;
    q->picos  = n - usados;
    q->usados = usados;
    q->tendenciaG = (nMeia[0] && nMeia[1])
        ? fabsf((float)(meia[1] / nMeia[1] - meia[0] / nMeia[0])) : 0.0f;
    // Diferenca entre duas medias de n/2 quadros, so de ruido, tem desvio
    // 2*sigma/sqrt(n). Quatro desvios disso ainda e ruido; acima, e tendencia.
    q->limiteTendG = fmaxf(4.0f * 2.0f * sigma / sqrtf((float)usados), 0.05f);
    for (int i = 0; i < N; i++) {
      if (!ch[i].active) { q->ruidoCanalG[i] = 0; continue; }
      double mu = ((double)soma[i] / usados - ch[i].offset_raw) / ch[i].calib_factor;
      q->ruidoCanalG[i] = (float)sqrt(fmax(0.0, soma2[i] / usados - mu * mu));
    }
  }
  return true;
}

static void imprimirQualidade(const Qualidade &q) {
  Serial.printf("  ruido %.3f g | tendencia %.3f g (limite %.3f) | picos %d\n",
                q.ruidoG, q.tendenciaG, q.limiteTendG, q.picos);
  Serial.print(F("  ruido por canal:"));
  for (int i = 0; i < N; i++)
    if (ch[i].active) Serial.printf("  c%d=%.3f", i, q.ruidoCanalG[i]);
  Serial.println();
}

// ================================================================
//   OPERACOES POR CANAL
// ================================================================
void tareChannel(int i) {
  if (!ch[i].active) return;
  double media[N];
  if (!mediaBruta(TARA_QUADROS, media, NULL)) {
    Serial.printf("ERRO: tara do canal %d falhou.\n", i);
    return;
  }
  ch[i].offset_raw = lround(media[i]);
  Serial.printf("Tara c%d: offset=%ld\n", i, ch[i].offset_raw);
  reiniciarFiltro();
}

// Devolve false se nao conseguiu medir (canal mudo/saturado). Nesse caso os
// offsets antigos ficam, e o motivo sai como `erro_balanca`.
bool tareAll() {
  Serial.println(F("Tara: mesa VAZIA e parada..."));
  double media[N];
  Qualidade q;
  if (!mediaBruta(TARA_QUADROS, media, &q)) {
    Serial.println(F("ERRO: tara NAO feita — offsets anteriores mantidos."));
    emitErroBalanca("tara falhou: canal sem resposta ou saturado", NULL);
    reiniciarFiltro();
    return false;
  }
  for (int i = 0; i < N; i++)
    if (ch[i].active) ch[i].offset_raw = lround(media[i]);
  saveCalibration();
  ruidoTotalG = q.ruidoG;
  reiniciarFiltro();

  // Detalhes so quando ha algo a ver: leitura andando, picos, ou um canal
  // bem mais ruidoso que os outros. Tara normal = uma linha.
  float ruidos[N];
  int nAtivos = 0;
  for (int i = 0; i < N; i++) if (ch[i].active) ruidos[nAtivos++] = q.ruidoCanalG[i];
  ordenar(ruidos, nAtivos);
  float ruidoTipico = nAtivos ? ruidos[nAtivos / 2] : 0.0f;
  bool canalRuidoso = false;
  for (int i = 0; i < N; i++)
    if (ch[i].active && q.ruidoCanalG[i] > fmaxf(3.0f * ruidoTipico, 0.1f)) canalRuidoso = true;

  bool andou = q.tendenciaG > q.limiteTendG;
  if (andou)
    Serial.println(F("AVISO: a leitura andou durante a tara (algo encostando/assentando?)."));
  if (andou || q.picos > 2 || canalRuidoso)
    imprimirQualidade(q);
  Serial.printf("TARA OK (ruido %.2f g).\n", q.ruidoG);
  emitTaraBalanca("canais", NAN);
  return true;
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

// Como `readSerialLine`, mas distingue ENTER vazio (true, "") de tempo
// esgotado (false) — a calibracao precisa saber a diferenca.
// Linhas com '{' (vindas do PC) sao ignoradas aqui: nao podem virar resposta
// do operador no meio de uma calibracao.
static bool lerLinhaUsuario(String &out, unsigned long timeout_ms = 120000) {
  unsigned long t0 = millis();
  for (;;) {
    while (!Serial.available()) {
      if (millis() - t0 > timeout_ms) return false;
      delay(10);
    }
    delay(50);
    out = Serial.readStringUntil('\n');
    out.trim();
    if (out.indexOf('{') < 0) { consoleUltimoMs = millis(); return true; }
  }
}

// ================================================================
//   CALIBRACAO
// ================================================================

// Minimos quadrados: acha k (g/contagem) de cada canal em `usar` tal que
// soma_i k_i * A[r][i] ~= W em todas as linhas r. Equacoes normais + Gauss
// com pivoteamento parcial; devolve false se o sistema for singular (as
// posicoes nao distinguem os canais). Funcao pura: T15 a exercita.
bool resolverCalibracao(const double A[][N], int linhas, const bool usar[N],
                        double W, double k[N]) {
  int idx[N], m = 0;
  for (int i = 0; i < N; i++) if (usar[i]) idx[m++] = i;
  if (m == 0 || linhas < m) return false;

  double M[N][N + 1];
  for (int a = 0; a < m; a++)
    for (int b = 0; b <= m; b++) M[a][b] = 0.0;
  for (int r = 0; r < linhas; r++)
    for (int a = 0; a < m; a++) {
      for (int b = 0; b < m; b++) M[a][b] += A[r][idx[a]] * A[r][idx[b]];
      M[a][m] += A[r][idx[a]] * W;
    }

  double escala = 0.0;
  for (int a = 0; a < m; a++) if (fabs(M[a][a]) > escala) escala = fabs(M[a][a]);
  if (escala <= 0.0) return false;

  for (int c = 0; c < m; c++) {
    int piv = c;
    for (int r = c + 1; r < m; r++) if (fabs(M[r][c]) > fabs(M[piv][c])) piv = r;
    if (fabs(M[piv][c]) < 1e-9 * escala) return false;
    if (piv != c)
      for (int b = 0; b <= m; b++) { double t = M[c][b]; M[c][b] = M[piv][b]; M[piv][b] = t; }
    for (int r = c + 1; r < m; r++) {
      double f = M[r][c] / M[c][c];
      for (int b = c; b <= m; b++) M[r][b] -= f * M[c][b];
    }
  }
  double x[N];
  for (int a = m - 1; a >= 0; a--) {
    double s = M[a][m];
    for (int b = a + 1; b < m; b++) s -= M[a][b] * x[b];
    x[a] = s / M[a][a];
  }
  for (int i = 0; i < N; i++) k[i] = 0.0;
  for (int a = 0; a < m; a++) k[idx[a]] = x[a];
  return true;
}

// Mede uma posicao: espera assentar e faz a media robusta. Recusa so se a
// leitura estiver ANDANDO (tendencia), nunca por ruido — ruido some na media.
// Depois de 3 tentativas, mostra os numeros e o operador decide.
static bool medirPosicao(double liquido[N], float W) {
  double media[N];
  Qualidade q;
  for (int tentativa = 0; tentativa < 3; tentativa++) {
    delay(CAL_ASSENTAR_MS);
    if (!mediaBruta(CAL_QUADROS, media, &q)) return false;
    // 0,1% do peso de referencia tambem e aceitavel como "parado".
    float limite = fmaxf(q.limiteTendG, 0.001f * W);
    if (q.tendenciaG <= limite) {
      for (int i = 0; i < N; i++)
        liquido[i] = ch[i].active ? media[i] - ch[i].offset_raw : 0.0;
      if (q.ruidoG > 0.005f * W) {
        Serial.println(F("  AVISO: ruido alto para este peso de referencia:"));
        imprimirQualidade(q);
      }
      return true;
    }
    Serial.printf("  leitura andando %.3f g (limite %.3f) — medindo de novo...\n",
                  q.tendenciaG, limite);
  }
  Serial.println(F("  A leitura continua andando. Detalhes:"));
  imprimirQualidade(q);
  Serial.println(F("  Usar esta medicao mesmo assim? 's' + ENTER = sim, ENTER = repetir:"));
  String s;
  flushSerial();
  if (lerLinhaUsuario(s, 60000) && (s == "s" || s == "S")) {
    for (int i = 0; i < N; i++)
      liquido[i] = ch[i].active ? media[i] - ch[i].offset_raw : 0.0;
    return true;
  }
  return false;
}

// Calibracao da MESA (comando K). O peso, em cada posicao, se divide entre as
// celulas de um jeito diferente; com posicoes variadas o sistema separa o
// fator de cada uma, e o total fica certo em qualquer ponto da mesa.
void calibracaoCompleta() {
  bool usar[N];
  int m = 0;
  for (int i = 0; i < N; i++) { usar[i] = ch[i].active; if (usar[i]) m++; }
  if (m == 0) { Serial.println(F("Nenhum canal ativo.")); return; }

  String s;
  Serial.println(F("\n=== CALIBRACAO COMPLETA DA MESA ==="));
  Serial.println(F("Use UM peso conhecido, de preferencia >= 20% da capacidade."));
  Serial.println(F("Digite 'q' + ENTER em qualquer passo para cancelar."));
  Serial.println(F("1) Retire TUDO da mesa e digite ENTER:"));
  flushSerial();
  if (!lerLinhaUsuario(s) || s == "q") { Serial.println(F("Cancelada.")); return; }
  if (!tareAll()) return;

  Serial.println(F("2) Digite o valor do peso de referencia em gramas + ENTER:"));
  if (!lerLinhaUsuario(s) || s == "q") { Serial.println(F("Cancelada.")); return; }
  float W = s.toFloat();
  if (W <= 0.0f) { Serial.println(F("Valor invalido. Cancelada.")); return; }

  static double A[CAL_MAX_POS][N];
  char nomes[CAL_MAX_POS][32];
  int linhas = 0;

  // Roteiro: centro, depois em cima de cada celula ativa, depois extras.
  int roteiro[1 + N];
  int nRoteiro = 0;
  roteiro[nRoteiro++] = -1;                       // -1 = centro
  for (int i = 0; i < N; i++) if (usar[i]) roteiro[nRoteiro++] = i;

  Serial.println(F("3) Coloque o peso em cada posicao pedida, espere parar e digite ENTER."));
  Serial.println(F("   Depois das posicoes pedidas: mais posicoes melhoram, 'f' termina."));
  while (linhas < CAL_MAX_POS) {
    char nome[32];
    if (linhas < nRoteiro) {
      if (roteiro[linhas] < 0) snprintf(nome, sizeof nome, "CENTRO da mesa");
      else snprintf(nome, sizeof nome, "canto da celula %d (c%d)",
                    roteiro[linhas] + 1, roteiro[linhas]);
    } else {
      snprintf(nome, sizeof nome, "posicao extra %d", linhas - nRoteiro + 1);
    }
    Serial.printf("   -> Peso no %s e ENTER%s:\n", nome,
                  linhas >= nRoteiro ? " (ou 'f' para terminar)" : "");
    if (!lerLinhaUsuario(s) || s == "q") { Serial.println(F("Cancelada.")); reiniciarFiltro(); return; }
    if (s == "f") {
      if (linhas >= m) break;
      Serial.printf("   Ainda faltam posicoes (minimo %d).\n", m);
      continue;
    }
    if (!medirPosicao(A[linhas], W)) {
      Serial.println(F("   Posicao descartada — tente de novo."));
      continue;
    }
    strncpy(nomes[linhas], nome, sizeof nomes[linhas]);
    nomes[linhas][sizeof nomes[linhas] - 1] = '\0';
    linhas++;
  }

  double k[N];
  if (!resolverCalibracao(A, linhas, usar, W, k)) {
    Serial.println(F("ERRO: as posicoes nao distinguem os canais (sistema singular)."));
    Serial.println(F("Coloque o peso BEM em cima de cada celula. Nada foi alterado."));
    reiniciarFiltro();
    return;
  }

  // Sanidade antes de gravar qualquer coisa.
  float novo[N];
  float soma = 0.0f;
  for (int i = 0; i < N; i++) {
    if (!usar[i]) continue;
    if (k[i] <= 0.0) {
      Serial.printf("ERRO: c%d deu fator negativo/zero — celula com sinal INVERTIDO\n", i);
      Serial.println(F("(troque A+ com A- desse HX711) ou fiacao errada. Nada foi alterado."));
      reiniciarFiltro();
      return;
    }
    novo[i] = (float)(1.0 / k[i]);
    soma += novo[i];
  }
  float medio = soma / m;
  for (int i = 0; i < N; i++) {
    if (!usar[i]) continue;
    if (novo[i] < 0.5f * medio || novo[i] > 2.0f * medio) {
      Serial.printf("ERRO: fator de c%d (%.2f) muito diferente dos outros (media %.2f).\n",
                    i, novo[i], medio);
      Serial.println(F("Verifique essa celula/posicoes. Nada foi alterado."));
      reiniciarFiltro();
      return;
    }
  }

  Serial.println(F("\nResultado:"));
  for (int i = 0; i < N; i++) {
    if (!usar[i]) continue;
    Serial.printf("  c%d: %.4f -> %.4f cont/g\n", i, ch[i].calib_factor, novo[i]);
    ch[i].calib_factor = novo[i];
  }
  Serial.println(F("Conferencia (peso calculado em cada posicao medida):"));
  float piorPct = 0.0f;
  for (int r = 0; r < linhas; r++) {
    double est = 0.0;
    for (int i = 0; i < N; i++) if (usar[i]) est += A[r][i] / ch[i].calib_factor;
    float erroPct = (float)((est - W) / W * 100.0);
    if (fabsf(erroPct) > piorPct) piorPct = fabsf(erroPct);
    Serial.printf("  %-28s %9.2f g  (erro %+.3f%%)\n", nomes[r], est, erroPct);
  }
  if (piorPct > 0.5f)
    Serial.println(F("AVISO: erro > 0,5% em alguma posicao — mesa torcendo, celula com"
                     " folga ou peso mexeu. Considere repetir."));
  saveCalibration();
  reiniciarFiltro();
  Serial.println(F("Calibracao completa salva.\n"));
}

// Ajuste so da ESCALA geral (comando E): um peso conhecido em qualquer lugar.
// Corrige ganho comum a todos (ex.: peso de referencia diferente na ultima
// calibracao); nao corrige diferenca entre cantos — para isso, `K`.
void ajusteEscala() {
  String s;
  Serial.println(F("\n=== AJUSTE DE ESCALA ==="));
  Serial.println(F("1) Retire TUDO da mesa e digite ENTER ('q' cancela):"));
  flushSerial();
  if (!lerLinhaUsuario(s) || s == "q") { Serial.println(F("Cancelado.")); return; }
  if (!tareAll()) return;
  Serial.println(F("2) Coloque o peso conhecido no CENTRO, digite o valor (g) + ENTER:"));
  if (!lerLinhaUsuario(s) || s == "q") { Serial.println(F("Cancelado.")); return; }
  float W = s.toFloat();
  if (W <= 0.0f) { Serial.println(F("Valor invalido.")); return; }

  double liq[N];
  if (!medirPosicao(liq, W)) { reiniciarFiltro(); return; }
  double T = 0.0;
  for (int i = 0; i < N; i++) if (ch[i].active) T += liq[i] / ch[i].calib_factor;
  float esc = (float)(T / W);
  if (esc < 0.5f || esc > 2.0f) {
    Serial.printf("ERRO: leitura %.2f g para %.2f g — fora do plausivel. Nada alterado.\n",
                  T, W);
    reiniciarFiltro();
    return;
  }
  for (int i = 0; i < N; i++) if (ch[i].active) ch[i].calib_factor *= esc;
  saveCalibration();
  reiniciarFiltro();
  Serial.printf("Lia %.2f g, agora %.2f g (escala x%.5f). Salvo.\n", T, W, 1.0f / esc);
}

// Calibracao de UM canal (c0..c3), mantida por compatibilidade.
void calibrarCanal(int i) {
  if (i < 0 || i >= N) return;
  if (!ch[i].active) { Serial.printf("Canal %d INATIVO.\n", i); return; }
  Serial.printf("\n=== CALIBRACAO CANAL %d ===\n", i);
  Serial.println(F("ATENCAO: so vale com o peso EXATAMENTE sobre esta celula. Numa mesa"));
  Serial.println(F("rigida o peso se divide entre as celulas — prefira 'K'."));
  Serial.println(F("1) Retire o peso e digite ENTER:"));
  flushSerial();
  String s;
  if (!lerLinhaUsuario(s, 15000)) { Serial.println(F("Timeout.")); return; }
  tareChannel(i);
  Serial.println(F("2) Coloque peso conhecido, digite valor (g) + ENTER:"));
  if (!lerLinhaUsuario(s, 15000)) { Serial.println(F("Timeout.")); return; }
  float ref_g = s.toFloat();
  if (ref_g <= 0) { Serial.println(F("Valor invalido.")); return; }
  double liq[N];
  if (!medirPosicao(liq, ref_g)) { reiniciarFiltro(); return; }
  if (liq[i] <= 0) {
    Serial.println(F("Leitura nula ou negativa (sinal invertido?)."));
    reiniciarFiltro();
    return;
  }
  ch[i].calib_factor = (float)(liq[i] / ref_g);
  saveCalibration();
  reiniciarFiltro();
  Serial.printf("Fator canal %d: %.4f cont/g\n", i, ch[i].calib_factor);
}

// Ruido real da mesa (comando R): base para escolher `h` e para saber se o
// peso unitario e grande o bastante para contar sem erro.
void medirRuido() {
  const int Q = 50;
  Serial.println(F("\nMedindo ruido — mantenha a mesa PARADA..."));
  long raw[N];
  for (int k = 0; k < 2; k++) if (!lerQuadroBloqueante(raw)) { reiniciarFiltro(); return; }
  double s1[N] = {0}, s2[N] = {0}, t1 = 0, t2 = 0;
  float mn = INFINITY, mx = -INFINITY;
  unsigned long t0 = millis();
  for (int q = 0; q < Q; q++) {
    if (!lerQuadroBloqueante(raw)) { reiniciarFiltro(); return; }
    double tot = 0;
    for (int i = 0; i < N; i++) {
      if (!ch[i].active) continue;
      double g = (double)(raw[i] - ch[i].offset_raw) / ch[i].calib_factor;
      s1[i] += g; s2[i] += g * g; tot += g;
    }
    t1 += tot; t2 += tot * tot;
    if (tot < mn) mn = tot;
    if (tot > mx) mx = tot;
  }
  float sps = Q * 1000.0f / (float)(millis() - t0);
  Serial.printf("Taxa: %.1f quadros/s\n", sps);
  for (int i = 0; i < N; i++) {
    if (!ch[i].active) continue;
    double mu = s1[i] / Q;
    Serial.printf("  c%d: desvio %.3f g\n", i, sqrt(fmax(0.0, s2[i] / Q - mu * mu)));
  }
  double muT = t1 / Q;
  float sigma = (float)sqrt(fmax(0.0, t2 / Q - muT * muT));
  Serial.printf("  TOTAL: desvio %.3f g, pico-a-pico %.3f g (1 quadro)\n", sigma, mx - mn);
  float sugH = fmaxf(0.05f, 4.0f * sigma);
  Serial.printf("Sugestao de limiar: h%.2f (atual h%.2f)\n", sugH,
                countConfig.stability_threshold_g);
  if (countConfig.unit_weight_g > 0) {
    float razao = countConfig.unit_weight_g / sigma;
    Serial.printf("Peso unitario / ruido = %.1f -> contagem %s\n", razao,
                  razao >= 6.0f ? "SEGURA" : "ARRISCADA (unitario muito leve para esta mesa)");
  }
  ruidoTotalG = sigma;
  Serial.printf("Limiar efetivo de estabilidade agora: %.2f g\n", limiarEfetivo());
  reiniciarFiltro();
}

// ================================================================
//   DIAGNOSTICO GUIADO (comando V)
// ================================================================
//
// Mede o peso conhecido no centro e em cima de cada celula e diz O QUE esta
// errado: tara salva deslocada, celula invertida, cantos trocados, celula que
// nao sustenta a mesa, peso escapando por fora das celulas, mesa que nao volta
// ao zero. NAO grava nada: o zero de referencia e medido aqui, com a mesa
// vazia, e nao depende da tara salva (que pode ser justamente o problema).

void diagnosticoGuiado() {
  String s;
  Serial.println(F("\n=== DIAGNOSTICO GUIADO (nao grava nada) ==="));
  Serial.println(F("1) Retire TUDO da mesa e digite ENTER ('q' cancela):"));
  flushSerial();
  if (!lerLinhaUsuario(s) || s == "q") { Serial.println(F("Cancelado.")); return; }

  double zero[N];
  Qualidade q;
  if (!mediaBruta(CAL_QUADROS, zero, &q)) { reiniciarFiltro(); return; }
  Serial.println(F("Mesa vazia:"));
  imprimirQualidade(q);
  float desvTara = 0.0f;
  Serial.print(F("  mesa vazia agora vs tara salva [g]:"));
  for (int i = 0; i < N; i++) {
    if (!ch[i].active) continue;
    float d = (float)((zero[i] - ch[i].offset_raw) / ch[i].calib_factor);
    desvTara += d;
    Serial.printf("  c%d=%+.2f", i, d);
  }
  Serial.printf("  total=%+.2f\n", desvTara);

  Serial.println(F("2) Digite o valor do peso conhecido (g) + ENTER:"));
  if (!lerLinhaUsuario(s) || s == "q") { Serial.println(F("Cancelado.")); reiniciarFiltro(); return; }
  float W = s.toFloat();
  if (W <= 0.0f) { Serial.println(F("Valor invalido.")); reiniciarFiltro(); return; }

  int   alvo[1 + N];
  float g[1 + N][N];
  int   np = 0;
  alvo[np++] = -1;                                  // centro
  for (int i = 0; i < N; i++) if (ch[i].active) alvo[np++] = i;

  Serial.println(F("3) Em cada posicao: coloque o peso, espere parar e digite ENTER."));
  for (int p = 0; p < np; p++) {
    if (alvo[p] < 0) Serial.println(F("   -> Peso no CENTRO da mesa e ENTER:"));
    else Serial.printf("   -> Peso BEM EM CIMA da celula %d (c%d) e ENTER:\n",
                       alvo[p] + 1, alvo[p]);
    if (!lerLinhaUsuario(s) || s == "q") { Serial.println(F("Cancelado.")); reiniciarFiltro(); return; }
    delay(CAL_ASSENTAR_MS);
    double media[N];
    Qualidade qp;
    if (!mediaBruta(CAL_QUADROS, media, &qp)) { reiniciarFiltro(); return; }
    for (int i = 0; i < N; i++)
      g[p][i] = ch[i].active ? (float)((media[i] - zero[i]) / ch[i].calib_factor) : 0.0f;
  }

  Serial.println(F("4) RETIRE o peso e digite ENTER:"));
  if (!lerLinhaUsuario(s) || s == "q") { Serial.println(F("Cancelado.")); reiniciarFiltro(); return; }
  delay(CAL_ASSENTAR_MS);
  double vazia[N];
  Qualidade qv;
  if (!mediaBruta(CAL_QUADROS, vazia, &qv)) { reiniciarFiltro(); return; }
  float res[N], resTot = 0.0f;
  for (int i = 0; i < N; i++) {
    res[i] = ch[i].active ? (float)((vazia[i] - zero[i]) / ch[i].calib_factor) : 0.0f;
    resTot += res[i];
  }

  // ── Tabela ──
  Serial.println(F("\nPosicao            c0[g]    c1[g]    c2[g]    c3[g]   total[g]    erro"));
  for (int p = 0; p < np; p++) {
    if (alvo[p] < 0) Serial.print(F("centro         "));
    else Serial.printf("sobre c%d       ", alvo[p]);
    float tot = 0.0f;
    for (int i = 0; i < N; i++) {
      if (ch[i].active) Serial.printf(" %8.2f", g[p][i]);
      else              Serial.print(F("       --"));
      tot += g[p][i];
    }
    Serial.printf("  %9.2f  %+6.2f%%\n", tot, (tot - W) / W * 100.0f);
  }
  Serial.print(F("sem peso       "));
  for (int i = 0; i < N; i++) {
    if (ch[i].active) Serial.printf(" %8.2f", res[i]);
    else              Serial.print(F("       --"));
  }
  Serial.printf("  %9.2f\n", resTot);

  // ── Diagnostico ──
  Serial.println(F("\n=== DIAGNOSTICO ==="));
  int problemas = 0;
  float tolZero = fmaxf(1.0f, 6.0f * q.ruidoG);

  if (fabsf(desvTara) > tolZero) {
    problemas++;
    Serial.printf("* A TARA SALVA esta deslocada %+.2f g da mesa vazia: foi feita com algo\n"
                  "  em cima (ou no boot com peso na mesa). Por isso leituras saem %s.\n"
                  "  -> faca 't' com a mesa vazia.\n",
                  desvTara, desvTara > 0 ? "a mais" : "NEGATIVAS/a menos");
  }

  bool invertido[N] = {false, false, false, false};
  for (int p = 1; p < np; p++) {
    int i = alvo[p];
    float tot = 0.0f;
    int j = -1;
    for (int c = 0; c < N; c++) {
      if (!ch[c].active) continue;
      tot += g[p][c];
      if (j < 0 || g[p][c] > g[p][j]) j = c;
    }
    if (g[p][i] < -0.05f * W) {
      invertido[i] = true;
      problemas++;
      Serial.printf("* c%d fica NEGATIVO (%.1f g) com o peso em cima dela: celula INVERTIDA.\n"
                    "  Na remontagem a celula %d provavelmente foi presa ao contrario (veja a\n"
                    "  SETA gravada nela: deve apontar para baixo, no sentido da carga) ou\n"
                    "  A+/A- trocados nesse HX711.\n", i, g[p][i], i + 1);
    } else if (j != i && g[p][j] > 0.3f * W) {
      problemas++;
      Serial.printf("* Peso sobre a celula %d aparece no c%d: os cabos/HX711 desses cantos\n"
                    "  estao trocados, ou a celula %d nao esta sustentando a mesa.\n",
                    i + 1, j, i + 1);
    } else if (tot < 0.8f * W) {
      problemas++;
      Serial.printf("* Com o peso sobre a celula %d o total foi so %.1f g de %.1f g: parte do\n"
                    "  peso passa POR FORA das celulas (algo encostando nesse canto) ou o\n"
                    "  fator de c%d esta muito errado.\n", i + 1, tot, W, i);
    }
  }

  for (int i = 0; i < N; i++) {
    if (!ch[i].active || invertido[i]) continue;
    if (g[0][i] < -0.05f * W) {
      problemas++;
      Serial.printf("* c%d fica NEGATIVO com o peso no centro: celula invertida ou mesa\n"
                    "  empenada/apoiada em 3 pes.\n", i);
    } else if (fabsf(g[0][i]) < 0.03f * W) {
      problemas++;
      Serial.printf("* c%d quase nao recebe peso no centro (%.1f g): esse pe nao apoia a mesa\n"
                    "  (celula mais baixa, folga ou espacador faltando na remontagem).\n",
                    i, g[0][i]);
    }
  }

  if (fabsf(resTot) > fmaxf(0.005f * W, tolZero)) {
    problemas++;
    Serial.printf("* Sem o peso a mesa NAO voltou ao zero (%+.2f g): atrito, algo encostando\n"
                  "  ou celula com folga.\n", resTot);
  }

  float totCentro = 0.0f;
  for (int i = 0; i < N; i++) totCentro += g[0][i];
  float erroCentro = (totCentro - W) / W * 100.0f;

  if (problemas == 0) {
    if (fabsf(erroCentro) > 0.5f)
      Serial.printf("Mecanica e ligacoes OK. So a escala esta %+.2f%% fora: rode 'K'.\n", erroCentro);
    else
      Serial.println(F("Nenhum problema encontrado. Balanca medindo certo."));
  } else {
    Serial.println(F("\nResolva os itens acima, rode 'V' de novo ate nao sobrar nenhum,"));
    Serial.println(F("e SO ENTAO calibre com 'K'. Calibrar antes nao corrige esses defeitos."));
  }
  reiniciarFiltro();
}

// ================================================================
//   MODO MANUTENCAO (comandos `manut` e `manutf`)
// ================================================================
//
// `manut` para tudo (stream, ping, telemetria, contagem, comandos do PC) e
// abre um menu para calibrar CELULA POR CELULA: em cada uma, tara com a mesa
// vazia e peso conhecido em cima dela — tara e fator salvos na hora.
//
// O peso "em cima de uma celula" sempre escapa um pouco para as vizinhas numa
// mesa rigida. Por isso o fator de cada celula e calculado sobre o peso que
// ELA segurou (o conhecido menos o que as outras mediram), e quando as 4 estao
// feitas os 4 fatores sao refinados juntos com as mesmas medicoes. `manutf`
// devolve a balanca a operacao.

void printChannels();

static float  pesoManut = 0.0f;           // ultimo peso conhecido digitado
static bool   manutCalibrada[N];
static double manutLinha[N][N];           // contagens de cada canal, peso sobre a celula r
static float  manutW[N];
static bool   manutMudou = false;
static bool   streamAntesManut = true;

static void menuManut() {
  Serial.println(F("\n=== MODO MANUTENCAO ==="));
  Serial.println(F("  1, 2, 3, 4 = calibrar a celula (tara + peso conhecido, salva na hora)"));
  Serial.println(F("  t = tara da mesa vazia (salva)"));
  Serial.println(F("  p = leitura atual de cada celula"));
  Serial.println(F("  s = situacao das 4 celulas"));
  Serial.println(F("  ? = este menu"));
  Serial.println(F("  manutf = SAIR da manutencao"));
  Serial.println(F("Stream, ping, telemetria, contagem e comandos do PC estao PARADOS.\n"));
}

void entrarManut() {
  if (modoManut) { menuManut(); return; }
  if (countState != CountState::IDLE) setCountState(CountState::IDLE);
  // Ultimo aviso ao PC antes de a voz da maquina calar.
  emitErroBalanca("balanca em manutencao", "manut");
  streamAntesManut = streamOn;
  streamOn  = false;
  autoPrint = false;
  logDeriva = false;
  modoManut  = true;
  manutMudou = false;
  for (int i = 0; i < N; i++) manutCalibrada[i] = false;
  menuManut();
}

static void statusManut() {
  Serial.println(F("\nCelula  canal  fator[cont/g]     offset   nesta sessao"));
  for (int i = 0; i < N; i++) {
    Serial.printf("  %d      c%d    %11.4f  %9ld   %s\n", i + 1, i,
                  ch[i].calib_factor, ch[i].offset_raw,
                  !ch[i].active ? "INATIVA" : (manutCalibrada[i] ? "CALIBRADA" : "pendente"));
  }
  Serial.println();
}

// Com as 4 celulas medidas: resolve os 4 fatores juntos. Cada linha e
// dividida pelo seu peso, entao funciona mesmo se o peso conhecido mudou
// entre uma celula e outra.
static void calibracaoCombinada() {
  bool usar[N];
  int m = 0;
  for (int i = 0; i < N; i++) { usar[i] = ch[i].active; if (usar[i]) m++; }
  double A[N][N];
  int linhas = 0;
  for (int r = 0; r < N; r++) {
    if (!usar[r]) continue;
    for (int c = 0; c < N; c++) A[linhas][c] = manutLinha[r][c] / manutW[r];
    linhas++;
  }

  Serial.println(F("\nTodas as celulas medidas. Refinando os fatores em conjunto..."));
  double k[N];
  if (!resolverCalibracao(A, linhas, usar, 1.0, k)) {
    Serial.println(F("Nao deu para refinar (medicoes parecidas demais). Ficam os fatores individuais."));
    return;
  }
  float novo[N];
  float soma = 0.0f;
  for (int i = 0; i < N; i++) {
    if (!usar[i]) continue;
    if (k[i] <= 0.0) {
      Serial.printf("Refino recusado: c%d deu fator negativo. Ficam os fatores individuais.\n", i);
      return;
    }
    novo[i] = (float)(1.0 / k[i]);
    soma += novo[i];
  }
  float medio = soma / m;
  for (int i = 0; i < N; i++) {
    if (usar[i] && (novo[i] < 0.5f * medio || novo[i] > 2.0f * medio)) {
      Serial.printf("Refino recusado: c%d (%.2f) muito diferente das outras. Ficam os individuais.\n",
                    i, novo[i]);
      return;
    }
  }

  Serial.println(F("Celula  individual  ->  refinado [cont/g]"));
  for (int i = 0; i < N; i++) {
    if (!usar[i]) continue;
    Serial.printf("  %d     %10.4f  ->  %10.4f\n", i + 1, ch[i].calib_factor, novo[i]);
    ch[i].calib_factor = novo[i];
  }
  saveCalibration();

  Serial.println(F("Conferencia (peso calculado em cada medicao):"));
  for (int r = 0; r < N; r++) {
    if (!usar[r]) continue;
    double est = 0.0;
    for (int c = 0; c < N; c++) if (usar[c]) est += manutLinha[r][c] / ch[c].calib_factor;
    Serial.printf("  peso sobre a celula %d: %9.2f g de %.2f g (erro %+.3f%%)\n",
                  r + 1, est, manutW[r], (est - manutW[r]) / manutW[r] * 100.0);
  }
  reiniciarFiltro();
  Serial.println(F("\nCalibracao das 4 celulas concluida e SALVA."));
  Serial.println(F("Confira com 'p' (peso no centro e nos cantos) e digite 'manutf' para sair.\n"));
}

static void manutCalibrarCelula(int i) {
  if (!ch[i].active) {
    Serial.printf("Celula %d (c%d) esta INATIVA. Saia com 'manutf' e ative com '+%d'.\n",
                  i + 1, i, i);
    return;
  }
  String s;
  Serial.printf("\n--- CALIBRAR CELULA %d (canal c%d) --- ('q' cancela)\n", i + 1, i);
  Serial.println(F("1) Retire TUDO da mesa e digite ENTER:"));
  flushSerial();
  if (!lerLinhaUsuario(s) || s == "q") { Serial.println(F("Cancelado.")); return; }

  // Tara: todas as celulas, com a mesa vazia. As leituras desta celula sao
  // relativas a este zero, e ele fica salvo.
  double zero[N];
  Qualidade q;
  if (!mediaBruta(CAL_QUADROS, zero, &q)) { reiniciarFiltro(); return; }
  for (int c = 0; c < N; c++) if (ch[c].active) ch[c].offset_raw = lround(zero[c]);
  saveCalibration();
  ruidoTotalG = q.ruidoG;
  manutMudou  = true;
  Serial.printf("Tara salva (ruido %.3f g).\n", q.ruidoG);

  if (pesoManut > 0.0f)
    Serial.printf("2) Coloque o peso conhecido BEM EM CIMA da celula %d.\n"
                  "   Digite o valor em gramas + ENTER (so ENTER = %.2f g):\n", i + 1, pesoManut);
  else
    Serial.printf("2) Coloque o peso conhecido BEM EM CIMA da celula %d.\n"
                  "   Digite o valor em gramas + ENTER:\n", i + 1);
  if (!lerLinhaUsuario(s) || s == "q") { Serial.println(F("Cancelado.")); reiniciarFiltro(); return; }
  float W = (s.length() == 0) ? pesoManut : s.toFloat();
  if (W <= 0.0f) { Serial.println(F("Valor invalido.")); reiniciarFiltro(); return; }
  pesoManut = W;

  double liq[N];
  if (!medirPosicao(liq, W)) {
    Serial.println(F("Medicao descartada. Repita a celula."));
    reiniciarFiltro();
    return;
  }
  if (liq[i] <= 0.0) {
    Serial.printf("ERRO: a celula %d ficou NEGATIVA/nula com o peso em cima dela.\n"
                  "Celula montada ao contrario (confira a SETA) ou A+/A- invertidos.\n"
                  "Fator NAO alterado (a tara ficou salva).\n", i + 1);
    reiniciarFiltro();
    return;
  }

  // Quanto do peso as OUTRAS celulas seguraram, pelos fatores atuais delas.
  double outras = 0.0;
  for (int c = 0; c < N; c++)
    if (ch[c].active && c != i) outras += liq[c] / ch[c].calib_factor;
  double nesta = W - outras;
  if (nesta < 0.5 * W) {
    Serial.printf("AVISO: so %.0f%% do peso ficou na celula %d — o peso nao esta em cima\n"
                  "dela, ou ela nao apoia a mesa. Fator NAO alterado; reposicione e repita.\n",
                  nesta / W * 100.0, i + 1);
    reiniciarFiltro();
    return;
  }

  float novo = (float)(liq[i] / nesta);
  Serial.printf("Celula %d segurou %.1f g (%.0f%%); as outras, %.1f g.\n",
                i + 1, nesta, nesta / W * 100.0, outras);
  Serial.printf("Fator c%d: %.4f -> %.4f cont/g  (SALVO)\n", i, ch[i].calib_factor, novo);
  ch[i].calib_factor = novo;
  saveCalibration();

  for (int c = 0; c < N; c++) manutLinha[i][c] = liq[c];
  manutW[i] = W;
  manutCalibrada[i] = true;
  reiniciarFiltro();

  bool todas = true;
  for (int c = 0; c < N; c++) if (ch[c].active && !manutCalibrada[c]) todas = false;
  if (todas) {
    calibracaoCombinada();
  } else {
    Serial.print(F("Faltam as celulas:"));
    for (int c = 0; c < N; c++)
      if (ch[c].active && !manutCalibrada[c]) Serial.printf(" %d", c + 1);
    Serial.println(F("\nRetire o peso e digite o numero da proxima."));
  }
}

void sairManut() {
  int pend = 0;
  for (int i = 0; i < N; i++) if (ch[i].active && !manutCalibrada[i]) pend++;
  if (manutMudou && pend > 0)
    Serial.printf("AVISO: %d celula(s) nao foram calibradas nesta sessao.\n", pend);
  modoManut = false;
  streamOn  = streamAntesManut;
  reiniciarFiltro();
  lastPingMs  = millis();
  lastTelemMs = millis();
  Serial.println(F("\nSaiu do modo manutencao. Balanca operando."));
  // Os offsets mudaram: o PC precisa saber que houve tara dos canais.
  if (manutMudou) {
    emitTaraBalanca("canais", NAN);
    Serial.println(F("Recomendado agora: 'V' para conferir a balanca inteira."));
  }
  emitCfg();
  emitPing();
}

void processManut(String cmd) {
  if (cmd == "manutf") {
    sairManut();
  } else if (cmd == "manut" || cmd == "?") {
    menuManut();
  } else if (cmd.length() == 1 && cmd[0] >= '1' && cmd[0] < '1' + N) {
    manutCalibrarCelula(cmd[0] - '1');
  } else if (cmd == "t") {
    if (tareAll()) manutMudou = true;
  } else if (cmd == "p") {
    printChannels();
  } else if (cmd == "s") {
    statusManut();
  } else {
    Serial.printf("Em manutencao: '%s' nao vale aqui. '?' = menu, 'manutf' = sair.\n",
                  cmd.c_str());
  }
}

// ================================================================
//   IMPRESSAO
// ================================================================
static void printItens() {
  if (countConfig.unit_weight_g <= 0) return;
  float liq = currentTotal - countConfig.container_tara_g;
  float exata = liq / countConfig.unit_weight_g;
  Serial.printf("Itens: %ld (exata %.3f, recipiente %.2f g)\n",
                lroundf(exata), exata, countConfig.container_tara_g);
}

void printChannels() {
  Serial.print(F("Canais [g]: "));
  for (int i = 0; i < N; i++) {
    if (!ch[i].active)        Serial.print(F("--"));
    else if (canalMudo[i])    Serial.print(F("MUDO"));
    else if (ch[i].saturated) Serial.print(F("SAT"));
    else                      Serial.print(String(channelWeights[i], 2));
    Serial.print(i < N - 1 ? ' ' : '\n');
  }
  Serial.print(F("Total [g]: "));
  Serial.print(currentTotal, 2);
  Serial.printf("  (%s, media de %d quadros)\n",
                streamEstavel ? "ESTAVEL" : "instavel", caudaLen);
  printItens();
}

void printMap() {
  Serial.println(F("\n=== MAPA DE CANAIS ==="));
  for (int i = 0; i < N; i++) {
    // `bruto` e a ultima leitura crua do HX711 (-8388608 .. 8388607). Nos
    // extremos = saturado; perto de zero com a mesa vazia = celula sadia.
    Serial.printf("c%d DOUT=%d SCK=%d [%s] cal=%.4f off=%ld bruto=%ld%s%s\n",
                  i, ch[i].dout_pin, ch[i].sck_pin,
                  ch[i].active ? "ATIVO" : "INATIVO",
                  ch[i].calib_factor, ch[i].offset_raw, ch[i].last_raw,
                  ch[i].saturated ? " SATURADO" : "",
                  canalMudo[i] ? " SEM RESPOSTA" : "");
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
  // Inteiro MAIS PROXIMO: e a melhor estimativa de quantos objetos ha. Na 2.4
  // um 5,8 fora da tolerancia virava 5; agora vira 6 — o status e o aceite e
  // que dizem que a sobra nao fecha.
  int n = (int)lroundf(exact_count);
  float sobra_g = (exact_count - n) * unit_weight_g;
  if (fabsf(sobra_g) <= tolerance_g) {
    outResult = CountResult::OK;
    return n;
  }
  // Mesma convencao de status da 2.2/2.3: fracao < 0,5 => UNDER_TOLERANCE.
  float frac = exact_count - floorf(exact_count);
  outResult = (frac < 0.5f) ? CountResult::UNDER_TOLERANCE
                            : CountResult::OVER_TOLERANCE;
  return n;
}

bool validateCountRange(int rounded_count, int min_items, int max_items) {
  return (rounded_count >= min_items && rounded_count <= max_items);
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
// Nao le hardware: consome o que a aquisicao ja publicou (`currentTotal`,
// `streamEstavel`, `quadroSeq`). Ler por conta propria era o que, na 2.4,
// alimentava a media movel duas vezes por ciclo.
void stepCounting() {
  unsigned long dt = millis() - estadoDesdeMs;
  float lim = countConfig.stability_threshold_g;

  switch (countState) {
    case CountState::IDLE:
      break;

    case CountState::AWAITING_TARA: {
      // Mesa parada com algo em cima -> e o recipiente. Mesa parada e vazia
      // por 10 s -> nao ha recipiente, tara zero.
      if (estavelDesde(estadoDesdeSeq) &&
          (fabsf(currentTotal) > 2.0f * lim || dt >= TARA_REC_ESPERA_MS)) {
        float w = currentTotal;
        countConfig.container_tara_g = w;
        saveCountConfig();
        Serial.printf("Tara do recipiente: %.2f g\n", w);
        emitTaraBalanca("recipiente", w);
        setCountState(CountState::AWAITING_DEPOSIT);
      } else if (dt >= CONTAGEM_TIMEOUT_MS) {
        Serial.println(F("ERRO: a mesa nao estabilizou para a tara do recipiente."));
        emitErroBalanca("mesa nao estabilizou na tara do recipiente", NULL);
        setCountState(CountState::IDLE);
      }
      break;
    }

    case CountState::AWAITING_DEPOSIT:
      break;

    case CountState::TRANSIENT: {
      // Espera chegar pelo menos MEIO item E a mesa parar com quadros novos.
      // Sem a primeira condicao, a mesa ainda vazia ja contava como
      // "estavel" e a contagem saia antes de alguem depositar.
      float liquido = currentTotal - countConfig.container_tara_g;
      bool chegou = liquido >= 0.5f * countConfig.unit_weight_g;
      if (chegou && estavelDesde(estadoDesdeSeq)) {
        Serial.printf("Peso estavel: %.2f g (media de %d quadros)\n",
                      currentTotal, caudaLen);
        setCountState(CountState::COUNTING);
      } else if (dt >= CONTAGEM_TIMEOUT_MS) {
        Serial.println(F("Tempo esgotado esperando deposito estavel."));
        lastCount = countByWeight(currentTotal, countConfig);
        if (!streamEstavel) {
          lastCount.result = CountResult::UNSTABLE;
          lastCount.within_tolerance = false;
        }
        printCountResult(lastCount);
        emitContagem(lastCount);
        setCountState(CountState::DONE);
      }
      break;
    }

    case CountState::COUNTING: {
      lastCount = countByWeight(currentTotal, countConfig);
      const char *prob = problemaSensor();
      if (prob) {
        Serial.printf("ERRO: %s — contagem invalida.\n", prob);
        lastCount.result = CountResult::INVALID_WEIGHT;
        lastCount.within_tolerance = false;
      }
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

  auto checkTrue = [&](const char* name, bool ok) {
    total++;
    if (ok) { Serial.printf("  [PASS] %s\n", name); passed++; }
    else Serial.printf("  [FAIL] %s\n", name);
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
  checkTrue("T8: fora faixa", !out.within_tolerance && out.rounded_count == 5);

  out = countByWeight(1050.0f, tc);
  check("T9: 100 itens", CountResult::OK, true);
  checkCount("T9: count=100", 100);

  out = countByWeight(60.0f, tc);
  check("T10: 1 item", CountResult::OK, true);
  checkCount("T10: count=1", 1);

  out = countByWeight(100.5f, tc);
  check("T11: 5.05 (margem)", CountResult::OK, true);
  checkCount("T11: count=5", 5);

  // Estabilidade: a mesma funcao que a aquisicao usa a cada quadro.
  const float est[]   = {100.0f, 100.1f, 100.2f};
  checkTrue("T12: estavel (cauda 3)", caudaEstavel(est, 3, 0.5f) == 3);
  const float inst[]  = {100.0f, 105.0f, 103.0f};
  checkTrue("T13: instavel (cauda 1)", caudaEstavel(inst, 3, 0.5f) == 1);
  const float degrau[] = {100.0f, 100.1f, 100.0f, 150.0f, 150.2f};
  checkTrue("T14: degrau reinicia a media", caudaEstavel(degrau, 5, 0.5f) == 2);

  out = countByWeight(108.0f, tc);
  check("T15: 5.8 (fora tol)", CountResult::OVER_TOLERANCE, false);
  checkCount("T15: count=6 (mais proximo)", 6);

  // Calibracao por minimos quadrados com dados sinteticos: 4 celulas com
  // fatores conhecidos, peso dividido de forma diferente em 5 posicoes.
  {
    const double f[N] = {400.0, 410.0, 395.0, 405.0};       // cont/g reais
    const double div[5][N] = {
      {0.25, 0.25, 0.25, 0.25}, {0.70, 0.10, 0.10, 0.10},
      {0.10, 0.70, 0.10, 0.10}, {0.10, 0.10, 0.70, 0.10},
      {0.10, 0.10, 0.10, 0.70}
    };
    const double W = 500.0;
    double A[5][N];
    for (int r = 0; r < 5; r++)
      for (int i = 0; i < N; i++) A[r][i] = div[r][i] * W * f[i];
    bool usar[N] = {true, true, true, true};
    double k[N];
    bool ok = resolverCalibracao(A, 5, usar, W, k);
    for (int i = 0; ok && i < N; i++)
      if (fabs(1.0 / k[i] - f[i]) > 0.01) ok = false;
    checkTrue("T16: calibracao recupera fatores", ok);

    // Todas as posicoes iguais nao separam os canais -> deve recusar.
    double B[5][N];
    for (int r = 0; r < 5; r++)
      for (int i = 0; i < N; i++) B[r][i] = 0.25 * W * f[i];
    checkTrue("T17: posicoes iguais recusadas", !resolverCalibracao(B, 5, usar, W, k));
  }

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
    Serial.println(F("Calibracao salva."));
  } else if (cmd == "l") {
    loadCalibration();
    reiniciarFiltro();
    Serial.println(F("Calibracao carregada da memoria."));
  } else if (cmd == "json") {
    sairConsole(true);
  } else if (cmd == "manut") {
    entrarManut();
  } else if (cmd == "manutf") {
    Serial.println(F("A balanca nao esta em manutencao (entre com 'manut')."));
  } else if (cmd == "K") {
    calibracaoCompleta();
  } else if (cmd == "E") {
    ajusteEscala();
  } else if (cmd == "D") {
    logDeriva = !logDeriva;
    logDerivaT0 = millis();
    logDerivaUlt = 0;
    Serial.printf("Log de deriva: %s\n", logDeriva ? "ON (1 linha/s)" : "OFF");
    if (logDeriva) Serial.println(F("seg   c0[g]    c1[g]    c2[g]    c3[g]    total[g]"));
  } else if (cmd == "L") {
    ledsLigados = !ledsLigados;
    Serial.printf("LEDs: %s\n", ledsLigados ? "LIGADOS" : "DESLIGADOS");
  } else if (cmd == "V") {
    diagnosticoGuiado();
  } else if (cmd == "R") {
    medirRuido();
  } else if (cmd.length() == 2 && cmd[0] == 'c' && cmd[1] >= '0' && cmd[1] < '0' + N) {
    calibrarCanal(cmd[1] - '0');
  } else if (cmd.length() == 2 && cmd[0] == '+' && cmd[1] >= '0' && cmd[1] < '0' + N) {
    int i = cmd[1] - '0'; ch[i].active = true; saveCalibration(); reiniciarFiltro();
    Serial.printf("Canal %d ATIVADO.\n", i);
  } else if (cmd.length() == 2 && cmd[0] == '-' && cmd[1] >= '0' && cmd[1] < '0' + N) {
    int i = cmd[1] - '0'; ch[i].active = false; saveCalibration(); reiniciarFiltro();
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
    Serial.println(F("Coloque o recipiente vazio (a tara sai quando a mesa parar)..."));

  // --- Contagem: depositar e contar ---
  } else if (cmd == "x") {
    if (countConfig.unit_weight_g <= 0) {
      Serial.println(F("ERRO: Configure o peso unitario primeiro ('u' ou 'g<valor>')."));
      emitErroBalanca("configure o peso unitario primeiro", "x");
    } else {
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
      Serial.printf("Leituras estabilizacao: %d quadros (minimo efetivo 3)\n", v);
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
      reiniciarFiltro();
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
    Serial.println(F("  p = imprimir canais (+ itens)"));
    Serial.println(F("  m = mapa de canais"));
    Serial.println(F("  a = auto-print on/off"));
    Serial.println(F("  s = salvar calibracao NVS"));
    Serial.println(F("  l = carregar calibracao NVS"));
    Serial.println(F("  manut = MODO MANUTENCAO: calibrar celula por celula (sai com manutf)"));
    Serial.println(F("  K = CALIBRACAO COMPLETA da mesa"));
    Serial.println(F("  E = ajuste de escala com peso conhecido"));
    Serial.println(F("  R = medir ruido e sugerir limiar"));
    Serial.println(F("  V = DIAGNOSTICO GUIADO (rode antes de calibrar)"));
    Serial.println(F("  D = log de deriva por canto (1/s) on/off"));
    Serial.println(F("  L = LEDs liga/desliga (teste de interferencia)"));
    Serial.println(F("  c0..c3 = calibrar canal isolado (legado)"));
    Serial.println(F("  +0..+3 = ativar canal"));
    Serial.println(F("  -0..-3 = desativar canal"));
    Serial.println(F("  --- Contagem ---"));
    Serial.println(F("  u = peso unitario (interativo)"));
    Serial.println(F("  g<valor> = peso unitario direto (ex: g15)"));
    Serial.println(F("  k = tara do recipiente"));
    Serial.println(F("  o<valor> = tolerancia g (ex: o1.5)"));
    Serial.println(F("  i<min>-<max> = faixa (ex: i1-100)"));
    Serial.println(F("  r<valor> = quadros de estabilizacao (ex: r5)"));
    Serial.println(F("  h<valor> = limiar estabilidade g (ex: h0.5)"));
    Serial.println(F("  x = depositar itens e contar"));
    Serial.println(F("  n = ultimo resultado"));
    Serial.println(F("  T = executar testes"));
    Serial.println(F("  C = config atual"));
    Serial.println(F("  j = stream JSON de peso on/off"));
    Serial.println(F("  json = mostrar as linhas {..} no console agora\n"));
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
// continuam todos onde estavam: o que sai daqui sao linhas EXTRAS, e e o '{'
// que separa as duas vozes na mesma porta. O adapter ignora a linha sem '{'
// (para ele e log) e o humano ignora a linha com '{'.
//
// Esta secao nao interpreta a regra de negocio da contagem nem filtra
// leitura: ela so LE o que a aquisicao e `countByWeight` produziram e publica.
// O formato de cada mensagem e o mesmo da 2.3.

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
  // Em manutencao a voz da maquina fica calada (so passa o que for liberado
  // de proposito, como a recusa de comando do PC).
  if ((modoManut || modoConsole) && !emitirEmManut) return;
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
  // num estado que a maquina ja deixou para tras. Tambem marca DESDE QUANDO,
  // para que `k` e `x` so aceitem medicao posterior ao pedido.
  countState     = s;
  estadoDesdeMs  = millis();
  estadoDesdeSeq = quadroSeq;
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

// Espera a mesa parar com quadros medidos DEPOIS desta chamada. Um "estavel"
// de antes do comando pode ser da mesa ainda sem a descarga.
static bool esperarEstavel(unsigned long timeout_ms) {
  uint32_t seq0 = quadroSeq;
  unsigned long t0 = millis();
  while (!estavelDesde(seq0) && millis() - t0 < timeout_ms) {
    bombearAquisicao();
    delay(1);
  }
  return estavelDesde(seq0);
}

static void cmdTara(const char *linha, long cmd_id) {
  char os_id[MAX_OS_ID_LEN];
  if (!jsonTexto(linha, "os_id", os_id, sizeof os_id)) os_id[0] = '\0';
  ackOk(cmd_id, false);

  // Zero LOGICO da mesa, nao tara de hardware: o offset do HX711 e calibracao
  // e so muda por `t`/`c<n>`/`K`, com o operador presente. Aqui o que se move
  // e a referencia da OS — exatamente o que o weight-simulator faz.
  if (!esperarEstavel(PESAR_ESTAB_TIMEOUT_MS))
    Serial.println(F("[APSEN] mesa nao estabilizou para a tara — usando a leitura atual."));
  const char *prob = problemaSensor();
  if (prob) emitErroBalanca(prob, "tara");
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
  if (!esperarEstavel(PESAR_ESTAB_TIMEOUT_MS))
    Serial.println(F("[APSEN] mesa nao estabilizou no prazo — medindo assim mesmo."));

  const char *prob = problemaSensor();
  if (prob) {
    emitErroSensor(os_id, (int)slot, prob);
    return;
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
    Serial.println(F("Coloque o recipiente vazio (a tara sai quando a mesa parar)..."));

  } else if (strcmp(cmd, "tara_canais") == 0) {
    ackOk(cmd_id, false);
    tareAll();                               // emite `tara_balanca` ou `erro_balanca`

  } else if (strcmp(cmd, "contar") == 0) {
    if (countConfig.unit_weight_g <= 0.0f) {
      ackErro(cmd_id, "configure o peso unitario primeiro");
      return;
    }
    ackOk(cmd_id, false);
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

  // Em manutencao o comando e RECUSADO sem consumir o `cmd_id`: quando a
  // balanca voltar, o mesmo comando reenviado executa normalmente.
  if (modoManut) {
    emitirEmManut = true;
    ackErro(cmd_id, "balanca em manutencao");
    emitirEmManut = false;
    return;
  }

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

void entrarConsole() {
  consoleUltimoMs = millis();
  if (modoConsole) return;
  modoConsole = true;
  Serial.println(F("[console] linhas {..} ocultas. 'json' mostra de novo (ou 5 min sem comandos)."));
}

void sairConsole(bool avisar) {
  if (!modoConsole) return;
  modoConsole = false;
  if (avisar) Serial.println(F("[console] linhas {..} de volta."));
  // O adapter pode ter chegado enquanto a placa estava calada: ele precisa do
  // estado atual e do ping para reconhecer a porta.
  emitCfg();
  emitPing();
  lastPingMs = millis();
}

void despacharLinha(const String &linha) {
  // O '{' e o unico separador das duas vozes. Linha com '{' e o adapter: a
  // voz de maquina volta na hora, antes do ACK. Linha sem '{' e uma pessoa:
  // a voz de maquina sai da tela dela.
  if (linha.indexOf('{') >= 0) {
    sairConsole(false);
    processarJson(linha.c_str());
    return;
  }
  entrarConsole();
  if (modoManut) processManut(linha);
  else           processCommand(linha);
}

// ================================================================
//   ILUMINACAO DOURADA
// ================================================================
//
// Tres camadas somadas por LED:
//   1) respiracao lenta de toda a fita em dourado (ciclo de 4 s);
//   2) um feixe de luz dourado-clara que percorre a fita (uma volta a cada 3 s),
//      com cauda suave — e ele que "chama o olho" para a balanca;
//   3) faiscas aleatorias que acendem e apagam devagar, como reflexo em ouro.
//
// Roda em tarefa propria no core 0. Ninguem mais toca na fita, entao nao ha
// disputa pelo `fita.show()`.

static void tarefaLeds(void *) {
  const float DOIS_PI      = 6.2831853f;
  const float CAUDA        = 14.0f;     // LEDs de cauda do feixe
  static float faisca[LED_COUNT] = {0};

  for (;;) {
    if (!ledsLigados) {
      fita.clear();
      fita.show();
      vTaskDelay(pdMS_TO_TICKS(100));
      continue;
    }
    unsigned long t = millis();

    float respira = 0.50f + 0.18f * sinf(DOIS_PI * (t % 4000UL) / 4000.0f);
    float cabeca  = (t % 3000UL) / 3000.0f * LED_COUNT;

    if (esp_random() % 100 < 10) faisca[esp_random() % LED_COUNT] = 1.0f;

    for (int i = 0; i < LED_COUNT; i++) {
      float d = cabeca - i;
      if (d < 0) d += LED_COUNT;                 // distancia atras da cabeca
      float feixe = (d < CAUDA) ? 1.0f - d / CAUDA : 0.0f;
      feixe *= feixe;                            // cauda que some suave

      float nivel = respira + 0.45f * feixe + 0.35f * faisca[i];
      if (nivel > 1.0f) nivel = 1.0f;
      float claro = feixe + 0.8f * faisca[i];    // quanto puxa para o tom claro
      if (claro > 1.0f) claro = 1.0f;

      uint8_t r = (uint8_t)((OURO_R + (LUZ_R - OURO_R) * claro) * nivel);
      uint8_t g = (uint8_t)((OURO_G + (LUZ_G - OURO_G) * claro) * nivel);
      uint8_t b = (uint8_t)((OURO_B + (LUZ_B - OURO_B) * claro) * nivel);
      fita.setPixelColor(i, r, g, b);

      faisca[i] *= 0.90f;
    }
    fita.show();
    vTaskDelay(pdMS_TO_TICKS(20));               // ~50 quadros/s
  }
}

static void iniciarLeds() {
  fita.begin();
  fita.setBrightness(LED_BRILHO_MAX);
  fita.clear();
  fita.show();
  xTaskCreatePinnedToCore(tarefaLeds, "leds", 4096, NULL, 1, NULL, 0);
}

// ================================================================
//   SETUP
// ================================================================
void setup() {
  Serial.begin(115200);
  // Primeiro de tudo: a balanca ja acende dourada durante a tara de boot.
  iniciarLeds();
  delay(300);

  loadCalibration();
  loadCountConfig();

  for (int i = 0; i < N; i++) {
    pinMode(ch[i].sck_pin, OUTPUT);
    pinMode(ch[i].dout_pin, INPUT);
    digitalWrite(ch[i].sck_pin, LOW);
  }
  // O HX711 leva ~400 ms para assentar depois de ligado.
  delay(500);
  reiniciarFiltro();

  Serial.println(F("\n=== BALANCA v" FW_VERSION " ==="));
  Serial.println(F("ENTER = tara | 'l' = manter tara salva | em 5 s: tara automatica"));
  unsigned long t0 = millis();
  while (!Serial.available() && (millis() - t0 < 5000)) delay(10);
  if (Serial.available()) {
    String resp = Serial.readStringUntil('\n'); resp.trim();
    // Quem respondeu ao prompt e uma pessoa: console limpo desde o boot.
    if (resp.indexOf('{') < 0) { modoConsole = true; consoleUltimoMs = millis(); }
    if (resp == "l") { loadCalibration(); Serial.println(F("Tara salva mantida.")); }
    else tareAll();
  } else {
    tareAll();
  }
  reiniciarFiltro();

  int ativos = 0;
  bool problema = false;
  for (int i = 0; i < N; i++) {
    if (ch[i].active) ativos++; else problema = true;
    if (canalMudo[i] || ch[i].saturated) problema = true;
  }
  Serial.printf("Pronta: %d/%d celulas", ativos, N);
  if (countConfig.unit_weight_g > 0)
    Serial.printf(" | peso unitario %.4f g", countConfig.unit_weight_g);
  else
    Serial.print(F(" | peso unitario NAO configurado (g<valor>)"));
  Serial.println(F(" | '?' = comandos"));
  // O mapa completo so aparece quando ha algo errado nele; senao, 'm'.
  if (problema) printMap();

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

  bombearAquisicao();

  // Manutencao: a aquisicao continua (para `p`), todo o resto para.
  if (modoManut) {
    totalReady = false;
    delay(2);
    return;
  }

  stepCounting();

  if (modoConsole && millis() - consoleUltimoMs > CONSOLE_TIMEOUT_MS) {
    Serial.println(F("[console] 5 min sem comandos — linhas {..} de volta."));
    sairConsole(false);
  }

  if (logDeriva && millis() - logDerivaUlt >= 1000) {
    logDerivaUlt = millis();
    Serial.printf("%-5lu", (millis() - logDerivaT0) / 1000UL);
    for (int i = 0; i < N; i++) {
      if (ch[i].active) Serial.printf(" %8.2f", channelWeights[i]);
      else              Serial.print(F("       --"));
    }
    Serial.printf("  %9.2f%s\n", currentTotal, streamEstavel ? "" : "  ~");
  }

  if (totalReady) {
    if (autoPrint) {
      Serial.print(F("Peso total [g]: "));
      Serial.print(currentTotal, 2);
      Serial.print(streamEstavel ? F("  *") : F(""));
      if (countConfig.unit_weight_g > 0) {
        float exata = (currentTotal - countConfig.container_tara_g) /
                      countConfig.unit_weight_g;
        Serial.printf("  itens: %ld (%.3f)", lroundf(exata), exata);
      }
      Serial.println();
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

  // Curto de proposito: o HX711 a 80 SPS entrega a cada 12,5 ms, e quem
  // deixa a leitura esperar e o loop, nao o conversor.
  delay(2);
}
