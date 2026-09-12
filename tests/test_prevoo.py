# -*- coding: utf-8 -*-
"""
Tela de pré-voo — "está tudo de pé para apresentar?".

Três frentes, e cada uma cobre uma promessa que a tela faz:

1. **A tabela de serviços conhece a stack INTEIRA.** `prevoo.SERVICOS` é
   comparada contra o `docker-compose.yml` — nome, porta e caminho. Uma tabela
   desatualizada faria a tela dizer "tudo verde" sobre uma stack que ela não
   conhece por inteiro, que é o pior modo possível de falhar para uma página de
   conferência.

2. **Um serviço morto produz UM item vermelho, não uma página travada.** É o
   caso em que a tela é mais necessária. O teste mede a CONCORRÊNCIA das sondas
   (quantas ficam em voo ao mesmo tempo), e não o tempo de parede: cronômetro em
   suíte é flaky, e "demorou menos" não diz qual espera sumiu — a mesma escolha
   que `test_orchestrator.py` já faz para o `gather` das etapas 3 e 3b.

3. **Item que não está verde diz o que FAZER.** É o que separa esta tela de um
   relatório. Cobrado por varredura, não item a item: um item novo entra na
   conta sozinho.
"""
import asyncio
import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

RAIZ_REPO = Path(__file__).resolve().parent.parent
CENTRAL_DIR = RAIZ_REPO / "central-computer"


@pytest.fixture(scope="module")
def prevoo():
    """`prevoo.py` por caminho. Não importa FastAPI nem `database`."""
    caminho = CENTRAL_DIR / "prevoo.py"
    spec = importlib.util.spec_from_file_location("apsen_prevoo", caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["apsen_prevoo"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((RAIZ_REPO / "docker-compose.yml").read_text(encoding="utf-8"))


# ── Duplos de cliente HTTP ────────────────────────────────────────────────────

class RespostaFake:
    def __init__(self, status_code=200):
        self.status_code = status_code


class TimeoutFake(Exception):
    """Nome com "timeout" dentro, como o `httpx.TimeoutException`.

    `prevoo.py` separa timeout de recusa pelo NOME da classe justamente para não
    importar httpx — o duplo tem que exercitar esse caminho, não contorná-lo.
    """


class ClienteFake:
    """Cliente HTTP que responde por host e conta quantas sondas ficam em voo.

    `caidos` recusa a conexão (o serviço não subiu), `lentos` estoura o timeout
    (subiu e travou) e `erros` devolve um HTTP feio. São três desfechos que
    pedem AÇÕES diferentes na tela.
    """

    def __init__(self, caidos=(), lentos=(), erros=None, atraso=0.02):
        self.caidos = set(caidos)
        self.lentos = set(lentos)
        self.erros = dict(erros or {})
        self.atraso = atraso
        self.urls: list[str] = []
        self.em_voo = 0
        self.pico_em_voo = 0

    async def get(self, url, timeout=None):
        self.urls.append(url)
        self.em_voo += 1
        self.pico_em_voo = max(self.pico_em_voo, self.em_voo)
        try:
            # O `sleep` existe para que as sondas se sobreponham de verdade:
            # sem ele cada corrotina terminaria antes de a seguinte começar e o
            # pico seria 1 mesmo com `gather`.
            await asyncio.sleep(self.atraso)
            host = url.split("//", 1)[1].split(":", 1)[0]
            if host in self.caidos:
                raise ConnectionError("Connection refused")
            if host in self.lentos:
                raise TimeoutFake("read timeout")
            return RespostaFake(self.erros.get(host, 200))
        finally:
            self.em_voo -= 1


def _por_id(itens) -> dict:
    return {i["id"]: i for i in itens}


# ── 1. A tabela conhece a stack inteira ───────────────────────────────────────

def test_tabela_cobre_todos_os_servicos_do_compose(prevoo, compose):
    """Sem esta comparação, um serviço novo ficaria fora da conferência e a
    tela diria "tudo verde" sobre uma stack que ela não conhece inteira."""
    do_compose = set(compose["services"])
    conhecidos = ({s.nome for s in prevoo.SERVICOS}
                  | {prevoo.NOME_MYSQL, prevoo.NOME_GERADOR})
    assert conhecidos == do_compose, do_compose ^ conhecidos


def test_portas_e_caminhos_batem_com_o_healthcheck(prevoo, compose):
    """A sonda tem que bater na MESMA porta e no mesmo caminho que o compose
    usa para decidir se o serviço está saudável — duas respostas diferentes para
    a mesma pergunta seriam pior que nenhuma."""
    for servico in prevoo.SERVICOS:
        teste = compose["services"][servico.nome]["healthcheck"]["test"]
        texto = " ".join(str(x) for x in teste) if isinstance(teste, list) else str(teste)
        achado = re.search(r"http://localhost:(\d+)(/\w*)", texto)
        assert achado, servico.nome
        porta, caminho = achado.groups()
        assert int(porta) == servico.porta, servico.nome
        assert caminho == servico.caminho, servico.nome


def test_porta_do_compose_e_a_publicada(prevoo, compose):
    """A sonda fala pela rede interna, mas a porta é a mesma dos dois lados —
    divergir aqui faria a tela sondar um serviço que não existe naquela porta."""
    for servico in prevoo.SERVICOS:
        publicadas = compose["services"][servico.nome].get("ports") or []
        assert any(str(servico.porta) in str(p) for p in publicadas), servico.nome


def test_central_nao_se_sonda_por_http(prevoo):
    """Um auto-GET testaria sobretudo o event loop que acabou de servir ESTA
    requisição — e um timeout ali produziria o relatório absurdo "o central está
    fora" numa página que o central acabou de entregar."""
    assert prevoo.NOME_CENTRAL not in {s.nome for s in prevoo.SONDAVEIS}
    assert prevoo.item_central()["estado"] == prevoo.OK


def test_erp_simulator_aparece_mesmo_sem_porta(prevoo, compose):
    """Ele é worker de laço, sem API. Sumir faria a tela dizer "12 de 12" sobre
    uma stack de 13."""
    assert not (compose["services"][prevoo.NOME_GERADOR].get("ports") or [])
    item = prevoo.item_gerador(False)
    assert item["id"] == prevoo.NOME_GERADOR
    assert item["estado"] == prevoo.INFO


def test_gerador_pausado_vira_alerta_com_acao(prevoo):
    """Gerador pausado é uma das causas de "a planta não está fazendo nada"."""
    item = prevoo.item_gerador(True)
    assert item["estado"] == prevoo.ALERTA
    assert item["acao"]


# ── 2. Serviço fora: um item vermelho, e a tela não trava ─────────────────────

def test_stack_saudavel_fica_toda_verde(prevoo):
    cliente = ClienteFake()
    itens = asyncio.run(prevoo.sondar_servicos(cliente, timeout=0.5))

    assert len(itens) == len(prevoo.SONDAVEIS)
    assert all(i["estado"] == prevoo.OK for i in itens), itens
    assert all(not i["acao"] for i in itens), "item verde não precisa de ação"


def test_um_servico_fora_deixa_so_ele_vermelho(prevoo):
    cliente = ClienteFake(caidos={"vision-simulator"})
    itens = _por_id(asyncio.run(prevoo.sondar_servicos(cliente, timeout=0.5)))

    assert itens["vision-simulator"]["estado"] == prevoo.FALHA
    assert "docker compose up -d vision-simulator" in itens["vision-simulator"]["acao"]
    outros = [i for k, i in itens.items() if k != "vision-simulator"]
    assert all(i["estado"] == prevoo.OK for i in outros)


def test_servico_travado_e_servico_ausente_pedem_acoes_diferentes(prevoo):
    """"Não subiu" resolve-se com `up -d`; "subiu e não responde" só olhando o
    log. Dar a mesma frase aos dois desperdiça metade do valor da tela."""
    cliente = ClienteFake(caidos={"cnc-adapter"}, lentos={"weight-adapter"})
    itens = _por_id(asyncio.run(prevoo.sondar_servicos(cliente, timeout=0.5)))

    ausente = itens["cnc-adapter"]["acao"]
    travado = itens["weight-adapter"]["acao"]
    assert "up -d" in ausente and "logs" not in ausente
    assert "logs" in travado and "up -d" not in travado
    assert itens["weight-adapter"]["estado"] == prevoo.FALHA


def test_http_feio_tambem_e_falha(prevoo):
    cliente = ClienteFake(erros={"dashboard": 500})
    itens = _por_id(asyncio.run(prevoo.sondar_servicos(cliente, timeout=0.5)))
    assert itens["dashboard"]["estado"] == prevoo.FALHA
    assert "500" in itens["dashboard"]["detalhe"]


def test_sondas_correm_todas_ao_mesmo_tempo(prevoo):
    """A promessa que faz a tela responder em segundos.

    Mede a CONCORRÊNCIA, não o tempo de parede: cronômetro em suíte é flaky, e
    "demorou menos" não diz qual espera sumiu. Em série o pico seria 1, e o
    tempo total seria 11 × timeout — com um serviço morto, a tela levaria meio
    minuto justamente quando é mais necessária.
    """
    cliente = ClienteFake()
    asyncio.run(prevoo.sondar_servicos(cliente, timeout=0.5))
    assert cliente.pico_em_voo == len(prevoo.SONDAVEIS)


def test_sonda_que_explode_de_jeito_novo_nao_apaga_as_outras(prevoo):
    """`return_exceptions=True` é o cinto sobre o suspensório: sem ele, a página
    apareceria em branco no dia em que um serviço quebrasse de um jeito novo."""
    class ClienteQuebrado(ClienteFake):
        async def get(self, url, timeout=None):
            if "cnc-simulator" in url:
                raise BaseException("falha fora do contrato")  # noqa: TRY002
            return await super().get(url, timeout=timeout)

    itens = _por_id(asyncio.run(
        prevoo.sondar_servicos(ClienteQuebrado(), timeout=0.5)))

    assert len(itens) == len(prevoo.SONDAVEIS)
    assert itens["cnc-simulator"]["estado"] == prevoo.FALHA
    assert itens["dashboard"]["estado"] == prevoo.OK


# ── Banco ─────────────────────────────────────────────────────────────────────

def _diagnostico(**over) -> dict:
    base = {"versao": "8.0.36", "tabelas": 11, "schema_faltando": [],
            "medicamentos": 96, "slots_presentes": 8, "slots_esperados": 8}
    base.update(over)
    return base


def test_banco_saudavel_fica_verde(prevoo):
    itens = _por_id(prevoo.itens_banco(_diagnostico()))
    assert {i["estado"] for i in itens.values()} == {prevoo.OK}


def test_banco_fora_vira_um_item_so(prevoo):
    """Com o MySQL fora, dizer também "schema incompleto" e "catálogo vazio"
    seria derivar três diagnósticos da mesma causa — e quem lê acabaria caçando
    dois problemas que não existem."""
    itens = prevoo.itens_banco(None)
    assert len(itens) == 1
    assert itens[0]["id"] == "mysql"
    assert itens[0]["estado"] == prevoo.FALHA
    assert "up -d mysql" in itens[0]["acao"]


def test_schema_incompleto_manda_reiniciar_o_central(prevoo):
    """`init_db` cria o que falta em todo startup — é essa a saída, e não mexer
    no MySQL à mão."""
    itens = _por_id(prevoo.itens_banco(
        _diagnostico(schema_faltando=["`ordens`.`categoria`"])))
    assert itens["schema"]["estado"] == prevoo.FALHA
    assert "restart" in itens["schema"]["acao"]
    assert itens["mysql"]["estado"] == prevoo.OK


def test_catalogo_vazio_e_falha(prevoo):
    itens = _por_id(prevoo.itens_banco(_diagnostico(medicamentos=0)))
    assert itens["catalogo"]["estado"] == prevoo.FALHA
    assert "SKU" in itens["catalogo"]["detalhe"]


def test_slots_faltando_e_falha(prevoo):
    itens = _por_id(prevoo.itens_banco(_diagnostico(slots_presentes=6)))
    assert itens["slots_banco"]["estado"] == prevoo.FALHA
    assert "6 de 8" in itens["slots_banco"]["detalhe"]


# ── Ordens ────────────────────────────────────────────────────────────────────

def test_templates_validos(prevoo):
    assert prevoo.item_templates([], 10, True)["estado"] == prevoo.OK


def test_templates_sem_catalogo_e_alerta_e_nao_verde(prevoo):
    """`problemas` vazio com o catálogo FORA não significa "válido": só as
    checagens estruturais rodaram. Verde aqui seria um OK não verificado."""
    item = prevoo.item_templates([], 10, False)
    assert item["estado"] == prevoo.ALERTA
    assert item["acao"]


def test_template_quebrado_avisa_que_o_gerador_descarta(prevoo):
    item = prevoo.item_templates(["OS-X: medicamento fora do catálogo"], 10, True)
    assert item["estado"] == prevoo.FALHA
    assert "DESCARTA" in item["acao"]


def test_fila_cheia_vira_alerta(prevoo):
    assert prevoo.item_fila(2, 5)["estado"] == prevoo.OK
    cheia = prevoo.item_fila(5, 5)
    assert cheia["estado"] == prevoo.ALERTA
    assert "429" in cheia["detalhe"]


# ── Célula ────────────────────────────────────────────────────────────────────

HOME = (-120.0, 0.0)


def _snapshot(**over) -> dict:
    base = {
        "trava": {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""},
        "cnc": {"posicao_x": -120.0, "posicao_y": 0.0},
        "os_ativa": None,
        "alarmes_ativos": 0,
    }
    base.update(over)
    return base


def test_celula_em_repouso_fica_verde(prevoo):
    itens = _por_id(prevoo.itens_celula(_snapshot(), HOME))
    assert {i["estado"] for i in itens.values()} == {prevoo.OK}


def test_trava_ativa_e_falha_com_o_motivo(prevoo):
    """É o item mais importante da tela: com a trava ativa, NENHUMA OS anda."""
    itens = _por_id(prevoo.itens_celula(_snapshot(trava={
        "ativa": True, "os_id": "OS-9", "slot_id": 3,
        "motivo": "SKU errado em D3"}), HOME))
    assert itens["trava"]["estado"] == prevoo.FALHA
    assert "OS-9" in itens["trava"]["detalhe"]
    assert "D3" in itens["trava"]["detalhe"]
    assert "libere" in itens["trava"]["acao"].lower()


def test_cnc_fora_de_home_e_alerta_nao_falha(prevoo):
    """Fora de HOME é normal logo depois de uma OS — pintar de vermelho
    ensinaria a ignorar o vermelho."""
    itens = _por_id(prevoo.itens_celula(
        _snapshot(cnc={"posicao_x": 240.0, "posicao_y": -150.0}), HOME))
    assert itens["cnc"]["estado"] == prevoo.ALERTA
    assert "reset" in itens["cnc"]["acao"].lower()


def test_cnc_sem_posicao_conhecida(prevoo):
    itens = _por_id(prevoo.itens_celula(
        _snapshot(cnc={"posicao_x": None, "posicao_y": None}), HOME))
    assert itens["cnc"]["estado"] == prevoo.ALERTA


def test_os_em_execucao_avisa_que_o_reset_recusa(prevoo):
    itens = _por_id(prevoo.itens_celula(
        _snapshot(os_ativa={"os_id": "OS-7"}), HOME))
    assert itens["os_ativa"]["estado"] == prevoo.ALERTA
    assert "recusa" in itens["os_ativa"]["acao"]


def test_alarmes_abertos_viram_alerta(prevoo):
    itens = _por_id(prevoo.itens_celula(_snapshot(alarmes_ativos=4), HOME))
    assert itens["alarmes"]["estado"] == prevoo.ALERTA
    assert "4" in itens["alarmes"]["detalhe"]


# ── Modo ──────────────────────────────────────────────────────────────────────

def test_modo_realista_sem_gatilho_nao_pede_nada(prevoo):
    itens = _por_id(prevoo.itens_modo(False, 1.0, None, 3))
    assert itens["modo"]["estado"] == prevoo.INFO
    assert itens["velocidade"]["estado"] == prevoo.INFO
    assert itens["injecao"]["estado"] == prevoo.OK
    assert "3" in itens["websocket"]["detalhe"]


def test_modo_apresentacao_vira_alerta_e_lembra_da_injecao(prevoo):
    """Modo apresentação esquecido passa por "o hardware nunca falha"."""
    itens = _por_id(prevoo.itens_modo(True, 1.0, None, 0))
    assert itens["modo"]["estado"] == prevoo.ALERTA
    assert "injeção" in itens["modo"]["acao"].lower()


def test_fator_diferente_de_um_vira_alerta(prevoo):
    itens = _por_id(prevoo.itens_modo(False, 2.0, None, 0))
    assert itens["velocidade"]["estado"] == prevoo.ALERTA
    assert "2.00" in itens["velocidade"]["detalhe"]
    assert "FATOR_VELOCIDADE" in itens["velocidade"]["acao"]


def test_falha_armada_aparece_com_slot(prevoo):
    """Gatilho esquecido dispara no meio de outra explicação."""
    itens = _por_id(prevoo.itens_modo(
        True, 1.0, {"tipo": "divergencia_peso", "slot_id": 3}, 1))
    assert itens["injecao"]["estado"] == prevoo.ALERTA
    assert "divergencia_peso" in itens["injecao"]["detalhe"]
    assert "D3" in itens["injecao"]["detalhe"]


# ── 3. Resumo e a regra da ação ───────────────────────────────────────────────

def test_alerta_nao_impede_o_veredito(prevoo):
    """Uma tela que responde "não" sempre deixa de ser lida.

    Alerta é informação que o operador precisa ter, não impedimento — e o caso
    concreto é o modo apresentação, que foi ligado de propósito.
    """
    itens = (prevoo.itens_celula(_snapshot(), HOME)
             + prevoo.itens_modo(True, 2.0, {"tipo": "x", "slot_id": 1}, 0))
    r = prevoo.resumo(itens)
    assert r["contagem"][prevoo.ALERTA] >= 3
    assert r["pronto"] is True


def test_uma_falha_derruba_o_veredito(prevoo):
    itens = prevoo.itens_banco(None) + prevoo.itens_celula(_snapshot(), HOME)
    r = prevoo.resumo(itens)
    assert r["contagem"][prevoo.FALHA] == 1
    assert r["pronto"] is False
    assert r["total"] == len(itens)


def test_todo_item_nao_verde_diz_o_que_fazer(prevoo):
    """A regra que separa esta tela de um relatório, cobrada por varredura.

    Item novo entra na conta sozinho — sem lista manual para alguém esquecer de
    atualizar. `info` fica de fora: ele não pede ação por definição.
    """
    cliente = ClienteFake(caidos={"dashboard"}, lentos={"manut_web"},
                          erros={"cnc-adapter": 503})
    itens = (
        asyncio.run(prevoo.sondar_servicos(cliente, timeout=0.5))
        + [prevoo.item_gerador(True)]
        + prevoo.itens_banco(None)
        + prevoo.itens_banco(_diagnostico(medicamentos=0, slots_presentes=0,
                                          schema_faltando=["`ordens`.`x`"]))
        + [prevoo.item_templates(["quebrado"], 10, True),
           prevoo.item_templates([], 10, False),
           prevoo.item_fila(5, 5)]
        + prevoo.itens_celula(_snapshot(
            trava={"ativa": True, "os_id": "OS-1", "slot_id": 2, "motivo": "m"},
            cnc={"posicao_x": 240.0, "posicao_y": 150.0},
            os_ativa={"os_id": "OS-1"}, alarmes_ativos=2), HOME)
        + prevoo.itens_modo(True, 2.0, {"tipo": "t", "slot_id": 1}, 0)
    )

    sem_acao = [i for i in itens
                if i["estado"] in (prevoo.FALHA, prevoo.ALERTA) and not i["acao"]]
    assert sem_acao == [], sem_acao

    # E o contrário: item verde não carrega ação — ruído que treina o olho a
    # pular a caixa amarela.
    com_acao_a_toa = [i for i in itens if i["estado"] == prevoo.OK and i["acao"]]
    assert com_acao_a_toa == [], com_acao_a_toa


def test_todo_item_tem_a_forma_esperada_pela_pagina(prevoo):
    """A página agrupa por `grupo` e pinta por `estado`. Campo faltando vira
    item sem título numa caixa sem cor — sem erro no console do navegador."""
    itens = (
        [prevoo.item_central(), prevoo.item_gerador(False)]
        + prevoo.itens_banco(_diagnostico())
        + [prevoo.item_templates([], 10, True), prevoo.item_fila(0, 5)]
        + prevoo.itens_celula(_snapshot(), HOME)
        + prevoo.itens_modo(False, 1.0, None, 0)
    )
    grupos_da_pagina = {"Serviços", "Banco", "Ordens", "Célula", "Modo"}
    ids = set()
    for item in itens:
        assert set(item) == {"id", "grupo", "titulo", "estado", "detalhe", "acao"}
        assert item["grupo"] in grupos_da_pagina, item
        assert item["estado"] in (prevoo.OK, prevoo.ALERTA, prevoo.FALHA, prevoo.INFO)
        assert item["titulo"]
        assert item["id"] not in ids, f"id repetido: {item['id']}"
        ids.add(item["id"])


def test_ordem_dos_grupos_da_pagina_cobre_os_grupos_emitidos():
    """A página ordena os grupos à mão (é a ordem em que se investiga um
    problema). Um grupo novo no Python cairia no fim da tela em vez do lugar
    certo — e ninguém repararia."""
    pagina = (CENTRAL_DIR / "console_prevoo.html").read_text(encoding="utf-8")
    achado = re.search(r"var ORDEM_GRUPOS = \[(.*?)\];", pagina, re.S)
    assert achado
    da_pagina = set(re.findall(r'"([^"]+)"', achado.group(1)))
    assert da_pagina == {"Serviços", "Banco", "Ordens", "Célula", "Modo"}


def test_pagina_e_servida_pelo_console():
    """`console.pagina_prevoo` lê o arquivo do disco, como as outras duas."""
    fonte = (CENTRAL_DIR / "console.py").read_text(encoding="utf-8")
    assert "console_prevoo.html" in fonte
    assert "def pagina_prevoo" in fonte
    assert (CENTRAL_DIR / "console_prevoo.html").is_file()
