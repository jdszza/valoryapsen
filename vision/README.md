# Visão da célula

As três câmeras reais da célula, cada uma numa estação de visão que roda no
mini PC (no Windows, fora do Docker, porque é dona da webcam USB) e fala HTTP
com o `vision-adapter`.

| Pasta | O que é | Entra no git? |
|---|---|---|
| `visao_mesa/` | a estação da **mesa**: conta as caixinhas na caixa de coleta. `integracao_apsen/` é a camada HTTP que fala o contrato do vision-simulator | sim |
| `visao/` | a estação dos **dispensers**: lê QR + ArUco e diz se o medicamento certo está em cada dispenser. É o código e o modelo de configuração | sim |
| `visao_esq/`, `visao_dir/` | as pastas de EXECUÇÃO das duas câmeras dos dispensers, criadas por `iniciar_dispensers.bat` como cópia de `visao/` | só `config/` (a calibração de cada câmera) |
| `iniciar_dispensers.bat` | sobe uma estação dos dispensers (`esq` ou `dir`) | sim |
| `ensaio_dispensers.py` | ensaio da ponte adapter ↔ estação real dos dispensers, sem câmera | sim |
| `iniciar_visao.bat`, `gerar_arquitetura.py` | vieram no pacote original; ver abaixo | não |

## Regra nº 1: o código da visão não é editado aqui

Nenhum `.py` de `visao/` e `visao_mesa/` é editado por este repositório — nem
"melhorado". Foi esse código que se ensaiou, e a suíte do repositório não
importa cv2: um limiar ajustado por dentro invalida o ensaio sem ficar vermelho
em lugar nenhum. `tests/test_visao_intocada.py` guarda o hash de cada arquivo e
reprova arquivo mudado, sumido ou novo. Toda a integração com o central está do
lado de cá: no `vision-adapter`, no orquestrador e na configuração.

**O que pode mudar é configuração de bancada:**

| Arquivo | Quem escreve |
|---|---|
| `visao_mesa/config/mesa.json` | `calibrar.py` da mesa (e à mão: `fundo.modo`, `suavizacao`, `frames_validade`) |
| `visao_mesa/integracao_apsen/.env` | você — não versionado |
| `visao_esq/config/` e `visao_dir/config/` (`parametros.json`, `zonas.json`) | `camera.py`, `calibrar.py` e você |
| `visao/config/` | o modelo de onde as duas pastas de execução nascem |

## Como as estações conversam com o central

```
 central :8000 ──comando──▶ vision-adapter :8102 (container)
                                │        ▲        │ GET /api/estado
            /executar/capturar/mesa      │ eventos│
                                ▼        │        ▼
                    estação da mesa :8212     estações dos dispensers :8301 (esq) / :8302 (dir)
                    (contrato do simulador)    │ GET /api/visao/catalogo, POST /api/visao/estoque
                                               └──▶ vision-adapter :8102
```

- **Mesa.** A estação já fala o contrato do vision-simulator: o adapter só
  aponta `VISION_SIM_URL` para ela. Ela conta o TOTAL da caixa e manda a
  diferença desde a última foto da mesma OS. Em posições em que a câmera não vê
  a caixa inteira (`VISAO_MESA_POSICOES`) o central não pede foto, e a foto
  seguinte confere os slots juntos.
- **Dispensers.** A estação não recebe comando: olha sem parar e julga cada zona
  contra um catálogo que busca no adapter a cada 2 s. O adapter monta esse
  catálogo com o que o central mandou carregar NA OS CORRENTE (mais todo outro
  medicamento etiquetado, com um número "fantasma" sem zona, para a estação
  dizer o que achou), espera a estação julgar com ele, lê o `/api/estado` e
  emite o `leitura_dispenser_*` para o central. O estoque que a estação mede é
  aceito e descartado: quem manda no estoque é o central.

O porquê de cada uma dessas escolhas está no `CLAUDE.md`, seção "A visão real:
o adapter traduz, a estação não muda".

## Portas

| Porta | Quem |
|---|---|
| 8212 | estação da mesa (não a 8202 do default dela: a 8202 é do vision-simulator) |
| 8301 | estação dos dispensers, fileira esquerda (D1–D4) |
| 8302 | estação dos dispensers, fileira direita (D5–D8) |
| 8102 | vision-adapter (container), onde as três estações chamam |

## Como subir as estações

Ver [docs/DEPLOY_WINDOWS.md](../docs/DEPLOY_WINDOWS.md), seção "As três
estações de visão": `vision\iniciar_dispensers.bat esq|dir` para as câmeras dos
dispensers e `vision\visao_mesa\integracao_apsen\iniciar_estacao.bat` para a da
mesa, com os `.env` de cada lado.

**Não use `vision\iniciar_visao.bat`.** Ele veio no pacote original e sobe a
estação na porta 8000, que é a do central — a estação e o central disputariam a
mesma porta, e quem subir depois não sobe.

## Como voltar ao simulador

Tirar do `.env` da raiz `VISION_SIM_URL`, `VISAO_MESA_FONTE` e
`VISAO_DISPENSER_FONTE` (ou pôr as duas fontes em `simulador`) e recriar o
vision-adapter. Nada mais: o vision-simulator nunca saiu do compose, e os
defaults são os de antes das estações.

## Como etiquetar um medicamento novo

A estação dos dispensers só reconhece caixa com a etiqueta impressa pelo
`visao/src/gerar_qrcodes.py`: um QR e dois marcadores ArUco do dicionário
`DICT_4X4_50`. Quem liga a etiqueta ao medicamento do central é a tabela
`vision-adapter/etiquetas.json`.

1. **Escolha um `qr` e um `aruco` livres.** `qr` no padrão `MED-0NN`; `aruco`
   de 0 a 49 (o dicionário tem 50 ids). Nenhum dos dois pode repetir na tabela.
2. **Acrescente a linha NO FIM de `vision-adapter/etiquetas.json`**, com o
   `nome` exatamente como está em `medicamentos.nome` no central. A posição da
   linha dá o número fantasma do medicamento (1000 + posição); reordenar a
   tabela muda esses números. Linha inválida (qr vazio ou repetido, aruco
   repetido ou fora de 0..49, nome repetido) é descartada com ERROR no log do
   adapter, e `tests/test_vision_adapter_ponte.py` reprova nome que o central
   não tem. A tabela vai na imagem do adapter: recrie o container.
3. **Imprima a etiqueta.** O gerador lê o `config/medicamentos.json` da pasta
   em que roda, e esse arquivo é também o catálogo de reserva da estação e a
   fonte da cena sintética de teste. Então gere numa CÓPIA da pasta:

   ```bat
   robocopy vision\visao %TEMP%\etiquetas /E /XD dados logs .venv __pycache__
   cd %TEMP%\etiquetas
   rem edite config\medicamentos.json: um item por etiqueta a imprimir
   python src\gerar_qrcodes.py
   ```

   O resultado sai em `qrcodes/folha_etiquetas.pdf` da cópia. Imprima em 100%
   ("Tamanho real").

**O "DISPENSER n" impresso na etiqueta não vale.** O slot de cada medicamento
muda a cada OS, e quem diz qual é o certo é o catálogo que o adapter serve. O
gerador exige um número de dispenser **diferente** por item (é a regra de
`Catalogo.carregar`), mas não tem teto: para imprimir várias etiquetas de uma
vez, use para cada uma o mesmo número do catálogo, 1000 + a posição dela na
tabela (MIOSAN 5MG = 1001, …). Isso é limitação da ferramenta da visão, e ela
não é alterada por este repositório.

## Ensaio sem câmera

```bash
python vision/ensaio_dispensers.py
```

Sobe a estação dos dispensers de verdade lendo uma cena sintética, o
vision-adapter de verdade e um central falso, e confere que o veredito troca
quando a OS muda: 4 `leitura_dispenser_ok`, depois D1/D2 trocados → 2
`leitura_dispenser_divergencia`. Precisa de OpenCV e leva alguns minutos na
primeira vez (gera o vídeo); `--video` reaproveita um já gerado.
