# Protocolo serial APSEN — adapters ↔ firmware

Este arquivo é o CONTRATO que os três firmwares vão implementar, e é contra ele
que `tests/test_protocolo_placas.py` compara o código Python dos dois lados (os
adapters e as placas falsas de `tests/fakes/`). Firmware que discorde daqui não
falha com erro: ele fica mudo, e o sintoma chega como OS abortada por timeout
num slot que o operador vai procurar na bancada.

Uma porta por firmware, uma linha JSON por mensagem:

| adapter             | firmware                    | porta | subsistema (`sub`) | código |
|---------------------|-----------------------------|-------|--------------------|--------|
| `dispenser-adapter` | os 8 dispensers (mecanismos)| uma   | `dispenser`        | [`dispenser/servos_hub/`](../dispenser/README.md) |
| `dispenser-adapter` | as 8 telas TFT              | uma (a SEGUNDA porta do mesmo adapter) | `dispenser_tft` | [`dispenser/telas_tft/`](../dispenser/README.md) |
| `cnc-adapter`       | a mesa CNC                  | uma   | `cnc`              | — |
| `weight-adapter`    | a balança HX711             | uma   | `weight`           | [`weight/balanca2_3/`](../weight/README.md) |

Três dos quatro firmwares estão escritos. A balança foi a primeira
(`weight/balanca2_3/`, manual em `weight/README.md`) e as duas placas do
dispenser vieram depois (`dispenser/`, manual em `dispenser/README.md`). **A
mesa CNC continua sem código no repositório**, e para ela este documento segue
sendo o que alguém vai ler para escrevê-la.

O contrato **não mudou** quando o firmware apareceu: ele deixou de ser promessa,
que é coisa diferente. Quem confronta as cópias é `tests/test_protocolo_placas.py`
(documento × adapter × placa falsa) e, do lado do firmware,
`tests/test_balanca_serial.py` e `tests/test_dispenser_firmware.py`.

O `vision-adapter` **não** entra: a visão continua por HTTP. E `dispenser` e
`dispenser_tft` são **duas portas físicas do mesmo adapter**: acionar os 8
mecanismos, desenhar 8 telas e manter a serial não cabe num ESP só, e quem já
vê todo comando que desce e todo evento que sobe do slot é o dispenser-adapter
— ver a seção 6.

---

## 1. Enquadramento

* **Uma linha JSON por mensagem**, terminada em `\n`. Nada de framing binário.
* **Teto de 1024 bytes por linha**, nos dois sentidos. O firmware lê para um
  buffer fixo; linha maior é DESCARTADA de propósito e logada — nunca partida em
  duas, porque meia linha é JSON inválido e a mensagem sumiria sem rastro. O
  teto sai da ORIGEM do dado: a maior mensagem do contrato é o evento `peso_ok`
  (~450 B com todos os campos).
* **Lixo antes do `{` não mata a mensagem.** O firmware imprime log e JSON no
  mesmo Serial e eles saem grudados (`Iniciando HX711...{"cmd":"ping",...}`).
  Os dois lados extraem o JSON a partir do primeiro `{`; o que vier antes é log.
* **Linha sem JSON válido é log**, nunca exceção.
* `115200` baud, 8N1.
* Codificação UTF-8 (nomes de medicamento têm acento).

## 2. Mensagens de serviço — iguais nos três subsistemas

| direção | mensagem | quando |
|---|---|---|
| placa → adapter | `{"cmd":"ping","sub":"<subsistema>"}` | no boot e periodicamente |
| adapter → placa | `{"resp":"pong","epoch":<unix>,"sessao":<unix>}` | resposta ao ping |
| adapter → placa | `{"cmd":"<nome>","cmd_id":<n>,"sessao":<unix>,...campos}` | comando |
| placa → adapter | `{"resp":"ok","cmd_id":<n>}` | ACK positivo |
| placa → adapter | `{"resp":"erro","cmd_id":<n>,"msg":"..."}` | ACK negativo |
| placa → adapter | `{"evento":{...payload...}}` | resultado e telemetria |

**Quem inicia o ping é sempre a placa.** É assim que a porta é identificada: com
`<SUB>_SERIAL_URL` vazia, o adapter varre as portas e aceita a primeira que
emitir o ping DAQUELE `sub`. A detecção **não** usa VID/PID — o VID/PID de um
conversor USB-serial é o mesmo em placas de fabricantes diferentes, e casar por
ele acha a placa errada, que aqui significa mandar `dispensar` para a balança.

O `epoch` do pong é como a placa acerta o relógio sem NTP.

`sessao` é o epoch de BOOT do processo do adapter, e ela não muda enquanto ele
vive. Ver "`sessao` — o contador nasce de novo, e a placa precisa saber".

### ACK não é conclusão

O ACK diz que a placa **aceitou** o comando, não que o executou. Um `mover` leva
segundos; um `dispensar` leva mais. O resultado chega **depois**, como `evento`
assíncrono, e é ele que o orquestrador espera (`aguardar_evento`).

* prazo de ACK: curto e configurável (`<SUB>_ACK_TIMEOUT_S`, default **2 s**);
  é só dele que o endpoint `/comandos/*` depende para responder;
* prazo de CONCLUSÃO: continua no orquestrador (`TIMEOUT_CARREGAMENTO`,
  `TIMEOUT_DISPENSA`, `TIMEOUT_PESO`, ...), e não muda com o transporte.

### `cmd_id` — todo comando leva, e repetição é IGNORADA pela placa

O adapter gera um inteiro **monotônico por porta**, a partir de 1. A placa guarda
o último `cmd_id` executado **por subsistema** e, ao receber um `cmd_id` menor ou
igual, responde o ACK de novo **sem executar**.

Com ACK perdido, a reação natural de qualquer camada é reenviar — e reenviar
`dispensar` é dose dobrada no leito. O `serial_link.py` não reenvia nada, nem por
engano nem por configuração, mas o `cmd_id` existe para que a ponta de lá
sobreviva a um reenvio de qualquer origem (um restart do adapter, um operador
repetindo a ação, uma versão futura que decida retentar). É a parte do protocolo
que não dá para acrescentar depois sem trocar as duas pontas ao mesmo tempo.

### `sessao` — o contador nasce de novo, e a placa precisa saber

O `cmd_id` é monotônico **dentro de uma sessão do adapter**: ele nasce em 1 a
cada `LinkSerial` novo. A regra de idempotência acima, sozinha, transforma isso
num modo de falhar: um restart de 2 a 5 s do processo (o `.bat` da bancada
reinicia sozinho) faz o contador voltar a 1 com a placa ainda em
`ultimoCmdId = 47`, e **todo comando até 47 recebe ACK POSITIVO sem executar**.

Com o ciclo por relógio o sintoma mudou de lugar: não é só timeout — o central
segue o cronograma e dispara o `dispensar` de um `mover` que a mesa nunca fez.

Por isso o adapter manda `sessao` — o epoch de boot do processo dele — no pong
**e em todo comando**. A placa guarda a última que viu e, quando ela MUDA, zera
`ultimoCmdId`.

Três detalhes que são contrato, não implementação:

* **a comparação é de DIFERENÇA, não de ordem.** Relógio do host que ande para
  trás (NTP, fuso, máquina sem RTC) continua sendo uma sessão nova, que é o que
  importa;
* **`sessao` ausente ou zero significa "ainda não sei"**, e a primeira que a
  placa vê é adotada sem zerar nada. Zerar na primeira faria todo boot da placa
  descartar o primeiro comando legítimo;
* **a adoção vem ANTES da checagem de idempotência**, no caminho do comando. O
  pong também a carrega, mas o primeiro comando depois de um restart pode chegar
  antes do primeiro pong — e é justamente ele que não pode ser respondido como
  repetido.

A heurística anterior continua no firmware como rede de segurança: silêncio de
mais de `SESSAO_SILENCIO_MS` (10 s) sem pong também zera o contador. Ela cobre o
adapter que ficou fora por muito tempo; o que ela **não** cobre é o restart
rápido, que é o comum — 2 s de queda não chegam perto de três pings perdidos.

### Telemetria periódica não pode encher o canal

O dispenser emite status dos 8 slots a cada 15 s e a balança tem leitura
contínua. Num canal de 115200 baud compartilhado com os ACKs que o adapter está
esperando, despejo periódico compete com o caminho crítico. Portanto:

* a placa manda **transição na hora** e periódico com intervalo configurável;
* o adapter **descarta telemetria repetida idêntica** (comparação ignorando
  `ts`) em vez de encaminhá-la ao central — mesma regra que o central já aplica
  no broadcast do WebSocket. Os tipos sujeitos ao filtro são `telemetria`,
  `status`, `movendo` e `retornando`; transição nunca é filtrada.

### `injetar_falha`

Atravessa o adapter **sem interpretação**, até a placa — exatamente como já
atravessa até o simulador. É o que mantém a demonstração de falhas do console
funcionando com hardware real. A placa trata o valor num ramo isolado, antes de
qualquer sorteio, e valor desconhecido é ignorado **com aviso**.

---

## 3. `dispenser` — os 8 dispensers

**Firmware escrito:** `dispenser/servos_hub/servos_hub.ino` (ESP32 + PCA9685 +
8 servos + 8 TCRT5000). O [`dispenser/README.md`](../dispenser/README.md) é o
manual dele — as duas vozes na mesma porta, a contagem por IR, o terminal de
calibração que continua valendo e os pontos a confirmar na bancada. O que está
aqui é o contrato; o que está lá é a placa.

### Comandos (adapter → placa)

| `cmd` | campos | efeito |
|---|---|---|
| `carregar` | `dispenser_id`, `medicamento`, `sku`, `categoria`, `quantidade`, `os_id` | carrega o slot |
| `dispensar` | `dispenser_id`, `os_id`, `injetar_falha` (opcional) | dispensa a carga do slot |
| `limpar` | `dispenser_id`, `solicitado_por` | esvazia o slot |

### Eventos (placa → adapter, dentro de `{"evento":{...}}`)

| `tipo` | campos principais |
|---|---|
| `carregado` | `dispenser_id`, `os_id`, `medicamento`, `sku`, `categoria`, `quantidade_total`, `quantidade_residual`, `via_residual`, `ts` |
| `dispensado` | `dispenser_id`, `os_id`, `medicamento`, `quantidade_dispensada`, `quantidade_alvo`, `falha_mecanica`, `motivo_falha`, `quantidade_residual`, `falha_injetada`, `ts` |
| `limpeza_ok` | `dispenser_id`, `medicamento_limpo`, `solicitado_por`, `ts` |
| `erro` | `dispenser_id`, `os_id`, `codigo_erro`, `descricao`, `ts` |
| `telemetria` | `dispenser_id`, `componente`, `tipo_leitura`, `valor_c`, `unidade`, `ts` |
| `status` | `dispenser_id`, `status`, `os_id`, `medicamento`, `quantidade`, ... `ts` |

**`limpeza_ok` NÃO carrega `os_id`**, e isso é contrato, não esquecimento:
limpeza é operação de slot, não de OS. A chave de espera do orquestrador é
`limpeza:{dispenser_id}`, sem prefixo de OS (ver CLAUDE.md, "Eventos de limpeza
não têm `os_id`").

`erro` com `codigo_erro: "limpeza_em_operacao"` é a recusa de limpar um slot que
está carregando ou dispensando.

---

## 4. `cnc` — a mesa CNC

**Firmware escrito:** `cnc/receitas_manuais/receitas_manuais.ino` (ESP32 +
CoreXY). O [`cnc/README.md`](../cnc/README.md) é o manual dele — as duas vozes
da porta, as receitas de bancada, os trajetos medidos e o que falta conferir na
bancada. O que está aqui é o contrato; o que está lá é a placa.

**O central agenda, a mesa avisa, ninguém espera o aviso.** É a frase que
resume o modelo, e ela inverte o que este documento dizia antes. O ciclo
`mover` → `dispensar` não é mais um handshake: o central calcula QUANDO cada
peça acontece e dispara na hora marcada, tenha o evento chegado ou não. O
evento da placa serve para **registrar** — a posição medida, a contagem — e, no
máximo, para **cancelar**. Ver "Por que o relógio e não a confirmação", abaixo.

### Comandos (adapter → placa)

| `cmd` | campos | efeito |
|---|---|---|
| `mover` | `dispenser_alvo`, `os_id`, `receita`, `ciclo_atual`, `total_ciclos` | move a mesa até o slot |
| `homing` | `os_id`, `posicao_x` (opcional), `posicao_y` (opcional) | volta ao HOME |
| `estado_celula` | `trava_ativa`, `trava_slot_id` (opcional), `os_id` (opcional), `trava_resumo` (opcional) | trava do Triple Check |

**A coordenada NÃO vem no comando — o endereço é o dispenser.** O roteiro é
medido na bancada e gravado na placa, waypoint a waypoint, indexado pelo
DISPENSER; o firmware valida a faixa (`1 <= dispenser_alvo <= NUM_SLOTS`) e vai
ao waypoint dele. Em troca, é a placa que REPORTA onde parou, no `posicionado`,
e é essa medida que o central registra.

O waypoint é por dispenser e não por posição na receita porque a rota é decidida
no central em tempo de execução (serpentina) e muda quando um slot sai da OS:
"vá ao ponto 3" seria outro dispenser no dia seguinte, a mesa iria ao lugar
errado, e o sintoma chegaria como divergência de SKU num slot só —
indistinguível de uma falha mecânica.

`receita` é a letra A–J do slot da NVS: ela diz QUAL ordem está em execução, vai
para o log da placa, e **não** é usada para achar a posição.

### Eventos (placa → adapter)

| `tipo` | campos principais |
|---|---|
| `posicionado` | `os_id`, `dispenser_alvo`, `posicao_x`, `posicao_y`, `ciclo_atual`, `total_ciclos`, `ts` |
| `concluido` | `os_id`, `posicao_x`, `posicao_y`, `ts` |
| `erro` | `os_id`, `dispenser_alvo`, `codigo_erro`, `descricao`, `ts` |
| `telemetria` | `componente`, `tipo_leitura`, `valor`, `unidade`, `ts` |

Os três primeiros são transição, e os três viram linha em `cnc_eventos` no
central (CLAUDE.md, "Só transição vira linha").

**`estado_celula` PRODUZ evento na mesa**, e é a diferença dela para a placa das
telas, onde o mesmo comando só pinta. A tabela acima não dizia isso, e o
silêncio custou dois defeitos que ficaram de pé com a suíte verde.

Na transição para TRAVADA, e só na transição:

| a mesa estava… | o que sai |
|---|---|
| andando | `erro` com `codigo_erro: travado`, e depois o retorno ao HOME |
| parada | só o retorno ao HOME |

Com um `mover` em curso, quem emite o `erro` é o próprio comando interrompido —
é lá que se sabe qual OS e qual dispenser esperavam a chegada. A mesa vai ao HOME
nos dois casos porque é onde o supervisor espera encontrá-la para mexer na
bancada.

**O retorno ao HOME segue a regra do `homing`:** falhou, sai `erro` com
`homing_falhou` e **não** sai `concluido`. Um `concluido` ali afirmaria que a
mesa está no HOME quando ela está onde o eixo travou — e, pior que no `cmdHoming`,
`ja_fez_homing` fica `false` e todo `mover` seguinte é recusado com `sem_homing`:
o supervisor libera a trava e a OS SEGUINTE aborta com um erro que aponta para o
lugar errado.

**O `concluido` do retorno leva o `os_id` da OS que travou**, e quem decide o
que fazer com isso é o central: ele NÃO fecha OS travada (CLAUDE.md, a guarda em
`_handle_evento_cnc`). A mesa reporta o que ela fez; ela não diz o que a OS
virou.

A LIBERAÇÃO não produz evento nenhum e não refaz homing: a mesa já está no HOME
desde a ativação, e um segundo homing custaria segundos no exato momento em que
o supervisor acabou de liberar a produção.

**Nada de periódico durante o movimento — no SERIAL.** `movendo` e `retornando`
continuam existindo no `cnc_simulator`, que fala HTTP e alimenta a trajetória
ao vivo do dashboard; o FIRMWARE não os emite, e a diferença não é
esquecimento. Uma linha de ~200 B a 115200 baud custa ~17 ms, o curso mais
longo da célula dura ~2,0 s, e o central dispara o `dispensar` por relógio:
cada linha emitida no caminho empurra a chegada real para **depois** da hora
agendada — ou seja, comprimido caindo com a mesa ainda em trânsito. Por HTTP,
com uma conexão por evento, esse custo não existe.

| `tipo` | campos principais | quem emite |
|---|---|---|
| `movendo` | `os_id`, `dispenser_alvo`, `posicao_x`, `posicao_y`, `passo`, `total_passos`, `progresso_pct`, `ts` | só o `cnc_simulator` (HTTP) |
| `retornando` | `os_id`, `posicao_x`, `posicao_y`, `ts` | só o `cnc_simulator` (HTTP) |

### `codigo_erro` — vocabulário FECHADO

Todo evento `erro` da mesa carrega um destes, e nenhum outro. A lista é fechada
porque quem lê o outro lado é código: o central agrupa alarme por `codigo_erro`,
e a tela de necessidades manda o técnico à peça a partir dele. Um código
inventado na hora não dá erro em lugar nenhum — vira uma linha que ninguém sabe
ler.

| `codigo_erro` | comando | quando |
|---|---|---|
| `dispenser_invalido` | `mover` | `dispenser_alvo` fora de 1..`NUM_SLOTS` |
| `fora_do_envelope` | `mover` | o waypoint calibrado caiu fora dos limites da mesa |
| `sem_homing` | `mover` | origem nunca estabelecida — mande `homing` antes |
| `limit_disparado` | `mover` | fim de curso acionado, antes ou durante o movimento |
| `travado` | `mover` | trava do Triple Check ativa |
| `homing_falhou` | `homing` | um dos eixos não achou o fim de curso no prazo |
| `receita_desconhecida` | `mover` | **só o `cnc_simulator`** — a receita não está entre as gravadas |

`receita_desconhecida` é o único código que não vem do firmware, e a exceção
está aqui em vez de escondida: a placa ACEITA qualquer `receita` (ela é log, não
endereço — quem acha a posição é o waypoint do dispensador), enquanto o
simulador a valida para encenar, na demonstração, a ordem cujo roteiro ninguém
gravou. Os dois lados emitem `dispenser_invalido` para a mesma condição.

**Recusa é ACK negativo E evento `erro`, sempre os dois** — e eles vão para
leitores diferentes. O ACK negativo vira 502 no adapter e chega ao orquestrador
como `cmd_mover` falso: é ele que cancela o cronograma ANTES da hora do
`dispensar`. O evento vira alarme e linha de histórico, com o código que diz
onde ir. Só o ACK deixaria a recusa sem rastro; só o evento deixaria o central
seguindo o relógio até despejar medicamento numa mesa que não chegou.

A única falha que acontece DEPOIS do ACK positivo é o fim de curso disparando
com a mesa andando — aí não há ACK a corrigir, e o evento é o aviso. Tudo que
dá para conferir antes é conferido antes, por isso.

`homing` que estoura o prazo **não** emite `concluido`: emite `erro` com
`homing_falhou` e a placa fica sem origem, recusando todo `mover` seguinte. Um
`concluido` ali afirmaria que a mesa está no HOME, e ela está onde o eixo
travou.

### Por que o relógio e não a confirmação

O modelo anterior era um handshake: o `dispensar` só saía depois de o
`posicionado` chegar, e o ciclo só fechava com o `dispensado`. Ele foi trocado,
e o que se ganhou é previsibilidade — o tempo de uma OS passa a ser a soma de um
cronograma que o central calcula, em vez de depender de quantos eventos se
perderam no caminho.

**O que custa, e este parágrafo existe para que o custo não seja redescoberto
daqui a seis meses:**

* **Um `dispensado` que não chega não para nada na hora.** O prazo vence, o
  ciclo é marcado `sem_confirmacao`, e a OS segue para o próximo slot. Quem
  reconcilia é o Triple Check no fim de cada ciclo, onde o dispenser entra como
  **fonte que não mediu** — não como fonte que divergiu. Com limiar 1, basta
  uma das outras duas discordar para a OS travar.
* **A ausência do `posicionado` não cancela nada.** É a parte que quem mexer
  aqui vai querer "consertar", pondo um `await` de volta no meio do ciclo — e
  isso é o handshake de volta. Um evento perdido no encaminhamento (o
  `_post_central` do adapter desiste depois de 3 tentativas) chega ao central
  idêntico à mesa parada. Tratar os dois como iguais aborta OS com o hardware
  intacto, que é o custo que o modelo antigo pagava.
* **O que veta é só um `erro` EXPLÍCITO**, ou o ACK negativo do `mover`. São as
  duas únicas defesas antes do `dispensar`, e é por isso que o firmware recusa
  com ACK negativo em vez de apenas emitir evento.
* **O cronograma tem de acompanhar a máquina.** `DISPENSA_S_POR_UNIDADE` no
  central e o `(qty + 1) × 1000 ms` do `WP()` no firmware descrevem o MESMO
  mecanismo físico visto de dois lugares. Ajustar um só faz o central cortar a
  dispensa no meio: a OS termina "completa" com menos comprimido no leito, que
  é o pior desfecho possível desta frente.

---

## 5. `weight` — a balança HX711

**Firmware escrito:** `weight/balanca2_3/balanca2_3.ino`
(ESP32 + 4 × HX711), o primeiro desta célula a sair do papel. O
[`weight/README.md`](../weight/README.md) é o manual dele — tabela de eventos,
comandos aceitos pela serial, os dois comandos que o PC **não** pode mandar, e
o aviso do DTR. O que está aqui é o contrato; o que está lá é a placa.

A balança é uma **balança de bancada antes de ser um periférico da célula**:
ela conta por peso, calibra canal a canal e sempre foi configurada pelo Monitor
Serial. Daí a seção ter duas famílias de mensagem, e a linha entre elas ser o
destino: o que é da OS sobe ao central, o que é da bancada para no adapter.

### Comandos (adapter → placa)

| `cmd` | campos | efeito |
|---|---|---|
| `tara` | `os_id` | zera a balança |
| `pesar` | `os_id`, `slot_id`, `quantidade_esperada`, `quantidade_real`, `peso_unitario_g`, `injetar_falha` (opcional) | lê o peso e calcula o desvio |

**O comando leva DUAS quantidades, e confundi-las inverte o teste:**
`quantidade_esperada` é o alvo da OS (base do peso ESPERADO, e portanto do
desvio); `quantidade_real` é o que o dispenser reportou ter soltado (base do peso
que a mesa efetivamente ganha). A divergência emerge da diferença entre as duas —
é o que uma célula de carga mede na vida real. `quantidade_real` ausente cai na
esperada, preservando o contrato antigo.

**`tara` move o zero LÓGICO da mesa, e não toca no hardware.** O offset do HX711
é calibração, gravada na NVS, e só muda com o operador presente — é o
`tara_canais` da bancada. Tarar o hardware no meio de uma OS levaria junto o
peso que já estava na mesa, e a balança passaria a mentir para sempre, sem erro
em lugar nenhum.

**O `pesar` espera a mesa parar antes de medir** (até 3 s), e essa espera cabe
DEPOIS do ACK: o relógio que corre nela é o `TIMEOUT_PESO` do orquestrador
(15 s), nunca o `ack_timeout_s` (2 s). É a razão de o ACK existir separado.

### Comandos de bancada (só no serial)

Estes **não estão em `_ROTAS_SIM`** e **não existem no `weight-simulator`** —
são da PLACA, e só existem quando há placa. Uma entrada em `_ROTAS_SIM`
prometeria as duas pernas, e a perna HTTP daria 404 no transporte que é o
default. O adapter os expõe em `POST /bancada/*` e responde **503** com o
transporte `http`.

Eles existem porque, com `WEIGHT_TRANSPORTE=serial`, o adapter é o **dono** da
porta: ninguém mais abre o Monitor Serial. Sem eles, definir o peso unitário
passaria a exigir parar o adapter.

| `cmd` | campos | efeito | equivale a digitar |
|---|---|---|---|
| `peso_unitario` | `valor_g` | define o peso de uma unidade e grava na NVS | `g<valor>` |
| `tara_recipiente` | — | mede o pote vazio e o desconta da contagem | `k` |
| `tara_canais` | — | zera os offsets do HX711 (**calibração**, grava na NVS) | `t` |
| `contar` | — | espera estabilizar e conta por peso | `x` |
| `config` | — | publica a configuração gravada | `C` |
| `stream` | `on` (bool, ausente = liga) | liga/desliga o stream periódico de peso | `j` |

**O PC nunca manda `u` nem `c0`..`c3`.** Esses dois são interativos: o firmware
bloqueia em `readSerialLine()` por até 15 s esperando alguém digitar, e durante
esse tempo a placa não lê comando nem responde ACK. O equivalente não
bloqueante de `u` é `peso_unitario`; calibrar canal é operação de bancada, com
peso padrão na mão, e não tem equivalente remoto de propósito.

### Eventos (placa → adapter)

| `tipo` | campos principais |
|---|---|
| `tara_ok` | `os_id`, `peso_tara_g`, `ts` |
| `peso_ok` | `os_id`, `slot_id`, `quantidade_esperada`, `quantidade_real`, `peso_unitario_g`, `peso_esperado_g`, `peso_medido_g`, `peso_acumulado_g`, `desvio_g`, `desvio_pct`, `tolerancia_pct`, `dentro_tolerancia`, `falha_injetada`, `ts` |
| `peso_divergencia` | os mesmos campos do peso_ok acima |
| `erro_sensor` | `os_id`, `slot_id`, `descricao`, `ts` |
| `telemetria` | `componente`, `temperatura_c`, `peso_atual_g`, `ts` |
| `boot` | `fw`, `canais_ativos`, `uw_g`, `ts` |
| `peso` | `total_g`, `canais_g`, `sat`, `estavel`, `ts` |
| `contagem` | `total_g`, `liquido_g`, `exata`, `contagem`, `status`, `aceite`, `ts` |
| `cfg` | `uw_g`, `tara_g`, `tol_g`, `min`, `max`, `sreads`, `sthres_g`, `ts` |
| `estado` | `estado`, `ts` |
| `tara_balanca` | `alvo`, `valor_g`, `ts` |
| `erro_balanca` | `msg`, `cmd`, `ts` |

`peso_ok` é a maior mensagem do contrato e é ela que dimensiona
`MAX_LINHA_BYTES`.

**Os cinco primeiros sobem ao central. Os sete últimos PARAM no adapter** e
saem por `GET /balanca` (`_EVENTOS_BANCADA` em `weight-adapter/main.py`). O
central não tem endpoint para eles e não decide nada com eles; despejá-los em
`/api/v1/eventos/peso` misturaria duas conversas num histórico que hoje é só de
pesagem de OS — e como o evento atravessa sem interpretação, o central gravaria
o buraco sem erro. É a mesma divisão que a seção 6 faz com os eventos das telas.

O corolário é o que torna a lista verificável: um `tipo` fora de
`_EVENTOS_BANCADA` **é** encaminhado. Esquecer de acrescentar um evento novo
falha para o lado visível — uma linha estranha no central —, nunca para o mudo.

**`boot` é o único evento que muda o comportamento do adapter.** Ele marca a
tara como não confiável (`/balanca`, `tara_confiavel`), e o motivo é o item 7
deste documento ao contrário: abrir a porta **não** reinicia a placa, porque
DTR/RTS saem desligados antes do `open()`. Então um `boot` que chega aqui é um
boot que ninguém pediu — queda de energia, botão de reset, ou outro processo
abrindo a porta —, e o `setup()` da balança **tara sozinho** depois de 5 s: se
havia peso na mesa, a tara levou o peso junto. Não bloqueia nada; quem decide é
quem lê. Um `POST /comandos/tara` aceito devolve a confiança, porque é
exatamente o ato que o boot invalidou.

**Vocabulário fechado**, e ele é do firmware:

* `estado` ∈ `IDLE`, `AWAITING_TARA`, `AWAITING_DEPOSIT`, `TRANSIENT`,
  `COUNTING`, `DONE` (o `CountState` do sketch);
* `status` da contagem ∈ `OK`, `UNDER_TOLERANCE`, `OVER_TOLERANCE`, `UNSTABLE`,
  `INVALID_WEIGHT`, `NO_UNIT_WEIGHT` (o `CountResult`);
* `alvo` da tara ∈ `canais` (hardware) ou `recipiente` (o pote).

Nome divergente aqui não dá erro: dá uma tela que nunca sai de "desconhecido".

**`canais_g` traz `null` no canal inativo**, e não zero — zero é uma leitura, e
a bancada tem um canal desligado de propósito. Pela mesma razão todo float é
protegido contra `NaN`/`inf` na origem: emiti-los como `nan` faria o
`json.loads` do adapter descartar a linha INTEIRA, e um campo estragado levaria
junto os quinze que estavam certos.

**Só o stream é periódico**, a 5 Hz. `contagem`, `cfg`, `estado` e
`tara_balanca` saem na transição, como manda a seção 2 — e `estado` sai de
`setCountState()`, o único lugar do firmware que atribui `countState`.

---

## 6. `dispenser_tft` — as 8 telas TFT

**A segunda placa do dispenser-adapter, numa segunda porta.** Acionar os 8
mecanismos, desenhar 8 telas e manter a serial não cabe num ESP só: são DUAS
placas, em DUAS portas — `dispenser` (os mecanismos, a seção 3, sem mudança) e
`dispenser_tft` (as telas). O dono das duas é o **mesmo** `dispenser-adapter`,
com dois `LinkSerial` (`DISPENSER_TFT_TRANSPORTE`, `DISPENSER_TFT_SERIAL_URL`,
`DISPENSER_TFT_SERIAL_BAUD`, `DISPENSER_TFT_ACK_TIMEOUT_S`). Não entra serviço
novo: este adapter é um tradutor, e já vê todo comando que desce e todo evento
que sobe do slot — ou seja, já tem em mãos tudo que as telas precisam mostrar.
Um tft-adapter separado obrigaria o central a mandar a mesma informação duas
vezes, e duas cópias divergem.

Com `DISPENSER_TFT_TRANSPORTE=http` (o default) não há tela nenhuma e o
adapter se comporta exatamente como antes desta placa existir.

**Firmware escrito:** `dispenser/telas_tft/telas_tft.ino`. Ele compartilha com
a placa dos mecanismos o núcleo do protocolo (`apsen_serial.h`, cópia idêntica
nas duas pastas, com teste cobrando a igualdade) e não implementa nada além do
que esta seção descreve. O painel ainda não foi escolhido, e por isso a camada
de desenho fica atrás de quatro funções finas — ver
[`dispenser/README.md`](../dispenser/README.md).

### Comandos (adapter → placa)

| `cmd` | campos | efeito |
|---|---|---|
| `slot` | `dispenser_id`, `medicamento`, `sku`, `categoria`, `quantidade_alvo`, `quantidade_dispensada`, `quantidade_residual`, `status`, `os_id` | redesenha a tela do slot |
| `estado_celula` | `trava_ativa`, `trava_slot_id`, `os_id`, `trava_resumo` | há trava na célula, e de que slot é |

**`slot` sai na transição, e só nela:** ao aceitar `carregar`/`dispensar`/
`limpar` (estado que o adapter acabou de comandar) e ao receber `carregado`,
`dispensado`, `limpeza_ok` e `erro` da placa dos mecanismos — antes de
encaminhá-los ao central. Não há laço periódico: o canal é 115200 e a regra de
não competir com o caminho crítico é a da seção 2.

**`estado_celula` vai SÓ para esta placa.** A placa dos mecanismos não tem
tela e não precisa saber da trava — quem para a dispensa é o orquestrador, não
ela. `trava_slot_id` pode vir nulo (trava sem slot), mas a chave vem sempre.

**`trava_resumo` tem TETO DE 48 CARACTERES** e sai da CATEGORIA da divergência
("divergência de peso", "contagem divergente", "SKU errado", "dispenser
divergente"), nunca da string formatada. O motivo completo do central passa de
240 caracteres e **não viaja**: mandá-lo acoplaria o formato de mensagem do
central à largura de uma tela e criaria um segundo ponto de truncamento para
algo cosmético. O motivo completo é do display de 7" e da web, onde o
supervisor decide; a tela do slot responde uma pergunta só: *é este slot?*

**Falha da placa das telas NUNCA muda o caminho do dispenser.** Não recusa
comando, não atrasa ACK, não impede o encaminhamento do evento ao central. O
adapter loga e segue. Tela errada é cosmética; dispensa atrasada não é.

### Tabela de pintura

É o que o firmware das telas implementa, e nada além disso:

| estado recebido | a tela do slot mostra |
|---|---|
| `trava_ativa=false` | medicamento · SKU · dispensada/alvo · residual · status |
| `trava_ativa=true` e `trava_slot_id` == meu id | alerta + **AGUARDE SUPERVISOR** + `trava_resumo` |
| `trava_ativa=true` e outro slot | **PARADO — D{n}**, conteúdo esmaecido |

### Eventos (placa → adapter, dentro de `{"evento":{...}}`)

| `tipo` | campos principais |
|---|---|
| `telemetria` | `telas_ok`, `brilho_pct`, `ts` |
| `erro` | `dispenser_id`, `codigo_erro`, `descricao`, `ts` |

**Nenhum dos dois vai ao central.** Ele não tem endpoint de tela e não decide
nada com isso; despejá-los em `/api/v1/eventos/dispenser` misturaria duas
placas num histórico que hoje é de uma. Ficam no adapter: log e `/health`, que
passa a publicar o estado das DUAS portas, separadas por subsistema
(`serial` e `serial_tft`), no formato de `link.estado()`.

---

## 7. Execução: onde a porta é aberta

O código **não sabe** em qual sistema operacional está: ele chama
`serial.serial_for_url()`, que aceita `/dev/ttyUSB0`, `COM4`, `socket://h:p`,
`rfc2217://h:p` e `loop://` com a mesma API.

* **A célula montada roda num mini PC Windows.** O Docker Desktop não repassa
  porta COM para container, então os três adapters seriais rodam **fora do
  Docker**, no host, abrindo a COM direto com pyserial — como o painel de
  bancada já fazia, pelo mesmo motivo. No compose eles (e os simuladores que
  substituem) estão atrás do profile `simulado`. **Toda placa tem a COM fixada
  no Windows e a `<SUB>_SERIAL_URL` preenchida**: a varredura é só para
  desenvolvimento sem hardware, porque cada processo varrendo abre as portas
  dos outros por até 9,5 s cada e derruba o dono de verdade. O passo a passo
  está em `docs/DEPLOY_WINDOWS.md` e no README, seção "Portas seriais na célula
  montada".
* **Alvo Linux (se um dia houver).** As portas entram nos containers pelo
  `devices:` do compose (`/dev/ttyUSB0:/dev/ttyUSB0`). Nenhuma peça nova, o
  kernel garante dono único da porta, e o mapeamento fica declarado no mesmo
  arquivo onde o resto da topologia já está. O bloco está escrito e COMENTADO
  nos três serviços — e só vale para alvo Linux: ligar um device que não existe
  impede o serviço de subir, e o default é HTTP.
* **A ponte RFC2217 foi avaliada e preterida.** Ela manteria tudo em container
  expondo cada COM como socket TCP (`rfc2217://host:porta`), e o custo some do
  código — de dentro do adapter as duas montagens são a mesma chamada. Por isso
  fica escrito: mais um processo por porta para vigiar; latência a mais
  em todo comando e evento; "cabo caiu" e "ponte morreu" com o mesmo sintoma; e
  a perda da exclusividade de abertura — no Windows a COM é exclusiva de um
  processo (o segundo toma `ACCESS_DENIED`, que é o erro certo), e dois
  processos abrem o mesmo socket TCP sem erro nenhum, que é o que o item 1 do
  enquadramento existe para impedir.

### Configuração, por adapter

| variável | default | o que é |
|---|---|---|
| `<SUB>_TRANSPORTE` | `http` | `http` (simulador) ou `serial` (firmware) |
| `<SUB>_SERIAL_URL` | vazia | URL da porta; **vazia = varre e detecta pelo ping** |
| `<SUB>_SERIAL_BAUD` | `115200` | |
| `<SUB>_ACK_TIMEOUT_S` | `2` | prazo do ACK, não da conclusão |

`<SUB>` é `CNC`, `DISPENSER`, `WEIGHT` ou `DISPENSER_TFT` — as duas últimas no
mesmo `dispenser-adapter`, uma por porta. Para a balança, que é a que já tem
placa, isso quer dizer `WEIGHT_TRANSPORTE=serial` e `WEIGHT_SERIAL_URL=COM<n>`
com a COM fixada no Windows (`weight/README.md`). Com `http`, o comportamento é
EXATAMENTE o de antes desta feature — é o default justamente para que a suíte, o
CI e a demonstração em Docker não mudem de resultado (para as telas, `http`
significa "sem telas").

## 8. Sem placa: as placas falsas

`tests/fakes/placa_dispenser.py`, `placa_dispenser_tft.py`, `placa_cnc.py` e
`placa_weight.py` falam este contrato inteiro por `socket://`: respondem ping,
aceitam comando, devolvem ACK, ignoram `cmd_id` repetido e emitem os eventos
com atraso configurável (inclusive zero). Elas são o que permite exercitar o transporte sem hardware — e são também
o documento executável contra o qual o firmware foi escrito. É o mesmo papel
que `painel_operador/firmware/simulador_serial.py` já cumpre para o display.

Para os três subsistemas que já têm placa, a comparação fechou o círculo:
`tests/test_balanca_serial.py` e `tests/test_dispenser_firmware.py` confrontam
o FIRMWARE com a placa falsa e com este documento, campo por campo. Os dois
leem C++ por texto — não há AST para ele —, e por isso cada extrator tem um
piso de quantos símbolos precisa achar: um extrator que pare de casar deixaria
tudo verde para sempre, inclusive com o protocolo quebrado.
