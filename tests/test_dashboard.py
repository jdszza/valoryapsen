"""Dashboard: todo I/O mora no `_fetch`.

O `_render` buscava `/alarmes` por conta própria, no meio da montagem dos
cards. Era uma quarta requisição a cada 2s POR CLIENTE conectado, fora do
lugar onde as outras três estão — e num callback que deveria ser função pura
do que já chegou pelos stores.
"""
import pytest


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
            for i in range(1, 7)
        },
        "fila_os": [], "fila_tamanho": 0, "fila_capacidade": 5,
        "atribuicao_ia": [], "alarmes_ativos": 2,
        "trava": {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""},
        "peso": {"ultima_leitura": None, "slot_id": None, "ts": None},
        "visao": {"camera_dispenser": {}, "camera_mesa": {}},
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
