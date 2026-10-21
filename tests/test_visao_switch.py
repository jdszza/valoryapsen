# -*- coding: utf-8 -*-
"""O switch "Visão no Triple Check" do console.

CLAUDE.md, "Switch da visão no Triple Check". Desligado, a visão (câmera da
mesa, câmeras dos dispensers, fotos VAZIA/FINAL) continua medindo e sendo
registrada, mas NÃO TRAVA a OS: vira o alarme `visao_ignorada`. Balança,
dispenser e segurança continuam decidindo como sempre.

Os testes do ciclo medem CHAMADAS (quantas vezes `_ativar_trava` rodou, quais
alarmes saíram, que comandos foram enviados) e nunca tempo de parede.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from conftest import TEMPLATE_PADRAO
from test_central_eventos import SLOT_DIR, _evento_visao, _leitura_dispenser
from test_console import logado, console  # noqa: F401 — fixtures reaproveitadas


def _item(nome: str, quantidade: int = 5) -> dict:
    return {"medicamento": nome, "quantidade": quantidade, "sku": f"SKU-{nome}",
            "categoria": "teste"}


def _payload(os_id: str = "OS-V", *itens) -> dict:
    return {"os_id": os_id, "descricao": "teste", "template_id": TEMPLATE_PADRAO,
            "medicamentos": list(itens) or [_item("Dipirona", 4), _item("Paracetamol", 3)]}


def _alarmes(orq) -> list[str]:
    return [c["args"][1] for c in orq.banco.chamadas_de("salvar_alarme")]


def _status(orq) -> list:
    return [c["args"][1] for c in orq.banco.chamadas_de("atualizar_status_ordem")]


def _fotos(orq, etapa: str) -> list[dict]:
    return [p for p in orq.adapter.comandos("/comandos/capturar/mesa")
            if p.get("etapa") == etapa]


@pytest.fixture
def orq(carregar_orquestrador, monkeypatch):
    o = carregar_orquestrador()
    for campo in ("VISAO_CAIXA_VAZIA_REPETIR_S", "TIMEOUT_VISAO_MESA",
                  "TIMEOUT_CNC_CENTRO", "TIMEOUT_POSICIONAMENTO",
                  "TIMEOUT_VISAO_DISPENSER"):
        monkeypatch.setattr(o.modulo.settings, campo, 0.05)
    # Toda ativação de trava, contada — e liberada na hora, no papel do
    # supervisor, para a OS terminar dentro do `asyncio.run`.
    o.travas = []
    o.ao_travar = []          # roda com a trava ATIVA, antes da liberação
    real = o.modulo._ativar_trava

    async def _espia(os_id, slot_id, motivo, resumo=""):
        o.travas.append(motivo)
        evt = await real(os_id, slot_id, motivo, resumo=resumo)
        for gancho in o.ao_travar:
            gancho()
        o.modulo.liberar_trava("supervisor-teste")
        return evt
    monkeypatch.setattr(o.modulo, "_ativar_trava", _espia)
    o.historico = []
    monkeypatch.setattr(o.modulo, "_historico_fn",
                        lambda tipo, msg: o.historico.append((tipo, msg)))
    return o


def _desligar(orq) -> None:
    orq.modulo.chave_visao_tc.chave.desligar("teste")


def _ligar(orq) -> None:
    orq.modulo.chave_visao_tc.chave.ligar("teste")


def _rota(orq, *slots) -> list[int]:
    return orq.modulo.planejar_rota(list(slots), orq.modulo.HOME)


# ══════════════════════════════════════════════════════════════════════════════
# Task 1 — o estado do switch, as rotas e o histórico
# ══════════════════════════════════════════════════════════════════════════════

def test_chave_pura_liga_desliga_e_diz_se_mudou(carregar_orquestrador):
    m = carregar_orquestrador().modulo.chave_visao_tc
    t0 = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 21, 10, 5, tzinfo=timezone.utc)
    c = m.ChaveVisaoTC(ativo=True, desde=t0)

    assert c.ligar("x", t1) is False                 # já estava: nada muda
    assert (c.ativo, c.desde, c.por) == (True, t0, m.POR_BOOT)
    assert c.desligar("console", t1) is True
    assert (c.ativo, c.desde, c.por) == (False, t1, "console")
    assert c.desligar("outro") is False
    assert c.estado() == {"ativo": False, "desde": t1.isoformat(), "por": "console"}


@pytest.mark.parametrize("valor,esperado", [("0", False), ("1", True), ("nao", False),
                                            ("talvez", True), ("", True)])
def test_boot_nasce_no_padrao_do_env(carregar_orquestrador, valor, esperado):
    orq = carregar_orquestrador({"VISAO_TRIPLE_CHECK_PADRAO": valor})
    assert orq.modulo.visao_conta() is esperado
    assert orq.estado["visao_triple_check"]["ativo"] is esperado


def test_rotas_exigem_sessao(console):  # noqa: F811
    assert console.cliente.get("/console/api/visao-triple-check").status_code == 401
    assert console.cliente.post("/console/api/visao-triple-check",
                                json={"ativo": False}).status_code == 401
    assert console.modulo.orch.visao_conta() is True


def _linhas_switch(modulo) -> list[str]:
    return [e["msg"] for e in modulo._log_eventos if "Triple Check (pelo console)" in e["msg"]]


def test_get_e_post_com_sessao_e_post_repetido_nao_duplica(logado):  # noqa: F811
    modulo = logado.modulo
    inicial = logado.cliente.get("/console/api/visao-triple-check").json()
    assert inicial["ativo"] is True and inicial["ignoradas_na_os_atual"] == 0

    with modulo._lock:
        modulo._estado["os_ativa"] = {"os_id": "OS-EM-CURSO"}
    r1 = logado.cliente.post("/console/api/visao-triple-check", json={"ativo": False})
    r2 = logado.cliente.post("/console/api/visao-triple-check", json={"ativo": False})

    assert r1.status_code == r2.status_code == 200
    assert r1.json()["ativo"] is False and r1.json()["por"] == "console"
    assert r2.json() == r1.json()
    (linha,) = _linhas_switch(modulo)
    assert "visão FORA do Triple Check (pelo console)" in linha
    assert "OS-EM-CURSO" in linha
    assert modulo.orch.visao_conta() is False
    assert modulo._estado["visao_triple_check"]["ativo"] is False

    logado.cliente.post("/console/api/visao-triple-check", json={"ativo": True})
    assert "visão de volta ao Triple Check (pelo console)" in _linhas_switch(modulo)[0]
    assert len(_linhas_switch(modulo)) == 2


# ══════════════════════════════════════════════════════════════════════════════
# Task 2 — avaliar_triple_check com a visão informativa (pura)
# ══════════════════════════════════════════════════════════════════════════════

OK_PESO = {"tipo": "peso_ok"}
DIV_PESO = {"tipo": "peso_divergencia", "desvio_pct": 20.0}
MESA_OK = {"tipo": "leitura_mesa_ok", "quantidade_detectada": 5}
MESA_MAIS = {"tipo": "leitura_mesa_divergencia", "quantidade_detectada": 6}
MESA_MENOS = {"tipo": "leitura_mesa_divergencia", "quantidade_detectada": 4}


@pytest.mark.parametrize("caso,mesa,peso,trava_ligado,trava_desligado,ignoradas", [
    ("mesa a mais, resto ok", MESA_MAIS, OK_PESO, True, False, 1),
    ("câmera ok, balança divergente", MESA_OK, DIV_PESO, True, True, 0),
    ("mesa a mais e balança divergente", MESA_MAIS, DIV_PESO, True, True, 1),
    ("timeout da câmera, resto ok", None, OK_PESO, False, False, 1),
    ("falha de leitura da câmera, resto ok", {"tipo": "leitura_mesa_falha"}, OK_PESO,
     False, False, 1),
])
def test_tabela_triple_check(carregar_orquestrador, caso, mesa, peso, trava_ligado,
                             trava_desligado, ignoradas):
    avaliar = carregar_orquestrador().modulo.avaliar_triple_check
    ligado = avaliar(5, 5, mesa, peso, min_divergencias=1)
    desligado = avaliar(5, 5, mesa, peso, min_divergencias=1, visao_conta=False)

    assert ligado.travar is trava_ligado, caso
    assert ligado.visao_ignorada == ()
    assert desligado.travar is trava_desligado, caso
    assert len(desligado.visao_ignorada) == ignoradas, caso
    # A câmera nunca é causa da trava com o switch desligado: só informação.
    assert not any(d.startswith("câmera") for d in desligado.divergencias)
    assert desligado.leitura_mesa == mesa


def test_trava_com_balanca_cita_so_a_balanca(carregar_orquestrador):
    r = carregar_orquestrador().modulo.avaliar_triple_check(
        5, 5, MESA_MAIS, DIV_PESO, min_divergencias=1, visao_conta=False)
    assert r.travar and r.divergencias == ["balança: desvio=20.0%"]
    assert r.categorias == ("divergência de peso",)
    assert "câmera_mesa: detectou 6 de 5" in r.visao_ignorada[0]


def test_dispenser_continua_travando(carregar_orquestrador):
    r = carregar_orquestrador().modulo.avaliar_triple_check(
        5, 4, MESA_MENOS, OK_PESO, min_divergencias=1, visao_conta=False)
    assert r.travar and r.divergencias == ["dispenser: dispensou 4 de 5 esperados"]


def test_nenhuma_fonte_mediu_e_o_veredito_de_hoje(carregar_orquestrador):
    """Sem dispenser, câmera e balança medindo: nada contradisse, a OS segue —
    nas duas posições do switch."""
    avaliar = carregar_orquestrador().modulo.avaliar_triple_check
    for conta in (True, False):
        r = avaliar(5, None, None, None, min_divergencias=1, visao_conta=conta)
        assert r.travar is False and r.divergencias == []
        assert len(r.fontes_indisponiveis) == 3


def test_oclusao_continua_contada_com_o_switch_desligado(carregar_orquestrador):
    """O déficit da câmera anda nas duas posições: é o que permite religar no
    meio da OS sem a caixinha escondida virar "a mais" falso."""
    avaliar = carregar_orquestrador().modulo.avaliar_triple_check
    ligado = avaliar(5, 5, MESA_MENOS, OK_PESO, min_divergencias=1)
    desligado = avaliar(5, 5, MESA_MENOS, OK_PESO, min_divergencias=1, visao_conta=False)
    assert ligado.oclusao == desligado.oclusao == 1
    assert ligado.alertas and not desligado.alertas
    assert len(desligado.visao_ignorada) == 1
    reapareceu = avaliar(5, 5, MESA_MAIS, OK_PESO, min_divergencias=1,
                         tolerancia_camera_mais=1, visao_conta=False)
    assert reapareceu.reconciliado == 1 and reapareceu.visao_ignorada == ()


@pytest.mark.parametrize("resultado,rescan,trava_ligado,ignora_desligado", [
    ({"tipo": "leitura_dispenser_divergencia", "sku_lido": "X", "sku_esperado": "Y"},
     False, True, True),
    ({"tipo": "leitura_dispenser_ok"}, False, False, False),
    ({"tipo": "leitura_dispenser_falha", "motivo": "camera_obstruida"}, False, False, True),
    ({"tipo": "leitura_dispenser_falha", "motivo": "sem_etiqueta_cadastrada"},
     False, False, False),
    (None, False, False, True),
    ({"tipo": "visao_indisponivel"}, True, True, True),
], ids=["sku-errado", "ok", "falha", "sem-etiqueta", "timeout", "rescan-mudo"])
def test_tabela_sku(carregar_orquestrador, resultado, rescan, trava_ligado,
                    ignora_desligado):
    avaliar = carregar_orquestrador().modulo.avaliar_sku_dispenser
    ligado = avaliar(resultado, True, rescan=rescan)
    desligado = avaliar(resultado, False, rescan=rescan)
    assert ligado.travar is trava_ligado and ligado.ignorada is None
    assert desligado.travar is False
    assert bool(desligado.ignorada) is ignora_desligado


# ══════════════════════════════════════════════════════════════════════════════
# Task 3 — o ciclo lê o switch em cada slot
# ══════════════════════════════════════════════════════════════════════════════

def test_camera_mesa_divergente_com_switch_desligado_nao_trava(orq):
    rota = _rota(orq, 1, 2)
    orq.injecao.armar("divergencia_mesa", rota[0])
    _desligar(orq)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert orq.travas == []
    assert _alarmes(orq).count("visao_ignorada") == 1
    (alarme,) = [c for c in orq.banco.chamadas_de("salvar_alarme")
                 if c["args"][1] == "visao_ignorada"]
    assert alarme["args"][0] == "visao_ignorada"
    assert alarme["args"][2].startswith(
        "visão ignorada no Triple Check (switch desligado):")
    (marca,) = orq.banco.chamadas_de("registrar_visao_ignorada")
    assert marca["args"] == ("OS-V", rota[0])
    assert _status(orq)[-1] == "concluida"
    assert any(t == "os_concluida_visao_fora" and "1 leitura ignorada" in m
               for t, m in orq.historico)


def test_camera_mesa_divergente_com_switch_ligado_trava(orq):
    orq.injecao.armar("divergencia_mesa", _rota(orq, 1, 2)[0])

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert len(orq.travas) == 1 and "câmera_mesa" in orq.travas[0]
    assert "visao_ignorada" not in _alarmes(orq)


def test_balanca_divergente_com_switch_desligado_trava(orq):
    orq.injecao.armar("divergencia_peso", _rota(orq, 1, 2)[0])
    _desligar(orq)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert len(orq.travas) == 1 and "balança" in orq.travas[0]
    assert _status(orq)[-1] == "concluida"


def test_desligar_no_meio_vale_no_slot_seguinte(orq):
    rota = _rota(orq, 1, 2)
    orq.injecao.armar("divergencia_mesa", rota[1])

    async def _no_segundo_mover(payload):
        if payload["dispenser_alvo"] == rota[1]:
            _desligar(orq)
    orq.adapter.ao_comando["/comandos/mover"] = _no_segundo_mover

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert orq.travas == []
    assert "visao_ignorada" in _alarmes(orq)


def test_religar_no_meio_volta_a_travar_pela_camera(orq):
    rota = _rota(orq, 1, 2)
    orq.injecao.armar("divergencia_mesa", rota[1])
    _desligar(orq)

    async def _no_segundo_mover(payload):
        if payload["dispenser_alvo"] == rota[1]:
            _ligar(orq)
    orq.adapter.ao_comando["/comandos/mover"] = _no_segundo_mover

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert len(orq.travas) == 1 and "câmera_mesa" in orq.travas[0]


def test_religar_depois_de_uma_oclusao_nao_da_a_mais_falso(orq, monkeypatch):
    """Desligado, a câmera conta 1 a menos em D(a); religado, a caixinha
    escondida reaparece em D(b) como +1. O déficit andou com o switch desligado,
    então isso é reconciliação — não trava."""
    rota = _rota(orq, 1, 2)
    real = orq.adapter._responder

    def _responder(url, payload):
        if url.endswith("/comandos/capturar/mesa") and not payload.get("etapa"):
            esperada = payload["quantidade_esperada"]
            det = esperada - 1 if payload["slot_id"] == rota[0] else esperada + 1
            orq.adapter._evento(f"{payload['os_id']}:visao_mesa:{payload['slot_id']}",
                                tipo="leitura_mesa_divergencia",
                                quantidade_esperada=esperada,
                                quantidade_detectada=det, confianca=0.9)
            return
        real(url, payload)
    monkeypatch.setattr(orq.adapter, "_responder", _responder)
    _desligar(orq)

    async def _no_segundo_mover(payload):
        if payload["dispenser_alvo"] == rota[1]:
            _ligar(orq)
    orq.adapter.ao_comando["/comandos/mover"] = _no_segundo_mover

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert orq.travas == []
    assert _alarmes(orq).count("visao_ignorada") == 1   # a oclusão de D(a)
    assert "contagem_camera_abaixo" not in _alarmes(orq)


@pytest.mark.parametrize("tipo", ["sku_dispenser", "falha_leitura_dispenser"])
def test_falha_injetada_na_camera_do_dispenser_com_switch_desligado_segue(orq, tipo):
    orq.injecao.armar(tipo, _rota(orq, 1, 2)[0])
    _desligar(orq)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert orq.travas == []
    assert _alarmes(orq).count("visao_ignorada") == 1
    assert _status(orq)[-1] == "concluida"


def test_sku_errado_com_switch_ligado_trava_como_hoje(orq):
    orq.injecao.armar("sku_dispenser", _rota(orq, 1, 2)[0])

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert len(orq.travas) == 1 and "SKU errado" in orq.travas[0]


def test_trava_ativa_nao_e_liberada_pelo_switch_e_o_rescan_nao_trava_mais(orq):
    """Roteiro de emergência: trava de SKU ativa → desliga o switch → a trava
    continua → libera pelo botão de sempre → o re-scan (ainda divergente) não
    trava mais."""
    orq.adapter.capturas_divergentes = 2      # a 1ª leitura e o re-scan
    ainda_ativa: list = []

    def _desliga_com_a_trava_aberta():
        _desligar(orq)
        ainda_ativa.append(orq.modulo._trava_ativa)
    orq.ao_travar.append(_desliga_com_a_trava_aberta)

    asyncio.run(orq.modulo._processar_os(_payload("OS-V", _item("Dipirona", 4))))

    assert len(orq.travas) == 1 and "SKU errado" in orq.travas[0]
    assert ainda_ativa == [True]
    assert _alarmes(orq).count("visao_ignorada") == 1
    assert _status(orq)[-1] == "concluida"


def test_porta_aberta_com_switch_desligado_para_tudo(orq):
    from test_caixa_home import _abrir_e_retomar, _estado_porta, _ev
    _desligar(orq)
    parou = {"feito": False}

    async def _na_primeira_dispensa(payload):
        if not parou["feito"]:
            parou["feito"] = True
            await orq.modulo.evento_porta(_ev("porta_aberta", porta="frente",
                                              autorizada=False))
            assert orq.modulo.get_pausa_porta() is not None
    orq.adapter.ao_comando["/comandos/dispensar"] = _na_primeira_dispensa

    async def _cenario():
        await orq.modulo.evento_porta(_estado_porta())
        tarefa = asyncio.ensure_future(orq.modulo._processar_os(_payload()))
        while not parou["feito"]:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert orq.modulo.get_pausa_porta() is not None
        await _abrir_e_retomar(orq)
        await asyncio.wait_for(tarefa, 10)
        await orq.modulo.aguardar_tarefas_porta()

    asyncio.run(_cenario())

    assert orq.adapter.comandos("/comandos/parar")
    assert "porta_aberta_sem_autorizacao" in _alarmes(orq)
    assert _status(orq)[-1] == "concluida"


# ── O handler do central: um alarme por evento, nunca em dobro ───────────────

@pytest.mark.parametrize("evento,alarme_ligado", [
    (_leitura_dispenser(SLOT_DIR, "dispenser_dir", "leitura_dispenser_divergencia",
                        sku_lido="X"), "divergencia_sku"),
    (_leitura_dispenser(SLOT_DIR, "dispenser_dir", "leitura_dispenser_falha",
                        motivo="camera_obstruida"), "falha_leitura_dispenser"),
    (_leitura_dispenser(SLOT_DIR, "dispenser_dir", "leitura_dispenser_falha",
                        motivo="codigo_desconhecido_na_zona"),
     "codigo_desconhecido_dispenser"),
    ({"tipo": "leitura_mesa_falha", "camera": "mesa", "slot_id": 2, "os_id": "OS-1",
      "motivo": "camera_desalinhada"}, "falha_deteccao_mesa"),
], ids=["sku", "falha-disp", "codigo-desconhecido", "falha-mesa"])
def test_handler_nao_grava_o_alarme_da_camera_com_switch_desligado(
        carregar_central, evento, alarme_ligado):
    ligado = carregar_central()
    _evento_visao(ligado, dict(evento))
    assert [c["args"][1] for c in ligado.banco.chamadas_de("salvar_alarme")] == [alarme_ligado]

    desligado = carregar_central()
    desligado.modulo.chave_visao_tc.chave.desligar("teste")
    _evento_visao(desligado, dict(evento))
    assert desligado.banco.chamadas_de("salvar_alarme") == []
    # A leitura continua indo para o histórico.
    assert desligado.banco.chamadas_de("salvar_leitura_visao")


# ══════════════════════════════════════════════════════════════════════════════
# Task 4 — caixa VAZIA e FINAL no HOME
# ══════════════════════════════════════════════════════════════════════════════

def test_vazia_com_duas_e_switch_desligado_comeca_com_alarme(orq):
    orq.adapter.caixa_vazia = [2, 2, 2]
    _desligar(orq)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert len(_fotos(orq, "caixa_vazia")) == 1
    assert "caixa_nao_vazia" not in _alarmes(orq)
    assert "visao_ignorada" in _alarmes(orq)
    assert orq.adapter.comandos("/comandos/carregar")


def test_vazia_sem_resposta_e_switch_desligado_comeca(orq):
    orq.adapter.caixa_vazia = [None]
    _desligar(orq)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert "camera_mesa_indisponivel_inicio" not in _alarmes(orq)
    assert orq.adapter.comandos("/comandos/carregar")


def test_final_mais_um_e_switch_desligado_vai_ao_centro_com_a_marca(orq):
    orq.adapter.final_delta = 1
    _desligar(orq)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert orq.travas == []
    assert orq.adapter.comandos("/comandos/centro")
    assert _status(orq)[-3:] == ["conferindo_caixa", "indo_ao_centro", "concluida"]
    (marca,) = orq.banco.chamadas_de("registrar_visao_ignorada")
    assert marca["args"] == ("OS-V", None)
    (linha,) = [m for t, m in orq.historico if t == "os_concluida_visao_fora"]
    assert linha == ("OS OS-V concluída com a visão fora do Triple Check "
                     "(1 leitura ignorada)")


def test_final_mais_um_e_switch_ligado_trava(orq):
    orq.adapter.final_delta = 1

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert len(orq.travas) == 1 and "conferencia_final_a_mais" in orq.travas[0]


def test_final_sem_resposta_e_switch_desligado_vai_ao_centro(orq):
    orq.adapter.final_resposta = False
    _desligar(orq)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert orq.travas == [] and _status(orq)[-1] == "concluida"


def test_homing_final_falho_com_switch_desligado_trava(orq, monkeypatch):
    homings = {"n": 0}
    real = orq.adapter._responder

    def _responder(url, payload):
        if url.endswith("/comandos/homing"):
            homings["n"] += 1
            orq.adapter.homing_falha = homings["n"] == 2
        real(url, payload)
    monkeypatch.setattr(orq.adapter, "_responder", _responder)
    _desligar(orq)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert len(orq.travas) == 1 and "homing_final_falhou" in orq.travas[0]


@pytest.mark.parametrize("contada,peso,estado", [
    (8, True, "a_mais"), (6, False, "a_menos"), (6, True, "a_menos"),
    (None, True, "indisponivel"), (7, None, "ok")])
def test_avaliar_caixa_final_desligado_nunca_trava(carregar_orquestrador, contada,
                                                   peso, estado):
    avaliar = carregar_orquestrador().modulo.avaliar_caixa_final
    r = avaliar(contada, 7, peso, visao_conta=False)
    assert r.estado == estado and r.travar is False
    assert r.alarme == (None if estado == "ok" else "visao_ignorada")
    assert avaliar(contada, 7, peso) == avaliar(contada, 7, peso, visao_conta=True)
