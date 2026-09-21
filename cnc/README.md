# Mesa CNC CoreXY — receitas gravadas à mão

A mesa que percorre o corredor entre as duas fileiras de dispensers. Ela
substitui temporariamente o FluidNC + sender: em vez de receber G-code, ela
**guarda na própria NVS** o roteiro de cada ordem — dez receitas, `A` a `J`,
uma por OS padrão do `erp-simulator` — e executa a que for pedida.

| arquivo | o que é |
|---|---|
| `receitas_manuais/receitas_manuais.ino` | o firmware. CoreXY com dois drivers, dois fins de curso, homing, envelope por soft-limit, rampa trapezoidal e o terminal humano. |

> A Arduino IDE exige que o sketch esteja numa pasta com o mesmo nome — daí
> `receitas_manuais/receitas_manuais.ino`.

Compilar:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 cnc/receitas_manuais
```

> **O `arduino-cli` não está no PATH** nesta máquina — a Arduino IDE traz o
> dela. Defina uma vez por janela do PowerShell e use `& $ACLI` no lugar de
> `arduino-cli`, como fazem os outros dois firmwares:
>
> ```bash
> $ACLI = "$env:LOCALAPPDATA\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
> ```

---

## As duas vozes, e o `{` que as separa

A mesa atende DOIS interlocutores na mesma porta, como a balança e os
dispensers:

* **o humano** — o terminal inteiro descrito abaixo (`H`, `MP 4 20`, `EXEC C`,
  WASD). Continua igual, inclusive o boot barulhento;
* **a máquina** — o `cnc-adapter`, uma linha JSON por mensagem.

O separador é o `{`, e o corte é feito na linha **crua**, antes de qualquer
transformação. Essa ordem é a feature: `processar_comando()` põe a linha em
maiúsculas, e um `toUpperCase` em cima de `{"cmd":"mover","cmd_id":7}` produz
`{"CMD":"MOVER","CMD_ID":7}` — que não casa com chave nenhuma. O comando sumiria
sem erro, e o orquestrador esperaria para sempre por um ACK que ninguém ia
mandar.

Quem corta é `despacharLinha()`, no `apsen_serial.h`. O `processar_comando()`
também recusa uma linha com `{`, e essa segunda guarda não é redundância: ela
está no ponto EXATO onde a invariante pode ser violada, que é o que sobrevive a
um chamador novo — um auto-teste no boot, um replay de linha guardada — escrito
por quem não vai reler o header.

Nenhuma linha do caminho humano contém `{`, inclusive o boot. Há teste medindo.

---

## O endereço é o DISPENSER, e a posição volta MEDIDA

A mesa **não recebe coordenadas**. O comando `mover` traz `dispenser_alvo`, e a
placa vai ao waypoint calibrado que está nela (`dispenser_pos()`, o único ponto
de tradução do firmware). Em troca, ela **reporta** no evento `posicionado`
onde parou — `pos_x_mm()`/`pos_y_mm()` lidos DEPOIS do movimento —, e é essa
medida que o central registra.

**Por que não pela receita.** O waypoint gravado por `REC`/`MARK` é indexado
pela ORDEM DE GRAVAÇÃO, e a rota de uma OS é decidida no central em tempo de
execução (serpentina) e MUDA quando um slot sai da ordem por falta de estoque.
"Vá ao ponto 3" seria outro dispensador no dia seguinte: a mesa iria ao lugar
errado, a câmera leria o SKU de quem estava ali, e o sintoma chegaria como
divergência num slot só — o quadro exato de uma falha mecânica. As constantes
`D1`..`D8` não têm esse problema: elas são a posição física do dispensador, e é
delas que o próprio `WP()` das receitas já sai.

`receita` viaja no comando, vai para o log e **não** é usada para achar a
posição. Ela é opcional: a mesa sabe ir a um dispensador sem saber de qual OS
ele faz parte, e exigi-la transformaria um campo de rastreio em motivo de
recusa.

### Comandos (adapter → placa)

| `cmd` | campos | efeito |
|---|---|---|
| `mover` | `dispenser_alvo`, `os_id`, `receita`, `ciclo_atual`, `total_ciclos` | vai ao waypoint do dispensador |
| `homing` | `os_id` | refaz a origem contra os fins de curso |
| `estado_celula` | `trava_ativa`, `trava_slot_id`, `os_id`, `trava_resumo` | trava do Triple Check |

`FEED`, `ACCEL`, `STEP`, `NOVOCENTRO`, `REC`, `MARK` e `SAVE` **não existem em
JSON**, e a ausência é decisão. Eles mudam a calibração da máquina, e calibração
mudada de fora não deixa rastro na bancada onde alguém vai procurar por que a
mesa passou a parar dois milímetros adiante.

### Eventos (placa → adapter)

| `tipo` | campos |
|---|---|
| `posicionado` | `os_id`, `dispenser_alvo`, `posicao_x`, `posicao_y`, `ciclo_atual`, `total_ciclos`, `ts` |
| `concluido` | `os_id`, `posicao_x`, `posicao_y`, `ts` |
| `erro` | `os_id`, `dispenser_alvo`, `codigo_erro`, `descricao`, `ts` |

**Nada de periódico durante o movimento** — nem `movendo`, nem `retornando`,
nem progresso. O motivo é de tempo, e está na tabela de trajetos abaixo: uma
linha de ~200 B a 115200 baud custa ~17 ms, e o central dispara o `dispensar`
por relógio. Cada linha emitida empurra a chegada real para depois da hora
agendada — comprimido caindo com a mesa em trânsito.

### `codigo_erro` — vocabulário FECHADO

| `codigo_erro` | comando | quando |
|---|---|---|
| `dispenser_invalido` | `mover` | `dispenser_alvo` fora de 1..8 |
| `fora_do_envelope` | `mover` | o waypoint calibrado caiu fora dos limites |
| `sem_homing` | `mover` | origem nunca estabelecida |
| `limit_disparado` | `mover` | fim de curso, antes ou durante o movimento |
| `travado` | `mover` | trava do Triple Check ativa |
| `homing_falhou` | `homing` | um eixo não achou o fim de curso no prazo |

`receita_desconhecida` aparece no `cnc_simulator` e **não** nesta placa: a placa
aceita qualquer `receita` (é log, não endereço), e quem a valida é o simulador,
para encenar na demonstração a ordem cujo roteiro ninguém gravou.

Toda recusa sai com **ACK negativo E evento `erro`**. Os dois, porque vão para
leitores diferentes: o ACK negativo vira 502 no adapter e cancela o cronograma
do central antes da hora do `dispensar`; o evento vira alarme e histórico, com o
código que manda o técnico à peça certa.

A única falha depois do ACK positivo é o fim de curso disparando com a mesa
andando — aí não há ACK a corrigir, e o evento é o aviso.

---

## A trava do Triple Check para a mesa

A mesa é a peça que está fisicamente sobre a bancada onde o supervisor vai
mexer — e, sob o ciclo por relógio, é também a peça que continua andando sozinha
se ninguém a avisar. Por isso o `estado_celula`, que até agora ia só para as
telas TFT, passou a ir para ela também.

Com `trava_ativa: true`, a mesa:

1. responde ACK **imediatamente** — ele tem de ser aceito sobretudo NO MEIO de
   um movimento, e é para isso que `sync_move()` chama `serialPoll()` a cada
   passo;
2. interrompe o movimento (o laço confere a flag na mesma iteração barata em
   que já confere o hard-limit);
3. emite `erro` com `travado` para a OS que esperava a chegada, e faz homing;
4. **recusa** todo `mover` seguinte, com ACK negativo, até a liberação;
5. apita (`beep_alerta`) e mostra o `trava_resumo` no terminal — quem está na
   bancada precisa saber por que a mesa fugiu.

Com `trava_ativa: false` volta a aceitar comandos, **sem homing**: ela já está
no HOME desde a ativação, e um segundo homing custaria segundos no exato momento
em que o supervisor acabou de liberar a produção.

O comando age só na TRANSIÇÃO — receber `true` duas vezes é inofensivo. O
central reenvia o aviso sempre que a trava muda, sem saber o que a placa já
sabe.

`trava_resumo` tem teto de 48 caracteres e chega já cortado: ele é a CATEGORIA
da divergência ("divergência de peso"), nunca o motivo formatado, que passa de
240 caracteres e é do display de 7" e da web. A tela da mesa responde uma
pergunta só: é este slot?

---

## Os trajetos, e a conta que os gerou

Com `STEPS_PER_MM = 80`, `FEED_MM_MIN = 750` (→ 1000 passos/s) e
`ACCEL_MM_S2 = 500`, somando o meio-período real de cada pulso da rampa, em
CoreXY (`max_p = max(|dx+dy|, |dx−dy|) × 80`):

| trajeto | passos | tempo |
|---|---:|---:|
| HOME → D8 (**o pior da célula**) | 2000 | **2016 ms** |
| HOME → D7 | 1920 | 1936 ms |
| HOME → D4 (o mais curto do HOME) | 1200 | 1216 ms |
| D1 → D5 / D4 → D8 (pior par) | 960 | 976 ms |
| D1 → D2 (o mais curto) | 96 | 96 ms |

A rampa é irrelevante: `n_ramp` = 12 passos, 0,15 mm. O trajeto é tempo de
cruzeiro quase puro, e é por isso que ele é previsível — que é o que permite ao
central agendar em vez de esperar.

Daí saem os dois números do `central-computer/config.py`:

* **`CNC_TETO_TRAJETO_S = 2,5`** — 2016 ms mais folga, vale para qualquer par.
  É um **teto**, não uma cópia da geometria: precisa ser MAIOR que o trajeto
  real, não igual. Por isso ele não quebra quando alguém regrava um waypoint na
  bancada — mas **quebra se alguém subir o `FEED` sem subir o teto**.
* **`CNC_MARGEM_CHEGADA_S = 0,75`** — serial, ACK, salto de thread do adapter e
  latência do `loop()`.

O dwell gravado nas receitas é `(quantidade + 1) × 1000 ms` → 3 s (qty 2) a 16 s
(qty 15). O trajeto é 6 % a 40 % do ciclo; o dwell domina. **Essa conta é a
mesma do `DISPENSA_S_POR_UNIDADE` do central**, e as duas descrevem o mesmo
mecanismo físico: mudar uma sem a outra faz o central cortar a dispensa no meio,
e a OS termina "completa" com menos comprimido.

---

## Sem placa

`tests/fakes/placa_cnc.py` fala o contrato inteiro por `socket://` — um handler
de URL do próprio pyserial, servido por um socket TCP em localhost. O caminho de
abertura exercitado é o de verdade; o que muda é o que está do outro lado do
fio.

Ela é o duplo do firmware e o documento executável contra o qual ele foi
escrito. Dois detalhes que só ela tem:

* **a bancada dela é DIFERENTE do `POSICOES` do central**, de propósito. Iguais,
  toda asserção sobre posição passaria por concordância acidental — o central
  compararia o próprio número com uma cópia dele;
* **`mover_mudo`** encena a mesa que não confirma nunca, e `atraso_evento`
  encena a que confirma tarde demais. Sem os dois, nenhum teste consegue
  encenar o caso que define o ciclo por relógio.

---

## Hardware

Da cabeça do sketch — confira contra a bancada antes de energizar.

| o quê | pino |
|---|---|
| Motor X (A do CoreXY) | `STEP 23` · `DIR 22` · `EN 21` |
| Motor Y (B do CoreXY) | `STEP 25` · `DIR 33` · `EN 26` |
| Fim de curso X | `GPIO 19` — NC, `LOW` = acionado |
| Fim de curso Y | `GPIO 32` — NC, `LOW` = acionado |
| Buzzer | `GPIO 13` |

Resolução: `STEPS_PER_MM = 80`. Serial a **115200 baud**.

**O USB é só dados e a lógica do ESP32.** Os drivers de passo têm alimentação
própria; nada se move sem ela. Duas cautelas de sempre:

* **nunca conecte ou desconecte motor com a fonte dos drivers ligada** — o
  transitório mata driver;
* os dois motores são **habilitados no `setup()` e ficam energizados**. Com a
  planta ligada o carro não se empurra à mão: para movê-lo, use jog (`w a s d`)
  ou corte a alimentação dos drivers.

---

## Passo a passo — do zero até a mesa andando

### 1. Gravar o firmware

Descubra a porta (Gerenciador de Dispositivos → *Portas (COM e LPT)*), feche
qualquer Monitor Serial aberto nela e grave:

```bash
& $ACLI compile --fqbn esp32:esp32:esp32 cnc/receitas_manuais
```

```bash
& $ACLI upload -p COM5 --fqbn esp32:esp32:esp32 cnc/receitas_manuais
```

Troque `COM5` pela sua em todos os comandos daqui para a frente. Pela IDE dá
na mesma: abra `receitas_manuais/receitas_manuais.ino`, placa *ESP32 Dev
Module*, Upload.

### 2. Ligar — a ordem importa

1. **Confira o caminho livre.** O boot faz homing sozinho: o carro vai andar
   assim que a placa subir.
2. **Fonte dos drivers primeiro**, USB depois. Ligar o USB antes deixa o ESP32
   comandando driver sem alimentação — inofensivo, mas o homing falha e você
   perde 60 s de timeout descobrindo isso.
3. **Abra o Monitor Serial em 115200** antes ou logo após energizar. O boot
   inteiro vale ser lido, e ele passa rápido.

### 3. O primeiro boot, linha a linha

```
═══════════════════════════════════════════════════════
  NEXT 2K26 — Receitas Manuais v1
═══════════════════════════════════════════════════════
```

(um beep longo)

```
[NVS] Slots carregados. Total de 0 waypoints em 0 slots ativos.
[NVS] FEED=750 mm/min | ACCEL=500 mm/s² | STEP=1.00 mm | DWELL=500 ms
[NVS] Centro operacional: (5.75, 20.25) mm

─── SLOTS ───
   A :  0 waypoint(s) (vazio)
   ...
Estado limits: X=livre  Y=livre
```

Se algum fim de curso aparecer como `TRIGGERED`, o firmware avisa e espera 3 s
antes do homing. **Não precisa correr para soltar o carro**: o homing trata
esse caso — a busca do eixo já acionado sai na hora e o *pulloff* recua 3 mm.

Depois vem o homing (dois beeps no início, três no fim), a ajuda completa e o
status. `Pronto pra gravar receitas ou executar.` fecha o boot.

### 4. Conferir que o homing é de verdade

```
[HOMING] Seek X...
[HOMING] X TRIGGERED
[HOMING] Seek Y...
[HOMING] Y TRIGGERED
[HOMING] Zero em (0.00, 0.00)
```

É **isto** que precisa aparecer. Se um eixo não achar o fim de curso, o
firmware **não** finge que chegou:

```
[HOMING] TIMEOUT X — fim de curso não encontrado em 60000 ms
╚════ HOMING FALHOU no eixo X ════╝
[HOMING] Origem NÃO foi definida — a posição atual não vale nada.
```

Nesse estado `?` mostra `Homed=nao`, e `EXEC` refaz o homing antes de executar
qualquer coisa. Isso é a correção do defeito mais perigoso da versão anterior:
antes, o timeout era engolido, o firmware zerava mesmo assim e a mesa passava a
acreditar que estava no zero em qualquer lugar onde tivesse parado — com todo
soft-limit calculado a partir de uma origem inventada.

Mande `?` e confirme:

```
[STATUS] Pos=(0.00, 0.00)  Homed=SIM  Grav=off
```

### 5. Conferir o envelope ANTES de gravar qualquer coisa

O `?` imprime o envelope em vigor:

```
         Envelope ATIVO: X ∈ [-2.50, 8.00]  Y ∈ [-1.50, 42.00]
         Switch em -3.00 mm (pulloff 3.00) → piso seguro -2.50 mm
         ⚠ Piso limitado pelo pulloff. Medido era X≥-6.00 Y≥-1.50
```

Leia a seção [O envelope e a conta do pulloff](#o-envelope-e-a-conta-do-pulloff)
antes de mexer nesses números. Depois caminhe os quatro extremos com `MP` e
veja se o carro chega sem tocar em nada:

```
MP -2.5 0
MP 8 0
MP 0 42
MP 0 -1.5
C
```

`C` devolve ao centro operacional. Se algum extremo bater no fim de curso, o
envelope está errado — e o número a corrigir é o `_MEDIDO_MM` correspondente,
no sketch, não um valor chutado no terminal.

### 6. Gravar uma receita

A receita é a sequência de paradas de UMA ordem. Fluxo:

```
H                 ← garanta origem boa antes de gravar posição absoluta
REC C             ← abre o slot C (APAGA o que havia nele, na memória)
MP 2 12           ← leve o cabeçote à primeira parada (ou jog: wwwdd)
MARK              ← grava a posição atual, com o DWELL padrão
MARK 800          ← ou grava com 800 ms de parada
MP 2 26           ← próxima parada
MARK
POP               ← errou? remove o último waypoint
SHOW C            ← confira antes de persistir
SAVE              ← só aqui vai para a NVS (dois beeps)
```

`CANCEL` no lugar de `SAVE` descarta as mudanças e recarrega o slot da NVS.
Até 20 waypoints por receita, 10 receitas (`A`–`J`).

Três coisas que se aprendem errando:

* **`REC` apaga o slot na memória na hora.** Quem protege a receita antiga é o
  `CANCEL`; quem a destrói é o `SAVE`.
* **`MARK` grava a posição ATUAL**, então `Homed=SIM` é pré-requisito: sem
  origem válida você está gravando coordenadas de um sistema que não existe.
* **A letra ainda é escolha sua.** O amarrado entre "ordem padrão nº 3" e
  "receita C" é a task 6, no central. Hoje anote em papel qual letra é qual
  ordem.

### 7. Executar

```
EXEC C            ← executa a receita do slot C
EXEC ALL          ← executa A → B → ... → J, pulando vazios  (apelido: EXE)
LIST              ← quantos waypoints cada slot tem
```

**Execução exige o verbo `EXEC`, e isso é de propósito.** Antes, `d` era um jog
de 1 mm em X+ e `D` executava a receita da OS inteira: a diferença era só a
caixa, e a caixa depende do teclado, do Caps Lock e do terminal. Um `A` onde se
queria `a` fazia a mesa rodar uma OS completa. Hoje a letra solta responde com
a instrução certa e não move nada.

Corolário: `H` e `C` sozinhos continuam sendo *homing* e *centro*. As receitas
desses dois slots só rodam por `EXEC H` e `EXEC C`.

### 8. Desligar

Não há o que salvar: `SAVE` e os comandos de parâmetro já persistem na NVS.
Corte a alimentação dos drivers e depois o USB. Feche o Monitor Serial — no
Windows a COM é exclusiva de um processo, e deixá-lo aberto impede o
`cnc-adapter` de abrir a porta depois.

---

## O envelope e a conta do pulloff

O homing busca o fim de curso, **recua `PULLOFF_MM` (3,00 mm) e só então zera**.
Logo o switch fica a 3 mm do zero, do lado em que a busca correu — e, pelas
direções calibradas deste sketch, esse lado é o negativo nos dois eixos:

```
switch de X  →  x = −3,00 mm
switch de Y  →  y = −3,00 mm
```

O mínimo de X medido à mão era **−6,00**, ou seja **3 mm além do fim de curso**.
Um `MP -6 0` perfeitamente legítimo — aceito pelo soft-limit — corria para
dentro do switch e voltava `⚠ EMERGÊNCIA! Limit disparou`. Em Y a conta fecha:
−1,50 está 1,5 mm antes do switch.

A saída não foi inventar um envelope novo. O valor medido continua declarado e
visível (`LIMITE_MIN_X_MEDIDO_MM`), e o piso **ativo** é o mais conservador
entre ele e `PISO_SEGURO_MM = −PULLOFF_MM + MARGEM_SWITCH_MM` = −2,50 mm.
Derivar é aritmética sobre o pulloff, que é configuração conhecida; escrever um
número novo seria medir de mentira.

**Quando você medir o curso real**, corrija o `_MEDIDO_MM` no sketch. O piso
derivado continua valendo como teto de segurança, e o `?` avisa sempre que ele
estiver mandando.

---

## Comandos do terminal humano

### Movimento

| comando | efeito |
|---|---|
| `H` | refaz o homing |
| `?` | status: posição, homed, parâmetros, envelope ativo |
| `C` / `CENTRO` | vai ao centro operacional |
| `NOVOCENTRO x y` · `AQUI` · `PADRAO` | define o centro operacional (persiste) |
| `w` `a` `s` `d` | jog de `STEP` mm em Y+ / X− / Y− / X+ |
| `wwdd`, `ddss`, … | sequência: N letras = N jogs sucessivos |
| `X+2` · `X-2` · `Y+3` · `Y-3` | jog explícito em mm |
| `MP x y` | move para a posição absoluta (validada pelo envelope) |

### Parâmetros — todos persistem na NVS

| comando | faixa |
|---|---|
| `FEED N` | 30 – 6000 mm/min (cruzeiro da rampa) |
| `ACCEL N` | 20 – 5000 mm/s² |
| `STEP N` | 0,1 – 50 mm (tamanho do jog WASD) |
| `DWELL N` | 0 – 60000 ms (parada padrão dos waypoints) |

### Receita

| comando | efeito |
|---|---|
| `REC <A-J>` | abre o slot para gravação (limpa na memória) |
| `MARK` · `MARK <ms>` | grava a posição atual como waypoint |
| `POP` | remove o último waypoint gravado |
| `SAVE` | persiste na NVS e fecha a gravação |
| `CANCEL` | aborta e recarrega o slot da NVS |
| `EXEC <A-J>` · `EXEC ALL` · `EXE` | executa |
| `LIST` · `SHOW <A-J>` | inspeciona |
| `CLEAR <A-J> YES` · `CLEAR ALL YES` | apaga (confirmação obrigatória) |

`AJUDA` (ou `HELP`) imprime tudo isso na própria placa.

---

## O que a NVS guarda

Namespace `receitas`:

| chave | o quê |
|---|---|
| `n_<slot>` | quantos waypoints o slot tem |
| `x_<slot>_<i>` · `y_<slot>_<i>` · `t_<slot>_<i>` | posição e dwell de cada ponto |
| `feed` `accel` `step` `dwell` `cx` `cy` | parâmetros globais e centro operacional |

`SAVE` **apaga as chaves dos pontos que deixaram de existir** quando a receita
encolhe. Hoje isso é inofensivo — `num_pontos` controla a leitura —, e deixa de
ser assim que o formato do waypoint mudar (task 2): uma chave órfã do formato
antigo seria lida como ponto do formato novo, com um campo faltando e nenhum
erro.

Gravar firmware **não** apaga a NVS. Para zerar tudo, `CLEAR ALL YES`.

---

## Quando dá errado

| sintoma | causa provável | o que fazer |
|---|---|---|
| `HOMING FALHOU no eixo X` (ou Y) | switch não aciona: fiação, conector, carro preso antes do fim | conferir o GPIO da mensagem; `?` confirma `Homed=nao` |
| `⚠ EMERGÊNCIA! Limit disparou` + `Origem INVALIDADA` | a mesa encostou em algo no meio do movimento | `H`. O abort invalida a origem de propósito: se o limit disparou, a correia pode ter pulado |
| `[SOFT-LIMIT] (…) fora do envelope — REJEITADO` | destino fora de `?` | é o envelope funcionando; se o ponto é legítimo, o número a rever é o `_MEDIDO_MM` |
| `⚠ Limit já triggered — movimento abortado` | o carro está em cima de um fim de curso | `H` — a busca sai na hora e o pulloff recua 3 mm |
| Homing "passa" na hora, sem o carro andar | switch preso em acionado (fiação em curto, NC aberto) | ⚠ **o firmware não pega este caso**: ele zera onde estiver. Conferir o `Estado limits` no boot |
| `'A' sozinho não executa mais receita nenhuma` | hábito da versão anterior | `EXEC A` |
| Placa muda no Monitor Serial | baud errado, ou outro processo com a COM aberta | 115200; feche o outro Monitor |
| A mesa não se move com o adapter | **esperado hoje** — a placa não fala JSON | ver "O estado de hoje", no topo |

---

## Ligar na célula

O default continua sendo `CNC_TRANSPORTE=http`, com o `cnc_simulator`: é o que
a suíte, o CI e a demonstração em Docker usam, e nenhuma variável precisa mudar
para `docker compose --profile simulado up` subir os 13 serviços como sempre.

**A placa já obedece.** Ela fala as duas vozes, atende `mover`, `homing` e
`estado_celula`, e o `CNC_TRANSPORTE=serial` funciona de ponta a ponta. O que
falta é de bancada, não de código — a lista está no fim deste arquivo.

A configuração é a mesma dos outros adapters, e a COM tem que ser **fixada**
pelo motivo que o
[README da raiz](../README.md#portas-seriais-na-célula-montada) detalha (cinco
placas varrendo cinco portas é boot não-determinístico):

```
CNC_TRANSPORTE=serial
CNC_SERIAL_URL=COM8
CNC_SERIAL_BAUD=115200
CNC_ACK_TIMEOUT_S=2
```

**Esse bloco já está escrito, e o lugar dele é
[`../cnc-adapter/iniciar_host.bat`](../cnc-adapter/iniciar_host.bat).**
Você edita **uma linha** — a COM, no bloco do topo — e roda o `.bat`; ele liga
o transporte serial, aponta o central e sobe o adapter na porta 8101. Não vai
no `.env` da raiz: na célula montada este processo roda fora do Docker, e
ninguém lê o `.env` dele. É a mesma regra dos outros três processos com placa
— [Onde escrever o número da COM](../README.md#onde-escrever-o-número-da-com).

Para voltar ao simulador a qualquer momento, `set CNC_TRANSPORTE=http` antes de
chamar o `.bat` — a perna de cima do adapter é a mesma nos dois transportes, e
nada mais precisa mudar.

> **Antes do primeiro ciclo com a placa:** grave as dez receitas. O caminho do
> central não passa por elas (ele endereça por dispensador), mas o `EXEC` da
> bancada sim — e é o `EXEC` que se usa para conferir que a mesa para onde deve
> antes de deixar uma OS de verdade comandá-la.

---

## A CONFIRMAR NA BANCADA

* **`MARGEM_SWITCH_MM = 0,50`** — número novo, escolhido para cobrir a
  repetibilidade de um fim de curso mecânico comum. Não foi medido nesta
  máquina.
* **Curso útil real em −X.** O piso ativo é o derivado (−2,50). Se o curso for
  maior, corrija `LIMITE_MIN_X_MEDIDO_MM` — e confira se `PULLOFF_MM = 3,0` é
  o recuo de fato.
* **`LIMITE_MAX_X_MEDIDO_MM = +8,00`** e **`LIMITE_MAX_Y_MEDIDO_MM = +42,00`** —
  lado sem fim de curso: não há de que derivar, valem como foram medidos.
* **`LIMITE_MIN_Y_MEDIDO_MM = −1,50`** — fecha com a conta, mas herda o swap de
  sinal do tracker invertido.
* **Direções calibradas** (`HOMING_DIR_*`, `INVERTER_EIXO_Y`) depois de
  qualquer remontagem: a conta do pulloff depende delas, e sinal trocado só
  aparece em waypoint absoluto.
* **Centro operacional default** — mudou junto com o piso de X, porque é
  derivado do envelope. Bancada com NVS já gravada não sente.
* **As dez receitas**, uma por ordem padrão, com o cabeçote parando em cada
  dispensador. O caminho do central NÃO depende delas — ele endereça por
  `dispenser_alvo` —, mas a demonstração presencial e a conferência de
  geometria pelo `EXEC` dependem.

### Do ciclo por relógio — o central agenda, e estes números são o relógio

* **`CNC_TETO_TRAJETO_S = 2,5 s`.** Sai da conta do sketch (2016 ms no pior
  trajeto) e assume `FEED = 750`. **Meça o pior curso com cronômetro**, e se o
  valor real encostar no teto, suba o teto ANTES de subir o `FEED` — quem mexe
  no `FEED` pela bancada muda o trajeto e o central não fica sabendo.
* **`CNC_MARGEM_CHEGADA_S = 0,75 s`** é estimativa de serial + ACK + salto de
  thread. Meça o intervalo real entre o POST do `mover` e a chegada do
  `posicionado` com a mesa parada.
* **`DISPENSA_S_POR_UNIDADE = 1 s`** vem do `(qty+1)×1000` gravado nas receitas.
  Se o ciclo do servo for mais lento, TODO dwell está curto e o modelo por
  relógio corta a dispensa no meio — **é o pior sintoma possível desta frente**,
  porque a OS termina "completa" com menos comprimido no leito.
* **O custo do `serialPoll()` dentro do `sync_move()`.** Ele roda uma vez por
  passo (até 2000 num trajeto HOME→D8). Com o canal parado o custo é de poucos
  microssegundos por passo, mas isso não foi medido nesta placa — confira se o
  trajeto real ainda cabe em `CNC_TETO_TRAJETO_S` depois de ligar o serial.
* **O `EXEC` de bancada não lê a serial durante o dwell.** Um dwell de 16 s sem
  `pingPoll` faz a placa achar que o adapter sumiu e zerar `ultimoCmdId` no pong
  seguinte. Inofensivo hoje (durante o `EXEC` não há OS em curso), mas confira
  se a bancada passar a usar `EXEC` com o adapter conectado.
