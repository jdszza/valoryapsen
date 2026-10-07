# Visão da mesa — detecção, bounding box e contagem

Módulo de visão computacional do Projeto APSEN. A câmera fica acima da mesa.
A detecção tem dois níveis com limiar independente, e o **modo** decide o que
o nível 1 procura:

```
modo "caixa"  (há bandeja no quadro)
  nível 1   o maior retângulo do quadro  =  fundo do recipiente
  nível 2   todo retângulo circunscrito dentro dele  =  um medicamento

modo "mesa"   (não há bandeja: embalagens soltas na bancada)
  nível 1   a área útil é a ROI configurada, escala medida com régua
  nível 2   todo retângulo dentro dela  =  um medicamento
```

Escolher o modo errado é o erro mais caro do arquivo de configuração: com
`"caixa"` e sem bandeja no quadro, o sistema elege uma das próprias embalagens
como referencial, vai procurar conteúdo dentro dela e conta **1**.

## Por que o fundo da caixa é o referencial

1. **Resolve a altura variável da câmera.** O fundo tem dimensão conhecida em
   mm. Retificar o quadro para esse retângulo fixa a escala px/mm sozinho, em
   qualquer altura, sem marcador auxiliar nenhum.
2. **Elimina falso positivo por construção.** O que está fora do fundo não
   entra na contagem — reflexo na bancada, mão do operador e borda da mesa
   deixam de existir para o nível 2. Isso não é um filtro que pode falhar, é
   uma consequência do recorte.
3. **Dá o enquadramento certo para o limiar do conteúdo.** O limiar do nível 2
   é calculado só sobre os pixels de dentro da caixa, onde a luz é mais
   uniforme que no quadro inteiro.

Os limiares são separados porque são problemas ópticos diferentes: o fundo é um
contorno grande e de alto contraste contra a bancada; o conteúdo são retângulos
pequenos, encostados, do mesmo material e cor do fundo. Um limiar só nunca
serve para os dois.

## O modo LOCK

Cada limiar tem um botão de lock. Ligado, o valor do slider é suspenso e o
sistema escolhe o limiar a cada leitura pelo critério de **estabilidade**:

> varre a faixa inteira de limiares, mede quanto tempo a contagem fica
> constante, e fica no meio do patamar mais largo.

A contagem certa vive num patamar largo — uma caixa de verdade sobrevive a
dezenas de níveis de limiar. Um reflexo de LED só vira item numa faixa estreita
e some no passo seguinte. Empate entre patamares vai para o de maior
preenchimento médio: brilho e sombra produzem manchas irregulares, embalagem
produz retângulo cheio.

O lock também troca de **método**, não só de valor. Medido nos testes: com luz
uniforme, a versão sem normalização de iluminação dá um patamar 3x mais largo;
com LED lateral, só a versão com normalização acerta a contagem. Nenhuma das
duas serve sempre, por isso a escolha é por leitura.

## Quando o sistema se recusa a contar

Três situações em que a resposta certa é calar, e não chutar:

| Situação | Como é detectada |
|---|---|
| Reflexo especular | mancha localizada muito mais clara que a mediana **e** pixels estourados. As duas condições juntas — imagem globalmente clara estoura pixel sem ter reflexo, e sombra forte cria contraste local sem estourar nada |
| Fora de foco | variância do laplaciano abaixo do mínimo |
| Contagem instável | o patamar de limiar é estreito demais: a contagem existe só naquele valor exato, ou seja, é coincidência e não medida |

Nesses casos a saída é `"quantidade": null`. **`null` não é zero.** Zero é caixa
vazia; `null` é "a imagem não permite afirmar". Quem consome precisa tratar os
dois de forma diferente — inventar um número aqui viraria divergência com o
peso e com o ciclo da CNC, ou pior, passaria batido.

---

## Instalação

> **Este projeto precisa do PRÓPRIO ambiente virtual.** Ele usa
> `opencv-contrib-python`, e a visão dos dispensers (`apsen_sistema/visao`) usa
> `opencv-python`. Os dois pacotes instalam o mesmo módulo `cv2` por cima um do
> outro: instalados juntos, o `import cv2` passa a resolver para uma mistura das
> duas árvores e falha de formas difíceis de diagnosticar. Um `.venv` por
> projeto resolve, e é por isso que a instalação abaixo é feita de dentro da
> pasta `visao_mesa`.

```bash
cd visao_mesa
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux

pip install -r requirements.txt
```

Se em algum momento aparecer erro estranho de `cv2` neste ambiente:

```bash
pip uninstall -y opencv-python opencv-python-headless opencv-contrib-python
pip install -r requirements.txt
```

## Ordem de execução

### 1. Achar a webcam

```bash
python camera_finder.py
```

Varre os índices, testa cada backend e mostra **se o driver aceitou desligar**
autofoco, autoexposição e white balance. Câmera em modo automático é a causa
número 1 de contagem instável numa célula robótica: o braço entra no quadro, a
câmera reage, e o limiar calibrado ontem não vale mais hoje.

Anote o índice sugerido em `config/mesa.json` → `camera.indice`.

```bash
python camera_finder.py --ver 1     # abre uma janela para confirmar qual é
```

### 2. Escolher o modo e dar a escala

**Há uma bandeja no quadro?** Então `"fundo": {"modo": "caixa"}` e meça o fundo
dela com trena:

```json
"fundo": { "modo": "caixa", "comprimento_mm": 300.0, "largura_mm": 200.0 }
```

**Não há bandeja** (embalagens soltas na bancada)? Então `"modo": "mesa"` e
meça a escala com régua — quantos pixels tem 100 mm, dividido por 100:

```json
"fundo": { "modo": "mesa", "escala_px_por_mm": 2.0, "roi": [0.0, 0.0, 1.0, 1.0] }
```

**Há caixa, mas o contorno dela não aparece** (papelão contra papelão, sem
contraste nenhum)? Então `"modo": "manual"`: abra o `calibrar.py`, aperte `e` e
arraste os quatro cantos com o mouse. É o caso mais comum na bancada real, e
resolve os dois problemas de uma vez — dá correção de perspectiva e escala em
mm sem exigir que o detector ache a borda. A marcação fica salva; enquanto a
câmera e a caixa não se mexerem, não precisa repetir.

Em qualquer um dos dois, essa medida é o que converte pixel em milímetro. Se
estiver errada, todas as coordenadas saem erradas na mesma proporção — e a CNC
obedece sem reclamar.

### 3. Calibrar, nesta ordem

```bash
python src/calibrar.py
```

**Etapa 1 — painel `1_FUNDO_da_caixa`.** Mexa em `limiar` até o contorno
dourado abraçar exatamente o fundo da caixa. Enquanto o nível 1 estiver errado,
o nível 2 está medindo dentro do lugar errado; não adianta mexer nele.

**Etapa 2 — painel `2_CONTEUDO_medicamentos`.** Com o fundo certo, mexa em
`limiar` até a contagem bater com o número real de medicamentos. Se duas caixas
encostadas virarem uma, confira `cortar_juncoes` e veja as notas. Se aparecer item fantasma
na borda, aumente `margem interna mm`.

**Salvar:** arraste o slider **`SALVAR TUDO (0->1)`** — ele é o primeiro de cada
aba e grava as três de uma vez em `config/mesa.json`, que é o arquivo que o
`main.py` lê (a tecla `s` faz o mesmo). Depois ligue os dois LOCK (teclas `1` e
`2`) e salve de novo — em produção é o lock que roda, porque não há operador ao
lado do slider.

**`NOTAS_CALIBRAGEM.md` documenta os 79 parâmetros**, um a um: o que cada um
faz, o que acontece ao aumentar e ao diminuir, e um receituário por sintoma
("duas caixas contam como uma", "item fantasma na borda", e assim por diante).

**Aba 3 — `3_CAMERA_luz_e_foco`.** Não muda o algoritmo: escreve direto nos
controles da webcam (exposição, ganho, brilho, contraste, foco, white balance).
A janela "Luz e foco" mostra brilho, estouro, hotspot e nitidez com o alvo de
cada um, e diz o que fazer em ordem. **Exposição primeiro, foco depois** — área
branca estourada não tem textura, então ela derruba a medida de nitidez e faz
uma imagem em foco parecer desfocada.

**`AUTO AJUSTE (0->1)`** faz essa varredura sozinho, na ordem certa: percorre as
treze exposições, escolhe a de melhor nota (brilho no alvo, penalizando pesado o
estouro, que é irreversível), e só então percorre o foco em duas passadas —
grossa de 24 em 24, fina de 6 em 6 em volta do vencedor. Leva alguns segundos, a
janela continua respondendo, e ao terminar os sliders mostram o que ele achou
para você conferir antes de salvar. ESC cancela no meio sem fechar a calibragem.

**Toda ação é um slider visível, não só uma tecla.** Isso não é conforto: o
HighGUI do Windows **não entrega tecla nenhuma** ao `waitKey` quando o foco está
numa janela de sliders — que é exatamente onde a mão fica durante a calibragem.
Se as teclas parecerem não responder, é isso: clique na janela de **vídeo**
primeiro, ou use o slider correspondente, que funciona sempre.

| ação | controle | tecla |
|---|---|---|
| salvar em `config/mesa.json` | `SALVAR TUDO` (nas três abas) | `s` |
| marcar o fundo com o mouse | `MARCAR FUNDO c MOUSE` (aba 1) | `e` |
| auto-ajuste de luz e foco | `AUTO AJUSTE` (aba 3) | `a` |
| LOCK do fundo / do conteúdo | `LOCK` (aba 1 / aba 2) | `1` / `2` |
| alternar vista | `VISTA 0-2` (aba 1) | `v` |
| congelar o quadro | `CONGELAR quadro` (aba 1) | espaço |
| recarregar a config do disco | `RECARREGAR do disco` (aba 1) | `r` |
| mostra/esconde o painel de luz | — | `l` |
| sair (no auto-ajuste, cancela) | — | `q` / ESC |

Os sliders são a fonte única da verdade: a tecla `v`, por exemplo, escreve no
slider `VISTA`, então os dois nunca discordam do que está na tela.

No editor do `e`: arraste os cantos, **ENTER** aplica e salva, `r` volta ao
retângulo padrão, **ESC** cancela. Ao lado da imagem aparece a **prévia
retificada** — é nela que se confere a marcação: se as embalagens saírem tortas
ali, ainda está errada.

### 4. Rodar

```bash
python src/main.py                # janela com bounding box e contagem
python src/main.py --json         # uma linha JSON por leitura estável
python src/main.py --uma-vez      # uma leitura, imprime JSON e sai
python src/main.py --sem-janela   # sem interface, para rodar como serviço
```

### 5. Conferir sem câmera

```bash
python tests/teste_pipeline.py
```

Roda o pipeline completo em cenas sintéticas com ground truth: três alturas de
câmera, luz fraca, luz forte, LED lateral e dois níveis de reflexo. O modo LOCK
precisa acertar os oito — inclusive recusar os dois casos de reflexo forte.
É teste de regressão: qualquer mudança que quebre a contagem aparece aqui.

---

## Layout da janela

Os dois painéis ficam **um em cima do outro**, em metades de altura iguais: em
cima o quadro da câmera com o quadrilátero do fundo, embaixo a vista retificada
com as bounding box. Lado a lado eles somavam quase 1920 px de largura, e em
tela de notebook o Windows cortava o painel da direita — a contagem aparecia, as
últimas caixas não.

A janela se dimensiona sozinha pela tela (`GetSystemMetrics` no Windows, com
1366×768 como reserva) e usa 90% da altura, descontando barra de título e barra
de tarefas. A **largura sai a que o conteúdo precisa**, não a tela inteira: duas
vistas 16:9 empilhadas ocupam ~615 px de largura, e esticar até 1366 só encheria
a janela de faixa preta dos dois lados sem aumentar nada — de quebra, sobra
espaço à direita para as abas de slider na calibragem.

Nada é esticado em nenhum ponto: cada painel é encaixado com letterbox. A vista
está em escala mm conhecida, e distorcer faria uma embalagem de 77×34 mm
*parecer* outra proporção na tela — o tipo de erro que se confere a olho e passa.

## Desempenho

Três estágios independentes, em `src/fluxo.py`:

```
thread de captura   →  guarda sempre o frame mais novo, descarta os atrasados
thread de visão     →  pega o mais novo, processa, publica o resultado
laço principal      →  desenha o frame AO VIVO com o último resultado conhecido
```

O acoplamento era o problema, não a velocidade do algoritmo. Num laço único o
desenho só acontece depois que a detecção termina, então qualquer pico de
processamento vira engasgo visível. Pior: `cap.read()` bloqueia esperando o
sensor, e o laço inteiro fica refém do relógio da câmera.

Medido com webcam de 30 FPS simulada e a cena mudando a cada segundo: vídeo a
**30 FPS** (a taxa da câmera), detecção a 85–260 Hz, atraso de 31–60 ms entre o
frame exibido e a caixa desenhada. Antes eram 1,4 FPS no manual e 0,08 FPS no
lock, com a janela travando a cada busca de limiar.

O que cada peça faz:

| Peça | Efeito |
|---|---|
| Threads separadas | o vídeo nunca espera a detecção |
| `ler_direto()` sem `grab()` extra | `grab()` + `read()` eram duas esperas de frame por leitura — teto de 15 FPS na captura |
| Gate de mudança | a mesa fica parada quase o tempo todo; cena idêntica não é reprocessada |
| Cache com assinatura de config | mexer no slider invalida; o resto reaproveita |
| Busca em dois estágios, meia resolução | ~20 detecções por método em vez de 46 |
| Pré-processamento fora do laço de limiar | a normalização de iluminação custa 16 ms e não depende do limiar |
| Extração por recorte da caixa envolvente | antes varria a imagem inteira uma vez por componente |

### O gate de mudança e o erro que ele quase causou

A primeira versão comparava a **diferença média** de uma miniatura 16×16. Medido:
tirar uma caixa de sete move a média de 0,37 (só ruído) para 0,91 — perto demais
do ruído do sensor. **A retirada passava despercebida e o sistema ficava preso
no número velho**, que é pior que gastar CPU à toa.

O critério passou a ser a **maior diferença de célula**, numa miniatura 32×32:
na mesma troca, o máximo vai de 1 para 57. Mudança real é local e intensa; ruído
é difuso e fraco. Há ainda um reprocessamento forçado a cada 3 s como rede de
segurança, e `tests/teste_pipeline.py` tem um teste específico para isso.

Ajustes em `config/mesa.json` → `desempenho` (a seção não precisa existir; os
padrões entram sozinhos):

| campo | padrão | efeito |
|---|---|---|
| `limiar_mudanca` | 12.0 | maior = gate segura mais frames; alto demais perde retirada de caixa |
| `intervalo_forcado_s` | 3.0 | teto de quanto tempo uma leitura pode ficar velha |
| `intervalo_auto_s` | 0.7 | frequência máxima da busca de limiar no lock |
| `escala_varredura` | 0.5 | 0.35 deixa a busca ~2× mais rápida |
| `passo_grosso` | 8 | 12 ou 16 acelera, mas pode perder um patamar estreito |

---

## Estrutura

```
visao_mesa/
├── requirements.txt
├── NOTAS_CALIBRAGEM.md       o que cada parâmetro faz, e o efeito de mexer
├── camera_finder.py          descobre índice e backend da webcam
├── config/mesa.json          todos os 79 parâmetros, escritos por inteiro
├── src/
│   ├── visao_mesa.py         config, qualidade, nível 1, nível 2, pipeline
│   ├── camera.py             captura com automatismos travados
│   ├── fluxo.py              threads: captura, processamento, desenho
│   ├── desenho.py            overlay: quadrilátero, bounding box, barra
│   ├── autoluz.py            varredura de exposição e foco, passo a passo
│   ├── editor_fundo.py       marcação dos 4 cantos com o mouse
│   ├── calibrar.py           sliders + lock, em três abas
│   └── main.py               execução ao vivo
├── tests/
│   └── teste_pipeline.py     cenas sintéticas com ground truth
└── dados/                    saídas, evidências
```

## Saída

```json
{
  "quantidade": 7,
  "confiavel": true,
  "patamar": 52,
  "fundo_mm": [300.0, 200.0],
  "limiar_fundo": 88,
  "limiar_conteudo": 132,
  "metodo_conteudo": "otsu_desloc",
  "itens": [
    {"indice": 1, "centro_x_mm": 55.1, "centro_y_mm": 45.0,
     "comprimento_mm": 80.2, "largura_mm": 45.1, "angulo_graus": 0.0,
     "area_mm2": 3617.0}
  ]
}
```

As coordenadas saem em **milímetros no referencial do fundo da caixa**. Para
virar coordenada de CNC basta somar a origem da caixa na mesa — uma soma, não
uma nova calibração.
