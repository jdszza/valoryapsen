# Painel de bancada e display de 7"

> Referência completa do `painel_operador/` — a metade do repositório que roda
> FORA do Docker, na máquina da bancada, e que é dona da porta serial do
> display. O [README](../README.md) traz só o resumo e quando usá-lo.
>
> As decisões por trás do que está aqui (por que o espelho é de mão única, por
> que o slot vem do `dispenser_id`, por que o PIN virou hash) estão no
> `CLAUDE.md`.


O painel que o **operador de chão de fábrica** usa: uma tela de 7" com touch na
bancada, ao lado da célula, mais uma interface web para quem prefere o
navegador. Ele lista as ordens de expedição, mostra o estoque de cada dispenser,
autentica por PIN ou crachá RFID, registra histórico e abre desvios.

```
   computador central  ──── GET (HTTP) ───►  BACKEND  ──── USB serial ───►  DISPLAY
   (célula, no Docker)      espelho          Flask +                        ESP32 7"
                            de mão única     SQLite
```

**O central manda, o painel espelha.** As ordens que a célula executa e o
estoque que ela mede chegam por `GET` e nunca voltam: o painel não comanda a
célula. O porquê está no `CLAUDE.md`, seção "O painel de bancada espelha o
central, e o espelho é de mão única".

| Pasta | O que é |
|---|---|
| `backend/` | Flask + SQLite. Interface web, API e ponte serial com o display. |
| `firmware/` | Projeto PlatformIO do display ESP32-8048S070C (7", 800×480, touch). |

## Rodar o painel

**Na máquina Windows da bancada, fora do Docker.** Ele é dono de uma porta
serial USB, e um container Linux não enxerga a COM do host — não existe serviço
dele no `docker-compose.yml` nem `Dockerfile`, e isso é decisão, não pendência.

```bash
cd painel_operador/backend
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
set APSEN_SECRET=<64 hex>          # obrigatorio — ver "Segredos do painel"
set APSEN_API_TOKEN=<64 hex>       # sem ele, as rotas /api/* respondem 503
python app.py
```

`python app.py` é o **único** entrypoint, e ele escolhe o servidor sozinho:
**waitress** quando instalado (é o padrão do `requirements.txt`), o servidor de
desenvolvimento do Flask como fallback. Nunca suba com `waitress-serve app:app`
— isso importa o módulo sem passar por `iniciar_workers()`, e o painel sobe com
a web de pé e **sem a ponte serial**: display OFFLINE, espelho do central
parado, e nada no log dizendo por quê.

Interface em <http://127.0.0.1:5000>. Ou, do Explorer, `iniciar_backend.bat`.
Login inicial: **Administrador / 1234** (seed) — troque em *Admin → Operadores*
antes de qualquer uso real. Perfil `Operador` não entra na web: ele opera pelo
display.

Para ver as ordens da célula no painel, suba o central antes — e **confira que
o espelho está mesmo espelhando**: ver
[Testar o painel junto com a célula](#testar-o-painel-junto-com-a-célula), logo
abaixo. Sem o central o painel funciona igual, só que 100% local.

## Testar o painel JUNTO com a célula

São **dois processos independentes**, e só um deles está no Docker:

```
  docker compose (13 serviços)              fora do Docker, no host
  ┌──────────────────────────┐              ┌─────────────────────────┐
  │ central-computer  :8000  │◀──── GET ────│ painel_operador  :5000  │
  │  /os/historico           │   somente    │  Flask + SQLite         │
  │  /os/{os_id}             │   leitura    │  espelho a cada 5 s     │
  │  /dispensers/estado      │              └───────────┬─────────────┘
  └──────────────────────────┘                          │ USB serial
                                                  display de 7"
```

O painel **lê** e nunca escreve na célula: `central_client.py` só tem `GET`, e
há teste que falha se um `requests.post/put` aparecer lá. Um espelho que escreve
é um espelho que mente — ver a seção de decisões no `CLAUDE.md`.

### 1. Suba a célula

```bash
docker compose --profile simulado up -d --build   # na raiz; ver "Build e deploy"
```

Confira em <http://localhost:8000/console/prevoo> antes de seguir.

### 2. Suba o painel apontando para ela

O painel **não lê o `.env` da raiz** — ele é um processo do host, e as variáveis
precisam estar no ambiente dele.

```bash
cd painel_operador/backend
python -m venv .venv
.venv\Scripts\activate                # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

```bash
# Windows (cmd)                        # Linux/macOS
set APSEN_SECRET=<64 hex>              export APSEN_SECRET=<64 hex>
set APSEN_API_TOKEN=<64 hex>           export APSEN_API_TOKEN=<64 hex>
set CENTRAL_URL=http://localhost:8000  export CENTRAL_URL=http://localhost:8000
set PAINEL_CENTRAL=1                   export PAINEL_CENTRAL=1
python app.py
```

> **`CENTRAL_URL` é `localhost:8000`, não `central-computer:8000`.** O segundo é
> nome DNS da rede `apsen-net` e só resolve DENTRO dos containers; o painel roda
> fora. É a mesma armadilha do `BACKEND_URL` do app de manutenção.

### 3. Conferir que o espelho está mesmo espelhando

**Esta é a parte que não dá para pular.** `central_client` **nunca levanta** — por
decisão: timeout, conexão recusada e JSON inválido viram log e retorno vazio,
porque uma exceção de rede aqui derrubaria a thread que serve a tela que o
operador está olhando. O efeito colateral é que a integração falha **calada**:
"não apareceu ordem nenhuma" e "o `PAINEL_CENTRAL` está em 0" produzem a mesma
tela.

Quatro verificações, em ordem de valor:

```bash
# 1) as ordens da célula chegaram, com origem='central'
curl -s -H "X-API-Token: $APSEN_API_TOKEN" http://127.0.0.1:5000/api/resumo
#    → ordens_ativas[].origem == "central" e os_id_central preenchido
```

```bash
# 2) o estoque dos slots casa com o da célula, slot a slot
curl -s http://localhost:8000/dispensers/estado      # dispenser_id + quantidade_atual
curl -s -H "X-API-Token: $APSEN_API_TOKEN" http://127.0.0.1:5000/api/dispensers
#    → os 8 slots, mesma quantidade dos dois lados
```

```bash
# 3) ordem espelhada é SÓ-LEITURA (409, não 200)
curl -s -X PUT -H "X-API-Token: $APSEN_API_TOKEN" -H "Content-Type: application/json" \
     -d '{"status":"Concluido"}' http://127.0.0.1:5000/api/ordens/<id-espelhado>/status
#    → {"ok":false,"erro":"ordem do central"}
```

```bash
# 4) o vocabulário de status é fechado (409, não 302)
curl -s -X PUT -H "X-API-Token: $APSEN_API_TOKEN" -H "Content-Type: application/json" \
     -d '{"status":"Qualquer"}' http://127.0.0.1:5000/api/ordens/1/status
#    → {"ok":false,"erro":"status invalido"}
```

Na web, o sinal rápido é a lista em <http://127.0.0.1:5000/ordens>: ordem da
célula aparece com o rótulo `CENTRAL` e **sem** os botões de Iniciar/Pausar —
botão que o backend vai recusar não é botão desabilitado, é botão ausente.

### 4. Testar o painel SEM a célula

É um modo suportado, não um acidente: a bancada precisa funcionar em feira, em
treinamento e no dia em que o Docker não sobe.

```bash
set PAINEL_CENTRAL=0        # export PAINEL_CENTRAL=0
```

Desliga a integração inteira — nada sai pela rede, as telas voltam ao cadastro
local e `/ordens/nova` cria ordens `origem='local'`, com baixa FEFO. Vale testar
os dois modos: as duas metades (leitura espelhada e bloqueio de escrita) têm que
cair juntas, senão o painel ficaria sem poder ler e sem poder escrever ao mesmo
tempo.

### 5. E o display?

O display fala por **USB serial** e o backend auto-detecta a porta. Sem hardware
ligado o painel sobe igual e mostra o display como `OFFLINE` — web, espelho e
API funcionam sem ele. Para gravar e testar a placa de verdade, ver
[Gravar e testar o display físico](#gravar-e-testar-o-display-físico-esp32-s3),
logo abaixo.

Sem placa, quem exercita o protocolo é o simulador:

```bash
python painel_operador/firmware/simulador_serial.py --listar   # portas
python painel_operador/firmware/simulador_serial.py COM4
```

Ele faz **dois papéis**: empurra comandos de debug **e responde** aos pedidos que
o firmware faz sozinho. Sem a segunda metade o display fica OFFLINE e
`fetch_ordens_api` nunca roda — que é o caminho por onde o `os_id` longo do
central e o campo `origem` chegam.

> Precisa de **duas pontas**: o backend abre uma e o simulador precisa da outra.
> Com hardware, é o cabo. Sem hardware, é um par de COM virtuais (`com0com` no
> Windows, `socat -d -d pty,raw,echo=0 pty,raw,echo=0` no Linux/macOS).

---

# Gravar e testar o display físico (ESP32-S3)

Placa de referência: **ESP32-S3 HMI 7" 800×480**, 8 MB PSRAM / 16 MB Flash,
touch GT911, com **CH340** para USB/serial. Toolchain: **PlatformIO** no VS Code.

## O display NÃO fala com o computador central

Esta é a parte que muda o plano de teste:

```
  ESP32-S3 (7")  ──USB serial 115200──▶  painel_operador :5000  ──HTTP GET──▶  central :8000
     LVGL, sem rede         CH340              Flask + SQLite         docker compose
```

O firmware **não tem WiFi, Bluetooth nem MQTT** — as únicas menções no
`main.cpp` são comentários dizendo *"substitui WiFi/MQTT"*. O
`Serial.begin(115200)` do `display.h` é o **único canal** da placa.

Então **não configure rede na placa**. Quem busca os dados da célula é o
**backend**, e ele repassa pelo cabo: o display pede `get_ordens`, o backend
responde com o que espelhou do central.

Corolário que vale saber antes de depurar: **sem o backend rodando, o display
não tem ordem, catálogo nem estoque**. Ligar só a placa no PC não mostra nada.

## 1. Driver e porta

A placa usa um **CH340 externo**, não o USB nativo do ESP32-S3 — o
`platformio.ini` já força `ARDUINO_USB_MODE=0` por isso, porque os pinos do USB
nativo (GPIO19/20) estão ocupados pelo I2C do touch.

```bash
cd painel_operador/firmware
pio device list
```

Vazio? Instale o driver CH340 (WCH) e replugue.

## 2. Build e upload

O `platformio.ini` já está correto para essa placa (`esp32s3box`, PSRAM octal,
partição `huge_app`).

```bash
pio run                  # compila (a 1ª vez baixa LVGL, GFX, GT911, ArduinoJson)
pio run -t upload        # grava
```

No VS Code: ✓ *Build* e → *Upload* na barra do PlatformIO. Se o upload falhar,
segure **BOOT**, toque **RST**, solte BOOT e repita.

## 3. Ver o boot — e depois FECHAR o monitor

```bash
pio device monitor       # 115200, já fixado em monitor_speed
```

Esperado: `SD card OK` (ou `SD card FALHOU - continuando sem SD` — **o SD é
opcional**, só afeta os logos e o CSV local) e depois `{"cmd":"ping"}` repetindo.

**Esse ping é a chave da detecção**: quem inicia a conversa é o display; o
backend fica ouvindo as portas e responde `pong`. É assim que ele descobre qual
COM é o display, sem depender de VID/PID.

Agora **feche o monitor** (`Ctrl+C`) — ver o passo 5.

## 4. Suba a célula e o backend

```bash
docker compose --profile simulado up -d --build   # raiz; confira em /console/prevoo
```

```bash
cd painel_operador/backend && .venv\Scripts\activate
set APSEN_SECRET=<64 hex>
set APSEN_API_TOKEN=<64 hex>
set CENTRAL_URL=http://localhost:8000
set PAINEL_CENTRAL=1
python app.py
```

A linha que você quer no log: `Display encontrado na porta COM4`.

Plugou a placa depois? **Não precisa reiniciar** — o `serial_worker` reprocura a
cada 3 s.

## 5. ⚠️ A armadilha número 1: quem detém a COM

**Só um processo pode abrir a porta.** Com o *Serial Monitor* do PlatformIO
aberto, o backend **não acha o display** — e o sintoma é apenas "OFFLINE", sem
dizer por quê.

Antes de subir o backend, feche: o Serial Monitor do PlatformIO/VS Code, o
Monitor Serial do Arduino IDE, e qualquer `simulador_serial.py` rodando. E
**nunca use `APSEN_RELOADER=1` com hardware** — o reloader do Flask roda dois
processos, e os dois disputam a porta.

Detalhe que evita um susto: a sondagem abre a porta com **DTR/RTS desligados**
de propósito. Nessa placa esses dois sinais são o circuito de reset do ESP32 —
abrir do jeito padrão do pyserial reiniciaria o display a cada tentativa, num
ciclo em que ele nunca chegava a mandar o ping.

## 6. Verificar a corrente inteira

| na tela do display | significa |
|---|---|
| `OFFLINE` | o backend não pegou a porta — volte ao passo 5 |
| lista vazia | backend OK, mas o espelho do central não trouxe nada |
| ordens com rótulo **`CENTRAL`** | ✅ célula → backend → display |

Ordem vinda da célula aparece **sem** os botões Iniciar/Pausar/Concluir, só com
o rótulo `CENTRAL`. Não é bug: botão que o backend vai recusar não é botão
desabilitado, é botão ausente. Ordem criada em `/ordens/nova` (local) tem os
botões — é o contraste que prova que o espelho está sendo respeitado.

Para não esperar os 90 s do `erp-simulator`, dispare pelo
[console](http://localhost:8000/console).

## O protocolo, e o teste que o guarda

Uma linha JSON por mensagem, terminada em `\n`, 115200 baud:

| direção | mensagens |
|---|---|
| display → backend | 11 `cmd`: `ping`, `get_ordens`, `get_catalogo`, `get_operadores`, `get_dispensers`, `validar_operador`, `set_status`, `sync_dispensers`, `set_dispenser_med`, `get_trava`, `liberar_trava` |
| backend → display | 8 tags de `resp`: `pong`, `ordens`, `catalogo`, `operadores`, `dispensers`, `operador`, `ok`, `trava` |
| display → backend | 3 `event` (fire-and-forget): `historico`, `desvio`, `ordem_concluida` |
| backend → display | 3 `push` (não solicitados): `ordem_status`, `dispensers`, `trava` |

As mensagens da trava do Triple Check, por extenso:

```
display → backend   {"cmd":"get_trava"}
backend → display   {"resp":"trava","ativa":bool,"os_id":str,"slot_id":int|null,"motivo":str,"ts":int}
display → backend   {"cmd":"liberar_trava","nome":str,"pin":str}
backend → display   {"resp":"ok","ok":bool,"msg":str}
backend → display   {"push":"trava","ativa":bool,"os_id":str,"slot_id":int|null,"motivo":str}   ← só quando MUDA
```

As três decisões por trás desse contrato estão em
[A trava do Triple Check no display](#a-trava-do-triple-check-no-display).

Esse contrato é escrito **à mão em três lugares e duas linguagens** — firmware
(C++), backend e simulador (Python). Divergir não quebra nada visivelmente: o
display manda um `cmd` que ninguém trata e espera o timeout, ou o backend
responde uma tag que o display não aguarda e a linha é descartada em silêncio.
Os dois sintomas são "tela vazia, sem erro no log".

Por isso `tests/test_protocolo_serial.py` compara as três cópias e ainda checa,
em runtime, que o backend responde a cada comando — inclusive que **nenhuma
resposta passa dos 4096 bytes** do `s2_buf` do firmware, porque a linha que não
couber é descartada sem erro.

## Perfil Supervisor e a liberação da trava

O central para a fila inteira quando o Triple Check diverge, e só um humano a
solta. A decisão de operação foi tomada: **o supervisor libera a trava pelo
painel de bancada**, autenticado por PIN, tanto na web quanto no display de 7".

Do lado do painel:

| Perfil | Vê | Libera a trava? |
|---|---|---|
| `Admin` | tudo | **sim** |
| `Supervisor` | dashboard e ordens | **sim** |
| `PCP` / `PCM` | como antes | não |
| `Operador` | só o display | não |

A permissão é `trava_liberar`, em `PERMISSOES` no topo do `app.py`, e é
conferida **no servidor** (`POST /trava/liberar` exige a sessão de um perfil
que a tenha). Na web, o botão *Liberar trava* só aparece para quem pode —
botão que o backend vai recusar não é botão desabilitado, é botão ausente, a
mesma regra da ordem espelhada. O dashboard mostra a faixa vermelha com motivo,
OS e slot para todos os perfis que entram na web.

### Por que `central_comandos.py` existe separado

O espelho de ordens e estoque continua de **mão única**: `central_client.py`
só tem `GET`, e `tests/test_painel_ordens.py` reprova um `requests.post` lá
dentro. A liberação da trava é a **única** escrita que o painel faz no central
— deliberada, nomeada e num arquivo em que dá para ver todas as escritas de
uma vez: `backend/central_comandos.py`. Ele:

* autentica no central com uma **conta de serviço** (`PAINEL_CENTRAL_USER` /
  `PAINEL_CENTRAL_SENHA`, um usuário com role `supervisor` criado pelo app de
  manutenção), guarda o JWT (o central emite com 8 h) e refaz o login em 401;
* manda `em_nome_de` com o nome de quem digitou o PIN na bancada. Sem isso,
  toda liberação vinda daqui apareceria no log do central com o nome da conta
  de serviço, e o rastro de QUEM liberou — o ponto inteiro de existir uma
  trava — se perderia. O central grava `"<conta> (em nome de <nome>)"` em
  `log_manutencao`;
* **nunca levanta**, como o resto do painel: quem chama é a thread que serve a
  tela que o operador está olhando.

**Sem as duas variáveis, a liberação fica desligada com mensagem clara e o
painel sobe** — a mesma divisão do `APSEN_API_TOKEN`: uma variável ausente
derruba só o recurso, nunca a ponte serial. A leitura do estado da trava
(`GET /api/v1/trava`, público no central) fica em `central_client.py`, com os
outros GETs; só a escrita saiu de lá.

A thread do espelho lê a trava no **mesmo ciclo** das ordens e do estoque, a
cada `CENTRAL_SYNC_S`, e guarda o estado anterior: é a comparação entre os
dois que decide quando empurrar o push ao display.

## A trava do Triple Check no display

O contrato está na tabela do protocolo acima. As três decisões:

- **`liberar_trava` carrega `nome` + `pin`, em vez de usar o operador logado
  no display.** O operador logado é um Operador; o supervisor é outra pessoa,
  que chega à bancada, libera e vai embora — o mesmo desenho do
  `validar_operador`. O display lista, no popup de liberação, os operadores com
  perfil `Supervisor` ou `Admin` (a resposta de `get_operadores` traz
  `perfil`), e o PIN entra pelo mesmo numpad do login.
- **Quem confere o PIN é o backend, nunca o display** — igual ao
  `validar_operador` e pelo mesmo motivo (ver [PIN de operador](#pin-de-operador)):
  PIN de 4 dígitos são 10 mil candidatos, e hash só protege entrada que não dá
  para enumerar. A resposta nunca carrega o PIN. O pedido custa hash (~300 ms)
  + `POST` no central (`CENTRAL_TIMEOUT_S`, 3 s), então o firmware espera
  **5 s** nele — a mesma janela do `validar_operador`, não os 800 ms de sempre.
- **O push existe E o `get_trava` existe.** Push é uma linha serial, e linha
  serial se perde num reset do ESP32 ou numa reconexão da porta — o mesmo
  raciocínio de `fetch_ordens_api` refrescando ordem espelhada que já conhece.
  O push sai da thread do espelho **só na transição** (estado igual não gera
  push); `get_trava` cai num cache de 2 s (`TRAVA_CACHE_S`) pelo mesmo motivo
  do cache de dispensers: o pedido roda dentro da ponte serial, e central lento
  faria o display desistir antes de o backend ter a resposta.

No firmware, o motivo é guardado em `MAX_MOTIVO_LEN` (256), dimensionado pela
ORIGEM do dado: o orquestrador monta
`"Triple Check FALHOU (n/3 fontes divergentes, limiar=x) — Dk: "` mais até três
causas, e o pior caso real passa de 240 caracteres. O que não couber passa por
`copy_trunc()` e termina em `...` — truncar de propósito e deixar rastro, nunca
cortar em silêncio.

Na tela: a trava abre uma tela própria com OS, slot e motivo; um badge `TRAVA`
fica no cabeçalho enquanto ela durar (tocar nele reabre a tela); e o botão
*Liberar (supervisor)* abre o numpad pedindo nome + PIN. Sem placa, o
`simulador_serial.py` faz os dois papéis: responde `get_trava` e
`liberar_trava` (PIN `4321` da "Supervisora Teste") e empurra um push de trava
ativa, e depois liberada, no ciclo de comandos.

> **O firmware em campo precisa ser regravado de novo.** O display que está
> na bancada não conhece `get_trava`, `liberar_trava` nem o push `trava`: ele
> descarta os três em silêncio e continua mostrando a fila como se a célula
> estivesse rodando. Regrave (ver [Gravar e testar o display
> físico](#gravar-e-testar-o-display-físico-esp32-s3)) — a gravação acumula com
> as pendências anteriores (`os_id` longo e `validar_operador`).

## Segredos do painel

Duas variáveis, e elas falham de formas **deliberadamente diferentes**:

| Variável | Sem ela | Por quê |
|---|---|---|
| `APSEN_SECRET` | o processo **não sobe** | assina o cookie de sessão; sem ela tudo que o painel serve é forjável, e quem lê o repositório entra como Admin |
| `APSEN_API_TOKEN` | as rotas `/api/*` respondem **503**, o resto sobe | só o bloco `/api/*` fica sem dono; derrubar o processo levaria junto a ponte serial — a tela que o operador está olhando — por uma variável que a bancada talvez nem use |

É a mesma divisão do central: `SECRET_KEY` recusa o boot, `CONSOLE_SENHA`
ausente desliga o console e deixa a planta rodar. Nenhuma das duas tem valor
default — segredo versionado não é segredo.

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # gera qualquer uma
```

A regra vale para **qualquer** forma de subir o painel: `python app.py`, o
`iniciar_backend.bat` (que avisa antes, em vez de fechar a janela com um
traceback) e o `apsen.exe` do `desktop.py`, que importa o mesmo módulo.

`APSEN_ENV=dev` tolera uma `APSEN_SECRET` fraca (com aviso no console) para quem
só quer abrir o painel na própria máquina. Qualquer outro valor — inclusive
nenhum — trata segredo fraco como erro de configuração, e o processo para.

**As rotas `/api/*` exigem o header `X-API-Token`.** São as rotas que a estação
de visão usa, e por elas se cria ordem, se reescreve o estoque com baixa FEFO
real e se escreve no audit log; sem token, tudo isso estava aberto na rede da
fábrica. Token errado é `401`; variável ausente é `503` com a mensagem dizendo
qual variável definir.

```bash
curl -H "X-API-Token: $APSEN_API_TOKEN" http://localhost:5000/api/resumo
```

**Debug fica desligado.** `APSEN_DEBUG=1` liga o console do Werkzeug e, ligado,
o painel **só escuta em 127.0.0.1**: esse console executa Python arbitrário no
processo dono da porta serial e do banco da bancada, e depurar da própria
máquina é o único uso legítimo disso.

## PIN de operador

O PIN é gravado como **hash** (`werkzeug.security`, sem dependência nova — o
Werkzeug já vem com o Flask). A coluna `pin` deixou de existir; entrou
`pin_hash`, e um banco de versão anterior é convertido na primeira subida:
os PINs em claro viram hash e a coluna some, de uma vez só. Ninguém fica de
fora da bancada — o PIN de antes continua valendo.

**Quem confere o PIN do display é o BACKEND**, por `cmd: validar_operador`. A
resposta de `get_operadores` não carrega mais credencial nenhuma, e o
`/operadores.json` do cartão SD também não.

A alternativa — mandar o hash e deixar o display comparar — não resolvia nada
aqui: o PIN tem 4 dígitos, ou seja, 10 mil candidatos. Quem puser a mão no
cartão SD ou escutar o cabo USB tem todos os PINs em milissegundos, com qualquer
algoritmo; hash só protege entrada que **não** dá para enumerar. Validar no
backend traz junto o que faltava: operador desativado perde o acesso na hora,
em vez de continuar entrando até o display atualizar a lista.

O preço é não dar para logar no display com o backend fora do ar — e ele já não
dava: o backend é o único canal do display, sem ele não há ordem, catálogo nem
estoque na tela. Isso **não** contraria "a bancada funciona com o central
desligado": o central é outra máquina; este processo é o dono da porta serial.

Como conferir hash custa ~300 ms de propósito (é esse custo que separa um `.db`
levado no bolso de todos os PINs da bancada), esse é o único pedido em que o
firmware espera **5 s** em vez dos 800 ms de sempre.

Regravar o firmware, só quando for preciso:

```bash
cd painel_operador/firmware
pio run                                 # compila
pio run -t upload --upload-port COM4    # confira a porta antes
```

> **Derrube o backend antes de gravar.** Ele é o dono da porta serial, e o
> upload precisa dela.

> **O firmware em campo precisa ser regravado — e agora isso não é opcional.**
> O display que já está na bancada compara o PIN contra a lista que recebe do
> backend, e essa lista **não carrega mais PIN nenhum**: até ser regravado, ele
> recusa qualquer PIN digitado e ninguém entra na tela. Regrave e o login volta,
> agora conferido pelo backend (`cmd: validar_operador`).
>
> A gravação também resolve o que já estava pendente: o display antigo guarda o
> `os_id` em 16 bytes e oferece os botões de ação em toda ordem — ou seja,
> **trunca** os identificadores do central — ver [O firmware e o `os_id` do
> central](#o-firmware-e-o-os_id-do-central).
>
> O cartão SD do display guarda um cache em `/operadores.json`. O firmware novo
> o reescreve sem PIN na primeira sincronização, mas apagar o arquivo antes de
> gravar tira do cartão os PINs que já estão lá.

## Banco do painel (SQLite)

`backend/apsen.db` já vem com dados de demonstração: catálogo, ordens locais,
lotes, operadores e desvios. Para começar do zero, com o app parado:

```bash
del apsen.db apsen.db-wal apsen.db-shm      # Windows
rm -f apsen.db apsen.db-wal apsen.db-shm    # Linux/macOS
python app.py                               # recria schema + seed de demonstração
```

O seed de demonstração só roda **enquanto a tabela `lotes` estiver vazia**.

Banco de uma versão anterior é acertado por migração (`PRAGMA table_info` +
`ALTER TABLE`, idempotente). Coluna nova exige, portanto, **duas** entradas: a
definitiva no `CREATE TABLE` e a de reparo na migração — `CREATE TABLE IF NOT
EXISTS` não repara tabela que já existe. É a mesma disciplina do `database.py`
do central.

## Mapeamento de status

O central e o painel têm vocabulários diferentes. A tradução acontece em **um
lugar só** — `STATUS_CENTRAL_PARA_PAINEL`, em `backend/central_client.py`.
Espalhá-la por rota e por template é o que faz um status novo aparecer traduzido
numa tela e cru na seguinte.

| Central | Painel |
|---|---|
| `aguardando` | Pendente |
| `em_andamento` | Em Processo |
| `concluida` | Concluido |
| `erro` | **Erro** |
| `cancelada` | **Cancelado** |

`Erro` e `Cancelado` são valores **novos** no painel: antes desta integração
nenhuma ordem chegava a eles. Todo lugar que compara status por igualdade —
dashboard, `/ordens`, `/relatorio`, `/kpis`, `/api/resumo` — os conta e os
mostra. Status que o central passe a emitir e o mapa ainda não conheça cai em
`Erro`, e não em `Pendente`: um estado terminal desconhecido exibido como fila
deixaria o operador esperando a célula executar uma ordem que já acabou.

`Pausado` continua existindo e é só do painel — o central não o emite.

## Ordem espelhada é só-leitura

Linha de `ordens` com `origem='central'` não pode ter status alterado, ser
editada nem excluída pelo painel. O bloqueio vale nos **três** caminhos que
escrevem:

| Caminho | Recusa |
|---|---|
| Web (`/ordens/<id>/status/...`, `/editar`, `/excluir`) | redireciona com `flash` explicando; os botões nem aparecem |
| API (`PUT /api/ordens/<id>/status`) | `409` + `{"ok": false, "erro": "ordem do central"}` |
| Serial (`cmd: set_status` do display) | `{"resp":"ok","ok":false,"msg":"ordem do central"}` |

O mesmo vale para o estoque: `sync_dispensers` e `set_dispenser_med` recusam
slot espelhado, pelo mesmo motivo pelo qual já ignoravam o dispenser sob a
câmera — **quem manda no número é quem o mede**.

A tela `/ordens/nova` **não** muda: ela continua criando ordens locais
(`origem='local'`), que o central ignora e que seguem com baixa de estoque por
FEFO. As duas populações convivem na mesma tabela e o espelho nunca toca numa
linha local.

## O firmware e o `os_id` do central

O `os_id` do central tem a forma `{template_id}-{AAAAMMDDTHHMMSS}-{6 hex}` —
`OS-INFECTO-01-20260909T143012-A1B2C3` tem 36 caracteres, e o `template_id`
varia de tamanho. O firmware guardava esse campo em `char id[16]`.

O sintoma de truncar não era tela feia. Dois disparos do **mesmo template**
diferem só no carimbo de tempo e no hexadecimal, ou seja, no fim da string:
cortados em 16 bytes, os dois viram `OS-INFECTO-01-2` — o **mesmo** id. A partir
daí, o `set_status` do display vai para a ordem errada e o push de status do
backend casa com a linha errada no `strcmp`. Corrupção silenciosa, sem erro em
lugar nenhum.

Hoje os buffers saem de constantes nomeadas no topo de `firmware/src/main.cpp`,
dimensionadas contra a **origem** do dado e não contra o que costuma caber:

| Constante | Valor | De onde vem o número |
|---|---|---|
| `MAX_OS_ID_LEN` | 64 | `ordens.os_id` é `VARCHAR(60)` no central |
| `MAX_DESTINO_LEN` | 96 | recorte de `ordens.descricao`, `VARCHAR(200)` |
| `MAX_ITENS_RESUMO_LEN` | 320 | 8 itens × (nome 31 + `|` + qtd) + separadores |
| `MAX_LOTE_LEN` | 72 | `LOT-` + o `os_id` inteiro |

`PendingAction.param1` usa a mesma `MAX_OS_ID_LEN`: é o mesmo campo, e um
limite menor lá reintroduziria a colisão só na fila offline — o pior lugar para
ela aparecer, porque é onde ninguém está olhando.

Quando algo ainda assim não couber, `copy_trunc()` termina o texto em `...`.
Truncar de propósito e deixar rastro; nunca cortar em silêncio e deixar quem lê
achar que viu o valor inteiro. O resumo de itens é o único que trunca **por
item**, e não por caractere: cortar no meio de um par `nome|qtd` deixaria uma
quantidade pela metade, que `descontar_itens_ordem` leria como outro número —
truncamento que vira erro de estoque em vez de texto cortado.

### Ordem do central não tem botão

Os botões Iniciar / Retomar / Pausar / Concluir são **escondidos** na linha de
uma ordem espelhada, que em troca ganha o rótulo `CENTRAL` com o status. O
operador precisa entender antes de clicar, não descobrir depois por um popup de
erro — e o backend recusaria a escrita de qualquer jeito.

`get_ordens` passou a trazer `origem` por ordem. **Campo ausente vale `local`**:
é o contrato do backend anterior à integração e o do `simulador_serial.py`, e
não é por falta de um campo que uma ordem deve virar só-leitura.

A regra tem um corolário que não é óbvio: `queue_pending_action` **nunca**
enfileira ação de ordem do central. Ação enfileirada é ação que vai ser tentada
de novo quando o backend voltar; para essas, "depois" nunca é a hora certa — a
recusa não é do momento, é da ordem. Enfileirar só adiaria a mesma recusa e
gastaria um dos 20 slots de fila offline.

Ordem espelhada também nunca vira `ordem_atual`: o painel de ordem ativa é a
tela de quem está com a ordem na mão, e oferece Pausar e Concluir. Em
compensação, a lista mostra a ordem do central que está em **execução agora**
(`Separando`), o que ela não fazia para ordem local — se não mostrasse, a ordem
sumiria da tela do operador justamente enquanto a célula a executa.

### O display precisa ser AVISADO — ele não descobre sozinho

O espelho grava o status por `UPDATE` direto, de propósito: passar por
`_set_status_by_numero_os` dispararia a baixa de estoque de uma ordem cujo
estoque a célula já baixou. Só que era essa função que avisava o display.

E o display não descobre sozinho. `fetch_ordens_api` monta apenas ordem que ele
**ainda não conhece**, e a fila que ele recebe traz só `Pendente` e
`Em Processo` — uma OS concluída ou abortada **some** da lista servida em vez de
mudar de estado. Sem aviso explícito, a ordem congelava na tela do operador no
status em que entrou, para sempre.

Hoje `sincronizar_ordens_central` publica `ordem_status` quando — e só quando —
o status de uma ordem espelhada muda de fato. Ordem nova não gera push: o
display a recebe pelo `get_ordens` do ciclo seguinte.

Do lado do firmware há a rede de segurança: `fetch_ordens_api` refresca o status
de ordem **espelhada** que ele já conhece. Push é uma linha serial, e uma linha
serial se perde num reset do ESP32 ou numa reconexão da porta. Ordem **local**
conhecida não se toca — quem manda no estado dela é o display.

### Slot do display recicla em qualquer estado terminal

O firmware guarda 5 ordens e, com a lista cheia, recicla o slot de uma ordem já
terminada. Ele reconhecia só `Pronto`. Com `Erro` e `Cancelado` entrando no
vocabulário, uma OS que a célula abortou prenderia um slot para sempre — e
depois de cinco abortos o display pararia de aceitar ordem nova, sem nada no log
dizendo por quê. Abortar é evento de rotina nesta célula, não exceção: a 1% de
erro mecânico, 2 de 5 OS travaram numa execução contínua medida.

### O estoque dos slots é cacheado por 2 s

`get_dispensers` do display cai em `_dispensers_data`, que faz HTTP no central —
e isso roda **dentro da ponte serial**. O `serial_request` do firmware desiste em
**800 ms**; `CENTRAL_TIMEOUT_S` é **3 s**. Central lento (de pé, mas sem
responder) fazia o display desistir muito antes de o backend ter a resposta, e o
painel de estoque congelava sem que o log do firmware apontasse para o central.

O cache (`DISPENSERS_CACHE_S`) tira a rede do caminho serial no caso comum. Ele
também garante que as duas leituras de um mesmo pedido concordem — montar a
lista e decidir o que é só-leitura passam por aqui separadamente, e metade da
decisão tomada sobre um central que respondeu com a outra metade sobre um que
caiu deixaria o painel sem poder ler e sem poder escrever o mesmo slot.

### Exercitar isso sem hardware

Não há como compilar o firmware na suíte `pytest`, e nenhum teste finge que há.
Quem exercita o protocolo sem placa é `firmware/simulador_serial.py`, que faz
dois papéis: empurra comandos de debug **e responde** aos pedidos que o próprio
display faz (`ping`, `get_ordens`, `set_status`, ...). Sem o segundo papel o
display fica OFFLINE e `fetch_ordens_api` nunca roda — que é justamente o
caminho onde o `os_id` longo e o `origem` chegam.

Os dados dele cobrem de propósito: dois `os_id` do mesmo template (que colidem
em 16 bytes), `origem` `central`, `local` e **ausente**, uma ordem em
`Em Processo`, e pushes de `Erro` e `Cancelado` — estes últimos não chegam por
`get_ordens`, porque a fila do display só traz `Pendente` e `Em Processo`.

## Estrutura do backend do painel

| Arquivo | O que faz |
|---|---|
| `app.py` | Rotas web, API, ponte serial, regras de estoque e lote |
| `central_client.py` | Leitura do computador central. Só `GET` — ver o docstring |
| `central_comandos.py` | A única escrita no central: liberar a trava, por conta de serviço, em nome do supervisor |
| `templates/` | Telas (Jinja2 + Bootstrap) |
| `apsen.db` | Banco. Recriado automaticamente se apagado. |
| `desktop.py` | Empacotamento como app de desktop (opcional) |

As threads de fundo (serial e espelho) sobem em `iniciar_workers()`, chamada
pelo bloco de execução e pelo `desktop.py` — nunca no import. Importar o módulo
não pode abrir porta USB nem conexão de rede.

**API usada pelo display.** O display **não usa HTTP** — fala por serial USB com
mensagens JSON (`get_ordens`, `get_dispensers`, `set_status`, `validar_operador`,
`ping`…). As rotas `/api/*` equivalentes existem para depuração manual e para a
estação de visão, e todas exigem `X-API-Token` (ver [Segredos do
painel](#segredos-do-painel)). `GET /api/operadores` **não existe mais**: ela
devolvia o PIN de todos os operadores, sem autenticação, para um consumidor que
migrou para o serial anos atrás. O backend também empurra
mensagens não solicitadas quando algo muda: `{"push": "ordem_status", ...}`.
O display recebe no máximo **5** ordens (`MAX_ORDENS_DISPLAY`): o firmware
guarda `MAX_ORDENS 5` e o central limita a fila a `MAX_FILA_OS 5` — o teto casa
por construção. A lista traz `Pendente` **e** `Em Processo`; sem o segundo, o
operador não veria no display a ordem que a célula está executando agora.

**Perfis de acesso.** `Admin`, `Supervisor`, `PCP`, `PCM` e `Operador` — a
matriz está em `PERMISSOES`, no topo do `app.py`. `Operador` não tem acesso web
(só ao display); `Supervisor` vê o dashboard e as ordens e é quem libera a
trava do Triple Check (ver [Perfil Supervisor e a liberação da
trava](#perfil-supervisor-e-a-liberação-da-trava)).

**Estação de visão.** As rotas `/api/visao/*`, a tela `/visao` e as colunas
`dispenser_visao`, `sku_visao`, `aruco_visao` e `unidades_por_caixa` existem e
funcionam. A estação em si vive fora deste repositório.

## Armadilhas conhecidas do painel

Esta seção existe porque cada item aqui já custou horas.

**Não abra o monitor serial do PlatformIO.** `pio device monitor` **rouba a
porta do backend**. O display cai para OFFLINE e não volta enquanto o monitor
estiver aberto. Se precisar ver o log do ESP, derrube o backend antes.

**O reloader do Flask está desligado de propósito.** `app.py` roda com
`use_reloader=False`. Este processo **possui uma porta serial**; com o reloader
ligado, cada save do arquivo matava e recriava o dono da porta, e o display
ficava OFFLINE sem nada no log explicando. Para desenvolver sem hardware:
`APSEN_RELOADER=1 python app.py`.

**Sondar a porta não pode reiniciar o display.** O backend abre a serial com
**DTR/RTS desligados**. Nessa placa esses dois sinais são o circuito de reset do
ESP32-S3: abrir a porta do jeito padrão do `pyserial` reiniciava o display a
cada tentativa de detecção, e ele nunca chegava a responder. Se mexer em
`_probe_port`, **não volte ao `serial.Serial(porta)` direto.**

**O display muda de porta COM sozinho.** Ao trocar de porta USB o Windows dá
outro número e deixa a antiga como entrada fantasma (`Status: Unknown`). Sem
`APSEN_DISPLAY_PORTA` o backend varre todas as portas e acha sozinho — mas se
for **regravar o firmware**, confira a porta atual antes.

**Na célula montada, fixe a porta.** `APSEN_DISPLAY_PORTA=COMx` faz o backend
abrir só aquela porta e **não varrer**: com cinco placas no mini PC, cada
processo que varre abre as portas dos outros por até 9,5 s e derruba o dono de
verdade. A varredura é para desenvolvimento sem hardware. Ver o README, seção
"Portas seriais na célula montada", e [DEPLOY_WINDOWS.md](DEPLOY_WINDOWS.md).

**Processos órfãos.** Fechar o terminal nem sempre mata o Python. Se algo
estranho acontecer com a porta, o primeiro palpite deve ser processo duplicado,
não bug: `tasklist | findstr python`.

---

