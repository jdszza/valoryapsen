// =====================================================================
//  apsen_serial.h - a VOZ DE MAQUINA do protocolo serial APSEN
// =====================================================================
//
//  O nucleo do contrato de `docs/PROTOCOLO_SERIAL.md` §1 e §2, identico nas
//  tres placas que o incluem: enquadramento de linha, relogio pelo pong,
//  scanner de JSON, ACK, `cmd_id` idempotente e o ping que identifica a porta.
//  O que muda de uma placa para a outra — quais comandos existem e o que eles
//  fazem — fica no .ino.
//
//  TRES COPIAS, E UM TESTE QUE AS COMPARA
//  --------------------------------------
//  Este arquivo existe identico em `dispenser/servos_hub/`, em
//  `dispenser/telas_tft/` e em `cnc/receitas_manuais/`, e
//  `tests/test_dispenser_firmware.py` reprova se divergirem. Nao e preguica: a
//  Arduino IDE compila a PASTA do sketch, e
//  um `#include "../apsen_serial.h"` nao sobrevive a copia que o build faz
//  para o diretorio temporario. As alternativas sao transformar isto numa
//  biblioteca Arduino instalada na maquina — infraestrutura que faz o firmware
//  parar de compilar em qualquer maquina que nao a tenha — ou duplicar. E a
//  mesma avaliacao que `serial_link.py` ja registra para as suas tres copias,
//  com a mesma conclusao: nesta escala, o teste de igualdade custa menos que a
//  infraestrutura. Eram duas quando isto foi escrito e a mesa fez a terceira —
//  se um dia forem cinco, a conta muda.
//
//  O SKETCH QUE INCLUI ESTE HEADER PRECISA FORNECER
//  ------------------------------------------------
//    #define APSEN_SUBSISTEMA "<dispenser|dispenser_tft|cnc>"  ANTES do include
//    void logMsg(const char* tag, const char* fmt, ...);
//    void executarComando(const char* cmd, const char* linha, long cmd_id);
//    void linhaHumana(char* linha);   // a linha SEM '{'
//
// =====================================================================
#pragma once

#include <Arduino.h>
#include <time.h>
#include <math.h>

#ifndef APSEN_SUBSISTEMA
#error "defina APSEN_SUBSISTEMA antes de incluir apsen_serial.h"
#endif

// Comando sem Enter: se ficar este tempo sem chegar caractere, a linha HUMANA
// e executada assim mesmo (Serial Monitor com "Nenhum final de linha"). Vale
// so para o humano — ver o fim de `serialPoll`. O sketch pode redefini-lo
// antes do include.
#ifndef SERIAL_FIM_LINHA_MS
#define SERIAL_FIM_LINHA_MS 250
#endif

// Quem inicia o ping e SEMPRE a placa: e assim que o adapter descobre qual
// porta e qual, sem depender de VID/PID (o mesmo conversor USB-serial aparece
// em placas de fabricantes diferentes, e casar por ele mandaria `dispensar`
// para a balanca — o comando sai, nada dispensa, e a OS morre por timeout de
// um slot que esta intacto).
#define PING_INTERVAL_MS 3000

// Teto de UMA linha, o mesmo dos dois lados (`serial_link.MAX_LINHA_BYTES`).
// Nos DOIS sentidos, com decisoes OPOSTAS de proposito:
//   entrando - linha maior e descartada INTEIRA e logada, nunca partida em
//     duas: meia linha e JSON invalido, e as duas metades sumiriam em silencio;
//   saindo   - mensagem que nao cabe NAO SAI. Comando truncado e JSON
//     invalido, o outro lado o descarta, e quem esperava um ACK espera para
//     sempre. Falhar aponta para a mensagem; truncar apontaria para a placa.
static const size_t MAX_LINHA_BYTES = 1024;

// `ordens.os_id` e VARCHAR(60) no central — 64 cobre com folga. O limite sai
// da ORIGEM do dado, nao do tamanho que ele tem hoje: dois disparos do mesmo
// template diferem no FIM da string, e cortar o fim faz dois ids virarem um.
#define MAX_OS_ID_LEN 64

// Buffer de UMA linha de saida, do tamanho do teto: assim o unico limite e o
// do contrato, e nao um numero a parte que poderia ficar menor que ele.
static char jbuf[MAX_LINHA_BYTES + 1];

// Relogio: a placa nao tem RTC nem NTP. O `epoch` do pong e a unica fonte, e
// ate o primeiro pong os carimbos saem em 1970 — visivelmente errado, que e
// melhor que plausivel e errado.
static unsigned long epochBase   = 0;
static unsigned long epochMillis = 0;

static unsigned long lastPingMs  = 0;
static long          ultimoCmdId = 0;

// Quando chegou o ultimo pong. O adapter so responde pong ao NOSSO ping, entao
// silencio longo e o adapter fora do ar — e quando ele volta, o contador de
// `cmd_id` dele volta a nascer em 1. Ver `processarJson`.
static unsigned long ultimoPongMs = 0;
static const unsigned long SESSAO_SILENCIO_MS = 10000;   // > 3 pings sem pong

// A SESSAO do adapter: o epoch de boot do processo do outro lado. Ele a manda
// no pong e em TODO comando, e a placa so precisa notar que ela MUDOU.
//
// A heuristica do silencio (acima) cobre o adapter que ficou fora por mais de
// 10 s. Ela nao cobre o restart RAPIDO, que e o comum: 2 a 5 s de queda nao
// chegam perto de tres pings perdidos, e nesse caso o contador do adapter
// nascia em 1 com esta placa ainda em `ultimoCmdId = 47`. Todo comando ate 47
// recebia ackOk(repetido) SEM EXECUTAR — e, com o ciclo por relogio, o central
// segue o cronograma e dispara o `dispensar` de um `mover` que nunca aconteceu.
//
// Zero significa "ainda nao sei": a primeira sessao vista e adotada sem zerar
// nada, senao todo boot da placa descartaria o primeiro comando legitimo.
static unsigned long sessaoAdapter = 0;


// Fornecidos pelo sketch.
void logMsg(const char* tag, const char* fmt, ...);
void executarComando(const char* cmd, const char* linha, long cmd_id);
void linhaHumana(char* linha);

// -- Formatacao -------------------------------------------------------------

static void fmtF(char* dest, size_t n, float v, int casas) {
  // NaN e inf nao existem em JSON. Emiti-los como `nan` faria o `json.loads`
  // do adapter descartar a linha INTEIRA — um campo estragado levaria junto os
  // que estavam certos, e o sintoma seria um evento que nunca chegou.
  if (isnan(v) || isinf(v)) { snprintf(dest, n, "null"); return; }
  snprintf(dest, n, "%.*f", casas, v);
}

static void tsAgora(char* dest, size_t n) {
  unsigned long seg = epochBase + (millis() - epochMillis) / 1000UL;
  time_t t = (time_t)seg;
  struct tm g;
  gmtime_r(&t, &g);
  strftime(dest, n, "%Y-%m-%dT%H:%M:%S", &g);
}

static void emitir(int escritos, const char* origem) {
  // Mensagem que nao cabe NAO SAI. Ver MAX_LINHA_BYTES.
  if (escritos < 0 || (size_t)escritos >= sizeof(jbuf) ||
      (size_t)escritos > MAX_LINHA_BYTES) {
    Serial.printf("AVISO: mensagem '%s' (%d bytes) passou do teto da linha e NAO saiu.\n",
                  origem, escritos);
    return;
  }
  Serial.println(jbuf);
}

// -- Texto seguro para dentro de um JSON ------------------------------------

static void copiarCampo(char* dest, size_t n, const char* origem) {
  // Nome de medicamento vem do catalogo do central e pode trazer aspas ou
  // barra. Reemiti-los crus produziria JSON invalido, e o adapter descartaria
  // a linha INTEIRA sem erro: o evento sumiria, e quem esperava por ele
  // queimaria o prazo cheio. Sanitizar na ENTRADA resolve de uma vez para
  // todos os emissores, em vez de cada um ter de lembrar.
  size_t i = 0;
  for (; origem[i] != '\0' && i + 1 < n; i++) {
    unsigned char c = (unsigned char)origem[i];
    dest[i] = (c == '"' || c == '\\' || c < 0x20) ? ' ' : origem[i];
  }
  dest[i] = '\0';
}

static const char* citarOuNull(char* dest, size_t n, const char* v) {
  // Campo de texto vazio sai como `null`, nao como "": e o que o
  // `dispenser_simulator` emite (None), e o central grava a diferenca —
  // medicamento "" num slot vazio apareceria no painel como item sem nome.
  if (v && v[0] != '\0') snprintf(dest, n, "\"%s\"", v);
  else                   snprintf(dest, n, "null");
  return dest;
}

// -- Mensagens de servico ---------------------------------------------------

static void emitPing() {
  emitir(snprintf(jbuf, sizeof jbuf,
                  "{\"cmd\":\"ping\",\"sub\":\"" APSEN_SUBSISTEMA "\"}"), "ping");
}

static void pingPoll() {
  if (millis() - lastPingMs < PING_INTERVAL_MS) return;
  lastPingMs = millis();
  emitPing();
}

// -- Entrada: o scanner de JSON ---------------------------------------------
//
// Sem ArduinoJson: meia duzia de comandos nao paga a dependencia numa placa
// que ja carrega drivers e sensores. O scanner acha a CHAVE e le o valor logo
// depois dela. A limitacao e conhecida e esta dentro do contrato: um valor de
// texto que contivesse `"cmd":` enganaria a busca, e nenhum campo deste
// contrato pode conter aspas.

static const char* acharChave(const char* linha, const char* chave) {
  char alvo[40];
  snprintf(alvo, sizeof alvo, "\"%s\"", chave);
  const char* q = strstr(linha, alvo);
  if (!q) return NULL;
  q += strlen(alvo);
  while (*q == ' ') q++;
  if (*q != ':') return NULL;
  q++;
  while (*q == ' ') q++;
  return q;
}

static bool jsonTexto(const char* linha, const char* chave, char* dest, size_t n) {
  const char* q = acharChave(linha, chave);
  if (!q || *q != '"') return false;
  q++;
  size_t i = 0;
  while (*q && *q != '"' && i + 1 < n) {
    if (*q == '\\' && q[1]) q++;   // \" e \\ — o resto do contrato e ASCII
    dest[i++] = *q++;
  }
  dest[i] = '\0';
  return true;
}

static bool jsonBool(const char* linha, const char* chave, bool* out) {
  const char* q = acharChave(linha, chave);
  if (!q) return false;
  if (strncmp(q, "true", 4) == 0)  { *out = true;  return true; }
  if (strncmp(q, "false", 5) == 0) { *out = false; return true; }
  return false;
}

static bool jsonNumero(const char* linha, const char* chave, float* out) {
  const char* q = acharChave(linha, chave);
  if (!q || *q == 'n') return false;   // ausente ou `null`
  char* fim = NULL;
  float v = (float)strtod(q, &fim);
  if (fim == q) return false;
  *out = v;
  return true;
}

// `jsonTemChave` responde "a chave veio?", que nao e a mesma pergunta que "o
// valor e um numero". `trava_slot_id` pode vir nulo (trava sem slot), mas a
// CHAVE vem sempre: e ela que diz a cada tela se e este slot ou outro.
static bool jsonTemChave(const char* linha, const char* chave) {
  return acharChave(linha, chave) != NULL;
}

// -- ACK --------------------------------------------------------------------

static void ackOk(long cmd_id, bool repetido) {
  if (repetido)
    emitir(snprintf(jbuf, sizeof jbuf,
      "{\"resp\":\"ok\",\"cmd_id\":%ld,\"repetido\":true}", cmd_id), "ack");
  else
    emitir(snprintf(jbuf, sizeof jbuf,
      "{\"resp\":\"ok\",\"cmd_id\":%ld}", cmd_id), "ack");
}

static void ackErro(long cmd_id, const char* msg) {
  emitir(snprintf(jbuf, sizeof jbuf,
    "{\"resp\":\"erro\",\"cmd_id\":%ld,\"msg\":\"%s\"}", cmd_id, msg), "ack");
}

// Adota a sessao que chegou e, se ela for OUTRA, zera o contador de idempotencia.
// Comparacao por DIFERENCA e nao por ordem: relogio do host que ande para tras
// (NTP, fuso, maquina sem RTC) continua sendo uma sessao nova, que e o que
// importa aqui.
static void adotarSessao(const char* linha) {
  float s = 0;
  if (!jsonNumero(linha, "sessao", &s) || s <= 0) return;
  const unsigned long nova = (unsigned long)s;
  if (sessaoAdapter != 0 && nova != sessaoAdapter) {
    ultimoCmdId = 0;
    logMsg("APSEN", "sessao nova do adapter (%lu) - contador de cmd_id zerado", nova);
  }
  sessaoAdapter = nova;
}

// -- Despacho ---------------------------------------------------------------

static void processarJson(const char* linha) {
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
      // E detectavel sem mensagem nova: o adapter so responde pong ao NOSSO
      // ping, entao silencio de varios pings e ele fora do ar. Abrir a porta
      // nao reinicia a placa (o adapter desliga DTR/RTS antes de abrir), entao
      // quem tem de notar a volta e este lado.
      if (ultimoPongMs != 0 && millis() - ultimoPongMs > SESSAO_SILENCIO_MS) {
        ultimoCmdId = 0;
        logMsg("APSEN", "adapter voltou - contador de cmd_id zerado");
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
  // (ou anterior) responde ACK DE NOVO sem executar. Esta placa nao reenvia
  // nada e o `serial_link.py` tambem nao, mas o reenvio pode vir de qualquer
  // origem — um restart do adapter no meio do ciclo, um operador repetindo a
  // acao, uma versao futura que decida retentar — e reenviar `dispensar` e
  // dose dobrada no leito. E a parte do protocolo que nao da para acrescentar
  // depois sem trocar as duas pontas ao mesmo tempo.
  if (cmd_id >= 0 && cmd_id <= ultimoCmdId) {
    ackOk(cmd_id, true);
    return;
  }
  if (cmd_id >= 0) ultimoCmdId = cmd_id;

  executarComando(cmd, linha, cmd_id);
}

static void despacharLinha(char* linha) {
  // O '{' e o unico separador das duas vozes, e o corte e feito AQUI porque a
  // linha ainda esta CRUA. Nenhuma transformacao do caminho humano (maiusculas,
  // portao de terminal) pode tocar na linha de maquina: um toupper() em cima de
  // {"cmd":"dispensar"} produz {"CMD":"DISPENSAR"}, que nao casa com chave
  // nenhuma, e um portao que recusasse a linha deixaria o comando sem ACK.
  if (strchr(linha, '{')) { processarJson(linha); return; }
  linhaHumana(linha);
}

// -- Leitura da serial ------------------------------------------------------

void serialPoll() {
  // Buffer CRU para as duas vozes; o corte vem depois, em despacharLinha().
  //
  // Sao DOIS jogos de buffer, e o motivo e a REENTRANCIA: um laco longo do
  // sketch (na placa dos mecanismos, a dispensa) continua lendo a serial para
  // nao perder comando, e chama esta funcao de novo. A linha que o quadro de
  // FORA entregou a despacharLinha() ainda esta sendo interpretada quando o de
  // dentro comeca a escrever — com um buffer so, um pong chegando no meio de
  // um `dispensar` sobrescreveria o proprio `dispensar`, e o sintoma seria um
  // comando executado com os campos de outra mensagem.
  //
  // A profundidade maxima e 2 por construcao: o unico comando que bloqueia e
  // `dispensar`, e ele se recusa enquanto ha slot em operacao. O terceiro
  // nivel DRENA sem despachar — se um comando bloqueante novo aparecer um dia,
  // ele perde linha (visivelmente, no log) em vez de corromper a de ninguem.
  static char   buf[2][MAX_LINHA_BYTES + 1];
  static size_t len[2]         = { 0, 0 };
  static bool   descartando[2] = { false, false };
  static unsigned long ultimoChar[2] = { 0, 0 };
  static uint8_t nivel = 0;

  if (nivel >= 2) {
    while (Serial.available()) Serial.read();
    logMsg("ERRO", "serialPoll reentrante em 3 niveis - linha drenada sem despacho");
    return;
  }
  const uint8_t n = nivel++;

  while (Serial.available()) {
    char c = Serial.read();
    ultimoChar[n] = millis();

    if (c == '\r' || c == '\n') {
      if (descartando[n]) {
        logMsg("ERRO", "linha acima de %u bytes DESCARTADA inteira (nunca partida em duas)",
             (unsigned)MAX_LINHA_BYTES);
        descartando[n] = false;
        len[n] = 0;
        continue;
      }
      if (len[n] > 0) {
        buf[n][len[n]] = '\0';
        len[n] = 0;
        despacharLinha(buf[n]);
      }
      continue;
    }

    if (descartando[n]) continue;
    if (len[n] < MAX_LINHA_BYTES) {
      buf[n][len[n]++] = c;   // CRU: o toupper e do caminho humano, em linhaHumana()
    } else {
      // Linha acima do teto e descartada INTEIRA. Cortar no meio produziria
      // JSON invalido, e as duas metades sumiriam sem erro em lugar nenhum.
      descartando[n] = true;
    }
  }

  // Sem final de linha: executa apos um tempo sem chegar nada. Vale SO para o
  // caminho humano — e o Serial Monitor com "Nenhum final de linha", que e
  // como a bancada costuma estar. Mensagem de maquina sempre termina em \n, e
  // um JSON grande chegando em pedacos seria "executado" pela metade: metade
  // de um `dispensar`, e um ACK que ninguem devia ter recebido.
  if (len[n] > 0 && !descartando[n] && memchr(buf[n], '{', len[n]) == NULL &&
      millis() - ultimoChar[n] >= SERIAL_FIM_LINHA_MS) {
    buf[n][len[n]] = '\0';
    len[n] = 0;
    despacharLinha(buf[n]);
  }

  nivel--;
}

// O buffer de recepcao padrao do ESP32 e de 256 B, e a maior linha do contrato
// vai a 1024. Dimensiona-lo pelo TETO DA LINHA e o que garante que uma mensagem
// nunca chegue partida — e meia linha e JSON invalido, que some sem erro.
// Tem de ser chamado ANTES do Serial.begin().
static void apsenSerialInit() {
  Serial.setRxBufferSize(MAX_LINHA_BYTES);
}
