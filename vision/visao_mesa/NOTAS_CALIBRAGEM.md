# Notas de calibragem — o que cada parâmetro faz

Referência de bancada para `config/mesa.json`. Todos os 79 campos estão
escritos por inteiro no arquivo, nenhum depende de valor padrão escondido no
código.

**Como ler cada entrada:** a linha `padrão` é o valor de fábrica; `↑` é o que
acontece ao aumentar e `↓` ao diminuir. Onde houver risco, ele está dito.

**Comece por `fundo.modo`.** Ele decide se existe um recipiente no quadro
(`"caixa"`), se as embalagens ficam soltas na bancada (`"mesa"`) ou se os
quatro cantos foram marcados com o mouse (`"manual"`). Escolher errado faz o
sistema eleger uma embalagem como referencial e contar 1.

**Se uma tecla parecer não responder, não é você.** O HighGUI do Windows não
entrega tecla nenhuma ao programa quando o foco está numa janela de sliders —
e é nela que a mão fica durante a calibragem. Por isso **toda ação também é um
slider visível**: clique na janela de vídeo para usar teclas, ou simplesmente
arraste o controle, que funciona sempre.

**Três coisas na tela poupam trabalho repetido, e nenhuma delas depende de
atalho de teclado — são controles visíveis nas abas:**

- **`SALVAR TUDO (0->1)`** — primeiro slider de *cada* aba. Arraste de 0 para 1
  e as três abas (mesa, medicamentos e luz) vão de uma vez para
  `config/mesa.json`, que é o arquivo que o `main.py` lê. Não existe salvar
  "só uma aba": o arquivo é um só.
- **`AUTO AJUSTE (0->1)`** — na aba 3. Varre a exposição e depois o foco e para
  no melhor ponto **medido**. Ao terminar, os sliders mostram o que ele achou;
  confira na imagem e salve. ESC cancela no meio sem fechar a calibragem.
- **`MARCAR FUNDO c MOUSE (0->1)`** — na aba 1 (tecla `e`). Abre o editor de
  mouse para marcar o fundo da caixa. Ao aplicar, `fundo.modo` vira `"manual"`
  e a marcação é gravada.

A aba 1 tem ainda `VISTA 0-2`, `CONGELAR quadro` e `RECARREGAR do disco`, que
são as mesmas ações das teclas `v`, espaço e `r`. O slider é a fonte única da
verdade: a tecla escreve nele, então os dois nunca discordam do que está na
tela. **As abas são redimensionáveis** — se algum slider não aparecer, arraste
a borda da janela para baixo; o HighGUI corta o que não cabe, e um slider
cortado é indistinguível de um slider que não existe.

**Estes campos não são ajuste, são medida** — errar neles desalinha tudo o que
vem depois, e o sistema não tem como perceber:

- `camera.indice` — qual webcam (descubra com `python camera_finder.py`)
- `fundo.comprimento_mm` e `fundo.largura_mm` — o fundo do recipiente, com
  trena (modo `"caixa"`)
- `fundo.escala_px_por_mm` — pixels por milímetro, com régua (modo `"mesa"`)

**Ordem de calibragem:** câmera → fundo → conteúdo. Enquanto o nível 1 (fundo)
estiver errado, o nível 2 está medindo dentro do lugar errado e mexer nele só
gasta tempo.

---

## `camera` — captura

### `indice` · padrão: 2
Qual webcam o OpenCV abre. Não é ajuste: rode `camera_finder.py` e use o que
ele indicar. Muda sozinho ao trocar o cabo de porta USB.

### `largura` · padrão: 1280 · `altura` · padrão: 720
Resolução pedida ao driver.
- **↑** mais detalhe no frame, menos FPS, mais CPU.
- **↓** mais FPS e menos CPU.

Cuidado com a intuição aqui: quem manda no detalhe da contagem é
`fundo.px_por_mm`, não esta resolução. Subir para 1080p só ajuda se a caixa
ocupa uma parte pequena do quadro. Se ela preenche o quadro, 640×480 costuma
dar o mesmo resultado com o dobro do FPS.

### `fps` · padrão: 30
Taxa pedida. O driver pode não obedecer — o `camera_finder.py` mostra a real.
- **↑** vídeo mais fluido, mais CPU na thread de captura.
- **↓** economiza CPU; abaixo de 10 a resposta a uma retirada fica visível.

### `fourcc` · padrão: `"MJPG"`
Formato que o driver entrega. `MJPG` é comprimido; `YUYV` é cru.
Trocar para `YUYV` derruba o FPS em 720p para a casa de 5–10. Só mexa se a
câmera não aceitar MJPG.

### `autofoco` · padrão: `false`
**Deixe em `false`.** Com autofoco ligado, o braço robótico entrando no quadro
faz a câmera caçar foco e a calibragem de ontem deixa de valer hoje.

### `foco` · padrão: 30
Posição do foco fixo (0 = infinito, 255 = macro).
- **↑** aproxima o plano focal da lente — use se a caixa está perto.
- **↓** afasta o plano focal.

Ajuste olhando o `camera_finder.py --ver <indice>` até o texto da embalagem
ficar legível. Fora de foco, o sistema se recusa a contar (`nitidez_minima`).

### `autoexposicao` · padrão: `false`
**Deixe em `false`.** É a causa número 1 de contagem instável em célula
robótica: a câmera reage ao braço no quadro e reescalona o brilho.

### `exposicao` · padrão: -6
Tempo de exposição. No Windows/DSHOW a escala é −13 a −1 e é **logarítmica**:
cada passo dobra ou divide o tempo.
- **↑** (−6 → −4) imagem mais clara, mais risco de estourar no reflexo e mais
  borrão se algo se mexer.
- **↓** (−6 → −8) imagem mais escura e mais "congelada", menos reflexo.

No Linux/V4L2 a escala é outra (3 a 2047) e o sentido se inverte: número maior
= mais claro.

### `autobalanco_branco` · padrão: `false`
**Deixe em `false`.** Ligado, a cor da imagem muda quando algo colorido entra
no quadro, e com ela mudam os limiares.

### `temperatura_cor` · padrão: 4000
Temperatura de cor assumida, em Kelvin.
- **↑** (4000 → 6500) compensa luz mais fria; a imagem puxa para o quente.
- **↓** (4000 → 3000) compensa luz mais quente; a imagem puxa para o azul.

Ajuste até um papel branco na bancada aparecer branco. Só importa de verdade
se você usar o método de segmentação por cor.

### `ganho` · padrão: -1 · `brilho` · padrão: -1 · `contraste` · padrão: -1
Controles extras da webcam. **-1 significa "não mexer"** e deixa o driver
decidir — nem toda câmera expõe os três.
- **↑** clareia (ganho e brilho) ou aumenta a separação entre claro e escuro
  (contraste).
- **↓** escurece ou achata.

Prefira resolver o brilho pela **exposição**, não pelo ganho: ganho alto
clareia amplificando o sinal do sensor, e amplifica o ruído junto.

### `espelhar` · padrão: `false`
Inverte a imagem na horizontal. **Cuidado:** espelhar inverte o eixo X das
coordenadas que vão para a CNC. Só ligue se a câmera estiver fisicamente
montada invertida, e reconfira a calibragem depois.

### `frames_aquecimento` · padrão: 20
Frames descartados ao abrir a câmera, enquanto o sensor estabiliza.
- **↑** arranque mais lento, primeira leitura mais confiável.
- **↓** arranque mais rápido; abaixo de 10, a primeira leitura pode sair com
  brilho errado.

---

## `fundo` — nível 1, o referencial da contagem

### `modo` · padrão: `"mesa"`
**É a primeira decisão, e ela muda o que todo o resto significa.**

- `"caixa"` — existe uma bandeja ou recipiente no quadro, e os medicamentos
  estão **dentro** dele. O maior retângulo é o fundo desse recipiente, que
  serve de referencial: corrige a perspectiva e dá a escala em mm de graça,
  porque as dimensões dele são conhecidas.
- `"mesa"` — **não existe recipiente.** As embalagens ficam soltas sobre a
  bancada. A área útil passa a ser a região fixa definida em `roi`, e a escala
  vem de `escala_px_por_mm`, medida com régua.
- `"manual"` — **você marcou os quatro cantos com o mouse** (tecla `e` no
  `calibrar.py`). É o melhor dos dois mundos quando o recipiente existe mas não
  tem contorno detectável — caixa de papelão com embalagem de papelão dentro,
  onde não há contraste nenhum entre o fundo e a borda. Dá correção de
  perspectiva e escala real em mm, que o modo `"mesa"` não dá, sem depender de
  contraste, que o modo `"caixa"` exige. É trabalho de uma vez: enquanto a
  câmera e a caixa não se mexerem, a marcação continua valendo.

Como saber qual usar: olhe a tela do `calibrar.py`. Se o contorno dourado
abraçar uma das **embalagens** em vez de um recipiente, o modo está errado —
o sistema elegeu um medicamento como referencial e vai procurar conteúdo
dentro dele, encontrando zero. Foi exatamente o que aconteceu na primeira
bancada: três caixas na mesa, contorno em cima de uma delas, contagem = 1.

O modo `"caixa"` é melhor **quando há caixa**: ele corrige perspectiva e
sobrevive a mudança de altura da câmera sem recalibrar. O modo `"mesa"` perde
as duas coisas — se a câmera subir ou descer, `escala_px_por_mm` precisa ser
medida de novo.

### `roi` · padrão: `[0.0, 0.0, 1.0, 1.0]`
Região útil no modo `"mesa"`, em fração do quadro: `[x, y, largura, altura]`.
O padrão é o quadro inteiro.
- **Reduzir** é a forma mais barata de tirar da conta o que está na borda da
  bancada e não interessa — cabo, ferramenta, borda da mesa.
- **Ampliar** aproveita mais área, e traz junto tudo o que estiver nela.

Exemplo: `[0.05, 0.10, 0.80, 0.85]` começa a 5% da esquerda e 10% do topo,
com 80% da largura e 85% da altura.

### `escala_px_por_mm` · padrão: 2.0
Escala do modo `"mesa"`. **É medida, não ajuste.** Ponha uma régua na bancada,
tire um print, conte quantos pixels tem 100 mm e divida por 100.
- **↑** o sistema passa a achar que tudo é menor do que é.
- **↓** passa a achar que tudo é maior.

Errar aqui não muda a contagem, mas deforma todas as medidas e coordenadas em
mm — e são elas que vão para a CNC. **Depende da altura da câmera:** se o
suporte subir ou descer, meça de novo.

### `cantos_manuais` · padrão: `[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]`
Os quatro cantos do modo `"manual"`, na ordem **TL, TR, BR, BL**, em **fração
do quadro** (0 a 1) e não em pixel. Normalizado de propósito: trocar a
resolução da câmera de 1280×720 para 1920×1080 não invalida a marcação feita na
bancada.

Não edite na mão — use a tecla `e` no `calibrar.py`, arraste os cantos e aperte
ENTER. O editor mostra, ao lado, a **prévia retificada**: é nela que se vê se a
marcação está certa. Se as embalagens saírem tortas ou esticadas ali, ainda
está errada, mesmo que na imagem original pareça alinhada.

Ao salvar, os pontos são reordenados para TL/TR/BR/BL automaticamente. Sem essa
reordenação, arrastar um canto por cima do outro espelharia a vista — e o erro
não apareceria na marcação, apareceria na contagem, depois.

Com o modo `"manual"`, a escala vem de `px_por_mm` e das dimensões reais
(`comprimento_mm` × `largura_mm`), então **meça a caixa com trena**: é ela que
define quantos milímetros cabem na vista.

### `metodo` · padrão: `"borda"`
*(daqui para baixo, só vale no modo `"caixa"`)*
- `"borda"` — detecta o contorno por gradiente (Canny). Funciona mesmo quando
  a caixa tem cor parecida com a bancada.
- `"limiar"` — separa por brilho. Mais barato, exige bancada contrastante.

### `limiar` · padrão: 90 — **este é o SLIDER 1**
No método `borda`, é o limiar superior do Canny (o inferior é a metade).
- **↑** só as bordas mais fortes sobrevivem; se subir demais, o contorno da
  caixa se parte e o fundo some.
- **↓** mais bordas, incluindo textura da bancada e sombra; o contorno pode
  vazar para fora da caixa e o retângulo sair grande demais.

Ajuste até o contorno dourado abraçar exatamente o fundo da caixa.

### `limiar_minimo` · padrão: 20 · `limiar_maximo` · padrão: 220
Faixa que o modo LOCK varre. Não afeta nada com o lock desligado.
- **Faixa mais estreita** → busca mais rápida, risco de excluir o valor certo.
- **Faixa mais larga** → busca mais lenta e mais chance de achar um patamar
  falso longe do ponto de operação.

### `lock` · padrão: `false`
`true` suspende o slider e deixa o sistema escolher o limiar a cada leitura.
**Ligue em produção** — não há operador ao lado do slider.

### `desfoque` · padrão: 5 (ímpar)
Borra antes de procurar bordas.
- **↑** menos ruído virando borda falsa; cantos ficam mais arredondados e o
  retângulo perde precisão.
- **↓** cantos mais exatos, mais ruído.

### `dilatacao` · padrão: 3
Engrossa as bordas para fechar um contorno partido.
- **↑** fecha vãos maiores; se exagerar, cola a caixa em objetos vizinhos e o
  retângulo sai maior que a caixa — **e aí toda a escala em mm sai errada**.
- **↓** contorno mais fiel, mais risco de não fechar e o fundo não ser achado.

### `epsilon_poligono` · padrão: 0.02
Tolerância para simplificar o contorno em 4 vértices, como fração do perímetro.
- **↑** (0.05) fecha em 4 cantos com mais facilidade, mas os cantos saem menos
  precisos e a retificação distorce.
- **↓** (0.01) cantos mais exatos; o contorno pode não reduzir a 4 vértices e
  cair no retângulo mínimo, que ignora perspectiva.

### `area_minima_frac` · padrão: 0.06 · `area_maxima_frac` · padrão: 0.98
Fração do quadro que o fundo pode ocupar.
- **`area_minima_frac` ↑** ignora retângulos pequenos — use quando o detector
  agarra um medicamento em vez da caixa.
- **`area_maxima_frac` ↓** ignora retângulos gigantes — use quando ele agarra a
  bancada inteira ou a borda do quadro.

### `comprimento_mm` · padrão: 300.0 · `largura_mm` · padrão: 200.0
**Medida, não ajuste.** O fundo da caixa, com trena. São esses dois números que
convertem pixel em milímetro. Errar 10% aqui faz **todas** as coordenadas
saírem 10% erradas — e a CNC obedece sem reclamar.

Meça o retângulo que aparece contornado de dourado na tela. Se o contorno pega
a parede externa da caixa, meça a parede externa.

### `px_por_mm` · padrão: 2.0
Resolução da vista retificada. 2 px/mm numa caixa de 300×200 mm dá 600×400 px.
- **↑** (3.0) mais detalhe para medicamento pequeno; o custo de CPU cresce ao
  **quadrado** — 3.0 custa 2,25× mais que 2.0.
- **↓** (1.5) bem mais rápido; itens menores que ~15 mm começam a se perder.

### `suavizacao` · padrão: 0.7
Média dos cantos entre frames (0 = sem suavização, 0.9 = muito lenta).
- **↑** vista retificada mais firme, sem tremer.
- **↓** acompanha mais rápido se a caixa for movida; a imagem treme 1–2 mm.

Com a caixa fixa na bancada, valores altos são só ganho.

### `frames_validade` · padrão: 60
Por quantos frames a última homografia válida é reaproveitada quando o fundo
não é encontrado (braço robótico cobrindo a caixa, por exemplo).
- **↑** atravessa oclusões mais longas; se a caixa for movida durante a
  oclusão, o sistema mede no lugar errado por mais tempo.
- **↓** desiste mais rápido e mostra "fundo não encontrado".

A 30 FPS, 60 frames ≈ 2 segundos.

---

## `conteudo` — nível 2, os retângulos dentro do fundo

### `metodo` · padrão: `"otsu_desloc"`
- `"otsu_desloc"` — o Otsu acha o ponto de equilíbrio da cena e o slider
  desloca em torno dele. **O valor do slider continua valendo quando a luz da
  bancada muda** — por isso é o padrão.
- `"adaptativo"` — limiar calculado por vizinhança. Melhor com luz muito
  irregular, mais sensível a ruído.
- `"fixo"` — limiar absoluto. Só com iluminação rigorosamente controlada.

### `limiar` · padrão: 128 — **este é o SLIDER 2**
Em `otsu_desloc`, **128 = Otsu puro**; o valor é um deslocamento em torno dele.
- **↑** (acima de 128) limiar mais alto, menos pixels viram objeto: os itens
  encolhem, os mais escuros somem, a contagem cai.
- **↓** (abaixo de 128) mais pixels viram objeto: os itens incham, encostam uns
  nos outros e se fundem, a contagem cai por outro motivo.

A contagem certa fica num **patamar largo** no meio — se ela muda a cada passo
do slider, o problema é de iluminação, não de limiar.

### `limiar_minimo` · padrão: 40 · `limiar_maximo` · padrão: 220
Faixa varrida pelo LOCK. Mesma lógica do `fundo`.

### `lock` · padrão: `false`
`true` suspende o slider e o método, e deixa o sistema escolher os dois a cada
leitura. **Ligue em produção.**

### `inverter` · padrão: `false`
Inverte a máscara. Ligue quando os medicamentos são **mais escuros** que o
fundo da caixa. Sintoma de estar errado: o detector marca o fundo vazio como se
fosse um item gigante.

### `metodos_auto` · padrão: os quatro
Lista que o LOCK experimenta. O sufixo `+norm` liga a normalização de
iluminação naquele candidato.
- **Tirar métodos** → busca mais rápida, menos capacidade de se adaptar.
- Medido: com luz uniforme o método sem `+norm` dá um patamar 3× mais largo;
  com LED lateral, **só** o `+norm` acerta a contagem. Nenhum serve sempre —
  por isso os quatro.

### `normalizar_iluminacao` · padrão: `false`
Só vale no modo **manual** (com lock, o sufixo do método decide). Divide a
imagem por uma versão muito borrada dela mesma, removendo o gradiente de luz.
- **Ligar** → resolve brilho desigual e LED lateral.
- **Desligar** → patamar mais largo quando a luz já é uniforme.

### `sigma_iluminacao_mm` · padrão: 25.0
Raio, em mm, da estimativa de iluminação.
- **↑** (40) só gradientes muito largos contam como luz; corrige menos.
- **↓** (10) corrige mais, mas começa a apagar os próprios medicamentos, porque
  eles passam a ser vistos como "iluminação".

Regra prática: mantenha bem **maior** que o maior medicamento.

### `desfoque` · padrão: 3 (ímpar)
- **↑** menos ruído, bordas menos definidas entre caixas encostadas.
- **↓** bordas mais nítidas, mais ruído virando item fantasma.

### `adaptativo_bloco` · padrão: 51 (ímpar)
Tamanho da vizinhança do método adaptativo. Só vale nesse método.
- **↑** comportamento mais parecido com limiar global.
- **↓** mais local; medicamento grande começa a ser detectado só pelas bordas.

### `abertura_mm` · padrão: 1.0
Remove manchas menores que isso.
- **↑** mata poeira, risco e ruído; começa a corroer medicamentos pequenos.
- **↓** preserva detalhe, deixa passar ruído.

### `fechamento_mm` · padrão: 1.0
Une bordas partidas do mesmo objeto.
- **↑** ⚠️ **o ajuste mais perigoso do arquivo.** Acima de ~2 mm ele solda o vão
  entre duas caixas vizinhas e **duas viram uma na contagem**. Foi medido: a 2,0
  mm a contagem caiu de 7 para 6.
- **↓** preserva a separação entre caixas encostadas.

Quem tapa buraco de texto impresso é `preencher_buracos`, não este campo.

### `preencher_buracos` · padrão: `true`
Preenche vazios internos pelo contorno externo — o texto e o logo impressos na
embalagem deixam de rachar a caixa em vários pedaços.
- **Desligar** só se os medicamentos forem genuinamente vazados (anel, blister
  recortado) e você quiser medir o vazio.

### `margem_interna_mm` · padrão: 6.0
Faixa interna ignorada a partir da borda do fundo.
- **↑** mata o fantasma que a parede e a sombra da caixa criam colados na
  borda; medicamento encostado na parede deixa de ser contado.
- **↓** aproveita a caixa toda; volta o risco do fantasma de parede.

Regra: um pouco maior que a espessura visível da parede.

### `area_min_mm2` · padrão: 400.0 · `area_max_mm2` · padrão: 20000.0
Área aceita para um medicamento, em mm².
- **`area_min_mm2` ↑** corta ruído e fragmento; corta também o menor
  medicamento se passar do tamanho dele.
- **`area_max_mm2` ↓** corta blob gigante (duas caixas fundidas, fundo inteiro);
  corta também a maior embalagem se apertar demais.

Referência: 80 × 45 mm = 3600 mm². Deixe folga de ~30% para cada lado.

### `lado_min_mm` · padrão: 12.0 · `lado_max_mm` · padrão: 200.0
`lado_min_mm` é o menor **lado curto** aceito; `lado_max_mm` o maior **lado
longo**. Filtram o que a área sozinha deixa passar — uma tira fina de sombra
pode ter a área de uma caixa.

### `razao_aspecto_max` · padrão: 6.0
Maior razão comprimento/largura aceita.
- **↑** aceita embalagem mais alongada (bisnaga, blister comprido).
- **↓** rejeita tira de sombra e risco na bancada. Para caixas comuns,
  3.0 já filtra bem.

### `preenchimento_min` · padrão: 0.72
Quanto do retângulo envolvente o objeto precisa preencher (0 a 1). É o que
separa uma embalagem (retângulo cheio) de um reflexo em L ou uma sombra
irregular, que têm a mesma área mas não preenchem.
- **↑** (0.85) só aceita formas bem retangulares; medicamento parcialmente
  oculto começa a ser rejeitado.
- **↓** (0.55) aceita forma irregular; volta o falso positivo de sombra.

### `separar_encostadas` · padrão: `true`
Liga o watershed que corta caixas coladas.
- **Desligar** → mais rápido, e duas caixas encostadas contam como uma.

### `limiar_distancia` · padrão: 0.45
Onde cortar, na transformada de distância (0 a 1).
- **↑** (0.6) separa com mais agressividade; **risco de partir UMA caixa em
  duas** e contar a mais.
- **↓** (0.3) separa menos; duas caixas encostadas voltam a contar como uma.

Sintoma de estar alto demais: a contagem sobe quando você junta as caixas.

### `cortar_juncoes` · padrão: `true`
Corta a máscara ao longo do **vinco** entre embalagens encostadas.

É o estágio que resolve o caso que o watershed não resolve. Quatro caixas
iguais coladas formam um retângulo perfeito: a transformada de distância vira
uma crista contínua, sem um pico por caixa, e nada é separado. O que sobra de
sinal é a linha reta entre as embalagens, que existe mesmo sem vão, porque a
borda do papelão faz sombra.

Medido: sem este estágio, 4 caixas encostadas sem vão contavam como **1**.

- **Desligar** só se a arte da embalagem tiver tarjas retas longas que estejam
  causando corte indevido — e antes disso tente `juncao_comprimento_min_mm` ↑.

### `juncao_comprimento_min_mm` · padrão: 15.0
Comprimento mínimo da linha reta para ela contar como vinco. **É o que separa
vinco de texto impresso:** a divisa entre duas caixas atravessa a embalagem
inteira; uma letra tem alguns milímetros.
- **↑** (25) mais seletivo; embalagem estreita pode deixar de ser separada.
- **↓** (8) separa embalagem pequena; texto e código de barras começam a virar
  linha de corte.

Regra: um pouco menor que o **lado curto** da sua embalagem.

### `juncao_espessura_mm` · padrão: 0.8
Largura do corte.
- **↑** separa com mais garantia; **come essa medida da embalagem** de cada
  lado do corte — o comprimento medido encolhe e vai assim para a CNC.
- **↓** medida mais fiel, risco de o corte não separar.

Não precisa engrossar para corte na diagonal: a labelização passa a usar
conectividade 4 depois de um corte, e é isso que enxerga os dois lados como
manchas distintas. Medido: com conectividade 8, duas caixas a 45° só se
separavam com 1,5 mm de espessura — o que custaria 1,5 mm da medida.

### `juncao_margem_mm` · padrão: 1.5
Faixa junto ao contorno externo da mancha onde as bordas são ignoradas, para o
próprio contorno da embalagem não virar linha de corte.
- **↑** mais seguro contra corte na borda; vinco perto da beirada é perdido.
- **↓** aproveita vinco mais próximo da borda, mais risco de corte inútil.

### `juncao_sensibilidade` · padrão: 40
Limiar do Canny que procura o vinco (o superior é 3×).
- **↑** (80) só vincos bem marcados; embalagem clara sobre fundo claro deixa de
  ser separada.
- **↓** (20) acha vinco fraco, e também textura e ruído.

### `juncao_uniformidade` · padrão: 0.6
Fração da mediana que o menor pedaço pode ter para o corte ser aceito.

Os pedaços de um corte bom são **parecidos entre si** — uma bandeja guarda o
mesmo SKU, e separar duas embalagens iguais devolve duas metades iguais. Um
corte ruim, sobre uma tarja impressa, devolve um pedaço grande e um caco.
- **↑** (0.8) mais exigente; corte de embalagens de tamanhos diferentes passa a
  ser rejeitado.
- **↓** (0.3) aceita corte desigual — foi assim que um caco de 30×18 mm passou
  como se fosse uma caixa, subindo a contagem de 4 para 5.

Só mexa para baixo se a bandeja tiver embalagens de tamanhos bem diferentes.

---

## Aba 3 do `calibrar.py` — luz e foco

Os sliders da aba 3 escrevem direto nos controles da webcam, não no algoritmo.
O painel "Luz e foco" mostra quatro medidas com o alvo de cada uma:

| medida | alvo | o que é |
|---|---|---|
| brilho médio | 85 – 155 | média do quadro (0–255) |
| estourado % | < 5 | fração de pixels ≥ 250, sem textura nenhuma |
| hotspot | < 0,24 | o quanto o ponto mais claro se destaca da mediana |
| nitidez | > 1,5 | laplaciano relativo ao contraste |

**A ordem de ajuste não é negociável: primeiro a exposição, depois o foco.**
Área branca estourada não tem textura, então a medida de nitidez cai junto — e
o sintoma de "desfocado" costuma sumir sozinho quando o estouro sai. Mexer no
foco antes é ajustar contra um sintoma que não era o problema.

Medido numa foto real da bancada: a 20,5% de estouro o sistema recusava por
reflexo; baixando a exposição de −6 para −7 o estouro foi a zero e o brilho
médio caiu de 159 para 111. A nitidez, porém, subiu só de 0,76 para 0,80 — ou
seja, ali havia **os dois** problemas, e a luz sozinha não resolveria.

---

## `auto` — quando o sistema se recusa a contar

Estes campos não mudam o número; mudam **quando o sistema admite não saber**.
A saída vira `"quantidade": null`, que não é zero — zero é caixa vazia, `null` é
"a imagem não permite afirmar".

### `patamar_minimo` · padrão: 12
Largura mínima, em unidades de limiar, da faixa em que a contagem não muda.
- **↑** mais exigente: só aceita leitura que sobrevive a uma faixa larga.
  Mais recusas, menos chance de erro.
- **↓** aceita leitura que existe só num valor exato de limiar — ou seja,
  coincidência em vez de medida.

### `hotspot_critico` · padrão: 0.24 · `saturacao_critica` · padrão: 0.10
Detectam reflexo especular. **As duas condições precisam ocorrer juntas**, e
isso é proposital: imagem globalmente clara estoura pixel sem ter reflexo
nenhum, e sombra forte cria contraste local sem estourar nada.
- **↑** tolera mais brilho — o sistema conta em situações onde a borda da
  embalagem já está apagada pela luz. **Aumente com muito critério.**
- **↓** recusa mais cedo.

### `cobertura_minima` · padrão: 0.60
Fração da máscara que os itens aceitos precisam explicar. Sobrou muito branco
dentro da caixa que não virou medicamento? Então há algo ali que o detector não
entendeu.
- **↑** (0.75) mais exigente.
- **↓** (0.40) aceita leitura com área não explicada.

Medido: leitura correta fica acima de 0,80; leitura errada por reflexo cai para
~0,39. O padrão de 0,60 está no meio com folga dos dois lados.

### `nitidez_minima` · padrão: 0.15
Nitidez mínima. A medida é a variância do laplaciano **dividida pela variância
da própria imagem** — ou seja, nitidez relativa ao contraste, não absoluta.

A medida é feita no **recorte do quadro original**, dentro do quadrilátero do
fundo, e não na vista retificada. Foco é propriedade da captura: a vista é uma
reamostragem, e se a homografia amplia (quadrilátero pequeno no quadro,
`px_por_mm` alto) a interpolação borra e a nitidez cai mesmo com a lente
perfeita — se reduz, sobe, e uma bancada realmente desfocada passaria. Medindo
na captura, o veredito de foco não depende mais de `px_por_mm`, que é número de
escrituração e não de ótica.
- **↑** (0.5) exige imagem mais nítida, recusa mais.
- **↓** (0.05) aceita imagem borrada — e imagem borrada funde caixas vizinhas.

**O valor veio de medir o ponto de falha, não de estimativa.** Desfocando uma
cena de 4 embalagens encostadas até elas fundirem numa só:

| desfoque | nitidez | contagem |
|---|---|---|
| nenhum | 3,45 | 4 |
| leve | 0,24 | 4 |
| médio | 0,05 | 4 |
| forte | 0,02 | **1 — fundiu** |

A separação aguenta até 0,05. O padrão de 0,15 fica 3× acima disso: recusa o
desfoque que realmente funde embalagens e deixa passar o resto.

O valor anterior era 1,0, escolhido no olho, e recusava leituras que o detector
acertava com cobertura 1,00 — ou seja, o guarda estava barrando medida boa.

A versão anterior usava a variância crua e confundia **escuro** com
**desfocado**: medido na bancada real, escurecer a imagem para 40% derrubava a
medida de 65 para 12 e o sistema recusava por "fora de foco" uma imagem
perfeitamente nítida. Com a relativa, a mesma variação fica entre 3,2 e 3,6,
enquanto desfoque de verdade leva para 0,06–0,16 — separação de 20×.

Valores de referência da bancada: **~3,2 em foco**, **abaixo de 0,2 borrado**.

---

## `desempenho` — só custo de CPU

Nada aqui muda o resultado; muda quanto se paga por ele. A exceção é
`limiar_mudanca`, que **pode** mudar o resultado se for mal ajustado — está
explicada abaixo.

### `escala_varredura` · padrão: 0.5
Fração da resolução usada na busca de limiar do LOCK. A escala px/mm é reduzida
junto, então os filtros em mm continuam valendo.
- **↑** (1.0) busca mais precisa, 4× mais cara.
- **↓** (0.35) busca ~2× mais rápida; abaixo disso, item pequeno some durante a
  busca e o LOCK escolhe errado.

### `passo_grosso` · padrão: 8
Granularidade da primeira passada da busca.
- **↑** (16) busca mais rápida. ⚠️ Medido: com 16, o caso de LED lateral erra —
  o patamar vencedor tinha 16 unidades e caía entre duas amostras.
- **↓** (4) mais seguro, ~2× mais lento.

### `passo_fino` · padrão: 4
Granularidade do refino em volta do vencedor.
- **↑** refino mais rápido e mais grosseiro.
- **↓** limiar final mais preciso, refino mais lento.

### `raio_refino` · padrão: 20
Quantas unidades de limiar para cada lado o refino examina.
- **↑** refino mais abrangente e mais lento.
- **↓** mais rápido; pode não alcançar a borda real do patamar.

### `max_componentes` · padrão: 250
Acima disso a leitura é abandonada sem gastar CPU — um limiar que gera 250
manchas já está perdido.
- **↑** insiste mais; um limiar ruim passa a custar caro.
- **↓** abandona mais cedo; se os medicamentos forem muitos e pequenos, pode
  abandonar uma cena legítima.

### `gate_mudanca` · padrão: `true`
Não reprocessa cena idêntica. É o que leva o custo de 47 ms para 1,4 ms quando
a mesa está parada.
- **Desligar** só para depurar.

### `lado_miniatura` · padrão: 32
Lado da miniatura comparada entre frames.
- **↑** (48) detecta mudança menor, custa um pouco mais.
- **↓** (16) mais barato; mudança pequena começa a passar despercebida.

### `limiar_mudanca` · padrão: 12.0
Maior diferença de célula que ainda conta como "cena parada".

⚠️ **O único campo desta seção que pode causar erro de contagem.** Um gate
frouxo demais deixa o sistema preso num número velho — e um sistema de contagem
que fica preso não avisa ninguém, só concorda com o que já achava.

- **↑** (25) segura mais frames, economiza CPU, **risco de não perceber uma
  caixa sendo retirada**.
- **↓** (5) reprocessa mais, gasta mais CPU, nenhum risco de perder mudança.

Medido: tirar uma caixa de sete leva a maior diferença de célula de 1 (só
ruído) para 57. O padrão de 12 fica bem no meio dessa separação. `tests/
teste_pipeline.py` tem um teste específico para isso.

### `intervalo_forcado_s` · padrão: 3.0
Reprocessa de tempos em tempos mesmo sem mudança detectada — rede de segurança
para o caso de o gate errar.
- **↑** (10) economiza CPU; uma leitura pode ficar velha por mais tempo.
- **↓** (1) mais seguro, mais CPU.

### `intervalo_auto_s` · padrão: 0.7
Com que frequência, no máximo, o LOCK refaz a busca de limiar.
- **↑** (2.0) menos CPU; demora mais para se adaptar a uma mudança de luz.
- **↓** (0.3) adapta rápido, custa CPU.

O cache cai na hora quando você mexe num slider, independente deste valor.

---

## Caixa de papelão como recipiente

Papelão contra papelão não dá contorno: o fundo e as paredes têm a mesma cor, e
o modo `"caixa"` não acha um retângulo confiável ali — ele acaba agarrando uma
faixa qualquer. Por isso a bancada com caixa de papelão está configurada em
modo `"mesa"`, com a área útil valendo o quadro inteiro.

**Para poder usar o modo `"caixa"`** (que corrige perspectiva e dispensa medir a
escala de novo quando a câmera muda de altura), forre o fundo da caixa com uma
folha de cor contrastante e de dimensão conhecida — uma cartolina preta ou azul
serve. Aí o nível 1 passa a ter um retângulo de verdade para achar, e você só
precisa pôr as medidas da folha em `comprimento_mm` e `largura_mm`.

---

## Receituário por sintoma

| Sintoma | Onde mexer |
|---|---|
| Contorno dourado abraça uma embalagem em vez do recipiente | `fundo.modo` = `"mesa"` — não há recipiente no quadro |
| Três caixas na mesa e a contagem dá 1 | mesmo caso acima: `fundo.modo` = `"mesa"` |
| Contorno dourado pega a mesa, não a caixa | `fundo.area_minima_frac` ↑ |
| Contorno dourado não aparece | `fundo.limiar` ↓, `fundo.dilatacao` ↑, ou método `"limiar"` |
| Contorno não aparece de jeito nenhum: caixa de papelão, embalagem de papelão | `fundo.modo` = `"manual"` — tecla `e` e marque os 4 cantos com o mouse |
| Vista retificada sai espelhada ou de cabeça para baixo | refaça a marcação com `e` (ela reordena TL/TR/BR/BL ao aplicar) |
| Salvei na aba 3 e a aba 1 voltou ao antigo | não acontece: o `SALVAR TUDO` de qualquer aba grava as três |
| Teclas não respondem (`1`, `2`, `e`, `s`…) | o foco está numa aba de sliders; clique na janela de **vídeo**, ou use o slider equivalente |
| Painel da direita cortado / janela maior que a tela | resolvido: os painéis agora ficam em cima e embaixo, e a janela se dimensiona pela tela |
| Janela de vídeo pequena demais | arraste a borda: ela mantém a proporção (`WINDOW_KEEPRATIO`) e não achata a imagem |
| Não encontro o slider LOCK (ou qualquer outro) | arraste a borda da aba para baixo: o HighGUI corta o que não cabe na janela |
| Contorno treme | `fundo.suavizacao` ↑ |
| Contorno some quando o braço passa | `fundo.frames_validade` ↑ |
| Duas caixas encostadas contam como uma | `conteudo.cortar_juncoes` ligado, `juncao_sensibilidade` ↓, `juncao_comprimento_min_mm` ↓, `fechamento_mm` ↓ |
| Caixas coladas **sem vão** contam como uma só grande | é o `cortar_juncoes` que resolve — confira se está `true` |
| Uma caixa conta como duas | `conteudo.limiar_distancia` ↓, `juncao_comprimento_min_mm` ↑ |
| Corte picando embalagem com tarja impressa | `juncao_comprimento_min_mm` ↑, `juncao_uniformidade` ↑ |
| Item fantasma colado na borda | `conteudo.margem_interna_mm` ↑ |
| Sombra/risco virando item | `conteudo.preenchimento_min` ↑, `razao_aspecto_max` ↓ |
| Medicamento pequeno não é contado | `conteudo.area_min_mm2` ↓, `lado_min_mm` ↓, `fundo.px_por_mm` ↑ |
| Contagem oscila frame a frame | é iluminação, não limiar — ligue o LOCK e melhore a luz |
| Fundo vazio vira um item gigante | `conteudo.inverter` |
| Sistema recusa por reflexo o tempo todo | mude o ângulo da luz ou do LED; só depois pense em `hotspot_critico` |
| Sistema recusa por "contagem instável" | `conteudo.limiar` para o meio do patamar, ou ligue o LOCK |
| Sistema recusa por cobertura | há blob não explicado — quase sempre duas caixas fundidas pelo brilho |
| Contagem não muda quando tiro uma caixa | `desempenho.limiar_mudanca` ↓ |
| Vídeo travando | `desempenho.escala_varredura` ↓, `passo_grosso` ↑, `camera.largura/altura` ↓ |
| Fora de foco | aba 3: `AUTO AJUSTE` — ou na mão, resolvendo o estouro primeiro e só depois o `foco` |
| Não sei em que valor deixar exposição/foco | aba 3: `AUTO AJUSTE`, confira na imagem e salve |
| Imagem branca chapada, sem borda nas embalagens | aba 3: `exposicao` mais negativa (ou `AUTO AJUSTE`, que penaliza estouro) |
| Recusa "fora de foco" com a imagem nítida na tela | confira se `auto.nitidez_minima` está em 1.0 e não no valor antigo 25.0 |

---

## O que não mexer sem medir

1. **`fundo.comprimento_mm` e `largura_mm`** — são medida de trena. Errado aqui
   significa toda coordenada errada na mesma proporção, sem nenhum sintoma
   visível na tela.
2. **`camera.autofoco`, `autoexposicao`, `autobalanco_branco`** — os três em
   `false`. Qualquer um ligado faz a calibragem expirar sozinha.
3. **`conteudo.fechamento_mm` acima de 2 mm** — solda caixas vizinhas.
4. **`auto.hotspot_critico` e `saturacao_critica`** — afrouxar faz o sistema
   contar onde a imagem não permite. A recusa é uma funcionalidade, não um
   defeito.
5. **`desempenho.limiar_mudanca` acima de ~20** — o sistema para de perceber
   retirada de caixa.

Depois de qualquer mudança nesses, rode `python tests/teste_pipeline.py`. Ele
não usa câmera e pega regressão em minutos.
