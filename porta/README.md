# Porta NR-12 — ESP32 + RC522 + 2 fins de curso (porta presa por ímã)

O firmware é [`porta_nr12/porta_nr12.ino`](porta_nr12/porta_nr12.ino); o
contrato é [`docs/PROTOCOLO_SERIAL.md`](../docs/PROTOCOLO_SERIAL.md) §9; o que o
central faz com ele está no CLAUDE.md, seção "Portas NR-12: a porta pede, o
central para e autoriza".

**A placa tem USB PRÓPRIA no mini PC**, e o dono dela é o
[`porta-adapter`](../porta-adapter/main.py) (host, HTTP 8104, subido por
`porta-adapter\iniciar_host.bat` com a COM em `PORTA_SERIAL_URL`). Ela não
passa pela placa das telas TFT: travar ou regravar as telas não cega a vigia da
porta, e vice-versa.

**Não há fechadura elétrica.** Cada porta é presa por um **ímã simples**, e o
fim de curso é o único sensor dela: fechada, a alavanca fica pressionada; aberta,
o fim de curso solta. Quem puxa, abre — a qualquer momento.

**A porta valida a TAG; o central decide se a abertura é AUTORIZADA e se a
máquina RETOMA.** Passar uma tag cadastrada não autoriza nada sozinho: a placa
emite `pedido_acesso`, o central para a CNC e os dispensers, e só com a parada
confirmada manda `porta_liberar` — que acende o LED **"pode abrir"**. A regra de
operação é: **passe a tag, espere o LED acender, abra**. Aberta antes do LED (ou
sem tag), a abertura é **sem autorização**: vira alarme, e a célula para do
mesmo jeito. Sem resposta do central em 5 s, o LED não acende.

## Ligações

Os pinos do RC522 e dos fins de curso são os **validados na bancada** — o
sketch de teste que já funcionava, sem mudar um pino.

| peça | pino ESP32 | observação |
|---|---|---|
| RC522 SDA/SS | D23 | RC522 sempre em **3V3** |
| RC522 SCK | D22 | SPI a 1 MHz (`MFRC522_SPICLOCK`): tolera jumper longo |
| RC522 MOSI | D21 | |
| RC522 MISO | D19 | |
| RC522 RST | D15 | |
| ES1 — **teto** (abastecer remédio) | D4 | COM no GND, NO no pino, `INPUT_PULLUP` |
| ES2 — **frente** (caixa sobre a balança) | D2 | idem + resistor de 10 k entre D2 e 3V3 (o LED da placa puxa o D2 para baixo). **Se a gravação pela USB falhar, mude `PIN_ENDSTOP2` para 33** (D2 é pino de boot) e tire o resistor |
| USB | — | **no mini PC**: a COM fixada vai em `PORTA_SERIAL_URL`. GPIO16/17 ficaram livres |
| LED **"pode abrir"** | D25 | aceso só em LIBERADA: a abertura está autorizada. Sem fechadura, é ele o sinal para a pessoa |

Fechado = alavanca pressionada = LOW. **Fio rompido = HIGH = lido como aberto**
(falha segura). A célula só está fechada com os DOIS fins de curso
pressionados.

## Uma USB, duas vozes

As duas conversas andam na MESMA USB, separadas pelo `{` — a regra do
`apsen_serial.h` (a 4ª cópia, idêntica às outras três) em todas as placas:

* **JSON = máquina.** Linha com `{` é evento do §9, ACK ou comando do central.
  Quem a lê é o `serialPoll()` do header, e só ele.
* **Linha sem `{` = bancada.** Os comandos de um caractere abaixo chegam por
  `linhaHumana()`, **uma letra por linha** (maiúscula ou minúscula, espaços
  ignorados; `RS` numa linha só NÃO vira dois comandos), e os textos para
  humano saem sem `{` — um `{` num log viraria linha de máquina do outro lado.

O `porta-adapter` nunca manda linha sem `{`. E os dois não abrem a COM ao mesmo
tempo: **no Windows a COM abre em um programa por vez.** Para usar os comandos
de bancada pelo Monitor Serial, **PARE o porta-adapter antes** (feche a janela
dele) e suba-o de novo depois de fechar o Monitor. Enquanto ele estiver parado,
o central mantém a célula parada: link perdido é porta aberta.

| comando (Monitor Serial, 115200) | efeito |
|---|---|
| `R` | entra no modo REGISTRO — aproxime a tag a cadastrar |
| `S` | salva a tag lida (dentro do modo registro) |
| `C` | cancela o registro |
| `L` | lista as tags cadastradas |
| `E` | diagnóstico dos fins de curso (nível bruto de cada pino) |

As tags ficam na NVS (até 20) e sobrevivem ao desligamento. São **genéricas**:
o histórico do central registra porta, UID e horário, não a pessoa.

## A máquina de estados

`aguardando_rearme` é um flag à parte do estado: ele fica de pé depois de a
porta ter sido aberta (ou de uma liberação que ninguém usou), e faz a próxima
tag ser `rearme` em vez de `pedido_acesso`.

| estado | evento | próximo estado | mensagem emitida |
|---|---|---|---|
| (boot) | as duas fechadas | FECHADA | `boot`, `estado` |
| (boot) | alguma aberta | ABERTA_SEM_AUTORIZACAO | `boot`, `estado` |
| FECHADA | tag cadastrada, sem rearme pendente | AGUARDANDO_CENTRAL | `pedido_acesso`, `estado` |
| FECHADA | tag cadastrada, com rearme pendente | FECHADA (rearme limpo) | `rearme`, `estado` |
| qualquer | tag não cadastrada | (o mesmo) | `tag_negada` |
| AGUARDANDO_CENTRAL | `porta_liberar` | LIBERADA (LED aceso) | ACK, `liberada`, `estado` |
| AGUARDANDO_CENTRAL | `porta_negar` | FECHADA (sem rearme) | ACK, `estado`, `liberacao_expirada` (motivo recebido) |
| AGUARDANDO_CENTRAL | 5 s sem resposta | FECHADA (sem rearme) | `estado`, `liberacao_expirada` (`central_nao_respondeu`) |
| LIBERADA | `porta_negar` | FECHADA (LED apagado) | ACK, `estado`, `liberacao_expirada` (motivo recebido) |
| LIBERADA | uma porta abre | ABERTA_AUTORIZADA (LED apagado: a autorização foi usada) | `porta_aberta` (`autorizada: true`), `estado` |
| LIBERADA | janela vence sem abrir | FECHADA, **rearme pendente** | `estado`, `liberacao_expirada` (`janela_expirou`) |
| ABERTA_AUTORIZADA | a outra porta abre | ABERTA_AUTORIZADA | `porta_aberta` (`autorizada: true`) |
| FECHADA ou AGUARDANDO_CENTRAL | uma porta abre | ABERTA_SEM_AUTORIZACAO | `porta_aberta` (`autorizada: false`), `estado` |
| ABERTA_SEM_AUTORIZACAO | a outra porta abre | ABERTA_SEM_AUTORIZACAO | `porta_aberta` (`autorizada: false`) |
| ABERTA_* | as duas fecham | FECHADA, **rearme pendente** | `porta_fechada`, `estado` |
| qualquer | `porta_estado` | (o mesmo) | ACK, `estado` |
| fora de AGUARDANDO_CENTRAL | `porta_liberar` | (o mesmo) | ACK negativo `sem_pedido_pendente` |
| qualquer | a cada 500 ms | (o mesmo) | `estado` (heartbeat) |

**O LED "pode abrir" segue o estado: aceso só em LIBERADA**, e só o
`porta_liberar` leva até lá. Toda volta do laço o reescreve a partir do estado
(`atualizarLed`), e o boot o apaga antes de qualquer outra linha — um reset
nunca diz a ninguém que pode abrir.

**Sem resposta do central, a abertura não é autorizada.** `ESPERA_CENTRAL_MS` (5 s) é
maior que o teto de `PORTA_PARADA_MAX_S` do central (4 s) com folga para o
trajeto — é um limite superior, e `tests/test_porta_firmware.py` cobra a
relação.

## Checklist de bancada

1. **Ímã e fim de curso**: com a porta fechada pelo ímã, a alavanca tem de ficar
   PRESSIONADA (`E` mostra FECHADO); aberta, tem de soltar. Folga no ímã que
   deixe a alavanca no limiar vira porta "piscando" aberta/fechada — ajuste a
   posição do fim de curso até o `E` ficar estável nas duas posições.
2. **USB da porta no mini PC**: anote a COM no Gerenciador de Dispositivos
   (fixe o número em *Propriedades → Configurações de Porta → Avançado…*) e
   defina `PORTA_SERIAL_URL` no `porta-adapter\iniciar_host.bat`. Ela não pode
   coincidir com a COM dos mecanismos, das telas, da CNC nem da balança. Não
   há mais fio entre esta placa e a das telas.
3. **Identificar as portas**: ES1 (D4) = teto, ES2 (D2) = frente. Teste com
   `E` no Monitor Serial da USB, abrindo uma de cada vez.
4. **D2 e gravação**: se o upload falhar com o resistor de 10 k no D2, mova o
   ES2 para o GPIO33 e troque `PIN_ENDSTOP2` (sem o resistor).
5. **Cadastrar as tags**: **pare o porta-adapter**, abra o Monitor Serial na
   COM da porta, `R` → aproxime a tag → `S`, confira com `L`, feche o Monitor
   e suba o adapter de novo. No Windows a COM só abre em um programa por vez.
6. **LED "pode abrir" visível** de onde a pessoa passa a tag: é o único sinal
   de que a máquina já parou e a abertura está autorizada.
7. **Teste de link**: com o adapter no ar, `http://localhost:8104/health` mostra
   `"link_ok": true` e o heartbeat com menos de 1 s. Puxe o cabo USB da porta:
   o central tem de parar a célula em até ~2,5 s (`PORTA_HEARTBEAT_MAX_S`).
8. **Teste do "para tudo"** com a mesa andando: abrir a porta da frente no meio
   de um movimento e medir quanto a mesa ainda anda até parar.

## Parada só por software — decisão de protótipo

Aceita para o protótipo do desafio. **Em produção, uma auditoria NR-12 exige o
fim de curso cortando fisicamente a energia dos motores** (relé de segurança):
aqui, quem para a máquina é o central, por comando, e um central travado não
para nada. E a porta não tem trava: o ímã segura, mas não impede ninguém de
abrir — por isso toda abertura sem o "pode abrir" é registrada como sem
autorização e para a célula.
