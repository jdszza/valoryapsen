# Balança 4 pontos — ESP32 + 4 × HX711

A célula de carga da mesa de coleta. Ela tem **dois papéis**, e quase tudo neste
diretório se explica por essa divisão:

* **balança de bancada** — conta comprimidos por peso, calibra canal a canal,
  guarda a configuração na NVS. Sempre foi operada pelo Monitor Serial;
* **periférico da célula APSEN** — atende `tara` e `pesar` do orquestrador e
  devolve `peso_ok`/`peso_divergencia`, que são a **terceira fonte do Triple
  Check** (ver o README da raiz).

As duas conversas cabem na mesma porta USB, e a `2.3` é exatamente o que as fez
caber: uma segunda **voz** na serial, não um segundo firmware.

| arquivo | o que é |
|---|---|
| `balanca2.2.ino` | a versão só-humano, **intacta**. É o que se grava quando a 2.3 está sob suspeita. |
| `balanca2_3/balanca2_3.ino` | a 2.2 + a saída legível por máquina. É esta que vai para a bancada. |

> A Arduino IDE exige que o sketch esteja numa pasta com o mesmo nome — daí
> `balanca2_3/balanca2_3.ino`. A 2.2 fica solta de propósito: ela não é para
> abrir, é para comparar.

Compilar:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 weight/balanca2_3
```

> **Primeira vez com a placa na mão?** O roteiro do zero — gravar, calibrar,
> provar o transporte sem Docker e só então ligar na planta — está em
> [PRIMEIRO_ENSAIO.md](PRIMEIRO_ENSAIO.md). Este README é a referência; aquele é o
> passo a passo.

## As duas vozes, e o `{` que as separa

Uma mensagem por linha. **Toda linha de máquina tem um `{`**; toda linha sem `{`
é log humano. O adapter extrai o JSON a partir do primeiro `{` e ignora o resto
— então log e JSON podem sair **grudados** na mesma linha, que é o que o
firmware realmente faz no boot:

```
(Timeout) Tara automatica...{"evento":{"tipo":"boot","fw":"2.3",...}}
```

Nada do que a 2.2 imprimia foi removido, e nenhum comando de uma letra mudou: o
Monitor Serial da bancada funciona exatamente como antes. Quem lê o que importa
para a célula é o `weight-adapter`; quem lê o resto é o operador.

## Eventos (placa → PC)

Todos dentro de `{"evento":{...}}`, todos com `tipo` e `ts`. O contrato completo
está em [`../docs/PROTOCOLO_SERIAL.md`](../docs/PROTOCOLO_SERIAL.md) §5.

### Da OS — o adapter encaminha ao central

| `tipo` | quando | campos |
|---|---|---|
| `tara_ok` | fim do comando `tara` | `os_id`, `peso_tara_g` |
| `peso_ok` | fim do `pesar`, dentro da tolerância | `os_id`, `slot_id`, `quantidade_esperada`, `quantidade_real`, `peso_unitario_g`, `peso_esperado_g`, `peso_medido_g`, `peso_acumulado_g`, `desvio_g`, `desvio_pct`, `tolerancia_pct`, `dentro_tolerancia`, `falha_injetada` |
| `peso_divergencia` | idem, fora da tolerância | os mesmos |
| `erro_sensor` | canal saturado ou leitura inválida durante o `pesar` | `os_id`, `slot_id`, `descricao` |
| `telemetria` | a cada 15 s | `componente`, `temperatura_c`, `peso_atual_g` |

### Da bancada — param no adapter, saem por `GET /balanca`

| `tipo` | quando | campos |
|---|---|---|
| `boot` | fim do `setup()` | `fw`, `canais_ativos`, `uw_g` |
| `peso` | stream, 5 Hz | `total_g`, `canais_g`, `sat`, `estavel` |
| `contagem` | resultado do estado `COUNTING`, e no comando `n` | `total_g`, `liquido_g`, `exata`, `contagem`, `status`, `aceite` |
| `cfg` | depois de todo `saveCountConfig()`, e nos comandos `C` e `j` | `uw_g`, `tara_g`, `tol_g`, `min`, `max`, `sreads`, `sthres_g` |
| `estado` | toda troca de `countState` | `estado` |
| `tara_balanca` | fim de `tareAll()` e do estado `AWAITING_TARA` | `alvo` (`canais`/`recipiente`), `valor_g` |
| `erro_balanca` | comando desconhecido, valor inválido, canal saturado | `msg`, `cmd` |

O central não tem endpoint para os sete de baixo e não decide nada com eles.
Encaminhá-los não daria erro — o evento atravessa o adapter sem interpretação —,
daria linhas estranhas num histórico que hoje é só de pesagem de OS.

Detalhes que valem por si:

* **`canais_g` traz `null` no canal inativo**, e não zero: zero é uma leitura,
  e o canal 3 desta bancada está desligado de propósito.
* **`NaN`/`inf` viram `null`** na origem. Emiti-los como `nan` faria o
  `json.loads` do adapter descartar a linha **inteira**, e um campo estragado
  levaria junto os quinze que estavam certos.
* **Saturação vira evento só na TRANSIÇÃO** não-saturado → saturado. O aviso
  humano continua saindo a cada leitura, como na 2.2; um evento por leitura
  encheria a linha de 115200 baud que os ACKs do adapter estão esperando.
* **Vocabulário fechado**: `estado` é o `CountState` do sketch e `status` é o
  `CountResult`. Nome divergente não dá erro — dá uma tela que nunca sai de
  "desconhecido".
* **Só o stream é periódico.** `contagem`, `cfg`, `estado` e `tara_balanca`
  saem na transição, e `estado` sai de `setCountState()`, o único lugar do
  firmware que atribui `countState`.
* **`ts` sai do `epoch` que vem no pong.** A placa não tem RTC nem NTP: antes
  do primeiro pong o carimbo sai em 1970 — visivelmente errado, que é melhor
  que plausível e errado.

## Comandos

### Do PC, em JSON (com ACK e `cmd_id`)

Da OS:

```
{"cmd":"tara","cmd_id":7,"os_id":"..."}
{"cmd":"pesar","cmd_id":8,"os_id":"...","slot_id":3,"quantidade_esperada":10,
 "quantidade_real":10,"peso_unitario_g":50.0}
```

Da bancada — `peso_unitario` (`valor_g`), `tara_recipiente`, `tara_canais`,
`contar`, `config`, `stream` (`on`). O adapter os expõe em `POST /bancada/*`.

O ACK (`{"resp":"ok","cmd_id":n}`) diz que a placa **aceitou**, não que
terminou: o `pesar` espera a mesa estabilizar por até 3 s e só então mede. O
resultado chega depois, como evento.

### Do humano, no Monitor Serial

Todos os da 2.2, sem mudança, mais um:

| tecla | efeito |
|---|---|
| `t` `p` `m` `a` `s` `l` | tara dos canais, imprimir canais, mapa, auto-print, salvar/carregar NVS |
| `c0`..`c3` / `+0`..`+3` / `-0`..`-3` | calibrar / ativar / desativar canal |
| `u` / `g<valor>` | peso unitário, interativo / direto |
| `k` `x` `n` `C` `o<v>` `i<min>-<max>` `r<v>` `h<v>` | tara do recipiente, contar, último resultado, config, tolerância, faixa, leituras de estabilização, limiar |
| **`j`** | **liga/desliga o stream `peso`** (novo na 2.3, ligado por padrão) |
| `T` `?` | testes T1..T13, ajuda |

### O PC NUNCA manda `u` nem `c0`..`c3`

Os dois são **interativos**: o firmware bloqueia em `readSerialLine()` por até
**15 s** esperando alguém digitar, e nesse tempo a placa não lê comando nem
responde ACK. Um `u` mandado pelo adapter travaria a balança por 15 s no meio de
uma OS, e o sintoma seria `timeout_peso` num slot íntegro.

O equivalente não bloqueante de `u` é o comando `peso_unitario` (ou a letra
`g<valor>`). Calibrar canal **não tem** equivalente remoto, de propósito: é
operação de bancada, com peso padrão na mão.

## O reset ao abrir a porta — e por que a tara pode estar errada

No Windows, abrir uma porta serial normalmente pulsa **DTR/RTS**, e nesses dois
sinais está o circuito de reset/boot do ESP32. O `serial_link.py` dos adapters
**desliga os dois antes do `open()`** justamente por isso — sondar um
dispositivo não pode reiniciá-lo, e já aconteceu neste projeto com o display do
painel de bancada.

A consequência prática está do outro lado: como o adapter **não** reinicia a
placa, todo `boot` que chega até ele é um boot que **ninguém pediu** — queda de
energia, botão de reset, ou outro processo abrindo a porta. E o `setup()` da
balança faz **tara automática** depois de 5 s sem resposta:

> se havia peso na mesa naquele instante, a tara levou o peso junto.

Por isso o adapter, ao ver um `boot`, loga um aviso e marca
`tara_confiavel: false` em `GET /balanca`. **Não bloqueia nada** — a tara pode
estar certíssima (mesa vazia no reset), e recusar pesagem por causa disso
trocaria um número possivelmente errado por uma planta parada. Um
`POST /comandos/tara` aceito devolve a confiança, porque é exatamente o ato que
o boot invalidou.

O comportamento de tara no boot **não mudou** na 2.3: o operador de bancada
depende dele, e mudá-lo para agradar ao adapter quebraria quem usa a balança
sozinha.

Se outro programa (Monitor Serial, um `screen`, um script) abrir a porta
enquanto o adapter a tem, no Windows ele toma `ACCESS_DENIED` — a COM é
exclusiva de um processo, e é por isso que a ponte RFC2217 foi preterida
(`docs/DEPLOY_WINDOWS.md`).

## Duas taras, e confundi-las estraga a balança

| o quê | quem manda | o que faz |
|---|---|---|
| `tara` (comando da OS) | orquestrador | move o **zero lógico** da mesa. Não toca no hardware. |
| `tara_canais` / letra `t` | operador | zera os **offsets do HX711** e grava na NVS. É calibração. |
| `tara_recipiente` / letra `k` | operador | mede o pote vazio e o desconta da contagem. |

Tarar o **hardware** no meio de uma OS, com peso em cima, faria a balança mentir
para sempre — e a mentira ficaria gravada na NVS. É a razão de o comando da OS
ser o zero lógico e nada mais.

## Ligar na célula

```
WEIGHT_TRANSPORTE=serial
WEIGHT_SERIAL_URL=COM7        # a COM fixada desta placa, no Gerenciador de Dispositivos
WEIGHT_SERIAL_BAUD=115200
WEIGHT_ACK_TIMEOUT_S=2
```

**Esse bloco já está escrito, e o lugar dele é
[`../weight-adapter/iniciar_host.bat`](../weight-adapter/iniciar_host.bat).**
Você edita **uma linha** — a COM, no bloco do topo — e roda o `.bat`; ele liga
o transporte serial, aponta o central e sobe o adapter na porta 8103. Não vai
no `.env` da raiz: na célula montada este processo roda fora do Docker, e
ninguém lê o `.env` dele. É a mesma regra dos outros três processos com placa
— [Onde escrever o número da COM](../README.md#onde-escrever-o-número-da-com).

`WEIGHT_TRANSPORTE=http` (o **default**) mantém o `weight-simulator` e o
comportamento anterior a este firmware, byte a byte — é o que faz a suíte, o CI
e a demonstração em Docker não mudarem de resultado. Nesse modo `GET /balanca`
vem todo nulo e `POST /bancada/*` responde **503**: não há balança de bancada
atrás do simulador.

**A COM tem que ser fixada e a variável preenchida.** Com a URL vazia o adapter
varre todas as portas, abrindo as dos outros processos por até 9,5 s cada — com
cinco placas na célula isso é boot não-determinístico, em que uma placa às vezes
não é achada. O passo a passo está em
[`../docs/DEPLOY_WINDOWS.md`](../docs/DEPLOY_WINDOWS.md).

O USB é **só dados**: a alimentação da balança é externa.

## Sem placa

`tests/fakes/placa_weight.py` fala este contrato inteiro por `socket://` — um
handler de URL do próprio pyserial, então o caminho de abertura exercitado é o
de verdade. Ela é o duplo do firmware e o documento executável contra o qual ele
foi escrito; `tests/test_balanca_serial.py` confronta os dois.

```bash
pytest tests/test_balanca_serial.py tests/test_protocolo_placas.py
```
