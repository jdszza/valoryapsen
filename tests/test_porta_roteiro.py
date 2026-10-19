# -*- coding: utf-8 -*-
"""O roteiro de bancada da porta NR-12, ponta a ponta, com a USB própria.

As três peças são as de verdade, menos o fio e o hardware da célula:

    PlacaPorta (socket://) ──serial_link──> porta-adapter ──> central (orquestrador)
                           <──────────────               <── /comandos/porta/*

  * a placa é a `PlacaPorta` falsa, servindo o contrato do §9 pela porta dela;
  * o porta-adapter é o `porta-adapter/main.py` de verdade, com o `LinkSerial`
    de verdade — só o `_client` (o POST ao central) é trocado por uma chamada
    direta a `orchestrator.evento_porta`, o mesmo caminho do handler HTTP;
  * o central é o orquestrador de verdade, com CNC e dispensers no
    `AdapterFake` da suíte — e os três `/comandos/porta/*` que ele manda saem
    por `PORTA_ADAPTER_URL` e são entregues aos endpoints do porta-adapter.

O que o roteiro prova é a costura: nenhuma peça foi encenada à mão no meio do
caminho, então um evento que o adapter perdesse, reordenasse ou mandasse para
o lugar errado aparece aqui como OS que não conclui.
"""
import asyncio

import pytest
from fastapi import HTTPException

from fakes.placa_porta import PlacaPorta
from test_orchestrator import _item, _payload_os, _rotas, _status_gravados

pytest.importorskip("serial", reason="pyserial (tests/requirements-dev.txt)")

UID = "04A1B2C3"


class _CentralDireto:
    """O `_client` do porta-adapter: o POST vira `evento_porta` no central."""

    class _Resposta:
        status_code = 200

    def __init__(self, orq):
        self.orq = orq
        self.eventos: list[dict] = []

    async def post(self, url, json=None, timeout=None):
        assert url.endswith("/api/v1/eventos/porta"), url
        self.eventos.append(json)
        await self.orq.modulo.evento_porta(json)
        return self._Resposta()

    def tipos(self) -> list:
        return [e["tipo"] for e in self.eventos]


@pytest.fixture
def celula(carregar_orquestrador, carregar_adapter, monkeypatch):
    """Central com porta habilitada + porta-adapter + placa da porta."""
    orq = carregar_orquestrador(env={"PORTA_NR12_HABILITADA": "1"})
    placa = PlacaPorta(tags=(UID,), intervalo_ping=0.2, heartbeat_s=0.05).iniciar()
    sleep_real = asyncio.sleep
    porta = carregar_adapter("porta", env={"PORTA_SERIAL_URL": placa.url})
    # `carregar_adapter` troca `asyncio.sleep` por um no-op para o retry do
    # evento não dormir — e o atributo é do módulo `asyncio`, ou seja, de todo
    # mundo. Aqui o central e as esperas do roteiro precisam do relógio de
    # verdade; o retry nunca dorme, porque o "central" abaixo responde 200.
    monkeypatch.setattr(asyncio, "sleep", sleep_real)
    central = _CentralDireto(orq)
    porta._client = central

    base = orq.modulo.settings.PORTA_ADAPTER_URL + "/comandos/porta/"
    post_cnc_e_dispensers = orq.modulo._post

    async def _post(url, payload, timeout=10.0):
        if not url.startswith(base):
            return await post_cnc_e_dispensers(url, payload, timeout)
        orq.adapter.chamadas.append({"url": url, "payload": payload})
        nome = url[len(base):]
        try:
            if nome == "liberar":
                await porta.cmd_porta_liberar(porta.PortaLiberarReq(**payload))
            elif nome == "negar":
                await porta.cmd_porta_negar(porta.PortaNegarReq(**payload))
            else:
                await porta.cmd_porta_estado()
        except HTTPException:
            return False
        return True
    monkeypatch.setattr(orq.modulo, "_post", _post)

    links: list = []

    def ligar_usb():
        link = porta.serial_link.LinkSerial(
            subsistema=porta.SUBSISTEMA, url=porta.SERIAL_URL,
            ack_timeout_s=1.0, ao_receber_evento=porta._evento_da_placa,
            probe_assentar_s=0.0, probe_espera_s=1.0, reconexao_s=0.05)
        porta._link = link
        link.iniciar()
        links.append(link)
        return link

    class C:
        pass
    c = C()
    c.orq, c.placa, c.porta, c.central, c.ligar_usb = orq, placa, porta, central, ligar_usb
    yield c
    for link in links:
        link.parar()
    placa.parar()


async def _quando(condicao, limite_s: float = 10.0):
    for _ in range(int(limite_s / 0.01)):
        if condicao():
            return True
        await asyncio.sleep(0.01)
    raise AssertionError("a condição não aconteceu")


async def _subir_o_adapter(c):
    """porta-adapter sobe → ping/pong → `estado` → link_ok no central."""
    c.porta._loop = asyncio.get_running_loop()
    assert c.orq.modulo.get_pausa_porta()["motivo"] == "link_perdido", (
        "com a porta habilitada, a célula nasce parada")
    link = c.ligar_usb()
    await _quando(lambda: link.conectado)
    await _quando(lambda: c.placa.pongs_recebidos > 0)
    await _quando(lambda: "estado" in c.central.tipos())
    assert c.central.tipos().index("porta_link_ok") < c.central.tipos().index("estado")
    # Porta fechada, nada aberto no meio: a célula retoma sozinha.
    await _quando(lambda: c.orq.modulo.get_pausa_porta() is None)
    await c.orq.modulo.aguardar_tarefas_porta()
    return link


def test_roteiro_pedido_liberar_abrir_fechar_rearme_ate_a_os_concluida(celula):
    c = celula
    orq = c.orq
    orq.adapter.segurar_dispensas = 1
    orq.adapter.parcial_confirmado = 3

    async def _cenario():
        await _subir_o_adapter(c)

        async def _tag_no_meio_da_dispensa(_payload):
            if len(orq.adapter.comandos("/comandos/dispensar")) == 1:
                c.placa.passar_tag(UID)          # o operador passa a tag
                # A dispensa está FISICAMENTE em curso enquanto o pedido
                # atravessa placa → USB → adapter → central. O cronograma da
                # suíte é instantâneo, então o gancho segura o `dispensar` até
                # a pausa estar armada — o que a mecânica faz de graça.
                await _quando(lambda: orq.modulo.get_pausa_porta() is not None)
        orq.adapter.ao_comando["/comandos/dispensar"] = _tag_no_meio_da_dispensa

        os_task = asyncio.create_task(orq.modulo._processar_os(
            _payload_os("OS-ROT-USB", _item("Dipirona", 5), _item("Ibuprofeno", 4))))

        # pedido_acesso → o central para → porta_liberar pelo porta-adapter →
        # a placa acende o "pode abrir".
        await _quando(lambda: c.placa.estado_atual == "LIBERADA")
        assert c.placa.led_pode_abrir
        await _quando(lambda: "liberada" in c.central.tipos())
        # A pessoa abre a frente, com o LED aceso.
        c.placa.abrir("frente")
        await _quando(lambda: orq.modulo._porta.get("frente") == "aberta")
        assert orq.estado["porta"]["faixa"] == "MÁQUINA PARADA — PORTA FRENTE ABERTA"
        # Fecha: nada religa.
        c.placa.fechar("frente")
        await _quando(lambda: "porta_fechada" in c.central.tipos())
        await _quando(lambda: orq.estado["porta"]["faixa"]
                      == "PORTAS FECHADAS — PASSE A TAG PARA RETOMAR")
        homings_antes = len(orq.adapter.comandos("/comandos/homing"))
        # A tag do rearme → homing → o resto do slot → OS concluída.
        c.placa.passar_tag(UID)
        await asyncio.wait_for(os_task, 15)
        await orq.modulo.aguardar_tarefas_porta()
        assert len(orq.adapter.comandos("/comandos/homing")) > homings_antes

    asyncio.run(_cenario())

    # O que desceu à PLACA, pela USB dela: só a autorização.
    assert [e["cmd"] for e in c.placa.executados] == ["porta_liberar"]
    # O que subiu ao central, na ordem em que a porta mandou.
    tipos = [t for t in c.central.tipos() if t != "estado"]
    assert tipos == ["porta_link_ok", "pedido_acesso", "liberada", "porta_aberta",
                     "porta_fechada", "rearme"], tipos
    aberta = next(e for e in c.central.eventos if e["tipo"] == "porta_aberta")
    assert aberta["autorizada"] is True and aberta["porta"] == "frente"

    rotas = _rotas(orq)
    i_liberar = rotas.index("porta:/porta/liberar")
    assert rotas.index("cnc:/parar") < i_liberar and rotas.index("disp:/parar") < i_liberar
    assert not [r for r in rotas if r.startswith("disp:/porta")], (
        "comando de porta saiu pelo dispenser-adapter")
    dispensas = orq.adapter.comandos("/comandos/dispensar")
    assert dispensas[1]["quantidade"] == 2, "a retomada pede só o que faltou"
    assert _status_gravados(orq)[-1] == ("OS-ROT-USB", "concluida")
    alarmes = [ch["args"][1] for ch in orq.banco.chamadas_de("salvar_alarme")]
    assert "porta_aberta_sem_autorizacao" not in alarmes
    assert orq.modulo.get_pausa_porta() is None


@pytest.mark.parametrize("aberta_na_queda", [False, True],
                         ids=["nada_aberto_na_queda", "porta_aberta_na_queda"])
def test_usb_da_porta_cai_no_meio_e_a_celula_para(celula, aberta_na_queda):
    """USB desligada no meio da dispensa → `porta_link_perdido` → a célula para.

    Religada → `porta_estado` → `porta_link_ok` + `estado`. Daí a regra do
    central, que não mudou com o transporte: se a porta NÃO foi aberta durante
    a queda (o `estado` volta sem rearme pendente), não há o que rearmar e a
    OS retoma; se FOI aberta (a placa lembra, `aguardando_rearme`), a célula
    continua parada até a tag do rearme.
    """
    c = celula
    orq = c.orq
    orq.adapter.segurar_dispensas = 1
    orq.adapter.parcial_confirmado = 2

    async def _cenario():
        link = await _subir_o_adapter(c)

        async def _usb_cai_no_meio(_payload):
            if len(orq.adapter.comandos("/comandos/dispensar")) == 1:
                # O cabo sai com a dispensa em curso. A vigia do adapter vê a
                # USB cair e sobe UMA linha; o gancho segura o `dispensar` até
                # a pausa estar armada (o cronograma da suíte é instantâneo).
                await asyncio.to_thread(link.parar)
                await c.porta._vigiar_porta_uma_vez()
                await c.porta._vigiar_porta_uma_vez()
                await _quando(lambda: (orq.modulo.get_pausa_porta() or {}).get("motivo")
                              == "link_perdido")
        orq.adapter.ao_comando["/comandos/dispensar"] = _usb_cai_no_meio

        os_task = asyncio.create_task(orq.modulo._processar_os(
            _payload_os("OS-USB", _item("Dipirona", 5))))
        await _quando(lambda: orq.adapter.comandos("/comandos/parar"))
        assert c.central.tipos().count("porta_link_perdido") == 1
        assert orq.estado["porta"]["faixa"] == "LINK DA PORTA PERDIDO"

        if aberta_na_queda:
            # Com o cabo fora, alguém abre e fecha a frente. Nada chega ao
            # central — mas a placa lembra.
            c.placa.abrir("frente")
            c.placa.fechar("frente")
            assert c.placa.aguardando_rearme

        # O cabo volta: a vigia pede `porta_estado`, e o link volta com ele.
        antes = len(c.central.eventos)
        novo = c.ligar_usb()
        await _quando(lambda: novo.conectado)
        await c.porta._vigiar_porta_uma_vez()
        assert c.placa.executados[-1]["cmd"] == "porta_estado"
        await _quando(lambda: "porta_link_ok" in c.central.tipos()[antes:])
        depois = c.central.tipos()[antes:]
        assert depois[depois.index("porta_link_ok") + 1] == "estado"

        if aberta_na_queda:
            await asyncio.sleep(0.3)
            pausa = orq.modulo.get_pausa_porta()
            assert pausa is not None, "retomou sem a tag, com a porta aberta na queda"
            assert orq.estado["porta"]["faixa"] == "PORTAS FECHADAS — PASSE A TAG PARA RETOMAR"
            assert not os_task.done()
            c.placa.passar_tag(UID)             # rearme
        await asyncio.wait_for(os_task, 15)
        await orq.modulo.aguardar_tarefas_porta()

    asyncio.run(_cenario())
    assert _status_gravados(orq)[-1] == ("OS-USB", "concluida")
    assert orq.modulo.get_pausa_porta() is None
    alarmes = [ch["args"][1] for ch in orq.banco.chamadas_de("salvar_alarme")]
    assert alarmes.count("porta_link_perdido") == 1
