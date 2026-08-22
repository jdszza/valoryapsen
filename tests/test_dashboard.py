"""Dashboard: todo I/O mora no `_fetch`.

O `_render` buscava `/alarmes` por conta própria, no meio da montagem dos
cards. Era uma quarta requisição a cada 2s POR CLIENTE conectado, fora do
lugar onde as outras três estão — e num callback que deveria ser função pura
do que já chegou pelos stores.
"""
import pytest

from conftest import NUM_SLOTS, SLOTS_POR_FILEIRA


@pytest.fixture
def dashboard(carregar_simulador):
    """`dashboard/app.py` com `requests` duplado (mesma fábrica dos simuladores)."""
    return carregar_simulador("dashboard/app.py")


def _estado_minimo() -> dict:
    return {
        "cnc": {"status": "idle", "os_id": None, "dispenser_alvo": None,
                "posicao_x": 0.0, "posicao_y": 0.0, "ciclo_atual": 0,
                "total_ciclos": 0},
        "os_ativa": None,
        "dispensers": {
            str(i): {"status": "idle", "medicamento": None, "sku": None,
                     "categoria": None, "quantidade": 0, "quantidade_alvo": 0,
                     "quantidade_dispensada": 0, "quantidade_residual": 0,
                     "os_id": None}
            for i in range(1, NUM_SLOTS + 1)
        },
        "fila_os": [], "fila_tamanho": 0, "fila_capacidade": 5,
        "atribuicao_ia": [], "alarmes_ativos": 2,
        "trava": {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""},
        "peso": {"ultima_leitura": None, "slot_id": None, "ts": None},
        "visao": {"camera_dispenser_esq": {}, "camera_dispenser_dir": {},
                  "camera_mesa": {}},
    }


def test_render_nao_faz_requisicao_http(dashboard):
    """O callback de render é função pura dos stores."""
    alarmes = [{"tipo": "erro_cnc", "descricao": "Falha", "ts": "2026-01-01T00:00:00"}]

    dashboard.modulo._render(_estado_minimo(), [], [], alarmes)

    assert dashboard.chamadas == [], (
        f"_render fez {len(dashboard.chamadas)} requisição(ões): "
        f"{[c['url'] for c in dashboard.chamadas]}"
    )


def test_fetch_concentra_as_quatro_chamadas(dashboard):
    dashboard.requests.payload = {}

    dashboard.modulo._fetch(1)

    urls = [c["url"] for c in dashboard.chamadas]
    assert len(urls) == 4
    assert any(u.endswith("/estado") for u in urls)
    assert any("/log/eventos" in u for u in urls)
    assert any("/os/historico" in u for u in urls)
    assert any("/alarmes" in u for u in urls)


def test_render_usa_os_alarmes_recebidos_pelo_store(dashboard):
    """O card tem que sair do que veio no store, não de uma busca própria."""
    alarmes = [{"tipo": "divergencia_peso", "descricao": "Peso fora da faixa",
                "ts": "2026-01-01T12:00:00"}]

    saida = repr(dashboard.modulo._render(_estado_minimo(), [], [], alarmes))

    assert "Peso fora da faixa" in saida
    assert dashboard.chamadas == []


def test_backend_url_padrao_aponta_para_servico_existente(dashboard):
    """`http://backend:8000` era resíduo da migração: esse serviço não existe."""
    assert dashboard.modulo.BACKEND_URL == "http://central-computer:8000"


# ── Faixa de indicadores ──────────────────────────────────────────────────────

def _kpi(saida, label: str):
    """O bloco `ap-kpi` cujo rótulo é `label`.

    Procura pelo bloco, não pelo rótulo: `className`, `title` e valor moram
    todos nele, e é isso que cada teste daqui inspeciona.
    """
    for bloco in _percorrer(saida):
        if not str(getattr(bloco, "className", "")).startswith("ap-kpi"):
            continue
        if any(getattr(f, "className", None) == "ap-kpi-label" and f.children == label
               for f in _percorrer(bloco)):
            return bloco
    raise AssertionError(f"KPI {label!r} não está na faixa")


def _valor_do_kpi(bloco) -> str:
    for componente in _percorrer(bloco):
        if getattr(componente, "className", None) == "ap-kpi-value":
            return "".join(c for c in componente.children if isinstance(c, str))
    raise AssertionError("KPI sem ap-kpi-value")


def test_kpi_de_slots_conta_a_celula_inteira(dashboard):
    """"0/6" numa célula de 8 é um denominador que mente sobre a capacidade.

    O número aqui não era derivado de `NUM_SLOTS` e ficou para trás quando a
    célula cresceu — o mesmo drift que o resto da mudança removeu do código,
    sobrevivendo num literal que nenhum grep de `range(1, 7)` alcançava.
    """
    saida = dashboard.modulo._render(_estado_minimo(), [], [], [])

    assert _valor_do_kpi(_kpi(saida, "Slots em uso")).endswith(f"/{NUM_SLOTS}")


def test_kpi_de_fila_vazia_diz_sem_fila(dashboard):
    """Fila vazia é uma boa notícia, não um "0/5" que parece medição pendente."""
    saida = dashboard.modulo._render(_estado_minimo(), [], [], [])

    assert _valor_do_kpi(_kpi(saida, "Fila")) == "sem fila"


def test_kpi_de_fila_mostra_so_a_contagem(dashboard):
    """Com OS esperando, o valor é o número — a capacidade fica no title."""
    estado = _estado_minimo() | {"fila_tamanho": 3, "fila_capacidade": 5}

    saida = dashboard.modulo._render(estado, [], [], [])
    bloco = _kpi(saida, "Fila")

    assert _valor_do_kpi(bloco) == "3"
    assert "5" in (bloco.title or ""), "a capacidade sumiu junto com o denominador"


def test_kpi_de_fila_cheia_fica_em_vermelho(dashboard):
    """O aviso de teto atingido não se perde por não mostrar mais o /cap."""
    estado = _estado_minimo() | {"fila_tamanho": 5, "fila_capacidade": 5}

    bloco = _kpi(dashboard.modulo._render(estado, [], [], []), "Fila")

    assert "is-err" in bloco.className
    assert "cheia" in (bloco.title or "")


def test_kpi_de_fila_vazia_nao_e_fila_cheia(dashboard):
    """Payload sem `fila_capacidade` não pode pintar "sem fila" de vermelho."""
    estado = _estado_minimo()
    estado.pop("fila_capacidade", None)
    estado["fila_capacidade"] = 0

    bloco = _kpi(dashboard.modulo._render(estado, [], [], []), "Fila")

    assert "is-err" not in bloco.className


# ── A grade espelha o arranjo físico ──────────────────────────────────────────

def _percorrer(componente):
    """Todos os componentes da árvore, incluindo a raiz.

    Aceita a tupla que `_render` devolve (um item por Output) e desce por ela
    como se fosse mais um nível de filhos.
    """
    if isinstance(componente, (list, tuple)):
        for item in componente:
            yield from _percorrer(item)
        return

    yield componente
    filhos = getattr(componente, "children", None)
    if filhos is None:
        return
    if not isinstance(filhos, (list, tuple)):
        filhos = [filhos]
    for filho in filhos:
        yield from _percorrer(filho)


def _cartoes_de_dispenser(saida) -> list[str]:
    """Rótulos D1, D2… na ordem em que aparecem na página."""
    rotulos = []
    for componente in _percorrer(saida):
        texto = getattr(componente, "children", None)
        if isinstance(texto, str) and texto.startswith("D") and texto[1:].strip().isdigit():
            rotulos.append(texto.strip())
    return rotulos


def test_grade_mostra_um_cartao_por_slot_da_celula(dashboard):
    """Slot sem cartão é slot que o operador não vê travar."""
    saida = dashboard.modulo._render(_estado_minimo(), [], [], [])

    esperado = [f"D{i}" for i in range(1, NUM_SLOTS + 1)]
    assert _cartoes_de_dispenser(saida) == esperado


def _fileiras_de_cartoes(no) -> list:
    """As `dbc.Row` de cartões de slot dentro de um componente."""
    return [f for f in _percorrer(no)
            if getattr(f, "className", "") == "g-2" and _cartoes_de_dispenser(f)]


def _grade_de_dispensers(saida):
    """O bloco que agrupa os cartões: fileira, corredor, fileira.

    Localizado pelo CONTEÚDO — o menor componente que contém as duas fileiras —
    e não por posição na tupla de saída. O painel de visão também desenha o
    corredor (as duas câmeras de dispenser ficam uma de cada lado dele), então
    contar `ap-corredor` na página inteira misturaria os dois usos.
    """
    candidatos = [c for c in _percorrer(saida) if len(_fileiras_de_cartoes(c)) == 2]
    assert candidatos, "as duas fileiras de cartões não foram encontradas"
    return candidatos[-1]      # o mais interno: `_percorrer` desce do pai ao filho


def test_grade_separa_as_duas_fileiras_pelo_corredor(dashboard):
    """Duas fileiras com o corredor no meio — a forma da bancada, não uma lista.

    A `_render` devolve os cartões numa `html.Div` de três filhos: fileira,
    corredor, fileira. Uma linha só de oito cartões passaria por qualquer
    asserção de conteúdo, e é justamente o que não se quer na apresentação.
    """
    grade = _grade_de_dispensers(
        dashboard.modulo._render(_estado_minimo(), [], [], []))

    corredores = [c for c in _percorrer(grade)
                  if "ap-corredor" == getattr(c, "className", None)]
    assert len(corredores) == 1, "a grade não tem o corredor entre as fileiras"

    fileiras = _fileiras_de_cartoes(grade)
    assert [len(_cartoes_de_dispenser(f)) for f in fileiras] == [
        SLOTS_POR_FILEIRA, NUM_SLOTS - SLOTS_POR_FILEIRA
    ]


def test_painel_de_visao_mostra_as_tres_cameras(dashboard):
    """Câmera sem painel é câmera cuja falha ninguém enxerga.

    As duas de dispenser aparecem identificadas pelo LADO e pela faixa de slots
    que cobrem: "a câmera parou" só vira ação se apontar para um lugar da
    bancada.
    """
    saida = dashboard.modulo._render(_estado_minimo(), [], [], [])
    textos = " | ".join(c for c in _percorrer(saida) if isinstance(c, str))

    assert f"Câmera Esquerda · D1–D{SLOTS_POR_FILEIRA}" in textos
    assert f"Câmera Direita · D{SLOTS_POR_FILEIRA + 1}–D{NUM_SLOTS}" in textos
    assert "Câmera da Mesa · balança" in textos


def _n_outputs(modulo) -> int:
    """Quantos Outputs o callback de `_render` declara.

    O registro fica em `dash._callback.GLOBAL_CALLBACK_MAP` (e não no
    `app.callback_map`) porque o dashboard usa o decorador de módulo
    `@callback`, não `@app.callback`.
    """
    from dash._callback import GLOBAL_CALLBACK_MAP

    for spec in GLOBAL_CALLBACK_MAP.values():
        if getattr(spec["callback"], "__wrapped__", None) is modulo._render:
            return len(spec["outputs_indices"])
    raise AssertionError("callback de _render não encontrado no registro do Dash")


@pytest.mark.parametrize(
    "caminho, estado",
    [("com dados", _estado_minimo()), ("sem dados", {})],
)
def test_render_devolve_um_item_por_output(dashboard, caminho, estado):
    """Aridade: os dois caminhos de `_render` têm que casar com os Outputs.

    Não é hipótese: mexendo no cabeçalho eu tirei um `Output` e esqueci o item
    correspondente no `return` do caminho com dados. O Dash rejeita a resposta
    inteira, a tela fica vazia — e o teste não pegou, porque chamar `_render`
    como função Python comum não passa pela checagem de aridade do Dash.
    """
    esperado = _n_outputs(dashboard.modulo)
    saida = dashboard.modulo._render(estado, [], [], [])

    assert isinstance(saida, tuple), f"{caminho}: _render deve devolver tupla"
    assert len(saida) == esperado, (
        f"{caminho}: _render devolveu {len(saida)} itens para {esperado} Outputs"
    )
