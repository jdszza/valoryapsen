# TASKS — APSEN

Backlog único do repositório. Substitui `TASKS_EVOLUCAO.md` e
`TASK_PAINEL_OPERADOR.md`. A documentação do sistema está no
[`README.md`](README.md); as decisões de arquitetura, no `CLAUDE.md`.

**Mecânica:** cada bloco `text` é um prompt autocontido para colar no Claude
Code, um por vez, na raiz do repositório. **Correção + teste de regressão.
Sem commit** — você revisa e commita.

---

## Índice

- [O que já foi entregue](#o-que-já-foi-entregue)
- [P0 — Segurança do painel de bancada](#p0--segurança-do-painel-de-bancada)
- [P1 — Bugs funcionais](#p1--bugs-funcionais)
- [P2 — Robustez e performance](#p2--robustez-e-performance)
- [Tasks opcionais de apresentação](#tasks-opcionais-de-apresentação)
- [Decisões adiadas de propósito](#decisões-adiadas-de-propósito)

---

## O que já foi entregue

Rodada de evolução (commits `aeef1e2` e `20c1226`):

| # | Task | Estado |
|---|------|--------|
| 1 | 8 dispensers em duas fileiras frente a frente | ✅ feito |
| 2 | Três câmeras: uma por lado dos dispensers e uma da balança | ✅ feito |
| 3 | Remover o display Arduino e renomear a interface de :8051 para `manut` | ✅ feito |
| 4 | 10 ordens fixas com chave primária única por disparo | ✅ feito |
| 5 | Console de operação no central-computer | ✅ feito |

Rodada de apresentação e operação:

| # | Task | Estado |
|---|------|--------|
| 6 | Modo apresentação: falhas desligadas e velocidade ajustável | ✅ feito |
| 7 | Injeção de falha sob demanda, armada pelo console | ✅ feito |
| 8 | Reset limpo da planta e histórico de demonstração | ✅ feito |
| 9 | Tela de pré-voo (`/console/prevoo`) | ✅ feito |
| 10 | Tela de necessidades como primeira aba do app de manutenção | ✅ feito |
| 11 | Rename do serviço de ordens para `erp-simulator` | ✅ feito |

Integração do painel de bancada:

| # | Task | Estado |
|---|------|--------|
| 1 | Painel de bancada dentro do repo, espelhando ordens + slots do central | ✅ feito |
| 2 | Firmware: `os_id` do central e ordens só-leitura no display | ✅ feito (falta **regravar o display em campo**) |

> O display que está na bancada foi gravado antes da integração: ele trunca o
> `os_id` do central em 16 bytes e dois disparos do mesmo template colidem no
> mesmo id. Regravar o firmware é a única pendência dessa rodada — ver o
> [docs/BANCADA.md](docs/BANCADA.md), seção "O firmware e o `os_id` do central".

Supervisor, telas TFT e portas fixas:

| # | Task | Estado |
|---|------|--------|
| 1 | Bugs soltos: `ORDER BY prioridade` por `CASE`, `peso_atual_g` da balança no estado e no banco, `_ate` com prazo por env | ✅ feito |
| 2 | Perfil `supervisor` no central: portão `_get_supervisor_ou_admin`, role validada, `em_nome_de` na liberação | ✅ feito |
| 3 | Perfil Supervisor no painel, `central_comandos.py` (a única escrita no central) e a faixa da trava na web | ✅ feito |
| 4 | Trava no display de 7": `get_trava`, `liberar_trava`, push `trava`, tela + numpad nome/PIN | ✅ feito (falta **regravar o display em campo**) |
| 5 | Segunda placa do dispenser-adapter: as 8 telas TFT (`dispenser_tft`), `estado_celula`, `trava_resumo` ≤ 48 | ✅ feito |
| 6 | O central avisa a célula que travou (`/comandos/estado-celula`, uma vez, timeout curto) | ✅ feito |
| 7 | Portas fixas no mini PC Windows: `APSEN_DISPLAY_PORTA`, profile `simulado`, `docs/DEPLOY_WINDOWS.md` | ✅ feito |

> **Trava do Triple Check no display** saiu de "Decisões adiadas de propósito":
> a decisão de operação foi tomada — o supervisor libera pelo painel de
> bancada, autenticado por PIN, na web e no display. O display em campo precisa
> ser regravado de novo (a pendência acima acumula a tela de trava) — ver o
> [docs/BANCADA.md](docs/BANCADA.md), seção "A trava do Triple Check no display".

---

# Backlog aberto

Origem: varredura completa do código (setembro/2026). O relatório detalhado com
as 35 ocorrências está em `AUDITORIA_CODIGO.md`; aqui ficam as que viram
trabalho, agrupadas para serem executadas juntas.

## P0 — Segurança do painel de bancada

Estes quatro são o mesmo assunto e devem ser feitos numa tacada: o
`painel_operador/backend` nasceu como app de bancada isolado e hoje escuta em
`0.0.0.0` na rede da planta. O `central-computer` já resolveu cada um destes
problemas do lado dele — a referência está no próprio repositório.

```text
Contexto: projeto APSEN. Leia CLAUDE.md e README.md na raiz ANTES de mexer.
Escopo: painel_operador/backend/app.py.

Quatro problemas de segurança, todos na mesma superfície:

1. GET /api/operadores (app.py:3153) devolve o PIN de TODOS os operadores em
   texto puro, sem autenticação nenhuma. O login web é nome + PIN, então quem
   faz um curl nessa rota entra como Admin. A rota existia para o ESP32 buscar
   por HTTP; hoje o display fala por SERIAL (cmd get_operadores), então ela não
   tem mais consumidor.
   - Remova a rota. Se houver motivo para mantê-la, devolva só nome e perfil.
   - Pare de trafegar e de guardar PIN em claro: grave hash (werkzeug.security
     ou bcrypt) e valide por hash. A coluna `pin` sai; entra `pin_hash`, com
     migração idempotente no padrão que o init_db já usa (PRAGMA table_info +
     ALTER TABLE). O cmd serial get_operadores precisa continuar funcionando —
     decida e explique se o display passa a mandar o PIN para o backend validar
     ou se recebe o hash.

2. TODAS as rotas /api/* são abertas (linhas 2408, 2439, 2487, 2527, 2727,
   2751, 2777, 2794, 2841, 2854, 2859, 2867, 2883, 3153, 3164, 3188). Sem
   autenticação dá para criar e alterar ordens, REESCREVER O ESTOQUE com baixa
   FEFO real (POST /api/visao/estoque, PUT /api/dispensers/sync), trocar o
   medicamento de um slot e escrever no audit log.
   - Crie um decorator @api_token_required lendo APSEN_API_TOKEN do ambiente
     (header X-API-Token) e aplique a todo o bloco /api/*.
   - Sem a variável definida, decida entre recusar as rotas (503) ou recusar o
     boot — e explique a escolha. Não invente token default: o central já
     recusa segredo default e é essa a disciplina do repo.

3. app.run(debug=True) em host 0.0.0.0 (app.py:3200). O console do Werkzeug é
   execução remota atrás de um PIN.
   - debug só quando APSEN_DEBUG=1, default desligado.
   - Documente no README como servir em produção (waitress no .bat da bancada).

4. app.secret_key cai em "apsen-dev-2024-change-in-prod" (app.py:19) — quem lê
   o repositório assina cookie de Admin. É o mesmo bug que central-computer/
   config.py:validar_secret_key já resolve.
   - Aplique a mesma regra: sem APSEN_SECRET (ou com o default), aborte fora de
     APSEN_ENV=dev, com a mesma mensagem de receita para gerar a chave.

Testes: rota de PIN inexistente ou sem PIN no corpo; /api/* respondendo 401 sem
token e 200 com token; app não sobe com secret default fora de dev; login
continua funcionando com PIN hasheado, incluindo o operador seed.

Não faça commit.
```

Complementos da mesma frente, menores, que podem entrar junto:

- **Open redirect no login** (`app.py:1773`): `next` é usado direto no redirect.
  Aceitar só caminho relativo (`urlparse(next).netloc == ""`).
- **Nenhuma proteção CSRF** em todo o painel: todo POST de estado confia só no
  cookie. `Flask-WTF` (`CSRFProtect`) ou, no mínimo,
  `SESSION_COOKIE_SAMESITE="Strict"`.
- **Nada impede excluir o último Admin** (`app.py:3082`) — lockout irreversível
  pela UI. O central já tem essa guarda (`main.py:1728`).
- **`/admin/historico/limpar` apaga o audit log inteiro** (`app.py:3139`). Num
  sistema de dispensação, apagar a trilha sem registro é problema de
  conformidade — e zera os KPIs de tempo de ciclo junto. Arquivar em vez de
  deletar, ou no mínimo registrar quem/quando/quantas linhas.

## P1 — Bugs funcionais

```text
Contexto: projeto APSEN. Leia CLAUDE.md na raiz.

Cinco bugs de comportamento, cada um com sintoma silencioso:

1. painel_operador/backend/app.py:2266 — bloquear um lote não remove o saldo do
   estoque agregado. bloquear_lote muda só lotes.status; medicamentos.quantidade
   segue contando o lote bloqueado. Então verificar_estoque_ordem libera a
   ordem, consumir_fefo (linha 726) não acha lote 'Ativo', cai no ramo
   restante > 0 e grava "SEM LOTE REGISTRADO" — debitando o agregado assim
   mesmo. Resultado: dispensa de lote bloqueado, sem genealogia, que é o oposto
   do que a tela promete.
   Escolha entre subtrair do agregado ao bloquear (e devolver ao desbloquear) ou
   derivar medicamentos.quantidade de SUM(lotes.quantidade WHERE status='Ativo').
   Justifique — manter dois números que precisam concordar é o que criou o bug.

2. central-computer/orchestrator.py:795 e :831 — os comandos de carregamento e
   de scan são enviados em laço SEQUENCIAL com await, embora o comentário diga
   "em paralelo". _post tenta 3x com sleep(1) e timeout 10s: até ~32s por slot.
   Com 8 slots e adapter lento, o último comando sai depois de ~4 min enquanto
   TIMEOUT_CARREGAMENTO (180s) já corre para o PRIMEIRO slot — a OS aborta por
   timeout de um dispenser que nunca recebeu ordem.
   Troque por asyncio.gather. E aborte na hora quando o envio falhar (ok=False),
   em vez de esperar o timeout inteiro.

3. central-computer/orchestrator.py:1281 — exceção não tratada em _processar_os
   marca a OS como erro e zera os_ativa, mas NÃO chama _limpar_eventos_os nem
   _liberar_slot. O medicamento fica fisicamente no dispenser, o estado em
   memória guarda o os_id da OS morta, e a próxima OS bate em 409 na limpeza.
   _abortar_os já faz tudo isso certo — chame-o no except.

4. central-computer/orchestrator.py:1163 — _abortar_os reseta TODOS os slots
   (for key in _estado["dispensers"]), inclusive os que guardam residual de
   outra OS. O caminho de sucesso (linha 1105) só toca nos atribuídos. Itere
   sobre `atribuicoes`.

5. painel_operador/backend/app.py:2032 — o status da ordem vem da URL sem lista
   de valores válidos: /ordens/1/status/Qualquer grava o que vier, e a ordem
   some de todas as telas (dashboard, KPIs e a fila do display filtram por
   string exata) sem erro nenhum. O central faz essa validação em main.py:1596.
   Whitelist {Pendente, Em Processo, Pausado, Concluido, Erro, Cancelado}.

Testes de regressão para cada um, incluindo o caso que hoje passa calado.

Não faça commit.
```

Mais dois da mesma família, menores:

- **`numero_os` da nova ordem vem do formulário** (`app.py:1936` e `:2764`), não
  de `gerar_numero_os()`. Número duplicado vira `UNIQUE constraint failed` →
  HTTP 500; e um `numero_os` igual ao `os_id` de uma ordem do central cria uma
  linha `origem='local'` que faz o espelho **descartar para sempre** a ordem real
  da célula. Gerar no servidor e tratar `IntegrityError`.
- **`ORDER BY prioridade DESC` ordena texto** (`app.py:2817`): com
  Urgente/Alta/Normal/Baixa, o DESC alfabético põe **Alta em último**, atrás de
  Baixa. Trocar por `CASE`.

## P2 — Robustez e performance

```text
Contexto: projeto APSEN. Leia CLAUDE.md na raiz.

Sete itens de robustez, independentes entre si — pode fazer em qualquer ordem.

1. central-computer/database.py:23 — uma conexão MySQL nova por operação, sem
   pool. Cada evento de adapter faz 1 a 3 escritas, cada uma abrindo TCP +
   handshake + auth; com 8 slots de telemetria a cada 15s mais CNC, visão e
   balança, é a maior latência evitável do central. Introduza pool (DBUtils
   PooledDB ou um queue.Queue sobre _make_conn) — só o contextmanager _conn()
   muda.

2. central-computer/main.py:1544, 1573, 1578, 1583 — o parâmetro `limite` vai
   cru para LIMIT %s em /os/historico, /dispensas, /cnc/historico e /alarmes.
   ?limite=-1 vira erro de sintaxe MySQL (500) e um valor enorme varre a tabela.
   Só /api/v1/visao/historico faz clamp. Helper único, aplicado nos quatro.

3. central-computer/main.py:1380 — /api/v1/relatorio/os/{os_id} só decodifica o
   JWT; todo o resto passa por _get_tecnico, que confirma ativo=1 no banco.
   Técnico desativado segue baixando relatório de dispensação por até 8h. Troque
   por Depends(_get_tecnico).

4. central-computer/main.py:1592 — POST /auth/login não tem freio de força
   bruta, embora o console já tenha um (console.registrar_falha / bloqueado).
   Reaproveite o mesmo mecanismo, chaveado por IP + username.

5. Adapters (*/main.py, _post_central) — o evento é postado UMA vez; em falha,
   só loga. Esse é o único caminho de volta ao orquestrador: um evento
   'dispensado' perdido vira timeout e OS abortada. O _post do orquestrador
   retenta 3x; o caminho inverso, não. Aplique a mesma política de retry.

6. central-computer/orchestrator.py:679 — _post retenta QUALQUER status >= 300,
   inclusive 409 (limpeza_em_operacao) e 422, gastando 2s por recusa
   determinística. Retentar só em exceção de rede, timeout e 5xx.

7. painel_operador/backend/app.py:318 — get_db não liga PRAGMA foreign_keys=ON,
   então as FKs declaradas não valem. excluir_medicamento (linha 2179) deixa
   lotes, consumos e leituras de visão órfãos — e como _slot_para_id numera por
   POSIÇÃO (linha 1558), toda a numeração de slot do display muda depois de uma
   exclusão. Ligue o PRAGMA e desative (ativo=0) em vez de deletar medicamento
   com histórico.

Testes de regressão para cada item.

Não faça commit.
```

Itens de higiene, sem prompt próprio:

- `int()` sem proteção em campos de formulário do painel (`app.py:1919`, `2144`,
  `2145`, `2165`, `2166`, `2216`, `2238`) → HTTP 500 quando o campo vem vazio.
  `request.form.get("quantidade", type=int)`.
- `role` de usuário sem whitelist no central (`database.py:988` e `1001`) — um
  typo cria usuário sem acesso e sem erro.
- `get_alarmes_por_os` casa por `LIKE '%os_id%'` (`database.py:883`), sem escapar
  `%`/`_`. Uma coluna `os_id` própria em `alarmes` resolve de vez.
- `WSManager.broadcast` itera a lista viva (`main.py:196`) e `ws_endpoint` só
  remove o socket em `WebSocketDisconnect` (`main.py:2078`).
- `leitura_mesa_divergencia` formata com `:+d` (`main.py:724`) — `None` ou float
  levanta dentro do handler.
- KPI de tempo de ciclo usa um único "Iniciar" por OS (`app.py:2649`) — ordem
  reiniciada dá duração errada.
- `painel_operador/backend/apsen.db` não está no `.gitignore` (só `-wal`/`-shm`):
  um `git add -A` versiona o banco da bancada com os PINs dentro.

---

# Tasks opcionais de apresentação — ENTREGUES

As seis tasks desta seção foram executadas; os prompts saíram porque eram
instruções PARA executar, e o histórico do git as guarda. O que cada uma decidiu
— e por quê — está no `CLAUDE.md`, nas seções correspondentes:

| task | o que entregou | onde está documentada |
|------|----------------|----------------------|
| 6 | `MODO_APRESENTACAO` e `FATOR_VELOCIDADE` globais | "O modo apresentação desliga o ACASO" |
| 7 | gatilho de falha one-shot armado pelo console | "A falha injetada tem UM dono" |
| 8 | reset da planta e seed de histórico de demonstração | "O reset recusa em vez de resetar pela metade" |
| 9 | tela de pré-voo em `/console/prevoo` | "O pré-voo responde sim ou não" |
| 10 | tela de necessidades no app de manutenção | "A tela de necessidades ordena por IMPACTO" |
| 11 | rename do serviço de ordens para `erp-simulator` | "O serviço virou `erp-simulator`" |

O que sobrou de pendência real está registrado abaixo e no README:

- **Regravar o display da bancada.** O firmware gravado antes da integração
  trunca o `os_id` do central em 16 bytes, e dois disparos do mesmo template
  colidem no mesmo id — e o display em campo também não conhece a tela de
  trava (`get_trava`, `liberar_trava`, push `trava`). O passo a passo de
  gravação está no [docs/BANCADA.md](docs/BANCADA.md), em "Gravar e testar o
  display físico".
- **Os itens de higiene do P2**, listados na seção "Itens de higiene" acima.

---

# Decisões adiadas de propósito

Não são pendências esquecidas — cada uma foi deixada de fora por um motivo.


- **Estação de visão do pacote.** Ela conta caixas por QR/ArUco em bancada; o
  repo já tem `vision-adapter` e `vision-simulator` para as três câmeras da
  célula. São coisas diferentes com o mesmo nome, e juntá-las sem decidir qual é
  a fonte de verdade de "quantas unidades há no slot" produz exatamente a
  divergência que o CLAUDE.md descreve na seção de fontes de verdade.

- **Ordens locais empurradas para o central.** Hoje elas são criadas e
  ignoradas. Transformar a tela em porta de entrada de OS é fácil
  (`POST /api/v1/ordens` existe), mas cria uma segunda origem de ordem sem
  console — e o central já tem uma, em `/console`, com senha própria.

- **JWT do app de manutenção em `sessionStorage`** (`manut_web/app.py:126`).
  Legível por qualquer JS da página. Aceitável no Dash, registrado aqui como
  risco conhecido em vez de corrigido às cegas.
