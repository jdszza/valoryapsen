"""
Infraestrutura compartilhada dos testes do APSEN.

Nem os simuladores nem o computador central são pacotes importáveis: os
diretórios têm hífen no nome (`weight-simulator`, `central-computer`) e os
módulos sobem o uvicorn no bloco `__main__`. Por isso o carregamento é feito
por CAMINHO, via `importlib.util.spec_from_file_location`.

Cinco fábricas são oferecidas:

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
          central = carregar_central()   # ou carregar_central(env={...})
          asyncio.run(central.modulo._handle_evento_dispenser({...}))
          assert central.banco.chamadas_de("salvar_dispenser_estado")

  `carregar_manut` — importa `manut_web/app.py` (Dash) com `requests` duplado,
  para testar callback sem subir servidor nem navegador.

      def test_download(carregar_manut):
          manut = carregar_manut()
          manut.requests.content = b"csv..."
          dados, erro = manut.modulo._buscar_relatorio("OS-1", "csv", "jwt")

  `carregar_painel` — importa `painel_operador/backend/app.py` (Flask) com
  `requests` e `serial` duplados e o SQLite apontado para um arquivo
  temporário. Nenhuma porta serial abre, nenhum servidor sobe, nenhum HTTP sai.

      def test_espelho(carregar_painel):
          painel = carregar_painel(ordens_central=[...])
          with painel.conexao() as conn:
              painel.modulo.sincronizar_ordens_central(conn)

  `carregar_orquestrador` — importa `central-computer/orchestrator.py` sozinho,
  com banco duplado e `_post` trocado por um adapter fake que grava os comandos
  e responde no lugar do equipamento.

      def test_mais_uma(carregar_orquestrador):
          orq = carregar_orquestrador()
          asyncio.run(orq.modulo._abortar_os("OS-1", "erro_cnc", atribuicoes))
          assert orq.adapter.comandos("/comandos/limpar")
"""
import importlib.util
import inspect
import os
import sys
import threading
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent

# O central RECUSA subir com a SECRET_KEY default (ver `validar_secret_key`).
# Definido aqui, no import do conftest, porque `config` é importado uma única
# vez por sessão — a partir do primeiro teste que toque em `auth` ou `main` — e
# lê o ambiente nesse momento. Os testes rodam, portanto, como uma instalação
# bem configurada; a validação em si é testada em `test_seguranca.py`, que
# chama a função direto.
os.environ.setdefault("SECRET_KEY", "t" * 64)

# Mesma ideia do lado do painel de bancada: `app.py` RECUSA subir com
# `APSEN_SECRET` default (ver `resolver_secret_key`), e as rotas /api/* respondem
# 503 sem `APSEN_API_TOKEN`. Os dois são lidos pelo módulo importado, então a
# suíte roda como uma bancada bem configurada; quem testa a recusa em si é
# `test_painel_seguranca.py`, que passa o ambiente pela fábrica.
PAINEL_API_TOKEN = "token-de-teste-do-painel"
os.environ.setdefault("APSEN_SECRET", "p" * 64)
os.environ.setdefault("APSEN_API_TOKEN", PAINEL_API_TOKEN)

# Nº de dispensers da célula. Fixado aqui pelo mesmo motivo da SECRET_KEY: os
# módulos leem `NUM_SLOTS` uma vez, em constante de módulo, no primeiro import
# da sessão. `setdefault` deixa quem exporta a variável (para experimentar
# outra célula) ditar o valor, sem que a suíte carregue um número próprio.
NUM_SLOTS = int(os.environ.setdefault("NUM_SLOTS", "8"))
SLOTS_POR_FILEIRA = NUM_SLOTS // 2

# Caminho de cada simulador, relativo à raiz do repositório.
SIMULADORES = {
    "cnc":       "cnc_simulator/simulator.py",
    "dispenser": "dispenser_simulator/simulator.py",
    "vision":    "vision-simulator/simulator.py",
    "weight":    "weight-simulator/simulator.py",
}


# ── Duplo de `requests` ────────────────────────────────────────────────────────

class RespostaFake:
    """Resposta mínima com a superfície que os simuladores realmente usam.

    `content` e `headers` existem para o download de relatório do app de
    manutenção, que lê
    os bytes e o `Content-Disposition` em vez do JSON.
    """

    def __init__(self, status_code: int = 200, payload: dict | None = None,
                 content: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = ""
        self.content = content
        self.headers = headers if headers is not None else {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        """Como o `requests` real: levanta em 4xx/5xx, silencioso no resto.

        O erp-simulator usa isto nas duas cargas de boot (catálogo e ordens
        padrão). Sem o método, o `AttributeError` caía no `except Exception` do
        laço de retentativa e o teste via "central fora do ar" onde a resposta
        tinha sido 200.
        """
        if self.status_code >= 400:
            raise RequestsFake.exceptions.RequestException(
                f"HTTP {self.status_code}"
            )


class RequestsFake:
    """Stand-in para o módulo `requests`: grava as chamadas em vez de fazer HTTP.

    `status_code`, `payload`, `content` e `headers` são atributos mutáveis para
    que um teste possa simular adapter fora do ar (ex.:
    `sim.requests.status_code = 500`) ou devolver um arquivo.
    """

    def __init__(self):
        self.chamadas: list[dict] = []
        self.status_code = 200
        self.payload: dict = {}
        self.content: bytes = b""
        self.headers: dict = {}

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
            "params":  kwargs.get("params"),
            "headers": kwargs.get("headers"),
            "timeout": kwargs.get("timeout"),
        })
        return RespostaFake(self.status_code, self.payload,
                            self.content, self.headers)

    def post(self, url, json=None, **kwargs) -> RespostaFake:
        return self._registrar("POST", url, json, **kwargs)

    def get(self, url, json=None, **kwargs) -> RespostaFake:
        # `json=` explícito: o helper `_api` do app de manutenção manda
        # `json=None` em TODO
        # método, inclusive GET. Deixá-lo cair no **kwargs colidia com o
        # posicional de `_registrar` e virava TypeError engolido pelo try/except
        # do chamador — a tela renderizava vazia sem dizer por quê.
        return self._registrar("GET", url, json, **kwargs)


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


# ── App de manutenção e operação (Dash) ───────────────────────────────────────

MANUT_APP = RAIZ_REPO / "manut_web" / "app.py"


class ManutCarregada:
    """Módulo `manut_web/app.py` + as chamadas HTTP que ele tentou fazer."""

    def __init__(self, modulo, requests_fake: RequestsFake):
        self.modulo = modulo
        self.requests = requests_fake

    @property
    def chamadas(self) -> list[dict]:
        return self.requests.chamadas


@pytest.fixture
def carregar_manut(monkeypatch):
    """Importa `manut_web/app.py` com `requests` duplado.

    O import monta o layout e registra os callbacks; nada sobe servidor nem
    navegador. `dash` e `dash_bootstrap_components` são importados DE VERDADE
    (constam de `requirements-dev.txt`) — duplá-los custaria mais do que
    instalá-los, e o layout deixaria de ser exercitado de fato.

    `BACKEND_URL` é lido do ambiente numa constante de módulo, então `env` só
    tem efeito antes do import — igual aos simuladores.
    """

    def _carregar(env: dict[str, str] | None = None) -> ManutCarregada:
        for chave, valor in (env or {}).items():
            monkeypatch.setenv(chave, valor)

        requests_fake = RequestsFake()
        monkeypatch.setitem(sys.modules, "requests", requests_fake)

        nome_modulo = "apsen_manut_web"
        spec = importlib.util.spec_from_file_location(nome_modulo, MANUT_APP)
        modulo = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, nome_modulo, modulo)
        spec.loader.exec_module(modulo)

        return ManutCarregada(modulo, requests_fake)

    return _carregar


# ── Painel de bancada (Flask + SQLite) ────────────────────────────────────────

PAINEL_DIR = RAIZ_REPO / "painel_operador" / "backend"


class SerialFake:
    """Stand-in para `pyserial`.

    Duplado, e não instalado: a suíte não pode abrir uma porta USB, e o único
    caminho do painel que toca `serial` é a thread que `iniciar_workers()` sobe
    — que nenhum teste chama. O duplo existe só para o import do módulo passar.
    """

    class SerialException(Exception):
        pass

    class Serial:
        def __init__(self, *args, **kwargs):
            raise AssertionError("nenhum teste pode abrir porta serial")

    class tools:  # noqa: N801 — espelha o nome do módulo real
        class list_ports:  # noqa: N801
            @staticmethod
            def comports():
                return []


class CentralFake:
    """Respostas do computador central para o painel, por caminho de URL.

    Substitui `central_client._get` — e não `requests` — porque é aí que mora a
    decisão que os testes encenam: "o central respondeu isto", "o central não
    respondeu". `fora_do_ar=True` devolve `None` em tudo, exatamente como um
    timeout ou uma conexão recusada fazem depois de passarem pelo try/except.
    """

    def __init__(self, ordens=None, detalhes=None, dispensers=None):
        self.ordens = list(ordens or [])
        self.detalhes = dict(detalhes or {})
        self.dispensers = list(dispensers or [])
        self.fora_do_ar = False
        self.caminhos: list[str] = []

    def get(self, caminho: str, params: dict | None = None):
        self.caminhos.append(caminho)
        if self.fora_do_ar:
            return None
        if caminho == "/os/historico":
            return self.ordens
        if caminho == "/dispensers/estado":
            return self.dispensers
        if caminho == "/estado":
            return {"ok": True}
        if caminho == "/os/ativa":
            ativa = next((o for o in self.ordens if o.get("status") == "em_andamento"), None)
            return {"os_ativa": ativa}
        if caminho.startswith("/os/"):
            return self.detalhes.get(caminho[len("/os/"):])
        return None

    def chamadas_de_detalhe(self) -> list[str]:
        return [c for c in self.caminhos
                if c.startswith("/os/") and c not in ("/os/historico", "/os/ativa")]


class PainelCarregado:
    """Módulo `painel_operador/backend/app.py` + os duplos instalados nele."""

    def __init__(self, modulo, central: CentralFake, db_path):
        self.modulo = modulo
        self.central = central
        self.db_path = db_path
        self.cliente = modulo.app.test_client()

    def conexao(self):
        """Conexão nova no banco temporário — a mesma que as rotas abrem."""
        return self.modulo.get_db()

    def logar(self, perfil: str = "Admin") -> None:
        """Põe uma sessão válida no cliente de teste.

        As rotas web são protegidas por `login_required` + `requer(...)`, que
        leem a `session` do Flask. Gravá-la direto evita depender do PIN do seed.
        """
        with self.cliente.session_transaction() as sessao:
            sessao["op_id"] = 1
            sessao["op_nome"] = "Teste"
            sessao["perfil"] = perfil

    def api(self, metodo: str, caminho: str, **kwargs):
        """Requisição a uma rota /api/* já com o `X-API-Token`.

        Existe para que o token apareça em UM lugar: rota /api/* sem ele é 401,
        e um teste que o esquecesse falharia por um motivo que não é o dele.
        Quem testa a ausência do header chama `cliente` direto.
        """
        headers = {"X-API-Token": PAINEL_API_TOKEN, **kwargs.pop("headers", {})}
        return getattr(self.cliente, metodo)(caminho, headers=headers, **kwargs)

    def ordem(self, numero_os: str):
        conn = self.conexao()
        try:
            return conn.execute(
                "SELECT * FROM ordens WHERE numero_os=?", (numero_os,)
            ).fetchone()
        finally:
            conn.close()

    def criar_ordem_local(self, numero_os: str, itens, status: str = "Pendente") -> int:
        import json as _json
        conn = self.conexao()
        try:
            cur = conn.execute(
                """INSERT INTO ordens
                   (numero_os, itens, destino, prioridade, status,
                    data_criacao, data_atualizacao, origem, os_id_central)
                   VALUES (?,?,?,?,?, '2026-01-01 00:00:00', '2026-01-01 00:00:00', 'local', '')""",
                (numero_os, _json.dumps(itens), "Bancada", "Normal", status),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


@pytest.fixture
def carregar_painel(monkeypatch, tmp_path):
    """Fábrica que importa o backend do painel isolado da bancada.

    Três bordas são substituídas, e cada uma por um motivo diferente:

    * `serial` — duplado: nenhum teste pode tomar posse de um dispositivo USB.
    * `requests` — duplado como nos simuladores, para que nada saia pela rede
      mesmo que um caminho novo passe por baixo de `central_client._get`.
    * `central_client._get` — duplado pelo `CentralFake`, que é onde os testes
      encenam o central respondendo, respondendo diferente, ou não respondendo.

    O banco vai para `tmp_path` por `APSEN_DB`; `init_db()` roda no import e
    cria o schema lá. As threads de fundo NÃO sobem: elas moram em
    `iniciar_workers()`, que só o bloco de execução e o `desktop.py` chamam.
    """
    modulos_antes = set(sys.modules)

    def _carregar(ordens_central=None, detalhes_central=None,
                  dispensers_central=None, env=None) -> PainelCarregado:
        db_path = tmp_path / "painel.db"
        monkeypatch.setenv("APSEN_DB", str(db_path))
        for chave, valor in (env or {}).items():
            # Valor None APAGA a variável: é assim que um teste encena a bancada
            # sem `APSEN_API_TOKEN` — caso que `setenv` sozinho não alcança.
            if valor is None:
                monkeypatch.delenv(chave, raising=False)
            else:
                monkeypatch.setenv(chave, valor)

        monkeypatch.setitem(sys.modules, "requests", RequestsFake())
        monkeypatch.setitem(sys.modules, "serial", SerialFake)
        monkeypatch.setitem(sys.modules, "serial.tools", SerialFake.tools)
        monkeypatch.setitem(sys.modules, "serial.tools.list_ports",
                            SerialFake.tools.list_ports)

        # `app.py` importa `central_client` por nome absoluto, como o central
        # importa os vizinhos dele.
        monkeypatch.syspath_prepend(str(PAINEL_DIR))

        spec = importlib.util.spec_from_file_location(
            "apsen_painel_app", PAINEL_DIR / "app.py"
        )
        modulo = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "apsen_painel_app", modulo)
        spec.loader.exec_module(modulo)

        central = CentralFake(ordens_central, detalhes_central, dispensers_central)
        monkeypatch.setattr(modulo.central_client, "_get", central.get)
        modulo.app.config["TESTING"] = True

        return PainelCarregado(modulo, central, db_path)

    yield _carregar

    for nome in set(sys.modules) - modulos_antes:
        origem = getattr(sys.modules[nome], "__file__", None)
        if origem and Path(origem).parent == PAINEL_DIR:
            del sys.modules[nome]


# ── Ordens padrão ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def os_templates():
    """`central-computer/os_templates.py` importado por caminho.

    Módulo sem dependência nenhuma — nem de `config`, nem de banco —, então não
    precisa do aparato de `carregar_central`: basta o caminho. `scope="session"`
    porque ele não tem estado mutável e o import roda o autoteste de estrutura.
    """
    caminho = RAIZ_REPO / "central-computer" / "os_templates.py"
    spec = importlib.util.spec_from_file_location("apsen_os_templates", caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["apsen_os_templates"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


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


def requisicao(ip: str = "10.0.0.1"):
    """`starlette.Request` mínimo, para chamar um endpoint sem subir servidor.

    Um objeto de mentira com `.client.host` resolveria hoje e divergiria no dia
    em que a rota passasse a ler um header; construir o `Request` de verdade a
    partir de um scope ASGI custa as mesmas três linhas e não tem esse risco.
    """
    from starlette.requests import Request
    return Request({
        "type": "http", "method": "POST", "path": "/",
        "headers": [], "client": (ip, 51234),
    })


class CentralCarregado:
    """Módulo `main.py` do central + o duplo de banco instalado nele."""

    def __init__(self, modulo, banco: BancoFake):
        self.modulo = modulo
        self.banco = banco

    def slot(self, slot_id) -> dict:
        """Estado em memória de um slot — o dict que o dashboard enxerga."""
        return self.modulo._estado["dispensers"][str(slot_id)]


def _despejar_modulos_do_central(exceto: set | None = None) -> None:
    """Tira do `sys.modules` os módulos que moram em `central-computer/`.

    Eles leem o ambiente em constantes de módulo, no import. Enquanto ficarem no
    cache, um `monkeypatch.setenv` não muda coisa alguma. `exceto` preserva o
    que já estava carregado antes de um escopo — usado no teardown, para não
    despejar módulos de terceiros nem mexer no que a sessão trouxe de fora.
    """
    for nome in list(sys.modules):
        if exceto is not None and nome in exceto:
            continue
        modulo = sys.modules.get(nome)
        origem = getattr(modulo, "__file__", None)
        if origem and Path(origem).parent == CENTRAL_DIR:
            del sys.modules[nome]


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

    def _carregar(env: dict[str, str] | None = None) -> CentralCarregado:
        # `env` ANTES do exec, como em `carregar_simulador`: `config.py` lê as
        # variáveis em defaults de dataclass, avaliados no import do módulo.
        # Depois do import não há mais janela para configurá-lo.
        for chave, valor in (env or {}).items():
            monkeypatch.setenv(chave, valor)

        # Despeja os vizinhos do central do cache ANTES do exec, e não só no
        # teardown. O teardown limpa o que ESTE teste importou; `config` e
        # `database` podem já estar no cache desde outro arquivo da suíte, e aí
        # `main.py` reaproveitaria o módulo velho — o `env` acima não teria
        # efeito nenhum e o teste passaria a medir a configuração do vizinho.
        # O sintoma era exatamente esse: verde sozinho, vermelho na suíte
        # inteira, dependendo da ordem dos arquivos.
        _despejar_modulos_do_central()

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

    _despejar_modulos_do_central(exceto=modulos_antes)


# ── Orquestrador ───────────────────────────────────────────────────────────────

def _estado_zerado() -> dict:
    """Recorte de `main._estado` com o que o orquestrador realmente toca."""
    return {
        "os_ativa":       None,
        "atribuicao_ia":  [],
        "fila_os":        [],
        "fila_tamanho":   0,
        "alarmes_ativos": 0,
        # Publicação do gatilho de injeção (ver `central-computer/injecao.py`).
        # Ausente aqui, `_publicar_injecao` criaria a chave em vez de atualizá-la
        # e o teste não distinguiria "publicou" de "nasceu publicado".
        "falha_armada": None,
        "trava": {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""},
        "dispensers": {
            str(i): {
                "status":                "idle",
                "medicamento":           None,
                "sku":                   None,
                "categoria":             None,
                "quantidade":            0,
                "quantidade_alvo":       0,
                "quantidade_dispensada": 0,
                "quantidade_residual":   0,
                "os_id":                 None,
            }
            for i in range(1, NUM_SLOTS + 1)
        },
    }


class AdapterFake:
    """Duplo de `orchestrator._post`: grava os comandos e responde pela planta.

    Gravar não basta. O orquestrador BLOQUEIA em `aguardar_evento` depois de
    quase todo comando, então um duplo mudo trava a OS no primeiro timeout —
    por isso cada comando aceito devolve aqui, por `notificar_evento`, o mesmo
    evento que o adapter real devolveria. É o que permite rodar uma OS inteira
    dentro de um `asyncio.run()`, sem HTTP e sem simulador.

    Os eventos são emitidos de dentro do próprio `post`, ou seja, antes de o
    orquestrador começar a esperar. Não há corrida: todas essas chaves são
    pré-registradas com `registrar_evento` ANTES do comando sair.

    Atributos mutáveis para encenar falha: `aceita=False` (adapter fora do ar),
    `confirma_limpeza=False` (equipamento mudo → timeout),
    `quantidade_dispensada` (falha mecânica: solta menos que o alvo) e
    `capturas_divergentes` (as N primeiras leituras da câmera do dispenser
    voltam com SKU errado, que é o que ativa a trava de bloqueio).

    Ele também HONRA o campo `injetar_falha` do comando, como cada simulador
    honra o seu. É a quarta cópia da semântica de injeção (catálogo do central
    + três simuladores + este duplo), e ela é tolerável pelo mesmo motivo que a
    reprodução da regra da balança logo abaixo: um duplo que ignorasse o campo
    provaria que o central MANDA a injeção e nada sobre a OS resultante — que é
    justamente o que se quer ver. `tests/test_injecao.py` compara as strings
    das quatro cópias contra o catálogo.
    """

    def __init__(self, modulo):
        self.modulo = modulo
        self.chamadas: list[dict] = []
        self.aceita = True
        self.confirma_limpeza = True
        self.quantidade_dispensada: int | None = None   # None = dispensa o alvo
        self.capturas_divergentes = 0                   # scans com SKU errado
        self._carga: dict[int, int] = {}                # slot → quantidade carregada

    async def post(self, url: str, payload: dict, timeout: float = 10.0) -> bool:
        self.chamadas.append({"url": url, "payload": payload})
        if not self.aceita:
            return False
        self._responder(url, payload)
        return True

    def _responder(self, url: str, payload: dict) -> None:
        os_id = payload.get("os_id")

        if url.endswith("/comandos/limpar"):
            if not self.confirma_limpeza:
                return
            slot = payload["dispenser_id"]
            self._evento(f"limpeza:{slot}", tipo="limpeza_ok",
                         dispenser_id=slot, medicamento_limpo=None)

        elif url.endswith("/comandos/carregar"):
            slot = payload["dispenser_id"]
            self._carga[slot] = payload["quantidade"]
            self._evento(f"{os_id}:carregado:{slot}", tipo="carregado",
                         dispenser_id=slot, quantidade=payload["quantidade"])

        elif url.endswith("/comandos/capturar/dispenser"):
            slot = payload["slot_id"]
            injetada = payload.get("injetar_falha")
            if injetada == "falha_leitura_dispenser":
                self._evento(f"{os_id}:visao_dispenser:{slot}",
                             tipo="leitura_dispenser_falha",
                             motivo="injecao_demonstracao", falha_injetada=True)
            elif injetada == "sku_dispenser":
                self._evento(f"{os_id}:visao_dispenser:{slot}",
                             tipo="leitura_dispenser_divergencia",
                             sku_esperado=payload.get("sku_esperado", ""),
                             sku_lido="APSEN-INJETADO-000", confianca=0.9,
                             falha_injetada=True)
            elif self.capturas_divergentes > 0:
                # Slot carregado com o medicamento errado: o orquestrador trava
                # e só re-escaneia depois que o operador liberar.
                self.capturas_divergentes -= 1
                self._evento(f"{os_id}:visao_dispenser:{slot}",
                             tipo="leitura_dispenser_divergencia",
                             sku_esperado=payload.get("sku_esperado", ""),
                             sku_lido="SKU-ERRADO", confianca=0.99)
            else:
                self._evento(f"{os_id}:visao_dispenser:{slot}",
                             tipo="leitura_dispenser_ok",
                             sku_lido=payload.get("sku_esperado", ""), confianca=0.99)

        elif url.endswith("/comandos/tara"):
            self._evento(f"{os_id}:tara", tipo="tara_ok", peso_tara_g=0.0)

        elif url.endswith("/comandos/mover"):
            slot = payload["dispenser_alvo"]
            self._evento(f"{os_id}:posicionado:{slot}", tipo="posicionado",
                         posicao_x=payload["posicao_x"], posicao_y=payload["posicao_y"])

        elif url.endswith("/comandos/dispensar"):
            slot = payload["dispenser_id"]
            qtd = (self._carga.get(slot, 0) if self.quantidade_dispensada is None
                   else self.quantidade_dispensada)
            if payload.get("injetar_falha") == "falha_mecanica_dispenser":
                qtd = max(0, qtd - 1)       # uma a menos, como o simulador
            self._evento(f"{os_id}:dispensado:{slot}", tipo="dispensado",
                         dispenser_id=slot, quantidade_dispensada=qtd)

        elif url.endswith("/comandos/capturar/mesa"):
            slot = payload["slot_id"]
            esperada = payload["quantidade_esperada"]
            if payload.get("injetar_falha") == "divergencia_mesa":
                self._evento(f"{os_id}:visao_mesa:{slot}",
                             tipo="leitura_mesa_divergencia",
                             quantidade_esperada=esperada,
                             quantidade_detectada=max(0, esperada - 1),
                             confianca=0.85, falha_injetada=True)
            else:
                self._evento(f"{os_id}:visao_mesa:{slot}", tipo="leitura_mesa_ok",
                             quantidade_detectada=esperada, confianca=0.98)

        elif url.endswith("/comandos/pesar"):
            self._evento(f"{os_id}:peso:{payload['slot_id']}", **self._pesagem(payload))

        # `/comandos/homing` não tem evento aguardado — nada a devolver.

    @staticmethod
    def _pesagem(payload: dict) -> dict:
        """Reproduz a decisão do weight-simulator a partir das DUAS quantidades.

        A mesa ganha o peso do que foi REALMENTE dispensado e o esperado vem do
        alvo da OS; comparar um com o outro é o que faz a balança enxergar uma
        falha mecânica (ver CLAUDE.md, "O comando de pesagem leva DUAS
        quantidades"). Tolerância de 5%, como o `TOLERANCIA_PERC` do simulador.
        """
        unitario = payload.get("peso_unitario_g") or 50.0
        esperado = payload["quantidade_esperada"] * unitario
        medido   = payload["quantidade_real"] * unitario
        # Injeção desloca a LEITURA, não a massa — como no weight-simulator.
        injetada = payload.get("injetar_falha") == "divergencia_peso"
        if injetada and esperado > 0:
            medido = max(0.0, medido - esperado * 0.10)
        desvio   = abs(medido - esperado) / esperado * 100.0 if esperado > 0 else 0.0
        return {
            "tipo":            "peso_ok" if desvio <= 5.0 else "peso_divergencia",
            "peso_esperado_g": esperado,
            "peso_medido_g":   medido,
            "desvio_pct":      desvio,
            "falha_injetada":  injetada,
        }

    def _evento(self, chave: str, **dados) -> None:
        self.modulo.notificar_evento(chave, dados)

    def comandos(self, sufixo: str) -> list[dict]:
        """Payloads enviados para os endpoints terminados em `sufixo`."""
        return [c["payload"] for c in self.chamadas if c["url"].endswith(sufixo)]


class OrquestradorCarregado:
    """Módulo `orchestrator.py` + duplos de banco e de HTTP instalados nele."""

    def __init__(self, modulo, banco: BancoFake, adapter: AdapterFake, estado: dict,
                 post_real, injecao=None):
        self.modulo = modulo
        self.banco = banco
        self.adapter = adapter
        self.estado = estado
        # Módulo `injecao.py` tal como o orquestrador o enxerga, para o teste
        # armar o gatilho sem reimportar (ver `carregar_orquestrador`).
        self.injecao = injecao
        # O `_post` de verdade, guardado antes de o `AdapterFake` tomar o lugar
        # dele no módulo. Quase todo teste quer o duplo; quem testa a POLÍTICA
        # DE RETRY precisa da função original, com um cliente HTTP duplado por
        # baixo — e sem isto ela ficaria inalcançável depois do monkeypatch.
        self.post_real = post_real

    def slot(self, slot_id) -> dict:
        return self.estado["dispensers"][str(slot_id)]


@pytest.fixture
def carregar_orquestrador(monkeypatch):
    """Fábrica que importa `central-computer/orchestrator.py` isolado.

    Sem `main.py`: o orquestrador recebe as dependências por `inicializar()`,
    então basta injetar um estado zerado e um lock próprios. O event loop vai
    como `None` de propósito — `notificar_evento` então chama `Event.set()`
    direto, em vez de agendar no loop, que é o que permite ao teste rodar cada
    corrotina com um `asyncio.run()` descartável.
    """
    modulos_antes = set(sys.modules)

    def _carregar(env: dict[str, str] | None = None) -> OrquestradorCarregado:
        for chave, valor in (env or {}).items():
            monkeypatch.setenv(chave, valor)

        # Mesmo motivo de `carregar_central`: `config` e `injecao` guardam
        # estado de módulo (a configuração lida no import; o gatilho armado).
        # Reaproveitar o que outro arquivo da suíte deixou no cache faria um
        # teste ver o gatilho de outro — vermelho conforme a ordem dos arquivos.
        _despejar_modulos_do_central()

        monkeypatch.syspath_prepend(str(CENTRAL_DIR))

        spec = importlib.util.spec_from_file_location(
            "apsen_central_orchestrator", CENTRAL_DIR / "orchestrator.py"
        )
        modulo = importlib.util.module_from_spec(spec)
        sys.modules["apsen_central_orchestrator"] = modulo
        spec.loader.exec_module(modulo)

        banco = BancoFake()
        banco.instalar(modulo, monkeypatch)

        estado = _estado_zerado()
        modulo.inicializar(estado, threading.Lock(), lambda: None, None)

        adapter = AdapterFake(modulo)
        # O mesmo objeto-módulo que o orquestrador importou — é nele que o
        # teste arma o gatilho. Pegar `import injecao` por fora devolveria
        # outra instância depois do despejo acima.
        modulo_injecao = sys.modules["injecao"]
        post_real = modulo._post
        monkeypatch.setattr(modulo, "_post", adapter.post)
        monkeypatch.setattr(modulo, "_client", None)   # o duplo de _post cobre tudo

        return OrquestradorCarregado(modulo, banco, adapter, estado, post_real,
                                     modulo_injecao)

    yield _carregar

    _despejar_modulos_do_central(exceto=modulos_antes)
