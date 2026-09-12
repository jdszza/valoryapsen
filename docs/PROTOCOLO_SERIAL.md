# Protocolo serial APSEN — adapters ↔ firmware

Este arquivo é o CONTRATO que os três firmwares vão implementar, e é contra ele
que `tests/test_protocolo_placas.py` compara o código Python dos dois lados (os
adapters e as placas falsas de `tests/fakes/`). Firmware que discorde daqui não
falha com erro: ele fica mudo, e o sintoma chega como OS abortada por timeout
num slot que o operador vai procurar na bancada.

Uma porta por firmware, uma linha JSON por mensagem:

| adapter             | firmware                    | porta | subsistema (`sub`) |
|---------------------|-----------------------------|-------|--------------------|
| `dispenser-adapter` | os 8 dispensers             | uma   | `dispenser`        |
| `cnc-adapter`       | a mesa CNC                  | uma   | `cnc`              |
| `weight-adapter`    | a balança HX711             | uma   | `weight`           |

O `vision-adapter` **não** entra: a visão continua por HTTP.

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

## 6. Execução: onde a porta é aberta

O código **não sabe** em qual sistema operacional está: ele chama
`serial.serial_for_url()`, que aceita `/dev/ttyUSB0`, `COM4`, `socket://h:p`,
`rfc2217://h:p` e `loop://` com a mesma API.

* **Alvo — mini PC com Linux.** As portas entram nos containers pelo `devices:`
  do compose (`/dev/ttyUSB0:/dev/ttyUSB0`). Nenhuma peça nova, o kernel garante
  dono único da porta, e o mapeamento fica declarado no mesmo arquivo onde o
  resto da topologia já está. O bloco está escrito e COMENTADO nos três serviços:
  ligar um device que não existe impede o serviço de subir, e o default é HTTP.
* **Plano B — Windows.** O Docker Desktop não repassa porta COM para container.
  Uma ponte RFC2217 no host expõe cada porta como socket TCP e o adapter abre
  `rfc2217://host:porta`. O custo está registrado no CLAUDE.md e some do código:
  latência a mais, "cabo caiu" e "ponte morreu" com o mesmo sintoma, e a perda da
  exclusividade de abertura que o kernel dá — dois processos abrem o mesmo
  socket sem erro nenhum.

### Configuração, por adapter

| variável | default | o que é |
|---|---|---|
| `<SUB>_TRANSPORTE` | `http` | `http` (simulador) ou `serial` (firmware) |
| `<SUB>_SERIAL_URL` | vazia | URL da porta; **vazia = varre e detecta pelo ping** |
| `<SUB>_SERIAL_BAUD` | `115200` | |
| `<SUB>_ACK_TIMEOUT_S` | `2` | prazo do ACK, não da conclusão |

`<SUB>` é `CNC`, `DISPENSER` ou `WEIGHT`. Com `http`, o comportamento é
EXATAMENTE o de antes desta feature — é o default justamente para que a suíte, o
CI e a demonstração em Docker não mudem de resultado.

## 7. Sem placa: as placas falsas

`tests/fakes/placa_dispenser.py`, `placa_cnc.py` e `placa_weight.py` falam este
contrato inteiro por `socket://`: respondem ping, aceitam comando, devolvem ACK,
ignoram `cmd_id` repetido e emitem os eventos com atraso configurável (inclusive
zero). Elas são o que permite exercitar o transporte sem hardware — e são também
o documento executável contra o qual o firmware vai ser escrito. É o mesmo papel
que `painel_operador/firmware/simulador_serial.py` já cumpre para o display.
