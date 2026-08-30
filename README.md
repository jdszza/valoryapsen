# APSEN – Sistema de Contagem de Medicamentos

> **Projeto de Engenharia Mecatrônica – 3º Ano**  
> Valory · APSEN Farmacêutica

---

## Visão Geral

Sistema automatizado de contagem e validação de medicamentos. Uma mesa CNC percorre o corredor entre **duas fileiras de 4 dispensers**, frente a frente, para coletar os medicamentos de cada Ordem de Saída (OS). Três câmeras de visão computacional validam a célula: uma sobre cada fileira de dispensers (esquerda, D1–D4; direita, D5–D8), lendo o QR/DataMatrix do produto carregado em cada slot, e uma sobre a mesa de coleta — a **câmera da balança** —, que faz a contagem visual das unidades dispensadas. Uma célula de carga HX711 instalada sob a mesa CNC valida o peso de cada lote dispensado. Após cada dispensa, um **Triple Check** compara as 3 fontes (contagem do dispenser, câmera da mesa e balança) e trava o sistema em caso de divergência até intervenção do operador. A comunicação é 100% REST/HTTP e WebSocket — **sem MQTT**. MySQL para persistência, Plotly Dash para interface.

---

## Arquitetura (v3.2 — REST/HTTP)

```
Order Generator (sorteia 1 das 10 ordens padrão e instancia um os_id novo)
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
```

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
| `order-generator`     | —     | Dispara 1 das 10 ordens padrão por ciclo e envia ao central  |
| `dashboard`           | 8050  | Monitoramento read-only (Plotly Dash)                        |
| `manut_web`           | 8051  | Manutenção e operação com autenticação JWT (Plotly Dash)     |

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
order-generator dispara — para quem observa a planta, o comportamento continua
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
order-generator, que é o que dá naturalidade à planta rodando sozinha, passa a
atrapalhar quem precisa mostrar um caso específico.

O que dá para fazer de lá:

| Ação | Detalhe |
|------|---------|
| Listar as 10 ordens | nome, categoria, itens, quantidades e quantos slots ocupa |
| Disparar qualquer uma | um clique, sem confirmação — `os_id` novo a cada disparo |
| Pausar / retomar o automático | assume o controle sem competir com o gerador |
| Estado ao vivo | OS ativa, fila, trava (com motivo e slot) e os 8 dispensers |
| Liberar a trava do Triple Check | **com confirmação** — é a única ação destrutiva |
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

**Como a pausa chega ao gerador.** O order-generator é outro container, e o
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
1.  order-generator: sorteia 1 das 10 ordens padrão, instancia os_id
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

## Pré-requisitos

- Docker Engine ≥ 24
- Docker Compose v2
- ~2 GB RAM

---

## Como rodar

```bash
# 1. Clone o repositório
git clone https://github.com/seu-usuario/valoryapsen.git
cd valoryapsen

# 2. Crie o .env (OBRIGATÓRIO — nada sobe sem isto). Ele NÃO é versionado.
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

# ── Origens de browser autorizadas a chamar o central ────────────────────────
# Ajuste ao publicar fora da máquina local. `*` não é aceito no central.
CORS_ORIGINS=http://localhost:8050,http://localhost:8051

# ── Console de operação do central (http://localhost:8000/console) ───────────
# OPCIONAL e sem default embutido: vazia = console DESABILITADO (toda rota
# /console* responde 503). Senha PRÓPRIA, independente do login do app de
# manutenção.
CONSOLE_SENHA=
CONSOLE_SESSAO_HORAS=8
FIM

# Preencha as CINCO obrigatórias antes de seguir:
#   SECRET_KEY  MYSQL_ROOT_PASS  MYSQL_PASS  SEED_ADMIN_SENHA  SEED_MANUT_SENHA
# As demais já têm default utilizável. O compose recusa subir com qualquer
# obrigatória vazia, citando o nome da variável que falta.

# 3. Build e start
docker compose down -v
docker compose build --no-cache
docker compose up -d

# 3. Acompanhar logs
docker compose logs -f central-computer
docker compose logs -f weight-simulator
docker compose logs -f vision-simulator
docker compose logs -f dispenser-simulator
docker compose logs -f cnc-simulator
```

---

## Interfaces

| Interface   | URL                           | Acesso           |
|-------------|-------------------------------|------------------|
| Dashboard   | http://localhost:8050         | Público          |
| Manutenção  | http://localhost:8051         | JWT              |
| Console     | http://localhost:8000/console | `CONSOLE_SENHA`  |
| API Central | http://localhost:8000         | REST/WS          |
| Docs API    | http://localhost:8000/docs    | Swagger          |

O console é a interface de operação do central — ver
[Console de operação](#console-de-operação-console). Ele fica **desabilitado**
(503) enquanto `CONSOLE_SENHA` não estiver no `.env`.

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

POST /api/v1/ordens                      → recebe nova OS (order-generator)
                                           409 os_duplicada | 429 fila_cheia |
                                           503 persistencia_indisponivel
GET  /api/v1/fila                        → ocupação da fila (backpressure)
GET  /api/v1/gerador                     → flag de pausa do order-generator
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
POST /console/api/gerador                → pausa/retoma o order-generator
POST /console/api/liberar-trava          → libera a trava do Triple Check
```

---

## Estrutura do repositório

```
valoryapsen/
├── central-computer/       # Orquestrador + API central
│   ├── main.py             # FastAPI + handlers de eventos (dispenser, CNC, visão, peso)
│   ├── orchestrator.py     # Lógica de negócio (fila, atribuição, rota, Triple Check)
│   ├── database.py         # MySQL (10 tabelas, 96 medicamentos)
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
├── order-generator/        # Dispara 1 das 10 ordens padrão por ciclo (sem porta)
├── dashboard/              # Plotly Dash read-only :8050
├── manut_web/              # Plotly Dash manutenção e operação :8051
├── mysql/init.sql          # Só charset/collation; o schema vem do database.py
├── tests/                  # pytest — sem Docker, sem MySQL (`make test`)
├── docker-compose.yml
└── .gitignore
```

---

## Variáveis de ambiente relevantes

| Variável                        | Padrão                          | Serviço            |
|---------------------------------|---------------------------------|--------------------|
| `SECRET_KEY`                    | (obrigatória, vem do `.env`)    | central-computer   |
| `APSEN_ENV`                     | `prod` (`dev` afrouxa o segredo) | central-computer  |
| `CORS_ORIGINS`                  | `http://localhost:8050,http://localhost:8051` | central-computer |
| `AUTH_CACHE_TTL_S`              | `30`s de cache da revalidação   | central-computer   |
| `CONSOLE_SENHA`                 | (vazia) — vazia DESABILITA o console (503) | central-computer |
| `CONSOLE_SESSAO_HORAS`          | `8`h de cookie (faixa 0.25–24)  | central-computer   |
| `NUM_SLOTS`                     | `8` dispensers (par; 2 fileiras) | **todos** — central, simuladores, dashboard e order-generator |
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
| `RUIDO_G`                       | `2.0`g (ruído gaussiano)        | weight-sim         |
| `VEL_MM_S`                      | `80` mm/s                       | cnc-sim            |
| `HOME_X` / `HOME_Y`             | `-120` / `0` (fallback do HOME) | cnc-sim            |
| `INTERVALO_OS`                  | `90`s entre OS                  | order-generator    |
| `ESPERA_FILA_CHEIA`             | `20`s entre consultas à fila    | order-generator    |
| `MAX_ESPERAS_FILA`              | `15` esperas antes de pular o ciclo | order-generator |
| `RELOAD_CATALOGO_MIN`           | `30`min entre recargas de catálogo/ordens | order-generator |
| `ESPERA_PAUSA`                  | `10`s entre consultas ao flag de pausa | order-generator |

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
- **Revalidação a cada requisição autenticada**: o token não é a palavra final;
  usuário desativado perde acesso na hora e a `role` vale a do banco, não a do
  token (cache de `AUTH_CACHE_TTL_S`)
- Segredos só no `.env` (não versionado); o central **recusa subir** com
  `SECRET_KEY` default fora de `APSEN_ENV=dev`
- CORS restrito ao dashboard e ao app de manutenção (`CORS_ORIGINS`), não `*`
- Healthcheck em todos os serviços de aplicação e `depends_on` por
  `condition: service_healthy` — ordem de start não é prontidão
- Triple Check com trava de erro garante intervenção humana em qualquer divergência (limiar 1, ajustável por `TRIPLE_CHECK_MIN_DIVERGENCIAS`)
