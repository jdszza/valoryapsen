# -*- coding: utf-8 -*-
"""A ponte do vision-adapter com a estação real dos dispensers.

A estação (`vision/visao`) não recebe comando: julga cada zona sem parar contra
um catálogo que busca no adapter, e publica o veredito em `/api/estado`. O
adapter monta esse catálogo com o que o central mandou validar NA OS CORRENTE,
espera a estação julgar com ele, e traduz o veredito no `leitura_dispenser_*`
que o central já entende.

Três coisas aqui não podem errar, e cada uma tem o seu bloco:

- o CATÁLOGO nunca pode ser um que a estação recuse — recusado, ela segue
  julgando pela OS anterior, calada. Por isso ele é conferido contra o
  `Catalogo.de_itens` de verdade, importado do código da estação;
- o FRESCOR: ler o `/api/estado` na hora do comando devolve o veredito dado com
  o catálogo da OS anterior. Uma estação falsa com relógio próprio prova que a
  leitura só sai depois que ela julgou com o catálogo novo;
- a TRADUÇÃO: leitura incerta é falha, nunca divergência — divergência trava a
  OS.

Sem rede e sem espera de verdade: o relógio e o `_dormir` do adapter são
trocados por um relógio falso.
"""
import ast
import asyncio
import importlib.util
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RAIZ_REPO = Path(__file__).resolve().parent.parent
CONFIGURACAO_ESTACAO = RAIZ_REPO / "vision" / "visao" / "src" / "configuracao.py"
DATABASE = RAIZ_REPO / "central-computer" / "database.py"
ETIQUETAS = RAIZ_REPO / "vision-adapter" / "etiquetas.json"

ESQ = "http://estacao-esq:8301"
DIR = "http://estacao-dir:8302"


# ── Fontes de verdade lidas do próprio repositório ────────────────────────────

def _seed_do_central() -> dict[str, str]:
    """{nome: sku} de `_MEDICAMENTOS_SEED`, lido por AST — sem importar o
    `database.py`, que puxa pymysql."""
    arvore = ast.parse(DATABASE.read_text(encoding="utf-8"))
    for no in arvore.body:
        if isinstance(no, ast.Assign) and any(
                getattr(alvo, "id", None) == "_MEDICAMENTOS_SEED" for alvo in no.targets):
            return {nome: sku for nome, sku, *_ in ast.literal_eval(no.value)}
    raise AssertionError("_MEDICAMENTOS_SEED não encontrado em database.py")


SEED = _seed_do_central()


@pytest.fixture(scope="module")
def Catalogo():
    """O `Catalogo` DA ESTAÇÃO, importado por caminho — nunca uma cópia da regra."""
    nome = "visao_estacao_configuracao"
    spec = importlib.util.spec_from_file_location(nome, CONFIGURACAO_ESTACAO)
    modulo = importlib.util.module_from_spec(spec)
    # As dataclasses dela resolvem anotações pelo módulo em `sys.modules`.
    sys.modules[nome] = modulo
    try:
        spec.loader.exec_module(modulo)
        yield modulo.Catalogo
    finally:
        sys.modules.pop(nome, None)


# ── O adapter em modo estação, com relógio falso ──────────────────────────────

class Relogio:
    def __init__(self):
        self.t = 100.0
        self.ganchos: list[tuple[float, object]] = []

    async def dormir(self, segundos):
        self.t += segundos
        for quando, acao in list(self.ganchos):
            if self.t >= quando:
                self.ganchos.remove((quando, acao))
                acao()


# O relógio da ESTAÇÃO tem época própria e nada a ver com o do adapter: se o
# adapter comparasse `momento` com o relógio dele, nenhum teste passaria.
EPOCA_ESTACAO = datetime(2026, 10, 7, 3, 0, 0, tzinfo=timezone.utc)


class EstacaoFalsa:
    """O `/api/estado` de uma estação: uma linha por zona, `momento` em segundos.

    `linhas[slot]` é um dict ou uma função do relógio que devolve o dict — é
    assim que se encena o veredito mudando quando a estação busca o catálogo.
    """
    def __init__(self, relogio: Relogio, linhas: dict, nome="dispensers-esq"):
        self.relogio = relogio
        self.linhas = linhas
        self.nome = nome
        self.no_ar = True
        self.congelada_em = None  # câmera cega: o momento para

    def estado(self) -> dict:
        if not self.no_ar:
            raise ConnectionError("estação fora do ar")
        t = self.relogio.t if self.congelada_em is None else self.congelada_em
        momento = (EPOCA_ESTACAO + timedelta(seconds=int(t))).isoformat(timespec="seconds")
        linhas = []
        for slot, fonte in self.linhas.items():
            dados = fonte(self.relogio.t) if callable(fonte) else fonte
            linhas.append({"estacao": self.nome, "dispenser": slot, "momento": momento,
                           "confianca": 0.7, "caixas": None, "fracao": None,
                           "unidades": 1, "precisa_repor": 0, **dados})
        return {"estado": linhas}


class _Resposta:
    def __init__(self, corpo, status_code=200):
        self._corpo = corpo
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._corpo


class ClienteFake:
    def __init__(self, estacoes: dict, medicamentos: list[dict]):
        self.estacoes = estacoes
        self.medicamentos = medicamentos
        self.gets: list[str] = []
        self.posts: list[dict] = []

    async def get(self, url, timeout=None):
        self.gets.append(url)
        if url.endswith("/medicamentos"):
            return _Resposta(self.medicamentos)
        for base, estacao in self.estacoes.items():
            if url.startswith(base):
                return _Resposta(estacao.estado())
        raise ConnectionError(url)

    async def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        return _Resposta({"ok": True})

    @property
    def eventos(self) -> list[dict]:
        return [p["json"] for p in self.posts if p["url"].endswith("/api/v1/eventos/visao")]


class Ponte:
    """O adapter carregado + estação falsa + relógio, e um jeito de ler um slot."""
    def __init__(self, modulo, relogio, cliente, estacao_esq, estacao_dir):
        self.modulo = modulo
        self.relogio = relogio
        self.cliente = cliente
        self.esq = estacao_esq
        self.dir = estacao_dir

    def req(self, slot, medicamento, os_id="OS-1", **extra):
        return self.modulo.CapturarDispenserReq(
            slot_id=slot, os_id=os_id, medicamento_esperado=medicamento,
            sku_esperado=SEED.get(medicamento, ""), quantidade_esperada=3, **extra)

    def ler(self, slot, medicamento, os_id="OS-1", registrar=True) -> dict:
        """Registra o slot na OS (como o handler faz) e roda a leitura até o fim.

        Devolve o ÚNICO evento enviado ao central — mais de um é bug.
        """
        req = self.req(slot, medicamento, os_id)
        if registrar:
            self.modulo._registrar_na_os(os_id, slot, medicamento)
        camera = self.modulo.ponte.camera_do_slot(slot, self.modulo.NUM_SLOTS)
        antes = len(self.cliente.eventos)
        asyncio.run(self.modulo._ler_e_emitir(req, camera))
        novos = self.cliente.eventos[antes:]
        assert len(novos) == 1, novos
        return novos[0]


@pytest.fixture
def carregar_ponte(carregar_adapter, monkeypatch):
    def _carregar(linhas_esq=None, linhas_dir=None, medicamentos=None, fonte="estacao",
                  env=None):
        for nome in ("VISAO_MESA_FONTE", "VISAO_ETIQUETAS_ARQ", "NUM_SLOTS",
                     "VISAO_DISP_INTERVALO_CATALOGO_S", "VISAO_DISP_ASSENTAMENTO_S",
                     "VISAO_DISP_PRAZO_S", "VISAO_DISP_JANELA_S",
                     "VISAO_DISP_NAO_CADASTRADO", "TIMEOUT_VISAO_DISPENSER"):
            monkeypatch.delenv(nome, raising=False)
        modulo = carregar_adapter("vision", {
            "VISAO_DISPENSER_FONTE": fonte,
            "VISAO_DISP_ESQ_URL": ESQ, "VISAO_DISP_DIR_URL": DIR,
            **(env or {}),
        })
        relogio = Relogio()
        esq = EstacaoFalsa(relogio, linhas_esq or {})
        dir_ = EstacaoFalsa(relogio, linhas_dir or {}, nome="dispensers-dir")
        if medicamentos is None:
            medicamentos = [{"nome": n, "sku": s} for n, s in SEED.items()]
        cliente = ClienteFake({ESQ: esq, DIR: dir_}, medicamentos)
        modulo._client = cliente
        monkeypatch.setattr(modulo, "_relogio", lambda: relogio.t)
        monkeypatch.setattr(modulo, "_dormir", relogio.dormir)
        # O catálogo de antes de qualquer OS é antigo: o primeiro registro é que
        # o renova.
        modulo._t_catalogo = -1e9
        return Ponte(modulo, relogio, cliente, esq, dir_)
    return _carregar


def _linha(veredito, medicamento=None, sku=None):
    return {"veredito": veredito, "medicamento": medicamento, "sku": sku}


# ══════════════════════════════════════════════════════════════════════════════
# Task 4 — a tabela de etiquetas
# ══════════════════════════════════════════════════════════════════════════════

def test_a_tabela_versionada_carrega_inteira_sem_erro(caplog):
    spec =importlib.util.spec_from_file_location(
        "ponte_tabela", RAIZ_REPO / "vision-adapter" / "ponte_dispensers.py")
    ponte = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ponte)
    with caplog.at_level(logging.ERROR):
        etiquetas = ponte.carregar_etiquetas(ETIQUETAS)
    brutas = json.loads(ETIQUETAS.read_text(encoding="utf-8"))["etiquetas"]
    assert len(etiquetas) == len(brutas) >= 4
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_todo_nome_da_tabela_existe_no_catalogo_do_central():
    """Nome que não bate com `medicamentos.nome` é etiqueta que nunca será usada
    — o central manda carregar pelo nome do catálogo, e ninguém percebe."""
    brutas = json.loads(ETIQUETAS.read_text(encoding="utf-8"))["etiquetas"]
    fora = [e["nome"] for e in brutas if e["nome"] not in SEED]
    assert not fora, f"nomes da tabela que o central não tem: {fora}"


def test_tabela_com_erros_descarta_so_a_linha_ruim(carregar_ponte, tmp_path, caplog):
    ponte = carregar_ponte().modulo.ponte
    arquivo = tmp_path / "etiquetas.json"
    arquivo.write_text(json.dumps({"etiquetas": [
        {"nome": "MIOSAN 5MG", "qr": "MED-001", "aruco": 1},
        {"nome": "FLANCOX 500MG", "qr": "", "aruco": 2},           # qr vazio
        {"nome": "MECLIN 25MG", "qr": "MED-001", "aruco": 3},      # qr repetido
        {"nome": "LONIUM 40MG", "qr": "MED-004", "aruco": 1},      # aruco repetido
        {"nome": "RETEMIC 5MG", "qr": "MED-005", "aruco": 50},     # fora do DICT_4X4_50
        {"nome": "DONAREN 50MG", "qr": "MED-006", "aruco": -1},    # fora do DICT_4X4_50
        {"nome": " miosan  5mg ", "qr": "MED-007", "aruco": 7},    # nome repetido
        {"nome": "MECLIN 50MG", "qr": "MED-008", "aruco": "8"},    # aruco não inteiro
        {"nome": "MECLIN 25MG", "qr": "MED-009", "aruco": 9},
    ]}), encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        etiquetas = ponte.carregar_etiquetas(arquivo)

    assert [(e["qr"], e["posicao"]) for e in etiquetas] == [("MED-001", 1), ("MED-009", 9)]
    erros = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(erros) == 7, caplog.text


@pytest.mark.parametrize("conteudo", [None, "{nao e json", '{"etiquetas": 3}'])
def test_tabela_ilegivel_nao_derruba_o_adapter(carregar_ponte, tmp_path, caplog, conteudo):
    ponte = carregar_ponte().modulo.ponte
    arquivo = tmp_path / "etiquetas.json"
    if conteudo is not None:
        arquivo.write_text(conteudo, encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert ponte.carregar_etiquetas(arquivo) == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ══════════════════════════════════════════════════════════════════════════════
# O lado da câmera sai do slot — a mesma partição do simulador
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("num_slots", [2, 4, 8, 12])
def test_o_lado_do_slot_bate_com_o_do_vision_simulator(carregar_ponte, carregar_simulador,
                                                        num_slots):
    ponte = carregar_ponte().modulo.ponte
    simulador = carregar_simulador("vision", {"NUM_SLOTS": str(num_slots)}).modulo
    for slot in range(1, num_slots + 1):
        assert ponte.camera_do_slot(slot, num_slots) == simulador.camera_do_slot(slot), slot


# ══════════════════════════════════════════════════════════════════════════════
# (a) O catálogo
# ══════════════════════════════════════════════════════════════════════════════

CENARIOS = {
    "OS normal":            {1: "MIOSAN 5MG", 2: "FLANCOX 500MG", 3: "MECLIN 25MG"},
    "sem OS":               {},
    "slot sem etiqueta":    {1: "MIOSAN 5MG", 6: "RETEMIC 5MG"},
    "remedio duplicado":    {1: "MIOSAN 5MG", 5: "MIOSAN 5MG", 2: "LONIUM 40MG"},
    "tudo sem etiqueta":    {1: "RETEMIC 5MG", 2: "DONAREN 50MG"},
    "todas as etiquetas":   {1: "MIOSAN 5MG", 2: "FLANCOX 500MG", 7: "MECLIN 25MG",
                             8: "LONIUM 40MG"},
    "grafia diferente":     {4: "  flancox   500mg "},
}


@pytest.mark.parametrize("cenario", sorted(CENARIOS))
def test_o_catalogo_e_aceito_pela_estacao_de_verdade(carregar_ponte, Catalogo, cenario):
    """Recusado, o catálogo faz a estação seguir julgando pela OS ANTERIOR —
    em silêncio. Nenhum cenário pode produzir um."""
    modulo = carregar_ponte().modulo
    resposta = modulo.ponte.montar_catalogo(CENARIOS[cenario], modulo.ETIQUETAS)

    catalogo = Catalogo.de_itens(resposta["medicamentos"])  # levanta se recusar

    reais = [m.dispenser for m in catalogo.medicamentos if m.dispenser <= modulo.NUM_SLOTS]
    fantasmas = [m.dispenser for m in catalogo.medicamentos if m.dispenser > modulo.NUM_SLOTS]
    assert all(d > modulo.ponte.BASE_FANTASMA for d in fantasmas)
    assert set(reais) <= set(CENARIOS[cenario])
    # Toda etiqueta está no catálogo: num slot, ou como fantasma.
    assert len(catalogo.medicamentos) == len(modulo.ETIQUETAS)


def test_fantasma_tem_numero_estavel_e_fora_dos_slots(carregar_ponte):
    modulo = carregar_ponte().modulo
    resposta = modulo.ponte.montar_catalogo({3: "MECLIN 25MG"}, modulo.ETIQUETAS)
    por_qr = {m["qr"]: m["dispenser"] for m in resposta["medicamentos"]}
    assert por_qr == {"MED-001": 1001, "MED-002": 1002, "MED-003": 3, "MED-004": 1004}


def test_sem_etiqueta_e_duplicado_vao_para_incompletos(carregar_ponte):
    modulo = carregar_ponte().modulo
    resposta = modulo.ponte.montar_catalogo(
        {1: "MIOSAN 5MG", 5: "MIOSAN 5MG", 2: "LONIUM 40MG", 6: "RETEMIC 5MG"},
        modulo.ETIQUETAS)
    incompletos = {i["dispenser"]: i["motivo"] for i in resposta["incompletos"]}
    assert incompletos[6] == modulo.ponte.MOTIVO_SEM_ETIQUETA
    assert modulo.ponte.MOTIVO_REPETIDO in incompletos[1]
    assert modulo.ponte.MOTIVO_REPETIDO in incompletos[5]
    assert "D1, D5" in incompletos[1]
    # O slot que não tem problema segue.
    assert {"qr": "MED-004", "nome": "LONIUM 40MG", "dispenser": 2, "aruco": 4,
            "unidades_por_caixa": 1} in resposta["medicamentos"]


def test_os_nova_zera_o_catalogo(carregar_ponte):
    """Herdar o slot da OS anterior poria o mesmo remédio em dois dispensers."""
    modulo = carregar_ponte().modulo
    modulo._registrar_na_os("OS-A", 1, "MIOSAN 5MG")
    modulo._registrar_na_os("OS-A", 2, "FLANCOX 500MG")
    modulo._registrar_na_os("OS-B", 2, "MIOSAN 5MG")

    reais = {m["dispenser"]: m["nome"] for m in modulo._catalogo["medicamentos"]
             if m["dispenser"] <= modulo.NUM_SLOTS}
    assert reais == {2: "MIOSAN 5MG"}


def test_t_catalogo_so_muda_quando_o_conteudo_muda(carregar_ponte):
    p = carregar_ponte()
    p.modulo._registrar_na_os("OS-1", 1, "MIOSAN 5MG")
    t1 = p.modulo._t_catalogo
    p.relogio.t += 10
    p.modulo._registrar_na_os("OS-1", 1, "MIOSAN 5MG")   # re-scan: nada muda
    assert p.modulo._t_catalogo == t1
    p.modulo._registrar_na_os("OS-1", 2, "LONIUM 40MG")
    assert p.modulo._t_catalogo == t1 + 10


def test_o_endpoint_serve_o_catalogo_sem_token(carregar_ponte):
    modulo = carregar_ponte().modulo
    modulo._registrar_na_os("OS-1", 3, "MECLIN 25MG")
    resposta = TestClient(modulo.app).get("/api/visao/catalogo")
    assert resposta.status_code == 200
    # Só `medicamentos`: `incompletos` é uso interno — a estação imprimiria uma
    # linha por item a cada busca.
    assert resposta.json() == {"medicamentos": modulo._catalogo["medicamentos"]}


# ══════════════════════════════════════════════════════════════════════════════
# (b) O estoque que a estação publica é aceito e descartado
# ══════════════════════════════════════════════════════════════════════════════

def test_estoque_e_aceito_e_nao_vai_a_lugar_nenhum(carregar_ponte):
    p = carregar_ponte()
    resposta = TestClient(p.modulo.app).post("/api/visao/estoque", json={
        "estacao": "dispensers-esq",
        "leituras": [{"dispenser": 1, "caixas": 3, "veredito": "OK"}]})
    assert resposta.status_code == 200
    assert resposta.json() == {"ok": True, "resultados": []}
    assert p.cliente.posts == []


# ══════════════════════════════════════════════════════════════════════════════
# (c) Frescor
# ══════════════════════════════════════════════════════════════════════════════

def test_veredito_da_os_anterior_nao_vale(carregar_ponte):
    """A estação ainda julga pelo catálogo antigo até buscar o novo (t+2): até
    lá ela diz ERRO_POSICAO para o FLANCOX que agora está CERTO no D1. Ler na
    hora do comando travaria a OS por um erro que não existe."""
    relogio_inicio = 100.0
    linhas = {1: lambda t: (_linha("ERRO_POSICAO", "FLANCOX 500MG", "MED-002")
                            if t < relogio_inicio + 2.5
                            else _linha("OK", "FLANCOX 500MG", "MED-002"))}
    p = carregar_ponte(linhas_esq=linhas)

    evento = p.ler(1, "FLANCOX 500MG")

    assert evento["tipo"] == "leitura_dispenser_ok"
    assert p.relogio.t >= relogio_inicio + 4   # intervalo (2) + assentamento (2)


def test_momento_parado_vira_camera_sem_imagem(carregar_ponte):
    p = carregar_ponte(linhas_esq={1: _linha("OK", "MIOSAN 5MG", "MED-001")})
    p.esq.congelada_em = p.relogio.t        # câmera cega: momento não anda

    evento = p.ler(1, "MIOSAN 5MG")

    assert (evento["tipo"], evento["motivo"]) == ("leitura_dispenser_falha", "camera_sem_imagem")
    assert p.relogio.t >= 100.0 + p.modulo.VISAO_DISP_PRAZO_S


def test_catalogo_mudando_no_meio_reinicia_a_espera(carregar_ponte):
    """O comando do D2 da mesma OS chega no meio da espera do D1: o catálogo
    mudou de novo, e a estação ainda não o buscou."""
    p = carregar_ponte(linhas_esq={1: _linha("OK", "MIOSAN 5MG", "MED-001")})
    p.relogio.ganchos.append(
        (103.0, lambda: p.modulo._registrar_na_os("OS-1", 2, "LONIUM 40MG")))

    evento = p.ler(1, "MIOSAN 5MG")

    assert evento["tipo"] == "leitura_dispenser_ok"
    assert p.relogio.t >= 103.0 + 4


def test_rescan_com_catalogo_antigo_so_espera_o_assentamento(carregar_ponte):
    p = carregar_ponte(linhas_esq={3: _linha("OK", "MECLIN 25MG", "MED-003")})
    p.modulo._registrar_na_os("OS-1", 3, "MECLIN 25MG")
    p.relogio.t += 60                        # a trava durou um minuto

    evento = p.ler(3, "MECLIN 25MG")

    assert evento["tipo"] == "leitura_dispenser_ok"
    # Só os 2 s de assentamento, mais a janela de 3 momentos distintos — não o
    # intervalo do catálogo, que não mudou.
    assert 162.0 <= p.relogio.t < 166.0


def test_linha_de_estacao_antiga_com_momento_parado_e_ignorada(carregar_ponte):
    """O banco da estação guarda uma linha por (estacao, dispenser); a do nome
    antigo fica lá parada. Ler essa daria camera_sem_imagem com a câmera boa."""
    p = carregar_ponte(linhas_esq={1: _linha("OK", "MIOSAN 5MG", "MED-001")})
    estado_real = p.esq.estado

    def com_linha_morta():
        estado = estado_real()
        estado["estado"].insert(0, {"estacao": "PC-DA-BANCADA", "dispenser": 1,
                                    "momento": "2026-09-01T10:00:00+00:00",
                                    **_linha("ERRO_POSICAO", "LONIUM 40MG", "MED-004")})
        return estado
    p.esq.estado = com_linha_morta

    assert p.ler(1, "MIOSAN 5MG")["tipo"] == "leitura_dispenser_ok"


# ══════════════════════════════════════════════════════════════════════════════
# (c) Tradução — uma linha por caso da tabela
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("linha,tipo,motivo", [
    (_linha("OK", "MIOSAN 5MG", "MED-001"),            "leitura_dispenser_ok", None),
    (_linha("OK", "LONIUM 40MG", "MED-004"),           "leitura_dispenser_divergencia", None),
    (_linha("ERRO_POSICAO", "LONIUM 40MG", "MED-004"), "leitura_dispenser_divergencia", None),
    (_linha("DIVERGENCIA", "MIOSAN 5MG", "MED-001"),   "leitura_dispenser_divergencia", None),
    (_linha("NAO_CADASTRADO"),                         "leitura_dispenser_falha", "codigo_desconhecido_na_zona"),
    (_linha("VAZIO"),                                  "leitura_dispenser_falha", "produto_nao_identificado"),
    (_linha("INDETERMINADO", "MIOSAN 5MG", "MED-001"), "leitura_dispenser_falha", "leitura_inconclusiva"),
    (_linha("VEREDITO_NOVO", "MIOSAN 5MG", "MED-001"), "leitura_dispenser_falha", "leitura_inconclusiva"),
], ids=["ok", "ok-outro-nome", "erro-posicao", "divergencia", "nao-cadastrado",
        "vazio", "indeterminado", "veredito-desconhecido"])
def test_traducao_do_veredito(carregar_ponte, linha, tipo, motivo):
    p = carregar_ponte(linhas_esq={1: linha})

    evento = p.ler(1, "MIOSAN 5MG")

    assert evento["tipo"] == tipo
    assert evento.get("motivo") == motivo
    assert evento["camera"] == "dispenser_esq"
    assert evento["match_sku"] is (tipo == "leitura_dispenser_ok")
    if tipo == "leitura_dispenser_falha":
        assert evento["confianca"] == 0.0


def test_divergencia_diz_o_que_foi_achado_com_o_sku_do_central(carregar_ponte):
    """A trava mostra `lido=<sku_lido>` ao supervisor: o SKU do catálogo do
    central, não o código da etiqueta, que ninguém lá reconhece."""
    p = carregar_ponte(linhas_dir={7: _linha("ERRO_POSICAO", "LONIUM 40MG", "MED-004")})

    evento = p.ler(7, "MECLIN 25MG", os_id="OS-77")

    assert evento == {**evento,
                      "tipo": "leitura_dispenser_divergencia", "camera": "dispenser_dir",
                      "slot_id": 7, "os_id": "OS-77",
                      "sku_esperado": SEED["MECLIN 25MG"],
                      "sku_lido": SEED["LONIUM 40MG"],
                      "medicamento_lido": "LONIUM 40MG", "codigo_etiqueta": "MED-004",
                      "veredito_estacao": "ERRO_POSICAO", "estacao": "dispensers-dir",
                      "match_sku": False, "falha_injetada": False, "fonte": "estacao"}
    assert 0.0 <= evento["confianca"] <= 1.0
    assert datetime.fromisoformat(evento["ts"]).tzinfo is not None
    assert datetime.fromisoformat(evento["momento_leitura"]).tzinfo is not None


def test_nome_que_o_central_nao_tem_vai_com_o_codigo_da_etiqueta(carregar_ponte):
    p = carregar_ponte(linhas_esq={1: _linha("ERRO_POSICAO", "LONIUM 40MG", "MED-004")},
                       medicamentos=[{"nome": "MIOSAN 5MG", "sku": SEED["MIOSAN 5MG"]}])
    assert p.ler(1, "MIOSAN 5MG")["sku_lido"] == "MED-004"


def test_nada_identificado_diz_que_e_desconhecido(carregar_ponte):
    """Com o modo antigo (NAO_CADASTRADO é divergência), a trava diz que o código
    lido é desconhecido."""
    p = carregar_ponte(linhas_esq={1: _linha("NAO_CADASTRADO")},
                       env={"VISAO_DISP_NAO_CADASTRADO": "divergencia"})
    evento = p.ler(1, "MIOSAN 5MG")
    assert evento["tipo"] == "leitura_dispenser_divergencia"
    assert evento["sku_lido"] == p.modulo.ponte.SKU_DESCONHECIDO


def test_estacao_fora_do_ar_vira_camera_indisponivel(carregar_ponte):
    p = carregar_ponte(linhas_esq={1: _linha("OK", "MIOSAN 5MG", "MED-001")})
    p.esq.no_ar = False
    evento = p.ler(1, "MIOSAN 5MG")
    assert (evento["tipo"], evento["motivo"]) == ("leitura_dispenser_falha", "camera_indisponivel")


def test_zona_sem_linha_vira_zona_nao_calibrada(carregar_ponte):
    p = carregar_ponte(linhas_esq={2: _linha("OK", "FLANCOX 500MG", "MED-002")})
    evento = p.ler(1, "MIOSAN 5MG")
    assert (evento["tipo"], evento["motivo"]) == ("leitura_dispenser_falha", "zona_nao_calibrada")


def test_sem_etiqueta_falha_na_hora_sem_perguntar_a_estacao(carregar_ponte):
    p = carregar_ponte(linhas_esq={1: _linha("OK", "RETEMIC 5MG", None)})
    evento = p.ler(1, "RETEMIC 5MG")
    assert (evento["tipo"], evento["motivo"]) == ("leitura_dispenser_falha", "sem_etiqueta_cadastrada")
    assert not [u for u in p.cliente.gets if u.startswith(ESQ)]
    assert p.relogio.t == 100.0


def test_remedio_repetido_na_os_falha_sem_perguntar_a_estacao(carregar_ponte):
    """O slot está fora do catálogo: perguntado, a estação julgaria a caixa CERTA
    como ERRO_POSICAO (ela aponta para o fantasma) e travaria a OS à toa."""
    p = carregar_ponte(linhas_esq={1: _linha("ERRO_POSICAO", "MIOSAN 5MG", "MED-001")})
    p.modulo._registrar_na_os("OS-1", 3, "MIOSAN 5MG")
    evento = p.ler(1, "MIOSAN 5MG")
    assert (evento["tipo"], evento["motivo"]) == ("leitura_dispenser_falha", "medicamento_repetido_na_os")


def test_excecao_vira_um_evento_de_falha(carregar_ponte, monkeypatch):
    p = carregar_ponte(linhas_esq={1: _linha("OK", "MIOSAN 5MG", "MED-001")})

    def quebra(*_a, **_k):
        raise RuntimeError("bug")
    monkeypatch.setattr(p.modulo.ponte, "traduzir", quebra)

    evento = p.ler(1, "MIOSAN 5MG")   # `ler` exige exatamente um evento
    assert (evento["tipo"], evento["motivo"]) == ("leitura_dispenser_falha", "erro_interno")


# ══════════════════════════════════════════════════════════════════════════════
# O endpoint do comando
# ══════════════════════════════════════════════════════════════════════════════

def test_comando_responde_na_hora_e_le_em_segundo_plano(carregar_ponte, caplog):
    p = carregar_ponte(linhas_dir={6: _linha("OK", "FLANCOX 500MG", "MED-002")})
    req = p.req(6, "FLANCOX 500MG", injetar_falha="sku_dispenser")

    async def cenario():
        resposta = await p.modulo.capturar_dispenser(req)
        assert p.cliente.eventos == []          # ainda não leu nada
        await asyncio.gather(*list(p.modulo._leituras))
        return resposta

    with caplog.at_level(logging.WARNING):
        resposta = asyncio.run(cenario())

    assert resposta == {"ok": True, "camera": "dispenser_dir", "slot_id": 6,
                        "msg": "Lendo dispenser 6 na estação dispenser_dir"}
    # A injeção, com a câmera real, é feita pelo adapter sobre a leitura
    # verdadeira (que aqui foi OK).
    (evento,) = p.cliente.eventos
    assert (evento["tipo"], evento["sku_lido"], evento["falha_injetada"]) == \
        ("leitura_dispenser_divergencia", "APSEN-INJETADO-000", True)


def test_modo_simulador_continua_mandando_para_o_vision_sim(carregar_ponte):
    p = carregar_ponte(fonte="simulador")
    asyncio.run(p.modulo.capturar_dispenser(p.req(2, "FLANCOX 500MG")))
    assert [x["url"] for x in p.cliente.posts] == [
        p.modulo.VISION_SIM_URL + "/executar/capturar/dispenser"]
    assert p.modulo._slots_os == {}


# ══════════════════════════════════════════════════════════════════════════════
# A janela de estabilidade — o veredito da estação é a fotografia de um frame
# ══════════════════════════════════════════════════════════════════════════════

ERRO_LONIUM = _linha("ERRO_POSICAO", "LONIUM 40MG", "MED-004")
ERRO_MECLIN = _linha("ERRO_POSICAO", "MECLIN 25MG", "MED-003")
OK_MIOSAN = _linha("OK", "MIOSAN 5MG", "MED-001")


@pytest.mark.parametrize("amostras,tipo,motivo", [
    ([OK_MIOSAN, ERRO_LONIUM, OK_MIOSAN], "leitura_dispenser_ok", None),
    ([ERRO_LONIUM, ERRO_LONIUM, OK_MIOSAN], "leitura_dispenser_falha", "leitura_instavel"),
    ([OK_MIOSAN, ERRO_LONIUM, ERRO_LONIUM], "leitura_dispenser_divergencia", None),
    ([ERRO_LONIUM, ERRO_LONIUM, ERRO_MECLIN], "leitura_dispenser_falha", "leitura_instavel"),
    ([_linha("VAZIO"), _linha("VAZIO"), OK_MIOSAN], "leitura_dispenser_falha",
     "leitura_instavel"),
    ([_linha("VAZIO")] * 3, "leitura_dispenser_falha", "produto_nao_identificado"),
    ([OK_MIOSAN] * 3, "leitura_dispenser_ok", None),
    ([ERRO_LONIUM] * 3, "leitura_dispenser_divergencia", None),
], ids=["um-frame-ruim", "ultima-nao-confirma", "confirmada", "medicamentos-diferentes",
        "vazio-instavel", "vazio-estavel", "ok-estavel", "erro-estavel"])
def test_decisao_da_janela(carregar_ponte, amostras, tipo, motivo):
    ponte = carregar_ponte().modulo.ponte
    t, m, linha, contagens = ponte.decidir(amostras, "MIOSAN 5MG")
    assert (t, m) == (tipo, motivo)
    assert sum(contagens.values()) == len(amostras)
    assert linha in amostras


def test_um_frame_com_erro_deixa_de_travar(carregar_ponte):
    """A mão do operador num frame: antes, a primeira amostra fresca decidia."""
    def linha(t):
        return ERRO_LONIUM if 104.0 <= t < 105.0 else OK_MIOSAN
    p = carregar_ponte(linhas_esq={1: linha})

    evento = p.ler(1, "MIOSAN 5MG")

    assert evento["tipo"] == "leitura_dispenser_ok"
    assert evento["amostras"] == {"OK:MIOSAN 5MG": 2, "ERRO_POSICAO:LONIUM 40MG": 1}


def test_janela_conta_momentos_distintos(carregar_ponte):
    """Sondagem a cada 0,5 s e `momento` de 1 s: a mesma linha lida duas vezes
    não é duas amostras."""
    p = carregar_ponte(linhas_esq={1: OK_MIOSAN})
    evento = p.ler(1, "MIOSAN 5MG")
    assert sum(evento["amostras"].values()) == 3
    assert p.relogio.t >= 106.0               # 4 s de frescor + 2 momentos a mais


def test_janela_que_nao_fecha_no_prazo_falha_com_o_motivo_de_hoje(carregar_ponte):
    p = carregar_ponte(linhas_esq={1: OK_MIOSAN})
    # A câmera congela logo depois da primeira amostra fresca.
    p.relogio.ganchos.append((104.6, lambda: setattr(p.esq, "congelada_em", 104.6)))

    evento = p.ler(1, "MIOSAN 5MG")

    assert (evento["tipo"], evento["motivo"]) == ("leitura_dispenser_falha",
                                                  "camera_sem_imagem")


# ══════════════════════════════════════════════════════════════════════════════
# A estação PROVA que buscou o catálogo desta OS
# ══════════════════════════════════════════════════════════════════════════════

def _buscar(p, lado):
    return lambda: p.modulo.visao_catalogo_do_lado(lado)


def test_busca_depois_da_mudanca_libera(carregar_ponte):
    p = carregar_ponte(linhas_esq={1: OK_MIOSAN})
    p.relogio.ganchos.append((102.0, _buscar(p, "esq")))

    assert p.ler(1, "MIOSAN 5MG")["tipo"] == "leitura_dispenser_ok"


def test_busca_antes_da_mudanca_nao_libera(carregar_ponte):
    """Ela buscou — o catálogo da OS ANTERIOR. O relógio diria que já vale."""
    p = carregar_ponte(linhas_esq={1: OK_MIOSAN})
    p.modulo._ultima_busca["esq"] = 99.0

    evento = p.ler(1, "MIOSAN 5MG")

    assert (evento["tipo"], evento["motivo"]) == ("leitura_dispenser_falha",
                                                  "estacao_sem_catalogo_atual")


def test_busca_do_outro_lado_nao_libera(carregar_ponte):
    p = carregar_ponte(linhas_esq={1: OK_MIOSAN})
    p.modulo._ultima_busca["esq"] = 50.0
    p.relogio.ganchos.append((102.0, _buscar(p, "dir")))

    evento = p.ler(1, "MIOSAN 5MG")

    assert evento["motivo"] == "estacao_sem_catalogo_atual"


def test_estacao_na_url_antiga_cai_no_criterio_de_antes_com_um_aviso(carregar_ponte,
                                                                    caplog):
    p = carregar_ponte(linhas_esq={1: OK_MIOSAN, 2: _linha("OK", "FLANCOX 500MG",
                                                           "MED-002")})
    with caplog.at_level(logging.WARNING):
        assert p.ler(1, "MIOSAN 5MG")["tipo"] == "leitura_dispenser_ok"
        assert p.ler(2, "FLANCOX 500MG")["tipo"] == "leitura_dispenser_ok"

    avisos = [r for r in caplog.records if "/estacoes/esq/api/visao/catalogo" in r.getMessage()]
    assert len(avisos) == 1


def test_rota_por_lado_registra_e_rejeita_lado_desconhecido(carregar_ponte):
    p = carregar_ponte()
    cliente = TestClient(p.modulo.app)
    assert cliente.get("/estacoes/dir/api/visao/catalogo").status_code == 200
    assert p.modulo._ultima_busca == {"dir": p.relogio.t}
    assert cliente.get("/estacoes/meio/api/visao/catalogo").status_code == 404
    assert cliente.post("/estacoes/esq/api/visao/estoque", json={}).json() == \
        {"ok": True, "resultados": []}


def test_o_catalogo_servido_pela_rota_do_lado_e_aceito_pela_estacao(carregar_ponte, Catalogo):
    p = carregar_ponte()
    p.modulo._registrar_na_os("OS-1", 1, "MIOSAN 5MG")
    p.modulo._registrar_na_os("OS-1", 2, "RETEMIC 5MG")      # sem etiqueta
    servido = TestClient(p.modulo.app).get("/estacoes/esq/api/visao/catalogo").json()
    assert set(servido) == {"medicamentos"}
    Catalogo.de_itens(servido["medicamentos"])               # levanta se recusar


# ══════════════════════════════════════════════════════════════════════════════
# QR alheio na zona: VISAO_DISP_NAO_CADASTRADO
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("modo,tipo,motivo", [
    ("falha", "leitura_dispenser_falha", "codigo_desconhecido_na_zona"),
    ("divergencia", "leitura_dispenser_divergencia", None),
])
def test_nao_cadastrado_nos_dois_modos(carregar_ponte, modo, tipo, motivo):
    p = carregar_ponte(linhas_esq={1: _linha("NAO_CADASTRADO")},
                       env={"VISAO_DISP_NAO_CADASTRADO": modo})
    evento = p.ler(1, "MIOSAN 5MG")
    assert (evento["tipo"], evento.get("motivo")) == (tipo, motivo)


def test_nao_cadastrado_invalido_cai_em_falha_com_erro(carregar_ponte, caplog):
    with caplog.at_level(logging.ERROR):
        p = carregar_ponte(env={"VISAO_DISP_NAO_CADASTRADO": "trava"})
    assert p.modulo.VISAO_DISP_NAO_CADASTRADO == "falha"
    assert "VISAO_DISP_NAO_CADASTRADO" in caplog.text


# ══════════════════════════════════════════════════════════════════════════════
# Injeção de falha com a câmera REAL dos dispensers
# ══════════════════════════════════════════════════════════════════════════════

def _ler_com_injecao(p, slot, medicamento, injetar):
    req = p.req(slot, medicamento, injetar_falha=injetar)
    p.modulo._registrar_na_os("OS-1", slot, medicamento)
    camera = p.modulo.ponte.camera_do_slot(slot, p.modulo.NUM_SLOTS)
    asyncio.run(p.modulo._ler_e_emitir(req, camera))
    (evento,) = p.cliente.eventos
    return evento


def test_injecao_de_sku_errado_reescreve_a_leitura_real(carregar_ponte):
    p = carregar_ponte(linhas_esq={1: OK_MIOSAN})
    evento = _ler_com_injecao(p, 1, "MIOSAN 5MG", "sku_dispenser")
    assert (evento["tipo"], evento["sku_lido"], evento["falha_injetada"]) == \
        ("leitura_dispenser_divergencia", "APSEN-INJETADO-000", True)


def test_injecao_de_falha_de_leitura_reescreve_a_leitura_real(carregar_ponte):
    p = carregar_ponte(linhas_esq={1: OK_MIOSAN})
    evento = _ler_com_injecao(p, 1, "MIOSAN 5MG", "falha_leitura_dispenser")
    assert (evento["tipo"], evento["motivo"], evento["falha_injetada"]) == \
        ("leitura_dispenser_falha", "injetada", True)


def test_sem_injecao_o_evento_nao_e_marcado(carregar_ponte):
    p = carregar_ponte(linhas_esq={1: OK_MIOSAN})
    assert p.ler(1, "MIOSAN 5MG")["falha_injetada"] is False


def test_as_strings_de_injecao_do_adapter_batem_com_o_catalogo(carregar_ponte,
                                                               carregar_orquestrador):
    """A quinta cópia das strings (as outras quatro: `tests/test_injecao.py`)."""
    injecao = carregar_orquestrador().injecao
    modulo = carregar_ponte().modulo
    assert modulo.INJECAO_SKU_DISPENSER == injecao.TIPO_SKU_DISPENSER
    assert modulo.INJECAO_FALHA_LEITURA_DISPENSER == injecao.TIPO_FALHA_LEITURA_DISP
    assert modulo.INJECAO_DIVERGENCIA_MESA == injecao.TIPO_DIVERGENCIA_MESA
