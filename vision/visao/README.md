# Visão computacional — conferência de medicamentos em dispensers

Sistema que confere, por visão, se cada medicamento está no dispenser certo — e
**dispara um alerta quando não está**.

Duas versões convivem no mesmo projeto:

| | `src/main.py` | `src/estacao.py` |
|---|---|---|
| Lê código (QR + ArUco) | sim | sim |
| Reconhece a embalagem sem código | — | sim |
| Detecta etiqueta trocada (divergência) | — | sim |
| Conta o estoque de cada pilha | — | sim |
| Trilha de auditoria + painel web | — | sim |

**Primeiro contato: [COMO_TESTAR.md](COMO_TESTAR.md)** — quatro etapas, da
etiqueta na mão até o sistema completo. A etapa 1 leva 5 minutos e não exige
calibração nenhuma:

```bash
python src/camera.py            # escolha a câmera (necessário se usa Camo/OBS)
python src/testar.py --checar   # confere a instalação
python src/testar.py            # aponte a câmera para uma etiqueta impressa
```

Usa o celular como webcam? `python src/camera.py` lista as câmeras com nome,
marca qual é virtual e grava a escolha. Também dá para selecionar por nome em
qualquer programa: `--camera camo`.

Comece pelo `main.py` para validar a bancada; use o `estacao.py` quando quiser o
sistema completo. As decisões de arquitetura e o plano por fases estão em
**[ARQUITETURA.md](ARQUITETURA.md)**.

Cada tipo de medicamento tem um QR code único (todas as caixas do mesmo tipo
carregam o mesmo código). Cada dispenser tem um retângulo (“zona”) calibrado
por você na imagem da câmera. Se o QR lido dentro da zona do dispenser 3 for
o medicamento cadastrado para o dispenser 1 → **ERRO_POSICAO**.

---

## 1. Instalação

```bash
pip install -r requirements.txt

# Linux / Raspberry Pi — biblioteca nativa do leitor de QR (recomendado):
sudo apt install libzbar0
```

Sem o `libzbar0` o sistema cai automaticamente para o detector do OpenCV,
que funciona mas é menos tolerante a QR pequeno, torto ou mal iluminado.

## 2. Cadastrar os medicamentos

Edite `config/medicamentos.json`. O campo `qr` é o **conteúdo** do QR code:

```json
{ "qr": "MED-001", "nome": "Dipirona 500mg", "dispenser": 1 }
```

Regra validada automaticamente: **um dispenser = um único tipo de medicamento**.
Se você cadastrar dois medicamentos no mesmo dispenser, o sistema recusa a config.

## 3. Gerar e imprimir as etiquetas

```bash
python src/gerar_qrcodes.py --tamanho-mm 35 --copias 8
```

Gera em `qrcodes/` uma etiqueta por medicamento e a folha A4
`folha_etiquetas.pdf` (com régua de conferência no rodapé). Imprima em **100%**
/ “Tamanho real”, nunca “ajustar à página”.

Cada etiqueta traz **um QR code e dois marcadores ArUco** com o mesmo
significado. Não é redundância à toa: o QR aguenta reflexo (tem 30% de correção
de erro) e o ArUco aguenta ângulo, desfoque, pouca luz e código pequeno. Juntos
levam a leitura de 74% para 100% na bateria de cenários difíceis — detalhes e
números em [ROBUSTEZ.md](ROBUSTEZ.md).

Se quiser só o QR: `--sem-aruco`, ou `"aruco": null` no cadastro.

## 4. Calibrar as zonas dos dispensers ← passo principal

```bash
python src/calibrar.py                    # ao vivo, na câmera
python src/calibrar.py --imagem foto.jpg  # sobre uma foto da bancada
```

Você desenha um retângulo em volta da brecha de cada dispenser e ajusta até
ficar exato. Os QR codes são lidos ao vivo durante a calibração e coloridos
conforme o resultado — assim dá para conferir na hora se o retângulo está
pegando o código certo (verde = OK, vermelho = dispenser errado, amarelo =
fora de zona ou não cadastrado).

| Ação | Controle |
|---|---|
| Criar zona | arrastar o mouse numa área livre |
| Selecionar | clicar dentro da zona |
| Mover | arrastar de dentro da zona |
| Redimensionar | arrastar um dos 4 cantos |
| Numerar o dispenser | teclas `1`…`9` (troca com quem já tinha o número) |
| Ajuste fino | setas / `W A S D` (maiúsculas = 10 px) |
| 4 zonas iguais em coluna | `G` (bom ponto de partida) |
| Próxima zona | `TAB` |
| Apagar zona / todas | `DEL` ou `X` / `R` |
| Congelar imagem | `F` |
| **Salvar** | `ENTER` ou `C` |
| Sair sem salvar | `ESC` ou `Q` |

Resultado: `config/zonas.json`. As zonas são reescaladas automaticamente se a
resolução da câmera mudar depois.

Dica: deixe a zona **um pouco maior** que a brecha, mas sem encostar na do
vizinho. A regra usa o **centro** do QR code, então um código na fronteira é
atribuído ao dispenser em cuja zona o centro cair.

## 5. Rodar

```bash
python src/main.py                     # janela ao vivo
python src/main.py --camera 1
python src/main.py --sem-janela        # headless (só log/webhook/GPIO)
python src/main.py --video teste.mp4   # sobre um vídeo gravado
python src/main.py --gravar saida.mp4  # grava o vídeo com o overlay
```

Teclas: `Q`/`ESC` sair · `P` pausar · `E` salvar print · `F` tela cheia ·
`Z` alterna entre o painel Apsen e o overlay clássico.

A janela é o **painel de operação** (identidade Apsen): vídeo à esquerda,
contagem por dispenser à direita, última ocorrência embaixo, e um rodapé com
FPS e o tempo de cada etapa. Ele tem tamanho fixo (`interface.largura/altura`
em `config/parametros.json`), independente da resolução da câmera.

### FPS

Duas alavancas, nesta ordem:

1. **`deteccao.largura_processamento`** (padrão 960). O frame é reduzido para
   essa largura *antes* de qualquer processamento e da exibição. Sair de 1080p
   para 960 px corta ~68% dos pixels. Medido nesta base: 19,6 → 58,4 FPS.
2. **`interface.largura/altura`** (padrão 1280x720). Só afeta o desenho da
   tela, que já custa ~2,8 ms.

Se ainda estiver lento, não chute — meça:

```bash
python src/fps.py                          # 8 s e um veredito
python src/fps.py --larguras 0,1280,960,640
```

Ele separa **captura**, **redução**, **detecção** e **interface**, mostra o
teto de FPS da câmera (o que ela entrega sem processar nada) e diz qual dos
quatro corrigir. Camera virtual (Camo/Iriun) entregando 1080p costuma ser o
gargalo real — e nesse caso a correção é no app do celular, não no código.

## 6. Quando a leitura falhar (câmera ruim, ângulo, luz)

```bash
python src/diagnostico.py --segundos 30
```

Mede, por dispenser, taxa de leitura, foco, brilho, contraste, reflexo e
tamanho do código na imagem — e diz o que corrigir em cada um. O guia completo,
com a ordem de ataque, está em **[ROBUSTEZ.md](ROBUSTEZ.md)**. Em resumo, do
mais eficaz para o menos: travar foco e exposição, imprimir o código maior,
arrumar a luz (difusa e lateral, nunca de frente), subir a resolução.

## 7. Alertas

Quando um erro é confirmado, todos os canais ativos disparam:

| Canal | Onde configurar |
|---|---|
| Retângulo vermelho + banner na tela | sempre ativo na janela |
| Console colorido | sempre ativo |
| CSV com timestamp | `alertas.log_csv` → `logs/alertas.csv` |
| Beep | `alertas.som` |
| POST JSON para uma API | `webhook.ativo` + `webhook.url` |
| Buzzer/LED no Raspberry Pi | `gpio.ativo` + `gpio.pino` |

Estados possíveis:

**Contagem de unidades.** Cada etiqueta tem 3 códigos, então contar códigos
contaria 3 caixas onde há uma. O sistema agrupa os códigos por proximidade: cada
grupo é uma caixa. Assim ele informa "3x Dipirona 500mg no dispenser 1" e
continua separando o que está certo do que está errado — uma unidade fora de
lugar no meio de três certas mantém o dispenser em erro.

- `OK` — medicamento no dispenser certo
- `ERRO_POSICAO` — **medicamento no dispenser errado** (o alerta principal)
- `QR_DESCONHECIDO` — QR não cadastrado no catálogo
- `FORA_DE_ZONA` — QR visível fora de qualquer dispenser (só avisa se ligado)

### Evitando alarme falso

Em `config/parametros.json`:

- `frames_para_confirmar` (padrão 3) — o erro precisa ser **lido** N vezes
  antes de virar alerta. Não precisam ser frames seguidos: numa câmera que só
  decodifica de vez em quando, exigir frames consecutivos deixaria o sistema
  mudo. Suba se a câmera treme ou a luz oscila.
- `memoria_segundos` (padrão 2) — por quanto tempo a zona mantém o último
  código lido quando a leitura falha. Como o dispenser é estático, isso acaba
  com o status piscando.
- `cooldown_segundos` (padrão 10) — não repete o mesmo alerta antes disso,
  para não inundar o log enquanto ninguém corrige a caixa.
- `margem_zona` (padrão 0) — folga, em frações do lado do QR, para o centro
  ficar um pouco fora da zona sem invalidar.

## 8. Testar sem hardware

```bash
python tests/test_sistema.py                            # leitura de código
python tests/test_v2.py                                 # reconhecimento, fusão, estoque, auditoria
python tests/benchmark_robustez.py                      # mede a robustez a câmera ruim
python tests/cena_sintetica.py --trocar 1 3             # imagem com erro
python tests/cena_sintetica.py --video cena.mp4 --trocar 1 3
python src/main.py --video cena.mp4 --gravar overlay.mp4
```

A cena sintética monta 4 dispensers com QRs reais renderizados, permitindo
validar a lógica completa (leitura, zonas, debounce, cooldown, log) antes de
ter a bancada montada.

## 9. Estrutura

```
config/
  medicamentos.json    catálogo: QR -> medicamento + dispenser esperado
  zonas.json           gerado por calibrar.py
  parametros.json      câmera, detecção, alertas, webhook, GPIO
src/
  configuracao.py      carga/validação das configs e geometria das zonas
  camera.py            abertura da câmera e ajustes de foco/exposição
  preprocessamento.py  cascata de tratamento de imagem + métricas de qualidade
  leitor_qr.py         decodificação QR + ArUco, por zona, com cache
  detector.py          regras, estados, confirmação, memória e cooldown
  desenho.py           overlay: zonas, leituras, painel e banner
  alertas.py           canais: console, CSV, som, webhook, GPIO
  calibrar.py          ferramenta interativa de calibração das zonas
  diagnostico.py       mede a qualidade de leitura por dispenser e sugere ações
  fps.py               separa captura/redução/detecção/interface e aponta o gargalo
  interface.py         painel de operação com a identidade visual da Apsen
  gerar_qrcodes.py     etiquetas (QR + ArUco) e folha para impressão
  testar.py            primeiro teste, sem calibração (comece por aqui)
  main.py              aplicação ao vivo (só código)
  --- camada de produto ---
  reconhecimento.py    identifica a embalagem sem código, com rejeição
  fusao.py             cruza código e aparência -> veredito com confiança
  contagem.py          nível de estoque por altura da pilha
  eventos.py           banco de eventos com trilha encadeada por hash
  api.py               API HTTP + painel web
  estacao.py           pipeline completo (a versão "produto")
  cadastrar_visual.py  cadastro da aparência das embalagens
  calibrar_estoque.py  calibração da contagem
tests/
  cena_sintetica.py    gerador de cena/vídeo de teste
  degradacao.py        simulador de câmera ruim (ângulo, desfoque, luz, reflexo)
  embalagens.py        gerador de faces de embalagem sintéticas
  benchmark_robustez.py mede o ganho de cada melhoria
  test_sistema.py      testes da leitura de código
  test_v2.py           testes de reconhecimento, fusão, estoque e auditoria
  preview_interface.py renderiza a interface com dados falsos (confere layout)
```

## 10. Dicas de montagem física

- **Iluminação difusa e frontal.** O maior inimigo é o reflexo especular do
  plástico/blister em cima do QR. Luz de LED em barra nas laterais funciona
  melhor que um ponto único de frente.
- **Tamanho do código na imagem.** Regra prática medida aqui: o QR precisa de
  pelo menos ~70 px de lado no frame (abaixo de 60 px falha). O marcador ArUco
  aguenta bem menos, ~30 px — é ele que segura a leitura quando o QR já era.
  Com 4 dispensers em 1280 px de largura, imprima com 40–50 mm.
- **Câmera fixa.** A calibração é em pixels; se a câmera se mover, recalibre.
  Se ela for mexer com frequência, o caminho é colar um marcador ArUco em cada
  dispenser — dá para acrescentar depois, trocando só o `zona_do_ponto`.
- **Uma câmera para os 4** é o mais simples, desde que todas as brechas caibam
  no enquadramento com resolução suficiente.
- Se o dispenser for profundo, mire a brecha de forma que só a caixa **da
  frente** apareça — senão o sistema lê o QR de uma caixa mais atrás, o que é
  inofensivo (mesmo tipo, mesma zona), mas atrapalha a contagem.
