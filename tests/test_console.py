"""Testes do console de operação — a interface própria do central, em /console.

O console é a mesa de quem opera a planta numa apresentação: escolhe qual das
dez ordens padrão entra, pausa o gerador automático, libera a trava do Triple
Check. Ele tem senha própria (`CONSOLE_SENHA`), sessão por cookie assinado e
nenhuma rota no Swagger.

Quatro invariantes são o objeto deste arquivo, e os quatro falham em silêncio
se quebrarem:

  1. **Sem `CONSOLE_SENHA`, o console não existe.** A regressão que assusta é
     alguém "consertar" a ausência com um default embutido — o que abriria o
     disparo de OS para quem lesse o repositório. Aqui a ausência é 503 em
     TODAS as rotas, e senha nenhuma confere.
  2. **Sem sessão, nada.** A página não pode sair pelo caminho do redirect
     levando as ordens junto, e as rotas de ação não podem aceitar cookie
     forjado, vencido, ou emitido com outra senha.
  3. **O disparo manual é o MESMO caminho do order-generator** — chama
     `receber_ordem`, a função do `POST /api/v1/ordens`. Duas portas de entrada
     de OS divergem no primeiro ajuste de contrato, e a que fica para trás é a
     que um humano usa sob pressão.
  4. **A pausa é o flag que o gerador consulta.** O central não para o
     container do gerador; publica um booleano em `GET /api/v1/gerador` e o
     gerador o lê antes de cada envio. Os dois lados são exercitados aqui.
"""
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

SENHA = "senha-do-console-para-teste"


# ── Fixtures ──────────────────────────────────────────────────────────────────

class OrdensFake:
    """Duplo de `salvar_ordem` com a semântica do INSERT IGNORE (ver test_api_ordens)."""

    def __init__(self):
        self.gravadas: list[str] = []

    def __call__(self, os_id, descricao, medicamentos, payload_raw) -> bool:
        if os_id in self.gravadas:
            return False
        self.gravadas.append(os_id)
        return True


class FilaFake:
    def __init__(self):
        self.enfileiradas: list[dict] = []

    async def __call__(self, os_payload: dict) -> bool:
        self.enfileiradas.append(os_payload)
        return True


def _catalogo(modulo) -> list[dict]:
    """Catálogo que cobre TODO medicamento citado pelas dez ordens padrão.

    Montado a partir de `os_templates.nomes_usados()` e não de uma lista fixa:
    uma lista escrita à mão aqui envelheceria na primeira edição de template, e
    o teste passaria a falhar por um motivo que não é o que ele afirma.
    """
    return [
        {"nome": nome, "sku": f"SKU-{i:03d}", "categoria": "teste"}
        for i, nome in enumerate(sorted(modulo.os_templates.nomes_usados()))
    ]


@pytest.fixture
def console(carregar_central, monkeypatch):
    """Central servido por TestClient, com o console HABILITADO.

    `TestClient` sem `with`: a lifespan não roda, então nada aqui toca MySQL nem
    sobe o loop do orquestrador.
    """
    central = carregar_central()
    modulo = central.modulo

    # `console.py` e `main.py` compartilham a MESMA instância de `Settings`
    # (`from config import settings`), e o conftest reimporta os módulos do
    # central a cada teste — então mexer aqui não vaza para o teste seguinte.
    monkeypatch.setattr(modulo.console.settings, "CONSOLE_SENHA", SENHA)

    ordens = OrdensFake()
    fila = FilaFake()
    monkeypatch.setattr(modulo, "salvar_ordem", ordens)
    monkeypatch.setattr(modulo.orch, "enfileirar_os", fila)
    monkeypatch.setattr(modulo, "listar_medicamentos", lambda: _catalogo(modulo))

    return SimpleNamespace(
        cliente=TestClient(modulo.app, follow_redirects=False),
        modulo=modulo,
        ordens=ordens,
        fila=fila,
        central=central,
    )


@pytest.fixture
def logado(console):
    """O mesmo central, com a sessão já aberta pela tela de senha."""
    resposta = console.cliente.post("/console/login", data={"senha": SENHA})
    assert resposta.status_code == 303, "login da fixture falhou"
    return console


def _um_template(modulo) -> str:
    return modulo.os_templates.TEMPLATES[0]["template_id"]


# ══════════════════════════════════════════════════════════════════════════════
# 1. Console desabilitado: sem CONSOLE_SENHA, nenhuma rota fica ativa
# ══════════════════════════════════════════════════════════════════════════════
#
# A escolha entre 404 e 503 está documentada em `console.py` e no README: as
# duas escondem o console de quem não tem a senha e nenhuma das duas o abre, e
# a diferença só aparece para o operador que configurou errado — 404 o manda
# caçar o erro na URL, no build ou no proxy; 503 dizendo "defina CONSOLE_SENHA"
# encerra o assunto numa linha.

@pytest.fixture
def desabilitado(carregar_central, monkeypatch):
    central = carregar_central()
    monkeypatch.setattr(central.modulo.console.settings, "CONSOLE_SENHA", "")
    return SimpleNamespace(
        cliente=TestClient(central.modulo.app, follow_redirects=False),
        modulo=central.modulo,
    )


ROTAS_DO_CONSOLE = [
    ("GET",  "/console"),
    ("GET",  "/console/login"),
    ("POST", "/console/login"),
    ("POST", "/console/logout"),
    ("POST", "/console/api/disparar"),
    ("POST", "/console/api/gerador"),
    ("POST", "/console/api/liberar-trava"),
]


@pytest.mark.parametrize("metodo,rota", ROTAS_DO_CONSOLE)
def test_sem_senha_configurada_toda_rota_do_console_responde_503(
        desabilitado, metodo, rota):
    resposta = desabilitado.cliente.request(
        metodo, rota,
        json={"template_id": "OS-URO-01", "pausado": True},
    )

    assert resposta.status_code == 503, f"{metodo} {rota} não recusou"
    corpo = resposta.json()
    assert corpo.get("erro", corpo.get("detail")) is not None
    assert "CONSOLE_SENHA" in str(corpo)


def test_sem_senha_configurada_nenhuma_senha_confere(desabilitado):
    """A ausência não pode virar "qualquer senha serve" nem senha vazia."""
    mod = desabilitado.modulo.console

    assert mod.habilitado() is False
    assert mod.senha_confere("") is False
    assert mod.senha_confere(SENHA) is False
    assert mod.sessao_valida(mod.criar_sessao()) is False


def test_sem_senha_configurada_o_disparo_nao_entra_no_sistema(desabilitado, monkeypatch):
    """503 antes de qualquer efeito: nada é persistido e nada é enfileirado."""
    ordens = OrdensFake()
    fila = FilaFake()
    monkeypatch.setattr(desabilitado.modulo, "salvar_ordem", ordens)
    monkeypatch.setattr(desabilitado.modulo.orch, "enfileirar_os", fila)

    desabilitado.cliente.post("/console/api/disparar",
                              json={"template_id": "OS-URO-01"})

    assert ordens.gravadas == []
    assert fila.enfileiradas == []


def test_console_desabilitado_nao_afeta_o_resto_da_api(desabilitado):
    """O console é opcional: sem ele a planta segue igual."""
    assert desabilitado.cliente.get("/ping").status_code == 200
    assert desabilitado.cliente.get("/api/v1/fila").status_code == 200
    assert desabilitado.cliente.get("/api/v1/gerador").status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# 2. Sessão: redirect sem cookie, senha errada não entra, senha certa entra
# ══════════════════════════════════════════════════════════════════════════════

def test_console_sem_sessao_redireciona_para_a_tela_de_senha(console):
    resposta = console.cliente.get("/console")

    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/console/login"


def test_console_sem_sessao_nao_expoe_dado_nenhum(console):
    """O redirect não pode levar a página junto no corpo."""
    corpo = console.cliente.get("/console").text

    for vazamento in ("DISPARAR", "template_id", "/console/api/disparar",
                      _um_template(console.modulo)):
        assert vazamento not in corpo


def test_tela_de_senha_nao_carrega_a_senha(console):
    """Nada de segredo no HTML — nem no formulário, nem em comentário."""
    corpo = console.cliente.get("/console/login").text

    assert corpo.count("<form") == 1
    assert SENHA not in corpo


def test_senha_errada_nao_cria_sessao(console):
    resposta = console.cliente.post("/console/login", data={"senha": "chute"})

    assert resposta.status_code == 401
    assert console.modulo.console.COOKIE_SESSAO not in resposta.cookies
    # A recusa é visível: a tela volta com o motivo, não em branco.
    assert console.modulo._ERRO_SENHA in resposta.text
    # E a porta continua fechada na requisição seguinte.
    assert console.cliente.get("/console").status_code == 303


def test_a_tela_de_erro_nao_ecoa_o_que_foi_digitado(console):
    """O bloco de erro é injetado escapado, e o texto vem de constante do central.

    Vale a guarda mesmo com a mensagem sendo fixa hoje: a tentação futura é
    ecoar o usuário ("senha 'xyz' incorreta"), e aí o HTML passa a receber
    entrada de fora.
    """
    resposta = console.cliente.post("/console/login",
                                    data={"senha": "<script>alert(1)</script>"})

    assert "<script>" not in resposta.text


def test_senha_vazia_nao_cria_sessao(console):
    resposta = console.cliente.post("/console/login", data={"senha": ""})

    assert resposta.status_code == 401
    assert console.cliente.get("/console").status_code == 303


def test_senha_correta_cria_sessao_e_abre_o_console(console):
    resposta = console.cliente.post("/console/login", data={"senha": SENHA})

    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/console"

    pagina = console.cliente.get("/console")
    assert pagina.status_code == 200
    assert "text/html" in pagina.headers["content-type"]
    assert "Console de operação" in pagina.text
    # A senha nunca vai ao cliente.
    assert SENHA not in pagina.text


def test_cookie_de_sessao_e_httponly_e_restrito_ao_console(console):
    resposta = console.cliente.post("/console/login", data={"senha": SENHA})

    cabecalho = resposta.headers["set-cookie"]
    assert "HttpOnly" in cabecalho
    assert "Path=/console" in cabecalho
    assert "SameSite=lax" in cabecalho.replace("SameSite=Lax", "SameSite=lax")
    # O que trafega é a assinatura, jamais a senha.
    assert SENHA not in cabecalho


def test_logout_encerra_a_sessao(logado):
    assert logado.cliente.get("/console").status_code == 200

    logado.cliente.post("/console/logout")

    assert logado.cliente.get("/console").status_code == 303


def test_cookie_forjado_nao_abre_o_console(console):
    """Sem a assinatura, um `exp` no futuro não vale nada."""
    futuro = int(time.time()) + 3600
    console.cliente.cookies.set(console.modulo.console.COOKIE_SESSAO,
                                f"{futuro}.assinaturainventada", path="/console")

    assert console.cliente.get("/console").status_code == 303


def test_sessao_vencida_nao_vale(console):
    mod = console.modulo.console
    vencido = mod.criar_sessao(agora=time.time() - 24 * 3600)

    assert mod.sessao_valida(vencido) is False
    assert mod.sessao_valida(mod.criar_sessao()) is True


def test_trocar_a_senha_invalida_as_sessoes_abertas(console, monkeypatch):
    """A chave do HMAC é derivada da senha: trocá-la revoga quem já entrou.

    Sem isso, tirar o acesso de alguém exigiria trocar também a `SECRET_KEY` —
    que assina os JWT do sistema inteiro e derrubaria os técnicos junto.
    """
    mod = console.modulo.console
    cookie = mod.criar_sessao()
    assert mod.sessao_valida(cookie) is True

    monkeypatch.setattr(mod.settings, "CONSOLE_SENHA", SENHA + "-nova")

    assert mod.sessao_valida(cookie) is False


ACOES = [
    ("/console/api/disparar",      {"template_id": "OS-URO-01"}),
    ("/console/api/gerador",       {"pausado": True}),
    ("/console/api/liberar-trava", {}),
]


@pytest.mark.parametrize("rota,corpo", ACOES)
def test_acao_sem_sessao_responde_401(console, rota, corpo):
    resposta = console.cliente.post(rota, json=corpo)

    assert resposta.status_code == 401
    assert console.fila.enfileiradas == []
    assert console.modulo.console.gerador_status()["pausado"] is False


def test_freio_de_forca_bruta_bloqueia_a_rajada(console):
    """A porta é uma senha só, sem usuário e sem segundo fator."""
    mod = console.modulo.console

    for _ in range(mod.MAX_TENTATIVAS):
        assert console.cliente.post("/console/login",
                                    data={"senha": "chute"}).status_code == 401

    bloqueada = console.cliente.post("/console/login", data={"senha": "chute"})
    assert bloqueada.status_code == 429
    assert "Retry-After" in bloqueada.headers

    # E o bloqueio vale inclusive para quem acertar a senha na sequência —
    # senão bastaria intercalar palpites com a senha certa para nunca ser freado.
    assert console.cliente.post("/console/login",
                                data={"senha": SENHA}).status_code == 429


def test_login_correto_zera_o_contador_de_falhas(console):
    console.cliente.post("/console/login", data={"senha": "chute"})
    console.cliente.post("/console/login", data={"senha": SENHA})
    console.cliente.post("/console/logout")

    mod = console.modulo.console
    assert mod.bloqueado("testclient") == 0


# ══════════════════════════════════════════════════════════════════════════════
# 3. Disparo manual: o MESMO caminho do POST /api/v1/ordens
# ══════════════════════════════════════════════════════════════════════════════

def test_disparo_manual_passa_por_receber_ordem(logado, monkeypatch):
    """A prova de que não existe caminho paralelo de entrada de OS.

    O console poderia gravar e enfileirar por conta própria e ninguém notaria —
    até o dia em que uma recusa de contrato (409/429/503) mudasse de forma só de
    um lado. Aqui a função do endpoint é espionada: se o console deixar de
    chamá-la, este teste cai.
    """
    original = logado.modulo.receber_ordem
    chamadas = []

    async def espiao(req):
        chamadas.append(req)
        return await original(req)

    monkeypatch.setattr(logado.modulo, "receber_ordem", espiao)

    template_id = _um_template(logado.modulo)
    resposta = logado.cliente.post("/console/api/disparar",
                                   json={"template_id": template_id})

    assert resposta.status_code == 200
    assert len(chamadas) == 1
    # E o corpo que chegou lá é o mesmo tipo que o gerador posta.
    assert isinstance(chamadas[0], logado.modulo.NovaOSReq)
    assert chamadas[0].os_id.startswith(template_id)


def test_disparo_manual_persiste_e_enfileira(logado):
    template_id = _um_template(logado.modulo)

    resposta = logado.cliente.post("/console/api/disparar",
                                   json={"template_id": template_id})

    corpo = resposta.json()
    assert corpo["aceita"] is True
    assert corpo["template_id"] == template_id
    assert logado.ordens.gravadas == [corpo["os_id"]]
    assert [os["os_id"] for os in logado.fila.enfileiradas] == [corpo["os_id"]]


def test_disparos_do_mesmo_template_geram_os_id_unico(logado):
    """O template é fixo; a chave primária não pode ser.

    `ordens.os_id` é UNIQUE e o central responde 409 a um reenvio — sem sufixo,
    a segunda vez que a mesma ordem padrão fosse disparada seria recusada, e a
    apresentação acabaria no primeiro clique repetido.
    """
    template_id = _um_template(logado.modulo)

    ids = [
        logado.cliente.post("/console/api/disparar",
                            json={"template_id": template_id}).json()["os_id"]
        for _ in range(5)
    ]

    assert len(set(ids)) == 5
    assert all(os_id.startswith(template_id + "-") for os_id in ids)
    assert logado.ordens.gravadas == ids


def test_itens_disparados_saem_com_sku_do_catalogo(logado):
    """`sku` é resolvido na hora, como no gerador — nunca congelado no template.

    Um SKU velho viraria `leitura_dispenser_divergencia` num slot só: o quadro
    exato de um medicamento trocado.
    """
    template_id = _um_template(logado.modulo)
    logado.cliente.post("/console/api/disparar", json={"template_id": template_id})

    enviados = logado.fila.enfileiradas[0]["medicamentos"]
    assert enviados, "OS enfileirada sem itens"
    assert all(item["sku"].startswith("SKU-") for item in enviados)


def test_template_desconhecido_responde_404_sem_tocar_no_sistema(logado):
    resposta = logado.cliente.post("/console/api/disparar",
                                   json={"template_id": "OS-QUE-NAO-EXISTE"})

    assert resposta.status_code == 404
    assert resposta.json()["erro"] == "template_desconhecido"
    assert logado.ordens.gravadas == []
    assert logado.fila.enfileiradas == []


def test_catalogo_indisponivel_recusa_o_disparo(logado, monkeypatch):
    """Sem catálogo a OS sairia com `sku` vazio — a câmera sem o que comparar."""
    monkeypatch.setattr(logado.modulo, "listar_medicamentos", lambda: [])

    resposta = logado.cliente.post(
        "/console/api/disparar", json={"template_id": _um_template(logado.modulo)})

    assert resposta.status_code == 503
    assert resposta.json()["erro"] == "catalogo_indisponivel"
    assert logado.fila.enfileiradas == []


def test_fila_cheia_chega_ao_console_como_429(logado):
    """A recusa por backpressure precisa aparecer com clareza na tela.

    Mesmo corpo de contrato que o order-generator recebe (`fila_cheia`, com a
    ocupação junto) — porque é a mesma função respondendo.
    """
    fila_real = logado.modulo.orch._os_queue
    while not fila_real.full():
        fila_real.put_nowait({"os_id": f"OS-ENFILEIRADA-{fila_real.qsize()}"})

    resposta = logado.cliente.post(
        "/console/api/disparar", json={"template_id": _um_template(logado.modulo)})

    assert resposta.status_code == 429
    corpo = resposta.json()
    assert corpo["erro"] == "fila_cheia"
    assert corpo["fila"]["disponivel"] == 0
    assert corpo["mensagem"]
    assert logado.ordens.gravadas == []   # 429 vem ANTES do banco


# ══════════════════════════════════════════════════════════════════════════════
# 4. Pausa do gerador: o console escreve, o order-generator lê
# ══════════════════════════════════════════════════════════════════════════════

def test_gerador_nasce_rodando(console):
    assert console.cliente.get("/api/v1/gerador").json()["pausado"] is False


def test_pausar_altera_o_flag_que_o_gerador_consulta(logado):
    resposta = logado.cliente.post("/console/api/gerador", json={"pausado": True})

    assert resposta.status_code == 200
    assert resposta.json()["pausado"] is True
    # O que o order-generator vê, na rota que ele consulta.
    consulta = logado.cliente.get("/api/v1/gerador").json()
    assert consulta["pausado"] is True
    assert consulta["desde"] is not None


def test_retomar_desfaz_a_pausa(logado):
    logado.cliente.post("/console/api/gerador", json={"pausado": True})
    logado.cliente.post("/console/api/gerador", json={"pausado": False})

    consulta = logado.cliente.get("/api/v1/gerador").json()
    assert consulta["pausado"] is False
    assert consulta["desde"] is None


def test_pausa_e_publicada_no_snapshot_do_websocket(logado):
    """O console vê a pausa ao vivo pelo `/ws`, sem polling próprio."""
    logado.cliente.post("/console/api/gerador", json={"pausado": True})

    assert logado.modulo._estado["gerador_pausado"] is True
    assert logado.cliente.get("/estado").json()["gerador_pausado"] is True


def test_pausa_nao_bloqueia_o_disparo_manual(logado):
    """Pausar é assumir o controle, não parar a planta.

    Se a pausa também barrasse o console, o botão que existe para operar à mão
    ficaria inútil justamente no modo em que ele é usado.
    """
    logado.cliente.post("/console/api/gerador", json={"pausado": True})

    resposta = logado.cliente.post(
        "/console/api/disparar", json={"template_id": _um_template(logado.modulo)})

    assert resposta.status_code == 200
    assert len(logado.fila.enfileiradas) == 1


# ── O lado do order-generator ─────────────────────────────────────────────────

def test_order_generator_consulta_o_flag_antes_de_enviar(carregar_simulador):
    sim = carregar_simulador("order-generator/simulator.py")
    sim.requests.payload = {"pausado": True}

    assert sim.modulo._gerador_pausado() is True
    assert sim.chamadas[-1]["url"].endswith("/api/v1/gerador")
    assert sim.chamadas[-1]["metodo"] == "GET"


def test_order_generator_segue_gerando_se_o_flag_nao_responde(carregar_simulador):
    """Consulta auxiliar que falha não pode parar a planta.

    O pior caso deste default é uma OS entrar durante uma pausa que o operador
    ainda pode desfazer. O contrário — parar de gerar porque o central
    reiniciou — deixaria a planta em silêncio sem ninguém ter pedido.
    """
    sim = carregar_simulador("order-generator/simulator.py")
    sim.requests.status_code = 500

    assert sim.modulo._gerador_pausado() is False


def test_order_generator_espera_enquanto_a_pausa_durar(carregar_simulador, monkeypatch):
    sim = carregar_simulador("order-generator/simulator.py")
    monkeypatch.setattr(sim.modulo, "ESPERA_PAUSA", 0)

    respostas = iter([True, True, False])
    monkeypatch.setattr(sim.modulo, "_gerador_pausado", lambda: next(respostas))

    sim.modulo._aguardar_retomada()          # retorna quando a pausa sai

    assert next(respostas, "esgotado") == "esgotado"


def test_order_generator_nao_espera_quando_nao_ha_pausa(carregar_simulador, monkeypatch):
    sim = carregar_simulador("order-generator/simulator.py")
    dormiu = []
    monkeypatch.setattr(sim.modulo.time, "sleep", lambda s: dormiu.append(s))
    sim.requests.payload = {"pausado": False}

    sim.modulo._aguardar_retomada()

    assert dormiu == []


# ══════════════════════════════════════════════════════════════════════════════
# 5. Trava do Triple Check pelo console
# ══════════════════════════════════════════════════════════════════════════════

def test_liberar_trava_pelo_console_usa_a_mesma_funcao_do_endpoint_admin(
        logado, monkeypatch):
    """Dois portões, uma consequência.

    O que muda entre o app de manutenção e o console é a autenticação; o efeito
    — soltar o evento do orquestrador, limpar `_estado["trava"]` e publicar —
    tem que ser idêntico. Duas cópias divergiriam no passo fácil de esquecer: o
    snapshot que o dashboard lê, que ficaria vermelho com a OS já rodando.
    """
    modulo = logado.modulo
    with modulo._lock:
        modulo._estado["trava"] = {"ativa": True, "os_id": "OS-1", "slot_id": 3,
                                   "motivo": "divergência de peso"}
    monkeypatch.setattr(modulo.orch, "liberar_trava", lambda por: True)

    resposta = logado.cliente.post("/console/api/liberar-trava", json={})

    assert resposta.status_code == 200
    assert resposta.json() == {"ok": True, "liberado_por": "console"}
    assert modulo._estado["trava"]["ativa"] is False
    assert modulo._estado["trava"]["motivo"] == ""


def test_liberar_sem_trava_ativa_responde_409(logado, monkeypatch):
    monkeypatch.setattr(logado.modulo.orch, "liberar_trava", lambda por: False)

    resposta = logado.cliente.post("/console/api/liberar-trava", json={})

    assert resposta.status_code == 409
    assert resposta.json()["erro"] == "sem_trava"


# ══════════════════════════════════════════════════════════════════════════════
# 6. Discrição: o console não aparece no Swagger
# ══════════════════════════════════════════════════════════════════════════════

def test_nenhuma_rota_do_console_aparece_no_openapi(console):
    caminhos = console.cliente.get("/openapi.json").json()["paths"]

    intrusas = [rota for rota in caminhos if rota.startswith("/console")]
    assert intrusas == [], f"rota do console vazou para o Swagger: {intrusas}"


def test_o_flag_do_gerador_CONTINUA_no_openapi(console):
    """`/api/v1/gerador` não é rota de console: é contrato com o order-generator.

    Esconder um endpoint que outro serviço consome trocaria discrição por
    documentação faltando — quem mantiver o gerador precisa achá-lo no /docs.
    """
    caminhos = console.cliente.get("/openapi.json").json()["paths"]

    assert "/api/v1/gerador" in caminhos


def test_a_tela_de_senha_pede_para_nao_ser_indexada(console):
    assert 'name="robots"' in console.cliente.get("/console/login").text


def test_a_pagina_do_console_pede_para_nao_ser_indexada(logado):
    assert 'name="robots"' in logado.cliente.get("/console").text
