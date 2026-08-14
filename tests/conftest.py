"""
Infraestrutura compartilhada dos testes dos simuladores APSEN.

Os simuladores não são pacotes importáveis: os diretórios têm hífen no nome
(`weight-simulator`, `vision-simulator`) e o módulo sobe o uvicorn no bloco
`__main__`. Por isso o carregamento é feito por CAMINHO, via
`importlib.util.spec_from_file_location`.

Durante o import, `requests` é substituído por um duplo que grava as chamadas em
memória em vez de fazer HTTP — é assim que os testes inspecionam os eventos que o
simulador emitiria para o seu adapter.

Uso típico:

    def test_algo(carregar_simulador):
        sim = carregar_simulador("weight", env={"T_LEITURA": "0"})
        sim.modulo._do_pesar("OS-1", 1, 10, 50.0)
        assert sim.eventos_do_tipo("peso_ok")
"""
import importlib.util
import sys
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent

# Caminho de cada simulador, relativo à raiz do repositório.
SIMULADORES = {
    "cnc":       "cnc_simulator/simulator.py",
    "dispenser": "dispenser_simulator/simulator.py",
    "vision":    "vision-simulator/simulator.py",
    "weight":    "weight-simulator/simulator.py",
}


# ── Duplo de `requests` ────────────────────────────────────────────────────────

class RespostaFake:
    """Resposta mínima com a superfície que os simuladores realmente usam."""

    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = ""

    def json(self) -> dict:
        return self._payload


class RequestsFake:
    """Stand-in para o módulo `requests`: grava as chamadas em vez de fazer HTTP.

    `status_code` e `payload` são atributos mutáveis para que um teste possa
    simular adapter fora do ar (ex.: `sim.requests.status_code = 500`).
    """

    def __init__(self):
        self.chamadas: list[dict] = []
        self.status_code = 200
        self.payload: dict = {}

    # A API real expõe `requests.exceptions.*`; alguns handlers capturam por nome.
    class exceptions:  # noqa: N801 — espelha o nome do módulo real
        class RequestException(Exception):
            pass

        class Timeout(RequestException):
            pass

        class ConnectionError(RequestException):
            pass

    def _registrar(self, metodo, url, json=None, **kwargs) -> RespostaFake:
        self.chamadas.append({
            "metodo":  metodo,
            "url":     url,
            "json":    json,
            "timeout": kwargs.get("timeout"),
        })
        return RespostaFake(self.status_code, self.payload)

    def post(self, url, json=None, **kwargs) -> RespostaFake:
        return self._registrar("POST", url, json, **kwargs)

    def get(self, url, **kwargs) -> RespostaFake:
        return self._registrar("GET", url, None, **kwargs)


# ── Handle devolvido ao teste ──────────────────────────────────────────────────

class SimuladorCarregado:
    """Simulador importado + os eventos que ele tentou emitir."""

    def __init__(self, modulo, requests_fake: RequestsFake):
        self.modulo = modulo
        self.requests = requests_fake

    @property
    def chamadas(self) -> list[dict]:
        """Todas as chamadas HTTP interceptadas, na ordem."""
        return self.requests.chamadas

    @property
    def eventos(self) -> list[dict]:
        """Payloads JSON enviados — nos simuladores, um por evento emitido."""
        return [c["json"] for c in self.requests.chamadas if c["json"] is not None]

    def eventos_do_tipo(self, *tipos: str) -> list[dict]:
        return [e for e in self.eventos if e.get("tipo") in tipos]

    def limpar_eventos(self) -> None:
        self.requests.chamadas.clear()


# ── Fixture ────────────────────────────────────────────────────────────────────

@pytest.fixture
def carregar_simulador(monkeypatch):
    """Fábrica que importa um simulador por caminho, com `requests` mockado.

    `nome` é uma chave de SIMULADORES ou um caminho relativo à raiz do repo.
    `env` é aplicado ANTES do import — os simuladores leem as env vars em
    constantes de módulo, então é a única janela para configurá-los.
    """

    def _carregar(nome: str, env: dict[str, str] | None = None) -> SimuladorCarregado:
        caminho = RAIZ_REPO / SIMULADORES.get(nome, nome)
        if not caminho.is_file():
            raise FileNotFoundError(f"Simulador não encontrado: {caminho}")

        for chave, valor in (env or {}).items():
            monkeypatch.setenv(chave, valor)

        requests_fake = RequestsFake()
        monkeypatch.setitem(sys.modules, "requests", requests_fake)

        # Nome único por teste: cada carga é uma instância limpa, sem estado
        # global vazando de um teste para o outro.
        nome_modulo = f"apsen_sim_{caminho.parent.name.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(nome_modulo, caminho)
        modulo = importlib.util.module_from_spec(spec)
        # Registrado antes do exec para que pydantic resolva `__module__`.
        monkeypatch.setitem(sys.modules, nome_modulo, modulo)
        spec.loader.exec_module(modulo)

        return SimuladorCarregado(modulo, requests_fake)

    return _carregar
