# CLAUDE.md

Notas para quem (humano ou agente) for mexer neste repositório. A visão geral da
arquitetura está no [README.md](README.md), o contrato serial em
[docs/PROTOCOLO_SERIAL.md](docs/PROTOCOLO_SERIAL.md) e o painel de bancada em
[docs/BANCADA.md](docs/BANCADA.md); aqui ficam só as decisões que não dá para
ler no código.

> Houve um `ANALISE_ARQUITETURAL.md`, e ele não existe mais. As duas coisas que
> só moravam nele foram para o README — "Fontes de verdade" e "Contrato de
> entrada de uma OS" —, e as referências que apontavam para o vazio foram
> reescritas. `tests/test_referencias_docs.py` impede que outro ponteiro morto
> volte a entrar: documento que promete e não entrega é pior que documento
> nenhum, porque quem lê para de conferir o código.

## Testes

```
pip install -r tests/requirements-dev.txt
pytest tests/
```

Nenhum teste sobe MySQL, Docker ou uvicorn. `tests/conftest.py` importa os
módulos por caminho (os diretórios têm hífen no nome e não são pacotes) e
substitui as bordas: `requests` nos simuladores, `database.*` no central,
`orchestrator._post` no orquestrador.

## Schema do banco: uma fonte só, e é o `database.py`

O DDL vive em `_DDL_TABELAS`, `_COLUNAS_EVOLUTIVAS` e nos seeds de
`central-computer/database.py`. `mysql/init.sql` **não declara schema** — só
fixa charset e collation do banco.

A escolha não é de gosto: os dois caminhos não rodam com a mesma frequência.
`docker-entrypoint-initdb.d` executa uma única vez, na criação do volume
`mysql_data`; `init_db()` executa em todo startup do central. Qualquer objeto
que exista só no init.sql some num `docker compose down` sem `-v`, num rebuild,
ou ao apontar para um MySQL que já existia — e o código segue consultando o
objeto ausente. Foi o que aconteceu: `visao_leituras`,
`medicamentos.peso_unitario_g` e o seed dos slots só existiam no init.sql;
`_seed_medicamentos` batia em `peso_unitario_g`, tomava 1054, e o central não
subia. Manter o DDL do lado que sempre roda faz a divergência ser impossível,
não só improvável.

O seed de `dispenser_estado` segue a mesma lógica, e por isso deixou de
decidir por `COUNT(*)`: quando a célula passou de 6 para 8 slots, um banco com
as 6 linhas antigas já satisfazia "count >= 6" e D7/D8 nunca ganhariam linha —
slot sem linha é slot cujo estoque o banco nunca registra, porque
`salvar_dispenser_estado` faz UPDATE, não upsert. Hoje `_seed_dispenser_estado`
lê os ids existentes e insere só os que faltam: idempotente, tolerante a linha
apagada no meio, e correto quando o número de slots muda.

`CREATE TABLE IF NOT EXISTS` não repara tabela que já existe, então banco de
versão anterior é acertado por `_aplicar_colunas_faltantes`: ele lê o
`information_schema` uma vez e só emite `ALTER` para o que falta de verdade —
sem o `try/except: pass` que engolia junto os erros de verdade. Coluna nova
exige, portanto, **duas** entradas: a definitiva no `_DDL_TABELAS` (banco novo)
e a de reparo no `_COLUNAS_EVOLUTIVAS` (banco antigo).

### `init_db` separa três falhas que o PyMySQL entrega iguais

Tudo abaixo chega como `OperationalError`, e tratar os três como um só era o
que transformava erro de schema em 60 segundos de "MySQL não disponível"
seguidos de queda sem causa visível:

| código                   | exceção                | espera? |
|--------------------------|------------------------|---------|
| 2002/2003/2006/2013/1040/1053 | `BancoIndisponivel`    | 30 retries |
| 1044/1045/1049/1698      | `ConfiguracaoInvalida` | não |
| resto (1054, 1146, 1064) | `SchemaInvalido`       | não |

A linha entre as duas últimas existe para o log: `SchemaInvalido: Access denied
for user` mandava quem lê caçar tabela errada. 1045 poderia, em tese, aparecer
na janela em que o entrypoint do MySQL ainda não criou o usuário `apsen` —
mas nessa fase o servidor temporário sobe com `--skip-networking`, então o
central recebe 2003 (transitório) e não 1045. Se ainda assim acontecer,
`restart: unless-stopped` cobre.

### O drift volta a aparecer sozinho

`tests/test_schema.py` varre as queries de `database.py` por AST e exige que
toda tabela e coluna referenciada exista no DDL — sem lista manual para alguém
esquecer de atualizar. Os demais testes de lá cobram o par
`_DDL_TABELAS`/`_COLUNAS_EVOLUTIVAS` e falham se `init.sql` voltar a conter
`CREATE TABLE`, `ALTER TABLE` ou `INSERT`.

### O `_conn()` é um pool, e o teto é do que fica GUARDADO

Cada operação abria uma conexão nova: TCP + handshake + autenticação para um
INSERT, e fora. Não aparece numa query isolada, e o central não faz queries
isoladas — cada evento de adapter são de 1 a 3 escritas, e só a telemetria dos
slots são `NUM_SLOTS` gravações a cada 15s, somadas às da CNC, da visão e da
balança.

O pool é uma `queue.LifoQueue` sobre o `_make_conn` que já existia, não o
`PooledDB` do DBUtils: a dependência resolveria o mesmo, e este repositório já
recusou acrescentar uma ao central por menos (o `parse_qsl` do login do
console). LIFO e não FIFO porque devolver sempre a mais recente mantém o
conjunto quente pequeno e deixa as do fundo envelhecerem até o descarte — com
FIFO, um pico de 8 conexões mantém as 8 vivas para sempre.

Três regras, e cada uma cobre um modo de falhar:

1. **Conexão parada é verificada antes de voltar a ser usada.** O `wait_timeout`
   do MySQL fecha o socket sem avisar. O `ping(reconnect=True)` custa um round
   trip, então só roda depois de `_PING_APOS_S` (30s) parada — no caminho
   quente, que é o que o pool existe para acelerar, ele nunca roda.
2. **Conexão que viu exceção NÃO volta ao pool.** Erro no meio de um statement
   pode deixar resultado por ler no socket, e o sintoma é a operação SEGUINTE
   falhar — em outra thread, com outro SQL, sem relação visível com a causa.
   Vale para `BaseException`: um `CancelledError` deixa o socket no mesmo
   estado que um erro de SQL.
3. **Transação aberta morre com o empréstimo,** e só quem pediu
   `autocommit=False` paga o ROLLBACK de devolução. Com autocommit não há o que
   desfazer, e cobrar o round trip de todo mundo devolveria boa parte do que o
   pool economizou.

`MYSQL_POOL_MAX` (8, faixa 1..64) é teto do que fica GUARDADO, **não** do que
pode abrir: pool vazio abre uma nova, como antes, em vez de bloquear a thread.
Bloquear seria pior que o problema original — uma rajada acima do teto
congelaria as threads do `to_thread` e, com elas, o orquestrador, por causa de
um número de configuração.

`tests/test_db_pool.py` varre `database.py` por AST e falha se alguma função de
banco voltar a chamar `_make_conn` direto: a conexão nasceria fora do pool, não
seria devolvida, e o vazamento só apareceria no `max_connections` horas depois.

## A ordem de subida é do compose, e ela tem forma de losango

Antes só o MySQL tinha `healthcheck`; os `depends_on` dos adapters garantiam
ordem de *start*, não de prontidão. A primeira OS do boot saía antes de o
dispenser-adapter estar ouvindo, tomava `connection refused` e morria em timeout
de carregamento — falha que só aparecia no primeiro ciclo depois de um `up` e,
por isso, passava por "coisa de ambiente".

Hoje 12 dos 13 serviços têm healthcheck — todos menos o `erp-simulator`, cuja
exceção está justificada mais abaixo — e as dependências que importam usam
`condition: service_healthy`. O grafo é acíclico e tem quatro camadas:

```
mysql → central-computer → adapters → erp-simulator
             simuladores ↗
```

- **O central NÃO depende dos adapters.** Seria ciclo (adapter → central →
  adapter), e ele tolera adapter fora do ar: o `_post` retenta.
- **Cada adapter depende do central E do seu simulador.** Fala com o simulador
  no lifespan e com o central no primeiro evento.
- **O erp-simulator depende dos quatro adapters**, não só do central: quem
  executa a OS são eles. Sem isso, a primeira OS nasce antes de haver quem a
  execute.

O teste é `/ping` (o processo está de pé), nunca `/health`. Duas razões: `/health`
devolve 200 mesmo degradado — não serviria de portão sem interpretar o corpo — e,
se servisse, atrelaria a saúde do adapter à do central, fazendo um restart do
central marcar os quatro adapters como unhealthy em cascata. `/health` continua
sendo o endpoint de diagnóstico.

O comando é `python -c "import urllib.request; ..."`, não `curl`: as imagens são
`python:slim` e não têm curl nem wget. Healthcheck com curl reprova sempre, e
serviço que nunca fica healthy trava tudo que espera por ele.

`tests/test_compose.py` cobra healthcheck em todo serviço de aplicação, proíbe
ciclo, exige que toda dependência aponte para serviço existente e que a porta do
healthcheck seja a publicada. A única exceção registrada é o `erp-simulator`:
worker de laço, sem porta e sem ninguém dependendo dele — `restart:
unless-stopped` já cobre o que um `pgrep` diria.

## A célula tem 8 slots em duas fileiras, e a geometria tem UM dono

O arranjo é um corredor: quatro dispensers de cada lado, a mesa CNC passando
no meio. D1–D4 em `y = -150`, D5–D8 em `y = +150`, ambos em
`x ∈ {0, 120, 240, 360}`; HOME em `(-120, 0)`, no eixo do corredor e um passo
antes do primeiro par. Os pares frente a frente são D1↔D5, D2↔D6, D3↔D7, D4↔D8
— consequência da numeração, não de uma tabela à parte: o slot `i` e o slot
`i + NUM_SLOTS/2` compartilham o X por construção.

Passo, afastamento, HOME e `NUM_SLOTS` são constantes no topo de
`orchestrator.py`, e `POSICOES` sai de `_gerar_posicoes()`. Mexer no layout é
mexer em quatro números.

### A cópia do mapa no cnc_simulator não existe mais

Havia dois dicionários `POSICOES` idênticos, mantidos à mão em serviços
diferentes — um no orquestrador, outro no `cnc_simulator`. Duplicação que já
era frágil com 6 slots numa linha reta e que, com 8 slots em duas fileiras,
passaria a ter Y além de X para divergir. E divergência aí não dá erro: o
central manda a CNC para D7 e a CNC vai para onde a *cópia dela* diz que D7
fica. O sintoma seria a câmera da mesa e a balança acusando divergência num
slot só, que é exatamente o quadro de uma falha mecânica.

O mapa ficou com o central, e por um motivo concreto: `/executar/mover` **já
carregava `posicao_x`/`posicao_y`**, e o simulador já se movia por eles. O
dicionário local só era consultado na validação de faixa (`dispenser_alvo not
in POSICOES`) — para isso basta o número de slots. Hoje o simulador valida
`1 <= id <= NUM_SLOTS` e se move para o par de coordenadas que recebeu.

O homing seguiu junto: `cmd_homing` agora manda `posicao_x`/`posicao_y` do
HOME, como o mover. O `HOME_X`/`HOME_Y` do simulador sobrou como fallback do
contrato antigo e como a posição em que a CNC nasce, antes do primeiro comando
— não como fonte de verdade.

A alternativa (gerar as posições dos dois lados a partir das mesmas constantes
de geometria) mantinha duas implementações que precisariam concordar, e a
concordância dependeria de as env vars estarem iguais nos dois containers. Uma
fonte e um transporte é menos peça para alinhar.

#### E então a mesa passou a ter a própria geometria — a regra não mudou

O parágrafo acima descreve um arranjo que não existe mais: **o central não manda
mais coordenada nenhuma.** O comando `mover` leva o DISPENSER, a placa vai ao
waypoint que foi medido e gravado NELA, e ela REPORTA no evento `posicionado`
onde parou. Quem registra a posição é o central, a partir do que a máquina
informou.

O que **continua valendo**, e é a regra de verdade: *nenhum número que precise
CONCORDAR entre dois serviços é mantido à mão nos dois*. O que mudou foi qual é
a fonte. Antes, o mapa era do central e o simulador não tinha nenhum; hoje o
mapa é da MÁQUINA, e `orchestrator.POSICOES` sobrou como **modelo lógico** — é
com ele que a serpentina ordena a rota e que a câmera da mesa é apontada quando
o evento não trouxe a medida. Ele descreve a bancada; não a dita.

Por isso o `cnc_simulator` agora tem `BANCADA` e a placa falsa tem `WAYPOINTS`,
e **os dois são deliberadamente DIFERENTES de `POSICOES`**. Não é descuido: se
fossem iguais, toda asserção sobre posição passaria por concordância acidental
— o central compararia o próprio número com uma cópia dele, e continuaria verde
no dia em que voltasse a ignorar o que a máquina informa. `test_slots.py` e
`test_protocolo_placas.py` cobram essa diferença.

**E `CNC_TETO_TRAJETO_S` não viola a regra**, embora saia de uma conta sobre a
geometria da mesa. Ele é um **limite superior**, não um fato duplicado: precisa
ser MAIOR que o pior trajeto, não IGUAL a ele. Um fato duplicado passa a mentir
no instante em que o outro lado muda; um teto continua verdadeiro enquanto a
mesa couber embaixo dele. O que o quebra é subir o `FEED` da mesa sem subir o
teto — por isso a conta está escrita ao lado da constante em `config.py` e
repetida na lista de bancada do `cnc/README.md`, e não porque alguém precise
mantê-la sincronizada.

### `NUM_SLOTS` é env var, não constante por arquivo

Cinco serviços precisam do número: o central (mapa e validação), os quatro
simuladores (faixa que aceitam), o dashboard (grade) e o seed do banco. Todos
leem `NUM_SLOTS`, declarada uma vez no compose (`${NUM_SLOTS:-8}`). Nenhum
`range(1, 7)` sobrou, e nenhuma mensagem de erro carrega a faixa escrita à mão
— `f"slot_id deve ser 1-{NUM_SLOTS}"` — porque um literal "1-6" desatualizado
manda quem lê o log procurar o problema na faixa errada.

O central valida o valor (`config._num_slots`): par, entre 2 e 64, senão cai no
default de 8 com warning. Ímpar deixaria uma fileira mais longa que a outra —
geometria que o resto do código não modela.

### A rota é serpentina, e a escolha foi medida

`planejar_rota` fazia nearest-neighbor. Com uma fileira só, isso era decoração:
todo Y valia 0, e qualquer ordenação devolvia a mesma linha reta. Com duas
fileiras a escolha passa a custar milímetros de verdade.

O critério adotado é a **serpentina**: sobe a fileira esquerda em X crescente,
volta pela direita em X decrescente, pulando quem não está na OS. A razão não é
estética. O ciclo real é **fechado** — HOME → slots → HOME, porque o passo 5 de
`_processar_os` sempre faz homing —, e HOME mais as duas fileiras estão todos na
borda de um mesmo polígono convexo. Para pontos em posição convexa, o tour ótimo
é a ordem do contorno, e a serpentina é exatamente essa ordem.

Medido por força bruta contra a permutação ótima de cada um dos 255
subconjuntos não-vazios de 8 slots, partindo de HOME:

| heurística        | média vs. ótimo | pior caso |
|-------------------|-----------------|-----------|
| serpentina        | **1,000** (ótimo em 255/255) | 1,000 |
| nearest-neighbor  | 1,041           | 1,404 (slots {1,3,5,6,7,8}) |

O pior caso do NN é o cruzamento de corredor: ele atravessa cedo, atrás do slot
mais próximo, e depois precisa atravessar de novo para fechar o ciclo. É a
armadilha clássica do guloso — barato agora, caro na volta —, e ela só aparece
porque a volta passou a ser contabilizada. **Avaliar a rota sem a perna de
retorno inverte o resultado** (aí o NN ganha da serpentina), e é por isso que
`distancia_rota` inclui o retorno por padrão.

A vantagem não vem dos números escolhidos: repetindo a força bruta com passo de
50 a 400 mm e afastamento de 10 a 1000 mm, a serpentina segue ótima em todos os
subconjuntos. `tests/test_orchestrator.py` refaz essa varredura a cada rodada —
inclusive a comparação contra a permutação ótima —, então uma heurística nova
que melhore a média e piore um caso não passa.

`pos_inicial` continua na assinatura e decide por qual fileira começar: partir
do lado oposto ao que a CNC ocupa custa uma travessia a mais na ida e outra na
volta. Toda OS parte de HOME, onde o argumento empata e a serpentina desce pela
esquerda; o espelhamento vale para quem chame a função com a CNC parada do lado
direito (10% a 17% mais curto ali, sem a garantia de otimalidade, que é do
trajeto por HOME).

### O painel tem a forma da bancada

O dashboard desenha duas fileiras de quatro cartões com o corredor da CNC
entre elas, em vez de uma linha de oito. Não é enfeite: num painel de oito
cartões iguais, "D7 travou" precisa apontar para um lugar na bancada, não para
a sétima posição de uma lista.

O painel de visão repete o mesmo arranjo — câmera da esquerda, corredor (com a
câmera da mesa, que é quem anda por ele), câmera da direita —, pelo mesmo
motivo: "a câmera de baixo parou de ler" tem que cair no mesmo lado nos dois
painéis. Por isso `tests/test_dashboard.py` procura o corredor DENTRO da grade
de slots em vez de contar `ap-corredor` na página inteira: o motivo agora
aparece em dois lugares de propósito.

## Três câmeras, e o lado sai do slot

A célula tem uma câmera por fileira de dispensers — `dispenser_esq` (D1–D4) e
`dispenser_dir` (D5–D8) — mais a da mesa de coleta, `mesa`. **"Câmera da mesa"
é a câmera da balança**: ela fica sobre a mesa onde está o HX711 e faz a
contagem visual. O identificador não virou `balanca` porque o valor já está
gravado em `visao_leituras.camera`, e renomear exigiria migrar dados por um
ganho só de vocabulário. O README diz isso na tabela de câmeras.

**O TIPO do evento não mudou.** As duas câmeras de dispenser emitem
`leitura_dispenser_ok/_falha/_divergencia`, como antes; quem separa esquerda de
direita é o campo `camera`. Colocar o lado no tipo obrigaria o orquestrador e o
`avaliar_triple_check` a conhecer a geometria da bancada para reconhecer um
resultado que já é inequívoco sem ela. Pelo mesmo motivo a chave de espera
continua sendo `{os_id}:visao_dispenser:{slot}`: quem espera é o slot.

**Quem escolhe a câmera é o simulador, não quem comanda.**
`vision_simulator.camera_do_slot()` deriva o lado do `slot_id` com a mesma
partição de `_gerar_posicoes` (1..N/2 à esquerda). O comando
`/comandos/capturar/dispenser` não tem campo de câmera — se tivesse, um dia
pediria D7 à câmera da esquerda, e o sintoma seria divergência de SKU num slot
só: indistinguível de um medicamento realmente trocado.
`tests/test_vision_simulator.py` cobra a partição contra `POSICOES`, e não
contra uma cópia da regra.

Com as câmeras reais, o lado sai do slot **também no vision-adapter**
(`ponte_dispensers.camera_do_slot`): a estação dos dispensers não recebe
comando, então é o adapter que decide de qual das duas estações ler o veredito
do slot. São duas cópias da mesma partição em serviços que não importam um ao
outro, e `tests/test_vision_adapter_ponte.py` compara a do adapter com a do
simulador para várias contagens de slots — sem escrever a regra no teste. O
comando do central continua sem campo de câmera.

**Configuração: um padrão compartilhado, override por câmera.**
`PROB_FALHA_LEITURA_DISPENSER`, `PROB_DIVERGENCIA_DISPENSER` e
`T_SCAN_DISPENSER` valem para as duas fileiras; o sufixo `_ESQ`/`_DIR`
sobrescreve uma delas (`_cfg_float`). Duplicar toda a configuração por câmera
tornaria o caso comum — as duas iguais — duas vezes mais fácil de dessincronizar,
e o caso raro (uma câmera suja em campo) é justamente o que precisa de escape.
A da mesa não tem sufixo: é uma só.

**No central, a chave de estado é `"camera_" + camera`.** `_camera_dispenser()`
resolve o lado, e cai no slot quando o campo vem ausente ou com o valor antigo
`"dispenser"` (contrato de quando havia uma fileira só). Sem esse fallback a
leitura não acharia chave, sumindo do painel e do histórico sem erro no log —
o pior modo de falhar. A fonte do alarme carrega a câmera
(`camera_dispenser_dir_7`), que é o que manda o técnico à lente certa.

`visao_leituras.camera` é `VARCHAR(20)`: `dispenser_dir` tem 13 caracteres e
cabe: o DDL não precisou mudar.

## Ciclo de vida de um slot de dispenser

Um slot é um recurso físico compartilhado entre OS. O estoque carregado nele
**sobrevive** ao fim — ou ao abort — da OS que o carregou. Duas regras seguem
disso, e o sistema já parou de pé por violar as duas:

1. **Todo caminho que encerra uma OS precisa devolver os slots ao pool.**
   Resetar `_estado["dispensers"]` não esvazia o dispenser. `_abortar_os` recebe
   as atribuições da OS e manda `cmd_limpar` para cada slot reservado; a falha
   da limpeza vira o alarme `limpeza_pos_abort_falhou`, sem sobrescrever o motivo
   original do abort. Antes disso, cada OS abortada queimava um slot em
   definitivo e, depois de ~6 abortos, toda OS nova era rejeitada com `sem_slot`.

2. **Slot ocupado por medicamento diferente não é slot perdido.** O passo 3 de
   `atribuir_slots` aceita esse slot (o de menor resíduo, para descartar o mínimo
   de estoque) e o devolve marcado com `precisa_limpeza=True`.

### `atribuir_slots` é pura — de propósito

A função só decide e marca: não envia comando, não altera o estado que recebe.
Isso a mantém testável sem mock nenhum (`tests/test_orchestrator.py`) e deixa a
política de descarte visível em um lugar só.

O efeito colateral correspondente é explícito no `_processar_os`, na etapa
**1b**: antes de qualquer carregamento, todo slot com `precisa_limpeza` recebe
`cmd_limpar` e o orquestrador **bloqueia até a confirmação `limpeza_ok`**.
Carregar sem esperar a confirmação faz o dispenser recusar a carga com 409.

Quem chamar `atribuir_slots` em um contexto novo herda essa obrigação: ou honra
o `precisa_limpeza`, ou filtra as atribuições que o trazem.

### Exceção não tratada também é um caminho que encerra a OS

O `except` do `loop_orquestrador` gravava "erro", zerava `os_ativa` e parava
aí — cumprindo a regra do status terminal e violando a regra 1 acima. Três
rastros ficavam para trás, e nenhum deles dá erro no momento: o medicamento
seguia FISICAMENTE no dispenser, os slots guardavam o `os_id` da OS morta, e as
chaves `{os_id}:...` seguiam em `_pending_events`. O sintoma nascia na OS
SEGUINTE, longe dali — a etapa 1b mandava limpar o slot que ninguém liberou e
tomava 409.

Hoje o `except` chama `_abortar_os`, que já fazia as três coisas certas. As
atribuições vêm de `_estado["atribuicao_ia"]`, publicada na etapa 1: é o
registro de quais slots a OS chegou a reservar, e lista vazia (exceção antes da
reserva) faz o abort não mandar limpeza nenhuma. O abort é envolvido em
`try/except` próprio porque o loop é ÚNICO: falha na limpeza não pode derrubar
o consumidor da fila e parar a planta inteira.

Vale a forma geral, que este arquivo já registra pelo lado do painel
("O espelho grava sem `_set_status_by_numero_os`"): **caminho de saída que não
passa pela função canônica de encerramento precisa herdar TODOS os efeitos dela,
não só o que se lembrou de copiar.**

### `_abortar_os` toca só nos slots DESTA OS

O reset da memória varria `_estado["dispensers"]` inteiro; o caminho de sucesso
sempre iterou sobre `atribuicoes`. Era o caminho de erro que generalizava — e
`os_id` num slot é exatamente quem diz de quem é o medicamento parado ali.
Apagado, o resíduo de OUTRA OS vira órfão sem dono aparente: o painel mostra o
slot como idle e o `atribuir_slots` seguinte o toma por livre, sem passar pela
limpeza que o `precisa_limpeza` obrigaria.

Os testes antigos não pegavam isso porque encenavam uma OS por vez: os slots
que o abort não devia tocar já estavam idle, e "não mexeu" era indistinguível
de "mexeu para o mesmo valor". Hoje `tests/test_orchestrator.py` mantém sempre
um slot de outra OS na bancada, e compara o conjunto resetado pelo abort com o
do caminho de sucesso.

## Comando a todos os slots sai em `gather`, não em laço

As etapas 3 (carregamento) e 3b (scan das câmeras de dispenser) enviavam os
comandos num laço sequencial com `await`, sob um comentário que dizia "em
paralelo". Isoladamente seria só lento; o que faz disso um bug é o relógio do
lado de lá.

`_post` retenta 3x com `sleep(1)` e timeout de 10s — até ~32s por comando com o
adapter degradado. Com a célula cheia, o comando do ÚLTIMO slot só saía ~4 min
depois do primeiro, enquanto o `TIMEOUT_CARREGAMENTO` (180s) do PRIMEIRO já
corria desde que o evento foi registrado. A OS abortava por "timeout de
carregamento" de um dispenser que nunca tinha sido chamado — **e o log apontava
para o slot errado**, porque o slot que estoura não é o lento.

O paralelismo é do ENVIO, não do ciclo de dispensa: `mover`, `dispensar`,
`pesar` e a câmera da mesa seguem sequenciais, e há teste cobrando isso. A mesa
é uma só; paralelizar ali mandaria a CNC a dois slots ao mesmo tempo.

**Envio recusado (`ok=False`) decide na hora.** No carregamento, aborta — e o
motivo tem código próprio, `erro_envio_carregamento`, para não se confundir com
o timeout de um comando que chegou. No scan, NÃO aborta (câmera é fonte que
deixou de confirmar, não que contradisse — a mesma regra de
`leitura_dispenser_falha`), mas o slot é resolvido como "sem medição"
imediatamente, em vez de queimar `TIMEOUT_VISAO_DISPENSER` esperando um evento
que já se sabe inexistente. É para isso que serve `_nulo()`. A regra vale
também para a câmera da mesa no passo 4 (e na conferência final no HOME): envio
recusado resolve o slot como "câmera da mesa: comando não aceito" na hora, em
vez de esperar `TIMEOUT_VISAO_MESA` por cada slot com a estação caída.

O teste mede a CONCORRÊNCIA (quantos comandos ficam em voo), não o tempo de
parede: cronômetro em suíte é flaky, e "demorou menos" não diz qual espera
sumiu.

## Retry só onde tentar de novo pode dar outro resultado — e nos DOIS sentidos

`_post` do orquestrador retentava QUALQUER status >= 300, três vezes, com
`sleep(1)` entre elas. Para falha de rede e 5xx isso é exatamente o certo; para
409 e 422 são 2s gastos numa resposta que não vai mudar. O caso concreto é o 409
`limpeza_em_operacao` do dispenser-simulator, e ele aparece onde dói: a etapa 3
dispara os comandos de todos os slots em `gather`, e cada recusa determinística
segurava um slot por 2s a mais enquanto o `TIMEOUT_CARREGAMENTO` do PRIMEIRO já
corria — a mesma forma do bug que a seção do `gather` registra.

Hoje a decisão mora em `_vale_retentar(status)`: `>= 500`, mais 408 e 429. A
linha é o 500 e não uma lista de códigos — 5xx inteiro é "o lado de lá falhou",
inclusive um 501 de rota que o adapter não implementa. Retentar esse é barato, e
a lista curta é o que mantém a regra igual dos dois lados da ponte.

**E havia só um lado.** O caminho de volta — `_post_central` dos quatro adapters
— postava UMA vez e, em falha, só logava. Ele é o ÚNICO caminho de volta ao
orquestrador: um `dispensado` perdido deixa o orquestrador bloqueado em
`aguardar_evento`, estoura `TIMEOUT_DISPENSA` e aborta uma OS cujo hardware fez
tudo certo. Um 503 de central reiniciando dura segundos; a OS que ele mata dura
o turno. Hoje os quatro aplicam a mesma política, com o mesmo `_vale_retentar`,
e `tests/test_adapters.py` compara os quatro entre si **e** contra o critério do
orquestrador — quatro cópias divergem no primeiro ajuste, e a que fica para trás
é a do adapter que ninguém está depurando naquele dia.

O custo é o tempo de resposta AO SIMULADOR: com o central fora, o handler segura
a requisição por até `3 × TIMEOUT_EVENT + 2s`. O simulador desiste antes (o
`requests.post` dele tem timeout de 5s) e o encaminhamento segue até o fim assim
mesmo — que é o desejado: quem precisa do evento é o orquestrador, e o simulador
ignora o retorno (`_evento` é fire-and-forget).

## Um adapter, DOIS transportes — e o simulador não vai embora

Os simuladores de hardware vão ganhar firmware de verdade, e a conversa entre
cada firmware e o seu adapter é serial, por cabo USB, **uma porta por firmware**:
o dispenser-adapter fala com a placa dos 8 dispensers, o cnc-adapter com a da
mesa, o weight-adapter com a da balança. O vision-adapter ficou de fora — a
visão continua por HTTP, e a ausência é decisão, não pendência.

O transporte é **selecionável** (`<SUB>_TRANSPORTE`), e o default é `http`. A
alternativa — substituir o simulador pelo serial — parece mais limpa e é a errada
por três razões que não se resolvem depois:

1. **O CI e a suíte não têm placa.** Sem o caminho HTTP, todo teste que hoje
   exercita a planta inteira passaria a depender de um duplo do transporte; o que
   se testaria seria o duplo.
2. **A demonstração roda em Docker, em máquina emprestada.** "Mostre o sistema"
   não pode exigir três placas e três cabos.
3. **A bancada com hardware precisa do simulador de volta** no dia em que uma
   placa queimar. Transporte selecionável faz disso uma env var; substituição
   faria disso um rollback.

Por isso o default é `http` — e isso é o que garante que esta feature não mude o
resultado de nada que já rodava. A perna de CIMA de cada adapter ficou intacta:
os endpoints `/comandos/*` com os mesmos modelos Pydantic e os mesmos nomes de
campo, o `_post_central` com o retry que `tests/test_adapters.py` cobra, e o
payload do evento atravessando **sem interpretação** — como já atravessam
`injetar_falha` e as duas quantidades da pesagem. `central-computer/`, o
orquestrador, os simuladores, o dashboard e o app de manutenção não foram
tocados, e há teste comparando o payload que sai pelos dois transportes lado a
lado. É esse teste, e não a intenção, que prova que o central não precisou mudar.

Os nomes de comando e de campo também são os mesmos nos dois caminhos. Não é
preguiça: o evento é repassado cru ao central, então renomear no serial
obrigaria o adapter a **traduzir** — e tradução é o lugar onde os dois lados
divergem depois, em silêncio.

### ACK não é conclusão, e confundir os dois trava uma OS

O comando é escrito na porta e a placa responde um ACK curto
(`{"resp":"ok","cmd_id":n}`) dizendo que **aceitou**. O resultado chega DEPOIS,
como evento assíncrono, e é ele que o orquestrador espera em `aguardar_evento`.
Um `mover` leva segundos; um `dispensar` leva mais.

Daí a separação dos dois relógios: o prazo do ACK é curto e configurável
(`<SUB>_ACK_TIMEOUT_S`, 2s) e é só dele que o endpoint `/comandos/*` depende para
responder; o prazo da CONCLUSÃO continua onde sempre esteve, nos `TIMEOUT_*` do
orquestrador, e esta feature não o toca. Tratar o ACK como conclusão mandaria
`dispensado` ao central no instante em que o comando foi aceito — antes de
qualquer unidade cair na mesa — e a balança pesaria uma mesa vazia.

ACK negativo e ACK ausente viram a MESMA `HTTPException` que o `_post_sim` já
levantava: recusa da ponta de lá é 502, ponta de lá inalcançável é 503. O
orquestrador não distingue firmware de simulador, e não deveria.

### Todo comando leva `cmd_id`, e repetição é ignorada PELA PLACA

Com ACK perdido, a reação natural de qualquer camada é reenviar — e reenviar
`dispensar` é dose dobrada no leito. O `serial_link.py` não reenvia nada, nem por
engano nem por configuração, e há teste medindo uma linha por comando.

Mesmo assim todo comando carrega um inteiro monotônico por porta, e a placa
guarda o último executado por subsistema: um `cmd_id` repetido responde ACK de
novo **sem executar**. O reenvio pode vir de qualquer origem — um restart do
adapter no meio do ciclo, um operador repetindo a ação, uma versão futura que
decida retentar —, e esta é a parte do protocolo que **não dá para acrescentar
depois sem trocar as duas pontas ao mesmo tempo**. Por isso ela está escrita em
`docs/PROTOCOLO_SERIAL.md` antes de o firmware existir.

### Serial NUNCA no event loop

Os adapters são FastAPI/async e `Serial.read()`/`Serial.write()` são bloqueantes.
Uma leitura pendurada congela o adapter inteiro — inclusive o `/ping` que o
compose consulta como portão de subida, ou seja, um cabo ruim derrubaria em
cascata tudo que depende daquele adapter.

A leitura mora numa thread dedicada por porta, que empurra as linhas adiante; o
envio de comando é síncrono e o lado async passa por `asyncio.to_thread`. É a
mesma regra do central ("Nada de banco no event loop") e o teste é do mesmo tipo:
varredura por AST atrás de chamada serial dentro de `async def`, mais a guarda de
que o `to_thread` continua lá — sem ela, apagar a chamada deixaria o teste verde
para sempre.

O que torna essa varredura verificável é `serial_link.py` **não conhecer
asyncio**: ele é de threads, e quem faz o salto para o event loop
(`run_coroutine_threadsafe`) é o adapter. É também o que permite o evento vindo
da placa reusar o `_post_central` que já existe, em vez de ganhar uma segunda
implementação síncrona que divergiria do retry testado.

### O `/ping` do adapter não olha para a placa

Ele diz que ESTE processo está de pé, e é isso que o compose usa como portão —
atrelá-lo ao hardware faria um cabo solto marcar o serviço como unhealthy e
derrubar em cascata quem depende dele, justamente na hora em que o resto da
planta precisa continuar rodando. É a mesma razão pela qual o healthcheck nunca
usou `/health`.

Quem conta a verdade sobre a porta é o `/health`, que já é o endpoint de
diagnóstico: url aberta, conectada desde quando, último ping da placa, linhas
truncadas e telemetria descartada.

### Reconexão é rotina; queda recusa comando NA HORA

Cabo solto, placa reiniciada e ponte TCP caída são operação normal, não queda do
processo: o laço fecha, espera e tenta de novo, para sempre. Só a TRANSIÇÃO vira
linha de log — uma linha por tentativa encheria o log e esconderia a volta.

Enquanto está fora, comando recebido é recusado imediatamente, e comando em voo
quando a porta cai não espera o prazo inteiro. Esperar o `ack_timeout` para
descobrir o que já se sabe gastaria o relógio do `TIMEOUT_*` do orquestrador, que
já está correndo do outro lado.

### Detecção por PING DA PLACA, nunca por VID/PID

Com `<SUB>_SERIAL_URL` preenchida, ela manda e não há varredura. Vazia, o adapter
varre as portas e aceita a primeira que emitir o ping **daquele subsistema** — o
ping carrega o nome, e quem sempre inicia o ping é a placa.

Casar por VID/PID acharia a placa errada: o VID/PID de um conversor USB-serial é
o mesmo em placas de fabricantes diferentes. Errar aqui significa mandar
`dispensar` para a balança — o comando sai, nada dispensa, e a OS morre por
timeout de um slot que está íntegro.

E **DTR/RTS saem desligados ANTES de abrir**. Em placas ESP32-S3 esses dois
sinais são o circuito de reset: abrir a porta do jeito padrão do pyserial
reinicia a placa, e a sondagem entra num ciclo em que ela nunca termina de
bootar. Já aconteceu neste projeto, com o display do painel de bancada. Sondar um
dispositivo não pode reiniciá-lo.

### Enquadramento: uma linha JSON, e o lixo antes do `{` não mata a mensagem

O firmware imprime log e JSON no mesmo Serial, e eles saem grudados
(`Iniciando HX711...{"cmd":"ping"}`). Exigir que a linha comece com `{` descarta
justamente os pings do boot — que são os primeiros a chegar e são o que
identifica a porta. O `extrair_json` do painel de bancada foi copiado com o
comentário que explica por quê; linha sem JSON válido é log, nunca exceção.

A linha tem teto (1024 bytes), e ele é conferido nos DOIS sentidos com decisões
OPOSTAS, de propósito:

- **entrando**, linha grande é descartada inteira e logada — nunca partida em
  duas, porque meia linha é JSON inválido e as duas metades sumiriam em silêncio
  mais adiante;
- **saindo**, comando grande **falha** em vez de sair truncado. Comando truncado
  é JSON inválido, a placa o descarta, e o adapter fica esperando um ACK que
  nunca vem. Falhar aponta para o comando; truncar apontaria para a placa.

O teto sai da ORIGEM do dado — a maior mensagem do contrato é o evento `peso_ok`
—, não do tamanho que as mensagens têm hoje. É a mesma regra do truncamento de
`os_id` no firmware do display.

### Telemetria periódica não pode competir com o caminho crítico

O dispenser emite status dos 8 slots a cada 15s e a balança tem leitura contínua.
Num canal de 115200 baud compartilhado com os ACKs que o adapter está esperando,
despejo periódico compete com a OS em andamento — coisa que no HTTP, com uma
conexão por evento, não acontecia.

A placa manda transição na hora e periódico com intervalo configurável; o adapter
descarta telemetria **repetida idêntica** (comparação ignorando `ts`) em vez de
encaminhá-la ao central. É a mesma regra que o central já aplica no broadcast do
WebSocket, e a mesma linha: transição nunca é filtrada, porque atraso ali é
atraso de decisão do operador — e porque duas dispensas idênticas são dois fatos,
não um repetido.

### Três cópias de `serial_link.py`, e um teste que as compara

O arquivo é idêntico nos três adapters, e `tests/test_serial_link.py` reprova se
divergirem — a mesma forma de `tests/test_adapters.py` com o `_post_central`.

Cópia e não `shared/` porque cada adapter tem o seu próprio contexto de build no
compose (`build: ./cnc-adapter`): compartilhar obrigaria os três a virarem
`context: .` + `dockerfile:`, arrastaria o repositório inteiro para dentro das
três imagens e passaria a exigir `.dockerignore`. É a avaliação que a seção "Os
templates moram no CENTRAL" já registra — com a diferença de que lá a alternativa
era um GET, e aqui não existe GET que resolva: este código roda antes de haver
rede. **Se um dia existirem cinco cópias, a conta muda**; com três, o teste de
igualdade custa menos que a infraestrutura.

O corolário de sempre, e ele mordeu aqui: arquivo novo no adapter exige que o
Dockerfile o copie. O `weight-adapter` fazia `COPY main.py .` e teria subido sem
o `serial_link.py` — a imagem construiria, e o container morreria no import.
Hoje os três fazem `COPY . .`, e há teste cobrando isso junto com a linha do
`pyserial` no `requirements.txt`.

### Onde a porta é aberta: Linux é o alvo, RFC2217 é o plano B

O código **não sabe** em qual sistema operacional está, e isso é regra:
`serial.serial_for_url()` aceita `/dev/ttyUSB0`, `COM4`, `socket://h:p`,
`rfc2217://h:p` e `loop://` com a mesma API. Cada adapter recebe UMA variável
(`<SUB>_SERIAL_URL`) e não ramifica por sistema operacional em lugar nenhum.

- **Alvo: mini PC com Linux.** As portas entram nos containers pelo `devices:`
  do compose. Nenhuma peça nova, o kernel garante dono único da porta, e o
  mapeamento fica declarado no mesmo arquivo onde o resto da topologia já está.
  O bloco está escrito e COMENTADO nos três serviços: mapear um device que não
  existe impede o serviço de subir, e serviço que não sobe trava, por
  `depends_on`, tudo que espera por ele.
- **Plano B: Windows.** O Docker Desktop não repassa porta COM para container.
  Uma ponte RFC2217 no host expõe cada porta como socket TCP e o adapter abre
  `rfc2217://host:porta`.

**O custo do plano B está aqui porque ele some do código** — de dentro do
adapter, as duas montagens são a mesma chamada:

1. **latência a mais** em todo comando e todo evento, num canal que já concorre
   com telemetria;
2. **"cabo caiu" e "ponte morreu" produzem o mesmo sintoma.** No Linux, porta
   sumida é porta sumida; com a ponte, os dois viram "socket fechado", e quem lê
   o log não sabe se vai olhar o cabo ou reiniciar um processo do host;
3. **some a exclusividade de abertura que o kernel dá.** Dois processos abrem o
   mesmo socket TCP sem erro nenhum, e o resultado é o que o item 1 do contrato
   existe para impedir: duas partes escrevendo na mesma linha, bytes intercalados
   no meio de um JSON, e o outro lado descartando a linha inteira em silêncio.

Por isso o plano B é plano B, e não uma segunda opção de igual valor.

### Sem placa: as placas falsas

`tests/fakes/placa_{dispenser,cnc,weight}.py` falam o contrato inteiro por
`socket://` — um handler de URL do próprio pyserial, servido por um socket TCP em
localhost. Ou seja, o caminho de abertura exercitado é o de verdade; o que muda é
o que está do outro lado do fio. Nenhum teste abre porta física, e o `loop://` não
serviria: é loopback, e o que o adapter escreve volta para ele mesmo — não há duas
partes.

Elas são o duplo do firmware e também o documento executável contra o qual ele vai
ser escrito, o mesmo papel que `painel_operador/firmware/simulador_serial.py`
cumpre para o display de 7". `tests/test_protocolo_placas.py` confronta as três
cópias do contrato — o documento, os adapters e as placas —, e compara **campo a
campo**, não só nome de comando: nome certo com um campo a menos é o modo de
falhar mais provável, e o adapter repassa o evento cru, então o campo que some
não vira erro em lugar nenhum — vira coluna vazia no banco.

## Eventos de limpeza não têm `os_id`

Limpeza é operação de slot, não de OS: o payload `limpeza_ok` do
dispenser-simulator não carrega OS nenhuma. Por isso a chave de evento é
`limpeza:{dispenser_id}`, sem prefixo de OS — e, como consequência,
`_limpar_eventos_os` (que varre por `{os_id}:`) não a apaga. O helper
`orchestrator._liberar_slot` encapsula o par comando + espera e nunca levanta:
devolve `False` em recusa, HTTP falho ou timeout, para o chamador decidir sem
perder o erro que o trouxe até ali.

## A interface de :8051 é o app de manutenção, e a sigla antiga não volta

O que era a interface homem-máquina de chão de fábrica é hoje o painel onde o
**gestor da operação** acompanha as necessidades do sistema. O rename cobriu
diretório (`manut_web/`), serviço e container do compose (`manut_web` /
`apsen-manut`), targets do Makefile (`make log-manut`), fixtures
(`carregar_manut`), testes (`test_manut_*.py`) e toda a documentação. Só o nome
mudou: as dez abas, os callbacks e a porta 8051 são os mesmos.

Duas coisas NÃO mudaram, e confundi-las é o erro fácil:

- **As rotas `/manutencao/*` do central**, que já usavam esse nome desde antes.
  Rename de serviço não é rename de rota — comentário que chamava essas rotas
  pela sigla antiga foi reescrito; a rota, não.
- **A porta 8051**, que está no bookmark de quem opera.

O firmware ESP32 saiu junto. Falava MQTT (`PubSubClient`, tópicos `apsen/*`)
com um broker que não existe no compose desde a migração para REST/HTTP:
gravado, não conversava com nada. Migrá-lo era trabalho pendente há tempo demais
para continuar sendo pendência — o histórico do git guarda o layout de tela e a
pinagem para quem precisar.

A recaída é o risco real: rename espalhado por vinte arquivos volta por um merge
que ressuscita um comentário ou por um README copiado de versão antiga, e nada
disso quebra teste nenhum — o sistema segue funcionando com dois nomes para a
mesma coisa, que é o estado que o rename foi feito para acabar. Por isso
`tests/test_rename_manut.py` **varre os arquivos versionados** (`git ls-files`),
conteúdo e caminho, em vez de checar uma lista: pega o arquivo que ainda nem
existe.

Consequência prática, e ela vale para este arquivo também: a sigla antiga não
pode ser escrita por extenso em lugar nenhum — nem no teste que a proíbe, nem no
`test_compose.py`, que precisa afirmar que nenhum serviço a usa. Os dois a
montam por concatenação de duas metades, e o comentário no topo de cada um
explica por quê. Quem for documentar o rename escreve "a sigla antiga", como
aqui.

## O JWT não é a palavra final — o banco é

`_get_tecnico` decodificava o token e pronto. Duas consequências: técnico
desativado seguia com acesso total até o token expirar (8h), e a `role` do
token valia mesmo que o banco discordasse. Hoje toda requisição autenticada
revalida em `get_usuario` (que já filtra `ativo=1`) e **a role devolvida é a do
banco**, não a do token — é o que faz um token forjado ou desatualizado não
virar admin.

Cache de `AUTH_CACHE_TTL_S` (30s) para não ser uma query por request; as
mutações que o central conhece (desativar, ativar, editar role) chamam
`_invalidar_cache_usuario` e valem na hora. Falha do banco na revalidação é
401 — nunca "deixa passar por precaução".

`SECRET_KEY` default agora **impede o boot** (`config.validar_secret_key`), em
vez de só emitir warning: o valor está versionado no histórico público, e com
ele qualquer um assina `role=admin`. A saída de escape é explícita e visível
(`APSEN_ENV=dev`), nunca o silêncio. Todos os segredos — incluindo credenciais
do MySQL e senhas seed — saíram do compose para o `.env`, que o compose exige
com `${VAR:?mensagem}`.

### `_get_tecnico` é a porta ÚNICA, e o relatório voltou para ela

`GET /api/v1/relatorio/os/{os_id}` era a única rota autenticada que conferia o
token por conta própria (`decodificar_token` e pronto). Ela não herdou nada da
revalidação acima: técnico desativado perdia o app de manutenção inteiro e
seguia baixando, por até 8h, justamente o histórico de dispensação NOMINAL da
OS — o documento de maior valor para quem acabou de perder o acesso.

Hoje é `Depends(_get_tecnico)` como todas. `tests/test_seguranca.py` varre
`main.py` por AST atrás de qualquer função que chame `decodificar_token` sem
ser `_get_tecnico`: a metade criptográfica (o token foi assinado com a chave
certa) nunca pode voltar a valer sozinha, sem a metade que consulta o banco.

### O freio de força bruta serve duas portas

O console tinha freio; o `POST /auth/login` — que emite o JWT de `admin`, cujo
username do seed está no README — não tinha nenhum. As funções
(`console.registrar_falha` / `bloqueado` / `limpar_falhas`) nunca souberam o que
é `origem`, então compartilhá-las não custou parâmetro: o console usa o IP, o
login usa `login:{ip}|{username}`, baldes separados no mesmo dicionário. Um
segundo contador para a mesma regra divergiria no primeiro ajuste de janela.

**A chave é IP _e_ username, e isso é escolha.** Só o IP puniria o turno inteiro
por causa de um técnico que erra a senha — a bancada fala com o central por um
NAT só, e atrás de proxy todos caem no mesmo `client.host`. Só o username
deixaria qualquer um trancar a conta alheia de fora, que é negação de serviço
disfarçada de proteção. O par **não** cobre varredura de muitos usernames a
partir de um IP; cobrir isso pede um segundo balde, por IP e com teto mais alto.
O que o freio resolve é adivinhar a senha de uma conta conhecida.

### `limite` cru vira 1064, e 1064 não é erro de schema

O `?limite=` de `/os/historico`, `/dispensas`, `/cnc/historico` e `/alarmes` ia
direto para o `LIMIT %s`. `?limite=-1` é erro de SINTAXE no MySQL — uma rota de
leitura respondendo 500 porque alguém digitou um número negativo — e
`?limite=99999999` varre a tabela, o que em `dispensas` e `cnc_eventos` ocupa
uma conexão do pool e a memória do processo por bastante tempo.

Helper único (`_limite`, faixa 1..500), aplicado nas nove rotas com o parâmetro
— não só nas quatro do relato, porque as de `/manutencao/*` tomavam o mesmo
1064 e exigir JWT não protege o MySQL de nada. `/api/v1/visao/historico` perdeu
o `> 500` escrito à mão: ele era um teto sem piso, ou seja, a rota "com clamp"
também respondia 500 a `-1`.

Clamp e não `Query(ge=1, le=500)`: a validação do FastAPI responderia 422 a quem
hoje recebe dados, e o valor esquisito quase sempre vem de um dashboard montando
a URL, não de alguém pedindo o erro. `tests/test_limites.py` varre `main.py` por
AST e exige que toda função com parâmetro `limite` chame o helper.

## CORS: origem de browser é lista, não `*`

`allow_origins=["*"]` num serviço autenticado deixa qualquer página aberta no
mesmo browser do técnico disparar requisição em nome dele. Só duas origens falam
com o central pelo navegador — dashboard e app de manutenção —, então a lista
é explícita: `CORS_ORIGINS` (env, separada por vírgula, default
`http://localhost:8050,http://localhost:8051`).

Publicar fora da máquina local exige ajustar essa variável junto com a porta; é o
passo que se esquece, e o sintoma é o browser recusando a resposta sem que o log
do central acuse nada. Simuladores e adapters seguem com `*`: não autenticam nada
e nenhum browser fala com eles.

## Três invariantes de robustez do central

**Snapshot de `_estado` é `copy.deepcopy`, nunca `dict()`.** A cópia rasa
devolve os mesmos dicionários aninhados (`dispensers`, `cnc`, `visao`, `peso`,
`trava`), e a serialização acontece depois de soltar o lock. O `jsonable_encoder`
do FastAPI — e qualquer iteração em Python — levanta `RuntimeError: dictionary
changed size during iteration` se um evento chegar nesse intervalo. Pelo
encoder em C do `json.dumps` (o do WebSocket) não levanta: ele devolve
silenciosamente um snapshot inconsistente, que é pior de diagnosticar. Vale
para `get_estado`, para o `/ws` e para `_broadcast_estado`.

**Nada de banco no event loop.** Toda chamada a `database.*` no orquestrador
passa por `asyncio.to_thread`: uma query síncrona congela o central inteiro
enquanto o MySQL responde. `tests/test_orchestrator.py` varre o arquivo por AST
e falha se aparecer chamada direta — sem lista manual para alguém esquecer.
No mesmo espírito, o `gather` do peso unitário usa `return_exceptions=True`:
falha de banco em um item cai no fallback de 50 g
(`PESO_UNITARIO_PADRAO_G`) em vez de derrubar a OS inteira.

**A trava arma antes de aparecer.** `_ativar_trava` seta `_trava_ativa` e só
então publica `_estado["trava"]` e faz broadcast — nessa ordem, e dentro da
própria função, para que nenhum chamador precise lembrar dela. Publicar antes
abria a janela em que o supervisor vê a trava na tela, clica em "Liberar" e
`liberar_trava` responde 409 "nenhuma trava ativa"; a OS então fica travada
para sempre esperando um evento que já foi pedido e recusado.

## `verificar_senha`: hash malformado não pode virar 500

O caso que o try/except NÃO resolve: `bcrypt.checkpw` com hash truncado
(`"$2b$12$truncado"`) faz o backend Rust do bcrypt 4 entrar em pânico —
`pyo3_runtime.PanicException` herda de `BaseException`, não de `Exception`, e
o módulo nem é importável para se pôr no `except`. Por isso a defesa é validar
o formato do hash ANTES da chamada (`_RE_HASH_BCRYPT`); o try/except cobre só
o resto (`ValueError: Invalid salt` para lixo em geral).

Sobre senha longa: no bcrypt 4.1.3 `checkpw` com mais de 72 bytes devolve
False, não levanta — quem levanta é `hashpw`. O try/except fica assim mesmo,
para não depender da versão instalada.

## O navegador não fala com o backend — quem fala é o app de manutenção

`BACKEND_URL` (`http://central-computer:8000`) é nome DNS da rede Docker
`apsen-net`: resolve DENTRO dos containers e em lugar nenhum além disso. Todo
uso dele tem que ser server-side. Os botões CSV/XLSX do app de manutenção eram
âncoras para esse host com `?token={jwt}` na query — não baixavam nada (o
navegador não
resolve o nome) e ainda deixavam o JWT no histórico, no `Referer` e no log de
acesso do central.

Hoje `manut_web._buscar_relatorio` faz o GET de dentro da rede, com o token no
header `Authorization`, e devolve os bytes pelo `dcc.Download`. Por isso o
`?token=` saiu de `/api/v1/relatorio/os/{os_id}`: não havia mais cliente para
ele, e query param é o pior lugar para credencial.

Preferido a uma `PUBLIC_BACKEND_URL` (localhost:8000) porque esta exigiria
expor o central ao navegador, manter mais uma variável em sincronia com o
compose por ambiente — e o token continuaria na URL.

Detalhe de teste: `_baixar_relatorio` é registrado por
`callback(...)(_baixar_relatorio)` em vez do decorador. O decorador devolve o
wrapper do Dash, que só roda dentro de um callback context; com o registro
explícito a função continua Python comum e `tests/test_manut_relatorio.py` a
chama direto.

## O dashboard tem UM ponto de I/O, e é o `_fetch`

`_render` fazia a busca de alarmes por dentro, somando uma QUARTA requisição a
cada `POLL_MS` (2s) **por cliente conectado** — fora do lugar onde as outras três
já estavam, e dentro de um callback que deveria ser função pura do que veio nos
stores.

Hoje `_fetch` busca as quatro (`/estado`, `/log/eventos`, `/os/historico`,
`/alarmes`) e publica cada uma no seu `dcc.Store`; `_render` só lê. É o que torna
o custo do dashboard previsível: uma rodada de I/O por tique, não uma por tique
mais uma por render por cliente. `tests/test_dashboard.py` falha se `_render`
voltar a fazer HTTP.

## Só transição vira linha; periódico é segurado no broadcast

`cnc_eventos` recebia também o `movendo`, que o cnc_simulator emite a cada
`INTERVALO` (0.5s) durante todo o movimento. Medido numa OS de 6 slots em
linha (o layout da época, rota HOME→D1..D6→HOME, 80 mm/s): **40 linhas, das
quais 33 são `movendo`** — a
trajetória que o dashboard já mostra ao vivo e que ninguém consulta depois.
Hoje só `posicionado`, `concluido` e `erro` são gravados: **7 linhas por OS,
−82,5%**. Rastro de trajetória continua possível, amostrado, por
`CNC_AMOSTRAGEM_MOVENDO` (0 = desligado, o default).

O broadcast tinha o mesmo problema pelo outro lado: cada evento custa
`copy.deepcopy` do estado inteiro + JSON + envio a todos os clientes.
`_broadcast_estado(prioritario=False)` segura os tipos periódicos —
`movendo`, `telemetria`, `status` — a no máximo um envio por
`BROADCAST_MIN_INTERVALO_MS` (500ms), e o `_broadcast_flusher` manda o snapshot
retido no tique seguinte para a tela não congelar numa posição velha.
**Transição não passa pelo throttle**: trava, fim de OS, alarme, carga,
dispensa e limpeza saem na hora — atraso ali é atraso de decisão do operador.

`leituras_sensores` não encolhe por evento (a telemetria é o dado), então
entra por retenção: `_loop_expurgo` roda no startup e a cada
`EXPURGO_INTERVALO_HORAS` apagando o que passou de `RETENCAO_DIAS`, em lotes de
5000 para não segurar lock. Sem ele, ~42 mil linhas/dia cresciam para sempre.

## As 10 ordens são fixas; o `os_id` de cada disparo, não

O erp-simulator não monta mais OS item a item. Existem dez ordens padrão, com
nome, categoria, itens e quantidades declarados em
`central-computer/os_templates.py`, e o que o gerador sorteia é **qual delas
dispara**. De fora a planta continua imprevisível; a diferença é que o conteúdo
de cada OS passa a ser ensaiável.

O que a separação prende: **template fixo, chave primária nova a cada disparo**.
`ordens.os_id` é UNIQUE e o central responde 409 `os_duplicada` a um reenvio —
sem sufixo, a segunda vez que uma ordem padrão fosse disparada seria recusada e
a demonstração acabaria no primeiro ciclo. O formato é
`{template_id}-{AAAAMMDDTHHMMSS}-{6 hex}`: o prefixo diz QUAL ordem é (aparece
no log e no dashboard sem consultar nada), o carimbo ordena os disparos de um
mesmo template, e o hex cobre dois disparos no mesmo segundo — que o console de
operação permite e o gerador, dormindo `INTERVALO_OS`, não. Colisão exigiria os
três iguais, e o resultado seria um 409 no log, nunca dose dobrada: quem decide
é o UNIQUE do banco, não o formato.

### Os templates moram no CENTRAL, e o gerador consome de lá

Foi decisão, não conveniência. O console de operação (servido pelo próprio
central) precisa listar as dez e disparar a escolhida. Definidas só no gerador
— um container sem porta e sem API —, o central precisaria de uma segunda cópia
da lista, e duas listas de dez ordens mantidas à mão divergem: o console
mostraria uma coisa e a planta dispensaria outra, **sem erro em lugar nenhum**.

A alternativa avaliada foi um `shared/` copiado nas duas imagens. Ela troca o
contexto de build de dois serviços (`build: ./x` vira `context: .` +
`dockerfile:`), arrasta o repositório inteiro para dentro das imagens e passa a
exigir `.dockerignore` — infraestrutura para resolver o que um GET resolve, num
serviço que já depende do central por `depends_on: service_healthy` e já fala
HTTP com ele.

Sobrou UMA duplicação: o formato do `os_id` (`os_templates.novo_os_id` ×
`simulator._novo_os_id`), três linhas, porque o gerador não importa módulos do
central. É tolerável e o motivo é o mesmo que torna a duplicação do mapa de
posições intolerável (ver "A cópia do mapa no cnc_simulator não existe mais"):
divergir aqui produz ids com aparência diferente — visível na primeira linha do
log — e nunca uma OS errada.

### Item declara nome e quantidade; `sku` sai do catálogo

Congelar o SKU no template criaria a chance de ele divergir do que está em
`medicamentos` — e o SKU é justamente o que a câmera do dispenser compara
(`sku_esperado`). Um SKU velho viraria `leitura_dispenser_divergencia` e trava
do Triple Check num slot só: o quadro exato de um medicamento trocado. Por isso
`instanciar` resolve `sku` e `categoria` do catálogo na hora, dos dois lados.

### A validação existe três vezes porque as três falham diferente

| onde | o quê | reação |
|------|-------|--------|
| import de `os_templates` | estrutura (2..N itens, quantidade 2..15, item repetido, `template_id`) | levanta `TemplatesInvalidos` |
| boot do central + `GET /api/v1/ordens/templates` | estrutura **e** existência no catálogo | log + campo `problemas` |
| boot do erp-simulator | existência no catálogo e nº de itens ≤ `NUM_SLOTS` | descarta o template quebrado |

O central **não** cai por template inválido: ele serve a planta inteira, e
derrubá-lo por um nome de medicamento digitado errado trocaria um problema de
demonstração por um de produção. Quem recusa disparar é o gerador — e ele
descarta só o template quebrado, porque uma ordem com nome errado não é motivo
para as outras nove pararem. Nenhuma sobrando, ele encerra: acordar a cada
`INTERVALO_OS` sem ter o que enviar esconderia o motivo, e `restart:
unless-stopped` já retenta.

Medicamento que não existe é o erro mais provável de aparecer em cima da hora
(alguém corrige um nome no template e ele deixa de casar com o catálogo), e o
sintoma sem validação seria oblíquo: OS com `sku` vazio, câmera sem o que
comparar, e uma "falha de leitura" num slot só.

Detalhe do endpoint: `problemas` vazio **não** significa "válido" quando
`catalogo_carregado` é `false` — com o banco fora, só as checagens estruturais
rodaram. Daí os dois campos serem separados.

### O serviço virou `erp-simulator`, e o nome diz de ONDE a OS vem

Ele deixou de gerar o conteúdo das ordens quando as dez passaram a ser fixas:
hoje escolhe uma e a despacha. O nome anterior descrevia uma geração que não
existe mais — o mesmo resíduo de nomenclatura que motivou o rename do app de
manutenção.

**Por que `erp-simulator` e não `os-dispatcher`.** Duas razões, e a segunda é a
que decide:

1. **Vocabulário.** Todo outro simulador desta planta é nomeado pelo que
   SUBSTITUI — `cnc_simulator`, `dispenser_simulator`, `vision-simulator`,
   `weight-simulator`. "dispatcher" nomearia o MECANISMO, e o serviço seria o
   único batizado pelo como.
2. **Arquitetura.** Este container não é uma peça do APSEN: é o sistema do
   hospital que manda a ordem PARA o APSEN. "os-dispatcher" continuaria
   descrevendo o que ele faz e escondendo de onde a OS vem — que é justamente a
   informação que faltava. O diretório já se chamou `sap_simulator`, então o
   nome novo é a categoria do que ele sempre foi (SAP é um fornecedor; ERP é o
   papel).

O rename cobriu diretório (`git mv`), serviço e `container_name` do compose
(`erp-simulator` / `apsen-erp-sim`), targets do Makefile (`make log-erp`),
prefixo de log (`[ERP-SIM]`), testes (`test_erp_simulator.py`) e a documentação.
`tests/test_rename_erp.py` varre `git ls-files` — conteúdo e caminho —, como o
do app de manutenção e pelo mesmo motivo: rename espalhado volta por um merge
que ressuscita um comentário ou por um README copiado de versão antiga, e nada
disso quebra teste nenhum.

**O que NÃO mudou**, e confundir é o erro fácil:

- **`GET /api/v1/gerador`**, a rota do interruptor de pausa. Rename de serviço
  não é rename de contrato — a mesma regra que manteve as rotas `/manutencao/*`
  no rename do app de manutenção. Junto com ela ficaram
  `_estado["gerador_pausado"]` (que o dashboard e o console leem) e
  `console.definir_pausa`.
- **As env vars** (`INTERVALO_OS`, `ESPERA_FILA_CHEIA`, `MAX_ESPERAS_FILA`,
  `RELOAD_CATALOGO_MIN`, `ESPERA_PAUSA`): estão no `.env` de quem já roda a
  planta, e nenhuma delas carrega o nome antigo.

Consequência prática de operação: `docker compose up -d` deixa o container
antigo órfão, porque o `container_name` mudou. `docker compose down` antes do
`up`, ou `up -d --remove-orphans`, resolve — e o volume do MySQL não é afetado
nos dois casos.

### `_carregar_catalogo` mudou de papel, não de existência

Ele continua com a retentativa longa, que também é a sincronização de boot do
gerador (o catálogo só existe depois do seed do `init_db`). O que mudou é o uso:
era a fonte do sorteio, hoje é a fonte da validação e de `sku`/`categoria`. Por
isso devolve `{nome: medicamento}` em vez de agrupar por categoria — e por isso
`MIN_MEDS_POR_OS`, `MAX_MEDS_POR_OS`, `QTD_MIN` e `QTD_MAX` saíram do compose:
não há mais nada a sortear com eles.

## O console de operação mora no central, e não duplica caminho nenhum

O central passou a servir uma interface própria em `/console`: a mesa de onde
se escolhe QUAL das dez ordens padrão entra e QUANDO. Página única
(`console.html` + `console_login.html`), HTML/CSS/JS servidos pelo próprio
FastAPI — sem Node, sem bundler e sem dependência nova no central. A parte
testável (senha, sessão, freio de força bruta, flag de pausa) fica em
`console.py`, que não importa FastAPI; `main.py` tem só as rotas.

### O disparo manual É o `POST /api/v1/ordens`

`console_disparar` monta o corpo com `os_templates.instanciar` — o mesmo que o
gerador usa — e **chama `receber_ordem`**, a função do endpoint, devolvendo a
resposta dela com o status intacto. Não é uma reimplementação "equivalente":
409, 429 e 503 chegam à tela porque é o mesmo código respondendo.

O motivo é o mesmo do mapa de posições da CNC, mas o sintoma seria pior. Duas
portas de entrada de OS divergem no primeiro ajuste de contrato, e a que fica
para trás é justamente a que um humano usa sob pressão — com o agravante de
que a divergência não quebra teste nenhum: as duas continuam criando OS, só que
com regras diferentes.

Pelo mesmo princípio, `_liberar_trava` foi extraída de `liberar_trava`: o
endpoint de admin e o console autenticam diferente e fazem a MESMA coisa
depois. O passo que uma cópia esqueceria é o `_estado["trava"]` — a faixa
vermelha ficaria na tela com a OS já rodando.

E `sku` sai do catálogo na hora, como no gerador. Catálogo indisponível
**recusa** o disparo (503) em vez de instanciar com SKU vazio: sem SKU a câmera
do dispenser não tem o que comparar e o Triple Check perde uma fonte em
silêncio.

### Senha própria, e sem `CONSOLE_SENHA` o console não existe

`CONSOLE_SENHA` é independente do JWT de propósito. Amarrar o console ao login
do app de manutenção exigiria abrir o app que ele existe para não precisar
abrir, e criaria mais uma conta com poder de disparar OS — conta que sobrevive
à apresentação e que ninguém lembra de desativar.

**Vazia = console desabilitado**: toda rota `/console*` responde 503 e
`senha_confere` recusa qualquer coisa, inclusive a string vazia. Nunca uma
senha default embutida — ela ficaria versionada e abriria o disparo de OS para
quem lesse o repositório. É a mesma lógica de `validar_secret_key`, com a
diferença de que aqui a saída é desligar o recurso, não derrubar o boot: o
console é opcional e a planta roda sem ele.

**503 e não 404.** As duas escondem o console de quem não tem a senha e nenhuma
das duas o abre, então a escolha se decide pelo outro leitor — o operador que
configurou errado. 404 manda essa pessoa caçar o problema na URL, no build ou
no proxy; 503 dizendo "defina CONSOLE_SENHA" encerra o assunto numa linha. O
que um atacante ganha com a diferença é saber que existe um console, o que este
repositório já conta.

A sessão é `{expiração}.{HMAC(expiração)}` num cookie `HttpOnly` com path
`/console` — não há tabela nem segundo sistema de usuários, porque não há
usuário. A chave do HMAC é derivada de `SECRET_KEY` **e** de `CONSOLE_SENHA`:
trocar a senha revoga as sessões abertas, o que sem isso exigiria trocar também
a `SECRET_KEY` e derrubar os técnicos do app de manutenção junto.

Detalhe de implementação que parece gratuito e não é: o login lê o corpo com
`parse_qsl`, não com `request.form()`. O Starlette exige `python-multipart`
mesmo para `application/x-www-form-urlencoded` (ele checa a dependência antes
de escolher o parser), e a task pede para não acrescentar dependência ao
central sem necessidade real.

### A pausa do gerador é um flag que ELE consulta

O erp-simulator é outro container. Fazer o central pará-lo exigiria socket do
Docker montado, privilégio de administrador da máquina e um acoplamento novo
entre o central e o runtime que o hospeda — tudo para não dispensar medicamento
por alguns minutos. Em vez disso o central publica um booleano em
`GET /api/v1/gerador` (sem autenticação, como `/api/v1/fila`: quem lê é um
serviço da rede interna, que não tem JWT) e o gerador o consulta antes de cada
envio. O container segue de pé, e volta a produzir no instante em que o console
despausa.

A fonte do flag é `console._pausado`; `_estado["gerador_pausado"]` é
**publicação**, escrita pelo mesmo setter e na mesma ordem de `_ativar_trava`
— estado real primeiro, tela depois. É o que dá o estado ao vivo ao console
pelo `/ws` que já existia, sem polling novo.

**A pausa não é persistida, e isso é escolha.** Restart do central retoma o
automático. O modo default do sistema é gerar OS; uma pausa gravada em banco
sobreviveria à apresentação que a motivou, e o sintoma — planta em silêncio,
fila vazia, nenhum erro em lugar nenhum — é dos piores de diagnosticar.
Retomar sozinho erra para o lado visível.

Do lado do gerador, `_aguardar_retomada` espera SEM TETO, ao contrário do
backpressure de fila cheia. Fila cheia é a planta pedindo tempo e vale tentar
de novo depois; pausa é uma pessoa dizendo "eu assumo daqui", e desistir da
espera para disparar uma OS seria exatamente competir com ela. Central mudo
devolve "pode gerar", pelo mesmo motivo de `_esperar_vaga_na_fila`: consulta
auxiliar que falha não pode parar a planta.

Pausar **não** bloqueia o disparo manual. Se bloqueasse, o botão que existe
para operar à mão ficaria inútil no único modo em que ele é usado.

### Um clique para disparar, dois para liberar a trava

Disparo é a ação de rotina do console e a confirmação em dois passos é o que
faz a mão errar no ensaio; liberar a trava do Triple Check retoma uma OS com a
divergência registrada e não resolvida, então essa pede `confirm()`. A regra
vale para o que as tasks seguintes acrescentarem: destrutivo confirma, rotina
não.

### O protocolo serial tem TRÊS cópias, e agora um teste as confronta

O display fala com o backend por uma linha JSON por mensagem, e o contrato é
escrito à mão em três lugares, **em duas linguagens**: `firmware/src/main.cpp`
(C++, quem pergunta), `backend/app.py` (Python, quem responde) e
`firmware/simulador_serial.py` (Python, o duplo sem placa).

Divergir aí **não quebra nada visivelmente**, e os dois modos são simétricos: o
display manda um `cmd` que ninguém trata e fica parado até o `serial_request`
estourar; ou o backend responde com uma tag de `resp` que o display não aguarda
(`s2_awaited_resp_type` não casa) e a linha é descartada em silêncio. Os dois
aparecem como "tela vazia, display OFFLINE, nada no log".

`tests/test_protocolo_serial.py` prende o contrato inteiro — 9 `cmd`, 7 tags de
`resp`, 3 `event`, 2 `push` — nas três direções que importam: todo `cmd` do
firmware tem tratamento no backend, todo `resp` esperado é emitido, todo `push`
enviado é reconhecido, e o simulador atende o MESMO conjunto que o backend.

**A extração é o ponto frágil, e ela tem guarda própria.** Uma das cópias é C++,
então a comparação é por texto — e regex que para de casar deixa todos os testes
verdes para sempre, inclusive com o protocolo quebrado. Duas defesas: cada
extrator tem um piso de quantos símbolos precisa achar, e a primeira versão
desta comparação foi validada por MUTAÇÃO (quebrar o protocolo de propósito de
cada lado e exigir vermelho — 6 de 6).

Três armadilhas de extração que custaram falso positivo e estão registradas no
próprio teste:

- **Linha de `ack` não é comando enviado.** `{"ack":"ok","cmd":"nova_ordem"}` é
  o firmware CONFIRMANDO um comando de debug recebido. Contá-la como envio faria
  o teste exigir que o backend tratasse `nova_ordem`, que o display nunca manda.
- **Despacho por tabela.** `_GET_RESP_TAG` (backend) e `_RESPOSTAS_GET`
  (simulador) mapeiam quatro `get_*` de uma vez; um extrator que só procure
  `cmd == "X"` não os vê.
- **O firmware monta JSON de dois jeitos**: literal (`{\"cmd\":\"ping\"}`)
  e ArduinoJson (`doc["cmd"] = "set_status"`). `ordem_concluida` sai por
  `snprintf`, não por `doc["event"]`.

**O buffer do firmware entra na conta.** `static char s2_buf[4096]`, e a linha
que não couber é DESCARTADA sem erro. O teste mede as respostas reais de
`get_catalogo` e `get_dispensers` contra esse teto — é a mesma família do
truncamento de `os_id` que a seção do firmware registra: o limite tem que sair
da ORIGEM do dado, não do tamanho que ele tem hoje.

**`validar_operador` espera 5 s, e não os 800 ms dos demais.** Não é folga: quem
confere o PIN é o backend, e conferir hash custa ~300 ms por operador ativo de
propósito. O bloco de documentação no topo da seção serial do `main.cpp` registra
isso — ele já esteve desatualizado (sem `validar_operador` e sem o push
`dispensers`), e hoje há teste cobrando que ele liste o protocolo real.
Documentação que mente sobre o contrato é pior que documentação nenhuma: quem lê
para de conferir o código.

### O `requirements.txt` do painel não pode ser conferido pela suíte

`central_client.py` importa `requests` no topo e `app.py` importa
`central_client` incondicionalmente — mas `requests` **não estava** no
`requirements.txt`. Seguindo o README à risca, num venv limpo, o painel não
subia.

A suíte não pega isso **por construção**: `carregar_painel` troca `requests` no
`sys.modules` para nenhum teste fazer rede, então a dependência ausente fica
invisível para ela. Só aparece num venv limpo — que é justamente o que o README
manda criar. Vale a regra geral: **import novo no painel exige linha nova no
`requirements.txt`, e nenhum teste vai lembrar disso por você.**

## O painel de bancada espelha o central, e o espelho é de mão única

`painel_operador/` é o painel do operador de chão de fábrica: um backend Flask +
SQLite na porta 5000 e o firmware de um display de 7" ligado a ele por USB
serial. Ele **mostra** as ordens que a célula executa e o estoque que ela mede;
ele **não comanda** a célula. Toda a integração é de leitura, por endpoints que
o central já expõe sem autenticação (`/os/historico`, `/os/{os_id}`,
`/dispensers/estado`), e nenhum arquivo do central, dos adapters, dos
simuladores ou do compose mudou para ela existir.

**A mão única não é conservadorismo — é a única direção que não mente.** O
caminho de escrita óbvio seria `PUT /ordens/{os_id}/status`, e ele é
exatamente o errado: exige JWT de técnico e, pior, só grava a coluna `status`
do banco — **não fala com o orquestrador**. Um botão "Iniciar" no display
ligado nele mudaria a linha do banco enquanto a célula continua fazendo outra
coisa, e o painel passaria a afirmar um estado da planta que ninguém produziu.
Um espelho que escreve é um espelho que mente. Por isso `central_client.py` só
tem `GET`, e `tests/test_painel_ordens.py` falha se um `requests.post/put/...`
aparecer lá.

O corolário está no código, não só aqui: linha de `ordens` com
`origem='central'` é só-leitura nos **três** caminhos que escrevem status — web,
`PUT /api/ordens/<id>/status` e o `cmd: set_status` do serial. Os dois últimos
convergem em `_set_status_by_numero_os`, então o bloqueio mora lá e cobre os
dois de uma vez; a rota web escreve o próprio `UPDATE` e por isso repete a
checagem. Esquecer um deles não quebra teste algum por si só — é uma porta por
onde o painel volta a comandar.

### O espelho mora na tabela `ordens`, não em uma paralela

`ordens` já é o que a web, a API e o display leem. Um espelho em tabela separada
obrigaria cada uma dessas consultas a virar duas e a ser unida à mão, e a
primeira que alguém esquecesse de duplicar mostraria meia planta — sem erro no
log. Morar junto custa duas colunas (`origem`, `os_id_central`) e a disciplina
de que `origem` é regra: o UPSERT nunca toca em linha `local`, e a tela
`/ordens/nova` continua criando ordens locais que o central ignora, com baixa
FEFO como sempre.

O que a proximidade das duas populações torna fácil de errar é o estoque.
**Ordem espelhada não passa por `consumir_fefo`, `verificar_estoque_ordem` nem
`processar_conclusao_ordem`** — quem deu baixa foi a célula. O sync grava o
status por `UPDATE` direto justamente para não cair em
`_set_status_by_numero_os`, que dispara a conclusão; como o laço roda a cada
`CENTRAL_SYNC_S` (5s) e o histórico do central traz sobretudo OS concluídas, o
erro não seria "dobrar o consumo" e sim multiplicá-lo por uma vez a cada 5
segundos.

### O painel não é serviço do compose

Ele é dono de uma porta serial USB e roda no Windows da bancada. Container
Linux não enxerga a COM do host, então não há serviço no `docker-compose.yml`
nem `Dockerfile` — e a ausência é decisão, não pendência. A consequência
prática é que `CENTRAL_URL` aponta para `localhost:8000` (a porta publicada), e
não para `central-computer:8000`, que é nome DNS da rede `apsen-net` e só
resolve dentro dos containers — a mesma armadilha registrada em "O navegador
não fala com o backend".

`PAINEL_CENTRAL=0` desliga a integração inteira e devolve o painel ao
comportamento anterior a ela. Não é enfeite de configuração: a bancada precisa
funcionar com o central desligado — em feira, em treinamento e no dia em que o
Docker não sobe. Pelo mesmo motivo `central_client` **nunca levanta**: timeout,
conexão recusada e JSON inválido viram log e retorno vazio. Quem chama daqui é
o processo que possui a porta serial do display e serve a tela que o operador
está olhando; uma exceção de rede que suba não derruba "a sincronização",
derruba a thread. E o fallback de `_dispensers_data` — central fora do ar volta
ao cadastro local — existe pelo mesmo cálculo: tela em branco por rede caída é
pior que estoque um pouco velho.

### `Erro` e `Cancelado` são palavras novas no painel

O vocabulário do central (`aguardando | em_andamento | concluida | erro |
cancelada`) é traduzido em UM lugar: `STATUS_CENTRAL_PARA_PAINEL`, em
`central_client.py`. Os dois últimos não existiam no painel, e o modo de falhar
era silencioso — as telas somam por igualdade de status, então uma OS que a
célula abortou existiria no banco, apareceria na lista e não seria contada em
lugar nenhum: o painel diria que a planta está em dia. Por isso dashboard,
`/ordens`, `/relatorio`, `/kpis` e `/api/resumo` ganharam os dois contadores, e
`/ordens` ganhou o filtro.

Status desconhecido cai em `Erro`, **não** em `Pendente`. Um estado terminal
novo exibido como fila deixaria o operador esperando a célula executar uma ordem
que já acabou.

#### E o vocabulário é FECHADO — a escrita também

O corolário faltava do lado da escrita: `/ordens/<id>/status/<status>` tirava o
status da URL e o gravava como veio. `POST /ordens/1/status/Qualquer` respondia
302 e a ordem desaparecia de todos os contadores — mesmo sintoma do parágrafo
acima, agora produzido por dentro. A API (`PUT /api/ordens/<id>/status`) e o
`cmd: set_status` do display tinham o mesmo buraco, porque mandam o status como
texto livre.

`STATUS_VALIDOS` fecha a lista, e a checagem mora nos dois pontos que escrevem:
a rota web (que faz o próprio `UPDATE`) e `_set_status_by_numero_os` (que cobre
a API e o serial de uma vez) — a mesma geometria do bloqueio de ordem
espelhada, pelo mesmo motivo. O central já fazia isso no vocabulário dele
(`alterar_status_os`); esta é a metade de cá.

A lista é **derivada** de `STATUS_CENTRAL_PARA_PAINEL`, mais `Pausado` (só
local) e o fallback `STATUS_DESCONHECIDO`. Escrita à mão, ela ficaria para trás
de um status novo do central e passaria a recusar justamente o valor que o
próprio espelho acabou de gravar.

### `medicamentos.quantidade` conta saldo DISPENSÁVEL — lote bloqueado não entra

O painel guarda estoque em dois lugares: `lotes.quantidade`, linha a linha com
validade e fornecedor, e `medicamentos.quantidade`, o agregado que toda tela
mostra e que `verificar_estoque_ordem` consulta para liberar uma ordem.

`bloquear_lote` mexia só em `lotes.status`. O agregado seguia contando o saldo
bloqueado, e a partir daí a cadeia inteira funcionava sem erro em lugar nenhum:
`verificar_estoque_ordem` liberava a ordem → `consumir_fefo` não achava lote
'Ativo' → caía no ramo do resíduo → gravava `"SEM LOTE REGISTRADO"` → debitava
o agregado assim mesmo. Ou seja: o medicamento saía, e saía justamente do lote
que a tela dizia ter bloqueado, **sem genealogia**. Num painel cuja razão de
existir é rastreabilidade de lote, é o pior modo de falhar — bloquear
"funciona" na tela e não protege nada.

**Por que NÃO derivar o agregado de `SUM(lotes WHERE status='Ativo')`.** Seria
a forma de ter um número só, que é o que este arquivo defende em toda parte
(ver "A cópia do mapa no cnc_simulator não existe mais"). Aqui ela não cabe, e
por duas razões concretas:

1. **`medicamentos.quantidade` não é uma soma de lotes — é um número MEDIDO.**
   A estação de visão o reescreve, o espelho do central o reescreve
   (`_sync_dispensers_data`), a troca de medicamento do slot o reescreve, e o
   cadastro permite corrigi-lo à mão. Nenhum desses caminhos tem lote em que
   escrever: derivar obrigaria cada um a **inventar uma linha em `lotes`** —
   dado de rastreabilidade fabricado, que é o oposto do objetivo — ou a ter sua
   medição descartada em silêncio na leitura seguinte.
2. **Estoque sem lote cadastrado existe e é legítimo** — é o que o ramo
   `"SEM LOTE REGISTRADO"` cobre. Derivado, todo medicamento sem lote leria
   zero e o painel recusaria toda ordem sobre uma prateleira fisicamente cheia.

Então os dois números coexistem. O que criou o bug não foi a coexistência: foi
ela não ter invariante declarada nem dono. O dono é **`_mover_saldo_lote`**, e
toda transição que muda a dispensabilidade de um lote passa por lá. A função
soma e subtrai, não reconcilia — chamá-la duas vezes no mesmo sentido
duplicaria o saldo, e por isso quem decide a direção é o chamador, a partir do
status atual.

O corolário está em `baixa_lote`, e é o mesmo erro pelo outro lado: ela
descontava o saldo do agregado incondicionalmente. Lote bloqueado já saiu do
agregado no bloqueio, e descontá-lo de novo apagaria do estoque unidades que
estão na prateleira — sendo que bloquear→baixar é o caminho NORMAL (bloqueia-se
para investigar, baixa-se quando se confirma a perda).

Quem acrescentar um status de lote acrescenta em `_mover_saldo_lote` e na
constante ao lado, não em mais uma rota.

### As FKs do painel não valiam, e apagar medicamento renumerava a bancada

No SQLite o `foreign_keys` nasce DESLIGADO em toda conexão nova, e `get_db` não
o ligava: o `REFERENCES medicamentos(id)` de `lotes` era decoração — o banco
aceitava linha filha sem pai e não reclamava nunca. Some a isso um
`excluir_medicamento` que fazia `DELETE` incondicional, e o resultado tinha duas
metades, nenhuma visível na tela:

1. **Órfãos.** `lotes`, `lotes_baixas`, `ordem_lotes_consumidos` e
   `estoque_visao` apontam para `medicamento_id`, e a linha apontada sumia. Num
   painel cuja razão de existir é rastreabilidade, o registro de QUAL lote saiu
   em QUAL ordem passava a apontar para o vazio.
2. **Renumeração da bancada inteira.** `_slot_para_id` e
   `_dispensers_data_local` numeram o slot pela POSIÇÃO da linha (`i + 1`), não
   pelo id. Apagado o medicamento da posição 3, o que era D4 vira D3 e D5 vira
   D4: todo o estoque do display muda de slot, sem erro e sem log.

O PRAGMA ligado cobre a primeira metade só para `lotes`, que é a única com FK
declarada. **Por isso a decisão não pode ficar com o banco:** `_tem_historico`
consulta as quatro tabelas e, havendo rastro, a rota grava `ativo=0` em vez de
apagar. O `DELETE` sobrevive para o caso em que é inofensivo — cadastro errado
de cinco minutos atrás, sem lote, sem baixa, sem ordem e sem leitura — porque
desativar sempre deixaria lixo permanente na tela. `IntegrityError` de uma FK
que a lista não conhece cai na desativação, nunca em 500.

**A linha ficar é o que conserta a segunda metade,** e daí uma regra que parece
esquecimento e não é: `_slot_para_id` e `_dispensers_data_local` **não filtram
`ativo=1`**. Filtrar ali desfaria a correção pelo outro lado — o mesmo
deslocamento silencioso, produzido por um WHERE em vez de um DELETE. Quem filtra
é o seletor de entrada de lote: dar entrada de lote novo num item retirado do
catálogo é criar estoque para o que a operação já decidiu não usar. O saldo que
o medicamento já tem fica onde está — desativar é tirar do catálogo, não apagar
o que está na prateleira, e o espelho do central continua medindo aquele slot.

`reativar_medicamento` existe porque desativar sem caminho de volta seria uma
porta de uma folha: quem clicasse por engano ficaria com a linha na tela,
marcada como inativa, e sem nada a fazer.

### O slot do display é `dispenser_id`, não a posição na lista

`_dispensers_data` montava a bancada numerando as linhas de `medicamentos` por
posição (`i + 1`). Batia com os 8 slots da célula **por coincidência**: a
primeira exclusão de medicamento no cadastro local deslocaria a bancada inteira,
e o sintoma seria estoque trocado de slot sem erro nenhum. Hoje o número vem de
`GET /dispensers/estado` — `dispenser_id` é o slot. `minimo`, lote e validade
continuam saindo do cadastro local, casados por nome, porque o central não tem
nenhum dos três.

A escrita seguiu a leitura: `sync_dispensers` e `set_dispenser_med` recusam slot
espelhado, pelo mesmo motivo pelo qual já ignoravam o dispenser sob a câmera —
quem manda no número é quem o mede. Com o central fora, o bloqueio de escrita
cai junto com o espelho de leitura: as duas metades precisam concordar, senão o
painel ficaria sem poder ler e sem poder escrever ao mesmo tempo.

### Duas armadilhas de infraestrutura que a chegada da pasta revelou

**O `.gitignore` da raiz tinha `backend/` sem barra inicial.** Regra legada da
era MQTT que, sem âncora, casa em qualquer profundidade: ela engolia
`painel_operador/backend/` inteiro. O commit levaria o firmware e **nenhum**
arquivo do backend, sem git dizer nada — e `test_rename_manut.py`, que varre
`git ls-files`, aprovaria um diretório que nunca chegou ao índice. As três
regras legadas foram ancoradas (`/backend/`, `/sap_simulator/`, `/mosquitto/`).

**A sigla antiga veio no firmware.** `main.cpp` tinha duas ocorrências (uma
string de log e um comentário), hoje "painel". Elas não estavam no índice quando
chegaram, então a varredura não as via — o vermelho só apareceria no primeiro
commit. Vale a regra geral: arquivo novo entra sem a sigla, inclusive em
comentário e mensagem de log.

### O painel tinha quatro portas abertas, e as quatro falhavam calado

`GET /api/operadores` devolvia nome, perfil e o **PIN em claro** de todos os
operadores ativos, sem autenticação. O login web é nome + PIN: a rota era a
senha de administrador servida em JSON. Ela **saiu** — não foi reduzida a nome
e perfil. Existia para o ESP32 buscar a lista por HTTP, e o display fala por
serial desde a migração; era superfície sem consumidor. O teste cobra a
ausência da rota, e não o formato do corpo, porque "devolve só nome e perfil" é
o estado do qual a versão anterior partiu.

**O PIN virou hash (`werkzeug.security` — já vem com o Flask, sem dependência
nova), e quem confere o PIN do display é o BACKEND**, por `cmd:
validar_operador`. A alternativa — mandar o hash e deixar o display comparar,
como ele fazia com o texto — não resolve nada aqui: o PIN tem 4 dígitos, são
10 mil candidatos, e o firmware ainda gravava a lista em `/operadores.json` no
cartão SD. Hash só protege entrada que não dá para **enumerar**; contra 10⁴
tentativas offline, qualquer algoritmo cai. Validar no backend traz de carona o
que faltava: operador desativado perde o acesso na hora, em vez de valer até o
próximo `fetch_operadores_api`.

O preço — não dar para logar no display com o backend fora do ar — é aparente:
o backend é o **único** canal do display, e sem ele não há ordem, catálogo nem
estoque na tela. Isso não contraria "a bancada precisa funcionar com o central
desligado" (ver `PAINEL_CENTRAL`): o central é outra máquina; este processo é o
dono da porta serial.

Consequência de custo, e ela é a razão de um número no firmware: conferir hash
custa ~300 ms **de propósito** (é o que separa um `.db` levado no bolso de todos
os PINs da bancada), e um PIN errado percorre todos os ativos. Por isso
`validar_pin_backend` espera **5 s**, e não os 800 ms de `serial_request`. É a
seção "O timeout de quem pergunta manda no de quem responde" ao contrário: aqui
quem responde demora por escolha, e encurtar o hash para caber no timeout
trocaria segurança por latência que ninguém percebe.

A coluna `pin` **não** ficou vazia: ela foi embora. Enquanto existisse
(`NOT NULL` sem default), todo INSERT que a omitisse quebraria — e a tentação de
"só voltar a preencher" continuaria de pé. Como `ALTER TABLE ... DROP COLUMN` só
existe no SQLite ≥ 3.35 e o painel roda no Python que a bancada tiver, a coluna
sai por **reconstrução** da tabela, depois de converter os PINs gravados. Banco
que já está na bancada migra sozinho e ninguém perde o acesso.

**Todo o bloco `/api/*` exige `X-API-Token`** (`api_token_required`, lendo
`APSEN_API_TOKEN`). São 16 rotas, e por elas se criava ordem, se reescrevia o
estoque com baixa FEFO real e se escrevia no audit log — tudo aberto na rede da
fábrica. `tests/test_painel_seguranca.py` varre o `url_map` e reprova rota
/api/* sem o marcador do decorator: sem lista manual para alguém esquecer.

**Sem `APSEN_API_TOKEN` a API responde 503 e o painel SOBE.** É a diferença
deliberada para `APSEN_SECRET`, que recusa o boot como no central. Sem segredo
de sessão, tudo que o painel serve é forjável — derrubar é proporcional. Sem
token, só o bloco `/api/*` fica sem dono, e derrubar o processo levaria junto a
ponte serial, ou seja, a tela que o operador está olhando, por uma variável que
a bancada talvez nem use. É o formato do `CONSOLE_SENHA` no central: segredo
ausente desliga o recurso, não o processo — e 503 em vez de 404 pelo mesmo
motivo de lá, porque quem vai ler a resposta é quem configurou errado.

**`debug=True` fixo em `0.0.0.0` saiu.** Debug só com `APSEN_DEBUG=1` e, ligado,
o bind cai para `127.0.0.1` sempre: o console do Werkzeug executa Python
arbitrário no processo que é dono da porta serial e do banco, e "console na rede
da fábrica" é a única combinação sem uso bom. O teste varre o `app.py` por AST
atrás de `app.run(debug=True)` com host diferente de `127.0.0.1`.

Em produção quem serve é o **waitress**, e a escolha mora no `app.py`, não no
`.bat`. `waitress-serve app:app` importaria o módulo sem passar por
`iniciar_workers()`: o painel subiria com a web de pé e sem a ponte serial —
display OFFLINE, espelho parado, nada no log. Um entrypoint só, e ele sempre
passa pelos workers.

### `iniciar_workers()`, e por que o import não sobe thread

As threads de fundo (ponte serial e espelho) subiam no topo do módulo. Duas
consequências: a suíte não podia importar `app.py` sem tomar posse de um
dispositivo USB, e o `desktop.py` — que importa `app` e depois iniciava a serial
ele mesmo — subia **duas** threads disputando a mesma porta. Hoje o start mora
em `iniciar_workers()`, chamada pelo bloco de execução e pelo `desktop.py`.
`tests/conftest.py` ganhou a fábrica `carregar_painel` no mesmo padrão das
outras: módulo importado por caminho, banco em `tmp_path` (via `APSEN_DB`), e
`serial`, `requests` e `central_client._get` dublados.

### O espelho grava sem `_set_status_by_numero_os` — e paga por isso um aviso

O sync escreve o status por `UPDATE` direto justamente para não cair em
`_set_status_by_numero_os`, que dispararia `processar_conclusao_ordem` sobre um
estoque que a célula já baixou. O que a decisão levou junto, sem aparecer: era
essa função que avisava o display.

E o display não descobre sozinho. `fetch_ordens_api` monta apenas ordem que ele
**ainda não conhece**, e a fila servida traz só `Pendente` e `Em Processo` — uma
OS concluída ou abortada **some** da lista em vez de mudar de estado. O
resultado era uma ordem congelada na tela do operador no status em que entrou,
com o SQLite do painel certo e a tela errada: nenhum erro em lugar nenhum.

Hoje `sincronizar_ordens_central` publica `ordem_status` na transição — e só na
transição, porque o laço roda a cada 5s e push por ciclo é push por ruído. O
firmware tem a rede de segurança do outro lado: ele refresca o status de ordem
**espelhada** que já conhece, porque push é uma linha serial e linha serial se
perde num reset. Ordem **local** conhecida ele não toca; ali o dono do estado é
ele.

A lição generaliza: **todo caminho que contorna a função canônica de escrita
herda os efeitos colaterais que ela fazia** — não só os que se quis evitar. É a
mesma forma do "toda saída de `_processar_os` deve fechar em status terminal".

### Estado terminal novo é slot que ninguém libera

O display guarda 5 ordens e recicla o slot de uma que já terminou. Ele
reconhecia só `Pronto`. Ao introduzir `Erro` e `Cancelado` no vocabulário do
painel, cada OS abortada passaria a prender um slot **para sempre** — e depois
de cinco abortos o display pararia de aceitar ordem nova, calado.

É o mesmo formato do bug que "Ciclo de vida de um slot de dispenser" registra do
lado do central: lá, OS abortada não devolvia o slot físico ao pool e depois de
~6 abortos toda OS nova era rejeitada com `sem_slot`. Acrescentar um estado
terminal obriga a varrer quem enumera os terminais — e nesta célula abortar é
rotina, não exceção: a 1% de erro mecânico, 2 de 5 OS travaram na medição.

### O timeout de quem pergunta manda no de quem responde

`get_dispensers` do display cai em `_dispensers_data`, que faz HTTP no central —
dentro da ponte serial. O `serial_request` do firmware desiste em **800 ms**;
`CENTRAL_TIMEOUT_S` é **3 s**. Central lento (de pé, mas sem responder) fazia o
display desistir antes de o backend ter a resposta, e o painel de estoque
congelava sem que o log do firmware apontasse para o central — a resposta
atrasada chega depois de o firmware ter zerado `s2_awaited_resp_type` e é
descartada em silêncio.

A saída foi cache curto (`DISPENSERS_CACHE_S`, 2s), não timeout menor: encurtar
`CENTRAL_TIMEOUT_S` para caber nos 800ms transformaria "central lento" em
"central ausente" em todo o resto do painel. Curto de propósito — estoque de
dispenser é o número que o operador confere contra a bancada.

### O firmware dimensiona buffer pela ORIGEM do dado, não pelo que costuma caber

`OrdemExpedicao.id` era `char[16]`, e `PendingAction.param1` também. O `os_id`
do central é `{template_id}-{AAAAMMDDTHHMMSS}-{6 hex}` — 36 caracteres num caso
real, e o `template_id` varia.

O que torna esse truncamento perigoso é ONDE ele corta. Dois disparos do mesmo
template diferem só no carimbo e no hexadecimal, ou seja, no FIM da string:
cortados em 16 bytes viram o mesmo id. Daí o `set_status` do display vai para a
ordem errada e o push de status do backend casa com a linha errada no `strcmp`.
Nada disso dá erro — é corrupção silenciosa, e é o motivo de os limites hoje
saírem de constantes nomeadas (`MAX_OS_ID_LEN` 64 contra o `VARCHAR(60)` do
central, `MAX_DESTINO_LEN` contra o `VARCHAR(200)` da descrição) em vez de
números escolhidos pelo tamanho que os dados tinham no dia.

O corolário é o do mapa de posições da CNC, do outro lado: onde uma cópia
diverge produzindo dado *visivelmente* diferente, tolera-se; onde ela produz
dado *plausível e errado*, não. `param1` recebeu a mesma constante do `id` pelo
mesmo motivo — um limite menor lá traria a colisão de volta só na fila offline,
que é onde ninguém está olhando.

Quando algo mesmo assim não couber, `copy_trunc()` termina em `...`. A exceção é
o resumo de itens, que trunca por ITEM: cortar no meio de um par `nome|qtd`
deixaria uma quantidade pela metade, e `descontar_itens_ordem` a leria como
outro número — truncamento que vira erro de estoque em vez de texto cortado.

### Botão que o backend vai recusar não é botão desabilitado — é botão ausente

A ordem espelhada não tem Iniciar / Retomar / Pausar / Concluir na tela do
display; tem um rótulo `CENTRAL` com o status. Popup de erro depois do clique
ensina o operador que aquela tela mente às vezes; a ausência do botão diz de
antemão quem manda naquela ordem. `origem` ausente vale `local`, para que o
contrato antigo (e o `simulador_serial.py`) não vire só-leitura por omissão.

Pela mesma lógica `queue_pending_action` não enfileira ação de ordem do central:
enfileirar é prometer tentar de novo quando o backend voltar, e a recusa aqui
não é do momento — é da ordem. E ordem espelhada nunca vira `ordem_atual`, que
é a tela de quem está com a ordem na mão; em troca ela aparece na LISTA mesmo em
execução (`Separando`), o que não vale para ordem local. Sem isso ela sumiria da
tela do operador justamente enquanto a célula a executa.

O firmware não compila na suíte, e nenhum teste finge que compila. Quem exercita
o protocolo sem placa é `firmware/simulador_serial.py`, que passou a **responder**
aos pedidos do display além de empurrar comandos — sem isso o display fica
OFFLINE e `fetch_ordens_api`, o caminho que recebe o `os_id` longo, nunca roda.

## A fila de OS tem teto, e o gerador respeita o teto

O sistema recebe mais rápido do que processa: o erp-simulator posta a cada
`INTERVALO_OS` (90s) e uma OS leva mais que isso — de 90 a 140s medidos na
célula de 6 slots, e a de 8 acrescenta dois ciclos completos (carga paralela,
scans e, por slot, CNC + dispensa + câmera da mesa + pesagem). Com a trava do
Triple Check ativa é pior: o loop ÚNICO do orquestrador para por tempo
indeterminado esperando um humano, e o gerador continua postando. Com
`asyncio.Queue()` ilimitada, isso crescia sem limite em memória e no banco.

O teto é `MAX_FILA_OS` (default 5), e mora no `maxsize` da própria fila — não
numa checagem que cada chamador precise lembrar de fazer. Ele conta quem
**espera**: a OS em execução já saiu da fila.

Três consequências que andam juntas:

1. `POST /api/v1/ordens` responde **429 `fila_cheia`** e não persiste nada. A
   recusa vem ANTES de `salvar_ordem` de propósito: linha "aguardando" que
   ninguém vai processar é devolvida por `get_ordem_ativa` como a OS ativa.
   Se a vaga sumir entre a checagem e o `put_nowait` (corrida), a OS já
   gravada é fechada em "cancelada" pelo mesmo motivo.
2. `enfileirar_os` usa `put_nowait` e devolve `False`, em vez de `await put`.
   O `await` deixaria o request do gerador pendurado até abrir vaga — que, sob
   trava, pode ser "quando alguém aparecer".
3. `GET /api/v1/fila` existe para o gerador consultar antes de gerar OS. O
   `/estado` também traz `fila_tamanho`/`fila_capacidade`, mas serve o snapshot
   inteiro e passa pelo contador de alarmes; quem só quer saber se cabe mais
   uma não deveria pagar isso a cada ciclo.

Do lado do gerador, `_esperar_vaga_na_fila` espera `ESPERA_FILA_CHEIA` segundos
por consulta, no máximo `MAX_ESPERAS_FILA` vezes, e então **pula o ciclo** —
sem `sleep` apertado e sem sair do processo. Fila indisponível (central
reiniciando) devolve "pode seguir": travar a geração por causa de uma consulta
auxiliar seria pior que tentar e receber a recusa. E 429, como 409 e 503, não é
retentado: a OS recusada é descartada e a próxima já nasce com `os_id` novo.

## OS não entra na fila sem linha no banco

`POST /api/v1/ordens` só enfileira depois que `salvar_ordem` confirma a
gravação. A função devolve **booleano**: `False` quando o INSERT IGNORE não
inseriu (a OS já existia) e exceção quando o banco falhou. Os dois casos
recusam, com os corpos do contrato (README, "Contrato de entrada de uma OS";
declaração executável no `responses=` do endpoint) — 409 `os_duplicada` e 503
`persistencia_indisponivel`, via `JSONResponse` porque `HTTPException`
embrulharia tudo em `detail`.

Antes, `receber_ordem` ignorava o retorno e engolia a exceção. Reenviar uma OS
respondia `{"aceita": true}` e a processava de novo — dose dobrada no leito. E,
com o banco fora, a OS era dispensada sem linha em `ordens`/`os_itens`:
`atualizar_item_os` e `atualizar_status_ordem` não achavam o que atualizar e o
relatório (`GET /os/{os_id}`, CSV/XLSX) saía vazio. Entre não dispensar e
dispensar sem rastro, um sistema de medicação escolhe não dispensar.

O erp-simulator não retenta recusa (`_enviar_os` devolve `False` e o `main()`
dorme `INTERVALO_OS` até a OS seguinte, que tem `os_id` novo): 409 não vira
laço, 503 não derruba o processo.

## `alarmes_ativos` é derivado do banco, nunca incrementado

O badge "⚠ N alarme(s)" era um contador em memória somado em oito pontos de
`main.py`. Só subia: `manut_resolver_alarme` marcava `resolvido=1` no banco sem
tocar nele, e um restart o zerava com a tabela cheia de alarmes abertos. Hoje o
valor vem sempre de `database.get_total_alarmes_ativos()` (COUNT, não
`len(get_alarmes())` — o LIMIT faria 101 abertos virarem 100), publicado por
`main._contar_alarmes_ativos()`.

Ele entra em todo payload de `_broadcast_estado()`, ou seja, em todo evento —
só a telemetria dos slots são 8 queries a cada 15s. Por isso a leitura tem
cache de `_ALARMES_TTL_S` (5s). **TTL, e não invalidação por escrita**, porque
o `main.py` não é o único produtor: `orchestrator.py` grava alarme de trava, de
abort e de `limpeza_pos_abort_falhou` chamando `salvar_alarme` direto. Um cache
invalidado só pelos caminhos do main ficaria atrasado desses para sempre; o TTL
converge sozinho. Os pontos que o central conhece — handler que gravou alarme
(detectado nas próprias `db_tasks`), resolução pelo app de manutenção e o
startup na `lifespan` — passam `forcar=True` e não esperam a janela.

## Triple Check: 1 divergência trava, e a balança precisa poder divergir

A regra vive em `orchestrator.avaliar_triple_check()` — função de módulo, pura,
com assinatura explícita. Era uma closure dentro de `_processar_os`, alcançável
só depois de encenar carga, CNC, visão e pesagem; testar a decisão custava
montar a OS inteira, então ela não era testada.

**O limiar é 1.** Ele era 2 — uma fonte solitária acusando erro virava alarme e
a OS seguia para o paciente, o que fazia do Triple Check um double check
enquanto o README anunciava a regra conservadora. Os dois erros não custam a
mesma coisa: parar uma OS boa custa uma liberação de supervisor, deixar passar
uma OS ruim custa medicamento errado — ou na quantidade errada — no leito.
Ajustável por `TRIPLE_CHECK_MIN_DIVERGENCIAS` (faixa 1..3; fora dela cai no
default com warning), para não exigir deploy caso uma fonte prove ser ruidosa
em campo. Com limiar > 1, a divergência que não trava vira o alarme
`divergencia_abaixo_do_limiar` — sem ele, elevar o limiar apagaria o rastro do
que passou por baixo.

**Trava é evento de rotina, não de exceção — e ela para a fila inteira.** Com
`PROB_ERRO_MECANICO=0.01` por unidade, uma OS que usa a célula inteira move
~70 unidades (eram ~50 com 6 slots — mais slots, mais chances por OS): a
chance de pelo menos uma falha mecânica é alta, e com limiar 1 *toda* falha que
a balança enxerga trava. Medido em execução contínua: 2 de 5 OS travaram a 1%,
e uma única OS chegou a travar duas vezes (slots diferentes) a 5%. O
orquestrador é um loop ÚNICO — enquanto a trava não é liberada, nenhuma outra OS
anda, e o `MAX_FILA_OS` enche por trás. Isso é a regra funcionando, não defeito;
mas significa que operar este sistema **exige supervisor disponível**, e que
medir "OS por hora" sem contar o tempo de liberação dá um número que não existe.

**Fonte que não mediu ≠ fonte que divergiu.** Timeout, `leitura_mesa_falha` e
`erro_sensor` vão para `fontes_indisponiveis`, não para `divergencias`: não
contradizem nada, apenas deixam de confirmar. É o que torna o limiar 1
sustentável — contá-las converteria os ~2% de falha de leitura da câmera em
trava por ruído, e trava por ruído é trava desligada em campo. O mesmo critério
já valia para a câmera do dispenser (SKU errado bloqueia, falha de leitura não).

**A exceção ao limiar 1: câmera da mesa contando a MENOS, sozinha, não trava.**
A câmera real é fixa num poste e olha a caixa de coleta inclinada, e a visão
(`vision/visao_mesa`, `_avaliar`) não tem critério de oclusão: caixinha
escondida atrás da parede da caixa e caixinha que não caiu dão a mesma foto, com
confiança alta. Contar a menos é divergência só se o dispenser ou a balança
também acusarem no mesmo veredito; sozinha, vai para `alertas` do
`ResultadoTripleCheck` e vira o alarme `contagem_camera_abaixo` — a OS segue, e
quem segura o caso é a balança. Contar a MAIS continua travando, porque não se
esconde caixinha a mais: é unidade extra ou objeto estranho. A diferença é
calculada no central, dos dois inteiros (`quantidade_detectada` − o esperado da
foto), e nunca lida do `delta` do payload; divergência com diferença zero é
payload incoerente e vira fonte indisponível. Decisão de 06/10/2026.

**E o que se escondeu num slot reaparece no outro.** A tolerância à oclusão
durava exatamente um slot. A estação REGISTRA o total da foto que contou a
menos (é a base da subtração seguinte), e a caixa anda com a CNC: o ângulo muda
e a caixinha que estava atrás da parede volta a aparecer na foto do slot
seguinte, como `q + 1` contra um esperado `q` — "a mais", trava, e no slot
vizinho, onde nada deu errado. Por isso o passo 4 guarda `deficit_mesa` (as
unidades contadas a menos, sozinhas, que ainda não reapareceram) e o passa a
`avaliar_triple_check` como `tolerancia_camera_mais`: "a mais" até esse número
é reconciliação (`reconciliado`, log INFO, sem alarme), e só o EXCEDENTE
trava, com o excedente no texto. Toda dessincronia zera o déficit — a conta
deixou de ser comparável —, e a conferência no HOME aplica o mesmo déficit. O
alarme `contagem_camera_abaixo` continua sendo gravado na hora da oclusão: é a
balança que segura o caso, e alguém precisa ver quantas vezes ele acontece.

### O comando de pesagem leva DUAS quantidades

O contrato de `/comandos/pesar` (orquestrador → weight-adapter →
weight-simulator) carrega os dois números, e confundi-los inverte o teste:

- `quantidade_esperada` — alvo da OS. Base do peso ESPERADO e, portanto, do desvio.
- `quantidade_real` — o `quantidade_dispensada` do evento do dispenser. Base do
  peso que a mesa efetivamente ganha.

A mesa crescia pelo esperado, então a balança comparava o valor consigo mesma:
uma falha mecânica que soltasse 8 de 10 unidades continuava "pesando" 10, e a
fonte 3 só divergia por ruído gaussiano (σ=2 g contra ≥100 g esperados —
praticamente nunca). A divergência tem que emergir da diferença entre depositado
e esperado, que é o que uma célula de carga mede na vida real.

`quantidade_real` ausente cai em `quantidade_esperada` nos três pontos do
caminho, preservando o contrato antigo para chamador que não tenha a contagem
do dispenser em mãos — mas o orquestrador sempre a envia.

## A tela de necessidades ordena por IMPACTO, não por gravidade

O app de :8051 tinha dez abas independentes, e descobrir o que precisava de ação
exigia visitar todas: a trava numa, os alarmes em outra, o resíduo dos
dispensers numa terceira. A primeira aba passou a ser a lista do que precisa de
alguém agora.

A ordem é `necessidades.PRIORIDADE`, e ela é uma tupla porque **a ordem É o
dado**:

| # | categoria | por quê aqui |
|---|-----------|--------------|
| 1 | trava | bloqueia a produção AGORA — o loop é único |
| 2 | fila no teto | a planta está recusando OS com 429; costuma ser consequência da trava |
| 3 | alarmes abertos | já falhou, ninguém fechou |
| 4 | componentes fora da faixa | vai falhar, ainda não falhou |
| 5 | resíduo nos dispensers | estoque imobilizado |
| 6 | OS em erro recentes | já aconteceu e acabou — é diagnóstico, não pendência |

Ordenar por gravidade "de manutenção" poria desgaste de correia acima de trava
ativa, e a tela deixaria de responder à pergunta que a motivou.

**Cada linha leva a uma ABA, e é isso que a faz ponto de PARTIDA.** Uma lista
que diz "há 3 alarmes" e não leva a lugar nenhum obriga o gestor a refazer a
navegação que a tela existe para poupar. O `index` do botão é o ID DA ABA, não a
posição na lista: ela é redesenhada a cada `POLL_MS` (5s), e um índice
posicional mandaria o gestor para a aba errada sempre que uma pendência
entrasse ou saísse entre o desenho e o clique. `tests/test_necessidades.py`
compara os ids emitidos contra a sidebar do `manut_web/app.py` — aba inexistente
é um clique que não faz nada, e nada no Python quebra por causa disso.

**Os limiares são os MESMOS com que as outras abas já pintam de vermelho**
(65 °C, 80% de desgaste). Um limiar próprio aqui faria a tela de necessidades
listar um componente que a aba de temperaturas mostra em verde — e quem visse as
duas duvidaria das duas.

**Alarmes são agrupados por FONTE.** Sem agrupar, uma câmera com problema emite
dezenas de linhas iguais e empurra para fora da tela a OS em erro que talvez as
explique. A fonte é o agrupamento certo porque é ela que diz ONDE ir —
`camera_dispenser_dir_7` manda o técnico à lente certa, que é exatamente por que
a fonte carrega a câmera (ver "Três câmeras, e o lado sai do slot").

**O slot da OS em execução fica fora do resíduo.** Ali o "resíduo" é a carga
sendo dispensada agora, e sugerir limpeza seria sugerir abortar a OS — a mesma
distinção que o `_do_limpar` do simulador faz ao recusar limpeza de slot em
operação.

**Teto por CATEGORIA, com uma linha de "e mais N".** Cortar em silêncio mentiria
sobre o tamanho do problema; não cortar faria vinte alarmes empurrarem todas as
outras categorias para fora da tela.

### O agregado existe, e o motivo é o mesmo do `_fetch` do dashboard

`GET /manutencao/necessidades` junta seis fontes numa resposta. A alternativa —
o app chamar `/api/v1/trava`, `/manutencao/alarmes`, `/manutencao/sensores`,
`/dispensers/estado`, `/os/historico` e `/api/v1/fila` — seriam SEIS requisições
por tique, por gestor com a tela aberta, e esta é a PRIMEIRA aba, ou seja, a que
fica aberta. É a mesma regra da seção "O dashboard tem UM ponto de I/O", e aqui
o custo é menor ainda: três fontes saem do `_estado` em memória e as outras três
viram um `gather` de consultas independentes.

**A decisão não mora no endpoint.** O que é pendência, em que ordem e com que
limiar vive em `necessidades.py`, que é puro — sem FastAPI, sem `database`, sem
HTTP. Mesma separação de `prevoo.py` e `seed_demo.py`, e pelo mesmo motivo: dá
para testar a prioridade inteira sem subir nada.

**Banco fora não apaga a tela, mas ela avisa.** Trava, fila e resíduo vivem em
memória e continuam corretos; alarmes, componentes e OS em erro somem. Por isso
o `gather` usa `return_exceptions=True` e a resposta carrega
`banco_disponivel: false` — "tudo em dia" com o banco sem responder é a
afirmação mais perigosa que este endpoint pode fazer.

E `tudo_em_dia` é falso com QUALQUER item, inclusive informativo: a tela promete
"não há nada pendente", e resíduo parado é pendência — pequena, mas pendência.

## O pré-voo responde "sim" ou "não", e o vermelho diz o que fazer

`GET /console/prevoo` é a tela que se lê cinco minutos antes de a banca entrar.
Antes dela, a pergunta "está tudo de pé?" custava abrir o dashboard, o app de
manutenção, o `docker compose ps` e o log de três containers.

**Item vermelho carrega uma AÇÃO, e é isso que a separa de um relatório.**
"mysql: falha" manda quem lê procurar sozinho, justamente quando não há tempo;
"suba o banco: `docker compose up -d mysql`" encerra o assunto. A regra é
cobrada por varredura em `tests/test_prevoo.py`: todo item `falha` ou `alerta`
tem `acao`, e todo item `ok` NÃO tem — ação em item verde é ruído que treina o
olho a pular a caixa amarela.

O par mais útil dessa regra: **serviço ausente e serviço travado pedem frases
diferentes.** "Não subiu" resolve-se com `up -d`; "subiu e não responde" só
olhando o log. Por isso `_sondar` separa timeout de recusa — pelo NOME da classe
da exceção, porque `prevoo.py` não importa httpx (nem FastAPI, nem `database`).

**Alerta não derruba o veredito, e isso é escolha.** Nem tudo que não está OK
está quebrado: catálogo vazio impede a demonstração, mas modo apresentação
ligado é informação que o operador precisa ter — e foi ligado de propósito. Uma
tela que responde "não" sempre deixa de ser lida.

### A tela não pode travar por causa de um serviço morto

É o caso em que ela é mais necessária. As onze sondas correm em `gather` com
timeout de 2s cada: o tempo total é o da mais lenta, não a soma. Em série, um
serviço morto faria a página levar mais de vinte segundos.

O teste mede a CONCORRÊNCIA (quantas sondas ficam em voo ao mesmo tempo), não o
tempo de parede — mesma escolha do teste do `gather` das etapas 3 e 3b, e pelo
mesmo motivo: cronômetro em suíte é flaky, e "demorou menos" não diz qual espera
sumiu.

`return_exceptions=True` é o cinto sobre o suspensório. `_sondar` já não levanta,
mas uma exceção que escapasse levaria junto o resultado das outras dez, e a
página apareceria em branco no dia em que um serviço quebrasse de um jeito novo.

### Treze serviços, três formas de verificar

| serviço | como |
|---|---|
| 11 com porta HTTP | sonda em `/ping` (ou `/`, nos dois Dash) |
| `central-computer` | **por dentro** — ele está servindo esta página |
| `mysql` | uma consulta, em `database.diagnosticar` |
| `erp-simulator` | não dá para sondar: worker de laço, sem porta |

O central **não se sonda por HTTP**: um auto-GET testaria sobretudo o event loop
que acabou de servir esta requisição, e um timeout ali produziria o relatório
absurdo "o central está fora" numa página que o central acabou de entregar.

O `erp-simulator` aparece assim mesmo, como item informativo. Sumir faria a
tela dizer "12 de 12" sobre uma stack de 13 — e o gerador pausado é justamente
uma das causas de "a planta não está fazendo nada".

**A tabela `prevoo.SERVICOS` é comparada contra o `docker-compose.yml`** — nome,
porta e caminho, no mesmo espírito de `test_compose.py`. Uma tabela desatualizada
faria a tela dizer "tudo verde" sobre uma stack que ela não conhece inteira, que
é o pior modo possível de falhar para uma página de conferência. E o caminho é o
MESMO do healthcheck: duas respostas diferentes para a mesma pergunta seriam pior
que nenhuma.

### Banco fora é UM item vermelho, não quatro

Com o MySQL sem responder, dizer também "schema incompleto", "catálogo vazio" e
"slots faltando" seria derivar três diagnósticos da mesma causa — e quem lê
acabaria caçando três problemas que não existem. Por isso `_prevoo_banco`
devolve `None` e `itens_banco(None)` emite um item só.

Pelo mesmo motivo, `item_templates` com o catálogo fora é **alerta, não verde**:
só as checagens estruturais rodaram, e verde ali seria um OK que ninguém
verificou. É a mesma ressalva que `GET /api/v1/ordens/templates` já faz com os
campos `problemas` e `catalogo_carregado` separados.

### Tela própria, e não mais um painel no console

O console é a mesa de OPERAÇÃO, usada durante a apresentação; o pré-voo é lido
antes e não volta a ser aberto. Misturá-los poria quatro dezenas de linhas de
conferência entre o operador e o botão de disparar.

`database.diagnosticar` lê tudo numa conexão só — uma consulta por item custaria
cinco empréstimos do pool para montar uma tela que existe para ser rápida. A
checagem de schema cobre a presença de toda tabela do `_DDL_TABELAS` e de toda
coluna do `_COLUNAS_EVOLUTIVAS`; não é a varredura completa que o
`tests/test_schema.py` faz por AST, e o alvo é outro: o banco ANTIGO que tem as
tabelas e não recebeu os ALTER. É esse o drift que aparece em campo.

## O reset recusa em vez de resetar pela metade

Repetir a demonstração exigia `docker compose down -v`: apaga o banco e leva
minutos. `orchestrator.resetar_planta` devolve a bancada ao estado de boot em
segundos — fila, trava, gatilho de injeção, estoque dos slots, balança e CNC.

**Ele recusa (409) enquanto houver OS em execução, e essa é a decisão.** O
orquestrador é um loop ÚNICO parado dentro de `_processar_os`, a meio caminho de
um ciclo de CNC; não há como interrompê-lo de fora sem cancelar a task do loop,
o que pararia o consumidor da fila e calaria a planta inteira. E resetar POR
CIMA de uma OS viva é pior que não resetar: `cmd_limpar` num slot que está
dispensando toma 409 `limpeza_em_operacao`, a tara zera a balança no meio de uma
pesagem, e a OS aborta por divergência de peso que ninguém provocou.

Com a trava ativa, a mensagem de recusa é OUTRA — porque a saída é outra: há um
botão a clicar antes, e ele está ao lado do de resetar.

**Limpar é comandar, não é zerar dicionário.** O reset manda `cmd_limpar` nos
`NUM_SLOTS` (em `gather`, pelo mesmo motivo das etapas 3 e 3b) e espera
confirmação. Zerar só `_estado["dispensers"]` deixaria o medicamento dentro do
dispenser, e a etapa 1b da OS seguinte tomaria 409 num slot que ninguém liberou
— o bug que a seção "Ciclo de vida de um slot" já registra, agora produzido por
dentro. Slot que NÃO confirma entra em `slots_com_falha`, separado do que deu
certo: é o único desfecho que manda alguém olhar a bancada.

**A fila é drenada com `get_nowait`, nunca trocando a `Queue`.** O
`loop_orquestrador` está pendurado em `await _os_queue.get()`; substituir o
objeto o deixaria esperando na fila ANTIGA — consumidor parado, planta muda, e
o `maxsize` perdido junto.

**E drenar a fila não basta: as OS drenadas precisam fechar no BANCO.**
`salvar_ordem` roda antes do enfileiramento (ver "OS não entra na fila sem linha
no banco"), então cada OS que esperava tem linha em `ordens` com status
"aguardando". Deixá-las assim faz `get_ordem_ativa` cair no fallback de
"aguardando mais antiga" e o `GET /os/ativa` anunciar, para sempre, uma OS que
ninguém vai executar. Daí `cancelar_ordens_pendentes`.

**A tara vem DEPOIS da limpeza dos slots.** Zerar a balança e só então mexer na
bancada faria o offset da tara contar o peso do que ainda estava lá.

**O histórico é opção à parte, e o default é PRESERVAR.** "Repetir a demo" e
"apagar o que já rodou" são decisões diferentes: quase sempre se quer a bancada
limpa com o histórico de pé, que é justamente o que dá forma ao dashboard. E
`limpar_historico` não toca em `medicamentos`, `usuarios`, `log_manutencao` nem
`dispenser_estado` — catálogo e contas não são histórico, o log de manutenção é
a trilha de quem mexeu no equipamento (apagá-lo junto apagaria o registro do
próprio reset), e o estado dos dispensers é zerado pelo orquestrador, que também
precisa comandar a limpeza física.

`DELETE` e não `TRUNCATE`: o TRUNCATE é DDL, faz commit implícito e não devolve
contagem — e é a contagem que dá, a quem acabou de apagar a planta inteira, a
noção do tamanho do estrago.

## O seed de histórico é dado FABRICADO, e ele se identifica

Um banco recém-criado abre com todas as telas vazias, e a leitura natural disso
é "o sistema nunca rodou" — a impressão errada a dar numa apresentação.
`seed_demo.py` fabrica o histórico que falta.

Três regras, e as três são de segurança:

1. **Nunca roda sozinho.** Nenhuma chamada no `init_db`, no `lifespan` ou em
   healthcheck. Só pela rota do console, atrás de confirmação.
   `tests/test_reset_seed.py` varre `main.py` por AST e exige que o ÚNICO
   chamador seja `console_seed` — um seed que rodasse no boot transformaria o
   primeiro `docker compose up` de uma instalação de verdade em dado inventado
   no banco de produção.
2. **O dado se identifica.** Todo `os_id` daqui começa com `DEMO-`, então
   distingue-se de uma OS real em qualquer tela, log ou consulta SQL, sem
   depender de olhar a data — e não pode colidir com uma OS real, porque nenhum
   `template_id` começa com esse prefixo. O id sai de
   `os_templates.novo_os_id` com o prefixo na frente, e não de uma terceira
   grafia do formato.
3. **A geração é PURA.** `seed_demo.py` não abre conexão, não importa
   `database` e não conhece SQL: devolve listas de dicionários. Quem grava é
   `database.semear_historico_demo`. A separação não é estética — é o que
   mantém o SQL dentro do arquivo que o `tests/test_schema.py` varre por AST, e
   é o que permite testar a coerência do histórico sem MySQL nenhum.

### Coerência é o que faz o seed valer a pena

Histórico incoerente é pior que tela vazia: ele ensina a ler o painel errado.
Por isso as OS saem das dez ordens padrão com SKU resolvido do catálogo real, as
dispensas carregam as quantidades dos itens, e — o caso que mais tem como sair
torto — **uma OS em `erro` tem exatamente um item curto e um alarme
correspondente**. Status "erro" com todas as dispensas fechando certo seria
exatamente o painel que o seed existe para evitar.

Os alarmes semeados nascem **resolvidos**: alarme antigo em aberto faria o badge
"⚠ N alarme(s)" nascer com dezenas de pendências falsas, e a tela de
necessidades existe para listar o que ainda precisa de alguém.

A duração de cada OS CRESCE com o número de slots. Sem isso, a ordem de 8 slots
e a de 2 apareceriam com o mesmo tempo de ciclo no relatório.

### A série de sensor precisa de forma, e forma tem duas medidas

Ciclo diário (aquece no turno, esfria à noite) + deriva lenta + ruído pequeno.
Ruído puro não desenha nada; linha reta denuncia o dado fabricado. O teste
reprova os dois extremos com duas medidas: a amplitude tem que ser grande o
bastante para aparecer, e o salto entre pontos VIZINHOS tem que ser bem menor
que ela.

A defasagem entre componentes é de cerca de ±1 hora, e o aperto é a parte que
não é óbvia: uma fase grande faria os componentes não aquecerem todos no mesmo
minuto — o que é o desejado — mas jogaria o pico para a madrugada, e a série
teria forma sem ter SENTIDO. O teste cobra que a tarde seja mais quente que a
madrugada.

## A falha injetada tem UM dono, e ela viaja no comando

O Triple Check e a trava são o diferencial técnico do sistema, e sem a injeção
eles só apareciam quando o sorteio colaborava. `injecao.py` guarda UM gatilho
armado — tipo + slot —, o console arma e desarma, e o orquestrador o consome no
momento em que monta o comando aplicável.

**O gatilho mora no central e viaja no comando que já existia.** `injetar_falha`
é um campo a mais no corpo de `/comandos/dispensar`, `/comandos/capturar/*` e
`/comandos/pesar`; os adapters o repassam sem interpretar, como já fazem com as
duas quantidades da pesagem.

A alternativa avaliada — armar o SIMULADOR, por uma rota nova em três adapters e
três simuladores — é a que este repositório já recusou duas vezes, no mapa de
posições da CNC e na lista de ordens padrão: dois lugares guardando o mesmo
fato. E aqui a divergência apareceria na pior hora possível. O console diria
"armado" depois de um restart do simulador que esqueceu o gatilho, ou diria
"desarmado" com uma falha ainda a caminho — e quem estivesse explicando a planta
não teria como saber qual dos dois estava certo. Com uma fonte só, "armado" é
sempre o que o central vai injetar no próximo comando aplicável.

**O one-shot é `consumir()`, e ele é check-and-pop sob lock.** Não é zelo
abstrato: a etapa 3b dispara os comandos dos oito slots em `gather`. Ler,
decidir e apagar em passos separados deixaria dois slots verem o mesmo gatilho,
e a falha sairia em dois lugares — a surpresa que a feature existe para
eliminar.

**Armar substitui o gatilho anterior em vez de recusar.** Quem errou o slot quer
corrigir, não ler "já existe uma falha armada" e ter que desarmar antes. E há um
gatilho de cada vez de propósito: dois armados tornariam a próxima trava ambígua
justamente na hora de explicar o que a causou.

### Do lado do simulador, é um ramo — e ele vem ANTES do sorteio

Cada simulador decide a injeção num bloco único, no topo da operação, antes do
primeiro `random.random()`. Três consequências, e nenhuma é acidental:

- **Funciona com `MODO_APRESENTACAO` ligado**, porque o ramo não passa pelas
  probabilidades que o modo zera. É a combinação da banca: sem surpresa, com a
  falha escolhida, na hora escolhida. Os dois controles são independentes.
- **Nada da lógica normal muda.** Sem o campo, o corpo do comando sai byte a
  byte como saía antes da feature, e há teste cobrando isso.
- **Valor desconhecido é ignorado COM aviso**, nunca silenciosamente. Um typo do
  console não pode virar dispensa diferente da que se pediu.

Os valores injetados são fixos, e os sorteados continuam sorteados: o SKU
injetado é `APSEN-INJETADO-000` e a contagem da mesa erra sempre para MAIS, por
uma unidade. A primeira coisa que se faz com uma falha provocada é conferir se o
que apareceu na tela é o que se armou; um número diferente a cada ensaio
atrapalharia exatamente isso. Já foi para MENOS — o quadro mais fácil de explicar,
produto que não saiu —, e mudou quando a câmera a menos, sozinha, deixou de
travar (ver a exceção em "Triple Check: 1 divergência trava"): uma injeção a
menos passaria a mostrar só um alarme, e o botão existe para mostrar a trava.

**Com câmera real, quem injeta é o ADAPTER — e isso muda uma frase deste
arquivo de propósito.** "O adapter não interpreta `injetar_falha`" valia porque
havia um simulador do outro lado para executar. Com as estações reais não há: a
dos dispensers não recebe comando (o botão ficava "armado" → "consumido" e
nada acontecia), e a da mesa, com `ACEITAR_INJECAO=1`, devolvia `esperado − 1`
SEM medir e SEM registrar o total — a menos (alarme, não trava) e, pior, o
slot SEGUINTE via os dois slots juntos e travava no lugar errado. Hoje, só para
a câmera cuja fonte é `estacao`, o vision-adapter reescreve o evento
VERDADEIRO: nos dispensers a leitura roda normalmente (frescor, janela) e sai
como divergência com `APSEN-INJETADO-000` ou como falha `injetada`; na mesa o
comando segue para a estação SEM o campo (ela mede e registra o total de
verdade), e o evento que volta sai com `esperado + 1`, `falha_injetada` e a
contagem medida em `quantidade_detectada_real`. Se a estação falhou a leitura,
a falha segue intacta — não se injeta sobre o que não foi medido. Os valores são
os do simulador, para a tela mostrar o que se armou. `ACEITAR_INJECAO=0`
SEMPRE na estação da mesa (o `conferir_mesa.py` reprova o contrário), e o
console recusa armar `divergencia_mesa` num slot fora de `VISAO_MESA_POSICOES`:
ele nunca seria fotografado, e o gatilho ficaria armado para sempre.

**A injeção de peso desloca a LEITURA, não a massa.** `_do_pesar` aplica o
desvio ao delta do slot DEPOIS de salvar `_peso_anterior_g`. Tirar peso da mesa
de verdade seria simular medicamento evaporando, e o slot seguinte nasceria com
o delta inflado: o one-shot deixaria de ser one-shot sem ninguém notar, e a demo
mostraria duas travas onde se armou uma.

**A falha mecânica solta UMA unidade a menos**, e o número não é de gosto: as
ordens padrão vão até 15 unidades por slot, e 1/15 = 6,7% já passa da tolerância
de 5% da balança. Ou seja, a menor falha possível já é detectável pelo Triple
Check em qualquer template — perder mais só tornaria a demonstração menos
interessante.

### Quatro cópias das strings, e um teste que as confronta

O nome de cada tipo aparece no catálogo do central, em constantes nos três
simuladores e no `AdapterFake` da suíte. A duplicação é do mesmo tipo tolerado
em `os_templates.novo_os_id` × `simulator._novo_os_id` — serviços separados que
não importam módulo um do outro —, mas o modo de falhar aqui é pior: string
divergente não quebra nada visivelmente. O simulador ignora, o central acha que
injetou, e o gatilho armado simplesmente não dispara.

Por isso `tests/test_injecao.py` confronta as quatro contra `TIPOS`, exige que
todo tipo aponte para uma rota que o orquestrador de fato chama, e cobra que
cada simulador reconheça exatamente os tipos que o catálogo atribui a ele — nem
um a mais, nem um a menos.

### A trava bloqueante é o que trava também a SUÍTE

Quatro dos cinco tipos são bloqueantes, porque é isso que o Triple Check faz.
Num teste, isso significa `_processar_os` parado em `evento_lib.wait()` para
sempre: a suíte não falha, ela pendura. `_liberar_trava_automaticamente` faz o
papel do supervisor de dentro do broadcast — o instante exato em que a trava
aparece na tela — e devolve a contagem de liberações, que é o que permite
afirmar que o re-scan depois da liberação volta LIMPO. Sem o one-shot, ele
voltaria divergente de novo e a trava viraria laço infinito na frente da banca.

## O modo apresentação desliga o ACASO, e o fator só acelera o RELÓGIO

Duas variáveis, declaradas uma vez no compose e lidas pelo central e pelos
quatro simuladores: `MODO_APRESENTACAO` e `FATOR_VELOCIDADE`. Antes, zerar as
falhas para uma demonstração significava editar seis probabilidades espalhadas
por três serviços e recriar containers — e religá-las depois, para mostrar o
tratamento de erro, significava fazer tudo de novo pelo caminho inverso.

**`MODO_APRESENTACAO` zera probabilidade de falha, não aleatoriedade em geral.**
A linha é essa, e ela decide caso a caso:

| sorteio | zerado? | por quê |
|---------|---------|---------|
| `PROB_ERRO_MECANICO` (dispenser) | sim | produz falha |
| `PROB_FALHA_LEITURA_*` / `PROB_DIVERGENCIA_*` (3 câmeras) | sim | produz falha |
| `PROB_ERRO_SENSOR` (HX711) | sim | produz falha |
| `RUIDO_G` (σ da célula de carga) | **sim** | ver abaixo |
| confiança da leitura, jitter de temperatura, horas de uso | não | é textura, não falha |

`RUIDO_G` é o caso que parece fora do lugar e não é. Ele não é uma
probabilidade — é o desvio-padrão do ruído gaussiano — mas é fonte de falha de
verdade: com σ=2 g contra um esperado de 100 g (quantidade mínima de template ×
peso unitário padrão), a tolerância de 5% fica a 2,5σ, e uma OS de vários slots
tem alguns por cento de chance de uma `peso_divergencia` sem causa nenhuma. Uma
trava do Triple Check no meio da explicação é exatamente o que o modo existe
para evitar. Zerá-lo é o que faz "modo apresentação" significar determinístico,
e não "com menos sorteios".

O que o modo **não** desliga é a detecção: com o ruído zerado, a balança segue
divergindo quando a quantidade que caiu na mesa não bate com a esperada. O modo
tira o acaso, não a capacidade de mostrar o Triple Check funcionando — e é essa
separação que deixa a injeção de falha sob demanda conviver com ele.

**Nas câmeras, o modo vence o override por lado.** `PROB_..._DIR` é o escape
para simular uma câmera suja em campo, e `_prob()` é aplicado DEPOIS de
`_cfg_float()` justamente para que uma variável esquecida de um teste anterior
não reintroduza a falha que o modo foi ligado para não ter — cujo sintoma
(divergência de SKU num slot só) é indistinguível de medicamento trocado.

### O fator entra invertido na CNC, e só para cima nos timeouts

`FATOR_VELOCIDADE` multiplica TEMPO: `T_CARGA_UNID`, `T_DISPENSA_UNID`,
`T_SCAN_DISPENSER` (inclusive os overrides `_ESQ`/`_DIR`), `T_SCAN_MESA`,
`T_LEITURA` e `T_TARA`. Fator 2.0 é "metade da velocidade".

Duas exceções de sinal, e as duas têm motivo:

- **`VEL_MM_S` é DIVIDIDO.** É a única inversão do sistema: velocidade é o
  inverso de tempo, e multiplicar aqui faria a CNC acelerar justamente quando
  se pediu calma para narrar o movimento. O piso de duração
  (`DURACAO_MIN_S`, o antigo `max(1.0, ...)`) multiplica, senão um trajeto
  curto ficaria preso em 1 s com o resto da célula desacelerado.
- **`INTERVALO` não escala.** É a cadência de PUBLICAÇÃO da posição, não um
  tempo físico. Desacelerar mantendo a cadência dá mais passos — trajetória
  mais suave na tela, que é o desejado.

**Os `TIMEOUT_*` do orquestrador escalam por `max(1.0, fator)`, e o `max` é a
decisão.** Sem escalar nada, desacelerar a demo aborta a OS por
`timeout_carregamento` e o log culpa o dispenser, que fez exatamente o que se
pediu — é o erro mais provável desta feature. Mas escalar para BAIXO seria
trocar um bug por outro, por uma assimetria real:

- Acelerar não ganha nada com timeout menor. O timeout é teto de espera por um
  adapter travado, não parte do ciclo: encolhê-lo não economiza um segundo de
  demonstração.
- O que ele encolheria junto é a folga para o custo FIXO, que não escala com
  fator nenhum — `_post` retentando 3× com `sleep(1)` chega a ~32 s por
  comando, e o MySQL e a rede levam o que levam. Com `TIMEOUT_PESO` (15 s) a
  0.1, o teto viraria 1,5 s e a OS abortaria com a planta inteira saudável.

`INTERVALO_OS` do gerador **não** escala: é a cadência com que se QUER ordens,
uma escolha de apresentação, não um tempo simulado. Desacelerando muito, a fila
enche e o `MAX_FILA_OS` responde 429 — que é o comportamento correto e já
testado, não um efeito colateral novo.

### Cinco cópias do mesmo bloco, e um teste que as compara

`_modo_apresentacao()` e `_fator_velocidade()` existem idênticas nos quatro
simuladores e em `central-computer/config.py`. Não há `shared/`: os simuladores
são imagens separadas que não importam módulo nenhum do central, e a seção "Os
templates moram no CENTRAL" já registra por que um diretório copiado nas
imagens custa mais do que resolve.

Aqui, porém, o critério de "duplicação tolerável" da seção do mapa da CNC
(divergir produz dado *visivelmente* diferente) **não** salva sozinho: um
serviço que leia `MODO_APRESENTACAO` diferente dos outros produz exatamente a
surpresa que o modo existe para eliminar, e ninguém vai desconfiar do parser na
hora. Por isso `tests/test_modo_apresentacao.py` compara as **cinco** cópias
entre si, tabela de entradas por tabela de entradas — a mesma forma que
`tests/test_adapters.py` usa para o `_vale_retentar` dos quatro adapters.

Os tempos, no mesmo arquivo, são medidos pelos `time.sleep` de verdade e não
lidos das constantes: constante certa com `sleep` de um literal esquecido
passaria despercebido. E cada simulador tem um teste de CONTROLE — a mesma
configuração, com o modo desligado, falhando — para que "não emitiu falha" não
possa ser confundido com "o caminho da falha sumiu".

## Fontes de verdade

Dividido em duas, e a linha importa (a tabela está no README, seção "Fontes de
verdade"):

- **simulador** manda no hardware/estoque — `medicamento`, `sku`, `categoria`,
  `quantidade`;
- **orquestrador** manda no fluxo — `status` da etapa e `os_id` em execução.

O evento `status` é telemetria periódica (15s, todos os slots) e não pode
encostar no fluxo: quando encostava, desfazia o reset de fim de OS a cada ciclo.

### O status da OS vive nos DOIS lados, e os dois precisam ser escritos

`_estado["os_ativa"]` (memória, para o WebSocket) e `ordens.status` (banco, para
quem consulta por HTTP) contam a mesma história para públicos diferentes.
"em_andamento" era escrito só na memória: no banco toda OS ficava "aguardando"
até virar "concluida"/"erro". Como `get_ordem_ativa` filtrava
`status IN ('aguardando','em_andamento') ORDER BY criado_em DESC`, com fila ela
devolvia a OS enfileirada por ÚLTIMO — o `GET /os/ativa` mostrava a OS errada.
Hoje `_processar_os` grava "em_andamento" na entrada, e `get_ordem_ativa` procura
por esse status; o fallback para "aguardando" (a mais ANTIGA, que é a próxima da
fila) existe só para o app de manutenção não piscar entre duas OS.

A contrapartida: **toda saída de `_processar_os` deve fechar em status
terminal** — `concluida`, `erro` ou `cancelada`. Sem isso, gravar "em_andamento"
troca o bug antigo por um pior: OS eternamente em execução aos olhos do banco. A
saída fácil de esquecer é a rejeição por falta de slot, que não passa pelo ciclo
CNC; ela chama `_abortar_os` (sem atribuições — nenhum slot foi reservado, não há
resíduo a descartar) justamente para herdar o fechamento. Os quatro caminhos
estão cobertos em `tests/test_orchestrator.py`.

### `null` num periódico é "não informado", não "não tem"

O corolário que faltava, e ele custou a linha de dispensa: o periódico manda no
ESTOQUE, mas quem APAGA um campo é a transição. As duas coisas pareciam a
mesma enquanto o ramo `status` do `_handle_evento_dispenser` fazia
`d.update({"medicamento": payload.get("medicamento"), ...})` — o `get` devolve
`None` tanto para "o slot esvaziou" quanto para "esta mensagem não trouxe o
campo", e o firmware manda os dois com o mesmo JSON.

A sequência é a NORMAL, não uma borda. `cmdDispensar` conta a última unidade,
esvazia `sl.medicamento`, emite `status` (com medicamento nulo) e só então
emite `dispensado`. O central lia o nome do próprio snapshot — que o periódico
acabara de apagar — e chamava `salvar_dispensa(os_id, slot, None, …)`;
`dispensas.medicamento` é `VARCHAR(100) NOT NULL`, o INSERT morria em 1048, e a
dispensa que deu CERTO era exatamente a única sem linha no banco. Some do
relatório da OS, do CSV e do XLSX.

Hoje o ramo `status` só escreve campo que veio não-nulo, e quem zera é
`dispensado` com resíduo 0 ou `limpeza_ok` — que sabem que o slot esvaziou de
verdade. A LINHA de `dispenser_estado` continua seguindo a quantidade: slot
vazio não guarda medicamento lá.

A outra metade é do firmware, e é de ORDEM: `copiarCampo(med_evento, …)` estava
DEPOIS do `if (residual == 0)` que limpa o slot, então `emitDispensado` saía com
`"medicamento":""` no caminho de sucesso. Hoje a cópia vem antes, e
`tests/test_dispenser_firmware.py` compara as três posições
(cópia < limpeza < emissão) em vez de só conferir que a cópia existe — presença
estava certa o tempo todo; era a ordem que mentia.

E `d.get(chave, default)` **não** aplica o default quando a chave existe valendo
`None`. O fallback do nome é `or`, encadeado até `"(desconhecido)"`: entre
gravar um nome de placeholder e não gravar linha nenhuma, um sistema de
medicação grava — o que aconteceu com o paciente não pode depender de o nome ter
chegado.

### Telemetria da CNC também não encosta no snapshot

O mesmo erro, no handler ao lado, e ele sobreviveu porque a guarda do dispenser
foi escrita e a da CNC não. `_estado["cnc"].update({...})` rodava para TODO
tipo de evento, e a telemetria não traz posição, alvo nem ciclo: os defaults do
`payload.get` (`None` e `0.0`) entravam no snapshot como se fossem medida. Entre
dois eventos de movimento a mesa "voltava" para `(0,0)` com `dispenser_alvo`
nulo.

Com o ciclo por relógio isso pesa mais do que pesava, e por um motivo concreto:
`prevoo.itens_celula` compara a posição PUBLICADA com o HOME — a tela de
conferência passava a concordar com uma origem que ninguém mediu. A linha de log
tinha a mesma mentira pelo outro lado (`CNC telemetria | DNone | (0.0,0.0)`), e
`/log/eventos` é o que a bancada lê; por isso as duas foram separadas juntas.

## "Não respondeu" nunca vale como "está certo"

A regra já existia no Triple Check — `fontes_indisponiveis` não é
`divergencias` — e a rodada seguinte achou quatro lugares onde ela era violada.

**O re-scan da trava de SKU** tratava `res is None` como "assumindo corrigido".
O slot chegou ali porque a câmera LEU e acusou SKU errado; soltá-lo por falta de
resposta transformava o vision-adapter fora do ar num caminho para dispensar o
medicamento errado — bastava liberar a trava e esperar o timeout. Hoje o
silêncio MANTÉM a trava, e o motivo muda para `visao_indisponivel`: a saída é
outra (olhar a estação de visão, não trocar o medicamento do slot), e um motivo
repetido mandaria o supervisor mexer no lugar errado.

A diferença para o Triple Check é o estado de partida. Lá o silêncio deixa a OS
seguir porque nada a contradisse; aqui já há contradição registrada, e o
silêncio não a apaga.

**`cmd_homing` do fim da OS** tinha o retorno ignorado. Homing que não sai deixa
a mesa parada no último dispenser e a OS fecha como `concluida`, sem nada no
log; quem paga é a OS SEGUINTE, que planeja a serpentina a partir de HOME — e a
otimalidade dessa rota é a do ciclo FECHADO. Hoje o retorno vira alarme
`homing_nao_confirmado`, e **não** aborta: a dispensa já terminou e o Triple
Check já opinou, então abortar marcaria em erro uma OS que entregou tudo certo.

**O `homing_completo()` da mesa**, nos dois caminhos de trava, emitia
`concluido` sem conferir nada — violando a regra que o próprio `cmdHoming`
sessenta linhas abaixo já aplicava e que o §4 do protocolo já escrevia. Ali é
pior que no `cmdHoming`: `ja_fez_homing` fica `false`, todo `mover` seguinte é
recusado com `sem_homing`, e a OS que aborta é a de DEPOIS da liberação, com um
erro apontando para o lugar errado.

**E a placa falsa da mesa não modelava nada disso.** `estado_celula` devolvia
`[]` com o comentário "pintar não produz resultado", que é verdade para as telas
TFT e falso para a mesa — ela faz homing e emite evento. O silêncio da placa
falsa era o que deixava os dois defeitos acima verdes. Hoje ela emite o que o
firmware emite, e o §4 documenta isso.

## A trava do Triple Check não pode marcar a OS como concluída

O caso é o NORMAL da feature, não uma borda: a trava dispara entre dois ciclos,
com a mesa parada. O firmware então faz homing por conta própria — é onde o
supervisor espera encontrá-la — e emite `concluido` endereçado ao `trava_os_id`,
que é a OS que acabou de travar. O central lia isso e publicava a OS como
CONCLUÍDA em `/estado`, no `/ws` e no dashboard: a tela que a pessoa que vai
liberar a trava está olhando.

A guarda mora no CENTRAL, e não no firmware, por duas razões. Quem manda no
FLUXO é o central (README, "Fontes de verdade") — a mesa reporta o que ela fez,
não o que a OS virou. E a placa em campo precisaria ser regravada; a linha no
central vale na bancada que já está montada.

## O cronograma do ciclo é o único relógio, e ele tinha os únicos números crus

Os quatro números de `cronograma_do_ciclo` eram lidos com `float(os.getenv(...))`
direto — os ÚNICOS de `config.py` sem faixa, warning e queda no default. E o
comentário ao lado deles diz, com todas as letras, que um cronograma errado é a
única forma de esta feature derrubar comprimido no chão.

Dois modos de falhar, os dois silenciosos do jeito errado:

- **`2,5` com vírgula** (que é como se digita em pt-BR) ou a variável vazia
  levantavam `ValueError` na avaliação do `dataclass`, ou seja, no import: o
  central não subia e o traceback apontava para `config.py`, não para o `.env`;
- **zero ou negativo** faziam `_dormir_ou_cancelar` retornar NA HORA dizendo que
  o prazo foi cumprido — `asyncio.wait(timeout=-1)` acorda com `feitos` vazio,
  que é exatamente o sinal de "venceu". O `dispensar` saía com a mesa andando.

O corolário mordeu na suíte: `CRONOGRAMA_INSTANTANEO`, no `conftest`, pedia
`"0"` pelo ambiente. Com a faixa, ele passou a cair no DEFAULT DE PRODUÇÃO —
3,25 s por trajeto — e a suíte dobrou de tempo sem que nada ficasse vermelho
para contar. Hoje o fixture escreve direto no `settings`: a validação existe
para o que vem do `.env` de quem opera, e ali não há `.env`.

## OS órfã: o orquestrador é um loop único, e isso torna a reconciliação segura

`_processar_os` grava "em_andamento" na entrada. Processo que morre no meio do
ciclo deixa a linha assim para sempre, e `get_ordem_ativa` prefere
`em_andamento ORDER BY criado_em ASC`: a órfã MAIS ANTIGA vira "a OS ativa" para
sempre — no `GET /os/ativa`, no app de manutenção e no espelho do painel de
bancada. Na bancada isso já aconteceu: três ordens paradas desde 11/09, e o
endpoint anunciando a primeira delas desde então.

A `lifespan` as fecha em `erro`, com um alarme por linha. É seguro **porque o
orquestrador é um loop único**: no boot não existe OS em execução por definição,
então toda `em_andamento` é de um processo morto. `erro` e não `cancelada` —
cancelada é decisão de alguém (é a palavra do reset da planta), e ninguém
cancelou estas.

## `sessao`: o `cmd_id` nasce de novo, e a placa precisa saber

O `cmd_id` é monotônico DENTRO de uma sessão do adapter, e a placa ignora id
menor ou igual — certo contra reenvio, errado contra RESTART. Um restart de 2 a
5 s do processo (o `.bat` da bancada reinicia sozinho) fazia o contador voltar a
1 com a placa ainda em `ultimoCmdId = 47`: todo comando até 47 recebia ACK
POSITIVO **sem executar**. Com o ciclo por relógio o sintoma mudou de lugar —
não é mais só timeout: o central segue o cronograma e dispara o `dispensar` de
um `mover` que a mesa nunca fez.

Havia uma heurística do lado da placa (silêncio de mais de 10 s sem pong zera o
contador), e ela continua lá como rede de segurança. Ela não cobre o restart
RÁPIDO, que é justamente o comum.

`sessao` é o epoch de boot do processo do adapter, e viaja no pong **e em todo
comando**. Três detalhes que são contrato, não implementação:

- **a comparação é de DIFERENÇA, não de ordem** — relógio que ande para trás
  (NTP, fuso, máquina sem RTC) continua sendo outro processo;
- **ausente ou zero significa "ainda não sei"**, e a primeira sessão vista é
  adotada sem zerar nada: zerar na primeira faria todo boot da placa descartar o
  primeiro comando legítimo;
- **a adoção vem ANTES da checagem de idempotência**, no caminho do comando. O
  pong também a carrega, mas o primeiro comando depois do restart pode chegar
  antes do primeiro pong — e é justamente ele que não pode ser respondido como
  repetido.

São SEIS cópias do contrato: três `apsen_serial.h`, a cópia inline da balança,
três `serial_link.py` (idênticos entre si) e as placas falsas. A balança ainda
não inclui o header — adotá-lo exige reescrever a camada serial de um firmware
já gravado —, e o que fecha a lacuna enquanto isso é `tests/test_balanca_serial.py`
lendo os NÚMEROS e as FORMAS do header e cobrando-os dela.

## A varredura de portas deixou de ser o default, e os dois lados decidem diferente

Com `<SUB>_SERIAL_URL` vazia, cada adapter varre TODAS as portas, e cada
sondagem abre a porta por até ~9,5 s. Na célula montada são cinco placas e cinco
processos: enquanto um segura a COM da CNC para conferir, o cnc-adapter toma
`ACCESS_DENIED` na própria. Não dá erro — dá boot não-determinístico, em que uma
placa às vezes não é achada, e o log de cada processo mostra só a metade dele.

**Os adapters RECUSAM subir**, com a mensagem dizendo qual variável definir. Eles
existem para falar com uma porta e sem ela não fazem nada; serviço que não sobe
trava, por `depends_on`, quem espera por ele — que é o desejado com a bancada
mal configurada, em vez de uma OS morrendo por timeout num slot íntegro.

**O painel de bancada faz o OPOSTO**, e a diferença é a mesma do
`APSEN_API_TOKEN`: ele tem DOIS trabalhos, e o outro é servir a tela que o gestor
está olhando. Derrubá-lo por uma variável de serial trocaria um display offline
por um painel inteiro fora do ar. A varredura dele virou opt-in
(`APSEN_DISPLAY_VARRER=1`) e, sem ela, o display fica OFFLINE com uma linha
dizendo o que definir.

## O painel: FEFO, fuso, PIN de fábrica e o que o import faz

**O FEFO preferia o lote VENCIDO, por construção.** "Vence primeiro, sai
primeiro" — e um lote já vencido é, por definição, o que vence primeiro de
todos. Sem filtro de validade ele não era apenas aceito: era escolhido na frente
do lote bom. E `verificar_estoque_ordem` não ajudava, porque consulta
`medicamentos.quantidade`, que somava o saldo vencido junto. Na bancada foram
dois meses e 286 unidades ainda dispensáveis.

Lote vence pela passagem do TEMPO — é a única transição de dispensabilidade que
ninguém dispara —, então ela precisa de uma VARREDURA, e ela passa pelo dono do
agregado (`_mover_saldo_lote`) como toda outra. `expirar_lotes_vencidos` é
idempotente porque aquela função soma e subtrai, não reconcilia. O alerta de
`lotes_proximos_vencimento` passou a incluir `Vencido`: filtrar só por `Ativo`
faria o lote SUMIR da tela no dia seguinte ao vencimento, exatamente quando ele
mais precisa de alguém.

**O espelho do central misturava duas escalas de tempo.** Todo carimbo do painel
é hora local ingênua, e a escala só funciona porque é UMA — as telas exibem cru,
as consultas ordenam como texto, o KPI de SLA compara com outro `strftime`
local. Trocar tudo para UTC não seria correção: deslocaria toda data exibida e
deixaria as linhas antigas incomparáveis com as novas, na mesma coluna. O que
importa é NÃO MISTURAR, e `_normalizar_data` misturava: o central carimba em
UTC. Numa bancada no Brasil, toda OS espelhada nascia três horas no passado —
ordenava antes de ordens locais mais velhas e ganhava três horas de SLA.

**O PIN de fábrica não sobrevive mais ao primeiro acesso.** O hash sempre esteve
certo; o problema era o segredo, e ele está neste repositório. A coluna
`pin_provisorio` nasce 1 no seed, e um `before_request` só serve a tela de troca
enquanto ela estiver de pé — o redirect no login sozinho não bastaria, porque
quem já tem sessão continuaria navegando. A migração de um banco que já está na
bancada compara o hash gravado contra TODOS os PINs do seed, e não contra o do
operador de mesmo nome: lá havia um "Supervisor" (o perfil que libera a trava)
com o PIN do Administrador, e ele não está em `OPERADORES_DEFAULT`. O display é
AVISADO e não bloqueado: a troca acontece na web, onde há teclado.

**E importar `app.py` deixou de tocar em disco.** `init_db()` e
`seed_demo_data()` rodavam no topo do módulo, então qualquer import criava um
banco e o populava com dado fabricado. É a regra que o central já tem escrita
para o `seed_demo.py` dele. `preparar_banco()` é chamada por `iniciar_workers`,
por onde passam os três entrypoints — o comportamento de quem opera não mudou.

## Escrita perdida em silêncio: nem toda `db_task` pesa o mesmo

`_db` engolia QUALQUER falha de banco num `logger.warning`. Foi isso que deixou
o `medicamento` nulo de pé por tanto tempo: a linha não existia, e o único
rastro era um warning no meio de centenas de linhas de evento.

A linha entre "warning" e "alarme" **não é de gravidade abstrata — é de quem
conserta**:

- telemetria e `dispenser_estado` são MEDIÇÕES REPETIDAS: o valor seguinte chega
  em 15s e reescreve a linha. Perder uma é perder um quadro, e abrir alarme por
  cada uma encheria a tela de necessidades com um item a cada 15s enquanto o
  MySQL reinicia;
- `salvar_dispensa` e `atualizar_item_os` são o rastro NOMINAL de quem recebeu o
  quê: acontecem UMA vez, ninguém os reemite, e a linha perdida some do relatório
  da OS para sempre.

As duas últimas viram `logger.error` + alarme `persistencia_falhou` na fonte
`central` — fonte única de propósito, porque a tela de necessidades agrupa por
ela. O payload INTEIRO vai na descrição, e não `args[:2]`: quem for reconstruir
a linha à mão precisa dos números.

**A decisão é por IDENTIDADE (`fn is salvar_dispensa`), como `_abriu_alarme` já
fazia, e não por `fn.__name__`.** O nome é do objeto que chegou: a suíte troca
toda função de banco por um duplo, e um duplo se chama outra coisa. Com nome, a
regra deixaria de valer justamente onde ela é exercitada — e sem vermelho nenhum.

O alarme é outra escrita no MESMO banco, então com o MySQL fora ele falha junto.
Tudo bem: o `logger.error` já saiu. O que não pode acontecer é essa segunda falha
propagar — o handler é o único caminho de volta do adapter ao orquestrador, e um
500 ali faz o adapter retentar um evento já processado. Daí o `try/except`
próprio, e daí ele NÃO chamar `_db` de novo, para não recursar sobre a mesma
indisponibilidade. O alarme nasce FORA das `db_tasks`, então ele mesmo força
`_atualizar_alarmes_ativos`: `_abriu_alarme` não o veria, e o badge ficaria até
`_ALARMES_TTL_S` sem ele.


## O supervisor libera a trava pela bancada — e o rastro de QUEM liberou vai junto

O `TASKS.md` guardava "Trava do Triple Check no display" em "Decisões adiadas
de propósito" esperando uma decisão de operação. Ela foi tomada: o supervisor
libera a trava pelo painel de bancada, autenticado por PIN, na web e no display
de 7". Três metades, e a costura entre elas é o ponto.

**No central, o portão é à PARTE.** `POST /api/v1/admin/liberar-trava` dizia no
docstring "admin ou supervisor" e exigia `_get_admin` — a documentação mentia,
porque a role `supervisor` não existia. Hoje `_get_supervisor_ou_admin` aceita
as duas, e `_get_admin` continua só admin: afrouxá-lo daria ao supervisor a
gestão de usuários. `ROLES_VALIDAS` fechou o vocabulário da coluna (`admin`,
`supervisor`, `manutencao`); role desconhecida é 400, não gravada — um typo
criava usuário sem acesso a nada e sem erro em lugar nenhum. A validação na
borda compara com a TUPLA, não com `database.role_valida`: a suíte troca toda
função de `database` por um duplo que devolve None, e uma validação por função
viraria 400 para toda role.

**`em_nome_de` existe porque a bancada fala com o central por uma conta de
serviço.** Sem ele toda liberação vinda do painel apareceria no log como
"painel-bancada", e o rastro de QUEM liberou — o ponto inteiro de existir uma
trava — se perderia. O `liberado_por` gravado vira `"<conta> (em nome de <X>)"`,
sanitizado (caractere não imprimível vira espaço; quebra de linha viraria uma
segunda linha de log que ninguém escreveu) e truncado em 60 — truncado, não
recusado: nome longo não pode ser o que impede um supervisor de liberar a
produção. A liberação passou a entrar em `log_manutencao` (`trava_liberada`),
que é a trilha de quem mexeu no equipamento: até aqui ela só existia numa linha
de log de processo, e é a liberação, não a ativação, que diz quem assumiu a
responsabilidade pela OS que seguiu.

**No painel, a escrita tem UM arquivo, e ele é nomeado.** `central_client.py`
continua GET-only, e o teste que reprova um `requests.post` lá continua. A
liberação da trava é a única exceção ao espelho de mão única e vive sozinha em
`central_comandos.py`, onde dá para ver todas as escritas de uma vez. Ele
autentica com `PAINEL_CENTRAL_USER`/`PAINEL_CENTRAL_SENHA`, guarda o JWT e refaz
login em 401; sem as variáveis, a liberação fica desligada com a mensagem
dizendo qual definir — a mesma divisão do `APSEN_API_TOKEN`, porque derrubar o
processo levaria junto a ponte serial. A LEITURA da trava (`GET /api/v1/trava`)
ficou em `central_client.py`, com os outros GETs; só a escrita saiu. E ela roda
no MESMO ciclo do espelho, guardando o estado anterior: é a comparação entre os
dois que decide o push ao display.

**No display, nome + PIN, conferidos pelo backend.** `liberar_trava` carrega
`nome` e `pin` em vez de usar o operador logado: o operador logado é um
Operador, e o supervisor é outra pessoa que chega, libera e vai embora — o
desenho do `validar_operador`. Quem confere é o backend, pelo mesmo motivo de
lá (10 mil PINs de 4 dígitos não se protegem com hash no display), e a resposta
nunca carrega o PIN. O pedido custa hash (~300 ms) + POST no central (3 s), e é
por isso que o firmware espera 5 s nele. `get_trava` existe ALÉM do push
`trava`, porque push é uma linha serial e linha serial se perde num reset —
`get_trava` cai num cache de 2 s pelo motivo do cache de dispensers (o pedido
roda dentro da ponte serial). O firmware guarda o motivo em `MAX_MOTIVO_LEN`
(256), dimensionado pela origem (o formato do orquestrador passa de 240 no pior
caso), com `copy_trunc` terminando em `...`. O display em campo precisa ser
regravado de novo.

## Duas placas do dispenser, um adapter — e o que as telas NÃO recebem

Acionar os 8 mecanismos, desenhar 8 telas TFT e manter a serial não cabe num
ESP só: são DUAS placas em DUAS portas (`dispenser` e `dispenser_tft`), e o dono
das duas é o mesmo `dispenser-adapter`, com dois `LinkSerial`. Não entra
serviço novo porque este adapter já é um tradutor — vê todo comando que desce e
todo evento que sobe do slot, ou seja, já tem em mãos tudo que as telas
precisam mostrar. Um tft-adapter separado obrigaria o central a mandar a mesma
informação duas vezes, e duas cópias divergem. `serial_link.py` não mudou (as
três cópias continuam idênticas); a segunda porta é só outra instância.

**`http` nas telas significa "sem telas"**, e é o default para que a suíte, o
CI e a demonstração não mudem de resultado: `slot` não vai a lugar nenhum e
`estado_celula` desce ao simulador dos mecanismos, que só loga — a perna de
cima não pode saber qual transporte está embaixo, e sem a rota o http tomaria
404 e viraria recusa determinística enquanto o serial funciona.

**`slot` sai na transição, e só nela**: ao aceitar `carregar`/`dispensar`/
`limpar` e ao receber `carregado`/`dispensado`/`limpeza_ok`/`erro`, antes de
encaminhá-los ao central. Nada periódico no canal das telas. E **falha das
telas nunca muda o caminho do dispenser**: o envio é `create_task`, não
`await` — não recusa comando, não atrasa ACK, não segura o evento. Tela errada
é cosmética; dispensa atrasada não é. Os eventos DA placa das telas
(`telemetria`, `erro`) ficam no adapter, em log e no `/health` (que passou a
publicar as duas portas, `serial` e `serial_tft`): o central não tem endpoint
de tela e despejá-los em `/api/v1/eventos/dispenser` misturaria duas placas num
histórico que hoje é de uma.

**`trava_resumo` tem teto de 48 e sai da CATEGORIA, nunca do texto.** O
`ResultadoTripleCheck` ganhou `categorias` ("dispenser divergente", "contagem
divergente", "divergência de peso"), paralelo a `divergencias`, e
`resumo_da_trava` pega a primeira. O motivo formatado — que passa de 240
caracteres — não viaja: mandá-lo acoplaria o formato de mensagem do central à
largura de uma tela e criaria um segundo ponto de truncamento para algo
cosmético. A tela do slot responde uma pergunta só: é este slot?

**O aviso do central às telas nunca passa por `_post`.** `_post` retenta 3× com
sleep(1) e timeout de 10 s: uma placa fora do ar somaria ~32 s ao caminho da
trava, justamente o momento em que a tela tem que mudar na hora. `_avisar_telas`
faz UM POST com timeout de 3 s e é agendado, não esperado — a ativação da trava
continua na casa de milissegundos com o adapter fora, e há teste medindo. O
agendamento vem ANTES do broadcast, e a ordem importa: se a liberação chegar no
instante do broadcast, o aviso de "liberada" tem que sair depois do de "ativa".
Sai ao ativar, ao liberar e no reset; falha não aborta, não propaga, não entra
no veredito.

### A tela mostra a CAIXA, e quem traduz o nome é a placa

O painel chegou: oito ST7735 de 128×160, e cada slot carregado mostra a imagem
da caixa do medicamento (`dispenser/telas_tft/imagens.h`, 39 imagens). O
pinout e a inicialização vieram sem mudança do sketch de teste da bancada, que
ficou em `telas_tft/referencia/` — fora da raiz do sketch porque dois `.ino`
na mesma pasta são concatenados pela IDE, e dois `setup()` não compilam.

**O `slot` continua levando o NOME, e a tradução para número mora em
`catalogo_imagens.h`, ao lado das imagens — não no adapter.** A ordem das
imagens é um fato daquela pasta (quem gerou o `imagens.h` decidiu que a 5 é o
DESOL). Um campo `imagem` calculado no adapter seria a segunda cópia desse
fato, em outra linguagem e outro processo — e a divergência não dá erro: dá a
caixa do medicamento vizinho na tela, o quadro exato de um medicamento trocado.
É a regra de "A cópia do mapa no cnc_simulator não existe mais". De quebra, o
protocolo, o adapter e a placa falsa não mudaram.

A comparação ignora maiúsculas e espaços e mais nada. Nome sem imagem vira tela
de TEXTO com o nome escrito — nunca uma caixa "parecida", porque `XAFAC 15MG`
casado com a imagem do `XAFAC 10MG` é uma tela afirmando o que ninguém
conferiu. `tests/test_telas_imagens.py` cobra contagem, ordem, tamanho e que
todo nome exista no `_MEDICAMENTOS_SEED`; o `static_assert` do sketch cobra a
contagem na compilação.

O firmware não tem `toupper`: a normalização usa um `maiuscula()` próprio,
porque `test_dispenser_firmware.py` trata `toupper` como marca de terminal
humano e passaria a exigir um parser que esta placa não tem.

**Partition Scheme "Huge APP" é obrigatório** (~1,95 MB contra 1,25 MB do
default). O `TFT.ino` cabia no default só porque referenciava 8 imagens e o
linker descartava as outras.

## O mini PC é Windows: porta fixa é obrigatória, e os adapters seriais rodam no host

O `docs/PROTOCOLO_SERIAL.md` assumia mini PC Linux com `devices:` no compose. O
mini PC HP da célula roda **Windows**, e o Docker Desktop não repassa COM para
container. A decisão é a que o painel de bancada já tinha, pelo mesmo motivo:
os três adapters com porta serial rodam FORA do Docker, no host, abrindo a COM
direto com pyserial. A ponte RFC2217 foi avaliada e preterida, e o custo está
escrito em `docs/DEPLOY_WINDOWS.md` porque some do código: mais um processo por
porta, latência, "cabo caiu" igual a "ponte morreu", e a perda da exclusividade
de abertura que o Windows dá à COM e que um socket TCP não tem.

**Com cinco portas, varrer é competir.** Com `<SUB>_SERIAL_URL` vazia cada
adapter sonda todas as portas por até 9,5 s cada (`PROBE_ASSENTAR_S` +
`PROBE_ESPERA_S`), e o painel repete a varredura a cada 3 s. A competição não é
por uma porta compartilhada — é que, durante a busca, cada processo abre as
portas DOS OUTROS: enquanto o dispenser-adapter segura a COM da CNC para
conferir, o cnc-adapter toma `ACCESS_DENIED` na própria. Cinco processos, cinco
portas: boot não-determinístico em que uma placa às vezes não é achada. Por
isso **toda placa tem a COM fixada no Windows e a variável preenchida**, e o
painel ganhou `APSEN_DISPLAY_PORTA` (definida, abre só ela e não varre; vazia,
varre como sempre). A porta fixa passa pelo MESMO `_probe_port` — DTR/RTS
desligados e confirmação pelo ping —, porque fixar o número não dispensa
conferir a placa.

**No compose isso é o profile `simulado`.** Os três adapters seriais e os
quatro simuladores ficam atrás dele; `COMPOSE_PROFILES=simulado` no `.env` (a
linha entrou no modelo do README) mantém `docker compose up` subindo os 13
serviços como sempre, e sem a linha sobem só os de container. O
`erp-simulator` declara os três adapters seriais com `required: false`
(Compose ≥ 2.20): com o profile ligado a prontidão vale como sempre; sem ele o
compose avisa em vez de recusar subir o ERP por uma dependência que roda no
host. O vision-adapter e o vision-simulator ficam em container nos dois modos —
a visão é HTTP. As estações de visão REAIS rodam no host, donas das webcams
(ver "A visão real: o adapter traduz, a estação não muda"), e o simulador
continua subindo como rollback delas: quem escolhe a fonte de cada câmera é o
`.env` (`VISAO_MESA_FONTE`, `VISAO_DISPENSER_FONTE`), não o profile.
`tests/test_compose.py` prende o conjunto de cada lado e o `required: false`.


## A balança tem DUAS vozes na mesma porta, e o `{` é o que as separa

A balança foi o primeiro firmware a sair do papel (`weight/balanca2_3/`, a
`weight/balanca2.2.ino` intacta ao lado como referência de bancada). Ela chegou
falando só com humano — dezenas de `Serial.print` e comandos de uma letra — e a
`2.3` **não trocou** essa voz por uma de máquina: acrescentou uma segunda. Toda
linha de máquina tem um `{`; toda linha sem `{` é log, e o `extrair_json` do
`serial_link` já as ignorava.

A alternativa — traduzir o texto humano no adapter — é a que este repositório
recusa em toda parte: um parser de `Peso total [g]: 152.37` quebra no dia em
que alguém ajusta uma casa decimal, e quebra em silêncio. Manter as duas vozes
custa linhas a mais no canal e **nada** em acoplamento: o operador continua
abrindo o Monitor Serial e vendo a 2.2 que ele conhece.

O corolário mordeu na hora: `g<valor>` tem um equivalente de máquina, mas `u` e
`c0..c3` **não podem ter**. Eles bloqueiam em `readSerialLine()` por até 15 s
esperando alguém digitar, e nesse tempo a placa não lê comando nem responde
ACK — um `u` mandado pelo PC travaria a balança no meio de uma OS, e o sintoma
seria `timeout_peso` num slot íntegro.

### O que é da OS sobe; o que é da BANCADA para no adapter

A balança conta comprimidos por peso, calibra canal a canal e guarda config na
NVS — coisas que o central não tem endpoint para receber e não decide nada com.
Por isso `boot`, `peso`, `contagem`, `cfg`, `estado`, `tara_balanca` e
`erro_balanca` ficam no `weight-adapter` (`_EVENTOS_BANCADA`) e saem por
`GET /balanca`; só `tara_ok`, `peso_ok`, `peso_divergencia`, `erro_sensor` e
`telemetria` seguem para o central. É a mesma divisão que a seção 6 do
`docs/PROTOCOLO_SERIAL.md` já fazia com os eventos das telas TFT, e pelo mesmo
motivo: despejá-los em `/api/v1/eventos/peso` misturaria duas conversas num
histórico que hoje é só de pesagem de OS — sem erro, porque o evento atravessa
sem interpretação.

A lista é escrita pelo lado que falha VISÍVEL: um `tipo` fora dela **é**
encaminhado. Esquecer de acrescentar um evento novo dá uma linha estranha no
central, nunca um evento mudo.

O par disso são os comandos de bancada (`POST /bancada/*`, `_COMANDOS_BANCADA`).
Eles existem porque, com o transporte serial ligado, o adapter é o **dono** da
porta: ninguém mais abre o Monitor Serial, e sem eles definir o peso unitário
passaria a exigir parar o adapter. E ficam **fora** de `_ROTAS_SIM`: aquela
tabela promete as duas pernas, e o `weight-simulator` não tem balança para
configurar — a perna HTTP daria 404 no transporte que é o default. Com `http`,
`POST /bancada/*` responde 503 dizendo isso.

### Duas taras não são a mesma tara

`POST /comandos/tara` (o comando da OS) move o **zero lógico** da mesa e não
toca em hardware; `tara_canais` zera os offsets do HX711 e **grava na NVS**.
Tarar o hardware no meio de uma OS, com peso em cima, faz a balança mentir para
sempre — e a mentira fica gravada. A terceira, `tara_recipiente`, mede o pote.

### `boot` é o único evento que muda o comportamento do adapter

E é o item "DTR/RTS saem desligados ANTES de abrir" ao contrário. Como o
adapter **não** reinicia a placa, todo `boot` que chega até ele é um boot que
ninguém pediu — queda de energia, botão de reset, outro processo abrindo a
porta. E o `setup()` da balança faz **tara automática** depois de 5 s: se havia
peso na mesa, a tara levou o peso junto.

O adapter loga e marca `tara_confiavel: false`. **Não bloqueia nada**: a tara
pode estar certíssima (mesa vazia no reset), e recusar pesagem por causa disso
trocaria um número possivelmente errado por uma planta parada. Quem devolve a
confiança é um `POST /comandos/tara` aceito, porque é exatamente o ato que o
boot invalidou — e não um botão à parte, que alguém clicaria sem esvaziar a
mesa. O comportamento de tara no boot do firmware **não mudou**: quem depende
dele é o operador que usa a balança sozinha.

### O `cmd_id` é monotônico dentro de UMA sessão do adapter

O contrato manda a placa ignorar `cmd_id` menor ou igual ao último executado, e
isso é certo contra reenvio. Mas o contador nasce em 1 a cada `LinkSerial` novo:
um restart do processo do adapter faria a placa ver esse 1 como repetição,
responder **ACK positivo sem executar**, e o orquestrador esperaria para sempre
um evento que ninguém ia produzir — OS abortada por `timeout_peso` com a bancada
intacta. Abrir a porta não reinicia a placa (é o ponto acima), então quem tem
que notar a volta é ela.

O firmware detecta sem mensagem nova: o adapter só responde pong ao ping DA
PLACA, então silêncio de vários pings é o adapter fora do ar, e a volta zera
`ultimoCmdId`. O contrato não mudou — mudou quem sabe quando a sessão começou.

### O teste lê C++ por texto, e foi validado por mutação

`tests/test_balanca_serial.py` confronta firmware, adapter, placa falsa e
documento. A parte que lê o `.ino` é por texto (não há AST para C++), e regex
que para de casar deixa tudo verde para sempre — a mesma armadilha que
`test_protocolo_serial.py` registra para o display. Daí a validação por mutação:
nove quebras deliberadas do contrato, nove vermelhos. E duas extrações já
custaram falso positivo e têm helper próprio: a **declaração adiantada** casa
com a assinatura da definição (o `.ino` precisa delas porque `saveCountConfig()`
chama emissor definido lá embaixo), e o **comentário que explica por que algo
não é usado contém o nome da coisa não usada**.

A guarda do firmware MQTT (`test_rename_manut.py`) precisou aprender a
diferença: ela reprovava qualquer `.ino` versionado, e o que motivou a remoção
não era a extensão — era código gravado numa placa que não conversava com nada
(`PubSubClient`, tópicos `apsen/*`, broker que não existe no compose desde a
migração para REST). Hoje ela cobra o LUGAR com exceção para `weight/`, e um
segundo teste cobra o PROTOCOLO — marcas de API MQTT, nunca a palavra solta,
porque o `main.cpp` do display a cita em dois comentários que explicam
justamente a migração.

## A visão real: o adapter traduz, a estação não muda

As câmeras simuladas ganharam estações de verdade, que vieram prontas de um
pacote externo: `vision/visao_mesa` (a câmera da mesa) e `vision/visao` (as
câmeras dos dispensers, uma instância por fileira). Elas rodam no host Windows,
donas das webcams USB, como os adapters seriais.

**O código delas não é editado por este repositório.** Foi esse código que se
ensaiou, e a suíte não importa cv2: um limiar "melhorado" em `visao_mesa.py`
invalidaria o ensaio sem ficar vermelho em lugar nenhum. `tests/test_visao_intocada.py`
prende o sha256 de todo `.py` das duas estações (com `\r\n` normalizado, porque o
checkout no Windows troca o fim de linha sem ninguém ter mexido) e reprova
arquivo mudado, sumido ou novo. Configuração de bancada (`config/*.json`, o
`.env` da estação) pode mudar; código, não. Do resto da pasta `vision/` que veio
no pacote, só as duas estações entram no git: o `backend/` e o `firmware/` eram
uma cópia ANTIGA do painel — sem nenhuma das correções de segurança, com um
banco de PINs em claro — e saíram.

### A mesa só troca de endereço

A estação da mesa já falava o contrato do vision-simulator (o mesmo
`/executar/capturar/mesa`, os mesmos `leitura_mesa_*`), então o adapter só
aponta `VISION_SIM_URL` para ela (`host.docker.internal:8212` — não a 8202, que o
simulador publica no mesmo PC). O simulador continua no compose: é o rollback, e
voltar a ele é tirar as linhas do `.env`.

**Telemetria simulada é descartada no adapter.** O simulador segue no ar e
segue mandando temperatura das três câmeras a cada 60 s. Nenhuma estação real
emite telemetria, então, para uma câmera que já é real (`VISAO_*_FONTE=estacao`),
toda telemetria que chega foi inventada — e o central a gravaria em
`leituras_sensores` ao lado do dado de verdade. O filtro descarta só os
componentes que SABE serem do simulador; um componente novo passa.

**O adapter confere QUEM está do outro lado de `VISION_SIM_URL`.** Fonte e
endereço são duas variáveis, e esquecer uma passa calado: com
`VISAO_MESA_FONTE=estacao` e a URL no default, a contagem SORTEADA do simulador
entraria no Triple Check como câmera real. Os dois se identificam no `/ping`
(`apsen-vision-simulator` × `apsen-vision-station`); o adapter compara com o
que a fonte espera numa tarefa de fundo, a cada 30 s, e com a divergência
CONFIRMADA recusa `/comandos/capturar/mesa` com 503 — câmera real contando de
mentira é pior que câmera ausente. Identidade desconhecida (upstream fora)
segue como antes. O `/health` publica fontes, identidade, cobertura de
etiquetas, a idade da última busca de catálogo por lado e o `/status` da
estação da mesa; é ele que o pré-voo lê, numa sonda só.

**O `lifespan` não espera o upstream.** Na célula o Docker sobe ANTES das
estações do host, e as 30 tentativas de `_wait_for_upstream` seguravam o adapter
sem servir por até ~150 s — o compose o marcava unhealthy e não subia o
erp-simulator. Hoje a espera só loga, em segundo plano, e o `start_period` do
vision-adapter voltou ao padrão. Os números do `.env` passam por `_cfg_float`
(vírgula aceita, faixa, default com ERROR): `2,5` derrubava o adapter no
import, em laço de restart, com o traceback apontando para o `main.py`. Os
`TIMEOUT_*` do central passaram pelo mesmo leitor (`config._timeout_s`).

### Os dispensers: o adapter serve o catálogo e lê o veredito

A estação dos dispensers não recebe comando: julga cada zona sem parar contra um
catálogo que busca de tempos em tempos, e publica o veredito em `/api/estado`.
Quem sabe o que DEVERIA estar em cada slot é o central; quem sabe o que ESTÁ é a
estação; quem junta os dois é o adapter — ele serve o catálogo
(`GET /api/visao/catalogo`) e traduz o veredito em `leitura_dispenser_*`. O
central não mudou. A parte pura está em `vision-adapter/ponte_dispensers.py`.

**O catálogo é SÓ da OS corrente.** Herdar o slot da OS anterior poria o mesmo
medicamento em dois dispensers, e o `Catalogo.de_itens` da estação recusa o
catálogo inteiro — e, com a estação rodando, recusar não volta para o arquivo
local: ela segue julgando pelo catálogo ANTERIOR, sem nenhuma linha de log. Por
isso o catálogo nunca pode ser um que ela recuse: slot sem etiqueta e
medicamento repetido na OS vão para `incompletos`, e `tests/test_vision_adapter_ponte.py`
alimenta a resposta do adapter no `Catalogo.de_itens` de verdade, importado do
código da estação. Item malformado é pior ainda: `KeyError` e `TypeError` não
são capturados por ela e derrubam a estação.

**Fantasmas: todo medicamento etiquetado entra no catálogo.** O que não está em
slot nenhum da OS vai com dispenser `1000 + posição na tabela`, número sem zona
na imagem. É isso que faz a estação dizer O QUE achou: uma caixa de DONAREN no
D3 de uma OS que pediu RETEMIC é "DONAREN, cujo dispenser é outro" —
`ERRO_POSICAO` com nome. Fora do catálogo, a mesma caixa daria `NAO_CADASTRADO`
sem nome, e a trava diria que está errado sem dizer o quê.

**Slot fora do catálogo falha na hora, sem perguntar à estação.** Sem etiqueta,
ela não tem como reconhecer. Repetido na OS, perguntar é pior: o slot não tem
`esperado` no catálogo, a caixa certa aponta para o fantasma, e a estação daria
`ERRO_POSICAO` — trava por uma caixa que está certa.

**Frescor pelo relógio da estação.** Ler o `/api/estado` na hora do comando
devolve o veredito dado com o catálogo da OS ANTERIOR. A leitura só vale quando
o catálogo atual está servido há `VISAO_DISP_INTERVALO_CATALOGO_S` +
`VISAO_DISP_ASSENTAMENTO_S` (relógio do adapter, que sabe quando o conteúdo
mudou) E o `momento` da linha do slot avançou esse tanto desde o comando
(relógio da ESTAÇÃO — compará-lo com o nosso confundiria fuso ou relógio
desacertado com câmera parada). Catálogo mudando no meio (outro slot da mesma
OS) recomeça a contagem; re-scan da trava, com o catálogo antigo, espera só o
assentamento. O intervalo do adapter TEM de ser igual ao
`backend.intervalo_catalogo` das estações, e o `iniciar_dispensers.bat` confere.
Das linhas do `/api/estado` vale a mais recente do slot: a estação guarda uma
por nome de estação, e a de um `--estacao` antigo fica lá com o `momento` parado.

**O veredito da estação é a fotografia de UM frame, e por isso há janela.** A
estação grava em `estado_atual` o veredito de cada `processar`; o
`frames_para_confirmar` dela governa só o alerta sonoro. Na zona vence o pior
caso, então um único frame com a mão do operador, um reflexo ou um ArUco falso
de id CONHECIDO — e, com os fantasmas, todo medicamento etiquetado é conhecido:
o descarte de "id fora do catálogo", que era a defesa contra falso ArUco, deixa
de proteger à medida que a tabela cresce — virava `ERRO_POSICAO` e trava. O
adapter colhe `VISAO_DISP_JANELA_S` (mínimo 3) amostras de `momento`s
DISTINTOS e `ponte.decidir` julga: divergência só se a MESMA leitura divergente
aparece em 2/3 das amostras E na última; OK se a maioria é OK e nenhuma
divergência se repete; o resto é falha `leitura_instavel`, com as contagens no
campo `amostras`. Registro à parte, e é armadilha: **o `estacao.py` ignora
`dicionario_aruco` e `lado_minimo_aruco` do `parametros.json`** (só o
`main.py`, o `calibrar.py` e o `diagnostico.py` os leem). Trocar o dicionário e
reimprimir as etiquetas faria o leitor de produção parar de ver ArUco em
silêncio — não troque o `DICT_4X4_50`.

**O relógio supõe; a busca pela rota do lado PROVA.** A estação pode não trocar
de catálogo sem dizer nada: `buscar_catalogo` engole timeout e devolve `None`
("mantém o atual"), `recarregar_catalogo` faz `except ValueError: return` sem
log. Aí ela julga pela OS anterior, e o medicamento CERTO desta OS, que na
anterior era de outro slot, sai `ERRO_POSICAO` — trava falsa. Sem editar a
estação: ela monta a URL como `{backend.url}/api/visao/catalogo`, então cada uma
aponta para `http://127.0.0.1:8102/estacoes/<lado>` (configuração), e o adapter
registra QUAL lado buscou e quando. A amostra só vale se a estação daquele lado
buscou DEPOIS de o conteúdo mudar; sem busca no prazo, falha
`estacao_sem_catalogo_atual`. Lado que nunca usou a rota nova (URL antiga) cai
no critério só de relógio, com UM aviso no log. As rotas antigas ficam, para o
rollback. E o JSON servido à estação leva só `medicamentos`: ela imprime uma
linha por `incompletos` a cada busca, e com a maioria dos itens sem etiqueta o
console dela virava ruído.

**QR alheio na zona não trava (`VISAO_DISP_NAO_CADASTRADO`, default
`falha`).** Código lido que não está no catálogo é `QR_DESCONHECIDO` na estação,
estado de erro que VENCE a ocorrência certa na mesma zona — e o leitor filtra
por simbologia, não por conteúdo. Embalagem real traz cada vez mais QR (bula
digital, lote do fornecedor): como divergência, ele travava a OS com o
medicamento CERTO. O ganho de travar era pequeno — caixa errada sem etiqueta e
sem QR já sai `VAZIO` (falha). Hoje `NAO_CADASTRADO` é falha
`codigo_desconhecido_na_zona`, com alarme PRÓPRIO
(`codigo_desconhecido_dispenser`) para dar para contar quantas vezes acontece em
campo; `divergencia` é o comportamento antigo, e o procedimento D6 de
docs/BANCADA_VISAO.md é o que decide entre os dois.

**Sem etiqueta é COBERTURA, não falha de hardware.** Só 4 medicamentos têm
etiqueta, e eles cobrem 6 dos 46 itens das ordens padrão. Um alarme
`falha_leitura_dispenser` por item `sem_etiqueta_cadastrada` eram ~4 alarmes
abertos por OS, para sempre (não há o que resolver), empurrando para fora da
tela de necessidades o alarme que importa. Hoje esse motivo grava a leitura e
um log INFO; a cobertura é um número informativo no pré-voo ("N de M
medicamentos das ordens padrão têm etiqueta", com as ordens sem nenhum item
etiquetado). Pelo mesmo motivo de "um evento, um alarme", o handler do central
deixou de gravar `divergencia_contagem` para `leitura_mesa_divergencia`: quem
decide o que ela significa é o orquestrador (trava, oclusão, dessincronia,
payload incoerente), e ele grava o alarme certo — o do handler afirmava
"contagem incorreta" justamente quando o sistema decidia que não era. A
reconciliação de oclusão é a exceção: log, sem alarme.

**Leitura incerta é falha, nunca divergência** — divergência trava. `VAZIO`,
`INDETERMINADO`, veredito desconhecido, estação fora do ar, momento parado e
zona sem linha viram `leitura_dispenser_falha` com motivo próprio. Exatamente UM
evento por comando, inclusive sob exceção (`erro_interno`). O `sku_lido` é o SKU
DO CENTRAL do nome achado (`GET /medicamentos`), porque é ele que a trava mostra
ao supervisor; o código da etiqueta (`MED-004`) ninguém no central reconhece.

**O estoque que a estação mede é aceito e descartado.** Quem manda no estoque
dos dispensers é o central. A resposta é 2xx porque a estação reenvia para
sempre o que não recebeu 2xx.

**As estações sobem por um conferidor e se reerguem por um laço.** Nada do
que dá errado na configuração de uma estação dá erro na hora certa: o
`mesa.json` versionado está em `fundo.modo="manual"`, que com a caixa andando
contou 8 caixinhas como 8, 6, 6 e 4, sempre com `confiavel=True`; `PORTA` no
default colide com o vision-simulator; a pasta `visao_dir` nasce com as zonas
D1–D4 DO MODELO; `CAMERA_DIR=1` era o mesmo `camera.indice=1` da mesa; e o
`estacao.py` sai do laço na primeira leitura falha da câmera com código 0 — a
janela fecha e a conferência de SKU acaba pelo resto do turno, visível só como
`camera_indisponivel`. Nada disso se corrige DENTRO das estações (Regra nº 1),
então o contorno é do lado de cá: `vision/conferir_mesa.py` e
`vision/conferir_dispensers.py` (lógica em função pura, testada por
`tests/test_conferir_*.py`, e só biblioteca padrão — cada um roda com o Python do
venv da sua estação) reprovam a subida com o que fazer; `vision/iniciar_mesa.bat`
e `vision/iniciar_dispensers.bat` rodam a estação num laço que registra cada
queda e a sobe de novo em 5 s. A câmera dos dispensers passou a ser a gravada
POR NOME pelo `camera.py`: `--camera <número>` ia direto para o índice, e o
`.bat` sempre o passava. O `iniciar_estacao.bat` que veio dentro da estação da
mesa continua lá, intocado.

**A premissa foi executada, não só lida.** `vision/ensaio_dispensers.py` sobe a
estação de verdade lendo uma cena sintética, o adapter de verdade e um central
falso, e prova que o veredito troca quando a OS muda (4 ok; depois D1/D2
trocados → 2 divergências com o nome achado).

### A câmera da mesa que nem sempre vê a caixa inteira

A câmera da mesa é fixa num poste, inclinada, e a caixa anda com a CNC. A
estação conta o TOTAL da caixa e subtrai o último total que ELA registrou na
OS — e nunca refotografa um par (os_id, slot) que já recebeu.

**Pendência.** Em posição em que a caixa não aparece inteira
(`VISAO_MESA_POSICOES`, medido na bancada) o central não pede foto; e uma foto
que falha sem a estação registrar o total também não conta. Nos dois casos a
foto SEGUINTE vê os slots juntos, e comparar com a quantidade de um slot só
seria divergência falsa: ela leva a soma (`esperado_camera`) e os
`slots_cobertos`. No fim da OS, `VISAO_MESA_FINAL=HOME` confere o que sobrou,
usando o slot de um pendente que nunca foi pedido — se não houver, não há foto
possível, e fica o alarme.

**Dessincronia.** Timeout, envio recusado e `timeout_processamento` — que
continua rodando na estação e pode atualizar o total em silêncio — deixam sem
saber se a estação registrou o total. A divergência seguinte não é comparável:
vira fonte indisponível com alarme, e a própria foto ressincroniza. Liberar
qualquer trava da OS também dessincroniza: com a trava aberta, alguém pode ter
mexido na caixa.

Foto tirada sem o `dispensado` também dessincroniza, e é o caso menos óbvio. O
ciclo é por relógio: sem a confirmação, o central não sabe se o mecanismo
terminou (servo mais lento que o cronograma, pulso atrasado), e a estação acaba
de registrar um total que pode não incluir unidades ainda caindo. A que cair
depois da foto apareceria como "a mais" no slot SEGUINTE — trava no slot
errado, com o certo marcado OK. O central não espera o `dispensado` por causa
disso: o relógio não ganha portão novo; a foto seguinte só deixa de ser
comparada.

A conferência final no HOME **herda** a dessincronia (`_conferir_mesa_no_home`
recebe `dessincronizada`). Sem isso, timeout na última foto visível seguido de
slots invisíveis — ou trava liberada seguida só de slots invisíveis — chegava ao
HOME comparando um incremento que pode incluir o slot cujo total ninguém sabe se
foi registrado: "a mais" e trava. Ali a divergência vira o alarme
`conferencia_final_nao_comparavel`, com a contagem e o esperado no texto.

**A estação pode perder a memória da OS**, e o central não teria como saber. O
acumulado vive só na memória dela e expira em `VALIDADE_OS_H` (2 h no default)
desde a última foto: um restart no meio da OS, ou uma trava esperando
supervisor por mais de 2 h, faz a foto seguinte comparar a caixa INTEIRA com o
esperado de um slot. Quem detecta é o vision-adapter: ele vê passar toda
leitura que a estação emitiu e, antes de repassar a próxima captura de uma OS
que já tinha leitura, confere no `/status` da estação se ela ainda conhece a
OS. Não conhecendo, o evento dessa foto sai com `ressincronizar: true`, e o
central o trata como dessincronia (motivo "estação da mesa sem o acumulado desta
OS") — no passo 4 e no HOME. `/status` fora do ar não bloqueia nada. E o
`.env` da estação leva `VALIDADE_OS_H=24`: trava longa não pode apagar a conta.

**Câmera contando a menos, sozinha, não trava** — ver a exceção em "Triple
Check: 1 divergência trava". É por ela que a injeção `divergencia_mesa` passou a
contar uma a MAIS.

**`VISAO_SKU_HABILITADA=0`** existe para a bancada sem as câmeras dos
dispensers: sem a chave, o central mandava o comando a cada slot e retentava
3× para chegar ao mesmo desfecho de hoje — o slot segue sem conferência.
