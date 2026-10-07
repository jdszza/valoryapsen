# Do protótipo ao produto — arquitetura e plano

Documento de decisões técnicas para o projeto com Apsen e Softtek. O que já está
implementado e medido, o que falta, e em que ordem atacar.

---

## 1. O salto conceitual

A versão anterior respondia a uma pergunta: *"o código que eu imprimi está no
dispenser certo?"*. É pouco — ela valida a **etiqueta**, não o **medicamento**.
Quem colar MED-001 na caixa errada engana o sistema inteiro.

A versão atual responde a três perguntas independentes:

| Pergunta | Evidência | Módulo |
|---|---|---|
| Que código está aqui? | QR + ArUco | `leitor_qr.py` |
| Que produto está aqui? | aparência da embalagem | `reconhecimento.py` |
| Quanto tem aqui? | altura da pilha | `contagem.py` |

E cruza as duas primeiras. É desse cruzamento que sai a capacidade nova:

```
código diz "Dipirona"  +  embalagem parece Paracetamol  →  DIVERGÊNCIA
```

Nenhum leitor de código detecta isso, por mais robusto que seja. É o caso da
etiqueta trocada na linha, do produto reembalado, do lote irregular. Sai de
graça, porque as duas evidências já estavam sendo calculadas para outra coisa.

---

## 2. O que está implementado e medido

### Reconhecimento visual (`reconhecimento.py`)

Casamento de características (ORB) com três travas em série: teste de razão de
Lowe **contra outros SKUs**, verificação geométrica por homografia RANSAC, e
conferência de pixel após alinhamento.

Medido em 32 casos de variação e 48 casos de embalagem não cadastrada:

| Métrica | Resultado |
|---|---|
| Identificações corretas | 20/32 |
| **Trocou um medicamento por outro** | **0** |
| **Aceitou embalagem não cadastrada** | **0/48** |

O ponto de operação foi escolhido de propósito: **prefere calar a errar**. Num
sistema de medicamento, "não reconheci" é um estado gerenciável; "reconheci
errado" é um incidente. Os 12 casos não reconhecidos são ângulo extremo e
degradação severa — exatamente onde o código (QR/ArUco) continua funcionando.

Três decisões que valem registro:

1. **Sem rede neural, de propósito.** Cadastrar um SKU custa 4–8 fotos e um
   minuto, feito pelo operador na própria bancada. Não há treino, GPU, dataset
   rotulado nem ciclo de retreino quando a arte da embalagem muda. Para um
   catálogo que muda sem aviso, isso vale mais que alguns pontos de acurácia.
2. **É auditável.** O sistema mostra quais pontos casaram e qual foto de
   referência usou. Um classificador neural responde "97% Dipirona" e ninguém
   sabe por quê. Em ambiente regulado, poder mostrar a evidência é requisito,
   não luxo.
3. **Casamento discriminativo.** A primeira versão dava 1 falso positivo a cada
   4 embalagens estranhas — porque caixas da mesma fábrica compartilham marca,
   fonte e tarja, e esses pontos casam com tudo. A correção foi comparar contra
   um índice global e só contar o ponto que é **exclusivo** de um produto. Foi o
   que levou o falso positivo a zero.

### Fusão (`fusao.py`)

Seis vereditos: `OK`, `ERRO_POSICAO`, `DIVERGENCIA`, `NAO_CADASTRADO`, `VAZIO`,
`INDETERMINADO`. Cada um com nível de confiança e a fonte que o gerou
(`codigo+visual`, `codigo`, `visual`).

Regra de ouro: **divergência entre canais nunca vira silêncio**. Se os dois
discordam, o sistema para e chama gente, em vez de escolher um deles.

Há uma política opcional (`--exigir-visual`) em que só código não basta para
liberar. É a alavanca para separar "linha de alta criticidade" de "conferência
de rotina" sem trocar de sistema.

### Contagem de estoque (`contagem.py`)

Mede a altura ocupada da coluna e divide pela altura de uma caixa, aprendida uma
única vez a partir de uma pilha de quantidade conhecida. Não tenta "detectar
caixas" — caixas encostadas não têm divisa visível.

Medido em 7 níveis (0 a 6 caixas): **erro zero**. A calibração exige uma foto da
prateleira **vazia**; sem ela o método cai para densidade de borda e a leitura
sai marcada com confiança ≤ 0,35 em vez de mentir.

### Trilha de auditoria (`eventos.py`)

Cada evento carrega o hash do anterior. Alterar ou remover qualquer evento
quebra a cadeia de todos os seguintes, e `verificar_integridade()` aponta o `id`
exato. Testado nos dois ataques: alteração de conteúdo e remoção de linha.

Isso não impede a adulteração — impede a adulteração **silenciosa**, que é o que
uma auditoria pergunta. Conversa com o princípio ALCOA+ de integridade de dados
que a indústria já aplica a registros eletrônicos.

Armazenamento em SQLite (um arquivo, zero infraestrutura) com esquema já
pensado para Postgres.

### API e painel (`api.py`)

Endpoints REST (`/api/estado`, `/api/eventos`, `/api/resumo`, `/api/integridade`,
`/api/saude`) e um dashboard de página única que atualiza sozinho.

Só biblioteca padrão, de propósito: numa esteira corporativa cada dependência
nova é uma conversa com segurança da informação. Os contratos foram desenhados
para a migração para FastAPI ser mecânica.

---

## 3. Arquitetura de execução

```
                    ┌─────────────── ESTAÇÃO (1 por bancada) ───────────────┐
   webcam  ──────►  │  captura → detecção de mudança por zona               │
                    │              ↓ (só quando a imagem muda)              │
                    │      ┌───────┴────────┬──────────────┐                │
                    │   leitor de       reconhecedor    contador            │
                    │    código          de embalagem   de estoque          │
                    │      └───────┬────────┴──────────────┘                │
                    │              ↓                                        │
                    │           FUSÃO  →  veredito + confiança + fonte      │
                    │              ↓                                        │
                    │        registro de eventos (hash encadeado)           │
                    │              ↓                                        │
                    │     alertas locais  +  API HTTP  +  painel            │
                    └───────────────────────────────────────────────────────┘
                                          ↓
                          (fase 3)  agregador central / ERP
```

**O gate de mudança é o que torna isso viável em PC comum.** Reconhecer
embalagem custa ~120 ms e medir pilha ~5 ms; rodar isso a 30 FPS seria
inviável. Como dispenser é estático, comparar uma miniatura 16×16 da zona custa
microssegundos e evita o trabalho pesado em quase todos os frames. Só quando
alguém mexe na pilha o processamento completo acontece — que é exatamente
quando ele importa.

---

## 4. O que falta para virar produto

Sendo honesto sobre a distância que ainda existe:

| Lacuna | Impacto | Esforço |
|---|---|---|
| Validação com fotos reais de embalagem Apsen | **alto** — todos os números acima são de embalagens sintéticas | baixo |
| Limiares recalibrados no dataset real | alto — os limiares atuais são um ponto de partida | baixo |
| Autenticação e perfis no painel | alto para produção | médio |
| Agregação multiestação (hoje cada bancada é uma ilha) | alto na fase 3 | médio |
| Integração com ERP/WMS da Apsen | alto para valor de negócio | médio |
| Empacotamento e atualização remota da estação | médio | médio |
| Protocolo de validação (IQ/OQ/PQ) | alto para uso regulado | alto |
| Redundância de câmera / detecção de câmera obstruída | médio | baixo |

Duas dessas merecem destaque:

**Validação com dados reais é o primeiro passo, não o último.** Tudo que foi
medido usa embalagens geradas em código. Elas exercitam a mecânica certa, mas
não substituem 200 fotos de caixa Apsen em condição de bancada. O simulador de
degradação (`tests/degradacao.py`) e o benchmark já existem — trocar a fonte das
imagens é uma tarde de trabalho, e é o que transforma "funciona no meu teste" em
"funciona no seu chão".

**Detecção de câmera obstruída.** Hoje, se alguém cobrir a lente, o sistema vê
zonas vazias e reporta `VAZIO` — silenciosamente. Um sistema de segurança que
falha em silêncio é pior que nenhum. A correção é barata (nitidez e brilho
global já são calculados pelo `diagnostico.py`; falta promovê-los a alarme) e
deve entrar antes de qualquer piloto real.

---

## 5. Plano por fases

### Fase 1 — Provar com dados reais (2 a 3 semanas)

- 200+ fotos de embalagens Apsen reais na bancada, em condições variadas
- Recalibrar limiares e publicar a matriz de confusão real
- Alarme de câmera obstruída / fora de foco
- Meta objetiva: **zero troca de medicamento** e falso positivo abaixo de 1%

Entregável: relatório de desempenho com números do chão, não do simulador.

### Fase 2 — Endurecer a estação (3 a 4 semanas)

- Autenticação e perfis (operador / supervisor / qualidade)
- Empacotamento como serviço (systemd ou container) com autoinício
- Alarme de estação offline e de fila de eventos parada
- Exportação assinada para auditoria
- Testes de longa duração (72 h contínuas) medindo deriva e vazamento de memória

### Fase 3 — Escalar para várias bancadas (4 a 6 semanas)

- Postgres central (Supabase serve bem) com as estações publicando eventos
- Painel consolidado por linha/setor
- Configuração e catálogo distribuídos do centro para as estações
- Cadastro visual feito uma vez e replicado para todas as bancadas
- Integração com ERP/WMS: o veredito vira evento no sistema de origem

### Fase 4 — Qualificar (paralelo à fase 3)

- Especificação de requisitos e matriz de rastreabilidade requisito → teste
- IQ/OQ/PQ com o time de qualidade da Apsen
- Gestão de mudança: como uma alteração de limiar é aprovada e registrada
- Plano de revalidação quando a embalagem muda

---

## 6. Escolhas que eu defenderia numa banca

**Por que não YOLO / deep learning?** Foi considerado e descartado para esta
fase, não por preconceito: exige dataset rotulado que ainda não existe, GPU ou
inferência lenta em CPU, retreino a cada mudança de arte, e resposta não
auditável. O caminho de upgrade está desenhado — trocar o extrator de
características por um embedding CNN mantém toda a arquitetura de fusão,
rejeição e auditoria intacta. Quando houver 5.000 fotos rotuladas, a troca é
localizada em um módulo.

**Por que SQLite e não Postgres desde já?** Uma bancada é um processo local. Ir
para Postgres agora adicionaria um ponto de falha de rede a um sistema que
precisa funcionar mesmo com a rede caída — e é justamente quando a rede cai que
não se pode parar de registrar. O desenho é: estação sempre grava local, e
publica para o central quando dá. A fase 3 adiciona a publicação, não substitui
o local.

**Por que a política de rejeição é tão conservadora?** Porque os dois erros não
custam a mesma coisa. Deixar de reconhecer gera uma conferência manual. Trocar
um medicamento por outro gera um desvio. O sistema foi ajustado para o erro
barato, e isso é uma decisão de produto, não uma limitação técnica — os limiares
estão em um lugar só e são revisáveis com o time de qualidade.

**Por que trilha encadeada em vez de log comum?** Porque a primeira pergunta de
uma auditoria sobre um alerta de seis meses atrás é "como você sabe que isso não
foi alterado?". Um arquivo de log não responde. Custou 40 linhas de código.

---

## 7. Como rodar o que já existe

```bash
python src/calibrar.py                       # zonas dos dispensers
python src/gerar_qrcodes.py --tamanho-mm 40  # etiquetas híbridas
python src/cadastrar_visual.py --sku MED-001 # aparência da embalagem
python src/calibrar_estoque.py               # prateleira vazia + altura da caixa
python src/estacao.py                        # tudo junto + painel em :8000

python tests/test_sistema.py                 # leitura de código
python tests/test_v2.py                      # reconhecimento, fusão, estoque, auditoria
python tests/benchmark_robustez.py           # robustez a câmera ruim
```
