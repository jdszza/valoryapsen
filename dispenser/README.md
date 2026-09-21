# Dispensers — DUAS placas, DUAS portas, um adapter

Os oito dispensers da célula. Eles têm **duas metades**, e quase tudo neste
diretório se explica por essa divisão:

* **os mecanismos** — oito servos num PCA9685, oito sensores IR, e o terminal
  de calibração que a bancada sempre usou;
* **as telas** — oito TFTs que mostram, slot a slot, o que aquele dispenser
  está fazendo.

**São duas placas em duas portas**, e não é gosto: acionar 8 mecanismos,
desenhar 8 telas e manter a serial não cabe num ESP só. O dono das duas é o
**mesmo** `dispenser-adapter` — ele já vê todo comando que desce e todo evento
que sobe do slot, ou seja, já tem em mãos tudo que as telas precisam mostrar.
Um `tft-adapter` separado obrigaria o central a mandar a mesma informação duas
vezes, e duas cópias divergem.

| pasta | placa | `sub` | o que é |
|---|---|---|---|
| `servos_hub/` | mecanismos | `dispenser` | a calibração de servos que já existia **mais** a voz de máquina |
| `telas_tft/` | telas | `dispenser_tft` | só pinta; não aciona nada |
| `*/apsen_serial.h` | — | — | o núcleo do protocolo, **idêntico** nas duas pastas |

> A Arduino IDE exige que o sketch esteja numa pasta com o mesmo nome — daí
> `servos_hub/servos_hub.ino`.

Compilar:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 dispenser/servos_hub
```

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 dispenser/telas_tft
```

> **Primeira vez com a placa na mão?** O roteiro do zero — gravar, calibrar os
> oito servos, medir o prazo por unidade, provar o transporte sem Docker e só
> então ligar na planta — está em [PRIMEIRO_ENSAIO.md](PRIMEIRO_ENSAIO.md).
> Este README é a referência; aquele é o passo a passo.

## As duas vozes, e o `{` que as separa

Uma mensagem por linha. **Toda linha de máquina tem um `{`**; toda linha sem
`{` é log humano. O adapter extrai o JSON a partir do primeiro `{` e ignora o
resto — então log e JSON podem sair **grudados** na mesma linha, que é o que um
firmware realmente faz no boot:

```
[00:00:01.204] BOOT  servo 3 -> min (12 graus){"cmd":"ping","sub":"dispenser"}
```

Nada do que o sketch de calibração imprimia foi removido, e **nenhum comando do
terminal mudou**: o Monitor Serial da bancada funciona exatamente como antes.

Duas armadilhas deste sketch em particular, e as duas destruiriam a voz de
máquina se a linha JSON passasse por elas:

* **o portão do `MANUT`.** Com o terminal fechado, `processar()` recusa todo
  comando com "Terminal fechado. Digite MANUT para abrir." — o comando do
  adapter seria recusado **sem ACK**, e o orquestrador esperaria o prazo
  inteiro por um evento que nunca viria;
* **o `toupper()` da leitura.** `{"cmd":"dispensar"}` viraria
  `{"CMD":"DISPENSAR"}`, que não casa com chave nenhuma.

Por isso o corte é feito na leitura, **antes de qualquer transformação**: a
linha é guardada crua, o `{` decide para onde ela vai, e só o caminho humano
recebe maiúsculas e passa pelo portão. `tests/test_dispenser_firmware.py` cobra
as duas metades dessa regra.

E o **executar-sem-Enter** (`SERIAL_FIM_LINHA_MS`, 250 ms — o Serial Monitor
configurado com "Nenhum final de linha") vale **só** para o humano. Mensagem de
máquina sempre termina em `\n`; executar por tempo um JSON que chegou em
pedaços seria executar metade de um `dispensar`.

## Eventos (placa → PC)

Todos dentro de `{"evento":{...}}`, todos com `tipo` e `ts`. O contrato
completo está em [`../docs/PROTOCOLO_SERIAL.md`](../docs/PROTOCOLO_SERIAL.md)
§3 e §6.

### Dos mecanismos — o adapter encaminha ao central

| `tipo` | quando | campos |
|---|---|---|
| `carregado` | fim do `carregar` | `dispenser_id`, `os_id`, `medicamento`, `sku`, `categoria`, `quantidade_total`, `quantidade_residual`, `via_residual` |
| `dispensado` | fim do `dispensar` | `dispenser_id`, `os_id`, `medicamento`, `quantidade_dispensada`, `quantidade_alvo`, `falha_mecanica`, `motivo_falha`, `quantidade_residual`, `falha_injetada` |
| `limpeza_ok` | fim do `limpar` | `dispenser_id`, `medicamento_limpo`, `solicitado_por` |
| `erro` | qualquer falha que não seja o `dispensado` com `falha_mecanica` | `dispenser_id`, `os_id`, `codigo_erro`, `descricao` |
| `status` | transição de estado do slot, e periódico | `dispenser_id`, `medicamento`, `sku`, `categoria`, `quantidade`, `status`, `os_id`, `qtd_alvo`, `qtd_dispensada` |
| `telemetria` | periódico | `dispenser_id`, `componente`, `tipo_leitura`, `valor_c`, `unidade` |

### Das telas — param no adapter, saem por `GET /health`

| `tipo` | quando | campos |
|---|---|---|
| `telemetria` | a cada 15 s | `telas_ok`, `brilho_pct` |
| `erro` | uma tela não respondeu ao redesenho | `dispenser_id`, `codigo_erro`, `descricao` |

O central **não tem endpoint de tela** e não decide nada com esses dois.
Encaminhá-los não daria erro — o evento atravessa o adapter sem interpretação —,
daria duas placas misturadas num histórico que hoje é de uma.

Detalhes que valem por si:

* **`limpeza_ok` NÃO carrega `os_id`**, e isso é contrato, não esquecimento:
  limpeza é operação de **slot**, e a chave de espera do orquestrador é
  `limpeza:{dispenser_id}`, sem prefixo de OS.
* **`erro` desbloqueia quem espera.** O central o repassa para as chaves
  `{os_id}:carregado:{slot}` e `{os_id}:dispensado:{slot}`. Ficar mudo faria o
  orquestrador queimar o `TIMEOUT_*` inteiro para descobrir o que a placa já
  sabe.
* **Só transição e periódico, nada no meio.** O adapter descarta telemetria
  **repetida idêntica** (comparação ignorando `ts`); transição nunca é
  filtrada. Por isso o `status` e o `telemetria` saem inteiros do estado, sem
  `millis()` nem contador que ande sozinho: dois periódicos seguidos sem
  mudança são byte a byte iguais fora do carimbo, que é o que faz o filtro
  funcionar.
* **`ts` sai do `epoch` que vem no pong.** A placa não tem RTC nem NTP: antes
  do primeiro pong o carimbo sai em 1970 — visivelmente errado, que é melhor
  que plausível e errado.
* **Aspas e barras somem na ENTRADA.** Nome de medicamento vem do catálogo do
  central; reemitir uma aspa crua produziria JSON inválido, e o adapter
  descartaria a linha **inteira** sem erro.

### A telemetria não é temperatura, e isso é decisão

O `dispenser_simulator` emite temperatura porque é simulador: ele **inventa** o
número. Esta placa não tem sensor de temperatura, e o que sai daqui é gravado
em `leituras_sensores` e lido depois como se tivesse sido medido.

A leitura interna do ESP32 foi considerada e recusada: ela é **uma**
temperatura, a do SoC, e sairia oito vezes com oito rótulos diferentes
(`dispenser_1`..`dispenser_8`) — oito leituras iguais atribuídas a oito
mecanismos que a placa nem toca. Pior que não medir é medir a coisa errada com
o nome da certa: o app de manutenção pinta temperatura de vermelho a 65 °C, e
estaria pintando o chip.

O que esta placa **mede** de verdade, por slot, é o sensor IR: quantas unidades
passaram por ele desde o boot. É uso acumulado do mecanismo, que é justamente o
número de que a manutenção preventiva precisa. Os nomes de campo são os do
contrato (`valor_c` nasceu Celsius e continua sendo a chave que o central lê);
quem diz o que o número é são `tipo_leitura` (`pulsos_ir`) e `unidade` (`un`).
Nenhuma tela se confunde — tanto `necessidades.itens_componentes` quanto a aba
de temperaturas do app de manutenção só olham para `tipo == "temperatura"`.

## Comandos

### Do PC, em JSON (com ACK e `cmd_id`)

Para os mecanismos (`sub: "dispenser"`):

```
{"cmd":"carregar","cmd_id":7,"dispenser_id":3,"medicamento":"Dipirona 500mg",
 "sku":"APSEN-DIP-500","categoria":"analgesico","quantidade":10,"os_id":"..."}
{"cmd":"dispensar","cmd_id":8,"dispenser_id":3,"os_id":"..."}
{"cmd":"limpar","cmd_id":9,"dispenser_id":3,"solicitado_por":"tecnico"}
```

Para as telas (`sub: "dispenser_tft"`):

```
{"cmd":"slot","cmd_id":4,"dispenser_id":3,"medicamento":"...","sku":"...",
 "categoria":"...","quantidade_alvo":10,"quantidade_dispensada":0,
 "quantidade_residual":0,"status":"pronto","os_id":"..."}
{"cmd":"estado_celula","cmd_id":5,"trava_ativa":true,"trava_slot_id":3,
 "os_id":"...","trava_resumo":"divergência de peso"}
```

O ACK (`{"resp":"ok","cmd_id":n}`) diz que a placa **aceitou**, não que
terminou: um `dispensar` de 10 unidades leva dezenas de segundos. O resultado
chega depois, como evento — o prazo dele é o `TIMEOUT_DISPENSA` do
orquestrador, não o `ack_timeout_s` de 2 s.

**`cmd_id` repetido responde ACK de novo SEM executar.** Reenviar `dispensar` é
dose dobrada no leito. O contador do adapter nasce em 1 a cada restart do
processo dele, e a placa percebe a volta pelo silêncio: o adapter só responde
pong ao **nosso** ping, então vários pings sem pong zeram o contador local. Sem
isso, depois de um restart do adapter a placa responderia ACK sem executar, e o
orquestrador esperaria para sempre um evento que ninguém ia produzir.

### Do humano, no Monitor Serial (só os mecanismos)

Todos os do sketch de calibração, sem mudança:

| comando | efeito |
|---|---|
| `MANUT` / `MANUTF` | abre / fecha o terminal de manutenção |
| `SEL <0-7>` | seleciona o servo e vai para o meio da faixa |
| `MOVE <ang>` / `+` / `-` / `+5` / `-5` | move o selecionado, sem limite |
| `MIN` / `MAX` / `MIN <a> MAX <b>` / `SET <min> <max>` | grava a calibração na NVS |
| `GOMIN` / `GOMAX` / `TEST [ciclos]` / `TESTALL` | confere |
| `RESET` / `RESETALL` | apaga da NVS e volta ao `CAL_BACKUP` do código |
| `S <n> <ang>` | move respeitando a calibração (modo NORMAL) |
| `SHOW` / `EXPORT` / `SENS` / `IRLOG` / `BIP` / `STATUS [s]` / `CAL` / `NORMAL` / `HELP` | |

**Com uma dispensa em curso, a voz do humano espera.** O laço de dispensa lê a
serial para não perder comando, e um `TESTALL` digitado nesse instante moveria
servos de **dentro** do laço que já está movendo um — dois movimentos
intercalados, e o pulso de um slot contado no prazo do outro. A linha humana
recebe "Ocupado: dispensando D*n*" e nada acontece. A voz de máquina continua
passando, porque nenhum comando dela move nada enquanto há slot em operação.

A placa das telas **não tem terminal**: não há nada nela para calibrar, e um
terminal só existiria para ser mantido.

## A contagem por IR: uma unidade por ciclo

Para soltar *N* unidades, o servo faz *N* ciclos `min → max → min`, e **cada
ciclo só conta se o sensor IR daquele slot pulsar**. O debounce de `irPoll` é o
que impede o mesmo comprimido de ser contado duas vezes por tremida do sensor.

| constante | valor | o que é |
|---|---|---|
| `DISPENSA_SERVO_MS` | 250 ms | quanto o braço leva de um extremo ao outro, com carga |
| `DISPENSA_TIMEOUT_UNIDADE_MS` | 1500 ms | prazo do pulso, contado do **início** do ciclo |

> Os dois são **A CONFIRMAR NA BANCADA**: eles saem de medida, não de gosto.
> Curto demais é falha mecânica onde não houve; longo demais gasta o
> `TIMEOUT_DISPENSA` do orquestrador, que corre do outro lado. O passo 3 de
> [PRIMEIRO_ENSAIO.md](PRIMEIRO_ENSAIO.md) mostra como medir.

**Pulso que não vem no prazo INTERROMPE a dispensa.** Não tenta o mesmo
comprimido de novo — se ele caiu sem o sensor ver, repetir é dose dobrada — e
não segue para o próximo: se o mecanismo atolou, insistir agrava. O evento sai
com `quantidade_dispensada` = o que o IR contou, `falha_mecanica: true` e
`motivo_falha: "falha_mecanica"`, e quem decide o que fazer com o déficit é o
Triple Check, com três fontes.

O servo volta ao **min calibrado** em toda saída, inclusive na falha, e o
movimento usa `moverCalibrado`, nunca `moverRaw`: fora do `MANUT` é a
calibração que impede o braço de bater no fim de curso no meio de uma OS.

### Uma diferença de VALOR em relação ao simulador, e ela é deliberada

O `dispenser_simulator` desconta do estoque a **quantidade alvo**; este
firmware desconta **o que o IR contou**. O simulador pode fazer o que faz
porque não tem sensor. Aqui, descontar o alvo depois de uma interrupção na
unidade 3 de 10 diria ao central que o slot está vazio com sete comprimidos
dentro dele. Número gravado no histórico tem que ter vindo de uma medição.

Os **campos** do evento são os mesmos, com os mesmos nomes — é o que o adapter
repassa cru ao central, e um nome diferente não daria erro em lugar nenhum:
daria uma coluna vazia no banco.

### `carregar` e `limpar` não movem nada

Nesta célula não há mecanismo de carga nem de descarte: quem enche o cartucho e
quem retira o resíduo é uma pessoa. O firmware **registra** o que o central
declarou e confirma na hora — fingir um tempo de carga só atrasaria a OS. Quem
confere que as unidades estão mesmo lá são a estação de visão e a balança, e é
para isso que o Triple Check existe.

Consequência: **a carga vive em RAM e não na NVS**. Ela não sobrevive a uma
queda de energia — e não deve, porque quem tira comprimido do cartucho é uma
pessoa, e gravar daria ao central um estoque que a bancada talvez não tenha
mais.

`limpar` é recusado com `erro` / `codigo_erro: "limpeza_em_operacao"` — o 409
que o central já sabe tratar — quando o slot está carregando ou dispensando.
"pronto", "erro" e "limpo" são estados parados, e limpar slot com estoque
encalhado é exatamente o propósito do botão do app de manutenção.

## Injeção de falha

`injetar_falha` atravessa o adapter **sem interpretação** e chega à placa, que
a trata num ramo isolado **antes** de qualquer outra decisão — é por isso que a
injeção funciona com `MODO_APRESENTACAO` ligado: o modo desliga o acaso, não a
capacidade de mostrar o Triple Check.

O único valor reconhecido é **`falha_mecanica_dispenser`**, o mesmo de
`central-computer/injecao.TIPO_FALHA_MECANICA`. Ele solta **uma unidade a
menos**, e o número não é de gosto: as ordens padrão vão até 15 unidades por
slot, e 1/15 = 6,7% já passa da tolerância de 5% da balança — a menor falha
possível já é detectável em qualquer template.

Valor desconhecido é **ignorado com aviso**, nunca recusado: um typo do console
não pode virar dispensa diferente da que se pediu, e o comando em si está
correto.

## O que as telas mostram

É a tabela de §6, e nada além dela:

| estado recebido | a tela do slot mostra |
|---|---|
| `trava_ativa=false` | medicamento · SKU · dispensada/alvo · residual · status |
| `trava_ativa=true` e `trava_slot_id` == meu id | alerta + **AGUARDE SUPERVISOR** + `trava_resumo` |
| `trava_ativa=true` e outro slot | **PARADO — D{n}**, conteúdo esmaecido |

`trava_slot_id` pode vir **nulo** (trava sem slot), mas a **chave** vem sempre:
é ela que diz a cada tela se é este slot ou outro. Chave ausente é comando mal
formado e leva ACK negativo; chave com `null` é uma trava que não é de ninguém.

`trava_resumo` tem teto de **48 caracteres** e já vem cortado do central — ele
sai da CATEGORIA da divergência ("divergência de peso", "contagem divergente"),
nunca do motivo formatado, que passa de 240 caracteres. A placa não tenta
completar o texto nem pedir mais: a tela do slot responde uma pergunta só, *é
este slot?*. O motivo completo é do display de 7" e da web, onde o supervisor
decide.

**Falha de tela nunca muda o caminho do dispenser.** O ACK sai **antes** da
pintura; a placa não recusa comando e não atrasa nada porque um display não
respondeu. Tela errada é cosmética; dispensa atrasada não é.

### O painel ainda não foi escolhido, e o código deixa isso em aberto

Modelo, driver, tamanho, como as oito telas são selecionadas (oito CS de SPI,
um mux, ou I²C com endereço por tela) e onde entra o PWM do brilho são decisões
**físicas**. Por isso a camada de desenho fica atrás de quatro funções finas —
`telaIniciar`, `telaLimpar`, `telaLinha`, `telaMostrar` — e a escolha é um
`#define TELA_DRIVER`.

O default é **`TELA_DRIVER_LOG`**, e não um driver concreto, por duas razões:

1. ele compila em qualquer máquina, sem biblioteca a instalar. Um default que
   exigisse `TFT_eSPI` ou `Adafruit_ILI9341` quebraria o build de todo mundo
   por causa de um display que ninguém escolheu ainda;
2. ele é a ferramenta de bancada de verdade: o Monitor Serial mostra
   exatamente o que cada tela mostraria. Quando um painel não acender, é assim
   que se descobre se o problema é a ligação ou a mensagem.

O bloco `TELA_DRIVER_ILI9341_SPI` está escrito e **não compilado**, pronto para
a troca. Ele usa `Adafruit_GFX` + driver e **não** `TFT_eSPI` por um motivo
prático: o `TFT_eSPI` se configura por um `User_Setup.h` **dentro da pasta da
biblioteca**, fora deste repositório — a ligação da bancada ficaria gravada num
arquivo que nenhum commit registra. Aqui os pinos estão numa tabela no topo do
sketch, versionados.

## `apsen_serial.h`: duas cópias, e um teste que as compara

O núcleo do protocolo — enquadramento, relógio pelo pong, scanner de JSON, ACK,
`cmd_id` idempotente e o ping que identifica a porta — é **idêntico** nas duas
pastas.

Cópia e não biblioteca compartilhada porque a Arduino IDE compila a **pasta** do
sketch: um `#include "../apsen_serial.h"` não sobrevive à cópia que o build faz
para o diretório temporário. As saídas eram transformar o header numa
biblioteca Arduino instalada na máquina — infraestrutura que faz o firmware
parar de compilar em qualquer máquina que não a tenha — ou duplicar. É a mesma
avaliação que `serial_link.py` já registra para as suas três cópias, com a
mesma conclusão: com duas cópias, o teste de igualdade custa menos que a
infraestrutura. **Se um dia forem cinco, a conta muda.**

`tests/test_dispenser_firmware.py` reprova se as duas divergirem.

O header também guarda a razão de a linha ter teto nos **dois sentidos, com
decisões opostas**: entrando, linha acima de 1024 B é descartada **inteira** e
logada — nunca partida em duas, porque meia linha é JSON inválido e as duas
metades sumiriam em silêncio; saindo, mensagem que não cabe **não sai**, porque
comando truncado é JSON inválido, o outro lado o descarta, e quem esperava o
ACK espera para sempre.

E ele lê a serial com **dois jogos de buffer**, por causa da reentrância: o
laço de dispensa continua lendo a serial para não perder comando, e chama o
leitor de novo. Com um buffer só, um pong chegando no meio de um `dispensar`
sobrescreveria o próprio `dispensar` sendo interpretado, e o sintoma seria um
comando executado com os campos de outra mensagem.

## Ligar na célula

```
DISPENSER_TRANSPORTE=serial
DISPENSER_SERIAL_URL=COM5          # a COM fixada da placa dos mecanismos
DISPENSER_SERIAL_BAUD=115200
DISPENSER_ACK_TIMEOUT_S=2

DISPENSER_TFT_TRANSPORTE=serial
DISPENSER_TFT_SERIAL_URL=COM6      # a COM fixada da placa das telas
DISPENSER_TFT_SERIAL_BAUD=115200
DISPENSER_TFT_ACK_TIMEOUT_S=2
```

**Esse bloco já está escrito, e o lugar dele é
[`../dispenser-adapter/iniciar_host.bat`](../dispenser-adapter/iniciar_host.bat).**
Você edita **duas linhas** — as duas COM, no bloco do topo — e roda o `.bat`;
ele liga os dois transportes seriais, aponta o central e sobe o adapter na
porta 8100. Não vai no `.env` da raiz: na célula montada este processo roda
fora do Docker, e ninguém lê o `.env` dele. É a mesma regra dos outros três
processos com placa — [Onde escrever o número da COM](../README.md#onde-escrever-o-número-da-com).

`DISPENSER_TRANSPORTE=http` (o **default**) mantém o `dispenser_simulator` e o
comportamento anterior a este firmware, byte a byte — é o que faz a suíte, o CI
e a demonstração em Docker não mudarem de resultado. Para as telas, `http`
significa **"sem telas"**: o `slot` não vai a lugar nenhum e o `estado_celula`
desce ao simulador dos mecanismos, que só loga.

⚠️ **As duas COM têm que ser FIXADAS no Windows e as duas variáveis
preenchidas.** Com a URL vazia o adapter varre todas as portas, abrindo as dos
outros processos por até 9,5 s cada — com cinco placas na célula isso é boot
não-determinístico, em que uma placa às vezes não é achada. O passo a passo
está no README da raiz, seção
[Portas seriais na célula montada](../README.md), e em
[`../docs/DEPLOY_WINDOWS.md`](../docs/DEPLOY_WINDOWS.md).

**Feche o Serial Monitor antes de subir o adapter**: no Windows a COM é
exclusiva de um processo, e o segundo toma `ACCESS_DENIED`.

O USB é **só dados**: a alimentação dos servos e das telas é externa.

## Sem placa

`tests/fakes/placa_dispenser.py` e `tests/fakes/placa_dispenser_tft.py` falam
este contrato inteiro por `socket://` — um handler de URL do próprio pyserial,
então o caminho de abertura exercitado é o de verdade. Elas são o duplo do
firmware e o documento executável contra o qual ele foi escrito.

```bash
pytest tests/test_dispenser_firmware.py tests/test_dispenser_tft.py tests/test_protocolo_placas.py tests/test_serial_link.py
```

## A CONFIRMAR NA BANCADA

Nenhuma destas se decide no editor. Elas estão marcadas assim também no código.

| o quê | onde | por quê |
|---|---|---|
| **Os pinos do I²C do PCA9685** | `servos_hub.ino`, `PCA_SDA` / `PCA_SCL` | o esquemático da REV 1.0 mostra D21/D22 como NC e o código diz GPIO23/GPIO22. Quem decide é o multímetro. As mensagens do boot imprimem os valores das constantes com `%d` — mensagem de erro que aponta para o pino errado manda quem lê procurar no lugar errado |
| **Calibrar os 8 servos** e colar o `EXPORT` no `CAL_BACKUP` | terminal `MANUT` | sem calibração própria o `min` é 0° e pode estar **além** do fim de curso. O boot **não move** servo não calibrado, de propósito, e avisa no log |
| **`DISPENSA_TIMEOUT_UNIDADE_MS`** e **`DISPENSA_SERVO_MS`** | `servos_hub.ino` | medir o intervalo real entre o ciclo e o pulso do IR, com o medicamento de verdade |
| **Modelo, driver e ligação dos 8 TFTs** | `telas_tft.ino`, tabela do topo | escolha física; o código já está atrás de uma interface fina |
| **`TFT_PIN_CS[]`, `TFT_PIN_DC`, `TFT_PIN_RST`, `TFT_PIN_BRILHO`** | `telas_tft.ino` | dependem da escolha acima |
| **`TELA_LINHAS` e `TELA_COLUNAS`** | `telas_tft.ino` | o layout de §6 precisa de cinco campos mais o cabeçalho |
| **Fixar as duas COM** e preencher `DISPENSER_SERIAL_URL` / `DISPENSER_TFT_SERIAL_URL` | `.env` do host | ver acima |
