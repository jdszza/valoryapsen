# Primeiro ensaio com os dispensers de verdade

Roteiro de uma vez só: das duas placas na mão até um `dispensar` dentro de uma
OS. São **quatro ensaios**, e a ordem importa — cada um só faz sentido depois
que o anterior passou, porque cada um elimina uma classe de causa.

| # | o que liga | o que prova | precisa de Docker? |
|---|---|---|---|
| 1 | placa dos mecanismos + Monitor Serial | o firmware fala as duas vozes | não |
| 2 | mesma placa, terminal `MANUT` | os 8 servos estão calibrados e os 8 IR contam | não |
| 3 | as duas placas + `dispenser-adapter` no host | o transporte funciona e o adapter acha as duas portas | não |
| 4 | a stack + as duas placas | o central manda `carregar`/`dispensar` e o evento volta | sim |

Se o ensaio 4 falhar e você pulou o 3, não dá para saber se o problema é a
placa, o cabo, o adapter, o central ou o orquestrador. É para isso que eles
existem separados.

> **São DUAS placas em DUAS portas**, e as duas são do mesmo adapter. A das
> telas pode ficar de fora até o ensaio 3 — falha de tela nunca muda o caminho
> do dispenser, e o `DISPENSER_TFT_TRANSPORTE=http` desliga as telas sem
> desligar mais nada.

---

## Antes de começar

- [ ] ESP32 dos mecanismos, com o PCA9685, os 8 servos e os 8 TCRT5000 ligados,
      e **alimentação externa para os servos** (o USB é só dados — oito servos
      sob carga não saem da porta USB);
- [ ] ESP32 das telas, se os TFTs já estiverem escolhidos e ligados. Se não,
      pule: o firmware compila e roda sem painel nenhum;
- [ ] dois cabos USB de **dados** — cabo só de carga não enumera porta nenhuma,
      e o sintoma é "a COM não aparece";
- [ ] **um multímetro**, para o item 1.1 (os pinos do I²C do PCA9685 estão
      marcados A CONFIRMAR NA BANCADA, e quem decide é ele);
- [ ] **10 a 15 caixas de medicamento iguais**, das que vão para os dispensers
      — elas são a unidade que este sistema conta;
- [ ] Arduino IDE 2.x com o core `esp32:esp32` (já está nesta máquina).

> **Os comandos deste documento são PowerShell**, que é o terminal do mini PC.
> Três coisas mudam em relação ao bash, e cada uma falha de um jeito diferente:
>
> * `source` não existe — é builtin de shell POSIX;
> * `VAR=valor comando` não é sintaxe válida; variável de ambiente se define em
>   linha própria, com `$env:VAR = "valor"`;
> * **`curl` é alias de `Invoke-WebRequest`** no PowerShell 5.1, e pior: o
>   escape `\"` para executável nativo é quebrado nessa versão, então um corpo
>   JSON chega mutilado ao curl.exe e o FastAPI responde 422 com o comando
>   visivelmente certo na tela. Por isso as chamadas HTTP aqui usam
>   `Invoke-RestMethod`.

O `arduino-cli` não está no PATH, mas a Arduino IDE traz o dela. Defina uma vez
por janela:

```bash
$ACLI = "$env:LOCALAPPDATA\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
```

A biblioteca do PCA9685 é a única dependência, e ela se instala uma vez:

```bash
& $ACLI lib install "Adafruit PWM Servo Driver Library"
```

---

## Ensaio 1 — a placa dos mecanismos sozinha

### 1.1 Conferir os pinos do I²C ANTES de gravar

O esquemático da REV 1.0 mostra **D21/D22 como NC** e o código diz
**GPIO23 = SDA, GPIO22 = SCL**. Um dos dois está errado, e o multímetro decide:
com a placa **desligada**, ponha o multímetro em continuidade entre o pino SDA
do PCA9685 e o GPIO23 do ESP32, e entre SCL e o GPIO22.

- **Bateu?** Siga.
- **Não bateu?** Descubra em quais GPIO eles caem e troque `PCA_SDA` e
  `PCA_SCL` no topo do `servos_hub.ino`. Eles estão **num lugar só**, e as
  mensagens do boot imprimem esses valores com `%d` — não há par de números
  escrito à mão em mensagem nenhuma. Mensagem de erro que aponta para o pino
  errado manda quem lê procurar no lugar errado, e é isso que este passo evita.

### 1.2 Descobrir e fixar as COM

Com a placa ligada, veja o que apareceu:

```bash
& $ACLI board list
```

Anote o número e **fixe-o no Windows** antes de seguir: Gerenciador de
Dispositivos → *Portas (COM e LPT)* → botão direito → *Propriedades* →
*Configurações de Porta* → *Avançado…* → *Número da Porta COM*. Repita para a
placa das telas, se ela existir.

Fixar não é preciosismo. Sem a COM fixada e a variável preenchida, o adapter
varre todas as portas gastando até 9,5 s em cada candidata — e com as cinco
placas da célula isso vira boot não-determinístico em que uma placa às vezes
não é achada. O porquê está em
[`../docs/DEPLOY_WINDOWS.md`](../docs/DEPLOY_WINDOWS.md).

### 1.3 Gravar

```bash
& $ACLI compile --fqbn esp32:esp32:esp32 dispenser/servos_hub
```

```bash
& $ACLI upload -p COM5 --fqbn esp32:esp32:esp32 dispenser/servos_hub
```

Troque `COM5` pela sua em todos os comandos daqui para a frente.

### 1.4 Abrir o Monitor Serial e ver as DUAS vozes

Abra o monitor a **115200** e aperte o reset da placa. O que deve aparecer,
misturado:

```
=== ESP32 INICIOU - Servos Hub (calibracao) ===
[00:00:01.012] BOOT  Servos Hub REV 1.0 - calibracao de servos
[00:00:01.015] BOOT  PCA9685 OK em 0x40 (SDA=23 SCL=22)
[00:00:01.020] BOOT  sensores IR-1..8 nos GPIO 13,12,14,27,26,25,33,32
[00:00:01.030] LOAD  servo 0: min=0 max=180 (BACKUP do codigo)
[00:00:01.034] AVISO servo 0 nao foi movido no boot: min=0 vem do BACKUP, nao da calibracao (abra MANUT e calibre com SEL 0)
...
[00:00:02.500] BOOT  pronto. Terminal FECHADO - digite MANUT para calibrar
[00:00:02.510] STAT  S0:? S1:? ... | IR ........ | PCA OK
{"cmd":"ping","sub":"dispenser"}
```

Confira cinco coisas:

1. **`PCA9685 OK`** — se disser `NAO responde`, volte ao item 1.1. A mensagem
   de erro imprime os dois GPIO que ela realmente usou;
2. **as linhas humanas continuam todas lá**, inclusive o `STAT` periódico — se
   sumiram, você gravou outra coisa;
3. **o `ping` sai sozinho, com `"sub":"dispenser"`** — é por ele que o adapter
   vai identificar esta porta, e é a placa que sempre inicia;
4. **os avisos de "não foi movido no boot"** — estão certos numa placa nova.
   Servo sem calibração própria carrega o `min` do `CAL_BACKUP`, que é 0°, e 0°
   pode estar **além** do fim de curso mecânico. Este é o único movimento do
   boot capaz de forçar a mecânica, e ele cairia justamente na placa que
   ninguém conferiu ainda. Depois do ensaio 2 eles somem;
5. **nenhum `IR-9`** na tabela do `SHOW` — são oito sensores e oito servos, e
   um `IR-9` fantasma seria leitura fora do array.

Digite `MANUT` e depois `SHOW`: a tabela de calibração sai como sempre, com oito
linhas.

> **Um teste que vale 30 segundos**: com o terminal FECHADO, cole
> `{"cmd":"carregar","cmd_id":1,"dispenser_id":1,"medicamento":"Teste","sku":"X","categoria":"Y","quantidade":3,"os_id":"ENSAIO"}`
> no monitor e aperte Enter. Tem que sair um `{"resp":"ok","cmd_id":1}` e um
> evento `carregado` — **sem** o "Terminal fechado. Digite MANUT para abrir.".
> Se aparecer essa frase, a linha de máquina caiu no caminho do humano, e é o
> bug que a separação existe para evitar.
>
> **O Monitor precisa estar com "Nova linha"** para este teste. Com "Nenhum
> final de linha" a linha JSON não é executada, e isso é de propósito: o
> "executar depois de 250 ms sem caractere" vale só para o humano. Mensagem de
> máquina sempre termina em `
`, e executar por tempo um JSON que chegou em
> pedaços seria executar metade de um `dispensar` — e emitir um ACK que
> ninguém devia ter recebido.

### 1.5 Fechar o monitor

**Feche o Monitor Serial antes do ensaio 3.** No Windows a COM é exclusiva de
um processo: com o monitor aberto, o adapter toma `ACCESS_DENIED` e fica em
laço de reconexão. O sintoma é `"conectado": false` no `/health` sem nenhum
erro óbvio.

---

## Ensaio 2 — calibrar os 8 servos e medir o prazo por unidade

Este ensaio é todo no terminal `MANUT`, e é o que transforma a placa em
dispenser. Nada dele mudou com a voz de máquina.

### 2.1 O fluxo, servo a servo

```
MANUT          abre o terminal (o STAT periodico para)
SEL 0          seleciona o servo 0 e vai para o meio da faixa
MOVE 20        move para 20 graus  (ou +, -, +5, -5 para ajuste fino)
MIN            grava a posicao ATUAL como min
MOVE 150
MAX            grava a posicao ATUAL como max
TEST           varre min<->max e diz quantas vezes o IR-1 detectou
```

Repita com `SEL 1` .. `SEL 7`. Depois:

```
SHOW           a tabela dos oito, com a origem de cada um (ESP32 ou BACKUP)
TESTALL        varre todos, um por vez
```

**Os oito têm que aparecer como `ESP32` na coluna `origem`.** `BACKUP` quer
dizer que aquele servo não foi calibrado e continua com 0..180 de fábrica.

### 2.2 `EXPORT` — o passo que se esquece

```
EXPORT
```

Ele imprime a tabela pronta para colar por cima do `CAL_BACKUP`, no topo do
`servos_hub.ino`. **Cole e commite.**

Por que isso importa: a calibração vive na NVS do ESP32, e a NVS morre com uma
placa nova, uma flash apagada ou um `RESETALL`. Sem o `EXPORT` colado, a placa
volta ao 0..180 de fábrica e o primeiro movimento pode bater no fim de curso.
Com ele colado, o pior caso é uma calibração um pouco velha.

### 2.3 Medir o prazo por unidade — o número que o firmware precisa

Este é o passo que só existe aqui, e o número que sai dele vai para o código.

Ponha caixas no cartucho do slot 1 e, com o terminal aberto:

```
IRLOG          liga o log de deteccao dos sensores durante o MANUT
SEL 0
GOMAX
GOMIN
```

No log, compare o horário do `MOVE ... -> GOMAX` com o do `IR-1 ... DETECTOU`:

```
[00:04:12.310] MOVE  servo 0 -> MAX = 150 graus
[00:04:12.690] IR    IR-1 (GPIO13) DETECTOU  total=1
```

A diferença é o **tempo de queda** daquele medicamento. Repita **dez vezes** e
anote o **maior** valor — não a média: o prazo existe para não acusar falha
mecânica onde não houve, então ele tem que cobrir o pior caso normal.

Depois multiplique por **3** e arredonde para cima. Esse é o seu
`DISPENSA_TIMEOUT_UNIDADE_MS`.

| medida | exemplo |
|---|---|
| maior tempo de queda em 10 tentativas | 380 ms |
| × 3 (margem) | 1140 ms |
| arredondado | **1200 ms** |

A margem de 3× não é superstição: ela cobre a caixa que enrosca e sai, a
variação entre slots e o desgaste do servo ao longo de um turno. O outro lado
da conta é o `TIMEOUT_DISPENSA` do orquestrador, que corre enquanto a placa
espera — um prazo muito folgado gasta o relógio do outro lado e a OS aborta
apontando para o slot certo pelo motivo errado.

Meça também quanto o braço leva de um extremo ao outro **com carga** (o `TEST`
mostra) e ajuste `DISPENSA_SERVO_MS`. O default é 250 ms.

Os dois ficam no topo do `servos_hub.ino`:

```cpp
#define DISPENSA_SERVO_MS            250
#define DISPENSA_TIMEOUT_UNIDADE_MS 1200
```

Regrave depois de mudar (item 1.3).

### 2.4 Conferir que cada IR é do seu slot

```
SENS
```

Passe a mão na frente de **um** sensor e repita o `SENS`: só o contador daquele
tem que subir. Um par trocado aqui faz o firmware contar o pulso do vizinho
como se fosse o do slot que está dispensando, e o sintoma chega como
divergência num slot íntegro.

---

## Ensaio 3 — as duas placas + adapter, sem Docker

É o ensaio que prova o transporte, e o único que dá para ler inteiro sem subir
mais nada. Duas janelas de terminal.

### 3.1 Subir o adapter (janela 1)

Uma vez só, para criar o ambiente:

```bash
cd dispenser-adapter; python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Chamar o `python.exe` do venv direto, em vez de ativar, não é atalho: o
`Activate.ps1` esbarra na ExecutionPolicy do Windows em máquina recém-instalada,
e o erro que aparece não fala em política nenhuma.

Daí em diante, sempre:

```bash
.\iniciar_host.bat
```

**As duas COM estão nas duas primeiras linhas editáveis do `.bat`** — é lá que
se troca, não aqui. Ele define os dois transportes, os dois bauds, os dois
prazos de ACK e o `CENTRAL_URL`.

> **Sem a placa das telas?** Edite `DISPENSER_TFT_TRANSPORTE=http` no `.bat`.
> Isso significa "sem telas", não erro: o `slot` não vai a lugar nenhum e nada
> mais muda.

No log tem que aparecer, em segundos:

```
[STARTUP] httpx.AsyncClient criado | transporte=serial
[dispenser] porta tomada: COM5 @ 115200 baud (dono único deste processo)
[dispenser_tft] porta tomada: COM6 @ 115200 baud (dono único deste processo)
```

O central não está de pé, e **isso é esperado**: o adapter loga a falha de
encaminhamento e segue. Ele não depende do central para abrir as portas.

### 3.2 Conferir as DUAS portas (janela 2)

```bash
Invoke-RestMethod http://localhost:8100/health | ConvertTo-Json -Depth 5
```

O `/health` publica as duas, separadas por subsistema: `serial` (mecanismos) e
`serial_tft` (telas). O que interessa em cada uma: `"conectado": true`,
`url_aberta` com a COM certa, e `ultimo_ping_placa` com hora recente.

Se `conectado` for `false`, na esmagadora maioria das vezes é o Monitor Serial
ainda aberto (item 1.5).

> **`/ping` não olha para a placa.** Ele diz que este processo está de pé, e é
> isso que o compose usa como portão de subida — atrelá-lo ao hardware faria um
> cabo solto derrubar em cascata quem depende dele. Quem conta a verdade sobre
> a porta é o `/health`.

### 3.3 Carregar e dispensar pelo HTTP

Ponha **3 caixas** no cartucho do slot 1. Então:

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8100/comandos/carregar -ContentType application/json -Body '{"dispenser_id":1,"medicamento":"Teste","sku":"APSEN-TESTE-001","categoria":"teste","quantidade":3,"os_id":"ENSAIO-1"}'
```

A resposta do POST é só o **ACK**. O resultado chega depois, como evento — veja
no log do adapter:

```
[EVT] carregado             ← D1 | OS ENSAIO-1
```

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8100/comandos/dispensar -ContentType application/json -Body '{"dispenser_id":1,"os_id":"ENSAIO-1"}'
```

**Agora olhe a bancada**: o servo do slot 1 tem que fazer três ciclos completos
`min → max → min`, e três caixas têm que cair. No log:

```
[EVT] dispensado            ← D1 | OS ENSAIO-1
```

O evento tem que trazer `quantidade_dispensada: 3`, `quantidade_alvo: 3`,
`falha_mecanica: false` e `motivo_falha: null`.

**Se sair `quantidade_dispensada` menor que 3 com as caixas todas caídas**, o
prazo por unidade está curto demais — volte ao 2.3. **Se sair 3 com duas caixas
caídas**, um sensor está contando o que não deve: volte ao 2.4.

### 3.4 Provar a injeção de falha

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8100/comandos/carregar -ContentType application/json -Body '{"dispenser_id":1,"medicamento":"Teste","sku":"APSEN-TESTE-001","categoria":"teste","quantidade":3,"os_id":"ENSAIO-2"}'
```

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8100/comandos/dispensar -ContentType application/json -Body '{"dispenser_id":1,"os_id":"ENSAIO-2","injetar_falha":"falha_mecanica_dispenser"}'
```

Têm que cair **duas** caixas, e o evento vem com `quantidade_dispensada: 2`,
`quantidade_alvo: 3`, `falha_mecanica: true` e `falha_injetada: true`. É a
falha que o console arma para a banca, agora com mecânica de verdade.

### 3.5 Provar a limpeza

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8100/comandos/limpar -ContentType application/json -Body '{"dispenser_id":1,"solicitado_por":"ensaio"}'
```

O evento `limpeza_ok` **não carrega `os_id`** — limpeza é operação de slot, e é
assim que o contrato define. Nada se move: limpar é o operador retirando o
resíduo, e o firmware só registra que o slot está vazio.

### 3.6 Provar as telas (se elas existem)

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8100/comandos/estado-celula -ContentType application/json -Body '{"trava_ativa":true,"trava_slot_id":3,"os_id":"ENSAIO-3","trava_resumo":"divergência de peso"}'
```

A tela do **D3** tem que mostrar o alerta com **AGUARDE SUPERVISOR** e o
resumo; as outras sete, **PARADO — D3** com o conteúdo esmaecido. Para soltar:

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8100/comandos/estado-celula -ContentType application/json -Body '{"trava_ativa":false,"trava_slot_id":null,"os_id":"","trava_resumo":""}'
```

> **Com `TELA_DRIVER_LOG` (o default), a "tela" é o Monitor Serial da placa das
> telas.** Isso não é um teste de mentira: ele prova que o comando chegou e que
> o layout está certo, e separa "a mensagem não chegou" de "o painel não
> acendeu" — que é a pergunta difícil quando um display fica preto.

Passou daqui, o transporte está provado: comando saiu do HTTP, virou linha JSON
na serial, a placa deu ACK, executou, e o evento voltou. Todo o resto é
orquestração.

---

## Ensaio 4 — os dispensers dentro da planta

### 4.1 O `.env`

O bloco que importa, com os adapters seriais fora do Docker:

```
DISPENSER_ADAPTER_URL=http://host.docker.internal:8100
```

E o `COMPOSE_PROFILES=simulado` **fora**, ou os simuladores sobem e disputam a
porta 8100 com o processo do host. O resto está em
[`../docs/DEPLOY_WINDOWS.md`](../docs/DEPLOY_WINDOWS.md).

### 4.2 Subir a stack

```bash
docker compose down; docker compose up -d
```

> `;` e não `&&`: o Windows PowerShell 5.1 não tem os operadores de cadeia
> `&&`/`||` — eles são erro de PARSER, não comando que falha.

> O `down` é necessário e não é zelo: o `DISPENSER_ADAPTER_URL` mudou, e
> container já criado não relê variável de ambiente. Sem recriar o central, ele
> continua mandando `carregar` para o endereço antigo.

```bash
docker compose ps --format "table {{.Service}}\t{{.Status}}"
```

Nenhum `dispenser-adapter` nem `dispenser-simulator` — a porta 8100 fica livre
para o processo do host.

### 4.3 Subir o adapter do host e conferir

```bash
dispenser-adapter\iniciar_host.bat
```

```bash
Invoke-RestMethod http://localhost:8100/health | ConvertTo-Json -Depth 5
```

`serial.conectado: true`, `serial_tft.conectado: true`, e em `checks` o
`central-computer: ok`.

O **pré-voo** (<http://localhost:8000/console/prevoo>) é a resposta rápida para
"subiu certo?".

### 4.4 O catálogo precisa concordar com a caixa real

O `peso_unitario_g` de cada medicamento vem da tabela `medicamentos`, e é a
balança que compara. Se o catálogo diz 45 g e a caixa real pesa 38 g, o desvio
é de 15% contra uma tolerância de 5% — e **todo slot diverge**, com as caixas
fisicamente certas na mesa.

```bash
docker compose exec mysql mysql -uapsen -p apsen_db -e "SELECT nome, peso_unitario_g FROM medicamentos LIMIT 10;"
```

Carregue os cartuchos com os medicamentos que as ordens padrão usam — o console
(<http://localhost:8000/console>) lista as dez com os itens de cada uma.

### 4.5 Disparar uma OS pelo console

Abra o console, escolha uma ordem cujos itens estejam nos cartuchos, e dispare.
O que tem que acontecer, na ordem:

1. os slots da OS recebem `carregar` — **os oito em paralelo**, não um por vez;
2. a CNC vai ao primeiro slot (se ela ainda for simulada, isso é instantâneo);
3. **o servo daquele slot cicla**, e as caixas caem;
4. a câmera e a balança conferem;
5. repete para cada slot, e no fim a CNC volta ao HOME.

No dashboard (<http://localhost:8050>) os oito cartões desenham a bancada em
duas fileiras, com o corredor da CNC no meio — "D7 travou" aponta para um lugar
físico, não para a sétima posição de uma lista.

### 4.6 Provar a trava com falha de verdade

No console, **arme** "Falha mecânica no dispenser" no slot que a próxima OS vai
usar, e dispare. O dispenser solta uma unidade a menos, a balança mede o
déficit sozinha, o Triple Check trava a OS, e as telas do dispenser pintam o
alerta. Liberar é no console ou no painel de bancada, com PIN de supervisor.

É a demonstração inteira, com mecânica real.

---

## Sintomas e o que eles querem dizer

| sintoma | causa quase certa |
|---|---|
| a placa não é achada / `"conectado": false` | Monitor Serial aberto (a COM é exclusiva), ou COM errada na variável, ou a COM não foi fixada e outro processo está varrendo |
| `PCA9685 NAO responde` no boot | pinos do I²C (item 1.1). A mensagem imprime os dois GPIO que ela usou |
| o comando chega e **nenhum ACK volta** | a linha JSON caiu no caminho do humano. Confira com o teste do item 1.4: se aparecer "Terminal fechado. Digite MANUT para abrir.", é isso |
| o ACK volta mas o **evento nunca chega** ao central | `CENTRAL_URL` apontando para `central-computer:8000` — nome DNS da rede Docker, que não resolve no host. Tem que ser `localhost:8000` |
| `quantidade_dispensada` menor com as caixas todas caídas | `DISPENSA_TIMEOUT_UNIDADE_MS` curto demais (item 2.3) |
| o IR conta **duas vezes** o mesmo comprimido | debounce curto para este medicamento: `IR_DEBOUNCE_MS` (20 ms) no topo do sketch. Confira antes com `IRLOG` |
| o IR conta o pulso do **slot vizinho** | par servo↔sensor trocado na fiação (item 2.4) |
| o servo bate no fim de curso no boot | aquele servo está com a calibração do `CAL_BACKUP` (0..180). O boot **não move** servo não calibrado e avisa — se moveu, a NVS tinha um valor ruim. Calibre (2.1) e faça o `EXPORT` (2.2) |
| a OS aborta com `timeout_carregamento` | o adapter não está no ar, ou o `DISPENSER_ADAPTER_URL` do central ainda aponta para o container |

Um mais raro e mais confuso: se o adapter do host for reiniciado com a placa
ligada, o contador de `cmd_id` dele volta a 1. O firmware detecta pelo silêncio
de pongs e zera o dele — a linha `[APSEN] adapter voltou - contador de cmd_id
zerado` no Monitor Serial é a confirmação. Sem ela, o primeiro comando depois
do restart seria confirmado **sem ser executado**, e o orquestrador esperaria
para sempre por um evento que ninguém ia produzir.

---

## Como voltar atrás

Para devolver a planta ao estado 100% simulado: pare o adapter do host, comente
`DISPENSER_ADAPTER_URL` e ponha `COMPOSE_PROFILES=simulado` no `.env`. Depois:

```bash
docker compose down; docker compose up -d
```

Voltam os 13 serviços e o `dispenser_simulator`. Nada no banco, no dashboard ou
no histórico guarda resíduo do ensaio.

O manual das placas, com a tabela completa de eventos e comandos, é
[`README.md`](README.md); o contrato do protocolo é
[`../docs/PROTOCOLO_SERIAL.md`](../docs/PROTOCOLO_SERIAL.md) §3 e §6.
