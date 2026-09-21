"""
APSEN — Catálogo das 10 Ordens de Saída padrão.

As OS deixaram de ser sorteadas item a item do catálogo de 96 medicamentos. O
que existe hoje são DEZ ordens com identidade própria — nome, categoria, itens
e quantidades fixos — e o acaso ficou restrito a QUAL delas é disparada. Para
quem assiste, o sistema continua imprevisível; para quem apresenta, o conteúdo
de cada OS é conhecido de antemão.

Por que o arquivo mora no central e não no erp-simulator
──────────────────────────────────────────────────────────
O console de operação (servido pelo próprio central) precisa LISTAR as dez e
disparar a que o operador escolher. Se a definição vivesse só no gerador — um
container separado, sem porta e sem API —, o central não teria como enumerá-las
sem uma segunda cópia da lista, e duas listas de dez ordens mantidas à mão
divergem: o console mostraria uma coisa e a planta dispensaria outra, sem erro
em lugar nenhum.

A alternativa considerada foi um diretório `shared/` copiado nas duas imagens.
Ela exigiria trocar o contexto de build de dois serviços (`build: ./x` vira
`context: .` + `dockerfile:`), arrastar o repositório inteiro para dentro das
imagens e manter um `.dockerignore` — custo de infraestrutura para resolver um
problema que o central já resolve com um GET. O gerador consome
`GET /api/v1/ordens/templates`, que é a mesma dependência HTTP que ele já tem
(ele nem sobe sem o central: `depends_on: service_healthy`).

O que ficou duplicado, e por que é aceitável
────────────────────────────────────────────
Só o FORMATO do `os_id` (`novo_os_id`), reimplementado em três linhas no
gerador porque ele não pode importar este módulo. Divergência aí produz ids com
aparência diferente — visível na primeira linha do log —, não uma OS errada.
É o oposto do caso do mapa de posições da célula, cuja divergência seria
silenciosa e indistinguível de falha mecânica (ver CLAUDE.md).

Itens declaram apenas NOME e QUANTIDADE
───────────────────────────────────────
`sku`, `categoria` e `categoria_desc` são resolvidos do catálogo real na hora
de instanciar (`instanciar`). Congelar o SKU aqui criaria a chance de ele
divergir do que está na tabela `medicamentos` — e o SKU é justamente o que a
câmera do dispenser compara (`sku_esperado`). Um SKU velho no template viraria
`leitura_dispenser_divergencia` e trava do Triple Check num slot só: o quadro
exato de um medicamento trocado.

Editar as ordens na véspera da apresentação é, portanto, mexer em `TEMPLATES` —
nome do medicamento (como está no catálogo) e quantidade. Nada de lógica.
"""
import copy
import uuid
from datetime import datetime, timezone

# ── Limites da definição ──────────────────────────────────────────────────────
# O teto de itens é o número de dispensers da célula, que o chamador informa
# (`NUM_SLOTS`): uma OS com mais itens que slots não tem como ser atribuída e
# seria abortada com `sem_slot` depois de já ter entrado na fila.
MIN_ITENS = 2
QTD_MIN   = 2
QTD_MAX   = 15

# ── A receita gravada na mesa ────────────────────────────────────────────────
# A mesa CNC não recebe coordenadas: ela segue o roteiro que foi gravado nela,
# na bancada, waypoint a waypoint. São dez slots de receita (A–J), um por
# ordem padrão, e o comando `mover` leva a LETRA.
#
# A letra sai do ÍNDICE do template nesta lista, e não de um dicionário à
# parte. Um segundo mapa mantido à mão divergiria no primeiro template que
# alguém reordenasse ou renomeasse, e o sintoma seria a mesa executando o
# roteiro de OUTRA ordem — parada no dispenser errado, com a câmera acusando
# SKU divergente num slot só, que é o quadro de uma falha mecânica. É a mesma
# duplicação que este repositório já recusou no mapa de posições da célula.
#
# O limite de dez é da placa (slots A–J na NVS), e por isso é `validar_estrutura`
# quem o cobra: um décimo primeiro template nasceria sem receita para onde ir.
RECEITAS = "ABCDEFGHIJ"

# `ordens.os_id` é VARCHAR(60) e o id instanciado é
# `{template_id}-{AAAAMMDDTHHMMSS}-{6 hex}` = len(template_id) + 23.
TAM_MAX_OS_ID = 60
_FORMATO_TS   = "%Y%m%dT%H%M%S"
_TAM_SUFIXO   = 6
# Quanto o disparo acrescenta ao `template_id`: dois hífens, o carimbo e o hex.
SUFIXO_DISPARO = 1 + len(datetime(2000, 1, 1).strftime(_FORMATO_TS)) + 1 + _TAM_SUFIXO
# Teto próprio, mais apertado que o do banco: o dashboard e o console mostram o
# id inteiro numa coluna, e 24 + 23 = 47 caracteres ainda cabem na linha.
MAX_TEMPLATE_ID = 24


class TemplatesInvalidos(RuntimeError):
    """Definição de template quebrada. É bug de código, não falha de ambiente."""


# ══════════════════════════════════════════════════════════════════════════════
# AS DEZ ORDENS
# ══════════════════════════════════════════════════════════════════════════════
# Perfis deliberadamente diferentes, para a demonstração cobrir casos distintos:
#
#   OS-URO-01      2 itens  — a mais curta; fecha em dois ciclos de CNC
#   OS-DOR-01      3 itens  — curta
#   OS-VITAM-01    3 itens  — curta
#   OS-GASTRO-01   4 itens  — meia célula
#   OS-SNC-02      4 itens  — meia célula, reaproveita residual da OS-SNC-01
#   OS-CARDIO-01   5 itens
#   OS-SNC-01      5 itens
#   OS-LACTO-01    6 itens
#   OS-INFECTO-01  6 itens
#   OS-GERAL-01    8 itens  — CÉLULA CHEIA: rota em serpentina completa
#
# Medicamentos compartilhados entre ordens — é o que faz o reaproveitamento de
# residual do `atribuir_slots` (passo 1) aparecer na prática, em vez de todo
# slot ser sempre limpo e recarregado:
#
#   ALOIS 10MG        → OS-SNC-01, OS-SNC-02, OS-GERAL-01
#   INSIT 50MG        → OS-SNC-01, OS-SNC-02
#   FLANCOX 500MG     → OS-DOR-01, OS-GERAL-01
#   RETEMIC 5MG       → OS-URO-01, OS-GERAL-01
#   LONIUM 40MG       → OS-GASTRO-01, OS-GERAL-01
#   INPRUV DK 7000UI  → OS-VITAM-01, OS-GERAL-01
#
# Disparar OS-SNC-01 e logo depois OS-SNC-02 é a sequência que mostra a coisa
# com clareza: dois slots são reencontrados carregados e o passo 1 os reaproveita
# sem limpeza.
TEMPLATES: list[dict] = [
    {
        "template_id":    "OS-URO-01",
        "nome":           "Urologia — ronda noturna",
        "descricao":      "Urologia - Ala B / Ronda noturna (LOTE-2201)",
        "categoria":      "urologia",
        "categoria_desc": "Urologia",
        "itens": [
            {"medicamento": "RETEMIC 5MG",   "quantidade": 5},
            {"medicamento": "UNOPROST 2MG",  "quantidade": 3},
        ],
    },
    {
        "template_id":    "OS-DOR-01",
        "nome":           "Reumatologia / Dor — lote matinal",
        "descricao":      "Reumatologia / Dor - Ala A / Lote matinal (LOTE-3310)",
        "categoria":      "reumatologia",
        "categoria_desc": "Reumatologia / Dor / Anti-inflamatorios",
        "itens": [
            {"medicamento": "ARPADOL 400MG",  "quantidade": 6},
            {"medicamento": "FLANCOX 500MG",  "quantidade": 8},
            {"medicamento": "COLCHIS 0,5MG",  "quantidade": 4},
        ],
    },
    {
        "template_id":    "OS-VITAM-01",
        "nome":           "Vitaminas — suplementação ambulatorial",
        "descricao":      "Vitaminas / Nutricao - Ambulatorio (LOTE-4102)",
        "categoria":      "vitaminas",
        "categoria_desc": "Vitaminas / Nutricao / Suplementacao",
        "itens": [
            {"medicamento": "DESOL",             "quantidade": 4},
            {"medicamento": "INPRUV DK 7000UI",  "quantidade": 15},
            {"medicamento": "EXTIMA CHOCOLATE",  "quantidade": 3},
        ],
    },
    {
        "template_id":    "OS-GASTRO-01",
        "nome":           "Gastroenterologia — leito 118",
        "descricao":      "Gastroenterologia - Leito 118 (PAC-11842)",
        "categoria":      "gastroenterologia",
        "categoria_desc": "Gastroenterologia",
        "itens": [
            {"medicamento": "LONIUM 40MG",  "quantidade": 5},
            {"medicamento": "INILOK 40MG",  "quantidade": 3},
            {"medicamento": "MOTILEX",      "quantidade": 2},
            {"medicamento": "MAG B",        "quantidade": 2},
        ],
    },
    {
        "template_id":    "OS-SNC-02",
        "nome":           "Neurologia — leito 207 (reposição)",
        "descricao":      "Neurologia / Psiquiatria - Leito 207 / Reposicao (PAC-20713)",
        "categoria":      "snc",
        "categoria_desc": "Neurologia / Psiquiatria / SNC",
        "itens": [
            {"medicamento": "ALOIS 10MG",   "quantidade": 5},
            {"medicamento": "INSIT 50MG",   "quantidade": 6},
            {"medicamento": "PAXORAL 7MG",  "quantidade": 5},
            {"medicamento": "LENIX 50MG",   "quantidade": 2},
        ],
    },
    {
        "template_id":    "OS-CARDIO-01",
        "nome":           "Cardiologia — leito 302",
        "descricao":      "Cardiologia / Vascular - Leito 302 (PAC-30219)",
        "categoria":      "cardiologia",
        "categoria_desc": "Cardiologia / Vascular",
        "itens": [
            {"medicamento": "ZANIDIP 10MG",   "quantidade": 5},
            {"medicamento": "XAFAC 2,5MG",    "quantidade": 7},
            {"medicamento": "XAFAC 10MG",     "quantidade": 5},
            {"medicamento": "XAFAC 20MG",     "quantidade": 7},
            {"medicamento": "DOBEVEN 500MG",  "quantidade": 10},
        ],
    },
    {
        "template_id":    "OS-SNC-01",
        "nome":           "Neurologia — leito 204",
        "descricao":      "Neurologia / Psiquiatria - Leito 204 (PAC-20441)",
        "categoria":      "snc",
        "categoria_desc": "Neurologia / Psiquiatria / SNC",
        "itens": [
            {"medicamento": "ALOIS 10MG",       "quantidade": 7},
            {"medicamento": "DONAREN 50MG",     "quantidade": 5},
            {"medicamento": "INSIT 50MG",       "quantidade": 7},
            {"medicamento": "ATENTAH 18MG",     "quantidade": 10},
            {"medicamento": "COBI-12 1000MCG",  "quantidade": 4},
        ],
    },
    {
        "template_id":    "OS-LACTO-01",
        "nome":           "Intolerância a lactose — kit flora",
        "descricao":      "Intolerancia a Lactose / Probioticos - Kit flora (LOTE-5507)",
        "categoria":      "lactose",
        "categoria_desc": "Intolerancia a Lactose / Flora Intestinal / Probioticos",
        "itens": [
            {"medicamento": "LACTOSIL 4500 COMP",   "quantidade": 3},
            {"medicamento": "LACTOSIL 10000 COMP",  "quantidade": 3},
            {"medicamento": "LACTOSIL FLORA",       "quantidade": 2},
            {"medicamento": "PROBID",               "quantidade": 2},
            {"medicamento": "PROBIANS",             "quantidade": 2},
            {"medicamento": "FLORACOL",             "quantidade": 2},
        ],
    },
    {
        "template_id":    "OS-INFECTO-01",
        "nome":           "Infectologia — esquema antibiótico",
        "descricao":      "Infectologia / Antibioticos - Leito 415 (PAC-41508)",
        "categoria":      "infectologia",
        "categoria_desc": "Infectologia / Antibioticos",
        "itens": [
            {"medicamento": "LEVOXIN 500MG",     "quantidade": 3},
            {"medicamento": "LEVOXIN 750MG",     "quantidade": 3},
            {"medicamento": "LECZA XR 500MG",    "quantidade": 5},
            {"medicamento": "SIL-HP 4MG",        "quantidade": 10},
            {"medicamento": "SIL-HP 8MG",        "quantidade": 10},
            {"medicamento": "DUEPOLI ER 500MG",  "quantidade": 5},
        ],
    },
    {
        "template_id":    "OS-GERAL-01",
        "nome":           "Carro de emergência — célula cheia",
        "descricao":      "Multicategoria - Carro de emergencia / Celula cheia (LOTE-8008)",
        "categoria":      "geral",
        "categoria_desc": "Multicategoria - Carro de emergencia",
        "itens": [
            {"medicamento": "ALOIS 10MG",        "quantidade": 7},
            {"medicamento": "MECLIN 25MG",       "quantidade": 5},
            {"medicamento": "RETEMIC 5MG",       "quantidade": 5},
            {"medicamento": "LONIUM 40MG",       "quantidade": 5},
            {"medicamento": "FLANCOX 500MG",     "quantidade": 8},
            {"medicamento": "MIOSAN 5MG",        "quantidade": 2},
            {"medicamento": "LURATT 20MG",       "quantidade": 5},
            {"medicamento": "INPRUV DK 7000UI",  "quantidade": 15},
        ],
    },
]


# ── Leitura ───────────────────────────────────────────────────────────────────

def listar() -> list[dict]:
    """Cópia profunda dos templates.

    Cópia porque quem recebe costuma enriquecer os itens com dados do catálogo;
    mutar o original faria a segunda chamada devolver a primeira já enriquecida.
    """
    return copy.deepcopy(TEMPLATES)


def por_id(template_id: str) -> dict | None:
    for template in TEMPLATES:
        if template["template_id"] == template_id:
            return copy.deepcopy(template)
    return None


def receita_de(template_id: str) -> str | None:
    """A letra do slot de receita gravado na mesa, ou None se a ordem não é uma
    das dez padrão.

    None é um resultado legítimo e o chamador precisa tratá-lo: uma OS criada
    fora dos templates (um POST à mão, um teste) não tem roteiro gravado, e a
    mesa a recusaria com `receita_desconhecida` no meio do ciclo. Quem descobre
    isso ANTES do primeiro `mover` é o orquestrador.
    """
    for i, template in enumerate(TEMPLATES):
        if template["template_id"] == template_id:
            return RECEITAS[i] if i < len(RECEITAS) else None
    return None


def nomes_usados(templates: list[dict] | None = None) -> set[str]:
    """Todo medicamento citado por algum template — o que precisa existir na
    tabela `medicamentos`."""
    return {
        item["medicamento"]
        for template in (TEMPLATES if templates is None else templates)
        for item in template["itens"]
    }


# ── Validação ─────────────────────────────────────────────────────────────────

def _problema_de_receita(templates: list[dict]) -> list[str]:
    """Mais templates que slots de receita na placa é um template sem roteiro.

    Sem esta checagem, o décimo primeiro seria aceito no import, apareceria no
    console, e só falharia com a OS já na fila — `receita_de` devolvendo None
    para ele e a OS abortando no primeiro ciclo.
    """
    if len(templates) > len(RECEITAS):
        return [f"{len(templates)} templates para {len(RECEITAS)} slots de receita "
                f"(A–{RECEITAS[-1]}) — os excedentes não teriam roteiro na mesa."]
    return []


def validar_estrutura(templates: list[dict] | None = None,
                      num_slots: int = 8) -> list[str]:
    """Invariantes que não dependem do banco. Devolve a lista de problemas.

    Lista, e não exceção, porque o chamador decide o que fazer com ela: no
    import deste módulo vira `TemplatesInvalidos` (é bug de código), no endpoint
    vira campo da resposta — o console mostra QUAL ordem está quebrada, em vez
    de o central devolver 500 para a listagem inteira.
    """
    lista = TEMPLATES if templates is None else templates
    problemas: list[str] = []
    vistos: set[str] = set()

    for template in lista:
        tid = template.get("template_id", "")
        if not tid:
            problemas.append("Template sem `template_id`.")
            continue
        if tid in vistos:
            problemas.append(f"{tid}: `template_id` duplicado.")
        vistos.add(tid)

        if len(tid) > MAX_TEMPLATE_ID:
            problemas.append(
                f"{tid}: `template_id` tem {len(tid)} caracteres — máximo "
                f"{MAX_TEMPLATE_ID}, senão o os_id do disparo "
                f"({len(tid) + SUFIXO_DISPARO}) fica ilegível na tela."
            )
        if not template.get("descricao"):
            problemas.append(
                f"{tid}: sem `descricao` — a OS apareceria sem rótulo no dashboard."
            )

        itens = template.get("itens") or []
        if not MIN_ITENS <= len(itens) <= num_slots:
            problemas.append(
                f"{tid}: {len(itens)} item(ns) — a faixa é {MIN_ITENS}..{num_slots} "
                f"(o teto é o nº de dispensers da célula)."
            )

        nomes = [item.get("medicamento", "") for item in itens]
        for med in sorted({n for n in nomes if nomes.count(n) > 1}):
            problemas.append(
                f"{tid}: '{med}' aparece mais de uma vez — seriam dois slots "
                f"com o mesmo medicamento na mesma OS."
            )

        for item in itens:
            med = item.get("medicamento", "")
            qtd = item.get("quantidade")
            if not med:
                problemas.append(f"{tid}: item sem `medicamento`.")
            if not isinstance(qtd, int) or isinstance(qtd, bool) \
                    or not QTD_MIN <= qtd <= QTD_MAX:
                problemas.append(
                    f"{tid}: quantidade {qtd!r} de '{med}' fora da faixa "
                    f"{QTD_MIN}..{QTD_MAX}."
                )

    problemas.extend(_problema_de_receita(lista))
    return problemas


def validar_contra_catalogo(nomes_catalogo,
                            templates: list[dict] | None = None) -> list[str]:
    """Todo medicamento citado precisa existir na tabela `medicamentos`.

    É o erro mais provável de aparecer em cima da hora: alguém edita um nome no
    template e ele deixa de casar com o catálogo. Sem esta checagem a OS sairia
    com `sku` vazio, a câmera do dispenser não teria o que comparar e o sintoma
    chegaria disfarçado de falha de leitura, num slot só.
    """
    disponiveis = set(nomes_catalogo)
    return [
        f"'{nome}' não existe na tabela `medicamentos` "
        f"(catálogo carregado tem {len(disponiveis)} itens)."
        for nome in sorted(nomes_usados(templates) - disponiveis)
    ]


# ── Instanciação ──────────────────────────────────────────────────────────────

def novo_os_id(template_id: str, agora: datetime | None = None,
               sufixo: str | None = None) -> str:
    """Chave primária de UM disparo: `{template_id}-{AAAAMMDDTHHMMSS}-{6 hex}`.

    O template é fixo; o `os_id` não pode ser. `ordens.os_id` é UNIQUE e o
    central responde 409 `os_duplicada` a um reenvio — sem o sufixo, a segunda
    vez que uma ordem padrão fosse disparada seria recusada, e a demonstração
    acabaria no primeiro ciclo do gerador.

    O carimbo de tempo dá leitura e ordenação dentro do mesmo template; os 6 hex
    cobrem dois disparos no mesmo segundo (o console permite isso; o gerador,
    que dorme `INTERVALO_OS`, não). Colisão exigiria mesmo template, mesmo
    segundo e mesmo hex — e o resultado seria um 409 no log, não uma dose
    dobrada, porque quem decide é o UNIQUE do banco.
    """
    carimbo = (agora or datetime.now(timezone.utc)).strftime(_FORMATO_TS)
    return f"{template_id}-{carimbo}-{(sufixo or uuid.uuid4().hex[:_TAM_SUFIXO]).upper()}"


def instanciar(template: dict, catalogo: dict | None = None,
               agora: datetime | None = None, sufixo: str | None = None) -> dict:
    """Monta o corpo de `POST /api/v1/ordens` para um disparo do template.

    `catalogo` é `{nome: {"sku": ..., "categoria": ...}}` — o que
    `GET /medicamentos` devolve, indexado por nome. Ausente, ou sem o item, o
    SKU sai vazio: quem barra isso é `validar_contra_catalogo`, ANTES; aqui não
    há o que fazer além de não inventar um SKU.
    """
    catalogo = catalogo or {}
    momento = agora or datetime.now(timezone.utc)

    return {
        "os_id":          novo_os_id(template["template_id"], momento, sufixo),
        "template_id":    template["template_id"],
        "descricao":      template["descricao"],
        "categoria":      template.get("categoria", ""),
        "categoria_desc": template.get("categoria_desc", ""),
        "medicamentos": [
            {
                "medicamento": item["medicamento"],
                "sku":         catalogo.get(item["medicamento"], {}).get("sku", ""),
                "categoria":   catalogo.get(item["medicamento"], {}).get(
                                   "categoria", template.get("categoria", "")),
                "quantidade":  item["quantidade"],
            }
            for item in template["itens"]
        ],
        "criado_em":      momento.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "origem":         "OS_PADRAO_v1.0",
    }


# Autoteste no import. Estes invariantes não dependem de ambiente nenhum, então
# quebrá-los é erro de digitação em `TEMPLATES` — e falhar aqui, no import, sai
# mais barato que descobrir com a OS já na fila. O teto usado é o do maior
# template existente; a checagem contra o `NUM_SLOTS` real da célula é do
# chamador, que é quem conhece a célula.
_problemas_do_import = validar_estrutura(
    num_slots=max(len(t["itens"]) for t in TEMPLATES)
)
if _problemas_do_import:
    raise TemplatesInvalidos(
        "TEMPLATES inválidos:\n  - " + "\n  - ".join(_problemas_do_import)
    )
