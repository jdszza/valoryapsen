# -*- coding: utf-8 -*-
"""Porta NR-12 no central: histórico, alarmes, a faixa e o pré-voo.

  * toda mensagem da porta que MUDA estado vira UMA linha em `porta_eventos`,
    com UID, porta, a OS e o slot do momento — o heartbeat não vira linha;
  * alarme é um por evento, nunca em dobro: abertura sem autorização (fonte
    `porta_teto`/`porta_frente`), link perdido (na TRANSIÇÃO), máquina que não
    confirmou a parada, e tag negada repetida (3 em 1 min). Pedido de acesso e
    rearme NÃO são alarme;
  * o pré-voo tem UM item "Portas NR-12", com `acao` em todo vermelho/amarelo,
    e com o porta-adapter fora continua sendo um item — não três. O
    porta-adapter só é sondado com a porta habilitada: sem porta na célula, o
    serviço não existe e não pode virar vermelho.

O que se conta são chamadas a `salvar_porta_evento` e `salvar_alarme`, pelo
handler HTTP de verdade (`_handle_evento_porta`).
"""
import asyncio

import pytest


@pytest.fixture
def central(carregar_central, monkeypatch):
    c = carregar_central()
    orch = c.modulo.orch
    # O que a `lifespan` faz: o orquestrador publica no MESMO `_estado` do main.
    orch.inicializar(c.modulo._estado, c.modulo._lock, lambda: None, None)
    # O orquestrador grava alarme direto (fora do main): o duplo vai nele também.
    c.banco.instalar(orch, monkeypatch)
    comandos: list = []

    async def _post(url, payload, timeout=10.0):
        comandos.append((url, payload))
        if url.endswith("/comandos/homing"):
            orch.notificar_evento(f"{payload['os_id']}:homing", {"tipo": "concluido"})
        return True
    monkeypatch.setattr(orch, "_post", _post)
    c.comandos = comandos
    return c


def _ev(tipo, **campos):
    return {"origem": "porta", "tipo": tipo, **campos, "ts": "2026-10-17T10:00:00"}


def _estado(teto="fechada", frente="fechada", estado="FECHADA", rearme=False):
    return _ev("estado", teto=teto, frente=frente, estado=estado,
               aguardando_rearme=rearme)


def _rodar(central, *eventos):
    async def _cenario():
        for evento in eventos:
            await central.modulo._handle_evento_porta(evento)
        await central.modulo.orch.aguardar_tarefas_porta()
    asyncio.run(_cenario())


def _linhas(central):
    return [c["args"] for c in central.banco.chamadas_de("salvar_porta_evento")]


def _alarmes(central):
    return [c["args"][:2] for c in central.banco.chamadas_de("salvar_alarme")]


# ── 1. Histórico ──────────────────────────────────────────────────────────────

def test_cada_mensagem_que_muda_estado_vira_uma_linha(central):
    _rodar(central,
           _estado(),                                         # heartbeat: sem linha
           _ev("pedido_acesso", uid="04A1B2C3"),
           _ev("liberada", janela_ms=10000),
           _ev("porta_aberta", porta="frente", autorizada=True),
           _estado(frente="aberta", estado="ABERTA_AUTORIZADA"),
           _ev("porta_fechada"),
           _estado(rearme=True),
           _ev("rearme", uid="04A1B2C3"),
           _estado(),
           _ev("tag_negada", uid="DEADBEEF"),
           _ev("porta_link_perdido"),
           _ev("porta_link_ok"),
           _ev("liberacao_expirada", motivo="janela_expirou"))
    tipos = [linha[0] for linha in _linhas(central)]
    assert tipos == ["pedido_acesso", "liberada", "porta_aberta", "porta_fechada",
                     "rearme", "tag_negada", "porta_link_perdido", "porta_link_ok",
                     "liberacao_expirada"]
    # A linha diz QUAL porta e QUEM (o UID — a tag é genérica, não a pessoa).
    aberta = next(l for l in _linhas(central) if l[0] == "porta_aberta")
    assert aberta[1] == "frente" and aberta[2] is True
    pedido = next(l for l in _linhas(central) if l[0] == "pedido_acesso")
    assert pedido[3] == "04A1B2C3"


def test_a_linha_leva_a_os_em_andamento(central):
    with central.modulo._lock:
        central.modulo._estado["os_ativa"] = {"os_id": "OS-H1"}
    _rodar(central, _estado(), _ev("porta_aberta", porta="teto", autorizada=False))
    linha = next(l for l in _linhas(central) if l[0] == "porta_aberta")
    assert linha[5] == "OS-H1"


# ── 2. Alarmes: um por evento ─────────────────────────────────────────────────

def test_abertura_sem_autorizacao_e_um_alarme_com_a_fonte_da_porta(central):
    _rodar(central, _estado(), _ev("porta_aberta", porta="frente", autorizada=False))
    assert _alarmes(central) == [("porta_frente", "porta_aberta_sem_autorizacao")]


def test_a_segunda_porta_aberta_tambem_e_alarme_com_a_fonte_dela(central):
    _rodar(central, _estado(),
           _ev("porta_aberta", porta="frente", autorizada=False),
           _ev("porta_aberta", porta="teto", autorizada=False))
    assert _alarmes(central) == [("porta_frente", "porta_aberta_sem_autorizacao"),
                                 ("porta_teto", "porta_aberta_sem_autorizacao")]


def test_link_perdido_repetido_e_um_alarme_so(central):
    _rodar(central, _estado(), _ev("porta_link_perdido"), _ev("porta_link_perdido"))
    assert _alarmes(central) == [("porta", "porta_link_perdido")]
    assert [l[0] for l in _linhas(central)].count("porta_link_perdido") == 1


def test_pedido_de_acesso_e_rearme_nao_sao_alarme(central):
    _rodar(central, _estado(),
           _ev("pedido_acesso", uid="04A1B2C3"),
           _ev("porta_aberta", porta="frente", autorizada=True),
           _ev("porta_fechada"), _estado(rearme=True),
           _ev("rearme", uid="04A1B2C3"), _estado())
    assert _alarmes(central) == []


def test_tres_tags_negadas_em_um_minuto_viram_um_alarme(central):
    _rodar(central, *[_ev("tag_negada", uid="DEADBEEF")] * 2)
    assert _alarmes(central) == []
    _rodar(central, _ev("tag_negada", uid="DEADBEEF"))
    assert _alarmes(central) == [("porta", "tentativa_acesso_negada")]
    # A contagem recomeça: a quarta não repete o alarme.
    _rodar(central, _ev("tag_negada", uid="DEADBEEF"))
    assert len(_alarmes(central)) == 1


def test_maquina_que_nao_confirma_a_parada_e_um_alarme(central, monkeypatch):
    orch = central.modulo.orch

    async def _post(url, payload, timeout=10.0):
        central.comandos.append((url, payload))
        return not url.endswith("/comandos/parar")
    monkeypatch.setattr(orch, "_post", _post)
    with central.modulo._lock:
        central.modulo._estado["os_ativa"] = {"os_id": "OS-H2"}
    _rodar(central, _estado(), _ev("pedido_acesso", uid="04A1B2C3"))
    assert _alarmes(central) == [("porta", "maquina_nao_confirmou_parada")]
    assert any(u.endswith("/porta/negar") for u, _ in central.comandos)


# ── 2b. A porta em campo autoriza sozinha (§10) ───────────────────────────────
#
# O nr12_oficial acende o "pode abrir" ao mesmo tempo em que manda o pedido, e
# não recebe nada do PC. O central PARA — e não manda liberar nem negar.

def _comandos_porta(central) -> list[str]:
    base = central.modulo.orch.settings.PORTA_ADAPTER_URL
    return [u for u, _ in central.comandos if u.startswith(base)]


def test_pedido_autorizado_pela_porta_para_e_nao_manda_nada_a_porta(central):
    with central.modulo._lock:
        central.modulo._estado["os_ativa"] = {"os_id": "OS-H3"}
    _rodar(central, _estado(),
           _ev("pedido_acesso", uid="04A1B2C3", autorizada_pela_porta=True))
    orch = central.modulo.orch
    assert orch.get_pausa_porta()["motivo"] == "pedido_acesso"
    urls = [u for u, _ in central.comandos]
    assert any(u.endswith("/comandos/parar") and u.startswith(orch.settings.CNC_ADAPTER_URL)
               for u in urls)
    assert any(u.endswith("/comandos/parar")
               and u.startswith(orch.settings.DISPENSER_ADAPTER_URL) for u in urls)
    assert _comandos_porta(central) == [], "POST ao porta-adapter numa porta autônoma"
    assert _alarmes(central) == []


def test_parada_nao_confirmada_com_a_porta_ja_autorizada_e_alarme_sem_negar(central,
                                                                            monkeypatch):
    orch = central.modulo.orch

    async def _post(url, payload, timeout=10.0):
        central.comandos.append((url, payload))
        return not url.endswith("/comandos/parar")
    monkeypatch.setattr(orch, "_post", _post)
    with central.modulo._lock:
        central.modulo._estado["os_ativa"] = {"os_id": "OS-H4"}
    _rodar(central, _estado(),
           _ev("pedido_acesso", uid="04A1B2C3", autorizada_pela_porta=True))
    assert _alarmes(central) == [("porta", "maquina_nao_confirmou_parada")]
    texto = central.banco.chamadas_de("salvar_alarme")[0]["args"][2]
    assert "JÁ tinha autorizado" in texto and "04A1B2C3" in texto
    assert _comandos_porta(central) == [], "porta_negar numa porta que não recebe nada"


def test_pedido_sem_a_flag_continua_mandando_liberar(central):
    """O comportamento do firmware JSON (§9) segue de pé."""
    _rodar(central, _estado(), _ev("pedido_acesso", uid="04A1B2C3"))
    assert [u.rsplit("/", 1)[1] for u in _comandos_porta(central)] == ["liberar"]


def test_porta_aberta_sem_porta_identificada_e_alarme_na_fonte_porta(central):
    _rodar(central, _estado(), _ev("porta_aberta", porta=None, autorizada=False))
    assert _alarmes(central) == [("porta", "porta_aberta_sem_autorizacao")]
    texto = central.banco.chamadas_de("salvar_alarme")[0]["args"][2]
    assert "não identificada" in texto and "None" not in texto


# ── 3. A faixa, lida do central ───────────────────────────────────────────────

def test_get_porta_publica_a_faixa_e_o_estado_ao_vivo(central):
    _rodar(central, _estado(), _ev("porta_aberta", porta="frente", autorizada=False))
    corpo = central.modulo.get_porta()
    assert corpo["faixa"] == "MÁQUINA PARADA — PORTA FRENTE ABERTA"
    assert corpo["pausa"]["motivo"] == "porta_aberta_sem_autorizacao"
    assert central.modulo._estado["porta"]["faixa"] == corpo["faixa"]
    assert central.modulo._estado["pausa_porta"]["motivo"] == "porta_aberta_sem_autorizacao"


# ── 4. Pré-voo ────────────────────────────────────────────────────────────────

@pytest.fixture
def prevoo(central):
    return central.modulo.prevoo


def test_porta_aberta_no_prevoo_e_vermelho_com_acao(prevoo):
    item = prevoo.item_portas({"link_ok": True, "teto": "fechada", "frente": "aberta"},
                              {"motivo": "porta_aberta_sem_autorizacao"}, True, True, 1.0, 15)
    assert item["estado"] == prevoo.FALHA
    assert item["acao"] == "Feche a porta frente e passe a tag para rearmar."


def test_rearme_pendente_e_amarelo_com_acao(prevoo):
    item = prevoo.item_portas({"link_ok": True, "teto": "fechada", "frente": "fechada"},
                              {"aguardando_rearme": True}, True, True, 1.0, 15)
    assert item["estado"] == prevoo.ALERTA and "tag" in item["acao"]


def test_tudo_certo_e_verde_sem_acao(prevoo):
    item = prevoo.item_portas({"link_ok": True, "teto": "fechada", "frente": "fechada"},
                              None, True, True, 1.0, 15)
    assert item["estado"] == prevoo.OK and item["acao"] == ""


def test_adapter_fora_e_um_item_so_e_diz_qual_e_a_causa(central):
    """Com o adapter fora, o pré-voo não lista link perdido + heartbeat velho +
    portas desconhecidas: é UM item, apontando para o porta-adapter — e para o
    .bat que o sobe com a COM da porta, porque ele não é do compose."""
    prevoo = central.modulo.prevoo
    item = prevoo.item_portas({"link_ok": False}, {"motivo": "link_perdido"},
                              True, False, None, 15)
    assert item["estado"] == prevoo.FALHA
    assert "porta-adapter" in item["detalhe"]
    assert "Inicie porta-adapter\\iniciar_host.bat com a COM da porta" in item["acao"]
    assert "dispenser-adapter" not in item["acao"] and "UART" not in item["acao"]


def test_o_relatorio_do_prevoo_tem_um_item_de_porta(central, monkeypatch):
    m = central.modulo
    monkeypatch.setattr(m, "_console_exigir_sessao", lambda request: None)

    async def _servicos(cliente, servicos=()):
        return [m.prevoo._item("porta-adapter", "Serviços", "porta-adapter",
                               m.prevoo.FALHA, "fora", "suba")]

    async def _nada(*a, **k):
        return None

    async def _templates():
        return [], {}
    monkeypatch.setattr(m.prevoo, "sondar_servicos", _servicos)
    monkeypatch.setattr(m, "_prevoo_banco", _nada)
    monkeypatch.setattr(m, "_prevoo_visao", _nada)
    monkeypatch.setattr(m, "_diagnosticar_templates", _templates)
    relatorio = asyncio.run(m.console_prevoo(None))
    portas = [i for i in relatorio["itens"] if i["id"] == "portas_nr12"]
    assert len(portas) == 1


class _ClienteSonda:
    """Responde a toda sonda, menos aos hosts em `caidos` (conexão recusada)."""

    def __init__(self, caidos=()):
        self.caidos = set(caidos)
        self.urls: list = []

    async def get(self, url, timeout=None):
        self.urls.append(url)
        host = url.split("//", 1)[1].split(":", 1)[0]
        if host in self.caidos:
            raise ConnectionError("Connection refused")

        class _R:
            status_code = 200

            @staticmethod
            def json():
                return {}
        return _R()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _relatorio(central, monkeypatch, cliente) -> dict:
    m = central.modulo
    monkeypatch.setattr(m, "_console_exigir_sessao", lambda request: None)

    async def _nada(*a, **k):
        return None

    async def _templates():
        return [], {}
    monkeypatch.setattr(m, "_prevoo_banco", _nada)
    monkeypatch.setattr(m, "_prevoo_visao", _nada)
    monkeypatch.setattr(m, "_diagnosticar_templates", _templates)
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda *a, **k: cliente)
    return asyncio.run(m.console_prevoo(None))


def test_porta_habilitada_e_porta_adapter_fora_e_um_item_vermelho_com_a_acao(
        central, monkeypatch):
    m = central.modulo
    monkeypatch.setattr(m.settings, "PORTA_NR12_HABILITADA", True)
    monkeypatch.setattr(m.settings, "PORTA_ADAPTER_URL", "http://host.docker.internal:8104")
    cliente = _ClienteSonda(caidos={"host.docker.internal"})
    itens = _relatorio(central, monkeypatch, cliente)["itens"]

    # A sonda bate onde o central manda os comandos — não no nome do compose.
    assert "http://host.docker.internal:8104/ping" in cliente.urls
    (portas,) = [i for i in itens if i["id"] == "portas_nr12"]
    assert portas["estado"] == m.prevoo.FALHA
    assert "porta-adapter\\iniciar_host.bat com a COM da porta" in portas["acao"]
    # A sonda do serviço diz o mesmo: ele não é do compose, e "docker compose
    # up -d porta-adapter" seria a ação errada.
    (servico,) = [i for i in itens if i["id"] == "porta-adapter"]
    assert servico["estado"] == m.prevoo.FALHA
    assert "docker compose" not in servico["acao"]
    assert "iniciar_host.bat" in servico["acao"]


def test_porta_desabilitada_o_porta_adapter_nem_aparece(central, monkeypatch):
    m = central.modulo
    monkeypatch.setattr(m.settings, "PORTA_NR12_HABILITADA", False)
    cliente = _ClienteSonda(caidos={"porta-adapter", "host.docker.internal"})
    itens = _relatorio(central, monkeypatch, cliente)["itens"]

    assert not [u for u in cliente.urls if ":8104" in u], cliente.urls
    assert not [i for i in itens if i["id"] == "porta-adapter"]
    (portas,) = [i for i in itens if i["id"] == "portas_nr12"]
    assert portas["estado"] == m.prevoo.INFO
