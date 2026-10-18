# -*- coding: utf-8 -*-
"""A caixa de coleta conferida no HOME, e a OS que só fecha com a mesa no centro.

CLAUDE.md, "A caixa é conferida duas vezes no HOME, e a mesa só vai ao centro
com a visão ok". Os testes medem CHAMADAS (quais comandos saíram, com qual
`os_id`, quantas vezes) e nunca tempo de parede.
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import TEMPLATE_PADRAO


def _item(nome: str, quantidade: int = 5) -> dict:
    return {"medicamento": nome, "quantidade": quantidade, "sku": f"SKU-{nome}",
            "categoria": "teste"}


def _payload(os_id: str = "OS-C", *itens) -> dict:
    return {"os_id": os_id, "descricao": "teste", "template_id": TEMPLATE_PADRAO,
            "medicamentos": list(itens) or [_item("Dipirona", 4), _item("Paracetamol", 3)]}


def _status(orq) -> list:
    return [c["args"][1] for c in orq.banco.chamadas_de("atualizar_status_ordem")]


def _fotos(orq, etapa: str) -> list[dict]:
    return [p for p in orq.adapter.comandos("/comandos/capturar/mesa")
            if p.get("etapa") == etapa]


def _alarmes(orq) -> list[str]:
    return [c["args"][1] for c in orq.banco.chamadas_de("salvar_alarme")]


@pytest.fixture
def orq(carregar_orquestrador, monkeypatch):
    o = carregar_orquestrador()
    # Prazos de espera curtos: o que está em teste é a SEQUÊNCIA.
    for campo in ("VISAO_CAIXA_VAZIA_REPETIR_S", "TIMEOUT_VISAO_MESA",
                  "TIMEOUT_CNC_CENTRO", "TIMEOUT_POSICIONAMENTO"):
        monkeypatch.setattr(o.modulo.settings, campo, 0.05)
    # `salvar_alarme` com id, para provar que o alarme da espera é RESOLVIDO.
    ids = iter(range(100, 1000))

    def _salvar(fonte, tipo, descricao):
        o.banco.chamadas.append({"fn": "salvar_alarme", "args": (fonte, tipo, descricao),
                                 "kwargs": {}})
        return next(ids)
    monkeypatch.setattr(o.modulo, "salvar_alarme", _salvar)
    return o


def _liberar_trava_quando_ativar(orq, monkeypatch) -> list:
    """O supervisor de dentro do broadcast, como em test_orchestrator."""
    liberacoes: list = []

    def _broadcast():
        if orq.modulo._trava_ativa:
            liberacoes.append(orq.modulo._trava_motivo)
            orq.modulo.liberar_trava("supervisor-teste")
    monkeypatch.setattr(orq.modulo, "_broadcast_fn", _broadcast)
    return liberacoes


# ══════════════════════════════════════════════════════════════════════════════
# Task 4 — a caixa VAZIA antes da OS
# ══════════════════════════════════════════════════════════════════════════════

def test_caixa_vazia_a_os_comeca(orq):
    asyncio.run(orq.modulo._processar_os(_payload()))

    (vazia,) = _fotos(orq, "caixa_vazia")
    assert vazia["os_id"] == "VAZIA-OS-C"
    assert vazia["quantidade_esperada"] == 0
    assert "injetar_falha" not in vazia
    assert orq.adapter.comandos("/comandos/carregar")
    assert _status(orq)[:2] == ["aguardando_caixa_vazia", "em_andamento"]
    # O homing do INÍCIO vem antes da foto: a OS anterior terminou no centro.
    urls = [c["url"] for c in orq.adapter.chamadas]
    assert urls[0].endswith("/comandos/homing")


def test_caixa_com_duas_espera_esvaziar_com_alias_novo_e_resolve_o_alarme(orq, monkeypatch):
    orq.adapter.caixa_vazia = [2, 2, 0]
    resolvidos: list = []
    monkeypatch.setattr(orq.modulo, "resolver_alarme", resolvidos.append)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert [f["os_id"] for f in _fotos(orq, "caixa_vazia")] == [
        "VAZIA-OS-C", "VAZIA-OS-C-2", "VAZIA-OS-C-3"]
    assert _alarmes(orq).count("caixa_nao_vazia") == 1     # um por espera
    assert resolvidos == [100]                              # e resolvido
    assert orq.adapter.comandos("/comandos/carregar")       # e a OS começou


def test_nada_e_carregado_enquanto_a_caixa_nao_esvazia(orq):
    cargas_durante: list = []

    async def _gancho(payload):
        if payload.get("etapa") == "caixa_vazia":
            cargas_durante.append(len(orq.adapter.comandos("/comandos/carregar")))
    orq.adapter.ao_comando["/comandos/capturar/mesa"] = _gancho
    orq.adapter.caixa_vazia = [1, 0]

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert cargas_durante == [0, 0]
    assert orq.estado["caixa"] is None


def test_camera_muda_no_inicio_nao_comeca_e_repete(orq):
    orq.adapter.caixa_vazia = [None, "falha", 0]

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert len(_fotos(orq, "caixa_vazia")) == 3
    assert _alarmes(orq).count("camera_mesa_indisponivel_inicio") == 1
    assert orq.adapter.comandos("/comandos/carregar")


def test_envio_recusado_decide_na_hora(orq, monkeypatch):
    esperas: list = []
    original = orq.modulo._aguardar_sem_pausa

    async def _espia(chave, timeout, geracao):
        esperas.append(chave)
        return await original(chave, timeout, geracao)
    monkeypatch.setattr(orq.modulo, "_aguardar_sem_pausa", _espia)

    recusas = {"n": 0}
    real_post = orq.adapter.post

    async def _post(url, payload, timeout=10.0):
        if payload.get("etapa") == "caixa_vazia" and recusas["n"] == 0:
            recusas["n"] += 1
            orq.adapter.chamadas.append({"url": url, "payload": payload})
            return False
        return await real_post(url, payload, timeout)
    monkeypatch.setattr(orq.modulo, "_post", _post)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert not [c for c in esperas if c.startswith("VAZIA-OS-C:")]
    assert "VAZIA-OS-C-2:visao_mesa:1" in esperas


def test_chave_desligada_nao_fotografa_nada(orq, monkeypatch):
    monkeypatch.setattr(orq.modulo.settings, "VISAO_CAIXA_VAZIA", False)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert not _fotos(orq, "caixa_vazia") and not _fotos(orq, "caixa_final")
    assert orq.adapter.comandos("/comandos/centro")
    assert _status(orq)[-1] == "concluida"


def test_cancelar_durante_a_espera_cancela_sem_dispensar(orq):
    orq.adapter.caixa_vazia = [3, 3, 3, 3, 3, 3]
    vezes = {"n": 0}

    async def _gancho(payload):
        vezes["n"] += 1
        if vezes["n"] == 2:
            assert orq.modulo.cancelar_espera_caixa() == "OS-C"
    orq.adapter.ao_comando["/comandos/capturar/mesa"] = _gancho

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert _status(orq)[-1] == "cancelada"
    assert not orq.adapter.comandos("/comandos/carregar")
    assert not orq.adapter.comandos("/comandos/dispensar")
    assert orq.estado["os_ativa"] is None
    assert orq.modulo.cancelar_espera_caixa() is None


def test_homing_do_inicio_que_falha_nao_comeca(orq):
    orq.adapter.homing_falha = True

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert _status(orq)[-1] == "erro"
    assert "homing_inicial_falhou" in _alarmes(orq)
    assert not _fotos(orq, "caixa_vazia")
    assert not orq.adapter.comandos("/comandos/carregar")


# ══════════════════════════════════════════════════════════════════════════════
# Task 5 — a caixa CHEIA no HOME
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("contada,esperada,peso,estado,trava,alarme", [
    (7, 7, True, "ok", False, None),
    (7, 7, None, "ok", False, None),
    (8, 7, True, "a_mais", True, None),
    (6, 7, True, "a_menos", False, "conferencia_final_abaixo"),
    (6, 7, False, "a_menos", True, None),
    (6, 7, None, "a_menos", True, None),
    (None, 7, True, "indisponivel", True, None),
    (0, 0, None, "ok", False, None),
])
def test_avaliar_caixa_final(carregar_orquestrador, contada, esperada, peso, estado,
                             trava, alarme):
    r = carregar_orquestrador().modulo.avaliar_caixa_final(contada, esperada, peso)
    assert (r.estado, r.travar, r.alarme) == (estado, trava, alarme)
    if contada is not None and contada != esperada:
        assert str(contada) in r.texto and str(esperada) in r.texto


def test_final_ok_vai_ao_centro_sem_trava(orq):
    asyncio.run(orq.modulo._processar_os(_payload()))

    (final,) = _fotos(orq, "caixa_final")
    assert final["os_id"] == "FINAL-OS-C"
    assert final["quantidade_esperada"] == 7          # 4 + 3, o que o ciclo confirmou
    assert "injetar_falha" not in final
    assert not orq.modulo._trava_ativa
    assert orq.adapter.comandos("/comandos/centro")
    assert _status(orq)[-3:] == ["conferindo_caixa", "indo_ao_centro", "concluida"]


def test_final_mais_um_trava(orq, monkeypatch):
    orq.adapter.final_delta = 1
    liberacoes = _liberar_trava_quando_ativar(orq, monkeypatch)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert len(liberacoes) == 1 and "conferencia_final_a_mais" in liberacoes[0]
    assert "8" in liberacoes[0] and "7" in liberacoes[0]


def test_final_menos_um_com_balanca_ok_e_alarme_sem_trava(orq, monkeypatch):
    orq.adapter.final_delta = -1
    liberacoes = _liberar_trava_quando_ativar(orq, monkeypatch)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert liberacoes == []
    assert "conferencia_final_abaixo" in _alarmes(orq)
    assert _status(orq)[-1] == "concluida"


def test_final_menos_um_com_balanca_divergente_trava(orq, monkeypatch):
    orq.adapter.final_delta = -1
    liberacoes = _liberar_trava_quando_ativar(orq, monkeypatch)
    # A balança diverge num slot: o Triple Check daquele slot trava (e é
    # liberado), e a FINAL a menos também trava — a balança não fecha.
    orig = orq.adapter._pesagem

    def _pesagem(payload):
        r = orig(payload)
        if payload["slot_id"] == orq.adapter.comandos("/comandos/pesar")[0]["slot_id"]:
            r = {**r, "tipo": "peso_divergencia", "desvio_pct": 20.0}
        return r
    monkeypatch.setattr(orq.adapter, "_pesagem", _pesagem)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert any("conferencia_final_a_menos" in m for m in liberacoes)


def test_final_sem_resposta_trava(orq, monkeypatch):
    orq.adapter.final_resposta = False
    liberacoes = _liberar_trava_quando_ativar(orq, monkeypatch)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert len(liberacoes) == 1 and "conferencia_final_sem_camera" in liberacoes[0]
    # Liberada pelo supervisor (ele viu a caixa): segue ao centro.
    assert orq.adapter.comandos("/comandos/centro")
    assert _status(orq)[-1] == "concluida"


def test_caso_da_auditoria_de_07_10_caixa_certa_no_home_nao_trava(orq, monkeypatch):
    """Timeout numa foto de slot + slot invisível + caixa certa no HOME → ok.

    Antes, a conferência final herdava a dessincronia e o incremento podia
    incluir o slot cujo total ninguém sabia se fora registrado. A FINAL conta a
    caixa INTEIRA sob um `os_id` novo: não herda nada."""
    itens = (_item("A", 4), _item("B", 3), _item("C", 2))
    rota = orq.modulo.planejar_rota([1, 2, 3], orq.modulo.HOME)
    monkeypatch.setattr(orq.modulo.settings, "VISAO_MESA_POSICOES",
                        frozenset({rota[0], rota[1]}))      # o último é invisível
    real = orq.adapter._responder

    def _responder(url, payload):
        if (url.endswith("/comandos/capturar/mesa") and not payload.get("etapa")
                and payload["slot_id"] == rota[1]):
            return                                         # timeout nesta foto
        real(url, payload)
    monkeypatch.setattr(orq.adapter, "_responder", _responder)
    liberacoes = _liberar_trava_quando_ativar(orq, monkeypatch)

    asyncio.run(orq.modulo._processar_os(_payload("OS-AUD", *itens)))

    assert liberacoes == []
    (final,) = _fotos(orq, "caixa_final")
    assert final["quantidade_esperada"] == 9
    assert _status(orq)[-1] == "concluida"


def test_homing_final_que_falha_trava_e_nao_fotografa(orq, monkeypatch):
    liberacoes = _liberar_trava_quando_ativar(orq, monkeypatch)
    homings = {"n": 0}
    real = orq.adapter._responder

    def _responder(url, payload):
        # O 1º homing é o do início; o 2º (fim da OS) falha; o 3º, depois da
        # liberação, confirma.
        if url.endswith("/comandos/homing"):
            homings["n"] += 1
            orq.adapter.homing_falha = homings["n"] == 2
        real(url, payload)
    monkeypatch.setattr(orq.adapter, "_responder", _responder)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert len(liberacoes) == 1 and "homing_final_falhou" in liberacoes[0]
    assert "homing_final_falhou" in _alarmes(orq)
    # Liberada, a etapa se refaz desde o homing — e então fotografa.
    assert len(_fotos(orq, "caixa_final")) == 1
    assert _status(orq)[-1] == "concluida"


# ══════════════════════════════════════════════════════════════════════════════
# Task 6 — `centro` depois da visão ok
# ══════════════════════════════════════════════════════════════════════════════

def test_final_a_mais_nao_vai_ao_centro_antes_da_liberacao(orq, monkeypatch):
    orq.adapter.final_delta = 1
    no_momento: list = []

    def _broadcast():
        if orq.modulo._trava_ativa:
            no_momento.append(len(orq.adapter.comandos("/comandos/centro")))
            orq.modulo.liberar_trava("supervisor-teste")
    monkeypatch.setattr(orq.modulo, "_broadcast_fn", _broadcast)

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert no_momento == [0]
    assert len(orq.adapter.comandos("/comandos/centro")) == 1
    assert _status(orq)[-1] == "concluida"


def test_centro_que_nao_chega_alarma_sem_trava_e_reenviar_conclui(orq):
    orq.adapter.centro_falhas = 1

    async def _rodar():
        tarefa = asyncio.ensure_future(orq.modulo._processar_os(_payload()))
        while orq.modulo._reenviar_centro is None:
            await asyncio.sleep(0.01)
        # Parada esperando o console: não concluída, sem trava.
        assert _status(orq)[-1] == "indo_ao_centro"
        assert not orq.modulo._trava_ativa
        assert orq.estado["caixa"]["problema"] == "centro"
        assert orq.modulo.reenviar_centro() == "OS-C"
        await tarefa
    asyncio.run(_rodar())

    assert "cnc_nao_chegou_ao_centro" in _alarmes(orq)
    assert len(orq.adapter.comandos("/comandos/centro")) == 2
    assert _status(orq)[-1] == "concluida"
    assert orq.modulo.reenviar_centro() is None


def test_centro_mudo_espera_o_prazo_e_nao_conclui(orq):
    orq.adapter.centro_mudo = True

    async def _rodar():
        tarefa = asyncio.ensure_future(orq.modulo._processar_os(_payload()))
        while orq.modulo._reenviar_centro is None:
            await asyncio.sleep(0.01)
        assert "concluida" not in _status(orq)
        orq.adapter.centro_mudo = False
        orq.modulo.reenviar_centro()
        await tarefa
    asyncio.run(_rodar())
    assert _status(orq)[-1] == "concluida"


def test_concluida_so_depois_do_centro(orq):
    momentos: list = []

    async def _gancho(payload):
        momentos.append(list(_status(orq)))
    orq.adapter.ao_comando["/comandos/centro"] = _gancho

    asyncio.run(orq.modulo._processar_os(_payload()))

    assert "concluida" not in momentos[0]
    assert _status(orq).count("concluida") == 1


# ══════════════════════════════════════════════════════════════════════════════
# Task 7 — handler, alarmes e pré-voo
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tipo", ["leitura_mesa_ok", "leitura_mesa_divergencia",
                                  "leitura_mesa_falha"])
def test_leitura_final_grava_na_os_real_com_etapa_e_sem_alarme(carregar_central, tipo):
    central = carregar_central()
    asyncio.run(central.modulo._handle_evento_visao({
        "tipo": tipo, "camera": "mesa", "slot_id": 1, "os_id": "FINAL-OS-9-2",
        "quantidade_esperada": 7, "quantidade_detectada": 6}))

    (linha,) = central.banco.chamadas_de("salvar_leitura_visao")
    assert linha["args"][0] == "OS-9"
    assert linha["args"][-1] == "caixa_final"
    assert central.banco.chamadas_de("salvar_alarme") == []


def test_os_id_sem_prefixo_continua_como_veio(carregar_central):
    central = carregar_central()
    asyncio.run(central.modulo._handle_evento_visao({
        "tipo": "leitura_mesa_ok", "camera": "mesa", "slot_id": 2, "os_id": "MAPA-2-a",
        "quantidade_esperada": 3, "quantidade_detectada": 3}))
    (linha,) = central.banco.chamadas_de("salvar_leitura_visao")
    assert linha["args"][0] == "MAPA-2-a" and linha["args"][-1] == "slot"


def test_o_sufixo_de_repeticao_nao_come_o_hex_da_os(carregar_orquestrador):
    m = carregar_orquestrador().modulo
    real = "OS-URO-01-20261018T101010-123456"
    assert m.resolver_alias_visao(f"VAZIA-{real}-3") == (real, "caixa_vazia")
    assert m.resolver_alias_visao(f"FINAL-{real}") == (real, "caixa_final")


def test_um_alarme_por_espera_da_caixa(orq):
    orq.adapter.caixa_vazia = [2, 2, 2, 0]
    asyncio.run(orq.modulo._processar_os(_payload()))
    assert _alarmes(orq).count("caixa_nao_vazia") == 1


def test_prevoo_avisa_home_fora_das_posicoes(carregar_central):
    import importlib.util
    import pathlib
    raiz = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("prevoo_caixa", raiz / "central-computer" / "prevoo.py")
    prevoo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prevoo)
    saude = {"fontes": {"mesa": "estacao", "dispensers": "simulador"},
             "identidade_mesa": {"confere": True},
             "estacao_mesa": {"pronta": True}, "etiquetas": {"nomes": []}}
    com = {i["id"]: i for i in prevoo.itens_visao(saude, True, [], caixa_vazia=True,
                                                  home_visivel=False)}
    assert com["visao_mesa_home"]["estado"] == prevoo.ALERTA
    assert "VISAO_MESA_POSICOES" in com["visao_mesa_home"]["acao"]
    sem = {i["id"] for i in prevoo.itens_visao(saude, True, [], caixa_vazia=True,
                                               home_visivel=True)}
    assert "visao_mesa_home" not in sem


# ══════════════════════════════════════════════════════════════════════════════
# Task 8 — pausa da porta NR-12 e reinício do central nas etapas da caixa
# ══════════════════════════════════════════════════════════════════════════════

def _ev(tipo: str, **campos) -> dict:
    return {"origem": "porta", "tipo": tipo, **campos, "ts": "2026-10-18T10:00:00"}


def _estado_porta(rearme=False, **portas) -> dict:
    return _ev("estado", teto=portas.get("teto", "fechada"),
               frente=portas.get("frente", "fechada"), estado="FECHADA",
               aguardando_rearme=rearme)


async def _abrir_e_retomar(orq) -> None:
    """A porta da frente abre (sem autorização), fecha, e a tag rearma."""
    await orq.modulo.evento_porta(_ev("porta_aberta", porta="frente", autorizada=False))
    await orq.modulo.evento_porta(_ev("porta_fechada"))
    await orq.modulo.evento_porta(_estado_porta(rearme=True))
    await orq.modulo.evento_porta(_ev("rearme", uid="04A1B2C3"))
    await orq.modulo.evento_porta(_estado_porta())


def _rodar_com_pausa(orq, etapa: str, os_id: str = "OS-P") -> list[str]:
    """Abre a porta DENTRO da primeira foto da `etapa` (com o evento dela ainda
    no fio) e retoma. Devolve os aliases das fotos dessa etapa."""
    pausou = {"feito": False}
    real = orq.adapter._responder

    def _responder(url, payload):
        if (url.endswith("/comandos/capturar/mesa") and payload.get("etapa") == etapa
                and not pausou["feito"]):
            pausou["feito"] = True
            return                       # a foto do momento da pausa não volta
        real(url, payload)
    orq.adapter._responder = _responder

    async def _cenario():
        await orq.modulo.evento_porta(_estado_porta())
        tarefa = asyncio.ensure_future(orq.modulo._processar_os(_payload(os_id)))
        while not pausou["feito"]:
            await asyncio.sleep(0.01)
        await _abrir_e_retomar(orq)
        await asyncio.wait_for(tarefa, 10)
        await orq.modulo.aguardar_tarefas_porta()

    asyncio.run(_cenario())
    return [f["os_id"] for f in _fotos(orq, etapa)]


@pytest.mark.parametrize("etapa,prefixo", [("caixa_vazia", "VAZIA"),
                                           ("caixa_final", "FINAL")])
def test_pausa_na_foto_da_caixa_refaz_com_alias_novo(orq, etapa, prefixo):
    aliases = _rodar_com_pausa(orq, etapa)

    assert aliases == [f"{prefixo}-OS-P", f"{prefixo}-OS-P-2"]
    assert _status(orq)[-1] == "concluida"
    assert not orq.modulo._trava_ativa


def test_pausa_na_ida_ao_centro_refaz_homing_final_e_centro(orq):
    pausou = {"feito": False}
    real = orq.adapter._responder

    def _responder(url, payload):
        if url.endswith("/comandos/centro") and not pausou["feito"]:
            pausou["feito"] = True
            return                       # a mesa parou no caminho
        real(url, payload)
    orq.adapter._responder = _responder

    async def _cenario():
        await orq.modulo.evento_porta(_estado_porta())
        tarefa = asyncio.ensure_future(orq.modulo._processar_os(_payload()))
        while not pausou["feito"]:
            await asyncio.sleep(0.01)
        await _abrir_e_retomar(orq)
        await asyncio.wait_for(tarefa, 10)
        await orq.modulo.aguardar_tarefas_porta()
    asyncio.run(_cenario())

    assert [f["os_id"] for f in _fotos(orq, "caixa_final")] == ["FINAL-OS-C", "FINAL-OS-C-2"]
    assert len(orq.adapter.comandos("/comandos/centro")) == 2
    assert _status(orq)[-1] == "concluida"


def test_reinicio_retoma_a_etapa_final_com_o_esperado_do_banco(orq):
    """`conferindo_caixa`/`indo_ao_centro` no boot: homing → FINAL → centro."""
    asyncio.run(orq.modulo._retomar_etapa_final(
        {"os_id": "OS-R", "retomar_final": True, "esperado": 9}))

    (final,) = _fotos(orq, "caixa_final")
    assert (final["os_id"], final["quantidade_esperada"]) == ("FINAL-OS-R", 9)
    assert orq.adapter.comandos("/comandos/homing")
    assert orq.adapter.comandos("/comandos/centro")
    assert not orq.adapter.comandos("/comandos/dispensar")
    assert _status(orq)[-1] == "concluida"


def test_reinicio_sem_balanca_trava_caixa_a_menos(orq, monkeypatch):
    """Depois de um reinício a balança não tem o que dizer: a menos, trava."""
    orq.adapter.final_delta = -1
    liberacoes = _liberar_trava_quando_ativar(orq, monkeypatch)

    asyncio.run(orq.modulo._retomar_etapa_final({"os_id": "OS-R", "esperado": 9}))

    assert len(liberacoes) == 1 and "conferencia_final_a_menos" in liberacoes[0]


def test_lifespan_reenfileira_as_os_da_caixa(carregar_central, monkeypatch):
    central = carregar_central()
    monkeypatch.setattr(central.modulo, "os_para_retomar_no_boot", lambda: {
        "final": [("OS-F", 7)],
        "refila": [{"os_id": "OS-V", "medicamentos": [{"medicamento": "X"}]}]})
    enfileiradas: list = []

    async def _enfileirar(payload):
        enfileiradas.append(payload)
        return True
    monkeypatch.setattr(central.modulo.orch, "enfileirar_os", _enfileirar)

    asyncio.run(central.modulo._retomar_os_da_caixa_no_boot())

    assert enfileiradas[0] == {"os_id": "OS-F", "retomar_final": True, "esperado": 7}
    assert enfileiradas[1]["os_id"] == "OS-V"
    assert "retomar_final" not in enfileiradas[1]


def test_lifespan_com_fila_cheia_fecha_em_erro_com_alarme(carregar_central, monkeypatch):
    central = carregar_central()
    monkeypatch.setattr(central.modulo, "os_para_retomar_no_boot",
                        lambda: {"final": [("OS-F", 7)], "refila": []})

    async def _cheia(payload):
        return False
    monkeypatch.setattr(central.modulo.orch, "enfileirar_os", _cheia)

    asyncio.run(central.modulo._retomar_os_da_caixa_no_boot())

    assert ("OS-F", "erro") in [c["args"] for c in
                                central.banco.chamadas_de("atualizar_status_ordem")]
    assert "os_orfa_no_boot" in [c["args"][1] for c in central.banco.chamadas_de("salvar_alarme")]


def test_loop_despacha_a_retomada_final(orq, monkeypatch):
    chamadas: list = []

    async def _retomar(payload):
        chamadas.append(payload["os_id"])
        raise asyncio.CancelledError     # sai do laço infinito
    monkeypatch.setattr(orq.modulo, "_retomar_etapa_final", _retomar)

    async def _cenario():
        await orq.modulo.enfileirar_os({"os_id": "OS-F", "retomar_final": True,
                                        "esperado": 1})
        try:
            await orq.modulo.loop_orquestrador()
        except asyncio.CancelledError:
            pass
    asyncio.run(_cenario())
    assert chamadas == ["OS-F"]
