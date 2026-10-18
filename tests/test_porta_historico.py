# -*- coding: utf-8 -*-
"""Porta NR-12 no central: histórico, alarmes, a faixa e o pré-voo.

  * toda mensagem da porta que MUDA estado vira UMA linha em `porta_eventos`,
    com UID, porta, a OS e o slot do momento — o heartbeat não vira linha;
  * alarme é um por evento, nunca em dobro: abertura sem autorização (fonte
    `porta_teto`/`porta_frente`), link perdido (na TRANSIÇÃO), máquina que não
    confirmou a parada, e tag negada repetida (3 em 1 min). Pedido de acesso e
    rearme NÃO são alarme;
  * o pré-voo tem UM item "Portas NR-12", com `acao` em todo vermelho/amarelo,
    e com o adapter fora continua sendo um item — não três.

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
    portas desconhecidas: é UM item, apontando para o adapter."""
    prevoo = central.modulo.prevoo
    item = prevoo.item_portas({"link_ok": False}, {"motivo": "link_perdido"},
                              True, False, None, 15)
    assert item["estado"] == prevoo.FALHA and "dispenser-adapter" in item["acao"]


def test_o_relatorio_do_prevoo_tem_um_item_de_porta(central, monkeypatch):
    m = central.modulo
    monkeypatch.setattr(m, "_console_exigir_sessao", lambda request: None)

    async def _servicos(cliente):
        return [m.prevoo._item("dispenser-adapter", "Serviços", "dispenser-adapter",
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
