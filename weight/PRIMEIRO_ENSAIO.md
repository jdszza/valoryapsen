# Primeiro ensaio com a balança de verdade

Roteiro de uma vez só: da placa na mão até a balança medindo dentro de uma OS.
São **três ensaios**, e a ordem importa — cada um só faz sentido depois que o
anterior passou, porque cada um elimina uma classe de causa.

| # | o que liga | o que prova | precisa de Docker? |
|---|---|---|---|
| 1 | placa + Monitor Serial | o firmware fala as duas vozes, e a balança está calibrada | não |
| 2 | placa + `weight-adapter` no host | o transporte serial funciona, e o adapter acha a porta | não |
| 3 | a stack + a placa | o central manda `pesar` para a placa e o evento volta | sim |

Se o ensaio 3 falhar e você pulou o 2, não dá para saber se o problema é a
placa, o cabo, o adapter, o central ou o orquestrador. É para isso que os três
existem separados.

> **O `.env` já está configurado para isto.** A planta sobe com a balança real e
> **sem** dispenser, CNC e seus simuladores — a integração é incremental, e as
> outras duas placas entram depois. O que isso muda no que dá para testar hoje
> está no item 3.5.

---

## Antes de começar

- [ ] ESP32 da balança, com os 4 HX711 ligados e **alimentação externa** (o USB
      é só dados);
- [ ] um cabo USB de **dados** — cabo só de carga não enumera porta nenhuma, e o
      sintoma é "a COM não aparece";
- [ ] um **peso padrão** conhecido (uma massa de calibração, ou qualquer coisa
      cujo peso você confira numa balança de cozinha);
- [ ] **3 a 5 caixas de medicamento iguais**, das que vão para os dispensers —
      elas são a unidade que este sistema pesa (ver "A unidade é a CAIXA");
- [ ] o recipiente (pote/bandeja) que fica sobre a mesa de coleta;
- [ ] Arduino IDE 2.x instalado com o core `esp32:esp32` (já está nesta
      máquina).

> **Os comandos deste documento são PowerShell**, que é o terminal do mini PC.
> Três coisas mudam em relação ao bash, e cada uma falha de um jeito diferente:
>
> * `source` não existe — é builtin de shell POSIX;
> * `VAR=valor comando` não é sintaxe válida; variável de ambiente se define
>   em linha própria, com `$env:VAR = "valor"`;
> * **`curl` é alias de `Invoke-WebRequest`** no PowerShell 5.1, e pior: o
>   escape `\"` para executável nativo é quebrado nessa versão, então um corpo
>   JSON chega mutilado ao curl.exe e o FastAPI responde 422 com o comando
>   visivelmente certo na tela. Por isso as chamadas HTTP aqui usam
>   `Invoke-RestMethod`, que é nativo e devolve o objeto já desserializado.

O `arduino-cli` não está no PATH, mas a Arduino IDE traz o dela. Para encurtar o
resto do documento, defina uma vez por janela:

```bash
$ACLI = "$env:LOCALAPPDATA\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
```

---

## Ensaio 1 — a placa sozinha

### 1.1 Descobrir e fixar a COM

Com a placa ligada, veja o que apareceu:

```bash
& $ACLI board list
```

Anote o número e **fixe-o no Windows** antes de seguir: Gerenciador de
Dispositivos → *Portas (COM e LPT)* → botão direito → *Propriedades* →
*Configurações de Porta* → *Avançado…* → *Número da Porta COM*.

Fixar não é preciosismo. Sem a COM fixada e a variável preenchida, o adapter
varre todas as portas gastando até 9,5 s em cada candidata — e com as cinco
placas da célula isso vira boot não-determinístico em que uma placa às vezes
não é achada. O porquê está em [`../docs/DEPLOY_WINDOWS.md`](../docs/DEPLOY_WINDOWS.md).

### 1.2 Gravar o firmware

```bash
& $ACLI compile --fqbn esp32:esp32:esp32 weight/balanca2_3
```

```bash
& $ACLI upload -p COM7 --fqbn esp32:esp32:esp32 weight/balanca2_3
```

Troque `COM7` pela sua em todos os comandos daqui para a frente.

### 1.3 Abrir o Monitor Serial e ver as DUAS vozes

Abra o monitor a **115200** (Arduino IDE, PuTTY, o que preferir) e aperte o
botão de reset da placa.

**A mesa tem que estar VAZIA neste momento.** O `setup()` espera 5 s e, sem
resposta, faz tara automática — com peso em cima, a tara leva o peso junto.
(Esse comportamento é da 2.2 e não mudou de propósito: quem depende dele é o
operador que usa a balança sozinha.)

O que deve aparecer, misturado:

```
Calibracao carregada.
Config contagem carregada.

(Timeout) Tara automatica...
Tara c0: offset=8412
...
TARA OK.
{"evento":{"tipo":"tara_balanca","alvo":"canais","valor_g":null,"ts":"1970-01-01T00:00:00"}}

Balanca + Contagem pronta. '?' para comandos.
{"evento":{"tipo":"boot","fw":"2.3","canais_ativos":[true,true,true,false],"uw_g":10.00,"ts":"..."}}
{"cmd":"ping","sub":"weight"}
{"evento":{"tipo":"peso","total_g":0.12,"canais_g":[0.05,0.03,0.04,null],"sat":false,"estavel":true,"ts":"..."}}
```

Confira quatro coisas:

1. **as linhas humanas continuam todas lá** — se sumiram, você gravou outra
   coisa;
2. **o `ping` sai sozinho, com `"sub":"weight"`** — é por ele que o adapter vai
   identificar esta porta, e é a placa que sempre inicia;
3. **o stream `peso` sai a 5 Hz**, e `canais_g` tem `null` no canal inativo;
4. **o `ts` está em 1970** — está certo. A placa não tem relógio; ela só acerta
   a hora com o `epoch` que vem no pong do adapter, e aqui não há adapter
   nenhum. Hora visivelmente errada é melhor que hora plausível e errada.

Digite `?` e depois `C`: a ajuda humana sai como sempre, e o `C` agora traz uma
linha `{"evento":{"tipo":"cfg",...}}` junto.

> Se o stream atrapalhar a leitura enquanto você calibra, desligue com `j` e
> religue com `j` depois. Ele não tem nada a ver com o `a` (auto-print): um é a
> voz da máquina, o outro a do humano.

### 1.4 Calibrar (se ainda não estiver)

Nada aqui mudou em relação à 2.2 — é o procedimento que você já conhece:

- `m` — mapa dos canais, confira quais estão ativos;
- `c0`, `c1`, `c2` — calibração canal a canal, com o peso padrão;
- `t` — tara dos canais, com a mesa vazia;
- `p` — imprime os canais e o total.

**Confira a calibração antes de seguir:** ponha o peso padrão na mesa e veja se
o `total_g` do stream bate. Uma calibração torta faz *tudo* no ensaio 3 divergir,
e o log vai apontar para o Triple Check em vez de apontar para cá.

### 1.5 Fechar o monitor

**Feche o Monitor Serial antes do ensaio 2.** No Windows a COM é exclusiva de um
processo: com o monitor aberto, o adapter toma `ACCESS_DENIED` e fica em laço de
reconexão. O sintoma é `"conectado": false` no `/health` sem nenhum erro óbvio.

---

## Ensaio 2 — placa + adapter, sem Docker

É o ensaio que prova o transporte, e o único que dá para ler inteiro sem subir
mais nada. Duas janelas de terminal.

### 2.1 Subir o adapter (janela 1)

Uma vez só, para criar o ambiente:

```bash
cd weight-adapter; python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Chamar o `python.exe` do venv direto, em vez de ativar, não é atalho: o
`Activate.ps1` esbarra na ExecutionPolicy do Windows em máquina recém-instalada,
e o erro que aparece não fala em política nenhuma.

Daí em diante, sempre:

```bash
.\iniciar_host.bat
```

**A COM está na primeira linha editável do `.bat`** — é lá que se troca, não
aqui. Ele define `WEIGHT_TRANSPORTE`, o baud, o prazo do ACK e o `CENTRAL_URL`,
e usa o `python.exe` do venv sem ativar nada.

No log tem que aparecer, em segundos:

```
[STARTUP] httpx.AsyncClient criado | transporte=serial
[weight] porta tomada: COM7 @ 115200 baud (dono único deste processo)
```

O central não está de pé, e **isso é esperado**: o adapter loga a falha de
encaminhamento e segue. Ele não depende do central para abrir a porta.

### 2.2 Conferir a porta (janela 2)

```bash
Invoke-RestMethod http://localhost:8103/health | ConvertTo-Json -Depth 5
```

O que interessa em `serial`: `"conectado": true`, `"url_aberta": "COM7"`, e
`ultimo_ping_placa` com hora recente. Se `conectado` for `false`, na esmagadora
maioria das vezes é o Monitor Serial ainda aberto (item 1.5).

```bash
Invoke-RestMethod http://localhost:8103/balanca | ConvertTo-Json -Depth 5
```

Aqui você vê a placa por dentro: `boot`, `cfg`, e o `peso` ao vivo. Repita o
comando com a mão na mesa e o `total_g` muda — é a prova de ponta a ponta de
que o dado sai do HX711 e chega ao HTTP.

**`tara_confiavel` provavelmente virá `false`**, e está certo: o adapter viu um
`boot`, e todo boot que chega até ele é um boot que ninguém pediu — abrir a
porta não reinicia a placa (DTR/RTS saem desligados). Ele não bloqueia nada;
um `POST /comandos/tara` aceito devolve a confiança.

### 2.3 Medir quanto pesa UMA caixa

Este número é o que o ensaio 3 vai precisar. Mesa vazia:

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8103/bancada/tara-canais
```

Ponha **uma** caixa e leia:

```bash
(Invoke-RestMethod http://localhost:8103/balanca).peso
```

Anote o `total_g` com a leitura `estavel: true`. Chame esse número de **P**.

### 2.4 Contar por peso — o teste que fecha o ensaio

Mesa vazia, ponha o **recipiente vazio**:

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8103/bancada/tara-recipiente
```

Informe o peso de uma caixa (troque `45.0` pelo seu **P**):

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8103/bancada/peso-unitario -ContentType application/json -Body '{"valor_g": 45.0}'
```

Ponha **N caixas** no recipiente (3, 4, 5 — o que você tiver), espere parar, e:

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8103/bancada/contar
```

```bash
(Invoke-RestMethod http://localhost:8103/balanca).contagem
```

**O `contagem` tem que ser exatamente N, com `status: "OK"` e `aceite: true`.**

Se der N±1, o problema é calibração ou tolerância, não integração — volte ao
1.4. Se der `NO_UNIT_WEIGHT`, o `peso_unitario` não foi gravado; se der
`INVALID_WEIGHT`, a tara do recipiente ficou acima do peso medido.

Passou aqui, o transporte está provado: comando saiu do HTTP, virou linha JSON
na serial, a placa deu ACK, executou, e o evento voltou. Todo o resto é
orquestração.

---

## Ensaio 3 — a balança dentro da planta

### O estado configurado hoje

O `.env` está montado para **integração incremental**: balança real, dispenser
e CNC **desligados** — nem reais, nem simulados —, visão simulada em container.

```
COMPOSE_PROFILES ausente   → sobem 7 serviços, nenhum adapter serial
WEIGHT_ADAPTER_URL         → host.docker.internal:8103  (o adapter do host)
INTERVALO_OS=86400         → o gerador fica calado
```

O gerador está calado de propósito: sem o dispenser-adapter, **toda OS
automática aborta** na etapa de carregamento (`erro_envio_carregamento`), e a
cada 90 s isso encheria `ordens`, o histórico e o badge de alarmes com falhas
que ninguém provocou. O disparo manual pelo console continua normal.

### 3.1 A unidade é a CAIXA, e o catálogo precisa concordar

O `peso_unitario_g` de cada medicamento sai da tabela `medicamentos`, populada
por `_PESO_POR_DIMENSAO` a partir da dimensão da **caixa** — 15 g a 120 g,
default 50 g. Não são comprimidos: a unidade que esta planta move é a caixa.

É por isso que o **P** do item 2.3 importa. Se o catálogo diz 45 g e a caixa
real pesa 38 g, o desvio é de 15% contra uma tolerância de 5%, e **todo slot
diverge** — inclusive com as caixas fisicamente na mesa. Acerte o catálogo para
os medicamentos que você vai usar:

```bash
docker compose exec mysql mysql -uapsen -p apsen_db -e "SELECT nome, peso_unitario_g FROM medicamentos LIMIT 10;"
```

```bash
docker compose exec mysql mysql -uapsen -p apsen_db -e "UPDATE medicamentos SET peso_unitario_g=45.0 WHERE nome='ALOIS 10MG';"
```

### 3.2 Subir a stack no estado novo

A máquina provavelmente ainda tem os 13 serviços de pé, do jeito antigo. O
`down` derruba todos e o `up` sobe só os 7 que a configuração nova pede — o
volume do MySQL **não** é afetado, nenhum dado se perde:

```bash
docker compose down; docker compose up -d
```

> `;` e não `&&`: o Windows PowerShell 5.1 não tem os operadores de cadeia
> `&&`/`||` — eles são erro de PARSER, não comando que falha. Se você usa o
> PowerShell 7, os dois funcionam.

```bash
docker compose ps --format "table {{.Service}}\t{{.Status}}"
```

Devem aparecer **sete**: `mysql`, `central-computer`, `vision-adapter`,
`vision-simulator`, `erp-simulator`, `dashboard`, `manut_web`. Nenhum
`weight-adapter` — a porta 8103 fica livre para o processo do host.

> O `down` é necessário e não é zelo: o `WEIGHT_ADAPTER_URL` mudou, e container
> já criado não relê variável de ambiente. Sem recriar o central, ele continua
> mandando `pesar` para o endereço antigo.

### 3.3 Subir o adapter do host

```bash
weight-adapter\iniciar_host.bat
```

A COM está na primeira linha editável do `.bat` — troque lá, não aqui. O
central agora está de pé, então o encaminhamento de evento deve funcionar: no
log do adapter, `[EVT] ...` sem o aviso de falha que aparecia no ensaio 2.

### 3.4 Conferir

```bash
Invoke-RestMethod http://localhost:8103/health | ConvertTo-Json -Depth 5
```

`serial.conectado: true`, e em `checks` o `central-computer: ok`.

O pré-voo (<http://localhost:8000/console/prevoo>) vai mostrar **vermelho** em
`dispenser-adapter`, `cnc-adapter`, `dispenser-simulator`, `cnc-simulator` e
`weight-simulator`. **É o esperado nesta fase** — eles não estão no ar por
decisão, não por falha. A sonda de `:8103` é a que importa, e ela agora bate no
processo do host.

### 3.5 O que dá para testar AGORA, e o que não dá

Com dispenser e CNC fora, uma OS **não chega à pesagem**: ela aborta na etapa de
carregamento, muito antes do `pesar`. Ou seja, o caminho completo
`console → orquestrador → balança` só fecha quando as outras duas placas
entrarem.

Duas formas de exercitar a balança enquanto isso:

**a) Comando direto no adapter** — pula o orquestrador, e é o que prova que o
contrato de `pesar` funciona ponta a ponta com massa de verdade. Ponha uma caixa
na mesa e:

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8103/comandos/tara -ContentType application/json -Body '{"os_id": "ENSAIO-1"}'
```

```bash
Invoke-RestMethod -Method Post -Uri http://localhost:8103/comandos/pesar -ContentType application/json -Body '{"os_id":"ENSAIO-1","slot_id":1,"quantidade_esperada":1,"quantidade_real":1,"peso_unitario_g":45.0}'
```

A resposta do POST é só o **ACK** — o resultado chega depois, como evento. Veja
no log do adapter:

```
[EVT] peso_ok               ← slot=1 | OS ENSAIO-1
```

Ponha **duas** caixas e repita com `quantidade_esperada: 1`: tem que sair
`peso_divergencia`. Essa é a prova de que a terceira fonte do Triple Check
passou a medir massa real.

**b) Ligar só dispenser e CNC simulados**, para ver a OS inteira andar com a
balança verdadeira no meio. Serviços atrás de um profile sobem quando nomeados
explicitamente, sem mexer no `.env`:

```bash
docker compose up -d dispenser-adapter cnc-adapter dispenser-simulator cnc-simulator
```

Aí o `.env` precisa das duas linhas que estão comentadas? **Não** — os dois
adapters voltam a ser containers, e o default do compose já aponta para eles. As
linhas `host.docker.internal` só entram no dia em que essas placas forem reais.

Para desligar de novo:

```bash
docker compose stop dispenser-adapter cnc-adapter dispenser-simulator cnc-simulator
```

Neste modo, lembre do topo do ensaio: os dispensers são fictícios e não põem
nada na mesa. Ou você enche à mão entre o `dispensar` e o `pesar`
(`FATOR_VELOCIDADE=3.0` dá tempo), ou a OS trava no primeiro slot — que é a
balança funcionando, não falhando.

### 3.6 Quando a CNC e o dispenser entrarem

Três edições, e nada além disso:

1. no `.env`, descomente `DISPENSER_ADAPTER_URL` e `CNC_ADAPTER_URL`;
2. copie o `iniciar_host.bat` para cada adapter, trocando o prefixo das
   variáveis (`DISPENSER_`/`CNC_`), a COM e a porta (8100 / 8101);
3. devolva `INTERVALO_OS` para `90` quando quiser o gerador de volta.

O `COMPOSE_PROFILES` continua ausente: com as três placas reais, nenhum
simulador precisa subir.

## Os três erros mais prováveis

| sintoma | causa quase certa |
|---|---|
| `/health` com `"conectado": false` | Monitor Serial aberto (a COM é exclusiva), ou COM errada na variável |
| a placa reinicia e a tara sai errada | algum outro programa abriu a porta — só o adapter desliga DTR/RTS antes |
| **todo** slot diverge no ensaio 3 | ou ninguém pôs massa na mesa (esperado — leia o topo do ensaio 3), ou o `peso_unitario_g` do catálogo não é o da caixa real (item 3.1) |

Um quarto, mais raro e mais confuso: se o adapter do host for reiniciado com a
placa ligada, o contador de `cmd_id` dele volta a 1. O firmware detecta isso
pelo silêncio de pongs e zera o dele — a linha
`[APSEN] adapter voltou — contador de cmd_id zerado.` no Monitor Serial é a
confirmação. Sem ela, o primeiro comando depois do restart seria confirmado sem
ser executado.

---

## Como voltar atrás

Para devolver a planta ao estado 100% simulado: pare o adapter do host,
comente `WEIGHT_ADAPTER_URL`, devolva `INTERVALO_OS` para `90` e ponha
`COMPOSE_PROFILES=simulado` no `.env`. Depois:

```bash
docker compose down; docker compose up -d
```

Voltam os 13 serviços e o `weight-simulator`. Nada no banco, no dashboard ou no
histórico guarda resíduo do ensaio — e a `balanca2.2.ino` continua no
repositório se você quiser regravar a versão sem a voz de máquina.

O manual da placa, com a tabela completa de eventos e comandos, é
[`README.md`](README.md); o contrato do protocolo é
[`../docs/PROTOCOLO_SERIAL.md`](../docs/PROTOCOLO_SERIAL.md) §5.
