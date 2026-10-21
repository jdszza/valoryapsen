# -*- coding: utf-8 -*-
"""O switch "Visão no Triple Check" fora do console: painel de bancada,
display de 7", relatório da OS e pré-voo.

O painel continua só lendo (`central_client` segue GET-only): o aviso vem num
campo novo do `GET /api/v1/trava`, que ele já consumia.
"""
import asyncio
import csv
import io

import pytest

from test_display_trava import CONTA, TRAVA

FAIXA = "VISÃO FORA DO TRIPLE CHECK"


# ── Central: /api/v1/trava, detalhe da OS, relatório, pré-voo ────────────────

def test_trava_publica_o_switch_e_a_faixa(carregar_central):
    from fastapi.testclient import TestClient
    central = carregar_central()
    cliente = TestClient(central.modulo.app)

    ligado = cliente.get("/api/v1/trava").json()
    central.modulo.chave_visao_tc.chave.desligar("teste")
    desligado = cliente.get("/api/v1/trava").json()

    assert ligado["visao_triple_check"] is True and ligado["visao_faixa"] == ""
    assert desligado["visao_triple_check"] is False and desligado["visao_faixa"] == FAIXA
    assert desligado["ativa"] is False      # o resto do contrato não mudou


def _ordem(status="concluida", ignoradas=2) -> dict:
    return {"os_id": "OS-R", "status": status, "categoria": "c", "descricao": "d",
            "criado_em": "2026-10-21 10:00:00", "visao_ignoradas": ignoradas,
            "itens": [{"dispenser_id": 1, "visao_ignorada": 1},
                      {"dispenser_id": 2, "visao_ignorada": 0}]}


def test_detalhe_da_os_marca_a_concluida_com_a_visao_fora(carregar_central, monkeypatch):
    central = carregar_central()
    m = central.modulo
    monkeypatch.setattr(m, "get_ordem_por_id", lambda os_id: _ordem())
    assert m.os_detalhe("OS-R")["observacao_visao"] == (
        "OS OS-R concluída com a visão fora do Triple Check (2 leituras ignoradas)")

    monkeypatch.setattr(m, "get_ordem_por_id", lambda os_id: _ordem(ignoradas=0))
    assert "observacao_visao" not in m.os_detalhe("OS-R")


def _corpo(resposta) -> str:
    async def _ler():
        return "".join([p if isinstance(p, str) else p.decode()
                        async for p in resposta.body_iterator])
    return asyncio.run(_ler())


def test_csv_tem_a_contagem_e_o_marcador_por_slot(carregar_central):
    m = carregar_central().modulo
    dispensas = [{"dispenser_id": 1, "medicamento": "A", "quantidade_dispensada": 3,
                  "quantidade_alvo": 3, "validado": 1, "ts": "x"},
                 {"dispenser_id": 2, "medicamento": "B", "quantidade_dispensada": 2,
                  "quantidade_alvo": 2, "validado": 1, "ts": "x"}]
    linhas = list(csv.reader(io.StringIO(_corpo(
        m._gerar_csv("OS-R", _ordem(), dispensas, [], [])))))

    cab_os = linhas[1]
    assert cab_os[-1] == "Leituras da visão ignoradas"
    assert linhas[2][-1] == "2"
    i = linhas.index(["=== DISPENSAS ==="])
    assert linhas[i + 1][-1] == "Visão ignorada"
    assert [linhas[i + 2][-1], linhas[i + 3][-1]] == ["SIM", "NÃO"]


def test_xlsx_tem_a_contagem_e_o_marcador_por_slot(carregar_central):
    openpyxl = pytest.importorskip("openpyxl")
    m = carregar_central().modulo
    dispensas = [{"dispenser_id": 1, "medicamento": "A", "quantidade_dispensada": 3,
                  "quantidade_alvo": 3, "validado": 1, "ts": "x"}]
    resposta = asyncio.run(m._gerar_xlsx("OS-R", _ordem(), dispensas, [], []))
    wb = openpyxl.load_workbook(io.BytesIO(_corpo_bytes(resposta)))
    assert wb["OS"].cell(1, 7).value == "Leituras da visão ignoradas"
    assert wb["OS"].cell(2, 7).value == 2
    assert wb["Dispensas"].cell(2, 7).value == "SIM"


def _corpo_bytes(resposta) -> bytes:
    async def _ler():
        return b"".join([p async for p in resposta.body_iterator])
    return asyncio.run(_ler())


def test_prevoo_amarelo_com_acao_so_com_o_switch_desligado(carregar_central):
    prevoo = carregar_central().modulo.prevoo
    ligado = prevoo.item_visao_triple_check(True)
    desligado = prevoo.item_visao_triple_check(False)
    assert ligado["estado"] == prevoo.OK and not ligado["acao"]
    assert desligado["estado"] == prevoo.ALERTA
    assert desligado["acao"] == "religue o switch no topo do console"
    # Alerta não derruba o veredito do pré-voo.
    assert prevoo.resumo([desligado])["pronto"] is True


def test_prevoo_da_rota_inclui_o_item(carregar_central, monkeypatch):
    from test_console import SENHA
    from fastapi.testclient import TestClient
    m = carregar_central().modulo
    monkeypatch.setattr(m.console.settings, "CONSOLE_SENHA", SENHA)

    async def sem_servicos(*a, **k):
        return []
    monkeypatch.setattr(m.prevoo, "sondar_servicos", sem_servicos)

    async def none(*a, **k):
        return None
    monkeypatch.setattr(m, "_prevoo_banco", none)
    monkeypatch.setattr(m, "_prevoo_visao", none)
    cliente = TestClient(m.app, follow_redirects=False)
    cliente.post("/console/login", data={"senha": SENHA})
    m.chave_visao_tc.chave.desligar("teste")

    itens = cliente.get("/console/api/prevoo").json()["itens"]
    (item,) = [i for i in itens if i["id"] == "visao_triple_check"]
    assert item["estado"] == "alerta"


# ── Painel de bancada e display de 7" ────────────────────────────────────────

@pytest.fixture
def painel(carregar_painel):
    painel = carregar_painel(trava_central={**TRAVA, "ativa": False, "motivo": "",
                                            "visao_faixa": FAIXA}, env=CONTA)
    return painel


def _pushes(painel) -> list:
    enviados = []
    painel.modulo.push_to_display = lambda payload: enviados.append(payload)
    return enviados


def test_painel_espelha_a_faixa_e_o_display_a_recebe_em_ascii(painel):
    enviados = _pushes(painel)
    painel.modulo.sincronizar_trava_central(avisar_display=True)

    assert painel.modulo.trava_atual()["visao_faixa"] == FAIXA
    assert enviados[-1]["porta_faixa"] == "VISAO FORA DO TRIPLE CHECK"
    # O contrato serial não ganhou campo: a faixa viaja na linha que já existia.
    assert "visao_faixa" not in enviados[-1]
    assert "visao_faixa" not in painel.modulo._trava_para_display()


def test_porta_parada_pesa_mais_que_a_visao_na_linha_do_display(painel):
    painel.central.porta = {"faixa": "MÁQUINA PARADA — PORTA FRENTE ABERTA"}
    painel.modulo.sincronizar_trava_central()
    assert painel.modulo._trava_para_display()["porta_faixa"] == (
        "MAQUINA PARADA - PORTA FRENTE ABERTA")


def test_religar_e_transicao_e_tira_o_aviso(painel):
    enviados = _pushes(painel)
    painel.modulo.sincronizar_trava_central(avisar_display=True)
    painel.central.trava = {**painel.central.trava, "visao_faixa": ""}
    painel.modulo.sincronizar_trava_central(avisar_display=True)
    assert len(enviados) == 2 and enviados[-1]["porta_faixa"] == ""


def test_dashboard_do_painel_mostra_a_faixa(painel):
    painel.modulo.sincronizar_trava_central()
    painel.logar("PCP")
    html = painel.cliente.get("/").get_data(as_text=True)
    assert 'id="visao-banner"' in html and FAIXA in html


def test_dashboard_sem_a_faixa_com_o_switch_ligado(carregar_painel):
    painel = carregar_painel(trava_central={**TRAVA, "ativa": False}, env=CONTA)
    painel.modulo.sincronizar_trava_central()
    painel.logar("PCP")
    assert 'id="visao-banner"' not in painel.cliente.get("/").get_data(as_text=True)
