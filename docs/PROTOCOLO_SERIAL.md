# Protocolo serial APSEN — adapters ↔ firmware

Este arquivo é o CONTRATO que os três firmwares vão implementar, e é contra ele
que `tests/test_protocolo_placas.py` compara o código Python dos dois lados (os
adapters e as placas falsas de `tests/fakes/`). Firmware que discorde daqui não
falha com erro: ele fica mudo, e o sintoma chega como OS abortada por timeout
num slot que o operador vai procurar na bancada.

Uma porta por firmware, uma linha JSON por mensagem:

| adapter             | firmware                    | porta | subsistema (`sub`) |
|---------------------|-----------------------------|-------|--------------------|
| `dispenser-adapter` | os 8 dispensers (mecanismos)| uma   | `dispenser`        |
| `dispenser-adapter` | as 8 telas TFT              | uma (a SEGUNDA porta do mesmo adapter) | `dispenser_tft` |
| `cnc-adapter`       | a mesa CNC                  | uma   | `cnc`              |
| `weight-adapter`    | a balança HX711             | uma   | `weight`           |

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
| adapter → placa | `{"resp":"pong","epoch":<unix>}` | resposta ao ping |
| adapter → placa | `{"cmd":"<nome>","cmd_id":<n>,...campos}` | comando |
| placa → adapter | `{"resp":"ok","cmd_id":<n>}` | ACK positivo |
| placa → adapter | `{"resp":"erro","cmd_id":<n>,"msg":"..."}` | ACK negativo |
| placa → adapter | `{"evento":{...payload...}}` | resultado e telemetria |

**Quem inicia o ping é sempre a placa.** É assim que a porta é identificada: com
`<SUB>_SERIAL_URL` vazia, o adapter varre as portas e aceita a primeira que
emitir o ping DAQUELE `sub`. A detecção **não** usa VID/PID — o VID/PID de um
conversor USB-serial é o mesmo em placas de fabricantes diferentes, e casar por
ele acha a placa errada, que aqui significa mandar `dispensar` para a balança.

O `epoch` do pong é como a placa acerta o relógio sem NTP.

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

### Comandos (adapter → placa)

| `cmd` | campos | efeito |
|---|---|---|
| `mover` | `dispenser_alvo`, `os_id`, `posicao_x`, `posicao_y`, `ciclo_atual`, `total_ciclos` | move a mesa até o slot |
| `homing` | `os_id`, `posicao_x` (opcional), `posicao_y` (opcional) | volta ao HOME |

**As coordenadas vêm no comando.** A geometria da célula tem UM dono, e é o
central (ver CLAUDE.md, "A cópia do mapa no cnc_simulator não existe mais"). O
firmware valida a faixa (`1 <= dispenser_alvo <= NUM_SLOTS`) e se move para o par
de coordenadas que recebeu — nunca para uma tabela própria. Uma cópia do mapa no
firmware não daria erro: a mesa iria para onde a cópia DELA diz que D7 fica, e o
sintoma seria divergência num slot só, indistinguível de falha mecânica.

### Eventos (placa → adapter)

| `tipo` | campos principais |
|---|---|
| `posicionado` | `os_id`, `dispenser_alvo`, `posicao_x`, `posicao_y`, `ciclo_atual`, `total_ciclos`, `ts` |
| `concluido` | `os_id`, `posicao_x`, `posicao_y`, `ts` |
| `erro` | `os_id`, `dispenser_alvo`, `codigo_erro`, `descricao`, `ts` |
| `movendo` | `os_id`, `dispenser_alvo`, `posicao_x`, `posicao_y`, `passo`, `total_passos`, `progresso_pct`, `ts` |
| `retornando` | `os_id`, `posicao_x`, `posicao_y`, `ts` |
| `telemetria` | `componente`, `tipo_leitura`, `valor`, `unidade`, `ts` |

`movendo` e `retornando` são a trajetória ao vivo: periódicos, e por isso
sujeitos ao filtro de repetição. Só `posicionado`, `concluido` e `erro` viram
linha em `cnc_eventos` no central (CLAUDE.md, "Só transição vira linha").

---

## 5. `weight` — a balança HX711

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

### Eventos (placa → adapter)

| `tipo` | campos principais |
|---|---|
| `tara_ok` | `os_id`, `peso_tara_g`, `ts` |
| `peso_ok` | `os_id`, `slot_id`, `quantidade_esperada`, `quantidade_real`, `peso_unitario_g`, `peso_esperado_g`, `peso_medido_g`, `peso_acumulado_g`, `desvio_g`, `desvio_pct`, `tolerancia_pct`, `dentro_tolerancia`, `falha_injetada`, `ts` |
| `peso_divergencia` | os mesmos campos do peso_ok acima |
| `erro_sensor` | `os_id`, `slot_id`, `descricao`, `ts` |
| `telemetria` | `componente`, `temperatura_c`, `peso_atual_g`, `ts` |

`peso_ok` é a maior mensagem do contrato e é ela que dimensiona
`MAX_LINHA_BYTES`.

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
mesmo `dispenser-adapter`, uma por porta. Com `http`, o comportamento é
EXATAMENTE o de antes desta feature — é o default justamente para que a suíte, o
CI e a demonstração em Docker não mudem de resultado (para as telas, `http`
significa "sem telas").

## 8. Sem placa: as placas falsas

`tests/fakes/placa_dispenser.py`, `placa_dispenser_tft.py`, `placa_cnc.py` e
`placa_weight.py` falam este contrato inteiro por `socket://`: respondem ping,
aceitam comando, devolvem ACK, ignoram `cmd_id` repetido e emitem os eventos
com atraso configurável (inclusive zero). Elas são o que permite exercitar o transporte sem hardware — e são também
o documento executável contra o qual o firmware vai ser escrito. É o mesmo papel
que `painel_operador/firmware/simulador_serial.py` já cumpre para o display.
