# Auditoria de código — APSEN (valoryapsen)

Varredura completa de `central-computer`, 4 adapters, 5 simuladores, `dashboard`, `ihm_web`,
`mysql/init.sql`, Dockerfiles e `docker-compose.yml`. Base: commit `1ba2692`.

Legenda: **[REPRODUZIDO]** = executei o código e observei a falha · **[ANÁLISE]** = confirmado por
leitura do fluxo, não executado em runtime.

---

## P0 — quebram funcionalidade hoje

### 1. `weight-simulator`: toda pesagem falha com `UnboundLocalError` **[REPRODUZIDO]**

`weight-simulator/simulator.py:143-149` — o `global` declara apenas `_peso_anterior_g`, mas a
função também atribui `_peso_mesa_g`:

```python
with _lock:
    global _peso_anterior_g          # falta _peso_mesa_g
    _peso_mesa_g += peso_esperado_g + random.gauss(0, RUIDO_G)   # UnboundLocalError
```

Reprodução: `UnboundLocalError: cannot access local variable '_peso_mesa_g'`.
`pyflakes` também aponta (`local variable '_peso_mesa_g' ... referenced before assignment`).

**Impacto em cadeia:** a thread morre → nenhum evento de peso é emitido → o orquestrador espera
`TIMEOUT_PESO` (15 s) **por slot** → a fonte 3 do Triple Check nunca participa da validação, e
uma OS de 6 slots ganha 90 s de atraso puro.

**Correção:** `global _peso_mesa_g, _peso_anterior_g` (e `_peso_tara_g` se for escrever nela).
Vale mover as três declarações para o topo da função, fora do `with`.

> Existe uma versão local desse arquivo mais nova que o último commit — pode já estar corrigida
> na sua máquina. Confirme antes de aplicar.

---

### 2. A limpeza de dispenser nunca funciona depois da primeira OS **[REPRODUZIDO]**

`dispenser_simulator/simulator.py` — `_do_dispensar()` finaliza o slot com
`status="concluido"` e **não limpa `os_id`**. `_do_limpar()` recusa se
`status in ("carregando","pronto","dispensando","concluido")` **ou** `os_id is not None`.
Nenhum outro caminho zera esses dois campos — só `_do_limpar`, que é justamente quem está
bloqueado.

Reprodução (carregar → dispensar → limpar):

```
estado slot1 apos OS: {'status': 'concluido', 'os_id': 'OS-A', ...}
[DISP-1] Limpeza recusada — em operação (status=concluido)
evento: erro / limpeza_em_operacao
```

**Impacto:** o botão “🧹 Limpar Dispenser” da IHM é permanentemente inoperante.

**Correção:** ao concluir a dispensa, resetar `status="idle"` e `os_id=None`; e em `_do_limpar`,
bloquear apenas por `status in ("carregando","dispensando")` (operação em curso de verdade),
não por `"concluido"` nem por `os_id` residual.

---

### 3. A telemetria de status reescreve o estado do central com dados velhos **[ANÁLISE]**

`dispenser_simulator._telemetria_loop()` envia, a cada 15 s, o snapshot de cada slot como
`tipo="status"`. Como o slot ficou preso em `status="concluido"` + `os_id="OS-antiga"` (item 2),
`main._handle_evento_dispenser()` faz `d.update({... "status": ..., "os_id": ...})` e
**desfaz** o reset que o orquestrador aplicou no fim da OS (`status="idle"`, `os_id=None`).

**Impacto:**
- O dashboard mostra os 6 slots eternamente em “concluído”.
- `POST /manutencao/dispensers/{id}/limpar` responde **409** (`"concluido"` está em
  `STATUS_BLOQUEADOS`, `main.py:1132`) — a limpeza é bloqueada *duas vezes*, no central e no
  simulador.
- `dispenser_estado.ultima_os_id` no MySQL fica congelado na OS antiga.

**Correção:** resolver o item 2 na origem; e no central, tratar `tipo="status"` como fonte de
verdade só para estoque (quantidade/medicamento), não para `status` de fluxo nem `os_id` —
esses pertencem ao orquestrador.

---

### 4. Slot vira “zumbi” após OS abortada → caminho de parada total do sistema **[ANÁLISE]**

`orchestrator._abortar_os()` reseta só a memória do central. O estoque já carregado continua
fisicamente no simulador (ex.: 12 un. de um medicamento em D3) e o slot fica com `os_id`
preenchido.

Em seguida, `atribuir_slots()` só aceita um slot ocupado se o **medicamento for o mesmo**
(passo 1) ou se `quantidade == 0` (passo 2). Com 96 medicamentos no catálogo, a chance de uma
OS futura pedir exatamente aquele item é baixa → o slot fica inutilizável. E não dá para
liberá-lo manualmente por causa dos itens 2 e 3.

**Impacto:** cada OS abortada (timeout de carga, erro de CNC, falha de dispenser) queima um slot
de forma permanente. Depois de ~6 abortos, **toda** OS nova é rejeitada com `sem_slot` e o
sistema para. Como o `order-generator` roda indefinidamente e `PROB_ERRO_MECANICO` é 1% por
unidade, isso acontece sozinho em algumas horas.

**Correção:** em `_abortar_os`, emitir `cmd_limpar` (ou um novo `cmd_descarregar`) para os slots
atribuídos, e/ou permitir que `atribuir_slots` reaproveite um slot ocupado por medicamento
diferente disparando limpeza antes da carga.

---

### 5. `visao_leituras` e `peso_unitario_g` só existem no `init.sql` **[ANÁLISE]**

O schema está duplicado em dois lugares e eles **divergem**:

| Objeto | `mysql/init.sql` | `database._create_tables()` |
|---|---|---|
| tabela `visao_leituras` | ✅ | ❌ ausente |
| coluna `medicamentos.peso_unitario_g` | ✅ | ❌ ausente |

`init.sql` só roda na **primeira** criação do volume `mysql_data`. Num `docker compose down`
(sem `-v`) seguido de rebuild, ou apontando para um MySQL já existente, `_create_tables()` é
quem cria o schema — e aí:

- `_seed_medicamentos()` executa
  `SELECT COUNT(*) ... WHERE peso_unitario_g IS NULL` → erro 1054 (unknown column) →
  `init_db()` só captura `OperationalError` e repete 30×, então **o central-computer não sobe**.
- `salvar_leitura_visao()` / `get_historico_visao()` falham → aba “Visão” da IHM e
  `GET /api/v1/visao/historico` quebram.

**Correção:** eliminar a duplicação (uma única fonte de schema) ou, no mínimo, sincronizar
`_create_tables()` com o `init.sql` — incluindo `visao_leituras`, `peso_unitario_g` e o seed dos
6 registros de `dispenser_estado`.

---

## P1 — dados e estado incorretos

### 6. `ordens.status` nunca vira `em_andamento` → `/os/ativa` devolve a OS errada **[ANÁLISE]**

`atualizar_status_ordem()` só é chamado com `"concluida"` e `"erro"`. O status
`"em_andamento"` só existe na memória (`orchestrator.py:421`), nunca no banco.

`get_ordem_ativa()` faz `WHERE status IN ('aguardando','em_andamento') ORDER BY criado_em DESC
LIMIT 1` → com fila, retorna a OS **mais recente enfileirada**, não a que está executando.

**Correção:** persistir `em_andamento` no início de `_processar_os` e ordenar por `criado_em ASC`
(ou filtrar só por `em_andamento`).

### 7. O contador de alarmes só sobe **[ANÁLISE]**

`_estado["alarmes_ativos"]` é incrementado em 6 pontos de `main.py` e **nunca** decrementado —
`manut_resolver_alarme()` grava no banco mas não mexe no contador. Também não é inicializado a
partir do banco no startup (volta a 0 após restart mesmo com alarmes abertos).

**Correção:** derivar o número de `get_alarmes(resolvido=False)` (com cache curto) em vez de
manter contador manual.

### 8. OS duplicada é aceita e processada de novo **[ANÁLISE]**

`salvar_ordem()` usa `INSERT IGNORE` e retorna em silêncio quando `rowcount == 0`, mas
`receber_ordem()` enfileira do mesmo jeito e responde `{"aceita": true}`. O contrato em
`ANALISE_ARQUITETURAL.md` §4.1 prevê **409 `os_duplicada`**.

**Correção:** `salvar_ordem` retornar bool; `receber_ordem` responder 409 quando falso.

### 9. Download de relatório da IHM aponta para hostname interno do Docker **[ANÁLISE]**

`ihm_web/app.py:918` monta o `href` com `BACKEND_URL` = `http://central-computer:8000` —
DNS que só existe dentro da rede `apsen-net`. No navegador do operador o link não resolve.
Além disso o JWT vai na query string (fica em histórico do browser e em log de acesso).

**Correção:** usar uma URL pública configurável (`PUBLIC_BACKEND_URL`, ex.
`http://localhost:8000`) ou fazer o download via callback do próprio Dash com `dcc.Download`,
mantendo o token no header.

### 10. Triple Check: regra divergente + terceira fonte cega **[ANÁLISE]**

- **Regra:** o README diz que **qualquer** divergência trava; o código exige `n_div >= 2`
  (`orchestrator.py:793`). Com 1 divergência só registra alarme e a OS segue.
- **Fonte cega:** `weight-simulator._do_pesar()` sempre soma o peso **esperado** + ruído
  gaussiano — nunca a quantidade realmente dispensada. Se o dispenser perder 2 de 10 unidades
  por falha mecânica, a balança continua “vendo” 10. Na prática a fonte 3 só dispara por ruído
  (σ = 2 g contra ≥ 100 g esperados: praticamente nunca).
- Consequência combinada: hoje o Triple Check é, na melhor das hipóteses, um *double check* —
  e com o item 1 ativo, um *single check*.

**Correção:** o simulador precisa receber (ou inferir) a quantidade efetivamente dispensada.
O caminho mais simples é o orquestrador passar `quantidade_dispensada` (do evento do dispenser)
em vez de `quantidade_esperada` no `cmd_pesar`, mantendo `peso_esperado` calculado sobre o alvo.

---

## P2 — robustez, desempenho e operação

### 11. A fila cresce sem limite e sem backpressure **[ANÁLISE]**

`INTERVALO_OS=90 s`, mas uma OS de 6 slots leva ~90-140 s (carga + scans + 6 × [movimento +
dispensa + câmera + peso]) — e com o item 1 somam-se +15 s por slot. Com trava ativa, o
orquestrador (loop único) fica bloqueado **indefinidamente** e o `order-generator` continua
postando. A `asyncio.Queue` é ilimitada e as OS acumulam no banco com status `aguardando`.

**Correção:** limite de fila com 429/503 no `POST /api/v1/ordens`, ou o gerador consultando
`fila_tamanho` antes de enviar.

### 12. `GET /estado` e o snapshot inicial do WebSocket usam cópia rasa **[ANÁLISE]**

`main.py:781` (`return dict(_estado)`) e `main.py:1195` copiam só o primeiro nível sob o lock; os
dicionários aninhados (`dispensers`, `cnc`, `visao`, `peso`) continuam sendo os objetos vivos e
são serializados **fora** do lock. Um evento chegando nesse intervalo pode causar
`RuntimeError: dictionary changed size during iteration`.
`_broadcast_estado()` faz certo (`copy.deepcopy`) — basta usar o mesmo padrão nos outros dois.

### 13. I/O de banco síncrono dentro do event loop **[ANÁLISE]**

`orchestrator.py:431-432` (caminho “sem slot disponível”) chama `atualizar_status_ordem` e
`salvar_alarme` **sem** `asyncio.to_thread`, bloqueando o loop inteiro durante a query. Todos os
outros pontos do arquivo usam `to_thread` corretamente.

### 14. Volume de escrita e crescimento sem retenção **[ANÁLISE]**

`_handle_evento_cnc` grava em `cnc_eventos` **também para `tipo="movendo"`**, que a CNC emite a
cada `INTERVALO=0.5 s` durante todo o movimento — são centenas de linhas por OS, cada uma
disparando `deepcopy` do estado + broadcast JSON. Somando a telemetria (12 eventos a cada 15 s
do dispenser, 30 s da CNC, 60 s das câmeras e da balança), `cnc_eventos` e `leituras_sensores`
crescem indefinidamente. Não há política de retenção nem purge.

**Correção:** persistir só `posicionado`/`concluido`/`erro`; fazer *throttle* do broadcast de
`movendo`; e criar um job de expurgo (ex.: `DELETE ... WHERE ts < NOW() - INTERVAL 7 DAY`).

### 15. Sem healthcheck fora do MySQL **[ANÁLISE]**

Nenhum dos 12 serviços de aplicação define `healthcheck`, e os `depends_on` dos adapters não
usam `condition: service_healthy` — só ordem de start. Todos os serviços já expõem `/ping` e os
adapters expõem `/health`; é só ligar no compose.

### 16. Segurança **[ANÁLISE]**

- `SECRET_KEY` no `docker-compose.yml` é **exatamente** o `_DEFAULT_SECRET_KEY` do
  `config.py` — o próprio código emite o aviso no startup. Qualquer pessoa com acesso ao repo
  consegue forjar um JWT de admin. Gere uma chave e mantenha fora do compose.
- `_get_tecnico()` não revalida o usuário: um técnico desativado continua autenticado por até
  8 h (`TOKEN_EXP_HOURS`).
- `CORSMiddleware(allow_origins=["*"])` em todos os serviços, inclusive no central autenticado.
- Senhas seed reais em texto claro no compose e no README.

### 17. `verificar_senha` pode devolver 500 em vez de 401 **[ANÁLISE]**

`auth.verificar_senha` chama `bcrypt.checkpw` direto: hash malformado no banco levanta
`ValueError`, e senha acima de 72 bytes levanta erro no bcrypt ≥ 4. Como `login()` não trata,
o cliente recebe 500. Envolver em `try/except` e retornar `False`.

### 18. Janela de corrida ao ativar a trava **[ANÁLISE]**

No loop de SKU errado (`orchestrator.py:579-589`), `_estado["trava"]` é publicado (e vai para o
dashboard) **antes** de `_ativar_trava()` setar `_trava_ativa=True`. Se o admin clicar em
“Liberar” nesse intervalo, `liberar_trava()` retorna `False` → 409 “nenhuma trava ativa”, com a
UI já mostrando a trava. Inverta a ordem.

### 19. `asyncio.gather` sem `return_exceptions` no lookup de pesos **[ANÁLISE]**

`orchestrator.py:471-474`: uma falha de banco em `get_peso_medicamento` derruba a OS inteira
(cai no `except` genérico de `loop_orquestrador` e marca `erro`), quando o comportamento
esperado seria usar o fallback de 50 g e seguir.

### 20. Pontas soltas de manutenção **[ANÁLISE]**

- `Makefile` referencia serviços que não existem mais (`backend`, `sap_simulator`, `mosquitto`,
  `cnc_simulator`) — quase todos os targets de log estão quebrados, e `status` ainda sugere
  `mosquitto_sub`.
- `ihm_esp32/ihm_esp32.ino` ainda usa `PubSubClient` e tópicos `apsen/*` — incompatível com a
  arquitetura REST atual.
- `dashboard/app.py:26` e `ihm_web/app.py:27` têm default `BACKEND_URL="http://backend:8000"`
  (serviço inexistente); só funciona porque o compose sobrescreve.
- Imports mortos: `orchestrator` importa 5 funções de `database` que não usa; `main` importa 2;
  três simuladores importam `Optional` sem usar.
- `dashboard/app.py:380` faz uma requisição HTTP **dentro** do callback de render (fora do
  `_fetch`), somando uma 4ª chamada a cada 2 s por cliente conectado.
- `weight-adapter/main.py:receber_evento` não tem `return` (responde `null`); todos os outros
  adapters devolvem `{"ok": true, "encaminhado": ...}`.
- `vision-simulator` faz `from fastapi import HTTPException` dentro das funções de endpoint.
- `docker-compose.yml` ainda declara `version: "3.9"` (obsoleto no Compose v2).

---

## Ordem sugerida de correção

1. **Itens 1, 2, 3, 4** — são o mesmo tema (ciclo de vida do slot) e juntos representam a parada
   do sistema em produção contínua. Corrigir 2 e 3 destrava 4.
2. **Item 5** — evita o cenário “funciona na minha máquina, não sobe na sua”.
3. **Itens 10 e 6** — corrigem a validação e o que a interface mostra como verdade.
4. **Itens 16 e 9** — antes de qualquer demonstração fora da rede local.
5. O resto pode entrar como *hardening* incremental.

## O que não foi coberto

- Nada foi executado com os containers de pé: a auditoria é estática mais reprodução isolada de
  funções. Vale rodar `docker compose up` por algumas horas com `PROB_ERRO_MECANICO` elevado
  (ex.: 0.05) para ver os itens 4 e 11 se manifestarem em minutos.
- A versão local de `weight-simulator/simulator.py` (mais nova que o commit) não pôde ser lida.
- `ihm_esp32.ino` foi analisado apenas superficialmente — é código Arduino fora do fluxo REST.
