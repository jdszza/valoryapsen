# Integração com o PC central APSEN

Camada fina que faz a estação de visão da mesa falar o **mesmo idioma HTTP do
`vision-simulator`**, para que do lado do PC central baste trocar um endereço.

**O algoritmo de contagem não foi tocado.** Esta pasta só recebe o comando,
chama `VisaoMesa.processar()` como ele já é, traduz o resultado para o formato
de evento do central e manda de volta. Nenhum arquivo de `src/` foi alterado.

```
PC central ──POST──▶ vision-adapter (:8102) ──POST──▶ ESTA ESTAÇÃO (:8202)
                                                              │
PC central ◀──POST── vision-adapter ◀──── POST /eventos ──────┘
```

O fluxo é **assíncrono**: o comando de captura só dispara o trabalho e responde
na hora (< 10 ms medidos); o resultado volta depois, num POST separado.

## Subir

```bash
pip install -r requirements.txt                      # opencv + numpy (raiz)
pip install -r integracao_apsen/requirements.txt     # fastapi + uvicorn + requests
cp integracao_apsen/.env.example integracao_apsen/.env   # e ajuste o ADAPTER_URL
python -m integracao_apsen.servidor
```

No Windows: `integracao_apsen\iniciar_estacao.bat`.

A estação sobe e responde `/ping` **mesmo com o PC central desligado e com a
câmera ainda fechada** — a câmera só abre na primeira captura, de propósito: o
adapter chama `/ping` assim que sobe e não pode depender de hardware pronto.

## Rotas

| Rota | O que faz |
|---|---|
| `GET /ping` | `{"status":"ok","service":"apsen-vision-station"}`. Não toca na câmera. |
| `GET /status` | Diagnóstico: câmera pronta, fila, contadores, acumulado por OS, config em uso. |
| `POST /executar/capturar/mesa` | O coração. Responde na hora e captura em segundo plano. |
| `POST /executar/capturar/dispenser` | **HTTP 501** — esta estação não lê SKU (ver o relatório). |

Para cada comando aceito sai **exatamente um** evento: `leitura_mesa_ok`,
`leitura_mesa_divergencia` ou `leitura_mesa_falha`.

## As três coisas que mais quebram numa integração assim

**1. Total da caixa × o que caiu agora.** A câmera vê a caixa inteira; o central
pergunta quanto aquele dispenser acabou de soltar. A estação guarda o último
total por `os_id` e manda a diferença, com o total bruto junto em
`quantidade_total_caixa`. OS nova começa do zero — isto **assume que a caixa de
coleta começa vazia a cada OS**. Se o total cair (caixa mexida, oclusão), sai
falha `contagem_regrediu` e o acumulado **não** é atualizado: se foi oclusão, a
próxima captura fecha a conta sozinha.

**2. Comando repetido.** O central reenvia quando a resposta demora mais de 5 s.
O mesmo `(os_id, slot_id)` responde `200 "captura já em andamento"` e **não**
dispara outra captura — senão o acumulado seria descontado duas vezes e a
divergência apareceria no *slot seguinte*, longe da causa.

**3. Nunca inventar.** Leitura incerta é **falha**, nunca divergência:
divergência trava a OS e chama supervisor. Câmera tampada, câmera caída,
confiança abaixo do mínimo, tempo estourado, exceção — tudo vira
`leitura_mesa_falha` com motivo.

## De onde sai a `confianca`

A visão não produz probabilidade: produz um veredito (`confiavel`) e as medidas
que o sustentam. Em vez de carimbar um `0.95` fixo, a confiança é derivada do
que foi medido de fato:

| Parcela | Peso | O que é |
|---|---|---|
| estabilidade | 0,50 | fração dos N frames que concordaram com o vencedor, normalizada a partir de 60% |
| cobertura | 0,30 | fração da máscara que virou item (leitura correta ≥ 0,80; errada por reflexo ≈ 0,39) |
| patamar | 0,20 | largura da faixa de limiar em que a contagem não muda (só no modo lock) |

Imagem recusada pela visão sai com `confianca: 0.0` e vira falha. Maioria
apertada (2 de 3, ou 3 de 5) fica **abaixo** do mínimo de 0,60 de propósito:
empate disfarçado não vira número.

## Ensaio sem CNC

```bash
python integracao_apsen/fake_adapter.py --arquivo eventos.jsonl     # terminal 1
python -m integracao_apsen.servidor --adapter http://127.0.0.1:8102 # terminal 2
curl -X POST http://127.0.0.1:8202/executar/capturar/mesa ^
     -H "Content-Type: application/json" ^
     -d "{\"slot_id\":1,\"os_id\":\"OS-TESTE-1\",\"quantidade_esperada\":2}"
```

Sem webcam, acrescente `--imagem dados/bancada_caixa.png` ao servidor: ele
repete essa foto no lugar da câmera e **avisa em WARNING a cada captura**. Serve
para ensaiar o caminho HTTP, nunca para operar.

## Testes

```bash
python integracao_apsen/testes/test_integracao.py
```

36 conferências, sem webcam e sem o PC central: HTTP e máquina de estados,
idempotência, acumulado por OS, divergência, regressão, timeout, câmera tampada,
câmera caída, retentativa de envio e a contagem de verdade rodando sobre cenas
sintéticas.
