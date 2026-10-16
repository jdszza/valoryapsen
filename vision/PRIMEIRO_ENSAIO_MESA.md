# Primeiro ensaio com a câmera da MESA

Roteiro para levar a câmera da mesa (a câmera da balança, que conta as caixinhas
na caixa de coleta) da webcam na mão até o central recebendo a contagem dela.
São **três ensaios**, e a ordem importa pelo mesmo motivo do
[ensaio da balança](../weight/PRIMEIRO_ENSAIO.md): cada um só faz sentido depois
que o anterior passou, porque cada um elimina uma classe de causa.

| # | o que liga | o que prova | precisa de Docker? |
|---|---|---|---|
| 1 | webcam + `calibrar.py` / `main.py` | a câmera vê a caixa, acha o fundo e conta certo | não |
| 2 | a estação HTTP no host (`:8212`) | a estação recebe comando, conta e emite o evento | sim (o evento sobe pelo vision-adapter) |
| 3 | a stack com `VISAO_MESA_FONTE=estacao` | o central manda capturar para a câmera REAL e o evento volta | sim |

As regras de bancada (M1–M6) estão em
[BANCADA_VISAO.md](../docs/BANCADA_VISAO.md); o `.env` da estação e o firewall,
em [DEPLOY_WINDOWS.md](../docs/DEPLOY_WINDOWS.md). Este roteiro é a ordem em
que se faz tudo isso pela primeira vez.

> **O código da estação não se edita.** Tudo abaixo mexe em configuração
> (`config/mesa.json`, `.env`) e no mundo físico. `tests/test_visao_intocada.py`
> reprova qualquer `.py` mudado dentro de `vision/visao_mesa/`.

---

## Onde a bancada está hoje (13/10/2026)

Medido com `python vision\conferir_mesa.py`, antes de qualquer passo:

| item | estado | resolvido em |
|---|---|---|
| `.venv` da mesa | **criado em outra máquina** (aponta para `C:\Users\perfil\...`) — não executa | 1.1 |
| `fundo.modo` | `manual` — conta no lugar errado assim que a caixa anda, e sem avisar | 1.4 |
| `fundo.suavizacao` / `frames_validade` | `0.7` / `60` — reaproveitam os cantos do slot anterior | 1.5 |
| `camera.autofoco` | `true` — o foco caça a cada parada | 1.5 |
| `integracao_apsen\.env` | não existe — valeria a porta 8202, que é do vision-simulator | 2.1 |

E o resto da planta: balança simulada, **dispenser e CNC desligados**. Isso
significa que **nenhuma OS roda de ponta a ponta ainda** — o ensaio 3 prova o
caminho central → adapter → câmera real → central por um comando direto, não por
uma OS. O que fica para quando a CNC entrar está em 3.6.

---

## Antes de começar

- [ ] a webcam da mesa presa no poste, ligada por USB no mini PC — **sempre na
      mesma porta USB** (o índice da câmera muda com a ordem de enumeração);
- [ ] a caixa de coleta sobre a mesa, com o fundo **forrado de cartolina de cor
      contrastante** — papelão contra papelão não dá contorno, e o modo `caixa`
      precisa do contorno (M1);
- [ ] trena;
- [ ] 5 a 8 caixinhas de medicamento das que vão para os dispensers;
- [ ] nenhum outro programa usando a webcam (app Câmera, Teams, navegador) —
      webcam no Windows é de um processo só;
- [ ] a stack de pé (`docker compose ps` com `apsen-vision-adapter` healthy).

> **Os comandos são PowerShell**, a partir da raiz do repositório. Chamada HTTP
> é `Invoke-RestMethod`, não `curl`: no PowerShell 5.1 `curl` é alias de
> `Invoke-WebRequest`, e o escape de `\"` para executável nativo é quebrado — o
> corpo JSON chega mutilado e a resposta é 422 com o comando visivelmente certo.

---

## Ensaio 1 — a câmera sozinha

### 1.1 Recriar o venv da estação

O venv que está na pasta foi criado em outra máquina e aponta para um Python que
não existe aqui. Não tem conserto: apague e recrie. Precisa dos **dois**
requirements — o da visão (OpenCV contrib, numpy) e o da integração (FastAPI,
uvicorn, requests); sem o segundo, o servidor morre no import.

```bash
cd vision\visao_mesa
Remove-Item -Recurse -Force .venv
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r integracao_apsen\requirements.txt
.\.venv\Scripts\python.exe -c "import cv2, fastapi, uvicorn, requests; print(cv2.__version__, hasattr(cv2, 'aruco'))"
```

A última linha tem que imprimir a versão e `True`. Erro estranho de `cv2` (dois
pacotes OpenCV brigando):

```bash
.\.venv\Scripts\python.exe -m pip uninstall -y opencv-python opencv-python-headless opencv-contrib-python
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> Use sempre `.\.venv\Scripts\python.exe` em vez de ativar o venv: o
> `Activate.ps1` costuma esbarrar na política de execução do PowerShell, e o
> caminho explícito não deixa dúvida de qual Python está rodando.

### 1.2 Achar a webcam

Ainda em `vision\visao_mesa`:

```bash
.\.venv\Scripts\python.exe camera_finder.py
```

Ele varre os índices e diz se o driver aceitou desligar autofoco, autoexposição
e white balance. Confirme qual índice é a câmera da mesa abrindo a janela dela:

```bash
.\.venv\Scripts\python.exe camera_finder.py --ver 1
```

(troque o `1` pelo índice sugerido). Anote o índice em `config\mesa.json` →
`camera.indice`. Hoje está `1`.

### 1.3 Medir o fundo

Meça com trena **a cartolina** que forra o fundo da caixa — é ela que o modo
`caixa` reconhece, e é essa medida que converte pixel em milímetro. Em
`config\mesa.json`, dentro de `"fundo"`:

```json
"modo": "caixa",
"comprimento_mm": <lado maior da cartolina>,
"largura_mm": <lado menor da cartolina>
```

Ponha o `modo` em `caixa` **aqui, antes** de abrir o calibrador.

### 1.4 Calibrar

```bash
.\.venv\Scripts\python.exe src\calibrar.py
```

1. **Aba 3 — `3_CAMERA_luz_e_foco` primeiro.** Arraste `AUTO AJUSTE (0->1)`: ele
   acha a exposição e só depois o foco. Com a caixa na posição do meio da mesa,
   o foco escolhido tende a servir às 8 paradas.
2. **Aba 1 — `1_FUNDO_da_caixa`.** Mexa em `limiar` até o contorno dourado
   abraçar exatamente a cartolina. Enquanto isso não estiver certo, não adianta
   mexer na aba 2.
3. **Aba 2 — `2_CONTEUDO_medicamentos`.** Com 1, 3 e 5 caixinhas espalhadas,
   mexa em `limiar` até a contagem bater. Duas encostadas virando uma: veja
   `cortar_juncoes`; fantasma na borda: aumente `margem interna mm`.
4. Arraste `SALVAR TUDO (0->1)`, ligue os dois LOCK (teclas `1` e `2`) e salve de
   novo.

> **NÃO use `MARCAR FUNDO c MOUSE` (tecla `e`).** Ele grava `fundo.modo =
> "manual"` — quatro cantos fixos na imagem, que é exatamente o modo que conta
> no lugar errado quando a caixa anda com a CNC, e conta com `confiavel=True`.

Se as teclas não responderem: clique na janela de **vídeo** primeiro (o Windows
não entrega tecla a uma janela de sliders) ou use o slider equivalente. Os 79
parâmetros, um a um, estão em
[NOTAS_CALIBRAGEM.md](visao_mesa/NOTAS_CALIBRAGEM.md).

### 1.5 Os três campos que o calibrador desfaz

O `calibrar.py` regrava o `mesa.json` **inteiro** a cada "SALVAR TUDO". Por isso
estes vêm **depois** do último salvamento — e precisam ser refeitos depois de
qualquer recalibração. Em `config\mesa.json`:

| campo | valor | por quê |
|---|---|---|
| `fundo.suavizacao` | `0` | com outro valor, a foto de um slot herda os cantos do slot anterior |
| `fundo.frames_validade` | `0` | idem, com a homografia inteira |
| `camera.autofoco` | `false` | o foco caçaria a cada parada da mesa; o `foco` fixo é o que o auto ajuste achou |

Confira:

```bash
cd ..\..
python vision\conferir_mesa.py
```

Neste ponto as falhas de `fundo.*` e o alerta de autofoco têm que ter sumido.
Sobra só a do `.env`, que é o passo 2.1.

### 1.6 Contar ao vivo, sem HTTP

```bash
cd vision\visao_mesa
.\.venv\Scripts\python.exe src\main.py
```

Ponha 1, depois 3, depois 5 caixinhas; a contagem na janela tem que acompanhar.
Repita **com uma caixinha encostada na parede do lado da câmera** — é a que se
esconde (a câmera é inclinada). `q` sai.

**Critério para seguir:** contagem certa nas três quantidades, com a caixa onde
ela fica na maior parte das paradas. Se a caixinha encostada some sempre, o
problema é de montagem (M5): suba ou incline mais a câmera antes de continuar.

---

## Ensaio 2 — a estação HTTP no host

### 2.1 O `.env` da estação

Crie `vision\visao_mesa\integracao_apsen\.env` (não é versionado) com:

```bash
ADAPTER_URL=http://127.0.0.1:8102
PORTA=8212
HOST=0.0.0.0
T_ASSENTAMENTO_S=1.0
FRAMES_POR_CAPTURA=5
ACEITAR_INJECAO=0
VALIDADE_OS_H=24
```

O porquê de cada valor está comentado em
[DEPLOY_WINDOWS.md](../docs/DEPLOY_WINDOWS.md), seção "As três estações de
visão". Os dois que mais mordem: `PORTA=8212` (a 8202 do `.env.example` é do
vision-simulator, que continua no ar) e `ACEITAR_INJECAO=0` sempre.

### 2.2 Conferir

Da raiz do repositório:

```bash
python vision\conferir_mesa.py
```

**Tem que terminar sem nenhuma `[FALHA]`.** O `iniciar_mesa.bat` roda o mesmo
conferidor e se recusa a subir em vermelho, então não há como pular este passo.

### 2.3 Subir a estação (janela 1)

```bash
vision\iniciar_mesa.bat
```

Deixe a janela aberta: é nela que aparece o log de cada captura. Se a estação
cair, o `.bat` registra em `vision\visao_mesa\dados\reinicios.log` e a sobe de
novo em 5 s. Para encerrar de vez: Ctrl+C e responda S.

**Nunca `vision\iniciar_visao.bat`** — ele sobe na porta 8000, que é a do
central.

### 2.4 Conferir que a câmera abriu (janela 2)

```bash
(Invoke-RestMethod http://localhost:8212/ping).service
(Invoke-RestMethod http://localhost:8212/status).cameras
```

O primeiro tem que dizer `apsen-vision-station`; o segundo, `pronta: True`. A
câmera leva alguns segundos para abrir depois de o processo subir — `/ping`
responde antes disso de propósito.

### 2.5 A primeira captura

Ponha **3** caixinhas na caixa e peça a foto direto à estação:

```bash
$corpo = @{ slot_id = 1; os_id = "TESTE-MESA-1"; quantidade_esperada = 3 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8212/executar/capturar/mesa -ContentType "application/json" -Body $corpo
```

A resposta é imediata ("Processando scan da mesa"); o resultado sai como evento,
depois do assentamento e dos 5 quadros (~3 s). Na janela 1 aparece a leitura, e
ela sobe pelo vision-adapter até o central:

```bash
(Invoke-RestMethod "http://localhost:8000/api/v1/visao/historico?os_id=TESTE-MESA-1").leituras | Format-List
```

Esperado: `leitura_mesa_ok` com 3 detectadas.

### 2.6 O acumulado — o teste que fecha o ensaio

A estação conta o TOTAL da caixa e manda a diferença desde a última foto da
mesma OS. É isso que precisa funcionar para a contagem por slot fazer sentido.
**Sem tirar as 3**, acrescente **2** e peça a foto do slot 2 da mesma OS:

```bash
$corpo = @{ slot_id = 2; os_id = "TESTE-MESA-1"; quantidade_esperada = 2 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8212/executar/capturar/mesa -ContentType "application/json" -Body $corpo
```

Esperado: `leitura_mesa_ok` para o slot 2 com **2** (5 na caixa − 3 da foto
anterior). Se vier 5, a estação não está guardando o acumulado; se vier
divergência, olhe a janela 1 para ver quanto ela contou no total.

Duas regras para repetir o teste:

- **um `os_id` novo a cada rodada** (`TESTE-MESA-2`, `-3`...): a estação nunca
  refotografa um par (os_id, slot) que já recebeu;
- as leituras de teste entram no histórico do central, e as divergências viram
  alarme — resolva-os depois no app de manutenção (:8051), ou limpe o histórico
  pelo reset do console antes da apresentação.

**Critério para seguir:** 5 rodadas seguidas (5 `os_id` diferentes) com 3 + 2
dando `ok` nas duas fotos.

---

## Ensaio 3 — a câmera da mesa dentro da planta

### 3.1 Firewall: o container alcançando a estação

O vision-adapter está em container e alcança a estação por
`host.docker.internal:8212` — para o Windows, tráfego de entrada. Como
**Administrador**, uma vez (a regra já cobre os adapters e as outras estações):

```bat
netsh advfirewall firewall add rule name="APSEN adapters e estacoes" dir=in action=allow protocol=TCP localport=8100,8101,8103,8212,8301,8302 profile=private
```

Teste de dentro do container:

```bash
docker compose exec vision-adapter python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:8212/ping',timeout=3).read())"
```

Timeout com a regra criada: em *Configurações → Rede*, a interface
`vEthernet (WSL)` às vezes está como **pública**, e a regra vale só para a
privada.

### 3.2 O `.env` da raiz

Acrescente ao `.env` da raiz (o do compose):

```bash
# Câmera da MESA real: a estação no host. Os dispensers seguem simulados.
VISION_SIM_URL=http://host.docker.internal:8212
VISAO_MESA_FONTE=estacao
# Sem as câmeras dos dispensers, o central não pede scan de SKU (ver abaixo).
VISAO_SKU_HABILITADA=0
```

**O `VISAO_SKU_HABILITADA=0` não é opcional nesta fase.** Com
`VISAO_DISPENSER_FONTE` em `simulador` (o default, que fica), o vision-adapter
manda o scan dos dispensers para o MESMO `VISION_SIM_URL` — que agora é a
estação da mesa, e ela responde 501 a esse comando (ela não lê SKU). O central
retentaria cada slot à toa para chegar ao mesmo lugar: slot sem conferência de
SKU. Com a chave em 0 ele nem pede. Volta para 1 (ou sai do `.env`) no dia em
que as estações dos dispensers entrarem, junto com `VISAO_DISPENSER_FONTE=estacao`.

`VISAO_MESA_POSICOES` **fica de fora** por enquanto (= todas as posições
visíveis): o mapa de visibilidade (M4) só se mede com a CNC levando a caixa a
cada slot.

### 3.3 Recriar os dois serviços que leem essas variáveis

```bash
docker compose up -d vision-adapter central-computer
```

O `up -d` recria só os containers cuja configuração mudou. O vision-simulator
continua no ar: é o rollback.

### 3.4 Conferir que o adapter fala com a câmera real

Espere uns 30 s (o adapter confere a identidade do outro lado em segundo plano)
e:

```bash
$h = Invoke-RestMethod http://localhost:8102/health
$h.fontes; $h.identidade_mesa; $h.comandos_mesa_para; $h.estacao_mesa
```

| campo | tem que dizer |
|---|---|
| `fontes.mesa` | `estacao` |
| `identidade_mesa.confere` | `True` |
| `comandos_mesa_para` | `http://host.docker.internal:8212` |
| `estacao_mesa.pronta` | `True` |

`confere: False` significa que `VISION_SIM_URL` aponta para o simulador (ou o
contrário): o adapter **recusa** a captura da mesa com 503 nesse estado, porque
contagem sorteada entrando no Triple Check como câmera real é pior que câmera
ausente.

### 3.5 A captura pelo caminho do central

Agora o comando entra pelo vision-adapter — é a mesma rota que o orquestrador
usa no passo 4 de uma OS. Caixa vazia, ponha **4** caixinhas:

```bash
$corpo = @{ slot_id = 1; os_id = "TESTE-PLANTA-1"; quantidade_esperada = 4 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8102/comandos/capturar/mesa -ContentType "application/json" -Body $corpo
Start-Sleep -Seconds 6
(Invoke-RestMethod "http://localhost:8000/api/v1/visao/historico?os_id=TESTE-PLANTA-1").leituras | Format-List
```

Esperado: `leitura_mesa_ok` com 4, `camera` = `mesa`. No dashboard (:8050), o
painel de visão mostra a câmera da mesa no corredor com a leitura.

Por fim, o pré-voo (`http://localhost:8000/console/prevoo`), no grupo Visão:
"Fonte das câmeras" com `mesa: estacao · dispensers: simulador` e "Estação da
mesa" verde (`câmera pronta · fila 0`). Ele também mostra um **alerta amarelo**
"Posições da câmera da mesa" — esperado nesta fase: some quando o mapa M4 for
medido (3.6).

**Pronto: a câmera da mesa está ligada.** O que ela ainda não fez é conferir
uma OS de verdade — isso depende da CNC e do dispenser.

### 3.6 Quando a CNC e o dispenser entrarem

Nesta ordem, com a planta inteira:

1. **M3 — vibração.** Rode OS e olhe o campo `nitidez` nos eventos. Muitas
   falhas "fora de foco" pedem `T_ASSENTAMENTO_S` maior no `.env` da estação.
2. **M4 — mapa de visibilidade.** Para cada slot 1..8 e para o HOME: leve a mesa
   até lá, ponha 3 caixinhas (uma encostada na parede do lado da câmera) e faça
   5 capturas com `os_id` diferentes (`MAPA-<slot>-a`...`-e`), direto na
   estação como em 2.5. Entra no mapa só a posição com **5 de 5 `ok`**. Grave
   no `.env` da raiz `VISAO_MESA_POSICOES=<os slots aprovados>` — e, se o HOME
   passou, `VISAO_MESA_FINAL=HOME`, que confere no fim da OS o que ficou
   pendente nas posições cegas. Recrie o `central-computer`.
3. **Uma OS pelo console** com a balança ligada, e confira no relatório da OS
   que cada slot visível tem uma leitura da mesa.

---

## Os erros mais prováveis

| sintoma | causa provável |
|---|---|
| `iniciar_mesa.bat` para em vermelho | o conferidor achou algo — a linha diz o quê e onde corrigir |
| `/status` com `pronta: False` por muito tempo | índice de câmera errado, ou outro programa com a webcam aberta |
| contagem certa parada, errada depois que a caixa anda | `fundo.modo` não é `caixa`, ou `suavizacao`/`frames_validade` voltaram depois de um "SALVAR TUDO" |
| slot 2 vem com o total da caixa em vez da diferença | `os_id` diferente entre as fotos, ou a estação reiniciou entre elas |
| 503 em `/comandos/capturar/mesa` | `identidade_mesa.confere = False` — `VISION_SIM_URL` e `VISAO_MESA_FONTE` desencontrados |
| timeout do container para a 8212 | firewall, ou `vEthernet (WSL)` no perfil público |
| a estação não sobe: porta em uso | `PORTA` ficou 8202 (a do vision-simulator) |

## Como voltar ao simulador

Tire do `.env` da raiz as três linhas do passo 3.2 e recrie:

```bash
docker compose up -d vision-adapter central-computer
```

Nada mais: o vision-simulator nunca saiu do compose, e os defaults são os de
antes. A estação da mesa pode continuar rodando no host — ninguém mais a chama.
