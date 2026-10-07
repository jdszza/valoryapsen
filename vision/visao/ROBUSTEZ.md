# Fazendo funcionar com câmera ruim

Resumo do que foi feito e, principalmente, **em que ordem mexer** quando a
leitura falha por ângulo ou iluminação.

## O ganho medido

`python tests/benchmark_robustez.py` roda a etiqueta real por 12 cenários de
degradação (ângulo, desfoque, luz baixa, reflexo, ruído, compressão JPEG):

| cenário | QR sozinho | + cascata | + ArUco |
|---|---:|---:|---:|
| ideal | 100% | 100% | 100% |
| código com 55 px | 83% | 100% | 100% |
| ângulo 55° | 100% | 100% | 100% |
| luz baixa | 75% | 100% | 100% |
| luz baixa + ângulo 30° | 33% | 100% | 100% |
| reflexo forte | 100% | 100% | 100% |
| desfoque forte | 0% | 100% | 100% |
| webcam ruim (tudo junto) | 0% | 0% | 100% |
| **total** | **74%** | **91%** | **100%** |

E ficou mais rápido, não mais lento: **~1,6 ms por frame** contra ~30 ms da
versão anterior, porque agora só os recortes dos dispensers são decodificados,
e só quando a imagem daquela zona muda.

---

## As cinco mudanças

### 1. Etiqueta híbrida: QR + dois marcadores ArUco

Esta é a mudança que mais importa. QR e ArUco falham por motivos diferentes:

| | QR code | ArUco |
|---|---|---|
| ângulo forte | razoável | ótimo |
| desfoque / fora de foco | ruim | ótimo |
| pouca luz | razoável | ótimo |
| código pequeno na imagem | ruim | ótimo |
| **reflexo especular** | **ótimo** (30% de correção de erro) | ruim |

Um QR de 21×21 módulos precisa de ~3 px por módulo para ser lido. Um ArUco
4×4 tem 6 módulos de lado — no mesmo espaço os módulos ficam muito maiores, e
por isso ele sobrevive a desfoque e distância que apagam o QR. Em compensação,
o ArUco não tem correção de erro robusta e morre com uma mancha de reflexo em
cima — situação em que o QR passa tranquilo.

Por isso a etiqueta traz os dois, e **dois** ArUco em cantos opostos: reflexo é
quase sempre uma mancha localizada, então a que apaga um marcador deixa o outro
legível.

O QR continua sendo o código "oficial" — o sistema prefere ele quando os dois
são lidos. O ArUco só entra quando o QR falha. Se você não quiser o marcador,
`python src/gerar_qrcodes.py --sem-aruco` ou `"aruco": null` no cadastro.

### 2. Cascata de pré-processamento

Em vez de uma tentativa, o sistema gera versões tratadas do recorte e para na
primeira que decodificar:

```
direto → x2+adaptativo → ArUco → realce → x2+clahe → x3+adaptativo → ...
```

A variante que resolveu fica memorizada por dispenser e vai para a frente da
fila no frame seguinte. A que mais recupera é `x2+adaptativo` (ampliar 2× e
binarizar por limiar adaptativo): sozinha ela leva "desfoque forte" e "código
de 55 px" de 0% para 100%.

### 3. Leitura por zona, não do frame inteiro

Como as zonas já estão calibradas, o sistema recorta cada dispenser, amplia até
um tamanho útil e decodifica só isso. Um recorte de 300×500 custa 3 ms; o frame
inteiro custa 22 ms — e o código ocupa uma fração muito maior da imagem, o que
por si só melhora a leitura. Uma varredura do frame completo roda uma vez por
segundo, só para pegar código fora das zonas.

### 4. Confirmação por leitura, não por frame consecutivo

Antes o alerta exigia N frames **seguidos** com o erro. Se a câmera só
decodifica 1 frame em cada 8, isso nunca acontece — o sistema ficava mudo
justamente na câmera ruim. Agora o contador sobe a cada leitura bem-sucedida,
não importa quantos frames falharam no meio.

### 5. Memória temporal por dispenser

Dispenser é estático: a caixa que estava ali há 300 ms continua ali. Depois de
ler, a zona guarda o estado por `memoria_segundos` (padrão 2 s). Isso acaba com
o status e o alerta piscando quando a leitura é intermitente. Leitura vinda da
memória aparece com contorno fino e a marca `(memoria 0.8s)`, e **não** gera
linha nova no log — só sustenta o estado.

---

## Diagnóstico: descobrir qual é o problema

```bash
python src/diagnostico.py --segundos 30
```

Mede, por dispenser: taxa de leitura, nitidez, brilho, contraste, reflexo,
tamanho aparente do código e qual variante conseguiu ler. E traduz em ação:

```
DISPENSER 3 — Ibuprofeno 400mg
  taxa de leitura ..  34.2%   (QR 12, ArUco 91)
  nitidez ..........     31   (min 60)
  reflexo ..........    7.4%  (max 3%)
  -> imagem fora de foco (nitidez 31, minimo 60). Fixe o foco em
     parametros.json (autofoco: false, foco: <valor>)
  -> reflexo forte (7.4% da zona saturada em branco). Mude o angulo da luz
```

Se `ArUco 91, QR 12`, o QR está sendo carregado pelo marcador — sinal de que
vale imprimir maior ou melhorar a luz, mesmo o sistema estando funcionando.

---

## Ordem de ataque quando ainda falhar

Do mais eficaz para o menos, na prática:

**1. Trave foco e exposição.** Em câmera barata isso costuma valer mais que
qualquer algoritmo. O autofoco fica caçando foco a cada movimento e a
autoexposição muda o brilho quando alguém passa na frente. Em
`config/parametros.json`:

```json
"camera": { "autofoco": false, "foco": 30, "autoexposicao": false, "exposicao": -6 }
```

Os valores variam por modelo — ajuste olhando a imagem do `calibrar.py`. Se a
câmera não aceitar, o programa avisa e ignora.

**2. Aumente o código impresso.** `--tamanho-mm 45` ou `50`. A regra é o código
ocupar ≥ 70 px de lado no frame; o diagnóstico mostra quantos px você tem.

**3. Arrume a luz.** Difusa e lateral, nunca um ponto forte de frente — reflexo
especular no plástico é o defeito que mais mata leitura. Etiqueta em papel
fosco; papel brilhante e plástico transparente por cima pioram muito.

**4. Suba a resolução.** `"largura": 1920, "altura": 1080` multiplica os pixels
do código. Custa pouco: a leitura é feita nos recortes, não no frame inteiro.

**5. Reduza o ângulo.** Até ~55° o sistema aguenta bem. Acima disso, a brecha
do dispenser começa a esconder parte do código — é problema mecânico, não de
software: gire o dispenser ou a câmera.

**6. Afrouxe a confirmação.** Se a leitura é boa mas o alerta demora:

```json
"alertas": { "frames_para_confirmar": 2, "memoria_segundos": 3.0 }
```

---

## Se a CPU for fraca (Raspberry Pi)

O custo médio já é baixo (~2 ms/frame) porque a leitura pesada só roda quando a
imagem da zona muda. Se ainda assim faltar CPU:

```json
"deteccao": {
  "cascata_completa": false,   // só as variantes baratas
  "orcamento_ms": 120,         // teto de tempo por frame
  "intervalo_reavaliacao": 2.0 // reavalia zona parada com menos frequência
}
```

Manter `usar_aruco: true` — ele é barato perto do que resolve, e só é acionado
quando o QR falha.

## Falso positivo de ArUco

O ArUco é detectado de forma permissiva, então textura de texto pode virar um
id inexistente. Duas travas: marcador menor que `lado_minimo_aruco` (16 px) é
descartado, e **id ArUco fora do catálogo é ignorado em silêncio** em vez de
virar alerta. QR desconhecido continua alertando, porque QR não gera falso
positivo. Alerta falso é pior que uma leitura a menos.


---

## Limites de escala e de orientacao (medido)

`python tests/benchmark_pose.py` mede onde cada canal para de funcionar.

### Tamanho na imagem

| canal | menor tamanho com 100% de leitura |
|---|---|
| ArUco | **25 px** de lado do marcador |
| QR | **40 px** de lado do codigo |
| Embalagem (reconhecimento visual) | **~340 px** de largura da caixa no recorte |

O reconhecimento visual e, de longe, o canal mais exigente em resolucao: ele
precisa enxergar textura fina (texto da bula, arte), enquanto o codigo so
precisa distinguir modulos pretos e brancos.

### Rotacao no plano (caixa deitada, de ponta-cabeca, na diagonal)

| giro | QR | ArUco | embalagem |
|---|---|---|---|
| 0, 90, 180, 270 | OK | OK | OK |
| 15, 30, 45, 60, 135 | OK | OK | falha |

QR e ArUco sao invariantes a rotacao por construcao — os padroes de
posicionamento do QR e a codificacao de orientacao do ArUco existem justamente
para isso. Nao ha nada a fazer nesse canal.

O reconhecimento de embalagem so cobre os quatro angulos retos com o cadastro
padrao. **Mitigacao medida: cadastrar duas fotos giradas (45 e 90 graus) leva
os angulos intermediarios de 9/40 para 33/40, sem nenhum falso positivo novo.**
Se as caixas puderem cair em qualquer angulo, cadastre giradas.

### Inclinacao em relacao a camera (perspectiva)

| inclinacao | QR | ArUco | embalagem |
|---|---|---|---|
| 0 a 65 graus | OK | OK | ate ~40 graus |
| 75 graus | falha | OK | falha |

### O ArUco ainda vale a pena?

Vale — e nao e substituivel pelo reconhecimento visual. Em seis cenarios
dificeis (codigo pequeno, inclinado, desfocado, escuro):

| combinacao | cobertura |
|---|---|
| QR sozinho | 50% |
| QR + ArUco | 100% |
| embalagem sozinha | 0% (a caixa fica pequena demais nesses cenarios) |

Sao papeis diferentes: o **ArUco** resolve codigo pequeno, torto e desfocado; o
**reconhecimento de embalagem** resolve o caso em que nao ha codigo legivel
nenhum — e so funciona quando a caixa aparece grande no recorte.

### Dois defeitos encontrados e corrigidos nesta rodada

Os testes acima expuseram duas falhas reais no reconhecimento visual:

1. **Normalizacao por altura quebrava com a caixa girada 90 graus.** A imagem
   virava retrato, a altura passava a ser o lado maior e o conteudo chegava numa
   escala diferente da cadastrada. Corrigido normalizando pela raiz da area, que
   e invariante a rotacao.
2. **A conferencia de pixel comparava a imagem crua contra uma homografia
   calculada no espaco normalizado.** So funcionava por coincidencia, quando o
   recorte ja chegava no tamanho exato do cadastro. Era isso que fazia o
   reconhecimento morrer assim que a caixa mudava de distancia.

Depois das correcoes, o reconhecimento passou a funcionar numa faixa de
distancia de cerca de 3x (de ~340 px a mais de 1000 px de largura da caixa),
com consulta em multiplas escalas e continuando com **zero falso positivo**.
