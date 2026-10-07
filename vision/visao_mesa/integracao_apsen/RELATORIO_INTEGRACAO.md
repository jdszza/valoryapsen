# Relatório — integração da estação de visão da mesa com o PC central APSEN

Estação: `visao_mesa` (OpenCV clássico, contagem por retângulos circunscritos).
Camada de integração: `integracao_apsen/`. **Nenhum arquivo do algoritmo de
visão foi alterado** — a conferência está em 10.4.

---

## 10.1 O que foi integrado

| Rota / evento | Status | Observação |
|---|---|---|
| `GET /ping` | ✅ | Responde em **5 ms**, sem tocar na câmera. Funciona com o central desligado e com a câmera ainda fechada. |
| `GET /status` | ✅ | Câmera pronta, fila, contadores, acumulado por OS e a config em uso. |
| `POST /executar/capturar/mesa` | ✅ | Responde em **2–3 ms**, mesmo com a contagem levando 1,5 s. Nunca processa dentro da requisição. |
| `POST /executar/capturar/dispenser` | ⚠️ | **HTTP 501** com `{"detail": "estação de visão não faz leitura de SKU"}`. Nenhum evento é emitido. Ver 10.2. |
| `leitura_mesa_ok` | ✅ | Emitido quando a contagem bate e a confiança passa do mínimo. |
| `leitura_mesa_divergencia` | ✅ | Só quando a estação contou **com confiança** e o número difere. Leitura incerta nunca vira divergência. |
| `leitura_mesa_falha` | ✅ | Câmera indisponível, obstrução, baixa confiança, contagem regredida, timeout e qualquer exceção. |
| Telemetria | ❌ | Não há sensor de temperatura na estação. O documento manda não inventar valores, então nada é enviado. O construtor existe (`eventos.evento_telemetria`) para quando houver sensor. |
| Idempotência | ✅ | `(os_id, slot_id)` repetido responde `200 "captura já em andamento"` e **não** dispara nova captura. Testado com 3 comandos iguais → 1 captura, 1 evento. |
| Contagem acumulada → por slot | ✅ | `quantidade_detectada = total_agora − total_anterior`, com `quantidade_total_caixa` junto. Testado 2/6/7 → 2, 4, 1. |
| Regressão do total | ✅ | `motivo: "contagem_regrediu"` e o acumulado **não** é atualizado. |
| Retentativa de envio | ✅ | 3 tentativas, 1 s entre elas, timeout 5 s. **4xx não é retentado** (payload inválido) e vai para o log com o payload inteiro. Nunca levanta exceção. |
| Exatamente um evento por comando | ✅ | Porteiro (`EnvioUnico`) em todos os caminhos de saída, inclusive o vigia de timeout. Testado: timeout emite a falha e a contagem que termina depois **não** gera um segundo evento. |
| Uma captura por vez, em ordem | ✅ | Fila única e uma thread de captura. |
| `injetar_falha` | ✅ | Ignorado por padrão com `WARNING` no log; reproduz o simulador com `ACEITAR_INJECAO=1`. |
| Aceitar campos extras | ✅ | `extra="allow"` no modelo do comando. |
| `slot_id` fora de 1..8 | ✅ | HTTP 400 `{"detail":"slot_id deve ser 1-8"}`. Corpo sem `slot_id`/`os_id` → 422. |

---

## 10.2 O que NÃO deu para integrar

**1. Leitura de SKU (`/executar/capturar/dispenser`).**
Esta estação olha a **caixa de coleta** sobre a balança, não a prateleira do
dispenser, e não lê QR/DataMatrix. Emitir `leitura_dispenser_ok` daqui seria
afirmar que o SKU confere sem ninguém ter lido SKU nenhum. Responde 501.

*Para fechar:* a leitura de código **já existe no outro projeto** da célula,
`apsen_sistema/visao` (`src/leitor_qr.py`, `src/reconhecimento.py`), que é a
estação virada para os dispensers. Ela é outra máquina/outro processo e hoje
publica num backend diferente (`POST /api/visao/estoque`, porta 5000). Dá para
dar a ela a mesma camada — o Anexo A — mas é um segundo trabalho, não um ajuste
deste. Enquanto isso, o central mantém o simulador só para as câmeras de SKU ou
desliga essa etapa.

**2. Telemetria de temperatura.** Não há sensor. Nada é enviado, por instrução
do próprio documento.

**3. `confianca` como probabilidade de verdade.** O algoritmo é OpenCV clássico:
ele produz um **veredito** (`confiavel`) e as medidas que o sustentam, não uma
probabilidade calibrada. O que vai no campo é um índice derivado dessas medidas
(fórmula em 10.3). Ele ordena bem — leitura boa fica em 0,92–0,98, leitura
instável cai abaixo de 0,60 — mas não é "97,8% de chance de ter 4 caixinhas".
*Para fechar:* só com o YOLO da etapa seguinte, que dá score por objeto.

**4. Abortar de verdade uma captura travada.** O vigia de `T_MAX_PROCESSAMENTO_S`
emite a falha de timeout para o central não esperar os 30 s, mas **não
desbloqueia** uma leitura presa no driver da câmera — não há como interromper um
`cap.read()` bloqueado de fora. Se isso acontecer, a fila para até o driver
devolver. *Para fechar:* capturar num subprocesso, que dá para matar. Não fiz
porque o custo (um processo por captura) é alto para um caso que não apareceu
nos testes — mas está listado como risco em 10.7.

**5. Caixa de coleta que começa não-vazia.** Ver a suposição em 10.3.

---

## 10.3 Decisões e suposições tomadas

**A estação conta o TOTAL da caixa, não o incremento.** Confirmado no código:
`VisaoMesa` conta todos os retângulos circunscritos na área útil. Por isso a
subtração por OS foi implementada.

**Cada OS começa com a caixa de coleta vazia.** É a suposição que o documento
sugere, e não tenho como verificar daqui. Primeira captura de um `os_id` novo
assume `total_anterior = 0`. *Se na operação real não for verdade* — caixa
reaproveitada entre OS, sobra de uma OS cancelada — a alternativa é uma **foto
de linha de base**: o central manda um comando de captura com
`quantidade_esperada: 0` antes do primeiro dispenser, e a estação grava aquele
total como `total_anterior`. Isso precisa de uma rota nova (ou de um campo
`linha_de_base: true` no comando atual) e de uma mudança do lado do central, por
isso **não** foi implementado por conta própria.

**`CONFIANCA_MINIMA = 0.60`** — o default do documento, mantido. Com a fórmula
abaixo, ele reprova exatamente o caso que interessa reprovar: maioria apertada
(2 de 3 frames, ou 3 de 5) dá 0,561 e vira falha.

**`T_ASSENTAMENTO_S = 0.5`** — o default do documento, mantido. A visão **não**
tem equivalente interno: o `Estabilizador` do `main.py` (voto de maioria em 8
frames) é da operação ao vivo e não é usado nesta camada — aqui o papel dele é
feito pelo voto de maioria da própria captura.

**Como a confiança é medida.** Cinco fotos por captura, contagem em cada uma,
voto de maioria. A nota é:

| Parcela | Peso | Normalização | Por quê |
|---|---|---|---|
| estabilidade | 0,50 | `(frac − 0,6) / 0,4` | fração dos frames que concordaram. Evidência mais forte: número que se repete em cinco fotos não é coincidência de limiar. Começa em 60% para que maioria apertada zere a parcela. |
| cobertura | 0,30 | `(cob − 0,60) / 0,35` | fração da máscara que virou item. Medido na bancada: leitura correta 0,80–1,00; errada por reflexo ≈ 0,39. É o que separa "contei" de "contei errado". |
| patamar | 0,20 | `(pat − 12) / 36` | largura da faixa de limiar em que a contagem não muda. Peso menor porque só é medido de verdade no modo lock; com lock desligado o peso é redistribuído para os outros dois. |

Imagem recusada pela própria visão → `confianca: 0.0` e **falha**, nunca
divergência.

**Gate de mudança desligado nesta estação** (`desempenho.gate_mudanca = False`,
só na instância da integração). O gate existe para o vídeo ao vivo: numa cena
parada ele devolve o resultado em cache. Aqui isso fabricaria a estabilidade —
os frames 2..5 repetiriam o voto do frame 1 e a nota daria 100% sem ter sido
medida.

**O lock varre o limiar só no primeiro frame de cada captura.** Medido: 1.503 ms
com varredura contra ~300 ms com o limiar já escolhido, mesma contagem e mesma
cobertura. Dentro de uma captura a cena é a mesma, então varrer cinco vezes paga
cinco vezes pela mesma resposta e, numa máquina lenta, encosta no teto de 20 s.
Os frames 2..5 continuam sendo **fotos novas** (ruído, variação de luz e
embalagem que se mexeu ainda aparecem no voto); o que eles deixam de testar é a
fragilidade do limiar, que continua medida uma vez, pelo patamar. O estado do
lock é restaurado em `finally` — senão a captura seguinte herdaria o limiar da
luz de dez minutos atrás.

**Quadro sem contraste → `obstrucao_visual`.** Lente tampada ou luz apagada: a
visão responde "fora de foco", porque área sem textura derruba a medida de
nitidez, e o diagnóstico mandaria o operador mexer no foco de uma câmera que
está é tampada. A camada detecta antes (desvio padrão < 8) e reporta obstrução.

**Tradução dos motivos da visão** para o vocabulário do contrato, com a frase
original preservada no campo extra `motivo_visao`:

| Motivo da visão | `motivo` no evento |
|---|---|
| fundo da caixa não encontrado | `camera_desalinhada` |
| reflexo especular | `obstrucao_visual` |
| imagem fora de foco | `imagem_fora_de_foco` (fora do vocabulário, em snake_case) |
| contagem instável (patamar estreito) | `baixa_confianca` |
| cobertura baixa | `obstrucao_visual` |

**`produto_nao_detectado` não é usado.** Caixa vazia com a imagem boa é uma
contagem legítima de zero, e se o slot devia ter soltado 4, isso é
**divergência** — tem de travar a OS. Transformar em falha esconderia um
dispenser vazio.

**`injetar_falha` ignorado por padrão.** Numa estação real, um campo que chega
por HTTP não pode mudar o que a câmera afirma ter visto.

**A câmera é aberta uma vez e mantida aberta.** Abrir custa os 20 frames de
aquecimento; abrir e fechar a cada captura gastaria isso oito vezes por OS. Em
caso de erro de leitura o handle é descartado para a captura seguinte reabrir.

**`os_id` sem normalização nenhuma** (`str()` e nada mais). É a chave que acorda
o orquestrador.

---

## 10.4 Arquivos criados e alterados na estação de visão

### Criados — 13 arquivos, todos dentro de `integracao_apsen/`

| Caminho | O que é |
|---|---|
| `integracao_apsen/__init__.py` | marcador de pacote (vazio) |
| `integracao_apsen/config.py` | variáveis de ambiente e `.env`, com os nomes e defaults da seção 3 |
| `integracao_apsen/eventos.py` | monta os três eventos e converte os tipos; `validar_evento` confere o contrato da seção 5.4 |
| `integracao_apsen/cliente.py` | `POST /eventos` com 3 tentativas; sem retentativa em 4xx; nunca levanta exceção |
| `integracao_apsen/contagem.py` | ponte com `VisaoMesa`: fonte de frames, voto de maioria, confiança derivada, acumulado por OS |
| `integracao_apsen/servidor.py` | FastAPI, fila de captura, idempotência, vigia de timeout, um evento por comando |
| `integracao_apsen/fake_adapter.py` | adapter falso (só biblioteca padrão) para o teste de ponta a ponta |
| `integracao_apsen/requirements.txt` | fastapi, uvicorn, requests, pydantic, httpx — versões fixas |
| `integracao_apsen/.env.example` | todas as variáveis da seção 3, comentadas |
| `integracao_apsen/iniciar_estacao.bat` | start no Windows, com venv e aviso de `.env` ausente |
| `integracao_apsen/README.md` | como subir, as rotas e as três armadilhas da integração |
| `integracao_apsen/testes/__init__.py` | marcador de pacote (vazio) |
| `integracao_apsen/testes/test_integracao.py` | 36 conferências, sem webcam e sem o central |

### Alterados

**Nenhum arquivo do algoritmo de visão.** Conferido por hash: `src/*.py`,
`tests/teste_pipeline.py` e `config/mesa.json` estão byte a byte iguais aos que
já estavam na máquina antes desta tarefa. A suíte do pipeline continua com o
mesmo resultado de antes (`LOCK 0`, `ENCOSTADAS 0`, `MESA 0`, `GATE 0`,
`CANTOS 0`, `AUTOLUZ 0`, `BOTOES 0`, `LAYOUT 0`; as 2 falhas de `MANUAL` são as
conhecidas de antes, do limiar fixo em cena com reflexo).

Um arquivo de **empacotamento** foi atualizado, fora da estação:

- `../gerar_arquitetura.py` — o gerador do projeto. Passou a criar as pastas
  `integracao_apsen/` e `integracao_apsen/testes/` e a embutir os 13 arquivos
  novos (de 15 para 28). Mudança no código do gerador:

```diff
-PASTAS = ["src", "tests", "config", "dados"]
+PASTAS = ["src", "tests", "config", "dados",
+          "integracao_apsen", "integracao_apsen/testes"]
```

```diff
   7. python src/main.py
 
 Sem camera, para conferir que tudo funciona:
   python tests/teste_pipeline.py
+
+Para ligar a estacao no PC central APSEN (camada de integracao):
+  pip install -r integracao_apsen/requirements.txt
+  copy integracao_apsen\.env.example integracao_apsen\.env   (e ajuste o ADAPTER_URL)
+  python -m integracao_apsen.servidor
+  python integracao_apsen/testes/test_integracao.py    (sem camera e sem o central)
```

O resto da diferença é o dicionário `ARQUIVOS`, que é conteúdo comprimido em
base64 e não código. Conferido: gerar do zero reproduz os 28 arquivos byte a
byte, e a cópia gerada passa nos testes de integração.

---

## 10.5 Exemplos reais de payload

Copiados do log do adapter falso, numa sequência de três capturas da mesma OS.

**`leitura_mesa_ok`** — primeiro slot, 2 caixinhas na caixa:

```json
{
  "tipo": "leitura_mesa_ok",
  "camera": "mesa",
  "slot_id": 1,
  "os_id": "OS-20261001-0042",
  "quantidade_esperada": 2,
  "quantidade_detectada": 2,
  "delta": 0,
  "confianca": 0.978,
  "ts": "2026-10-01T23:16:32.973747+00:00",
  "posicao_x": 120.0,
  "posicao_y": 45.0,
  "versao_modelo": "visao_mesa-opencv-classico",
  "tempo_total_ms": 3069,
  "frames_lidos": 5,
  "frames_concordantes": 5,
  "estabilidade": 1.0,
  "cobertura": 0.987,
  "patamar": 44,
  "tempo_processamento_ms": 2568,
  "limiar_conteudo": 128,
  "metodo_conteudo": "otsu_desloc",
  "nitidez": 39.9,
  "estourado_pct": 0.41,
  "quantidade_total_caixa": 2,
  "quantidade_total_anterior": 0
}
```

**`leitura_mesa_divergencia`** — o slot devia ter soltado 4 e só caiu 1 (total
da caixa foi de 2 para 3):

```json
{
  "tipo": "leitura_mesa_divergencia",
  "camera": "mesa",
  "slot_id": 3,
  "os_id": "OS-20261001-0042",
  "quantidade_esperada": 4,
  "quantidade_detectada": 1,
  "delta": -3,
  "confianca": 0.978,
  "ts": "2026-10-01T23:16:37.137316+00:00",
  "posicao_x": 120.0,
  "posicao_y": 45.0,
  "versao_modelo": "visao_mesa-opencv-classico",
  "tempo_total_ms": 3026,
  "frames_lidos": 5,
  "frames_concordantes": 5,
  "estabilidade": 1.0,
  "cobertura": 0.986,
  "patamar": 44,
  "quantidade_total_caixa": 3,
  "quantidade_total_anterior": 2
}
```

**`leitura_mesa_falha`** — câmera tampada:

```json
{
  "tipo": "leitura_mesa_falha",
  "camera": "mesa",
  "slot_id": 6,
  "os_id": "OS-20261001-0042",
  "quantidade_esperada": 1,
  "motivo": "obstrucao_visual",
  "quantidade_detectada": 0,
  "delta": -1,
  "posicao_x": 120.0,
  "posicao_y": 45.0,
  "confianca": 0.0,
  "falha_injetada": false,
  "ts": "2026-10-01T23:16:38.822308+00:00",
  "versao_modelo": "visao_mesa-opencv-classico",
  "tempo_total_ms": 505,
  "frames_lidos": 1,
  "motivo_visao": "quadro sem contraste (lente tampada, luz apagada ou cabo solto)"
}
```

Um quarto, do servidor rodando de verdade (uvicorn na 8203) sobre a foto real da
bancada (`dados/bancada_caixa.png`, 3 caixinhas numa caixa de papelão):
`leitura_mesa_ok`, `quantidade_detectada: 3`, `confianca: 0.919`,
`tempo_total_ms: 2159`.

---

## 10.6 Como trazer para o repositório do PC central

### Pasta e árvore

Copiar para `vision-station/` na raiz do `valoryapsen`, no mesmo molde do
`painel_operador/`:

```
vision-station/
├── README.md                     (o da raiz da estação)
├── NOTAS_CALIBRAGEM.md
├── requirements.txt              opencv-contrib + numpy
├── camera_finder.py
├── iniciar_estacao.bat           ← copiar de integracao_apsen/ para a raiz
├── config/
│   └── mesa.json                 calibragem da bancada (versionar: é o estado bom conhecido)
├── src/                          o algoritmo, intocado
│   ├── visao_mesa.py  camera.py  fluxo.py  desenho.py
│   ├── autoluz.py     editor_fundo.py
│   ├── calibrar.py    main.py    __init__.py
├── integracao_apsen/             a camada nova
│   ├── __init__.py  config.py  eventos.py  cliente.py
│   ├── contagem.py  servidor.py  fake_adapter.py
│   ├── requirements.txt  .env.example  README.md  RELATORIO_INTEGRACAO.md
│   └── testes/__init__.py  testes/test_integracao.py
└── tests/
    └── teste_pipeline.py
```

**Não versionar:** `.venv/`, `__pycache__/`, `integracao_apsen/.env` (tem IP de
rede interna), `dados/` (fotos de bancada e saídas de teste — são MB de imagem
que ninguém revisa) e qualquer peso de modelo quando o YOLO entrar. As fotos de
bancada ficam fora do repo; se precisarem ser compartilhadas, um drive da
equipe, com o link no README. Sugestão de `.gitignore`:

```
.venv/
__pycache__/
*.pyc
integracao_apsen/.env
dados/
*.mp4
```

### Dependências

- **Python 3.11** (é o da máquina da câmera hoje).
- `requirements.txt` da raiz: `opencv-contrib-python>=4.8,<5`, `numpy>=1.24,<3`,
  `pygrabber>=0.2` (só Windows, opcional).
- `integracao_apsen/requirements.txt`: `fastapi==0.142.2`, `uvicorn==0.46.0`,
  `requests==2.33.1`, `pydantic==2.13.3`, `httpx==0.28.1` (este só para os
  testes). Cada linha tem a faixa compatível no comentário, caso alguma versão
  exata não exista no Python de destino.
- **Driver da câmera:** nenhum além do UVC do Windows. A Logitech C925e é
  classe padrão. O backend usado é o **DSHOW**, que é o único que expõe foco e
  exposição nessa webcam — está fixado em `src/camera.py`.
- **CUDA:** não se aplica. É OpenCV clássico em CPU.

### Como rodar

```bat
cd vision-station
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -r integracao_apsen\requirements.txt
copy integracao_apsen\.env.example integracao_apsen\.env
notepad integracao_apsen\.env        :: ajustar ADAPTER_URL
iniciar_estacao.bat
```

Para deixar de pé sem ninguém logado: Agendador de Tarefas do Windows,
"ao iniciar o computador", executando o `.bat` com "executar estando o usuário
conectado ou não". Serviço com NSSM também serve.

### Onde ela roda

**Na máquina em que a webcam está fisicamente ligada.** Não é preferência: a
estação precisa do dispositivo USB. Dois casos:

- **Câmera no próprio mini PC** → a estação roda no mini PC, ao lado do Docker.
  Então `ADAPTER_URL=http://127.0.0.1:8102`, e do lado do central o
  `VISION_SIM_URL` precisa ser **`http://host.docker.internal:8202`** — de
  dentro do contêiner, `127.0.0.1` é o próprio contêiner, não o Windows.
- **Câmera na mini HP separada** → a estação roda lá, `ADAPTER_URL` aponta para
  o IP do mini PC, e `VISION_SIM_URL` aponta para o IP da mini HP.

Rodar a estação dentro de um contêiner Docker no Windows **não** funciona para
este caso: repassar USB para contêiner Linux no Docker Desktop não é suportado.

### O que muda do lado do PC central (só descrição, nada editado)

1. `docker-compose.yml`, serviço `vision-adapter`:
   - `VISION_SIM_URL: http://vision-simulator:8202` → `http://host.docker.internal:8202`
     (ou `http://<IP_DA_ESTACAO>:8202`);
   - no Windows, acrescentar ao serviço:
     `extra_hosts: ["host.docker.internal:host-gateway"]`;
   - tirar `depends_on: vision-simulator`, ou mover o simulador para o profile
     `simulado` — senão o compose sobe um simulador que ninguém usa, e ele
     ocupa a 8202 se estiver com porta publicada.
2. **Câmeras de SKU:** a estação responde 501. Duas saídas, escolha de vocês:
   manter o `vision-simulator` no ar **só** para `/executar/capturar/dispenser`
   (o adapter precisaria rotear por rota), ou desligar a etapa de SKU e aceitar
   que o slot segue sem validação de produto. O central já trata a recusa.
3. **Firewall do Windows:**
   - no mini PC: liberar **entrada na 8102** (o adapter recebe os eventos);
   - na máquina da câmera: liberar **entrada na 8202** (o adapter manda o
     comando). Na primeira execução o Windows abre o diálogo para o
     `python.exe` — aceitar para rede **privada**. Se a máquina estiver com o
     perfil "Rede pública", a regra não vale; conferir em
     `Configurações → Rede → Propriedades`.
   - Regra via linha de comando, como Administrador:
     `netsh advfirewall firewall add rule name="APSEN visao 8202" dir=in action=allow protocol=TCP localport=8202`

### Checklist de ponta a ponta

1. Na máquina da câmera: `python camera_finder.py` e conferir que o índice em
   `config/mesa.json` é o da webcam externa.
2. `python src/calibrar.py` — contorno na caixa, contagem batendo com o número
   real de caixinhas, `SALVAR TUDO`.
3. `python -m integracao_apsen.servidor` e, de **outra** máquina da rede,
   `curl http://<IP_DA_ESTACAO>:8202/ping` → `{"status":"ok",...}`.
   Se não responder de fora mas responder local, é firewall.
4. No mini PC: `docker compose up -d vision-adapter` com o `VISION_SIM_URL` novo;
   conferir no log do adapter que o `/ping` dele à estação passou.
5. Pôr 2 caixinhas na caixa de coleta e disparar um comando manual:
   `curl -X POST http://<IP>:8202/executar/capturar/mesa -H "Content-Type: application/json" -d "{\"slot_id\":1,\"os_id\":\"OS-TESTE\",\"quantidade_esperada\":2}"`
   → o evento tem de aparecer no log do adapter em segundos.
6. Rodar uma **OS de verdade com 2 slots** e conferir: o primeiro evento traz
   `quantidade_total_caixa` igual ao total na caixa, o segundo traz o incremento
   certo (e não o total).
7. Tirar uma caixinha no meio da OS → `leitura_mesa_falha` com
   `contagem_regrediu`, e a OS **não** trava por causa disso.
8. Soltar um slot a menos de propósito → `leitura_mesa_divergencia` e a OS trava
   esperando o supervisor. É o teste que prova que o Triple Check está ligado.
9. Derrubar a estação no meio de uma OS → o central tem de desistir daquela
   câmera em 30 s, sem derrubar o resto.
10. Subir a estação de novo e repetir o passo 5, para confirmar que ela volta
    sozinha sem precisar reiniciar o adapter.

---

## 10.7 Riscos e pendências

**Tempo de processamento na máquina de verdade.** Aqui, 5 frames levam ~2,5 s
(1,5 s o primeiro, ~0,3 s os demais). A mini HP é mais lenta; se der 3× isso,
são ~8 s — dentro dos 20 s de teto e dos 30 s do central, mas sem folga grande.
**Teste isto primeiro**: rode uma captura e olhe `tempo_total_ms` no evento. Se
passar de 10 s, baixe `FRAMES_POR_CAPTURA` para 3 (perde-se precisão da
estabilidade, não da contagem).

**Caixinhas empilhadas.** O algoritmo conta retângulos vistos de cima. Duas
embalagens **empilhadas** contam como uma, e nenhum ajuste de limiar resolve —
é limitação da câmera única de topo. Se a caixa de coleta for funda e os
medicamentos puderem cair um sobre o outro, a contagem vai divergir para menos.
É o risco número 1 desta integração, e aparece como divergência (trava a OS),
não como falha. *Mitigação:* caixa rasa, ou aceitar que o peso é quem resolve
esse caso no Triple Check.

**Caixinhas encostadas.** Já tratado pelo corte de junções (há teste para 0°,
15°, 30°… 90° e vãos de 0 a 3 mm), mas foi validado em cena sintética. Vale
repetir na bancada com as embalagens reais da Apsen.

**Reflexo e iluminação.** É o que mais derruba leitura. A estação recusa em vez
de errar (vira falha, não divergência), mas uma luminária mal posicionada pode
gerar falha atrás de falha e, na prática, desligar a câmera do Triple Check sem
ninguém perceber. *Recomendo* olhar o `/status` depois da primeira OS: se
`contadores.falha` estiver alto, é luz, não software. A aba 3 do `calibrar.py`
tem o auto-ajuste de exposição e foco.

**Oclusão pela própria mesa CNC.** A captura acontece depois que o slot solta; se
o braço ou a estrutura ficar sobre a caixa, o total cai e sai
`contagem_regrediu`. O acumulado não é atualizado, então a captura seguinte se
recupera sozinha — mas aquele slot fica sem confirmação da câmera. Vale conferir
na bancada se a posição de captura deixa a caixa desobstruída.

**Caixa de coleta não-vazia no começo da OS.** Hoje é suposição (10.3). Se
acontecer, **todas** as contagens da OS saem erradas para menos, e a primeira
vira divergência. Fácil de detectar: o primeiro evento da OS traz
`quantidade_total_caixa` maior que `quantidade_detectada`.

**Captura travada no driver.** O vigia manda a falha, mas a fila para (10.2,
item 4). Se acontecer na bancada, a saída é reiniciar a estação — e aí vale
implementar a captura em subprocesso.

**A estação não valida que a câmera está olhando a caixa certa.** Se alguém
mover o tripé, ela continua contando, só que outra coisa. O modo `manual` com os
quatro cantos marcados ajuda (o quadrilátero fica fixo), mas não há alarme.
*Pendência:* um evento de telemetria quando a área útil mudar de posição seria o
jeito certo de avisar.
