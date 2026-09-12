# APSEN – Sistema de Contagem de Medicamentos

> **Projeto de Engenharia Mecatrônica – 3º Ano**
> Valory · APSEN Farmacêutica

Documentação única do repositório. O backlog e as tasks pendentes estão em
[`TASKS.md`](TASKS.md); as decisões de arquitetura e as armadilhas de
implementação, em `CLAUDE.md`.

---

## Índice

- [Visão geral](#visão-geral)
- [Arquitetura](#arquitetura-v32--resthttp)
- [Layout físico da célula](#layout-físico-da-célula)
- [As 10 ordens padrão](#as-10-ordens-padrão)
- [Console de operação](#console-de-operação-console)
- [Fluxo de uma OS](#fluxo-de-uma-os)
- [Triple Check](#triple-check)
- [Camada de visão computacional](#camada-de-visão-computacional)
- [Modo apresentação](#modo-apresentação-demo-sem-imprevisto)
- [Injeção de falha sob demanda](#injeção-de-falha-sob-demanda)
- [Reset rápido da planta](#reset-rápido-da-planta)
- [Histórico de demonstração](#histórico-de-demonstração)
- [Pré-voo: está tudo de pé?](#pré-voo-está-tudo-de-pé)
- [Tela de necessidades](#tela-de-necessidades-app-de-manutenção)
- [**Build e deploy**](#build-e-deploy)
- [Roteiro de demonstração](#roteiro-de-demonstração--passo-a-passo)
- [Painel de bancada](#painel-de-bancada-painel_operador)
  - [Testar o painel junto com a célula](#testar-o-painel-junto-com-a-célula)
  - [Gravar e testar o display físico](#gravar-e-testar-o-display-físico-esp32-s3)
- [Interfaces e segredos](#interfaces)
- [Endpoints](#endpoints-principais)
- [Estrutura do repositório](#estrutura-do-repositório)
- [Variáveis de ambiente](#variáveis-de-ambiente)
- [Concorrência e segurança](#concorrência-e-segurança)

---

## Visão Geral

Sistema automatizado de contagem e validação de medicamentos. Uma mesa CNC percorre o corredor entre **duas fileiras de 4 dispensers**, frente a frente, para coletar os medicamentos de cada Ordem de Saída (OS). Três câmeras de visão computacional validam a célula: uma sobre cada fileira de dispensers (esquerda, D1–D4; direita, D5–D8), lendo o QR/DataMatrix do produto carregado em cada slot, e uma sobre a mesa de coleta — a **câmera da balança** —, que faz a contagem visual das unidades dispensadas. Uma célula de carga HX711 instalada sob a mesa CNC valida o peso de cada lote dispensado. Após cada dispensa, um **Triple Check** compara as 3 fontes (contagem do dispenser, câmera da mesa e balança) e trava o sistema em caso de divergência até intervenção do operador. A comunicação é 100% REST/HTTP e WebSocket — **sem MQTT**. MySQL para persistência, Plotly Dash para interface.

O repositório tem **duas metades que sobem separado**:

| Metade | O que é | Como sobe | Portas |
|---|---|---|---|
| **Célula** | central-computer, 4 adapters, 4 simuladores, erp-simulator, dashboard, manut_web | `docker compose up` | 8000, 8050, 8051 |
| **Painel de bancada** | Flask + SQLite + display ESP32 por serial USB | `python app.py` na máquina da bancada | 5000 |

O painel **não** está no `docker-compose.yml` de propósito: ele é dono de uma porta
serial USB, e um container Linux não enxerga a COM do host. Ver
[Painel de bancada](#painel-de-bancada-painel_operador).

---

## Arquitetura (v3.2 — REST/HTTP)

```
ERP Simulator (sorteia 1 das 10 ordens padrão e instancia um os_id novo)
  │ POST /api/v1/ordens
  ▼
┌────────────────────────────────────────────────────────────────────┐
│                     Central Computer  :8000                        │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Orquestrador (orchestrator.py)                              │  │
│  │  • Fila de OS (1 por vez)                                    │  │
│  │  • Atribuição de slots por categoria/residual                │  │
│  │  • Rota CNC em serpentina (duas fileiras)                    │  │
│  │  • Fluxo: carga → visão dispenser → CNC → dispensa →         │  │
│  │           visão mesa → peso → Triple Check                   │  │
│  │  • Trava de erro: bloqueia até admin liberar                 │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  REST + MySQL + WebSocket /ws                                      │
└──────┬────────────────┬─────────────────┬───────────────┬──────────┘
       │                │                 │               │
  POST comandos    POST comandos     POST comandos   POST comandos
       │                │                 │               │
┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐  ┌────▼────────┐
│  Dispenser  │  │    CNC      │  │   Vision    │  │   Weight    │
│  Adapter    │  │   Adapter   │  │   Adapter   │  │   Adapter   │
│   :8100     │  │    :8101    │  │    :8102    │  │    :8103    │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └────┬────────┘
       │                │                 │               │
┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐  ┌────▼────────┐
│  Dispenser  │  │    CNC      │  │   Vision    │  │   Weight    │
│  Simulator  │  │  Simulator  │  │  Simulator  │  │  Simulator  │
│   :8201     │  │    :8200    │  │    :8202    │  │    :8203    │
│ 8 slots     │  │ firmware    │  │ cam disp +  │  │ HX711 sob   │
│ (2 fileiras)│  │ CNC         │  │ cam mesa    │  │ mesa CNC    │
└─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘

Dashboard  :8050  ←─ GET /estado (polling) ──── Central Computer
Manutencao :8051  ←─ JWT + REST + WS ────────── Central Computer
Displays          ←─ WebSocket /ws ──────────── Central Computer
MySQL      :3306  ←─ pymysql (sync) ─────────── Central Computer

Painel de bancada :5000 ─ GET (espelho de mão única) ─→ Central Computer
                        └─ USB serial ─→ display ESP32 7"
```

### Serviços Docker (13 total)

| Serviço               | Porta | Função                                                       |
|-----------------------|-------|--------------------------------------------------------------|
| `mysql`               | 3306  | Banco de dados MySQL 8                                       |
| `central-computer`    | 8000  | Orquestrador, API REST, WebSocket                            |
| `dispenser-adapter`   | 8100  | Bridge HTTP: central ↔ dispenser-simulator                   |
| `cnc-adapter`         | 8101  | Bridge HTTP: central ↔ cnc-simulator                         |
| `vision-adapter`      | 8102  | Bridge HTTP: central ↔ vision-simulator                      |
| `weight-adapter`      | 8103  | Bridge HTTP: central ↔ weight-simulator (HX711)              |
| `dispenser-simulator` | 8201  | Simula os 8 dispensers físicos (mecânico, sem CV)            |
| `cnc-simulator`       | 8200  | Simula firmware da mesa CNC                                  |
| `vision-simulator`    | 8202  | Simula as 3 câmeras: uma por fileira + a da mesa/balança     |
| `weight-simulator`    | 8203  | Simula célula de carga HX711 instalada sob a mesa CNC        |
| `erp-simulator`       | —     | O ERP que emite as ordens: dispara 1 das 10 padrão por ciclo |
| `dashboard`           | 8050  | Monitoramento read-only (Plotly Dash)                        |
| `manut_web`           | 8051  | Manutenção e operação com autenticação JWT (Plotly Dash)     |

### Displays dos dispensers

Os displays físicos de cada dispenser são **consumidores passivos** do WebSocket do Central Computer. Não possuem lógica de negócio, não enviam eventos e não participam do fluxo operacional. Simplesmente exibem o produto associado ao slot.

O Central emite o evento abaixo sempre que um produto é atribuído a um dispenser:

```json
{
  "event": "produto_alterado",
  "dispenser": 5,
  "produto": "Dipirona 500mg"
}
```

---

## Layout físico da célula

Os 8 dispensers ficam em **duas fileiras de 4, frente a frente**, com a mesa CNC
percorrendo o corredor entre elas. Os pares D1↔D5, D2↔D6, D3↔D7 e D4↔D8 ficam
um de frente para o outro.

```
             x=0     x=120   x=240   x=360
              │       │       │       │
   fileira    D1      D2      D3      D4        y = -150 mm
   esquerda   ▔▔      ▔▔      ▔▔      ▔▔
  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   y =    0 mm  ← corredor da CNC
   ●HOME      ▁▁      ▁▁      ▁▁      ▁▁
   (-120,0)   D5      D6      D7      D8        y = +150 mm
   fileira direita
```

| Slot | X (mm) | Y (mm) | Fileira  | Slot | X (mm) | Y (mm) | Fileira |
|------|--------|--------|----------|------|--------|--------|---------|
| D1   | 0      | −150   | esquerda | D5   | 0      | +150   | direita |
| D2   | 120    | −150   | esquerda | D6   | 120    | +150   | direita |
| D3   | 240    | −150   | esquerda | D7   | 240    | +150   | direita |
| D4   | 360    | −150   | esquerda | D8   | 360    | +150   | direita |

HOME fica em **(−120, 0)**: no eixo do corredor, um passo antes do primeiro par
e equidistante de D1 e D5.

**A geometria tem um dono só.** O passo (120 mm), o afastamento lateral
(150 mm), o HOME e o número de slots são constantes no topo de
`central-computer/orchestrator.py`, e o mapa `POSICOES` é *derivado* delas. O
`cnc-simulator` não guarda cópia nenhuma: ele recebe `posicao_x`/`posicao_y` em
cada comando de movimento (e do homing) e só valida a faixa do id. O número de
slots vem da env var `NUM_SLOTS`, a mesma para todos os serviços.

**Rota em serpentina.** Com uma fileira só, o Y de todo slot era 0 e qualquer
heurística devolvia a mesma linha reta. Com duas, o orquestrador desce uma
fileira em X crescente e volta pela outra em X decrescente. O ciclo real é
fechado (HOME → slots → HOME) e todos os pontos estão na borda de um mesmo
polígono convexo, então a ordem do contorno *é* o trajeto ótimo: medido por
força bruta nos 255 subconjuntos possíveis de 8 slots, a serpentina acerta o
ótimo em 255/255, contra um nearest-neighbor 4,1% pior em média e 40,4% pior no
pior caso. Ver `planejar_rota` e `tests/test_orchestrator.py`.

---

## As 10 ordens padrão

As Ordens de Saída **não são mais sorteadas item a item** do catálogo. Existem
dez ordens fixas, com nome, categoria, itens e quantidades definidos em
`central-computer/os_templates.py`. O acaso que sobrou é **qual delas** o
erp-simulator dispara — para quem observa a planta, o comportamento continua
imprevisível; para quem apresenta, o conteúdo de cada OS é conhecido de antemão.

| Template        | Ordem                                    | Itens | Perfil                          |
|-----------------|------------------------------------------|-------|---------------------------------|
| `OS-URO-01`     | Urologia — ronda noturna                 | 2     | a mais curta                    |
| `OS-DOR-01`     | Reumatologia / Dor — lote matinal        | 3     | curta                           |
| `OS-VITAM-01`   | Vitaminas — suplementação ambulatorial   | 3     | curta                           |
| `OS-GASTRO-01`  | Gastroenterologia — leito 118            | 4     | meia célula                     |
| `OS-SNC-02`     | Neurologia — leito 207 (reposição)       | 4     | reaproveita residual da SNC-01  |
| `OS-CARDIO-01`  | Cardiologia — leito 302                  | 5     |                                 |
| `OS-SNC-01`     | Neurologia — leito 204                   | 5     |                                 |
| `OS-LACTO-01`   | Intolerância a lactose — kit flora       | 6     |                                 |
| `OS-INFECTO-01` | Infectologia — esquema antibiótico       | 6     |                                 |
| `OS-GERAL-01`   | Carro de emergência — célula cheia       | 8     | **célula cheia**, rota completa |

Todos os 39 medicamentos citados são itens reais do catálogo APSEN. Seis deles
aparecem em mais de uma ordem (`ALOIS 10MG`, `INSIT 50MG`, `FLANCOX 500MG`,
`RETEMIC 5MG`, `LONIUM 40MG`, `INPRUV DK 7000UI`), o que faz o reaproveitamento
de residual entre OS acontecer de verdade: disparar `OS-SNC-01` e logo depois
`OS-SNC-02` reencontra dois slots já carregados, e o passo 1 do `atribuir_slots`
os usa sem limpar.

**O template é fixo; o `os_id` é único a cada disparo**, no formato
`{template_id}-{AAAAMMDDTHHMMSS}-{6 hex}` — por exemplo
`OS-SNC-01-20260814T221104-A3F291`. `ordens.os_id` é UNIQUE e o central recusa
reenvio com 409, então sem o sufixo a segunda vez que uma ordem padrão fosse
disparada seria rejeitada.

Editar as ordens na véspera de uma apresentação é mexer só na lista `TEMPLATES`
— nome do medicamento (como está no catálogo) e quantidade, entre 2 e 15. As
regras (2 a 8 itens, sem item repetido, faixa de quantidade) são validadas no
import do módulo, no boot do central e no boot do gerador; `GET
/api/v1/ordens/templates` serve as dez com o diagnóstico junto.

---

## Console de operação (`/console`)

O central serve uma interface própria em **http://localhost:8000/console**: a
mesa de onde se escolhe **qual** das dez ordens padrão entra no sistema e
**quando**. Ela existe para a hora da apresentação — quando o sorteio do
erp-simulator, que é o que dá naturalidade à planta rodando sozinha, passa a
atrapalhar quem precisa mostrar um caso específico.

O que dá para fazer de lá:

| Ação | Detalhe |
|------|---------|
| Listar as 10 ordens | nome, categoria, itens, quantidades e quantos slots ocupa |
| Disparar qualquer uma | um clique, sem confirmação — `os_id` novo a cada disparo |
| Pausar / retomar o automático | assume o controle sem competir com o gerador |
| Estado ao vivo | OS ativa, fila, trava (com motivo e slot) e os 8 dispensers |
| Liberar a trava do Triple Check | **com confirmação** — é a única ação destrutiva |
| **Armar uma falha** | escolhe tipo e slot; dispara uma vez e se desarma sozinha |
| **Resetar a planta** | **com confirmação** — limpa os slots de verdade, zera a balança, CNC para HOME |
| **Semear histórico** | **com confirmação** — dado de DEMONSTRAÇÃO para o dashboard não abrir vazio |
| **Pré-voo** | tela à parte (`/console/prevoo`): a stack inteira conferida item a item |
| Ver a resposta do central | código HTTP e corpo de cada ação, inclusive as recusas |

**Acesso.** Rota discreta: não é linkada de lugar nenhum e não aparece no
Swagger (`include_in_schema=False`). A senha é `CONSOLE_SENHA`, do `.env` —
**própria do console, independente do login JWT do app de manutenção**. A
sessão é um cookie `HttpOnly` assinado por HMAC, válido por
`CONSOLE_SESSAO_HORAS` (8h) e restrito ao caminho `/console`; trocar a senha
invalida na hora as sessões abertas, porque ela entra na chave de assinatura.
Cinco senhas erradas em um minuto bloqueiam a origem por um minuto.

> **Sem `CONSOLE_SENHA` o console não existe**: toda rota `/console*` responde
> **503** com a instrução de definir a variável, e nenhuma senha confere. Não há
> senha default embutida — ela estaria versionada aqui e abriria o disparo de OS
> para quem lesse o repositório. A escolha de 503 em vez de 404: as duas
> escondem o console de quem não tem a senha e nenhuma das duas o abre, então a
> diferença só aparece para o operador que configurou errado — 404 o manda caçar
> o erro na URL, no build ou no proxy; 503 encerra o assunto numa linha.

**O disparo manual não é um segundo caminho de entrada.** O console monta o
corpo com `os_templates.instanciar` — o mesmo que o gerador usa — e chama
`receber_ordem`, a função do `POST /api/v1/ordens`. Fila cheia (**429**), OS
duplicada (409) e banco fora do ar (503) aparecem na tela exatamente como o
gerador os recebe, porque é o mesmo código respondendo. Duas portas de entrada
divergiriam no primeiro ajuste de contrato, e a que ficaria para trás é a que
um humano usa sob pressão.

**Como a pausa chega ao gerador.** O erp-simulator é outro container, e o
central não o para: ele publica um booleano em `GET /api/v1/gerador`, e o
gerador consulta essa rota antes de cada envio (`ESPERA_PAUSA` entre consultas
enquanto a pausa durar). Mandar o central falar com o daemon do Docker exigiria
socket montado, privilégio de administrador da máquina e um acoplamento novo
entre o central e o runtime que o hospeda — tudo para não dispensar medicamento
por alguns minutos. Com o flag, o container segue de pé e volta a produzir no
instante em que o console despausa. A pausa **não é persistida**: restart do
central retoma o automático, porque uma pausa gravada sobreviveria à
apresentação que a motivou e o sintoma seria uma planta em silêncio sem erro em
lugar nenhum.

Pausar **não** bloqueia o disparo manual — pausar é assumir o controle, não
parar a planta.

**Estado ao vivo pelo `/ws`.** A página consome o mesmo WebSocket do dashboard;
nenhum polling novo foi criado. A bancada é desenhada em duas fileiras de
quatro com o corredor da CNC no meio, como no dashboard e pelo mesmo motivo:
"D7 travou" tem que apontar para um lugar na bancada, não para a sétima posição
de uma lista.

---

## Fluxo de uma OS

```
1.  erp-simulator: sorteia 1 das 10 ordens padrão, instancia os_id
    →  POST /api/v1/ordens  →  central-computer
2.  central-computer: atribui slots por categoria/residual disponível
3.  central-computer: planeja rota CNC (serpentina — desce uma fileira, volta pela outra)
4.  central-computer  →  weight-adapter  →  POST /tara  (zera balança para a OS)
5.  central-computer  →  dispenser-adapter  →  POST /executar/carregar  (paralelo por slot)
6.  dispenser-simulator: carrega remédios, reporta "carregado"
7.  central-computer  →  vision-adapter  →  POST /comandos/capturar/dispenser  (paralelo)
    ↳  vision-simulator: escolhe a câmera pelo slot (D1-D4 → esquerda, D5-D8 → direita),
       lê QR/barcode e reporta a leitura ao central com o campo `camera` preenchido
    ↳  central: SKU errado → trava imediata + retry após operador corrigir; falha de leitura → alarme não-bloqueante
8.  Para cada slot na rota CNC:
    a. central-computer  →  cnc-adapter  →  POST /executar/mover
    b. cnc-simulator: interpola posição, reporta "posicionado"
    c. central-computer  →  dispenser-adapter  →  POST /executar/dispensar
    d. dispenser-simulator: dispensa mecanicamente, reporta "dispensado" (contagem)
    e. central-computer  →  vision-adapter  →  POST /comandos/capturar/mesa
       ↳  vision-simulator: a câmera da mesa (a da balança) conta os produtos e
          reporta o resultado ao central via evento
    f. central-computer  →  weight-adapter  →  POST /pesar
       ↳  weight-simulator: lê delta de peso na mesa, reporta desvio em relação ao esperado
    g. Triple Check: compara as 3 fontes (dispenser, câmera mesa, balança)
       • 1 fonte divergente já basta → trava.ativa=True, OS suspensa até
         liberação por admin/supervisor
       • OK → continua para o próximo slot
9.  cnc-adapter  →  POST /executar/homing
10. OS marcada como "concluída" no MySQL
11. Broadcast WebSocket para Dashboard, app de Manutenção e Displays
```

---

## Triple Check

Após cada dispensa de slot, o orquestrador valida **3 fontes independentes**:

| Fonte          | O que mede                               | Evento divergente               |
|----------------|------------------------------------------|---------------------------------|
| Dispenser      | Quantidade contada mecanicamente         | `dispensado` → `quantidade_dispensada` ≠ alvo |
| Câmera da mesa (a da balança) | Contagem por visão computacional | `leitura_mesa_divergencia`      |
| Balança HX711  | Delta de peso (incremento do slot) em g  | `peso_divergencia`              |

Se **qualquer** das 3 fontes divergir além da tolerância, o sistema ativa a **trava de erro**:
- A OS é suspensa (não abortada)
- Um alarme `trava_ativada` (componente `triple_check`) é registrado no banco
- O estado `trava.ativa = True` é transmitido via WebSocket
- O dashboard exibe banner vermelho
- Apenas admin ou supervisor pode liberar via `POST /api/v1/admin/liberar-trava`

O limiar é uma unidade porque os dois erros não custam a mesma coisa: parar uma OS boa custa uma liberação de supervisor, deixar passar uma OS ruim custa medicamento errado — ou na quantidade errada — chegando ao paciente. Se em campo uma fonte específica provar gerar trava sem causa real, o limiar é ajustável por env var no `central-computer`, **sem alterar código**:

| Env var                         | Default | Efeito                                        |
|---------------------------------|---------|-----------------------------------------------|
| `TRIPLE_CHECK_MIN_DIVERGENCIAS` | `1`     | Nº de fontes divergentes que ativa a trava (faixa 1–3; valor fora da faixa cai no default) |

Com limiar acima de 1, a divergência que não trava ainda vira alarme `divergencia_abaixo_do_limiar` no banco — quem elevou o limiar precisa poder auditar o que passou por baixo dele.

**Fonte que não mediu não é fonte que divergiu.** Timeout e falha de leitura da câmera (`leitura_mesa_falha`), ou `erro_sensor` da balança, entram como *fonte indisponível*: são registradas em log, mas não contam para o limiar. Elas não contradizem nada — apenas deixam de confirmar. Contá-las transformaria os ~2% de falha de leitura da câmera em trava por ruído, e trava por ruído é trava desligada em campo. É o mesmo critério já aplicado à câmera do dispenser, onde SKU errado bloqueia e falha de leitura não.

A balança mede o peso **total acumulado** desde a tara. Para validar cada slot individualmente, o sistema calcula o **delta** (incremento desde a última leitura), eliminando o efeito do acúmulo de slots anteriores na mesa. O comando de pesagem carrega duas quantidades — `quantidade_esperada` (alvo da OS, base do peso esperado) e `quantidade_real` (o que o dispenser reportou ter soltado, base do peso que entra na mesa) —, e é da diferença entre as duas que a divergência de peso emerge.

A regra vive em `orchestrator.avaliar_triple_check()`, função pura e testada em `tests/test_orchestrator.py`.

---

## Camada de Visão Computacional

A validação por CV é responsabilidade exclusiva do `vision-adapter` + `vision-simulator`. O `dispenser-simulator` é hardware puro e não faz validação de qualidade.

São **três câmeras físicas**: uma por fileira de dispensers e uma sobre a mesa de coleta.

| Câmera            | `camera`         | Posição            | Cobre  | Valida                          | Outcomes                                                    |
|-------------------|------------------|--------------------|--------|---------------------------------|-------------------------------------------------------------|
| Dispensers Esquerda | `dispenser_esq` | Sobre a fileira esquerda | D1–D4 | QR/DataMatrix/barcode do produto | `leitura_dispenser_ok`, `falha`, `divergencia` (SKU errado) |
| Dispensers Direita  | `dispenser_dir` | Sobre a fileira direita  | D5–D8 | QR/DataMatrix/barcode do produto | `leitura_dispenser_ok`, `falha`, `divergencia` (SKU errado) |
| Mesa (**a da balança**) | `mesa`      | Sobre a mesa de coleta, onde fica o HX711 | mesa | Posição e contagem de unidades | `leitura_mesa_ok`, `falha`, `divergencia` (contagem errada) |

O identificador `mesa` designa a câmera da **balança** — o nome vem de antes e foi mantido porque já existe gravado em `visao_leituras.camera`.

As duas câmeras de dispenser emitem os **mesmos tipos de evento**; o que separa uma da outra é o campo `camera`. É de propósito: o orquestrador e o Triple Check se importam com o resultado da leitura, não com qual lente olhou, e colocar o lado no tipo os obrigaria a conhecer a geometria da bancada.

Qual das duas olha um slot **não é escolha de quem comanda**: o `vision-simulator` deriva o lado do próprio `slot_id` (`camera_do_slot()`, mesma partição de `orchestrator._gerar_posicoes` — ids 1..N/2 à esquerda, N/2+1..N à direita). O central manda só o slot. Deixar o comando escolher a câmera abriria a chance de pedir a leitura de D7 à câmera da esquerda, e o sintoma seria uma divergência de SKU num slot só — indistinguível de um medicamento realmente trocado.

Câmeras de dispenser (SKU errado): **bloqueante** — aciona trava imediatamente. Operador remove o medicamento errado, admin libera a trava, sistema re-escaneia o slot. Repete até confirmar SKU correto. Falha de leitura (câmera não conseguiu ler) é não-bloqueante — gera alarme e continua.
Câmera da mesa: **bloqueante via Triple Check** — faz parte da validação de 3 fontes por slot.

Probabilidades configuráveis por env var (padrão 2% cada). As de dispenser valem para **as duas** câmeras de fileira:
- `PROB_FALHA_LEITURA_DISPENSER` — câmera do dispenser não consegue ler
- `PROB_DIVERGENCIA_DISPENSER` — leu mas SKU errado
- `PROB_FALHA_LEITURA_MESA` — câmera da mesa não detecta produto
- `PROB_DIVERGENCIA_MESA` — detecta mas conta errado

Para simular **uma** câmera com problema, acrescente o sufixo `_ESQ` ou `_DIR` (vale também para `T_SCAN_DISPENSER`); sem o sufixo, vale o valor compartilhado:

```yaml
PROB_FALHA_LEITURA_DISPENSER:      0.02   # as duas fileiras
PROB_FALHA_LEITURA_DISPENSER_DIR:  0.30   # ...menos a da direita, que está suja
```

A câmera da mesa é uma só e não tem lado a sobrescrever.

O estado ao vivo (`GET /estado`) traz as três em `visao`: `camera_dispenser_esq`, `camera_dispenser_dir` e `camera_mesa`. Dashboard e app de manutenção mostram as três — no dashboard, cada câmera de dispenser fica do lado do corredor que ela cobre, para casar com o painel de slots.

---

# Modo apresentação: demo sem imprevisto

Dois controles globais, no `.env`, que valem para o central e para os quatro
simuladores de uma vez. Antes deles, zerar as falhas para uma apresentação
significava editar seis probabilidades espalhadas por três serviços e recriar
containers — e religá-las depois, para mostrar o tratamento de erro, era o mesmo
trabalho pelo caminho inverso.

```bash
# .env
MODO_APRESENTACAO=1     # planta determinística: nenhuma falha aleatória
FATOR_VELOCIDADE=2.0    # metade da velocidade, para explicar cada etapa
```

```bash
docker compose up -d    # não precisa rebuild: são env vars
```

## `MODO_APRESENTACAO`

Ligado, zera **toda** probabilidade de falha aleatória, independentemente das
variáveis individuais:

| o que é zerado | serviço |
|---|---|
| `PROB_ERRO_MECANICO` (atolamento na carga e na dispensa) | dispenser-sim |
| `PROB_FALHA_LEITURA_DISPENSER` e `PROB_DIVERGENCIA_DISPENSER`, **inclusive** os overrides `_ESQ`/`_DIR` | vision-sim |
| `PROB_FALHA_LEITURA_MESA` e `PROB_DIVERGENCIA_MESA` | vision-sim |
| `PROB_ERRO_SENSOR` (HX711) | weight-sim |
| `RUIDO_G` — o σ do ruído da célula de carga | weight-sim |

`RUIDO_G` entra na lista porque é fonte de falha de verdade, não enfeite: com
σ=2 g contra um esperado de 100 g, a tolerância de 5% fica a 2,5σ e uma OS de
vários slots tem alguns por cento de chance de uma `peso_divergencia` sem causa
— ou seja, de uma trava do Triple Check no meio da explicação.

O que o modo **não** desliga é a detecção. Com o ruído zerado, a balança segue
divergindo quando o dispenser solta menos unidades do que o esperado; as câmeras
seguem acusando SKU errado. O modo tira o acaso, não a capacidade de mostrar o
Triple Check.

Cada serviço registra no boot em que modo está:

```
[DISP-SIM] WARNING MODO APRESENTACAO LIGADO — nenhuma falha mecânica aleatória será emitida
[VISION-SIM] WARNING MODO APRESENTACAO LIGADO — nenhuma falha nem divergência aleatória ...
```

E o estado ao vivo publica os dois valores, para o painel poder mostrá-los:

```json
{ "modo_apresentacao": true, "fator_velocidade": 2.0 }
```

## `FATOR_VELOCIDADE`

Multiplica todos os **tempos simulados** — `T_CARGA_UNID`, `T_DISPENSA_UNID`,
`T_SCAN_DISPENSER` (e os `_ESQ`/`_DIR`), `T_SCAN_MESA`, `T_LEITURA` e `T_TARA`.
Faixa 0.1–10; fora dela, cai em 1.0 com aviso no log.

| valor | efeito |
|---|---|
| `0.5` | demo no **dobro** da velocidade |
| `1.0` | tempos reais (padrão) |
| `2.0` | **metade** da velocidade, para narrar cada etapa |

Duas coisas não seguem a multiplicação direta, e as duas por motivo:

- **`VEL_MM_S` é dividido.** Velocidade é o inverso de tempo: fator 2.0
  ("metade da velocidade") tem que produzir **menos** mm/s. O piso de duração de
  um movimento escala junto, para que um trajeto curto não fique preso em 1 s
  com o resto da célula desacelerado.
- **`INTERVALO` (cadência de publicação da posição da CNC) não escala.** É
  telemetria, não tempo físico: mantendo a cadência, desacelerar dá mais pontos
  de trajetória, que é o que se quer na tela.

**Os `TIMEOUT_*` do orquestrador escalam junto, por `max(1, FATOR_VELOCIDADE)`.**
Sem isso, desacelerar a demo aborta a OS por `timeout_carregamento` — com o log
culpando um dispenser que fez exatamente o que se pediu. O `max` existe porque
acelerar não ganha nada com timeout menor (o timeout é teto de espera por um
adapter travado, não parte do ciclo) e encolheria a folga para o custo fixo —
retentativa de `_post`, MySQL, rede — que não escala com fator nenhum.

`INTERVALO_OS` do erp-simulator **não** escala: é a cadência com que se quer
ordens, decisão de apresentação. Desacelerando muito, a fila enche e o
`MAX_FILA_OS` responde 429, como deve.

## Injeção de falha sob demanda

Sem ela, o Triple Check e a trava — o diferencial técnico do sistema — só
aparecem quando o sorteio colabora. Na apresentação isso dá duas situações
ruins: ou a falha não acontece e o recurso não é mostrado, ou acontece no meio
de outra explicação.

No console você arma **um** gatilho: tipo + slot. Ele dispara na próxima
ocorrência aplicável e **se desarma sozinho**. Cinco tipos:

| Tipo | Efeito | Trava? |
|---|---|---|
| `sku_dispenser` | a câmera da fileira lê um SKU que não bate | **sim** — e entra no laço de re-scan |
| `falha_leitura_dispenser` | a câmera não consegue ler o código | não — alarme, a OS continua |
| `divergencia_mesa` | a câmera da balança conta uma a menos | **sim** |
| `divergencia_peso` | o HX711 mede fora da tolerância | **sim** |
| `falha_mecanica_dispenser` | o dispenser solta uma unidade a menos | **sim**, pela balança |

O último é o mais completo de mostrar: a falha é FÍSICA, e a balança a detecta
sozinha — uma unidade de 15 já é 6,7% contra uma tolerância de 5%. A trava nasce
de um erro real, não de um sensor mentindo.

**Funciona com `MODO_APRESENTACAO` ligado, e é essa a combinação da banca.** O
modo desliga o acaso; a injeção liga o que você escolheu. São controles
independentes.

**Nada persiste.** Restart do central desarma. E o log diz, de forma
inequívoca, quando uma falha foi provocada — no central
(`[INJECAO] ⚡ INJETADA — …`), no simulador (`⚡ INJEÇÃO ARMADA (demonstração)`)
e no próprio evento, que carrega `falha_injetada: true`. Analisar os logs depois
sem conseguir separar falha real de falha provocada seria pior que não ter a
feature.

## Reset rápido da planta

Repetir a demonstração exigia `docker compose down -v` — apaga o banco e leva
minutos. O botão **Resetar** do console devolve a bancada ao estado de boot em
segundos:

- esvazia a fila (as OS que esperavam são **canceladas** no banco, não deixadas
  em "aguardando" para sempre);
- libera a trava e desarma a falha injetada;
- manda `cmd_limpar` nos 8 slots e **espera confirmação** — limpeza física, não
  só memória;
- zera a balança (tara) e leva a CNC para HOME;
- opcionalmente apaga o histórico (`ordens`, `os_itens`, `dispensas`,
  `cnc_eventos`, `alarmes`, `leituras_sensores`, `visao_leituras`). **O default
  é preservar**: quase sempre se quer a bancada limpa com o histórico de pé.

O catálogo de medicamentos, os usuários e o log de manutenção **nunca** são
apagados — não são histórico de operação.

> **O reset recusa (409) enquanto houver OS em execução.** O orquestrador é um
> loop único parado no meio de um ciclo de CNC; resetar por cima mandaria limpar
> um slot que está dispensando (409), zeraria a balança no meio de uma pesagem e
> abortaria a OS por uma divergência que ninguém provocou. Com a trava ativa,
> libere-a primeiro (o botão está ao lado) e espere a OS fechar.

Slot que não confirmar a limpeza aparece separado no relatório — é o único
desfecho que manda alguém conferir a bancada.

## Histórico de demonstração

> ⚠️ **Dado fabricado.** Nada disto aconteceu na planta. Existe para o dashboard
> e o app de manutenção não abrirem vazios numa instalação nova — tela em branco
> passa a impressão de sistema que nunca rodou.

O botão **Semear** cria N ordens concluídas nos últimos D dias, com dispensas,
alarmes já resolvidos e curvas de temperatura. Tudo coerente: as OS saem das dez
ordens padrão, o SKU vem do catálogo real, as quantidades batem com os
templates, os horários caem no turno (7h–19h) e a duração cresce com o número de
slots.

**Todo `os_id` semeado começa com `DEMO-`**, então dado de demonstração e
operação real nunca se confundem — em tela, em log ou em consulta SQL.

Ele **nunca roda sozinho**: não há chamada no boot, no `init_db` nem em
healthcheck, e há um teste que varre `main.py` por AST para garantir que o único
chamador continua sendo a rota do console. Para tirá-lo, use o reset com
"apagar também o histórico".

## Pré-voo: está tudo de pé?

**http://localhost:8000/console/prevoo** — a tela que se lê cinco minutos antes
de apresentar. Responde "pronto" ou "N itens impedem a apresentação" em cima, e
abaixo a lista item a item, em verde, amarelo ou vermelho.

O que ela confere:

| grupo | itens |
|---|---|
| Serviços | os 13 do compose — 11 sondados em `/ping` (ou `/`), o central por dentro, o `erp-simulator` como informação (não tem porta) |
| Banco | MySQL conectado, schema completo, catálogo populado, os 8 slots em `dispenser_estado` |
| Ordens | as 10 ordens padrão válidas contra o catálogo; ocupação da fila |
| Célula | trava do Triple Check, posição da CNC (em HOME?), OS em execução, alarmes abertos |
| Modo | apresentação ou realista, fator de velocidade, falha armada, clientes WebSocket |

**Todo item vermelho ou amarelo diz o que fazer** — `docker compose up -d X`,
"libere a trava no botão do topo", "reinicie o central: `init_db` cria o que
falta". É o que separa a tela de um relatório, e há um teste que varre todos os
itens cobrando isso.

**Amarelo não derruba o veredito.** Modo apresentação ligado, CNC fora de HOME
ou fila cheia são informações que o operador precisa ter, não impedimentos —
uma tela que responde "não" sempre deixa de ser lida.

**Ela responde em ~2 s mesmo com serviço morto.** As sondas correm em paralelo
com timeout curto: um serviço fora produz um item vermelho, não uma página
travada — que é justamente o caso em que ela é mais necessária.

## Tela de necessidades (app de manutenção)

A **primeira aba** de http://localhost:8051 — a lista do que precisa de alguém
agora, para não ser preciso visitar as dez abas para descobrir se há algo a
fazer.

A ordem é a do **impacto**, não a da gravidade abstrata:

| # | o quê | por quê aqui |
|---|---|---|
| 1 | trava do Triple Check | bloqueia a produção **agora** — o orquestrador é um loop único |
| 2 | fila no teto | a planta já está recusando ordens com 429 |
| 3 | alarmes abertos, agrupados por fonte | já falhou, ninguém fechou |
| 4 | componentes acima do limiar | vai falhar, ainda não falhou |
| 5 | dispensers com resíduo parado | estoque imobilizado |
| 6 | OS em erro nas últimas horas | já acabou — é diagnóstico |

**Cada linha tem um botão que leva à aba onde se resolve.** É o que a torna um
ponto de partida e não mais um relatório.

**Sem pendência, a tela diz isso com todas as letras** — e nunca diz isso com o
banco fora: nesse caso ela avisa que a lista está incompleta, porque trava,
fila e resíduo continuam corretos mas alarmes, componentes e OS em erro não
puderam ser lidos.

Os limiares (65 °C, 80% de desgaste) são os mesmos com que as outras abas já
pintam de vermelho — um limiar próprio faria esta tela listar um componente que
a aba de temperaturas mostra em verde.

> **Um endpoint agregado, e não seis chamadas.** Esta é a aba que fica aberta, e
> o app repolla a cada 5 s: seis requisições por tique por gestor conectado é o
> custo que `GET /manutencao/necessidades` existe para evitar. É a mesma regra do
> ponto único de I/O do dashboard.

---

# Build e deploy

Toda a stack sobe por Docker Compose: **13 serviços**, uma rede
(`apsen-net`) e um volume (`mysql_data`). Não há nada para instalar no host
além do Docker — nem Python, nem Node, nem MySQL.

## Pré-requisitos

- Docker Engine ≥ 24 e Docker Compose v2 (`docker compose version` — subcomando, sem hífen)
- ~2 GB RAM
- Python 3.10+ (para os testes e para o painel de bancada)

## Quickstart (primeira subida)

```bash
git clone <url-do-repo> && cd valoryapsen
```

Crie o `.env` (o modelo completo está [logo abaixo](#o-env-da-raiz)) e preencha
as **cinco obrigatórias**. Depois:

```bash
docker compose up -d --build
```

Leva alguns minutos na primeira vez (build das imagens + seed do MySQL). Quando
terminar:

| Interface | URL |
|---|---|
| Dashboard | http://localhost:8050 |
| App de manutenção | http://localhost:8051 |
| Console de operação | http://localhost:8000/console |
| **Pré-voo** (está tudo de pé?) | http://localhost:8000/console/prevoo |
| API + Swagger | http://localhost:8000/docs |

A resposta rápida para "subiu certo?" é o **pré-voo**: ele confere os 13
serviços, o banco, o catálogo, os 8 slots, as 10 ordens padrão e o modo em
vigor, item a item, em segundos.

## O `.env` da raiz

O `docker-compose.yml` usa `${VAR:?...}` nas variáveis obrigatórias: se faltar
alguma, o compose **recusa subir** e diz qual é. Nenhum segredo tem default.

```bash
# gere a SECRET_KEY (não reaproveite exemplo de README):
python -c "import secrets; print(secrets.token_hex(32))"
```

```bash
cat > .env <<'FIM'
# ── Assinatura dos JWT ───────────────────────────────────────────────────────
# OBRIGATÓRIO. Gere com:
#   python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=

# "dev" tolera SECRET_KEY fraca, só com aviso no log. Qualquer outro valor
# (inclusive vazio → prod) trata segredo fraco como erro e IMPEDE o boot.
APSEN_ENV=prod

# ── MySQL ────────────────────────────────────────────────────────────────────
# OBRIGATÓRIOS: MYSQL_ROOT_PASS e MYSQL_PASS. Lidos na PRIMEIRA subida (criação
# do volume mysql_data); trocar depois exige `docker compose down -v`, que
# apaga os dados.
MYSQL_ROOT_PASS=
MYSQL_USER=apsen
MYSQL_PASS=
MYSQL_DB=apsen_db

# ── Usuários seed do app de manutenção ──────────────────────────────────────
# OBRIGATÓRIAS. Criadas só na primeira inicialização do banco. TROQUE AMBAS
# após o primeiro login, pelo próprio app (aba Usuários) — ficam em claro aqui.
SEED_ADMIN_SENHA=
SEED_MANUT_SENHA=

# ── Geometria da célula ──────────────────────────────────────────────────────
NUM_SLOTS=8

# ── Origens de browser autorizadas a chamar o central ────────────────────────
# Ajuste ao publicar fora da máquina local. `*` não é aceito no central.
CORS_ORIGINS=http://localhost:8050,http://localhost:8051

# ── Modo de demonstração ─────────────────────────────────────────────────────
# OPCIONAIS. 1 zera toda falha aleatória dos simuladores; o fator multiplica os
# tempos simulados (0.5 = dobro da velocidade, 2.0 = metade). Ver "Modo
# apresentação".
MODO_APRESENTACAO=0
FATOR_VELOCIDADE=1.0

# ── Console de operação do central (http://localhost:8000/console) ───────────
# OPCIONAL e sem default embutido: vazia = console DESABILITADO (toda rota
# /console* responde 503). Senha PRÓPRIA, independente do login do app de
# manutenção.
CONSOLE_SENHA=
CONSOLE_SESSAO_HORAS=8
FIM
```

Preencha as **cinco obrigatórias** antes de seguir:
`SECRET_KEY`, `MYSQL_ROOT_PASS`, `MYSQL_PASS`, `SEED_ADMIN_SENHA`, `SEED_MANUT_SENHA`.
As demais já têm default utilizável.

## Esperar tudo ficar saudável

Todo serviço tem healthcheck e as dependências usam
`depends_on: condition: service_healthy` — a ordem se resolve sozinha, mas o
MySQL leva ~30 s no primeiro boot.

```bash
docker compose ps                          # STATUS deve ler "healthy" em todos
docker compose logs -f central-computer
```

Está pronto quando aparecer:

```
MySQL conectado e schema verificado. Medicamentos OK.
[TEMPLATES] 10 ordens padrão válidas contra o catálogo.
Computador Central APSEN v3.1 iniciado.
```

## Atalhos do Makefile

O `Makefile` embrulha os comandos mais usados. `make help` lista todos.

| Comando | O que faz |
|---|---|
| `make up` | `docker compose up -d` (recusa se faltar o `.env`) |
| `make build` | build incremental das imagens |
| `make rebuild` | **`down -v`** + build sem cache + `up -d` — ⚠️ **APAGA O BANCO** |
| `make down` | derruba containers e rede (**mantém** o banco) |
| `make restart` | `down` + `up -d` (recria os containers; mantém o banco) |
| `make ps` / `make status` | estado dos containers |
| `make logs` | log agregado de todos |
| `make log-central`, `make log-erp`, `make log-cnc-sim`, … | log de um serviço só |
| `make shell-central` / `make shell-mysql` | shell dentro do container |
| `make config` | valida o `docker-compose.yml` sem subir nada |
| `make test` | roda a suíte (sem Docker, sem MySQL) |
| `make lint` | pyflakes em todos os serviços |

> **`make rebuild` não é um `make build` mais forte.** Ele começa com
> `docker compose down -v`, e o `-v` remove o volume `mysql_data` — catálogo,
> usuários, ordens e histórico vão junto. Para reconstruir sem perder o banco,
> use `docker compose build --no-cache && docker compose up -d`.

## Rebuild: o que recriar depois de mudar o quê

A pergunta que decide é **se o banco pode ser perdido**. `down` sem `-v`
preserva o volume; com `-v`, apaga.

| Mudou | Comando | Banco |
|---|---|---|
| Só código Python | `docker compose up -d --build` | preservado |
| Um serviço só | `docker compose up -d --build central-computer` | preservado |
| `requirements.txt` | `docker compose build --no-cache <serviço>` + `up -d` | preservado |
| `.env` (variável de runtime) | `docker compose up -d` | preservado |
| `.env` (credencial do MySQL) | `docker compose down -v` + `up -d --build` | **APAGADO** |
| Schema quebrado / versão antiga | `docker compose down -v` + `up -d --build` | **APAGADO** |
| Nome ou `container_name` de serviço | `docker compose down` + `up -d --build` | preservado |

As credenciais do MySQL (`MYSQL_ROOT_PASS`, `MYSQL_PASS`, `MYSQL_USER`,
`MYSQL_DB`) são lidas **uma única vez**, na criação do volume: trocá-las depois
exige `down -v`. O `SECRET_KEY` e o resto não — são lidos em todo startup.

> **Renomeou um serviço?** `docker compose up -d` deixa o container antigo
> órfão, porque o `container_name` mudou junto. Use `docker compose down` antes,
> ou `up -d --remove-orphans`. Nenhum dos dois toca no volume do MySQL.

O que **não** precisa de rebuild: `console.html`, `console_login.html` e
`console_prevoo.html` são lidos do disco a cada requisição, então editá-los vale
com um `docker compose restart central-computer` — ou, dentro do container, na
hora.

## Derrubar

```bash
docker compose stop     # pausa, mantém banco e containers
docker compose down     # remove containers e rede, MANTÉM o banco (volume)
docker compose down -v  # remove tudo, inclusive o banco
```

## Problemas comuns

| Sintoma | Causa | O que fazer |
|---|---|---|
| `compose: variable is not set` | falta variável no `.env` | a mensagem diz qual — o `:?` é intencional |
| central sai no boot com `ConfiguracaoInsegura` | `SECRET_KEY` default/curta fora de `APSEN_ENV=dev` | gerar chave de 64 hex |
| central em restart loop, log `SchemaInvalido` | banco de versão anterior | `docker compose down -v` e subir de novo |
| `MySQL não disponível (n/30)` | MySQL ainda subindo | normal no primeiro boot; espera ~30 s |
| dashboard em branco | central ainda não `healthy` | `docker compose ps` |
| `/console` responde 503 | `CONSOLE_SENHA` vazia | definir no `.env` e reiniciar o central |
| OS recusada com 429 | fila no teto (`MAX_FILA_OS=5`), possivelmente com trava ativa | ver `/api/v1/trava` e liberar |
| display OFFLINE no painel | outro processo detém a COM | fechar Arduino IDE / monitor serial; não usar `APSEN_RELOADER=1` com hardware |

---

# Roteiro de demonstração — passo a passo

Com a stack de pé (ver [Build e deploy](#build-e-deploy)), este é o caminho
que mostra o sistema inteiro funcionando, na ordem em que ele se explica.

## 1. Conferir que está vivo

```bash
curl http://localhost:8000/ping                 # {"status":"ok",...}
curl http://localhost:8000/estado               # cnc, 8 dispensers, visão, peso, trava
curl http://localhost:8000/api/v1/fila          # {"tamanho":0,"capacidade":5,...}
curl http://localhost:8000/medicamentos | head  # 96 medicamentos do seed
```

## 2. Ver uma OS rodando de ponta a ponta

O `erp-simulator` posta uma OS a cada 90 s sozinho. Para não esperar:

**Pelo console** (recomendado) — abra http://localhost:8000/console, entre com
`CONSOLE_SENHA`, escolha uma das 10 ordens padrão e dispare. Deixe o dashboard
aberto em outra aba: a CNC anda, os slots carregam, a câmera lê, a balança pesa.

**Por curl:**

```bash
curl -X POST http://localhost:8000/api/v1/ordens \
  -H "Content-Type: application/json" \
  -d '{"os_id":"OS-TESTE-001","descricao":"Teste manual","categoria":"snc",
       "medicamentos":[{"medicamento":"ALOIS 10MG","sku":"ALOIS 10MG CX C/7 CP","categoria":"snc","quantidade":5}]}'
```

Respostas de contrato, nenhuma retentada pelo gerador:
`200` aceita · `409` `os_duplicada` · `429` `fila_cheia` · `503` `persistencia_indisponivel`.

Acompanhar:

```bash
docker compose logs -f central-computer | grep ORCH   # cada etapa do ciclo
curl http://localhost:8000/os/OS-TESTE-001            # detalhe + itens
curl "http://localhost:8000/dispensas?os_id=OS-TESTE-001"
```

## 3. Testar a trava do Triple Check

É o comportamento mais importante de validar: **1 fonte divergente já para a
OS**. Os simuladores injetam divergência por probabilidade, mas não é preciso
esperar o sorteio: arme a falha no console e ela sai na próxima ocorrência
aplicável (ver [Injeção de falha sob demanda](#injeção-de-falha-sob-demanda)).

```bash
curl http://localhost:8000/api/v1/trava     # {"ativa":true,"os_id":...,"motivo":...}
```

Liberar (a OS retoma de onde parou), pelos dois portões:

```bash
# a) app de manutenção — JWT com role admin
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","senha":"<SEED_ADMIN_SENHA>"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['token'])")

curl -X POST http://localhost:8000/api/v1/admin/liberar-trava \
  -H "Authorization: Bearer $TOKEN"

# b) console de operação — botão "Liberar trava", com confirmação em dois passos
```

## 4. Pausar a geração automática

Útil para testar sem OS novas chegando no meio:

```bash
curl http://localhost:8000/api/v1/gerador      # estado do interruptor
```

Para pausar/retomar, use o botão no `/console` — a escrita é só de lá.

## 5. Testes automatizados

Não precisam de Docker nem de MySQL: chamam função direto e usam stub.

```bash
python -m pip install -r tests/requirements-dev.txt
python -m pytest -q                          # tudo
python -m pytest tests/test_orchestrator.py -v
python -m pytest tests/test_seguranca.py tests/test_auth.py -v
python -m pytest tests/test_painel_ordens.py -v   # painel de bancada
python -m pytest tests/test_compose.py -v         # valida o docker-compose.yml
```

A fábrica `carregar_painel` (em `tests/conftest.py`) importa o `app.py` do painel
por caminho, com o banco em `tmp_path` e três bordas dubladas: `serial` (nenhum
teste toma posse de um USB), `requests` e `central_client._get` (onde os testes
encenam o central respondendo — ou não respondendo).

---

# Painel de bancada (`painel_operador/`)

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
docker compose up -d --build          # na raiz; ver "Build e deploy"
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
docker compose up -d --build                    # raiz; confira em /console/prevoo
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
| display → backend | 9 `cmd`: `ping`, `get_ordens`, `get_catalogo`, `get_operadores`, `get_dispensers`, `validar_operador`, `set_status`, `sync_dispensers`, `set_dispenser_med` |
| backend → display | 7 tags de `resp`: `pong`, `ordens`, `catalogo`, `operadores`, `dispensers`, `operador`, `ok` |
| display → backend | 3 `event` (fire-and-forget): `historico`, `desvio`, `ordem_concluida` |
| backend → display | 2 `push` (não solicitados): `ordem_status`, `dispensers` |

Esse contrato é escrito **à mão em três lugares e duas linguagens** — firmware
(C++), backend e simulador (Python). Divergir não quebra nada visivelmente: o
display manda um `cmd` que ninguém trata e espera o timeout, ou o backend
responde uma tag que o display não aguarda e a linha é descartada em silêncio.
Os dois sintomas são "tela vazia, sem erro no log".

Por isso `tests/test_protocolo_serial.py` compara as três cópias e ainda checa,
em runtime, que o backend responde a cada comando — inclusive que **nenhuma
resposta passa dos 4096 bytes** do `s2_buf` do firmware, porque a linha que não
couber é descartada sem erro.

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

**Perfis de acesso.** `Admin`, `PCP`, `PCM` e `Operador` — a matriz está em
`PERMISSOES`, no topo do `app.py`. `Operador` não tem acesso web (só ao display).

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
outro número e deixa a antiga como entrada fantasma (`Status: Unknown`). O
backend varre todas as portas e acha sozinho — mas se for **regravar o
firmware**, confira a porta atual antes.

**Processos órfãos.** Fechar o terminal nem sempre mata o Python. Se algo
estranho acontecer com a porta, o primeiro palpite deve ser processo duplicado,
não bug: `tasklist | findstr python`.

---

## Interfaces

| Interface   | URL                           | Acesso           |
|-------------|-------------------------------|------------------|
| Dashboard   | http://localhost:8050         | Público          |
| Manutenção  | http://localhost:8051         | JWT              |
| Console     | http://localhost:8000/console | `CONSOLE_SENHA`  |
| Pré-voo     | http://localhost:8000/console/prevoo | `CONSOLE_SENHA` |
| API Central | http://localhost:8000         | REST/WS          |
| Docs API    | http://localhost:8000/docs    | Swagger          |
| Painel de bancada | http://localhost:5000   | PIN (hash)       |
| API do painel     | http://localhost:5000/api/*   | `APSEN_API_TOKEN` |

O console fica **desabilitado** (503) enquanto `CONSOLE_SENHA` não estiver no `.env`.

**Usuários padrão (app de Manutenção):**

| Usuário  | Senha                        | Perfil      |
|----------|------------------------------|-------------|
| `admin`  | `SEED_ADMIN_SENHA` do `.env` | admin       |
| `manut1` | `SEED_MANUT_SENHA` do `.env` | manutencao  |

> **Troque as duas após o primeiro login**, pelo próprio app (aba 👥 Usuários).
> As senhas seed ficam em arquivo, em claro, e são lidas apenas na criação do
> banco — depois disso o `.env` só serve para lembrar quem tem acesso.

### Segredos

Nada de segredo entra no repositório. O `docker-compose.yml` lê tudo do `.env`
(ignorado pelo git) e **falha ao subir** se faltar algo:

| Variável | Para quê |
|----------|----------|
| `SECRET_KEY` | assina os JWT. Gere com `python -c "import secrets; print(secrets.token_hex(32))"` |
| `MYSQL_ROOT_PASS`, `MYSQL_PASS` | credenciais do banco (lidas na criação do volume) |
| `SEED_ADMIN_SENHA`, `SEED_MANUT_SENHA` | senhas iniciais do app de manutenção |
| `APSEN_ENV` | `prod` (default) recusa segredo fraco; `dev` só avisa |
| `CONSOLE_SENHA` | senha do console de operação. **Opcional** — vazia = console desabilitado (503), nunca uma senha default |
| `APSEN_SECRET` | chave de sessão do painel de bancada. **Sem default** — sem ela o painel não sobe, como o central sem `SECRET_KEY` |
| `APSEN_API_TOKEN` | token do header `X-API-Token` das rotas `/api/*` do painel. **Sem default** — ausente, elas respondem 503 e o resto do painel sobe |

O central **se recusa a subir** com `SECRET_KEY` default, vazia ou com menos de
32 caracteres, a não ser com `APSEN_ENV=dev`. O valor default antigo está no
histórico público deste repositório: com ele qualquer pessoa forja um token
`role=admin`, libera a trava do Triple Check e altera usuários.

---

## Endpoints principais

```
GET  /ping                               → health check
GET  /estado                             → estado completo em memória
GET  /os/ativa                           → OS em execução (ou a próxima da fila)
GET  /os/historico                       → histórico de OS
GET  /medicamentos                       → catálogo (96 medicamentos APSEN)
GET  /api/v1/ordens/templates            → as 10 ordens padrão (+ diagnóstico)
GET  /dispensers/estado                  → estado dos 8 slots no DB
GET  /alarmes                            → alarmes ativos/resolvidos
WS   /ws                                 → push de estado em tempo real

POST /api/v1/ordens                      → recebe nova OS (erp-simulator)
                                           409 os_duplicada | 429 fila_cheia |
                                           503 persistencia_indisponivel
GET  /api/v1/fila                        → ocupação da fila (backpressure)
GET  /api/v1/gerador                     → flag de pausa do erp-simulator
                                           (ele consulta antes de cada envio)
POST /api/v1/eventos/dispenser           → recebe eventos (dispenser-adapter)
POST /api/v1/eventos/cnc                 → recebe eventos (cnc-adapter)
POST /api/v1/eventos/visao               → recebe resultados de CV (vision-adapter)
POST /api/v1/eventos/peso                → recebe leituras de peso (weight-adapter)

POST /auth/login                         → gera JWT
GET  /auth/me                            → usuário autenticado
POST /manutencao/dispensers/{id}/limpar  → limpa slot
GET  /manutencao/alarmes                 → alarmes (autenticado)
PUT  /manutencao/alarmes/{id}/resolver   → resolve alarme
GET  /manutencao/log                     → log de manutenção
POST /api/v1/admin/liberar-trava         → libera trava de erro (admin/supervisor)
GET  /api/v1/visao/historico             → histórico de leituras de CV
GET  /api/v1/relatorio/os/{os_id}?formato=csv|xlsx
                                         → relatório da OS (só header Authorization;
                                           o app de manutenção baixa server-side
                                           e entrega ao navegador)
```

O console de operação tem rotas próprias, **fora do Swagger**
(`include_in_schema=False`) e atrás do cookie de sessão:

```
GET  /console                            → a página (sem sessão: 303 → /console/login)
GET  /console/login                      → tela de senha
POST /console/login                      → confere CONSOLE_SENHA, emite o cookie
POST /console/logout                     → encerra a sessão
POST /console/api/disparar               → dispara uma ordem padrão
                                           (chama o MESMO receber_ordem do
                                           POST /api/v1/ordens)
POST /console/api/gerador                → pausa/retoma o erp-simulator
POST /console/api/liberar-trava          → libera a trava do Triple Check
GET  /console/api/injecao                → tipos de falha + o gatilho armado
POST /console/api/injecao                → arma a próxima falha (tipo + slot)
POST /console/api/injecao/desarmar       → desarma sem consumir (409 se não havia)
POST /console/api/reset                  → reset da planta (409 com OS em execução)
POST /console/api/seed                   → histórico de DEMONSTRAÇÃO (dado fabricado)
GET  /console/prevoo                     → tela de pré-voo
GET  /console/api/prevoo                 → o relatório de pré-voo, item a item
```

```
GET  /manutencao/necessidades            → lista priorizada do que precisa de ação
                                           (agregado: evita 6 chamadas por tique)
```

---

## Estrutura do repositório

```
valoryapsen/
├── README.md               # ESTE arquivo — documentação única
├── TASKS.md                # backlog: o que falta fazer
├── CLAUDE.md               # decisões de arquitetura e armadilhas (lido pelo Claude Code)
├── central-computer/       # Orquestrador + API central
│   ├── main.py             # FastAPI + handlers de eventos (dispenser, CNC, visão, peso)
│   ├── orchestrator.py     # Lógica de negócio (fila, atribuição, rota, Triple Check)
│   ├── database.py         # MySQL (11 tabelas, 96 medicamentos)
│   ├── os_templates.py     # As 10 ordens padrão (fonte única; o gerador lê daqui)
│   ├── console.py          # Console de operação: senha, sessão HMAC, flag de pausa
│   ├── console.html        # Página do console (HTML+CSS+JS, sem build nem framework)
│   ├── console_login.html  # Tela de senha do console
│   ├── config.py           # Settings via env vars
│   ├── auth.py             # JWT + bcrypt
│   ├── requirements.txt
│   └── Dockerfile
├── dispenser-adapter/      # Bridge HTTP: central ↔ dispenser-simulator (:8100)
├── cnc-adapter/            # Bridge HTTP: central ↔ cnc-simulator (:8101)
├── vision-adapter/         # Bridge HTTP: central ↔ vision-simulator (:8102)
├── weight-adapter/         # Bridge HTTP: central ↔ weight-simulator (:8103)
├── dispenser_simulator/    # Simula os 8 dispensers físicos (:8201)
├── cnc_simulator/          # Simula firmware da CNC (:8200)
├── vision-simulator/       # Simula as 3 câmeras (2 de fileira + mesa) (:8202)
├── weight-simulator/       # Simula célula de carga HX711 sob a mesa CNC (:8203)
├── erp-simulator/          # O ERP que emite as OS: 1 das 10 padrão por ciclo (sem porta)
├── dashboard/              # Plotly Dash read-only :8050
├── manut_web/              # Plotly Dash manutenção e operação :8051
├── painel_operador/        # Painel de bancada — FORA do Docker
│   ├── backend/            # Flask + SQLite + ponte serial (:5000)
│   ├── firmware/           # PlatformIO — display ESP32 7"
│   └── iniciar_backend.bat
├── mysql/init.sql          # Só charset/collation; o schema vem do database.py
├── tests/                  # pytest — sem Docker, sem MySQL
├── docker-compose.yml
├── Makefile
└── .gitignore
```

---

## Variáveis de ambiente

### Central e simuladores (via `.env` + `docker-compose.yml`)

> **`MODO_APRESENTACAO` e `FATOR_VELOCIDADE` mandam por cima.** Com
> `MODO_APRESENTACAO=1`, toda coluna de probabilidade desta tabela vale zero —
> inclusive os overrides `_ESQ`/`_DIR` e o `RUIDO_G`. Com `FATOR_VELOCIDADE`
> diferente de 1, todo `T_*` é multiplicado por ele, `VEL_MM_S` é dividido, e os
> `TIMEOUT_*` do central escalam por `max(1, fator)`. Ver
> "[Modo apresentação](#modo-apresentação-demo-sem-imprevisto)".

| Variável                        | Padrão                          | Serviço            |
|---------------------------------|---------------------------------|--------------------|
| `MODO_APRESENTACAO`             | `0` — `1` zera TODA falha aleatória | **central + 4 simuladores** |
| `FATOR_VELOCIDADE`              | `1.0` — multiplica os tempos simulados (faixa 0.1–10) | **central + 4 simuladores** |
| `SECRET_KEY`                    | (obrigatória, vem do `.env`)    | central-computer   |
| `APSEN_ENV`                     | `prod` (`dev` afrouxa o segredo) | central-computer  |
| `CORS_ORIGINS`                  | `http://localhost:8050,http://localhost:8051` | central-computer |
| `AUTH_CACHE_TTL_S`              | `30`s de cache da revalidação   | central-computer   |
| `MYSQL_POOL_MAX`                | `8` conexões guardadas (faixa 1–64) | central-computer |
| `CONSOLE_SENHA`                 | (vazia) — vazia DESABILITA o console (503) | central-computer |
| `CONSOLE_SESSAO_HORAS`          | `8`h de cookie (faixa 0.25–24)  | central-computer   |
| `NUM_SLOTS`                     | `8` dispensers (par; 2 fileiras) | **todos** — central, simuladores, dashboard e erp-simulator |
| `DISPENSER_ADAPTER_URL`         | `http://dispenser-adapter:8100` | central-computer   |
| `CNC_ADAPTER_URL`               | `http://cnc-adapter:8101`       | central-computer   |
| `VISION_ADAPTER_URL`            | `http://vision-adapter:8102`    | central-computer   |
| `WEIGHT_ADAPTER_URL`            | `http://weight-adapter:8103`    | central-computer   |
| `TIMEOUT_CARREGAMENTO`          | `180`s                          | central-computer   |
| `TIMEOUT_POSICIONAMENTO`        | `120`s                          | central-computer   |
| `TIMEOUT_DISPENSA`              | `120`s                          | central-computer   |
| `TIMEOUT_VISAO_DISPENSER`       | `30`s                           | central-computer   |
| `TIMEOUT_VISAO_MESA`            | `30`s                           | central-computer   |
| `TIMEOUT_PESO`                  | `15`s                           | central-computer   |
| `TRIPLE_CHECK_MIN_DIVERGENCIAS` | `1` (faixa 1–3)                 | central-computer   |
| `MAX_FILA_OS`                   | `5` OS esperando (faixa 1–1000) | central-computer   |
| `BROADCAST_MIN_INTERVALO_MS`    | `500`ms entre broadcasts periódicos | central-computer |
| `CNC_AMOSTRAGEM_MOVENDO`        | `0` (não grava trajetória; N = 1 em N) | central-computer |
| `RETENCAO_DIAS`                 | `30` dias de histórico          | central-computer   |
| `EXPURGO_INTERVALO_HORAS`       | `24`h entre expurgos            | central-computer   |
| `T_CARGA_UNID`                  | `0.3`s por unidade              | dispenser-sim      |
| `T_DISPENSA_UNID`               | `0.5`s por unidade              | dispenser-sim      |
| `PROB_ERRO_MECANICO`            | `0.01` (1% falha mecânica)      | dispenser-sim      |
| `PROB_FALHA_LEITURA_DISPENSER`  | `0.02` (2% falha câmera) — as duas fileiras | vision-sim |
| `PROB_DIVERGENCIA_DISPENSER`    | `0.02` (2% SKU errado) — as duas fileiras | vision-sim |
| `T_SCAN_DISPENSER`              | `1.5`s — as duas fileiras       | vision-sim         |
| `..._ESQ` / `..._DIR`           | herdam o valor acima; sobrescrevem UMA câmera | vision-sim |
| `PROB_FALHA_LEITURA_MESA`       | `0.02` (2% não detecta)         | vision-sim         |
| `PROB_DIVERGENCIA_MESA`         | `0.02` (2% contagem errada)     | vision-sim         |
| `T_SCAN_MESA`                   | `2.0`s                          | vision-sim         |
| `TOLERANCIA_PERC`               | `5.0`%                          | weight-sim         |
| `PROB_ERRO_SENSOR`              | `0.01` (1% falha HX711)         | weight-sim         |
| `T_LEITURA`                     | `1.5`s                          | weight-sim         |
| `T_TARA`                        | `0.5`s de estabilização da tara | weight-sim         |
| `RUIDO_G`                       | `2.0`g (ruído gaussiano)        | weight-sim         |
| `VEL_MM_S`                      | `80` mm/s                       | cnc-sim            |
| `HOME_X` / `HOME_Y`             | `-120` / `0` (fallback do HOME) | cnc-sim            |
| `INTERVALO_OS`                  | `90`s entre OS                  | erp-simulator    |
| `ESPERA_FILA_CHEIA`             | `20`s entre consultas à fila    | erp-simulator    |
| `MAX_ESPERAS_FILA`              | `15` esperas antes de pular o ciclo | erp-simulator |
| `RELOAD_CATALOGO_MIN`           | `30`min entre recargas de catálogo/ordens | erp-simulator |
| `ESPERA_PAUSA`                  | `10`s entre consultas ao flag de pausa | erp-simulator |

### Painel de bancada (fora do Docker)

| Variável | Padrão | Para quê |
|---|---|---|
| `CENTRAL_URL` | `http://localhost:8000` | Onde o computador central atende. |
| `CENTRAL_TIMEOUT_S` | `3` | Teto de cada requisição ao central. |
| `CENTRAL_SYNC_S` | `5` | Intervalo entre passadas do espelho de ordens. |
| `PAINEL_CENTRAL` | `1` | `0` desliga a integração inteira: nada sai pela rede e o painel volta a se comportar como antes dela. |
| `APSEN_DB` | `backend/apsen.db` | Caminho do banco. Existe para a suíte apontar para um arquivo temporário. |
| `APSEN_SECRET` | **sem default** | Chave de sessão do Flask. Ausente, default ou com menos de 32 caracteres, o painel **não sobe** (salvo `APSEN_ENV=dev`). |
| `APSEN_API_TOKEN` | **sem default** | Token do header `X-API-Token` das rotas `/api/*`. Ausente, elas respondem 503 e o resto do painel sobe normal. |
| `APSEN_ENV` | vazio | `dev` tolera `APSEN_SECRET` fraca, com aviso. Qualquer outro valor não tolera. |
| `APSEN_DEBUG` | `0` | `1` liga o console do Werkzeug e **força o bind em 127.0.0.1**. |
| `APSEN_HOST` / `APSEN_PORTA` | `0.0.0.0` / `5000` | Onde o painel escuta quando o debug está desligado. |
| `APSEN_RELOADER` | `0` | `1` liga o auto-reload do Flask. Deixe desligado com o display conectado. |

Valor não numérico em `CENTRAL_TIMEOUT_S` / `CENTRAL_SYNC_S` cai no padrão com
um aviso no log, em vez de derrubar o import: o painel é iniciado por um `.bat`
na bancada, e um erro de digitação não pode ser a diferença entre ter e não ter
painel.

---

## Concorrência e segurança

- `asyncio.get_running_loop()` no lifespan (Python ≥3.10 safe)
- `asyncio.to_thread()` para todo I/O MySQL — event loop nunca bloqueado
- `call_soon_threadsafe()` para notificação de eventos a partir de qualquer thread
- `threading.Lock` por slot no dispenser-simulator (race condition eliminada)
- `threading.Lock` atômico no cnc-simulator para `_em_movimento` (race condition eliminada)
- `threading.Lock` + `_peso_anterior_g` no weight-simulator para leitura delta thread-safe
- Pré-registro de eventos antes de enviar comandos (resposta rápida nunca perdida)
- `copy.deepcopy` sob o lock em todo snapshot de estado — a cópia rasa deixava
  os dicionários aninhados vivos durante a serialização, fora do lock
- JWT + bcrypt para autenticação do app de manutenção
- Console de operação com senha própria e sessão HMAC (`HttpOnly`, path
  `/console`), desabilitado por completo quando `CONSOLE_SENHA` não está
  definida — sem senha default embutida
- Painel de bancada: `APSEN_SECRET` sem default (o processo recusa subir, como o
  central), rotas `/api/*` atrás de `X-API-Token`, PIN de operador em hash e
  conferido pelo backend — o display não guarda credencial, nem no cartão SD
- Painel de bancada: `debug` só com `APSEN_DEBUG=1` e, aí, escutando apenas em
  `127.0.0.1` — o console do Werkzeug é execução remota atrás de um PIN
- **Revalidação a cada requisição autenticada**: o token não é a palavra final;
  usuário desativado perde acesso na hora e a `role` vale a do banco, não a do
  token (cache de `AUTH_CACHE_TTL_S`)
- Segredos só no `.env` (não versionado); o central **recusa subir** com
  `SECRET_KEY` default fora de `APSEN_ENV=dev`
- CORS restrito ao dashboard e ao app de manutenção (`CORS_ORIGINS`), não `*`
- Healthcheck em todos os serviços de aplicação e `depends_on` por
  `condition: service_healthy` — ordem de start não é prontidão
- Triple Check com trava de erro garante intervenção humana em qualquer divergência (limiar 1, ajustável por `TRIPLE_CHECK_MIN_DIVERGENCIAS`)

> ⚠️ O **painel de bancada** ainda não tem o mesmo nível: as rotas `/api/*` são
> abertas, o `debug=True` está ligado e a chave de sessão tem default embutido.
> Os itens estão no topo do backlog em [`TASKS.md`](TASKS.md).
