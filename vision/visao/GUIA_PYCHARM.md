# Guia rápido — abrir, configurar e testar no PyCharm

## 1. Abrir o projeto

1. Descompacte `visao_dispensers.zip` numa pasta sua (ex: `C:\projetos\visao_dispensers`).
2. PyCharm → **File → Open** → selecione a pasta **`visao_dispensers`**
   (a pasta que contém `README.md`, `src/`, `config/`), não a pasta `src`.
3. Clique com o botão direito na pasta **`src`** → **Mark Directory as → Sources Root**.
   Sem isso o PyCharm sublinha de vermelho os `import configuracao`, `import detector` etc.
   O código roda mesmo assim (cada script ajusta o `sys.path`), mas o autocomplete quebra.

## 2. Criar o interpretador e instalar as dependências

**Settings → Project → Python Interpreter → Add Interpreter → Add Local → Virtualenv**
(Python 3.10 ou superior, `Base interpreter` apontando para sua instalação).

Depois, no **Terminal do PyCharm** (aba de baixo, já dentro do venv):

```bash
pip install -r requirements.txt
```

Dependência nativa do leitor de QR (`pyzbar`):

| Sistema | Comando |
|---|---|
| Windows | já vem no wheel. Se der `ImportError: Unable to find zbar`, instale o [Visual C++ Redistributable 2013](https://www.microsoft.com/download/details.aspx?id=40784) (x64 **e** x86) |
| Linux / Raspberry Pi | `sudo apt install libzbar0` |
| macOS | `brew install zbar` |

Se não conseguir instalar, o sistema cai sozinho no detector do OpenCV — funciona,
só é menos tolerante a QR pequeno ou com reflexo. Para conferir qual está ativo:

```bash
python -c "import sys; sys.path.insert(0,'src'); from leitor_qr import backend_disponivel; print(backend_disponivel())"
```

## 3. Criar as Run Configurations

**Run → Edit Configurations → + → Python**, uma para cada script abaixo.
Em todas: **Working directory = a pasta raiz do projeto**.

| Nome | Script | Parameters |
|---|---|---|
| `1 - Testes` | `tests/test_sistema.py` | — |
| `2 - Gerar QR` | `src/gerar_qrcodes.py` | `--tamanho-mm 30 --copias 8` |
| `3 - Calibrar` | `src/calibrar.py` | — |
| `4 - Rodar` | `src/main.py` | — |
| `5 - Cena de teste` | `tests/cena_sintetica.py` | `--trocar 1 3` |

> Rode `tests/test_sistema.py` com o **Run** normal (▶), não com o pytest.
> É um script comum; se o PyCharm oferecer "Run pytest in test_sistema.py", troque
> em Edit Configurations → Python (não "Python tests").

**Comece pela configuração `1 - Testes`.** Ela valida o sistema inteiro sem
câmera e sem dispenser montado. Você deve ver 20 linhas `[PASS]` e
`Todos os testes passaram.`

---

## 4. Onde eu emito os QR codes?

Rode a configuração **`2 - Gerar QR`** (ou `python src/gerar_qrcodes.py`).
Saída na pasta **`qrcodes/`**:

- `MED-001_Dipirona-500mg.png` … uma etiqueta por medicamento, com legenda
- `folha_etiquetas.pdf` … folha A4 com 8 cópias de cada, pronta para imprimir

Imprima em **100%** / "Tamanho real" — nunca "Ajustar à página", senão o QR sai
menor do que o previsto.

### Testando na tela, sem impressora

Para os primeiros testes você não precisa imprimir nada:

- abra `qrcodes/MED-001_Dipirona-500mg.png` no celular, em tela cheia, e aponte
  a webcam; ou
- gere a bancada inteira simulada e mostre num segundo monitor:

  ```bash
  python tests/cena_sintetica.py --saida bancada.png
  ```

  Abra `bancada.png` em tela cheia, aponte a webcam para o monitor e calibre em
  cima dela. É o jeito mais rápido de ver o sistema funcionando de ponta a ponta
  antes de ter o hardware.

### Posso usar outro gerador de QR?

Pode — qualquer gerador serve. A única exigência é que o **conteúdo** do código
seja exatamente igual ao campo `qr` do cadastro, por exemplo o texto `MED-001`.
O `gerar_qrcodes.py` só facilita: ele já usa correção de erro alta (aguenta
sujeira e reflexo) e coloca a legenda com o dispenser de destino na etiqueta.

---

## 5. Onde eu associo cada QR code ao seu dispenser?

Arquivo **`config/medicamentos.json`**. É aqui que mora a regra "este
medicamento pertence ao dispenser N":

```json
{
  "medicamentos": [
    { "qr": "MED-001", "nome": "Dipirona 500mg",   "dispenser": 1 },
    { "qr": "MED-002", "nome": "Paracetamol 750mg","dispenser": 2 },
    { "qr": "MED-003", "nome": "Ibuprofeno 400mg", "dispenser": 3 },
    { "qr": "MED-004", "nome": "Amoxicilina 500mg","dispenser": 4 }
  ]
}
```

- `qr` — o conteúdo impresso no código. Todas as caixas do mesmo tipo levam este
  mesmo código.
- `dispenser` — o número do dispenser onde ele **deve** estar.

Troque os nomes pelos medicamentos reais e rode o `gerar_qrcodes.py` de novo.
Se você cadastrar dois medicamentos no mesmo dispenser, o programa recusa a
configuração na hora — é a regra "uma pilha, um tipo só".

---

## 6. Onde eu digo a posição de cada dispenser na imagem?

Isso é a **calibração**, e é a configuração `3 - Calibrar` (`src/calibrar.py`).
São coisas diferentes e complementares:

| Arquivo | Responde | Como se edita |
|---|---|---|
| `config/medicamentos.json` | *qual medicamento* pertence ao dispenser 3 | você digita |
| `config/zonas.json` | *onde na imagem* fica o dispenser 3 | desenhando na tela |

Com a câmera apontada para a bancada, rode `3 - Calibrar`. Abre uma janela com a
imagem ao vivo:

1. **Arraste o mouse** em volta da brecha do primeiro dispenser → nasce um retângulo.
2. Com ele selecionado, aperte **`1`** para dizer que aquele é o dispenser 1.
3. Repita para os outros três (teclas `2`, `3`, `4`).
4. Ajuste: arraste de dentro para **mover**, arraste um dos 4 cantos para
   **redimensionar**, ou use as **setas** / `W A S D` para o ajuste fino
   (maiúsculas movem 10 px de uma vez).
5. **`ENTER`** salva em `config/zonas.json`.

Atalhos que ajudam:

- **`G`** cria 4 retângulos iguais em coluna — bom ponto de partida, aí você só ajusta.
- **`F`** congela a imagem, para posicionar com calma sem a cena mudando.
- **`TAB`** passa para a próxima zona; **`X`** apaga a selecionada; **`R`** apaga todas.
- **`H`** mostra/esconde a lista de comandos na tela.

Enquanto você calibra, os QR codes visíveis já são lidos e coloridos:
**verde** = está no dispenser certo, **vermelho** = está no dispenser errado,
**amarelo** = fora de qualquer zona ou não cadastrado. Ou seja, dá para conferir
o acerto do retângulo na hora, sem sair da ferramenta.

Sem câmera ainda? Calibre sobre uma foto:

```bash
python src/calibrar.py --imagem bancada.png
```

### Detalhe importante da regra

A decisão usa o **centro** do QR code. Se o centro cai dentro do retângulo do
dispenser 3, o sistema considera que aquele medicamento está no dispenser 3.
Por isso: deixe o retângulo **um pouco maior que a brecha**, mas **sem encostar
no vizinho**. Retângulos sobrepostos geram um aviso ao salvar.

---

## 7. Rodar de verdade

Configuração **`4 - Rodar`** (`src/main.py`). A janela mostra as 4 zonas, o QR
lido em cada uma e um painel lateral com o status. Ao detectar um medicamento no
dispenser errado: banner vermelho, X sobre o código, beep, linha em
`logs/alertas.csv`.

Teste o alerta na prática: pegue a etiqueta `MED-001` (dispenser 1) e coloque na
frente do dispenser 3. Depois de 5 frames o alerta dispara.

Se a câmera não abrir, descubra o índice certo:

```bash
python src/camera.py
```

E rode com ele: `python src/main.py --camera 1`.

## 8. Problemas comuns

| Sintoma | Causa provável / solução |
|---|---|
| `FileNotFoundError: config/zonas.json` | você ainda não calibrou — rode `src/calibrar.py` |
| Imports em vermelho no PyCharm | falta marcar `src` como **Sources Root** (passo 1.3) |
| `ImportError: Unable to find zbar` (Windows) | instale o VC++ Redistributable 2013, x64 e x86 |
| Janela abre preta / câmera não abre | rode `python src/camera.py` e use o índice listado |
| QR não é lido | está pequeno demais (precisa de ~80 px de lado no frame), com reflexo, ou fora de foco |
| Alerta pisca e some | normal: é o `frames_para_confirmar`. Aumente em `config/parametros.json` se a câmera treme |
| Ele lê mas diz "fora de zona" | o centro do QR está caindo fora do retângulo — recalibre ou aumente a zona |
