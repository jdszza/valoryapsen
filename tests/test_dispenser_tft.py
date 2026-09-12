"""A segunda placa do dispenser-adapter: as 8 telas TFT (`dispenser_tft`).

Acionar os 8 mecanismos, desenhar 8 telas e manter a serial não cabe num ESP
só. São DUAS placas em DUAS portas, e o dono das duas é o MESMO
dispenser-adapter, com dois `LinkSerial`. O que estes testes prendem:

  1. com `DISPENSER_TFT_TRANSPORTE` no default (`http`) o adapter se comporta
     EXATAMENTE como antes desta placa existir — nenhuma tela, nenhum envio a
     mais, o mesmo corpo para o simulador;
  2. `slot` sai na TRANSIÇÃO (comando aceito, evento recebido) e só nela;
     telemetria periódica dos mecanismos não vira `slot`;
  3. falha da placa das telas NUNCA muda o caminho do dispenser: não recusa
     comando, não atrasa ACK, não impede o evento de chegar ao central;
  4. os eventos DA placa das telas ficam no adapter (log e /health), nunca no
     central; e `trava_resumo` nunca passa de 48 caracteres.

O contrato em si (comandos, campos, eventos) é conferido contra o documento em
`test_protocolo_placas.py`; o transporte, em `test_serial_link.py`.
"""
import asyncio
import json
import threading
import time

import pytest

from fakes.placa_dispenser import PlacaDispenser
from fakes.placa_dispenser_tft import PlacaDispenserTFT
from test_serial_link import ClienteHTTPFake, _ate

pytest.importorskip("serial", reason="pyserial (tests/requirements-dev.txt)")


class Montagem:
    def __init__(self, modulo, placa_disp, placa_tft, loop, cliente):
        self.modulo = modulo
        self.placa_disp = placa_disp
        self.placa_tft = placa_tft
        self.loop = loop
        self.cliente = cliente

    def chamar(self, corotina, timeout: float = 5.0):
        return asyncio.run_coroutine_threadsafe(corotina, self.loop).result(timeout)

    @property
    def eventos_no_central(self) -> list:
        return [c["json"] for c in self.cliente.posts]

    def comandos_de_tela(self, cmd: str = "slot") -> list:
        return [e for e in self.placa_tft.executados if e["cmd"] == cmd]


@pytest.fixture
def adapter_com_telas(carregar_adapter):
    """dispenser-adapter com a porta das telas ligada a uma placa falsa — e,
    opcionalmente, a dos mecanismos também."""
    criados: list = []

    def _montar(mecanismos_serial: bool = True, ack_timeout_s: float = 1.0):
        placa_tft = PlacaDispenserTFT(intervalo_ping=0.2).iniciar()
        placa_disp = PlacaDispenser(intervalo_ping=0.2).iniciar() if mecanismos_serial else None
        env = {
            "DISPENSER_TFT_TRANSPORTE": "serial",
            "DISPENSER_TFT_SERIAL_URL": placa_tft.url,
            "DISPENSER_TFT_ACK_TIMEOUT_S": str(ack_timeout_s),
        }
        if placa_disp is not None:
            env.update({
                "DISPENSER_TRANSPORTE": "serial",
                "DISPENSER_SERIAL_URL": placa_disp.url,
                "DISPENSER_ACK_TIMEOUT_S": str(ack_timeout_s),
            })
        modulo = carregar_adapter("dispenser", env=env)

        loop = asyncio.new_event_loop()
        pronto = threading.Event()

        def _rodar():
            asyncio.set_event_loop(loop)
            loop.call_soon(pronto.set)
            loop.run_forever()

        thread = threading.Thread(target=_rodar, name="loop-adapter-tft", daemon=True)
        thread.start()
        pronto.wait(5)

        cliente = ClienteHTTPFake()
        modulo._client = cliente
        modulo._loop = loop

        links = []
        link_tft = modulo.serial_link.LinkSerial(
            subsistema=modulo.TFT_SUBSISTEMA, url=modulo.TFT_SERIAL_URL,
            ack_timeout_s=modulo.TFT_ACK_TIMEOUT_S,
            ao_receber_evento=modulo._evento_da_placa_tft,
            probe_assentar_s=0.0, probe_espera_s=1.0, reconexao_s=0.05,
        )
        modulo._link_tft = link_tft
        link_tft.iniciar()
        links.append(link_tft)
        if placa_disp is not None:
            link = modulo.serial_link.LinkSerial(
                subsistema=modulo.SUBSISTEMA, url=modulo.SERIAL_URL,
                ack_timeout_s=modulo.ACK_TIMEOUT_S,
                ao_receber_evento=modulo._evento_da_placa,
                probe_assentar_s=0.0, probe_espera_s=1.0, reconexao_s=0.05,
            )
            modulo._link = link
            link.iniciar()
            links.append(link)
        for link in links:
            assert _ate(lambda: link.conectado)

        montagem = Montagem(modulo, placa_disp, placa_tft, loop, cliente)
        criados.append((montagem, links, thread))
        return montagem

    yield _montar

    for montagem, links, thread in criados:
        for link in links:
            link.parar()
        montagem.placa_tft.parar()
        if montagem.placa_disp is not None:
            montagem.placa_disp.parar()
        montagem.loop.call_soon_threadsafe(montagem.loop.stop)
        thread.join(3)
        montagem.loop.close()


def _carregar_req(modulo, slot: int = 1):
    return modulo.ComandoCarregarReq(
        dispenser_id=slot, medicamento="Dipirona 500mg", sku="APSEN-001",
        categoria="analgesico", quantidade=10, os_id="OS-1")


# ── 1. Com o default, nada muda ───────────────────────────────────────────────

def test_com_o_default_o_adapter_e_exatamente_o_de_antes(carregar_adapter, monkeypatch):
    modulo = carregar_adapter("dispenser")
    assert modulo.TFT_TRANSPORTE == "http"
    assert modulo._link_tft is None

    enviados: list = []

    async def _fake(path, payload, timeout=None):
        enviados.append((path, payload))
        return {"ok": True}

    monkeypatch.setattr(modulo, "_post_sim", _fake)
    asyncio.run(modulo.cmd_carregar(_carregar_req(modulo)))
    asyncio.run(modulo.cmd_dispensar(modulo.ComandoDispensarReq(dispenser_id=1, os_id="OS-1")))
    asyncio.run(modulo.cmd_limpar(modulo.ComandoLimparReq(dispenser_id=1, solicitado_por="t")))

    # Os mesmos três posts de sempre, com o mesmo corpo — e nada além deles.
    assert [p for p, _ in enviados] == ["/executar/carregar", "/executar/dispensar",
                                        "/executar/limpar"]
    assert enviados[0][1] == {"dispenser_id": 1, "medicamento": "Dipirona 500mg",
                              "sku": "APSEN-001", "categoria": "analgesico",
                              "quantidade": 10, "os_id": "OS-1"}
    assert enviados[1][1] == {"dispenser_id": 1, "os_id": "OS-1", "injetar_falha": None}


def test_com_o_default_slot_nao_vai_a_lugar_nenhum(carregar_adapter):
    """Sem telas não há o que pintar: `slot` nem vira tarefa."""
    modulo = carregar_adapter("dispenser")

    async def _cenario():
        return modulo._enviar_tft("slot", {"dispenser_id": 1})

    assert asyncio.run(_cenario()) is None


def test_com_o_default_estado_celula_desce_ao_simulador(carregar_adapter):
    """A perna de cima não pode saber qual transporte está embaixo: com http,
    `estado_celula` vai ao simulador (que só loga) em vez de tomar 404."""
    modulo = carregar_adapter("dispenser")
    cliente = ClienteHTTPFake()
    modulo._client = cliente

    resposta = asyncio.run(modulo.cmd_estado_celula(modulo.EstadoCelulaReq(
        trava_ativa=True, trava_slot_id=3, os_id="OS-1", trava_resumo="divergência de peso")))

    assert resposta == {"ok": True, "telas": "ok"}
    (post,) = cliente.posts
    assert post["url"].endswith("/executar/estado-celula")
    assert post["json"] == {"trava_ativa": True, "trava_slot_id": 3, "os_id": "OS-1",
                            "trava_resumo": "divergência de peso"}


def test_evento_com_o_default_chega_ao_central_como_sempre(carregar_adapter):
    modulo = carregar_adapter("dispenser")
    cliente = ClienteHTTPFake()
    modulo._client = cliente
    payload = {"tipo": "carregado", "dispenser_id": 2, "os_id": "OS-1",
               "medicamento": "Dipirona 500mg", "quantidade_total": 10}

    asyncio.run(modulo.receber_evento(modulo.EventoReq(**payload)))

    assert cliente.posts[0]["json"]["tipo"] == "carregado"
    assert cliente.posts[0]["url"].endswith("/api/v1/eventos/dispenser")


# ── 2. `slot` sai na transição, e só nela ─────────────────────────────────────

def test_carregar_espelha_o_slot_ao_aceitar_e_ao_confirmar(adapter_com_telas):
    m = adapter_com_telas()

    m.chamar(m.modulo.cmd_carregar(_carregar_req(m.modulo, slot=1)))

    # 1º: o estado que o adapter acabou de comandar ("carregando");
    # 2º: o `carregado` da placa dos mecanismos, ANTES de ir ao central.
    assert _ate(lambda: len(m.comandos_de_tela()) == 2)
    primeiro, segundo = m.comandos_de_tela()
    assert (primeiro["dispenser_id"], primeiro["status"]) == (1, "carregando")
    assert (segundo["dispenser_id"], segundo["status"]) == (1, "pronto")
    assert segundo["medicamento"] == "Dipirona 500mg" and segundo["quantidade_alvo"] == 10
    assert m.placa_tft.telas[1]["status"] == "pronto"
    assert _ate(lambda: len(m.eventos_no_central) == 1)
    assert m.eventos_no_central[0]["tipo"] == "carregado"


def test_dispensar_e_limpar_espelham_as_transicoes(adapter_com_telas):
    m = adapter_com_telas()
    m.chamar(m.modulo.cmd_carregar(_carregar_req(m.modulo, slot=2)))
    assert _ate(lambda: len(m.comandos_de_tela()) == 2)

    m.chamar(m.modulo.cmd_dispensar(m.modulo.ComandoDispensarReq(dispenser_id=2, os_id="OS-1")))
    assert _ate(lambda: len(m.comandos_de_tela()) == 4)
    assert [c["status"] for c in m.comandos_de_tela()[2:]] == ["dispensando", "concluido"]
    assert m.comandos_de_tela()[3]["quantidade_dispensada"] == 10

    m.chamar(m.modulo.cmd_limpar(m.modulo.ComandoLimparReq(dispenser_id=2, solicitado_por="t")))
    assert _ate(lambda: len(m.comandos_de_tela()) == 6)
    assert [c["status"] for c in m.comandos_de_tela()[4:]] == ["limpando", "limpo"]
    assert m.comandos_de_tela()[5]["medicamento"] == ""


def test_telemetria_periodica_dos_mecanismos_nao_vira_slot(adapter_com_telas):
    """Nada periódico no canal das telas: o canal é 115200 e compete com os
    ACKs que o adapter está esperando."""
    m = adapter_com_telas()
    m.placa_disp.emitir(m.placa_disp.telemetria(1, 27.4))
    m.placa_disp.emitir({"tipo": "status", "dispenser_id": 1, "status": "idle",
                         "quantidade": 0, "ts": "2026-09-12T10:00:00"})
    assert _ate(lambda: len(m.eventos_no_central) == 2)
    time.sleep(0.3)
    assert m.comandos_de_tela() == []


# ── 3. Falha das telas nunca muda o caminho do dispenser ─────────────────────

def test_placa_de_telas_fora_nao_atrasa_nem_recusa_dispensar(adapter_com_telas):
    m = adapter_com_telas(ack_timeout_s=2.0)
    m.chamar(m.modulo.cmd_carregar(_carregar_req(m.modulo, slot=1)))
    assert _ate(lambda: len(m.eventos_no_central) == 1)

    m.placa_tft.derrubar_cabo()
    assert _ate(lambda: not m.modulo._link_tft.conectado)

    inicio = time.monotonic()
    resposta = m.chamar(m.modulo.cmd_dispensar(
        m.modulo.ComandoDispensarReq(dispenser_id=1, os_id="OS-1")))
    duracao = time.monotonic() - inicio

    assert resposta["ok"] is True
    assert duracao < 1.0, f"dispensar levou {duracao:.2f}s com a placa de telas fora"
    assert _ate(lambda: len(m.eventos_no_central) == 2)
    assert m.eventos_no_central[1]["tipo"] == "dispensado"
    assert m.eventos_no_central[1]["quantidade_dispensada"] == 10


def test_placa_de_telas_muda_nao_segura_o_ack_do_dispenser(adapter_com_telas):
    """Telas que não confirmam: o `limpar` responde no tempo do ACK dos
    MECANISMOS, não no timeout das telas."""
    m = adapter_com_telas(ack_timeout_s=1.5)
    m.placa_tft.mudo = True

    inicio = time.monotonic()
    resposta = m.chamar(m.modulo.cmd_limpar(
        m.modulo.ComandoLimparReq(dispenser_id=1, solicitado_por="t")))
    duracao = time.monotonic() - inicio

    assert resposta["ok"] is True
    assert duracao < 1.0, f"limpar esperou {duracao:.2f}s pelas telas"
    assert _ate(lambda: len(m.eventos_no_central) == 1)   # limpeza_ok chegou


def test_estado_celula_com_telas_fora_responde_200_e_diz_falha(adapter_com_telas):
    m = adapter_com_telas(mecanismos_serial=False)
    m.placa_tft.derrubar_cabo()
    assert _ate(lambda: not m.modulo._link_tft.conectado)

    resposta = m.chamar(m.modulo.cmd_estado_celula(m.modulo.EstadoCelulaReq(
        trava_ativa=True, trava_slot_id=3, os_id="OS-1", trava_resumo="SKU errado")))

    assert resposta == {"ok": True, "telas": "falha"}


# ── 4. O que é das telas fica nas telas ──────────────────────────────────────

def test_estado_celula_chega_so_a_placa_das_telas(adapter_com_telas):
    m = adapter_com_telas()

    resposta = m.chamar(m.modulo.cmd_estado_celula(m.modulo.EstadoCelulaReq(
        trava_ativa=True, trava_slot_id=3, os_id="OS-1", trava_resumo="divergência de peso")))

    assert resposta == {"ok": True, "telas": "ok"}
    assert _ate(lambda: m.placa_tft.celula.get("trava_ativa") is True)
    assert m.placa_tft.celula == {"trava_ativa": True, "trava_slot_id": 3,
                                  "os_id": "OS-1", "trava_resumo": "divergência de peso"}
    time.sleep(0.2)
    assert m.placa_disp.executados == [], "a placa dos mecanismos não tem tela"
    assert m.eventos_no_central == []


def test_trava_resumo_nunca_passa_de_48_caracteres(adapter_com_telas):
    m = adapter_com_telas(mecanismos_serial=False)
    longo = "divergência de peso; contagem divergente; SKU errado; dispenser divergente — " * 3

    m.chamar(m.modulo.cmd_estado_celula(m.modulo.EstadoCelulaReq(
        trava_ativa=True, trava_slot_id=8, os_id="OS-1", trava_resumo=longo)))

    assert _ate(lambda: m.placa_tft.celula.get("trava_ativa") is True)
    assert len(m.placa_tft.celula["trava_resumo"]) <= 48
    assert m.modulo.TRAVA_RESUMO_MAX == 48
    assert m.modulo._resumo_trava("  duas   linhas\nde   texto ") == "duas linhas de texto"


def test_trava_sem_slot_leva_a_chave_nula(adapter_com_telas):
    """A CHAVE vai sempre: é ela que diz a cada tela se é "este slot"."""
    m = adapter_com_telas(mecanismos_serial=False)
    m.chamar(m.modulo.cmd_estado_celula(m.modulo.EstadoCelulaReq(
        trava_ativa=False, trava_slot_id=None, os_id="", trava_resumo="")))
    assert _ate(lambda: "trava_slot_id" in m.placa_tft.celula)
    assert m.placa_tft.celula["trava_slot_id"] is None


def test_eventos_da_placa_das_telas_ficam_no_adapter(adapter_com_telas):
    """Telemetria e erro das telas: log e /health, nunca o central."""
    m = adapter_com_telas(mecanismos_serial=False)

    m.placa_tft.emitir(m.placa_tft.telemetria(telas_ok=7, brilho_pct=60))
    m.placa_tft.emitir(m.placa_tft.erro(slot=3))

    assert _ate(lambda: m.modulo._tft_estado["erros"] == 1)
    assert m.modulo._tft_estado["ultima_telemetria"]["telas_ok"] == 7
    assert m.modulo._tft_estado["ultimo_erro"]["dispenser_id"] == 3
    time.sleep(0.2)
    assert m.eventos_no_central == []

    saude = m.chamar(m.modulo.health())
    assert saude["telas"]["ultimo_erro"]["codigo_erro"] == "tela_sem_resposta"
    assert saude["serial_tft"]["subsistema"] == "dispenser_tft"
    assert saude["serial_tft"]["conectado"] is True


def test_health_publica_as_duas_portas_separadas_por_subsistema(adapter_com_telas):
    m = adapter_com_telas()
    saude = m.chamar(m.modulo.health())

    assert saude["serial"]["subsistema"] == "dispenser"
    assert saude["serial"]["url_aberta"] == m.placa_disp.url
    assert saude["serial_tft"]["subsistema"] == "dispenser_tft"
    assert saude["serial_tft"]["url_aberta"] == m.placa_tft.url
    assert saude["status"] == "ok"

    m.placa_tft.derrubar_cabo()
    assert _ate(lambda: not m.modulo._link_tft.conectado)
    degradado = m.chamar(m.modulo.health())
    assert degradado["checks"]["placa-dispenser-tft"] == "desconectada"
    assert degradado["checks"]["placa-dispenser"] == "ok"
    assert degradado["status"] == "degradado"


def test_as_duas_portas_sao_links_distintos_com_cmd_id_proprio(adapter_com_telas):
    m = adapter_com_telas()
    assert m.modulo._link is not m.modulo._link_tft
    assert m.modulo._link.subsistema == "dispenser"
    assert m.modulo._link_tft.subsistema == "dispenser_tft"

    m.chamar(m.modulo.cmd_estado_celula(m.modulo.EstadoCelulaReq(
        trava_ativa=False, trava_slot_id=None, os_id="", trava_resumo="")))
    m.chamar(m.modulo.cmd_limpar(m.modulo.ComandoLimparReq(dispenser_id=1, solicitado_por="t")))
    assert _ate(lambda: m.placa_disp.executados and m.comandos_de_tela("estado_celula"))
    # Cada porta conta do 1: o `cmd_id` é monotônico POR PORTA.
    assert m.placa_disp.executados[0]["cmd_id"] == 1
    assert m.comandos_de_tela("estado_celula")[0]["cmd_id"] == 1


def test_cmd_id_repetido_nao_reexecuta_na_placa_das_telas():
    """A placa falsa é o duplo do firmware: um `cmd_id` repetido responde ACK
    de novo SEM redesenhar — é a parte do protocolo que não dá para acrescentar
    depois sem trocar as duas pontas ao mesmo tempo."""
    placa = PlacaDispenserTFT()
    linha = json.dumps({"cmd": "estado_celula", "cmd_id": 7, "trava_ativa": True,
                        "trava_slot_id": 2, "os_id": "OS-1", "trava_resumo": "SKU errado"})
    placa._processar(linha)
    placa._processar(linha)
    assert len(placa.executados) == 1
    placa.parar()
