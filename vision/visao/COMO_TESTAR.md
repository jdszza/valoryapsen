# Como testar, do zero até o sistema completo

Quatro etapas. Cada uma funciona sozinha — se parar na 1, você já sabe se a
câmera e as etiquetas prestam.

---

## Etapa 0 — Escolher a câmera (1 minuto)

Pule se for usar a webcam integrada. **Se você usa o celular como webcam**
(Camo, Iriun, DroidCam, EpocCam) ou o OBS, faça isto primeiro:

```bash
python src/camera.py
```

Ele lista todas as câmeras com **nome e resolução**, marca qual é virtual, abre
uma prévia lado a lado e grava a sua escolha em `config/parametros.json`. Depois
disso todos os programas usam essa câmera.

```
  [0] Integrated Webcam  1280x720
  [1] Reincubate Camo  1920x1080  <- camera virtual (celular/OBS)
```

Antes de rodar: abra o app **no computador e no celular**, e confirme que a
prévia aparece dentro do app. A câmera virtual só existe para o sistema
enquanto o app está conectado.

**Confirme antes de confiar no nome.** No Windows, a lista de nomes que o
sistema fornece nem sempre segue a mesma numeração do OpenCV — então o nome
mostrado pode estar associado ao índice errado. A prévia mostra a **imagem** de
cada índice, e a imagem não mente. Escolha por ela.

Para ter os nomes certos no Windows: `pip install pygrabber`. Com ele instalado
a associação nome↔índice passa a ser confiável (ele lê o mesmo DirectShow que o
OpenCV usa) e aí `--usar camo` funciona direto.

Comandos úteis:

```bash
python src/camera.py --qual        # qual câmera os programas vão abrir agora
python src/camera.py --testar 1    # abre o índice 1, mostra a imagem, grava se confirmar
python src/camera.py --usar camo   # grava por nome (precisa de nome confiável)
python src/testar.py --camera 1    # só nesta execução, sem gravar
```

Quando o nome é confiável, ele fica gravado junto com o índice em
`parametros.json`. Na próxima vez o sistema procura a câmera **pelo nome** e só
usa o índice se não achar — assim a escolha sobrevive a mudar a ordem dos
dispositivos.

---

## Etapa 1 — Só a etiqueta na frente da câmera (5 minutos)

Não precisa de dispenser, não precisa calibrar nada.

```bash
python src/testar.py --checar    # confere a instalação e sai
python src/testar.py             # abre a câmera
```

Segure uma etiqueta impressa na frente da câmera. A tela mostra, para cada
código lido: o medicamento, o tipo (QR ou ArUco), o tamanho em pixels e um
veredito de folga.

**O que fazer aqui:** aproxime e afaste devagar até parar de ler. Ao sair, o
programa imprime o menor tamanho em que cada código ainda foi lido. Esse número
é o seu orçamento de distância — guarde.

**Contagem de unidades:** coloque várias etiquetas na frente da câmera. O canto
superior mostra quantas **caixas** de cada medicamento estão visíveis — não
quantos códigos. Uma etiqueta tem 3 códigos (1 QR + 2 ArUco), e o sistema agrupa
os três numa unidade só. Se uma caixa estiver com o QR tapado, os ArUco dela
sustentam a contagem.

O FPS aparece no rodapé. Abaixo de 15, rode `python src/fps.py` — ele mede e
diz qual das quatro etapas está segurando (ver Etapa 5).

| veredito | significa |
|---|---|
| FOLGA BOA | está confortável, pode afastar mais |
| NO LIMITE | funciona, mas qualquer piora derruba |
| PEQUENO DEMAIS | imprima maior ou aproxime a câmera |

Se **nada** for lido, na ordem:

1. meça a régua no rodapé da folha impressa — tem que dar 50 mm
2. veja se a etiqueta aparece grande e nítida na imagem
3. verifique reflexo de luz em cima do código
4. `python src/diagnostico.py` mede foco, luz e reflexo e diz o que corrigir

Sem câmera à mão? `python src/testar.py --imagem foto.jpg` funciona com uma
foto tirada do celular.

---

## Etapa 2 — Os 4 dispensers, com alerta de posição errada (20 minutos)

Cole ou apoie uma etiqueta de cada medicamento onde ficará a brecha de cada
dispenser. Não precisa da bancada final — quatro etiquetas lado a lado numa
mesa já servem.

```bash
python src/calibrar.py     # desenhe um retângulo por dispenser
python src/main.py         # a versão enxuta: só código
```

A janela do `main.py` é o painel de operação: vídeo à esquerda, um cartão por
dispenser à direita com o que foi lido e quantas unidades, e o rodapé com FPS e
o custo de cada etapa. `Z` volta para o overlay clássico, `F` põe em tela
cheia, `E` salva um print.

Na calibração: arraste o mouse em volta da primeira etiqueta, aperte `1`,
repita com `2`, `3`, `4` e salve com `ENTER`. Os códigos são lidos ao vivo e
pintados de verde/vermelho enquanto você ajusta.

**O teste que importa:** troque a etiqueta do dispenser 1 com a do 3. Em cerca
de 3 leituras o alerta dispara — banner vermelho, X sobre o código, beep e uma
linha em `logs/alertas.csv`.

---

## Etapa 3 — Estoque e painel web (mais 20 minutos)

```bash
python src/calibrar_estoque.py   # V grava a prateleira vazia, 1..9 informa quantas caixas
python src/estacao.py            # sistema completo + painel em http://localhost:8000
```

Na calibração de estoque, para cada dispenser: esvazie a pilha e aperte `V`,
depois coloque um número conhecido de caixas e aperte a tecla desse número.
Pronto — daí em diante ele conta sozinho.

O painel mostra o estado de cada dispenser, o nível de estoque, as ocorrências
recentes e um selo de integridade da trilha de auditoria.

---

## Etapa 4 — Reconhecimento da embalagem (opcional, mas é o diferencial)

Só faz sentido com caixas de verdade — ele identifica o produto pela arte da
embalagem, não pela etiqueta.

```bash
python src/cadastrar_visual.py --sku MED-001    # ESPAÇO captura, ENTER salva
python src/estacao.py
```

Tire de 4 a 8 fotos por medicamento, variando um pouco o ângulo e a distância,
e inclua uma com a luz mais fraca. Se as caixas puderem ficar tortas na
prateleira, capture também duas giradas (45° e 90°) — foi medido que isso
recupera quase todos os ângulos intermediários.

**O teste que só esta etapa permite:** cole a etiqueta do MED-001 numa caixa
que fisicamente é outro medicamento. O código diz uma coisa, a embalagem diz
outra, e o sistema acusa `DIVERGENCIA`. Nenhum leitor de código pega isso.

Atenção a um limite medido: o reconhecimento visual precisa da caixa ocupando
pelo menos ~340 px de largura no recorte da zona. Se o dispenser aparecer
pequeno no frame, use resolução maior ou aproxime a câmera.

---

## Etapa 5 — FPS: medir antes de mexer (2 minutos)

```bash
python src/fps.py                            # 8 s e um veredito
python src/fps.py --larguras 0,1280,960,640  # compara resoluções
```

A saída separa quatro custos que se confundem quando se olha só o FPS:

| etapa | o que é | como se corrige |
|---|---|---|
| captura | o tempo que a câmera leva para entregar o frame | resolução **no app da câmera**; USB em vez de Wi-Fi; fechar Teams/Meet |
| redução | levar o frame à largura de processamento | pedir menos pixels à câmera |
| detecção | a leitura (QR + ArUco) | fixar a câmera, `limiar_mudanca`, `orcamento_ms` |
| interface | desenhar a tela | `interface.largura/altura` |

A primeira linha do relatório é a mais importante: o **teto de FPS da câmera**.
Ele é medido sem processar nada. Se a câmera entrega 12 FPS, nenhum ajuste no
código passa disso — o loop está esperando ela, e a correção é no app do
celular (Camo: Settings → Resolution → 720p) ou no cabo.

O rodapé da janela mostra os mesmos números ao vivo, mais
`leitura pesada: N% dos frames`. Em bancada parada esse número deveria ficar
baixo; se estiver alto, a imagem está mudando frame a frame (câmera tremendo,
ruído de sensor) e a leitura cara roda sempre — suba `limiar_mudanca`.

Referência medida nesta base, com câmera simulada em 1080p e ruído de sensor:

| largura de processamento | detecção | interface | total | FPS |
|---|---|---|---|---|
| original (1920) | 41,1 ms | 10,0 ms | 51,1 ms | 19,6 |
| 960 | 12,7 ms | 3,8 ms | 17,1 ms | **58,4** |

---

## Testar sem hardware nenhum

```bash
python tests/test_sistema.py        # leitura de código
python tests/test_v2.py             # reconhecimento, fusão, estoque, auditoria
python tests/benchmark_robustez.py  # robustez a câmera ruim
python tests/benchmark_pose.py      # limites de escala e de orientação
python tests/preview_interface.py   # desenha a interface com dados falsos

# cena sintética com erro proposital, de ponta a ponta
python tests/cena_sintetica.py --video cena.mp4 --frames 60 --trocar 1 3
python tests/cena_sintetica.py --salvar-zonas
python src/main.py --video cena.mp4
```

---

## Problemas comuns

| sintoma | causa provável |
|---|---|
| `FileNotFoundError: config/zonas.json` | ainda não calibrou — `python src/calibrar.py` |
| Câmera não abre | `python src/camera.py` lista as câmeras com nome |
| Camo não aparece na lista | o app precisa estar aberto no PC **e** no celular, conectado |
| Camo aparece mas a imagem é preta | outro programa está segurando a câmera (Teams, Meet, navegador) |
| Escolhi o Camo mas abre a integrada | o nome estava associado ao índice errado; use `--testar N` e confirme pela imagem |
| Nomes das câmeras errados (Windows) | `pip install pygrabber` |
| Foco/exposição não obedecem | normal em câmera virtual — quem manda é o app no celular |
| `ImportError: Unable to find zbar` (Windows) | instale o VC++ Redistributable 2013 (x64 e x86) |
| Conta menos caixas que o real | as etiquetas estão coladas demais; separe ~1 cm entre elas |
| Alerta demora | `frames_para_confirmar` em `config/parametros.json` |
| Contagem de estoque errada | falta gravar a prateleira vazia (`V` no `calibrar_estoque.py`) |
| Move a câmera e para de funcionar | a zona é em pixels — recalibre depois de mexer na câmera |
| FPS baixo | `python src/fps.py` diz qual das quatro etapas é o gargalo |
| FPS baixo e a câmera é o celular | quase sempre é 1080p no app; baixe para 720p no Camo/Iriun |
| Janela grande demais / pequena demais | `interface.largura/altura` em `config/parametros.json` |
