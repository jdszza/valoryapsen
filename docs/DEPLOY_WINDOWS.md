# Deploy no mini PC Windows da célula

> Como a célula montada sobe no mini PC HP com Windows: o que roda em container,
> o que roda no host, qual COM é de quem, as variáveis de cada processo, como
> subir no boot e as armadilhas que já custaram horas. O contrato de cada porta
> está em [PROTOCOLO_SERIAL.md](PROTOCOLO_SERIAL.md); o painel de bancada, em
> [BANCADA.md](BANCADA.md); o resumo e o porquê, no README, seção
> "Portas seriais na célula montada".

> **Antes de ligar as cinco placas pela primeira vez: no mini PC, toda placa
> tem que ter o número da COM fixado no Windows e a variável de porta
> preenchida. A varredura automática é só para desenvolvimento sem hardware.**
> O porquê está na seção [Por que a porta fixa é obrigatória](#por-que-a-porta-fixa-é-obrigatória).

## O desenho

```
mini PC HP (Windows) ─┬─ COM_A  dispenser      (os 8 mecanismos)     ┐
                      ├─ COM_B  dispenser_tft  (as 8 telas TFT)      │ dispenser-adapter  (host)
                      ├─ COM_C  cnc            (a mesa)                 cnc-adapter        (host)
                      ├─ COM_D  weight         (a balança HX711)        weight-adapter     (host)
                      └─ COM_E  display de 7"  (a bancada)              painel_operador    (host)

câmeras: HTTP/Ethernet (vision-adapter, sem serial — em container)
alimentação: fonte externa. O USB do mini PC carrega SÓ dados.
```

Cinco portas, cinco donos, **um processo por porta**. Nenhum processo varre.

## O que roda onde

O Docker Desktop do Windows **não repassa porta COM para container**. Quem
precisa de uma COM roda fora do Docker — é a mesma decisão que o painel de
bancada já tinha, pelo mesmo motivo (BANCADA.md: *"Ele é dono de uma porta
serial USB, e um container Linux não enxerga a COM do host — isso é decisão,
não pendência"*).

| Processo | Onde | Por quê |
|---|---|---|
| `mysql`, `central-computer`, `dashboard`, `manut_web`, `erp-simulator` | container | não tocam em porta nenhuma |
| `vision-adapter`, `vision-simulator` | container | a visão é HTTP/Ethernet; o simulador serve a bancada até a estação de câmeras existir |
| `dispenser-adapter` (duas COM: mecanismos + telas TFT) | **host** | dono de COM_A e COM_B |
| `cnc-adapter` | **host** | dono de COM_C |
| `weight-adapter` | **host** | dono de COM_D |
| `painel_operador` (backend + display) | **host** | dono de COM_E, como já era |

No compose isso é o **profile** `simulado`: os três adapters seriais e os quatro
simuladores estão atrás dele. Com `COMPOSE_PROFILES=simulado` no `.env` (a
máquina de desenvolvimento e a demonstração), `docker compose up` sobe a planta
simulada inteira, como sempre. **No mini PC, deixe `COMPOSE_PROFILES` fora do
`.env`** (ou vazio): sobe só o que roda em container.

```bash
docker compose up -d --build        # no mini PC: 7 serviços, os de container
```

### A alternativa preterida: ponte RFC2217

Manter tudo em container e expor cada COM como socket TCP por uma ponte RFC2217
no host (`<SUB>_SERIAL_URL=rfc2217://host.docker.internal:400x`) foi avaliado
e descartado. O custo some do código — de dentro do adapter as duas montagens
são a mesma chamada — e é por isso que precisa ficar escrito:

1. **mais um processo por porta para supervisionar** — três pontes, cada uma
   podendo morrer sozinha, e nenhuma delas é serviço do compose;
2. **latência a mais** em todo comando e todo evento, num canal que já concorre
   com telemetria;
3. **"cabo caiu" e "ponte morreu" produzem o mesmo sintoma** — os dois viram
   "socket fechado", e quem lê o log não sabe se vai olhar o cabo ou reiniciar
   um processo do host;
4. **a perda da exclusividade de abertura.** No Windows a COM é exclusiva de um
   processo — o segundo a tentar toma `ACCESS_DENIED`, que é o erro certo. Dois
   processos abrem o mesmo socket TCP sem erro nenhum, e o resultado é o que o
   item 1 do contrato serial existe para impedir: duas partes escrevendo na
   mesma linha, bytes intercalados no meio de um JSON, e o outro lado
   descartando a linha inteira em silêncio.

## A tabela de COM por placa

Preencha ao montar a célula e **fixe os números no Windows** (ver abaixo).
Sem isto, a próxima troca de porta USB renumera tudo.

| Placa | Subsistema (`sub` do ping) | COM fixada | Variável (processo do host) |
|---|---|---|---|
| mecanismos dos 8 dispensers | `dispenser` | COM_A = `COM__` | `DISPENSER_SERIAL_URL` (dispenser-adapter) |
| as 8 telas TFT | `dispenser_tft` | COM_B = `COM__` | `DISPENSER_TFT_SERIAL_URL` (dispenser-adapter) |
| mesa CNC | `cnc` | COM_C = `COM__` | `CNC_SERIAL_URL` (cnc-adapter) |
| balança HX711 | `weight` | COM_D = `COM__` | `WEIGHT_SERIAL_URL` (weight-adapter) |
| display de 7" | (ping sem `sub`) | COM_E = `COM__` | `APSEN_DISPLAY_PORTA` (painel_operador) |

Qual placa está em qual COM: abra **uma** porta de cada vez com o monitor
serial do PlatformIO a 115200 e leia o ping — `{"cmd":"ping","sub":"cnc"}` é
a mesa; o display manda `{"cmd":"ping"}` sem `sub`. **Feche o monitor antes de
subir os processos** (ver Armadilhas).

### Fixar a COM no Windows

Gerenciador de Dispositivos → *Portas (COM e LPT)* → botão direito na porta →
*Propriedades* → aba *Configurações de Porta* → *Avançado…* → *Número da Porta
COM*. Escolha um número, e o mesmo conversor USB-serial mantém o número mesmo
trocando de tomada USB (o Windows amarra o número ao par VID/PID + serial do
conversor).

Duas armadilhas conhecidas dessa tela:

- **Entradas fantasma.** O Windows deixa registradas as portas de dispositivos
  já usados (aparecem como "em uso" na lista de *Número da Porta COM*). Para
  ver e apagar: no Gerenciador de Dispositivos, *Exibir → Mostrar dispositivos
  ocultos*; ou `set devmgr_show_nonpresent_devices=1` antes de abri-lo.
- **Conversores idênticos.** Placas com o mesmo chip (CH340, CP2102) têm o
  mesmo VID/PID; é por isso que a detecção **nunca** casa por VID/PID e sim
  pelo `sub` do ping — e é por isso que o número fixado tem que ser conferido
  contra o ping uma vez, na montagem.

## Por que a porta fixa é obrigatória

Com `<SUB>_SERIAL_URL` vazia, o `serial_link._conectar` de cada adapter **varre
todas as portas** procurando a sua placa, e em cada candidata gasta
`PROBE_ASSENTAR_S` (1,5 s) + `PROBE_ESPERA_S` (8 s) = **até 9,5 s**. O painel
faz o mesmo em `find_display_port` / `_probe_port`, e o `serial_worker` repete
a varredura a cada 3 s, para sempre.

A competição não é por uma porta compartilhada — cada placa tem a sua. A
competição existe porque, **durante a busca, cada processo abre as portas dos
outros** para descobrir se a placa dele está ali. Enquanto o dispenser-adapter
segura por 9,5 s a COM da CNC só para conferir, o cnc-adapter toma
`ACCESS_DENIED` na própria porta. Com cinco processos e cinco portas isso não dá
erro: dá **boot não-determinístico de dezenas de segundos**, em que uma placa
às vezes simplesmente não é achada no ciclo.

A conta: 5 processos × até 9,5 s por porta sondada × até 5 portas cada = a
janela em que qualquer um deles pode estar segurando a porta de outro. Fixar a
porta elimina a varredura, e sem varredura não há competição.

O que NÃO muda com a porta fixa: a abertura continua com **DTR/RTS desligados**
antes de abrir (nessa placa esses sinais são o circuito de reset do ESP32-S3) e
a confirmação continua sendo o **ping** — fixar o número não dispensa conferir
que há a placa certa do outro lado.

## As variáveis de cada processo do host

Os adapters no host **não leem o `.env` da raiz** — ele é do compose. Cada um
recebe o ambiente do `.bat` (ou da tarefa agendada) que o inicia.

### Comum aos três adapters

| Variável | Valor no mini PC | Por quê |
|---|---|---|
| `CENTRAL_URL` | `http://localhost:8000` | a porta **publicada** do central. `central-computer:8000` é nome DNS da rede Docker e não resolve no host (a mesma armadilha do `BACKEND_URL`) |
| `<SUB>_TRANSPORTE` | `serial` | o firmware, não o simulador |
| `<SUB>_SERIAL_URL` | `COMx` (a fixada) | **obrigatória** na célula montada |
| `<SUB>_SERIAL_BAUD` | `115200` | |
| `<SUB>_ACK_TIMEOUT_S` | `2` | prazo do ACK, não da conclusão |

`<SUB>` é `DISPENSER`, `CNC` ou `WEIGHT`; o dispenser-adapter leva também
`DISPENSER_TFT_TRANSPORTE=serial` e `DISPENSER_TFT_SERIAL_URL=COM_B`.

E o **central alcança os adapters do host** por `host.docker.internal`, o nome
que o Docker Desktop dá à máquina hospedeira. No `.env` do mini PC:

```bash
DISPENSER_ADAPTER_URL=http://host.docker.internal:8100
CNC_ADAPTER_URL=http://host.docker.internal:8101
WEIGHT_ADAPTER_URL=http://host.docker.internal:8103
# VISION_ADAPTER_URL fica no default: o vision-adapter continua em container.
```

O `central-computer` lê essas três variáveis do ambiente (`config.py`); o compose
as declara com os nomes DNS de sempre como default, e o `.env` as sobrescreve.

### Como subir cada adapter no host

```bat
cd C:\apsen\cnc-adapter
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set CENTRAL_URL=http://localhost:8000
set CNC_TRANSPORTE=serial
set CNC_SERIAL_URL=COM5
python -m uvicorn main:app --host 0.0.0.0 --port 8101
```

O mesmo para `dispenser-adapter` (porta 8100, com as duas `SERIAL_URL`) e
`weight-adapter` (porta 8103). As portas HTTP são as mesmas que o compose
publicava: nada muda para o central, o dashboard ou o pré-voo.

### Consequências no compose

- os três adapters seriais e os quatro simuladores estão atrás do profile
  `simulado` — na célula montada, **não sobem** em container;
- o `erp-simulator` declara a dependência deles com `required: false`: com o
  profile ligado ela vale como sempre; sem o profile, o compose só avisa e sobe
  o ERP. Quem garante que os adapters do host já estão de pé quando a primeira
  OS sai é a ordem de boot do host (abaixo);
- o `/console/prevoo` continua sondando `:8100`, `:8101` e `:8103` — no host,
  agora —, e é a tela que diz se os cinco processos subiram.

## Subir no boot

Cinco processos precisam subir sozinhos, e **antes** de o compose emitir a
primeira OS. Na ordem:

1. Docker Desktop (inicia com o Windows; marque *Start Docker Desktop when you
   sign in*) e `docker compose up -d` — os containers com `restart:
   unless-stopped` voltam sozinhos;
2. os três adapters do host;
3. o painel de bancada (`painel_operador\iniciar_backend.bat`).

Use o **Agendador de Tarefas** (uma tarefa por processo, gatilho *Ao iniciar a
sessão*, *Executar somente quando o usuário estiver conectado*, com o `.bat` de
cada um e o diretório de trabalho certo), ou um único `.bat` na pasta *Inicializar*
que chame os quatro em janelas separadas (`start "cnc-adapter" cmd /k ...`).
Janela separada é melhor que serviço do Windows aqui: o log de cada porta fica
visível, e "fechou a janela" é um sintoma que qualquer um da bancada lê.

`restart: unless-stopped` não existe para processo do host: um adapter que morra
**não volta sozinho**. Enquanto não houver supervisor de processo, o sintoma no
pré-voo é `:810x` vermelho — e o `.bat` com `cmd /k` deixa o traceback na tela.

## Armadilhas conhecidas

**Só um processo pode abrir a porta — e agora são cinco portas.** O Serial
Monitor do PlatformIO (ou do Arduino IDE, ou um `simulador_serial.py`
esquecido) aberto em qualquer uma delas derruba quem deveria estar com ela, e o
sintoma é só "desconectado" no `/health` do adapter (ou `OFFLINE` no display).
Feche todos antes de subir os processos; para regravar um firmware, derrube o
processo dono daquela COM primeiro.

**`ACCESS_DENIED` numa porta que "está livre".** Quase sempre é processo
duplicado: fechar a janela nem sempre mata o Python. `tasklist | findstr python`
antes de procurar bug.

**O número da COM mudou sozinho.** Ou a porta não foi fixada, ou a placa foi
ligada por outro conversor. Confira o ping com o monitor serial e fixe de novo.

**`CENTRAL_URL=http://central-computer:8000` no host.** Não resolve: é nome da
rede Docker. No host é `http://localhost:8000`. O mesmo vale ao contrário: o
central em container só alcança o host por `host.docker.internal`.

**`APSEN_RELOADER=1` com hardware.** O reloader do Flask roda dois processos, e
os dois disputam COM_E. Nunca na bancada.

**`COMPOSE_PROFILES=simulado` no `.env` do mini PC.** Sobe os simuladores e os
adapters em container por cima dos do host: o central passa a mandar comando
para `dispenser-adapter:8100` (o container, que fala com o simulador) e a
célula física fica parada com tudo "verde" no dashboard. No mini PC a variável
fica de fora.
