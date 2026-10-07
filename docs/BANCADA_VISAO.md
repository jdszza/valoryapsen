# Bancada da visão real — procedimento físico

Este é o trabalho de mão que vem ANTES de ligar `VISAO_MESA_FONTE=estacao` ou
`VISAO_DISPENSER_FONTE=estacao` no `.env` da raiz. Ele não é código: são
medidas e calibrações feitas na célula montada, e é delas que saem os valores
que o `.env` carrega. Como subir as estações está em
[DEPLOY_WINDOWS.md](DEPLOY_WINDOWS.md); o porquê de cada decisão, no
`CLAUDE.md`, seção "A visão real: o adapter traduz, a estação não muda".

O código das duas estações (`vision/visao_mesa` e `vision/visao`) **não é
editado** por este repositório — `tests/test_visao_intocada.py` prende o hash
de cada `.py`. Tudo abaixo mexe em configuração (`config/*.json`, `.env`) e no
mundo físico.

## Mesa (`vision/visao_mesa`)

- **M1. O modo do fundo tem de seguir a caixa.** Com a câmera fixa no poste e a
  caixa andando com a CNC, o modo `"manual"` (quatro cantos fixos na imagem)
  conta no lugar errado em toda parada menos a calibrada — e conta com
  `confiavel=True`, ou seja, sem nenhum aviso. Use `fundo.modo = "caixa"`: ele
  acha o fundo a cada foto. Papelão contra papelão não dá contorno: forre o
  fundo com cartolina de cor contrastante e meça com trena —
  `comprimento_mm`/`largura_mm` são as medidas da cartolina
  (`vision/visao_mesa/NOTAS_CALIBRAGEM.md`, "Caixa de papelão como
  recipiente").
- **M2. Dois campos do `mesa.json` são OBRIGATÓRIOS para caixa que se move:**
  `fundo.suavizacao = 0` e `fundo.frames_validade = 0`. Com outros valores a
  foto de um slot reaproveita os cantos (ou a homografia inteira) do slot
  anterior. O `calibrar.py` regrava o arquivo INTEIRO a cada "SALVAR TUDO",
  então **depois de qualquer "SALVAR TUDO", rode `python vision\conferir_mesa.py`**
  (o `vision\iniciar_mesa.bat` roda sozinho a cada subida e para em vermelho):
  ele confere o modo do fundo, os dois campos acima, o autofoco, o `.env` da
  estação, os pacotes do venv e se a webcam da mesa é a mesma de uma estação de
  dispenser.
- **M3. Vibração da CNC.** A mesa está parada na foto, mas pode estar
  balançando. Comece com `T_ASSENTAMENTO_S=1.0` no `.env` da estação e olhe o
  campo `nitidez` nos eventos: foto tremida vira falha "fora de foco" (que não
  trava), então muitas falhas por foco pedem assentamento maior.
- **M4. Mapa de visibilidade (`VISAO_MESA_POSICOES`).** Para cada slot 1..8 e
  para o HOME: leve a mesa até lá, ponha 3 caixinhas espalhadas (uma encostada
  na parede do lado da câmera) e dispare 5 capturas com `os_id` diferentes:

  ```bash
  curl -X POST http://127.0.0.1:8212/executar/capturar/mesa -H "Content-Type: application/json" -d "{\"slot_id\":1,\"os_id\":\"MAPA-1-a\",\"quantidade_esperada\":3}"
  ```

  Entra no mapa só a posição com **5 de 5 `ok`**. Anote a tabela: é ela que
  justifica o valor da variável. Os eventos dessas capturas chegam ao central
  com `os_id` `MAPA-…` e entram no histórico de leituras e alarmes — faça o
  mapa antes de limpar o histórico, ou resolva os alarmes depois.
- **M5. A câmera inclinada está fora do que a visão validou** (ela foi
  ensaiada com câmera por cima). Se em NENHUMA posição der 5 de 5, o problema é
  de montagem, não de integração: suba ou incline mais a câmera antes de ligar
  `VISAO_MESA_FONTE=estacao`.
- **M6. Reiniciar a estação da mesa com OS rodando custa uma conferência, não
  uma trava.** O acumulado da OS vive só na memória dela; o vision-adapter nota
  quando ela o perdeu (reinício, ou `VALIDADE_OS_H` vencida numa trava longa) e
  marca a foto seguinte como `ressincronizar` — o central não a compara, grava o
  alarme `camera_mesa_ressincronizando` e segue. Mesmo assim, reinicie ENTRE OSs
  quando puder.

## Dispensers (`vision/visao`, duas pastas)

- **D1.** Rode `vision\iniciar_dispensers.bat esq` uma vez (cria a pasta
  `vision\visao_esq`) e feche; idem `dir`.
- **D2.** Em cada pasta: `python src\camera.py` (escolhe a câmera — ela fica
  gravada POR NOME no `parametros.json`) e `python src\calibrar.py` — **numere
  as zonas com o número REAL do slot**: esquerda 1–4, direita **5–8** (teclas
  `5`…`8` no calibrar). O `iniciar_dispensers.bat` roda
  `vision\conferir_dispensers.py <lado>` antes de subir e para em vermelho se as
  zonas ainda forem as do modelo (medidas em outra bancada — QR na zona do
  vizinho trava a OS), se estiverem numeradas fora do lado, se ninguém escolheu
  a câmera ou se ela é a mesma de outra estação.

  **As três webcams precisam ser distinguíveis.** Escolha modelos diferentes,
  ou deixe cada uma SEMPRE na mesma porta USB, etiquetada: com três câmeras do
  mesmo modelo, o nome não distingue nenhuma, e o índice muda com a ordem em
  que o Windows as enumera. `CAMERA_ESQ`/`CAMERA_DIR` no `.bat` ficam vazios;
  um número ali é escape manual e pula o nome.

  **Teclas da janela da estação:** ESC encerra (o laço do `.bat` sobe a estação
  de novo em 5 s); `p` PAUSA — e com ela pausada o `momento` congela, ou seja,
  a conferência de SKU daquele lado vira falha "camera_sem_imagem" até
  despausar. Não pause com OS rodando.
- **D3.** Em cada `config\parametros.json`: `backend.url` **com o lado** —
  `"http://127.0.0.1:8102/estacoes/esq"` na pasta da esquerda,
  `"http://127.0.0.1:8102/estacoes/dir"` na da direita —,
  `backend.intervalo_catalogo = 2` (igual ao `VISAO_DISP_INTERVALO_CATALOGO_S`
  do vision-adapter) e `backend.ativo = true`. A rota do lado é o que deixa o
  adapter PROVAR que aquela estação buscou o catálogo da OS corrente; com a URL
  antiga (sem o lado), ele cai no critério só de relógio e avisa no log.
- **D4.** Cole as etiquetas. Para medicamento novo, primeiro a tabela
  (`vision-adapter/etiquetas.json`), depois imprima. As etiquetas antigas que
  dizem "DISPENSER N" não valem mais: o slot sai da OS, não da etiqueta.
- **D5.** Com o central rodando uma OS, abra `http://localhost:8301` e `:8302`
  (o painel da própria estação): os dispensers da OS têm de aparecer `OK`.
- **D6. QR alheio na zona.** Com as caixas REAIS da linha APSEN nas zonas, rode
  uma OS e confira em `http://localhost:8301` (e `:8302`) se alguma zona dá
  `NAO_CADASTRADO`. Código que a estação lê e não está na tabela de etiquetas —
  QR de bula digital, etiqueta de lote do fornecedor — vence a etiqueta certa
  na mesma zona. Com `VISAO_DISP_NAO_CADASTRADO=falha` (o default) isso não
  trava: o slot sai sem conferência e o central grava o alarme
  `codigo_desconhecido_dispenser`. Se acontecer, cubra o QR da embalagem — ou,
  sabendo que não há QR alheio nas caixas, decida voltar a
  `VISAO_DISP_NAO_CADASTRADO=divergencia` (aí ele trava, como antes).

## Riscos conhecidos

- **Caixinhas empilhadas** na caixa de coleta contam como uma (limitação da
  câmera única). Como a câmera contando a menos, sozinha, não trava, isso vira
  alarme — e quem segura o caso é a balança.
- **Captura travada no driver da webcam** para a fila da estação da mesa até o
  driver devolver. Sintoma: falhas `timeout_processamento` em sequência.
  Reinicie a estação ENTRE OSs.
- **A ponte dos dispensers depende do relógio da própria estação** (`momento`)
  e de `intervalo_catalogo` igual nos dois lados: desalinhados, a leitura sai
  antes de o catálogo novo valer.
