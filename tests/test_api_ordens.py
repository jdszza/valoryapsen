"""Testes do POST /api/v1/ordens — a porta de entrada das OS.

A fila é o que dispensa medicamento; o banco é o que registra o que foi
dispensado. O endpoint aceitava uma sem a outra nos dois sentidos:

  * `salvar_ordem` usa INSERT IGNORE e devolvia `None` em silêncio quando a OS
    já existia. O endpoint enfileirava assim mesmo e respondia
    `{"aceita": true}` — a mesma OS processada duas vezes, dose dobrada no
    leito. O contrato (ANALISE_ARQUITETURAL §4.1) manda 409 `os_duplicada`.
  * a falha de banco era capturada, logada e ignorada: a OS ia para a fila sem
    linha em `ordens`/`os_itens`, então nada tinha o que atualizar e o
    relatório saía vazio — dispensa sem rastro.

Hoje os dois caminhos recusam, e é isso que este arquivo prende.
"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def _payload(os_id: str = "OS-2024-001") -> dict:
    return {
        "os_id":     os_id,
        "descricao": "Separação lote A",
        "categoria": "Analgésicos",
        "medicamentos": [
            {"medicamento": "Dipirona 500mg", "sku": "DIP500",
             "categoria": "Analgésicos", "quantidade": 10},
            {"medicamento": "Ibuprofeno 400mg", "sku": "IBU400",
             "categoria": "Analgésicos", "quantidade": 5},
        ],
    }


# ── Duplos ────────────────────────────────────────────────────────────────────

class OrdensFake:
    """Duplo de `salvar_ordem` com a semântica do INSERT IGNORE.

    Devolve True na primeira gravação de um `os_id` e False nas seguintes.
    `erro` encena banco fora do ar.
    """

    def __init__(self):
        self.gravadas: list[str] = []
        self.erro: Exception | None = None

    def __call__(self, os_id, descricao, medicamentos, payload_raw) -> bool:
        if self.erro:
            raise self.erro
        if os_id in self.gravadas:
            return False
        self.gravadas.append(os_id)
        return True


class FilaFake:
    """Duplo de `orchestrator.enfileirar_os` — a fila que dispensa remédio.

    Devolve bool como a função real: False quando a fila encheu entre a
    checagem de vaga e o enfileiramento.
    """

    def __init__(self):
        self.enfileiradas: list[str] = []
        self.aceita = True

    async def __call__(self, os_payload: dict) -> bool:
        if not self.aceita:
            return False
        self.enfileiradas.append(os_payload["os_id"])
        return True


@pytest.fixture
def api(carregar_central, monkeypatch):
    """Central servido por TestClient, com banco e fila duplados.

    `TestClient` sem `with`: a lifespan NÃO roda, então nada aqui toca MySQL,
    httpx ou o loop do orquestrador — o teste exercita só o endpoint.
    """
    central = carregar_central()
    ordens  = OrdensFake()
    fila    = FilaFake()
    monkeypatch.setattr(central.modulo, "salvar_ordem", ordens)
    monkeypatch.setattr(central.modulo.orch, "enfileirar_os", fila)

    return SimpleNamespace(
        cliente=TestClient(central.modulo.app),
        ordens=ordens,
        fila=fila,
        central=central,
    )


# ── OS nova ───────────────────────────────────────────────────────────────────

def test_os_nova_e_aceita_e_enfileirada(api):
    resposta = api.cliente.post("/api/v1/ordens", json=_payload())

    assert resposta.status_code == 200
    assert resposta.json() == {"aceita": True, "os_id": "OS-2024-001",
                               "posicao_fila": 1}
    assert api.ordens.gravadas == ["OS-2024-001"]
    assert api.fila.enfileiradas == ["OS-2024-001"]


# ── Duplicada ─────────────────────────────────────────────────────────────────

def test_reenvio_da_mesma_os_responde_409_e_nao_enfileira(api):
    """O bug: a segunda POST enfileirava de novo e respondia `aceita: true`."""
    primeira = api.cliente.post("/api/v1/ordens", json=_payload())
    segunda  = api.cliente.post("/api/v1/ordens", json=_payload())

    assert primeira.status_code == 200
    assert segunda.status_code == 409
    # Corpo exato do contrato — nada de embrulho em `detail`.
    assert segunda.json() == {"erro": "os_duplicada", "os_id": "OS-2024-001"}
    assert api.fila.enfileiradas == ["OS-2024-001"]   # uma só, a da primeira


def test_os_duplicada_nao_regrava_itens(api):
    api.cliente.post("/api/v1/ordens", json=_payload())
    api.cliente.post("/api/v1/ordens", json=_payload())

    assert api.ordens.gravadas == ["OS-2024-001"]


def test_os_ids_diferentes_nao_colidem(api):
    """A recusa é por `os_id`, não um "só uma OS por vez"."""
    r1 = api.cliente.post("/api/v1/ordens", json=_payload("OS-A"))
    r2 = api.cliente.post("/api/v1/ordens", json=_payload("OS-B"))

    assert (r1.status_code, r2.status_code) == (200, 200)
    assert api.fila.enfileiradas == ["OS-A", "OS-B"]


# ── Banco fora do ar ──────────────────────────────────────────────────────────

def test_falha_de_banco_responde_503_e_nao_enfileira(api):
    """Sem linha em `ordens` não há relatório: melhor não dispensar."""
    api.ordens.erro = RuntimeError("MySQL server has gone away")

    resposta = api.cliente.post("/api/v1/ordens", json=_payload())

    assert resposta.status_code == 503
    assert resposta.json() == {"erro": "persistencia_indisponivel",
                               "os_id": "OS-2024-001"}
    assert api.fila.enfileiradas == []


def test_os_recusada_por_503_pode_ser_reenviada(api):
    """A recusa não queima o `os_id`: com o banco de volta, a OS entra."""
    api.ordens.erro = RuntimeError("MySQL server has gone away")
    api.cliente.post("/api/v1/ordens", json=_payload())

    api.ordens.erro = None
    resposta = api.cliente.post("/api/v1/ordens", json=_payload())

    assert resposta.status_code == 200
    assert api.fila.enfileiradas == ["OS-2024-001"]


# ── Fila cheia (backpressure) ─────────────────────────────────────────────────
#
# O gerador posta uma OS a cada 90s; uma OS leva mais que isso (90 a 140s
# medidos com 6 slots, mais com os 8 de hoje). Com a
# trava do Triple Check ativa, o loop único do orquestrador para até um humano
# liberar — e o gerador continua postando. Com fila ilimitada, as OS se
# acumulavam em memória e no banco como "aguardando", sem teto.


def _encher_fila(api) -> int:
    """Enche a fila REAL do orquestrador até o `maxsize` e devolve a capacidade."""
    fila = api.central.modulo.orch._os_queue
    while not fila.full():
        fila.put_nowait({"os_id": f"OS-ENFILEIRADA-{fila.qsize()}"})
    return fila.maxsize


def test_fila_cheia_responde_429_e_nao_enfileira(api):
    capacidade = _encher_fila(api)

    resposta = api.cliente.post("/api/v1/ordens", json=_payload())

    assert resposta.status_code == 429
    corpo = resposta.json()
    assert corpo["erro"] == "fila_cheia"
    assert corpo["os_id"] == "OS-2024-001"
    assert corpo["fila"]["tamanho"] == capacidade
    assert corpo["fila"]["disponivel"] == 0
    assert api.fila.enfileiradas == []


def test_fila_cheia_nao_persiste_a_os(api):
    """Recusa antes do banco: linha "aguardando" órfã viraria a OS ativa."""
    _encher_fila(api)

    api.cliente.post("/api/v1/ordens", json=_payload())

    assert api.ordens.gravadas == []


def test_ultima_vaga_ainda_e_aceita(api):
    """O limite é o teto, não um degrau antes dele."""
    fila = api.central.modulo.orch._os_queue
    while fila.qsize() < fila.maxsize - 1:
        fila.put_nowait({"os_id": f"OS-{fila.qsize()}"})

    resposta = api.cliente.post("/api/v1/ordens", json=_payload())

    assert resposta.status_code == 200
    assert api.fila.enfileiradas == ["OS-2024-001"]


def test_resposta_429_traz_retry_after(api):
    """O cliente precisa saber quando voltar, não só que foi recusado."""
    _encher_fila(api)

    resposta = api.cliente.post("/api/v1/ordens", json=_payload())

    assert int(resposta.headers["Retry-After"]) > 0


def test_corrida_na_ultima_vaga_fecha_a_os_em_cancelada(api):
    """A vaga sumiu entre a checagem e o enfileiramento: a OS já foi gravada.

    Deixá-la "aguardando" seria pior que recusar — `get_ordem_ativa` devolve a
    mais antiga nesse status e o painel passaria a mostrar uma OS que ninguém
    vai processar.
    """
    api.fila.aceita = False

    resposta = api.cliente.post("/api/v1/ordens", json=_payload())

    assert resposta.status_code == 429
    assert resposta.json()["erro"] == "fila_cheia"
    assert api.fila.enfileiradas == []
    (cancelamento,) = api.central.banco.chamadas_de("atualizar_status_ordem")
    assert cancelamento["args"] == ("OS-2024-001", "cancelada")


# ── Endpoint de leitura da fila ───────────────────────────────────────────────

def test_endpoint_de_fila_expoe_ocupacao_e_capacidade(api):
    """É o que o erp-simulator consulta antes de gerar a próxima OS."""
    resposta = api.cliente.get("/api/v1/fila")

    assert resposta.status_code == 200
    fila = resposta.json()
    assert fila["tamanho"] == 0
    assert fila["capacidade"] == api.central.modulo.settings.MAX_FILA_OS
    assert fila["disponivel"] == fila["capacidade"]
    assert fila["cheia"] is False
    assert fila["trava_ativa"] is False


def test_endpoint_de_fila_acompanha_o_enchimento(api):
    capacidade = _encher_fila(api)

    fila = api.cliente.get("/api/v1/fila").json()

    assert (fila["tamanho"], fila["disponivel"], fila["cheia"]) == (capacidade, 0, True)


# ── Listagem das 10 ordens padrão ─────────────────────────────────────────────
#
# O catálogo das ordens fixas mora no central (`os_templates.py`) e não no
# erp-simulator, porque o console de operação precisa listá-las. Este
# endpoint é o que torna isso possível — e o que o gerador consome no boot, em
# vez de manter uma segunda cópia da lista. O conteúdo dos templates é testado
# em `test_erp_simulator.py`; aqui prende-se o CONTRATO da rota.

def _catalogo_fake(central):
    """`listar_medicamentos` devolvendo tudo o que os templates citam."""
    return lambda *a, **k: [
        {"nome": nome, "sku": f"SKU-{nome}", "categoria": "generica"}
        for nome in central.modulo.os_templates.nomes_usados()
    ]


def test_templates_sao_servidos_com_sku_do_catalogo(api, monkeypatch):
    """O template declara nome e quantidade; o SKU vem do banco na hora.

    SKU congelado no template envelheceria em silêncio — e é ele que a câmera
    do dispenser compara (`sku_esperado`).
    """
    monkeypatch.setattr(api.central.modulo, "listar_medicamentos",
                        _catalogo_fake(api.central))

    corpo = api.cliente.get("/api/v1/ordens/templates").json()

    assert corpo["total"] == 10 == len(corpo["templates"])
    assert corpo["num_slots"] == api.central.modulo.settings.NUM_SLOTS
    assert corpo["catalogo_carregado"] is True
    assert corpo["problemas"] == []
    for template in corpo["templates"]:
        assert template["total_itens"] == len(template["itens"])
        for item in template["itens"]:
            assert item["no_catalogo"] is True
            assert item["sku"] == f"SKU-{item['medicamento']}"


def test_templates_acusam_medicamento_que_sumiu_do_catalogo(api, monkeypatch):
    """É o erro mais provável de aparecer em cima da hora, e ele precisa
    aparecer NOMEADO — não como uma OS com `sku` vazio já na fila."""
    completo = _catalogo_fake(api.central)()
    monkeypatch.setattr(api.central.modulo, "listar_medicamentos",
                        lambda *a, **k: [m for m in completo
                                         if m["nome"] != "ALOIS 10MG"])

    corpo = api.cliente.get("/api/v1/ordens/templates").json()

    assert corpo["problemas"], "medicamento ausente não foi acusado"
    assert all("ALOIS 10MG" in p for p in corpo["problemas"])
    itens = [i for t in corpo["templates"] for i in t["itens"]
             if i["medicamento"] == "ALOIS 10MG"]
    assert itens and all(i["no_catalogo"] is False for i in itens)


def test_templates_com_banco_fora_do_ar_nao_fingem_estar_validos(api, monkeypatch):
    """Lista de problemas vazia SEM catálogo não pode ser lida como 'tudo certo'
    — daí o `catalogo_carregado`."""
    def _explodir(*a, **k):
        raise RuntimeError("mysql fora")

    monkeypatch.setattr(api.central.modulo, "listar_medicamentos", _explodir)

    resposta = api.cliente.get("/api/v1/ordens/templates")

    assert resposta.status_code == 200          # a listagem não depende do banco
    corpo = resposta.json()
    assert corpo["catalogo_carregado"] is False
    assert corpo["total"] == 10
    assert all(i["sku"] == "" for t in corpo["templates"] for i in t["itens"])


def test_ordem_instanciada_de_um_template_e_aceita_pelo_endpoint(api):
    """Fecha o laço: o payload que `instanciar` monta é o que `NovaOSReq` aceita.

    Sem este teste, um campo renomeado num lado só apareceria em produção como
    OS recusada com 400 — e a suíte dos templates continuaria verde, porque ela
    nunca posta nada.
    """
    templates = api.central.modulo.os_templates
    payload = templates.instanciar(templates.TEMPLATES[0])

    resposta = api.cliente.post("/api/v1/ordens", json=payload)

    assert resposta.status_code == 200
    assert resposta.json()["os_id"] == payload["os_id"]
    assert api.fila.enfileiradas == [payload["os_id"]]


def test_dois_disparos_do_mesmo_template_nao_colidem_no_endpoint(api):
    """O template é fixo, a chave primária não: o segundo disparo não pode
    tomar 409 `os_duplicada`."""
    templates = api.central.modulo.os_templates
    template = templates.TEMPLATES[0]

    primeira = api.cliente.post("/api/v1/ordens",
                                json=templates.instanciar(template))
    segunda  = api.cliente.post("/api/v1/ordens",
                                json=templates.instanciar(template))

    assert (primeira.status_code, segunda.status_code) == (200, 200)
    assert len(set(api.fila.enfileiradas)) == 2


def test_listar_templates_duas_vezes_nao_acumula_enriquecimento(api, monkeypatch):
    """`os_templates.listar()` devolve cópia: sem isso, a segunda chamada
    serviria os itens já mexidos pela primeira."""
    monkeypatch.setattr(api.central.modulo, "listar_medicamentos",
                        _catalogo_fake(api.central))

    primeira = api.cliente.get("/api/v1/ordens/templates").json()
    segunda  = api.cliente.get("/api/v1/ordens/templates").json()

    assert primeira["templates"] == segunda["templates"]
    assert api.central.modulo.os_templates.TEMPLATES[0]["itens"][0].keys() == \
           {"medicamento", "quantidade"}


# ── Validação de entrada ──────────────────────────────────────────────────────

def test_os_sem_medicamentos_e_400_e_nao_toca_no_banco(api):
    resposta = api.cliente.post(
        "/api/v1/ordens", json={"os_id": "OS-2024-001", "medicamentos": []}
    )

    assert resposta.status_code == 400
    assert api.ordens.gravadas == []
    assert api.fila.enfileiradas == []


# ── `salvar_ordem` em si ──────────────────────────────────────────────────────
#
# Os testes acima duplam `salvar_ordem`; estes rodam a função de verdade contra
# um cursor falso, para que o booleano que o endpoint lê não seja só uma
# convenção do duplo.

class CursorFake:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount
        self.sqls: list[str] = []

    def execute(self, sql, args=None):
        self.sqls.append(" ".join(sql.split()))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def inserts_em(self, tabela: str) -> list[str]:
        return [s for s in self.sqls if f"INTO {tabela} " in s]


class ConexaoFake:
    def __init__(self, rowcount: int):
        self.cur = CursorFake(rowcount)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _instalar_conexao(database, monkeypatch, rowcount: int) -> ConexaoFake:
    conexao = ConexaoFake(rowcount)

    @contextmanager
    def _conn(*args, **kwargs):
        yield conexao

    monkeypatch.setattr(database, "_conn", _conn)
    return conexao


@pytest.fixture
def database(carregar_central):
    """O `database.py` real — o import do central já o deixou em `sys.modules`."""
    carregar_central()          # coloca `central-computer` no sys.path
    import database as modulo
    return modulo


def test_salvar_ordem_devolve_true_quando_insere(database, monkeypatch):
    conexao = _instalar_conexao(database, monkeypatch, rowcount=1)

    inserida = database.salvar_ordem(
        "OS-1", "d", _payload()["medicamentos"], {"categoria": "Analgésicos"}
    )

    assert inserida is True
    assert len(conexao.cur.inserts_em("os_itens")) == 2
    assert conexao.commits == 1


def test_salvar_ordem_devolve_false_quando_a_os_ja_existe(database, monkeypatch):
    """rowcount 0 = INSERT IGNORE não inseriu. Itens não podem duplicar."""
    conexao = _instalar_conexao(database, monkeypatch, rowcount=0)

    inserida = database.salvar_ordem(
        "OS-1", "d", _payload()["medicamentos"], {"categoria": "Analgésicos"}
    )

    assert inserida is False
    assert conexao.cur.inserts_em("os_itens") == []
    assert (conexao.commits, conexao.rollbacks) == (0, 1)
