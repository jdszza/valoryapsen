# -*- coding: utf-8 -*-
"""O roteiro de bancada da porta NR-12 em campo, ponta a ponta (§10).

As três peças são as de verdade, menos o fio e o hardware da célula:

    PlacaReceptorPorta (socket://) ──LeitorLinhas──> porta-adapter ──> central (orquestrador)

  * a placa é o receptor falso, encenando a máquina de estados do
    `nr12_oficial.ino` e escrevendo as linhas de TEXTO dele;
  * o porta-adapter é o `porta-adapter/main.py` de verdade, com o
    `LeitorLinhas` de verdade — só o `_client` (o POST ao central) é trocado
    por uma chamada direta a `orchestrator.evento_porta`, o mesmo caminho do
    handler HTTP;
  * o central é o orquestrador de verdade, com CNC e dispensers no
    `AdapterFake` da suíte. O que ele mandar a `PORTA_ADAPTER_URL` é entregue
    aos endpoints do porta-adapter — e o roteiro cobra que não mande
    `liberar` nem `negar`: a porta em campo autoriza sozinha.

O que o roteiro prova é a costura: um evento que o adapter perdesse,
reordenasse ou traduzisse errado aparece aqui como célula que não retoma ou OS
que não conclui.
"""
import asyncio
import time

import pytest
from fastapi import HTTPException

from fakes.placa_receptor_porta import PlacaReceptorPorta
from test_orchestrator import _item, _payload_os, _status_gravados

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
    """Central com porta habilitada + porta-adapter + receptor falso."""
    orq = carregar_orquestrador(env={"PORTA_NR12_HABILITADA": "1"})
    placa = PlacaReceptorPorta(tags=(UID,)).iniciar()
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
    comandos_porta: list[str] = []

    async def _post(url, payload, timeout=10.0):
        if not url.startswith(base):
            return await post_cnc_e_dispensers(url, payload, timeout)
        nome = url[len(base):]
        comandos_porta.append(nome)
        try:
            await {"liberar": porta.cmd_porta_liberar, "negar": porta.cmd_porta_negar,
                   "estado": porta.cmd_porta_estado}[nome]()
        except HTTPException:
            return False
        return True
    monkeypatch.setattr(orq.modulo, "_post", _post)

    leitores: list = []

    def ligar_usb():
        leitor = porta.leitor_linhas.LeitorLinhas(porta.SERIAL_URL, porta._linha_da_usb,
                                                  reconexao_s=0.05)
        porta._leitor = leitor
        leitor.iniciar()
        leitores.append(leitor)
        return leitor

    class C:
        pass
    c = C()
    c.orq, c.placa, c.porta, c.central, c.ligar_usb = orq, placa, porta, central, ligar_usb
    c.comandos_porta = comandos_porta
    yield c
    for leitor in leitores:
        leitor.parar()
    placa.parar()
    assert placa.bytes_recebidos == 0, "o adapter escreveu na USB do receptor"


async def _quando(condicao, limite_s: float = 10.0):
    # O prazo é de RELÓGIO, não uma contagem de voltas: no Windows a resolução
    # do relógio do asyncio é ~15,6 ms, e mil voltas de `sleep(0.01)` eram
    # ~30 ms em vez de 10 s.
    prazo = time.monotonic() + limite_s
    while time.monotonic() < prazo:
        if condicao():
            return True
        await asyncio.sleep(0.01)
    if condicao():
        return True
    raise AssertionError("a condição não aconteceu")


async def _subir_o_adapter(c):
    """O adapter sobe com o estado DESCONHECIDO: a USB abre e nada sobe."""
    c.porta._loop = asyncio.get_running_loop()
    assert c.orq.modulo.get_pausa_porta()["motivo"] == "link_perdido", (
        "com a porta habilitada, a célula nasce parada")
    leitor = c.ligar_usb()
    await _quando(lambda: leitor.conectado and c.placa.conectada)
    await c.porta._vigiar_porta_uma_vez()
    assert c.central.eventos == [], "subiu evento de uma porta que ninguém conhece"
    return leitor


async def _ligar_a_porta(c):
    """BOOT_OK prova o estado (as duas fechadas, sem rearme): a célula retoma
    pela regra de sempre do link perdido sem porta aberta no meio."""
    leitor = await _subir_o_adapter(c)
    c.placa.ligar_porta()
    await _quando(lambda: "estado" in c.central.tipos())
    assert c.central.tipos()[:2] == ["porta_link_ok", "boot"]
    await _quando(lambda: c.orq.modulo.get_pausa_porta() is None)
    await c.orq.modulo.aguardar_tarefas_porta()
    return leitor


async def _ritual_de_subida(c, homings_antes: int):
    """tag (PAUSA) → 10 s (LIBERACAO_EXPIRADA) → tag (REARME_OK) → homing."""
    orq = c.orq
    c.placa.passar_tag(UID)
    await _quando(lambda: "liberada" in c.central.tipos())
    await asyncio.sleep(0.2)
    assert orq.modulo.get_pausa_porta() is not None, "retomou só com o pedido de acesso"
    assert c.placa.led_pode_abrir, "a porta autoriza sozinha"
    c.placa.expirar()
    await _quando(lambda: "liberacao_expirada" in c.central.tipos())
    await asyncio.sleep(0.2)
    assert orq.modulo.get_pausa_porta() is not None, "retomou sem o rearme"
    assert orq.estado["porta"]["faixa"] == "PORTAS FECHADAS — PASSE A TAG PARA RETOMAR"
    c.placa.passar_tag(UID)
    await _quando(lambda: orq.modulo.get_pausa_porta() is None)
    await orq.modulo.aguardar_tarefas_porta()
    assert len(orq.adapter.comandos("/comandos/homing")) > homings_antes


def test_subida_estado_desconhecido_tag_expira_tag_e_a_celula_fica_livre(celula):
    c = celula

    async def _cenario():
        await _subir_o_adapter(c)
        await _ritual_de_subida(c, homings_antes=0)

    asyncio.run(_cenario())
    tipos = [t for t in c.central.tipos() if t != "estado"]
    assert tipos == ["porta_link_ok", "pedido_acesso", "liberada",
                     "liberacao_expirada", "rearme"], tipos
    pedido = next(e for e in c.central.eventos if e["tipo"] == "pedido_acesso")
    assert pedido["autorizada_pela_porta"] is True
    assert c.comandos_porta == [], "o central mandou comando a uma porta autônoma"


def test_tag_no_meio_da_dispensa_para_abre_fecha_rearma_e_a_os_conclui(celula):
    c = celula
    orq = c.orq
    orq.adapter.segurar_dispensas = 1
    orq.adapter.parcial_confirmado = 3

    async def _cenario():
        await _ligar_a_porta(c)

        async def _tag_no_meio_da_dispensa(_payload):
            if len(orq.adapter.comandos("/comandos/dispensar")) == 1:
                c.placa.passar_tag(UID)          # o operador passa a tag
                # A dispensa está FISICAMENTE em curso enquanto a linha
                # atravessa porta → receptor → USB → adapter → central. O
                # cronograma da suíte é instantâneo, então o gancho segura o
                # `dispensar` até a pausa estar armada.
                await _quando(lambda: orq.modulo.get_pausa_porta() is not None)
        orq.adapter.ao_comando["/comandos/dispensar"] = _tag_no_meio_da_dispensa

        os_task = asyncio.create_task(orq.modulo._processar_os(
            _payload_os("OS-ROT-CAMPO", _item("Dipirona", 5), _item("Ibuprofeno", 4))))

        # A porta já autorizou sozinha; o central PARA.
        await _quando(lambda: orq.adapter.comandos("/comandos/parar"))
        assert c.placa.led_pode_abrir
        c.placa.abrir("frente")
        await _quando(lambda: orq.modulo._porta.get("frente") == "aberta")
        assert orq.estado["porta"]["faixa"] == "MÁQUINA PARADA — PORTA FRENTE ABERTA"
        c.placa.fechar("frente")
        await _quando(lambda: "porta_fechada" in c.central.tipos())
        await _quando(lambda: orq.estado["porta"]["faixa"]
                      == "PORTAS FECHADAS — PASSE A TAG PARA RETOMAR")
        assert not os_task.done(), "fechar a porta religou a célula"
        homings_antes = len(orq.adapter.comandos("/comandos/homing"))
        c.placa.passar_tag(UID)                  # rearme → homing → o resto
        await asyncio.wait_for(os_task, 15)
        await orq.modulo.aguardar_tarefas_porta()
        assert len(orq.adapter.comandos("/comandos/homing")) > homings_antes

    asyncio.run(_cenario())

    tipos = [t for t in c.central.tipos() if t != "estado"]
    assert tipos == ["porta_link_ok", "boot", "pedido_acesso", "liberada", "porta_aberta",
                     "porta_fechada", "rearme"], tipos
    aberta = next(e for e in c.central.eventos if e["tipo"] == "porta_aberta")
    assert aberta["autorizada"] is True and aberta["porta"] == "frente"
    assert c.comandos_porta == [], "POST a /comandos/porta/liberar|negar"
    dispensas = orq.adapter.comandos("/comandos/dispensar")
    assert dispensas[1]["quantidade"] == 2, "a retomada pede só o que faltou"
    assert _status_gravados(orq)[-1] == ("OS-ROT-CAMPO", "concluida")
    alarmes = [ch["args"][1] for ch in orq.banco.chamadas_de("salvar_alarme")]
    assert "porta_aberta_sem_autorizacao" not in alarmes
    assert "maquina_nao_confirmou_parada" not in alarmes
    assert orq.modulo.get_pausa_porta() is None


@pytest.mark.parametrize("aberta_na_queda", [False, True],
                         ids=["nada_aberto_na_queda", "porta_aberta_na_queda"])
def test_usb_do_receptor_cai_no_meio_e_a_celula_para(celula, aberta_na_queda):
    """USB do receptor desligada no meio da dispensa → `porta_link_perdido` →
    a célula para. Religada → NADA sobe: o estado da porta é desconhecido, e a
    célula continua parada até uma linha que o prove.

    Se ninguém mexeu na porta durante a queda, a prova é o ritual de subida
    (tag → 10 s → tag). Se alguém abriu e fechou, a porta ficou em rearme
    pendente — e a primeira tag já é o REARME_OK.
    """
    c = celula
    orq = c.orq
    orq.adapter.segurar_dispensas = 1
    orq.adapter.parcial_confirmado = 2

    async def _cenario():
        leitor = await _ligar_a_porta(c)

        async def _usb_cai_no_meio(_payload):
            if len(orq.adapter.comandos("/comandos/dispensar")) == 1:
                await asyncio.to_thread(leitor.parar)
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
            # Com a USB fora, alguém abre e fecha a frente. As linhas se
            # perdem — mas a porta lembra (rearme pendente).
            c.placa.abrir("frente")
            c.placa.fechar("frente")
            assert c.placa.aguardando_rearme

        antes = len(c.central.eventos)
        novo = c.ligar_usb()
        await _quando(lambda: novo.conectado and c.placa.conectada)
        c.porta.PORTA_ESTADO_REENVIO_S = 0.0
        await c.porta._vigiar_porta_uma_vez()
        await asyncio.sleep(0.3)
        assert c.central.eventos[antes:] == [], "subiu evento sem a porta provar o estado"
        assert orq.modulo.get_pausa_porta() is not None
        assert not os_task.done()

        if aberta_na_queda:
            c.placa.passar_tag(UID)              # já é o REARME_OK
            await _quando(lambda: "rearme" in c.central.tipos()[antes:])
        else:
            await _ritual_de_subida(c, len(orq.adapter.comandos("/comandos/homing")))
        await asyncio.wait_for(os_task, 15)
        await orq.modulo.aguardar_tarefas_porta()
        depois = c.central.tipos()[antes:]
        assert depois[0] == "porta_link_ok"

    asyncio.run(_cenario())
    assert _status_gravados(orq)[-1] == ("OS-USB", "concluida")
    assert orq.modulo.get_pausa_porta() is None
    alarmes = [ch["args"][1] for ch in orq.banco.chamadas_de("salvar_alarme")]
    assert alarmes.count("porta_link_perdido") == 1
    assert c.comandos_porta == []
