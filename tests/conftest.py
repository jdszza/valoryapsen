"""
Infraestrutura compartilhada dos testes do APSEN.

Nem os simuladores nem o computador central são pacotes importáveis: os
diretórios têm hífen no nome (`weight-simulator`, `central-computer`) e os
módulos sobem o uvicorn no bloco `__main__`. Por isso o carregamento é feito
por CAMINHO, via `importlib.util.spec_from_file_location`.

Duas fábricas são oferecidas:

  `carregar_simulador` — importa um simulador com `requests` substituído por um
  duplo que grava as chamadas em memória em vez de fazer HTTP. É assim que os
  testes inspecionam os eventos que o simulador emitiria para o seu adapter.

      def test_algo(carregar_simulador):
          sim = carregar_simulador("weight", env={"T_LEITURA": "0"})
          sim.modulo._do_pesar("OS-1", 1, 10, 50.0)
          assert sim.eventos_do_tipo("peso_ok")

  `carregar_central` — importa `central-computer/main.py` com todas as funções
  de `database.py` trocadas por duplos, para que nenhum teste precise de MySQL.

      def test_outra_coisa(carregar_central):
          central = carregar_central()
          asyncio.run(central.modulo._handle_evento_dispenser({...}))
          assert central.banco.chamadas_de("salvar_dispenser_estado")
"""
import importlib.util
import inspect
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


# ── Computador central ─────────────────────────────────────────────────────────

CENTRAL_DIR = RAIZ_REPO / "central-computer"


class BancoFake:
    """Duplo de `database.py`: grava as escritas em vez de falar com o MySQL.

    A instalação varre o `database` real e substitui, no módulo do central,
    toda referência que aponte para uma função DEFINIDA lá. Assim nenhuma
    chamada de banco escapa por esquecimento — inclusive as que surgirem no
    futuro. O filtro por `__module__` é o que impede o duplo de engolir
    `datetime`, `json` e companhia, que `database` só reexporta.
    """

    def __init__(self):
        self.chamadas: list[dict] = []

    def instalar(self, modulo_central, monkeypatch) -> None:
        import database

        for nome in dir(database):
            if nome.startswith("_"):
                continue
            real = getattr(database, nome)
            if not (inspect.isfunction(real) and real.__module__ == database.__name__):
                continue
            if getattr(modulo_central, nome, None) is real:
                monkeypatch.setattr(modulo_central, nome, self._duplo(nome))

    def _duplo(self, nome: str):
        def _registrar(*args, **kwargs):
            self.chamadas.append({"fn": nome, "args": args, "kwargs": kwargs})
            return None
        return _registrar

    def chamadas_de(self, nome: str) -> list[dict]:
        return [c for c in self.chamadas if c["fn"] == nome]

    def limpar_chamadas(self) -> None:
        self.chamadas.clear()


class CentralCarregado:
    """Módulo `main.py` do central + o duplo de banco instalado nele."""

    def __init__(self, modulo, banco: BancoFake):
        self.modulo = modulo
        self.banco = banco

    def slot(self, slot_id) -> dict:
        """Estado em memória de um slot — o dict que o dashboard enxerga."""
        return self.modulo._estado["dispensers"][str(slot_id)]


@pytest.fixture
def carregar_central(monkeypatch):
    """Fábrica que importa `central-computer/main.py` sem tocar no MySQL.

    `main.py` importa os vizinhos por nome absoluto (`orchestrator`,
    `database`, `auth`, `config`), então o diretório entra no `sys.path` antes
    do exec. O import em si não abre conexão — `database.py` só conecta dentro
    de cada função —, por isso basta trocar as funções por duplos depois.

    Os módulos do próprio central são descartados no teardown, para que cada
    teste receba um `_estado` zerado. Os de terceiros ficam no cache: `bcrypt`
    é uma extensão PyO3 que só aceita ser inicializada uma vez por processo.
    """
    modulos_antes = set(sys.modules)

    def _carregar() -> CentralCarregado:
        monkeypatch.syspath_prepend(str(CENTRAL_DIR))

        spec = importlib.util.spec_from_file_location(
            "apsen_central_main", CENTRAL_DIR / "main.py"
        )
        modulo = importlib.util.module_from_spec(spec)
        # Registrado antes do exec para que pydantic resolva `__module__`.
        sys.modules["apsen_central_main"] = modulo
        spec.loader.exec_module(modulo)

        banco = BancoFake()
        banco.instalar(modulo, monkeypatch)
        return CentralCarregado(modulo, banco)

    yield _carregar

    for nome in set(sys.modules) - modulos_antes:
        origem = getattr(sys.modules[nome], "__file__", None)
        if origem and Path(origem).parent == CENTRAL_DIR:
            del sys.modules[nome]
