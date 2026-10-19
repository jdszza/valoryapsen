# -*- coding: utf-8 -*-
"""O porta-adapter — a porta NR-12 com USB própria, e nenhuma decisão.

A porta deixou de viajar pela placa das telas: ela tem USB própria no mini PC
e este adapter é o dono dela (`docs/PROTOCOLO_SERIAL.md` §9). Para ele a porta
é:

  1. uma ORIGEM de evento: tudo que a placa manda sobe CRU ao central, pela
     rota `/api/v1/eventos/porta`, na ordem em que veio;
  2. três comandos REST que viram `porta_*` na linha serial, com o ACK que a
     PORTA der (502 recusa, 503 inalcançável — o mesmo de todo adapter);
  3. um LINK — a USB da porta e a idade do heartbeat somadas num estado só, e
     só a TRANSIÇÃO sobe: `porta_link_perdido` uma vez, `porta_link_ok` só
     depois do primeiro `estado`.

E duas recusas que são contrato: sem `PORTA_SERIAL_URL` o serviço não sobe
(varrer portas competiria com as outras placas), e a placa errada na COM
configurada é "cabo trocado" — e a célula não anda, porque a placa errada não
emite o `estado` da porta.

Montado com a placa falsa da porta de verdade, por `socket://`, e o cliente
HTTP duplado no lugar do central. A lógica veio do dispenser-adapter sem mudar
de comportamento, e estes testes vieram com ela (`test_dispenser_adapter_porta.py`
deixou de existir).
"""
import ast
import asyncio
import re
import threading
import time
from pathlib import Path

import pytest

from fakes.placa_porta import PlacaPorta
from fakes.placa_weight import PlacaWeight
from test_serial_link import ClienteHTTPFake, _ate

pytest.importorskip("serial", reason="pyserial (tests/requirements-dev.txt)")

RAIZ_REPO = Path(__file__).resolve().parent.parent
MAIN = RAIZ_REPO / "porta-adapter" / "main.py"
UID = "04A1B2C3"


class Montagem:
    def __init__(self, modulo, porta, loop, cliente):
        self.modulo = modulo
        self.porta = porta
        self.loop = loop
        self.cliente = cliente

    def chamar(self, corotina, timeout: float = 5.0):
        return asyncio.run_coroutine_threadsafe(corotina, self.loop).result(timeout)

    def eventos(self) -> list:
        return [c["json"] for c in self.cliente.posts
                if c["url"].endswith("/api/v1/eventos/porta")]

    def tipos(self) -> list:
        return [e["tipo"] for e in self.eventos()]


@pytest.fixture
def montar(carregar_adapter):
    """porta-adapter + uma placa na COM configurada (por padrão, a da porta).

    Monta o que a `lifespan` montaria — o event loop numa thread, o `_client`
    duplado e o `LinkSerial` com `ao_receber_evento=_evento_da_placa` —, menos
    o laço da vigia: os testes chamam `_vigiar_porta_uma_vez()` quando querem,
    para não depender de relógio.
    """
    criados: list = []

    def _montar(placa=None, ack_timeout_s: float = 1.0):
        placa = placa or PlacaPorta(tags=(UID,), intervalo_ping=0.2,
                                    heartbeat_s=0.05).iniciar()
        modulo = carregar_adapter("porta", env={
            "PORTA_SERIAL_URL": placa.url,
            "PORTA_ACK_TIMEOUT_S": str(ack_timeout_s),
        })
        loop = asyncio.new_event_loop()
        pronto = threading.Event()

        def _rodar():
            asyncio.set_event_loop(loop)
            loop.call_soon(pronto.set)
            loop.run_forever()

        thread = threading.Thread(target=_rodar, name="loop-porta-adapter", daemon=True)
        thread.start()
        pronto.wait(5)

        cliente = ClienteHTTPFake()
        modulo._client = cliente
        modulo._loop = loop
        link = modulo.serial_link.LinkSerial(
            subsistema=modulo.SUBSISTEMA, url=modulo.SERIAL_URL,
            ack_timeout_s=modulo.ACK_TIMEOUT_S,
            ao_receber_evento=modulo._evento_da_placa,
            probe_assentar_s=0.0, probe_espera_s=1.0, reconexao_s=0.05,
        )
        modulo._link = link
        link.iniciar()
        assert _ate(lambda: link.conectado)
        m = Montagem(modulo, placa, loop, cliente)
        criados.append((m, link, thread))
        return m

    yield _montar

    for m, link, thread in criados:
        link.parar()
        m.porta.parar()
        m.loop.call_soon_threadsafe(m.loop.stop)
        thread.join(3)
        m.loop.close()


@pytest.fixture
def com_porta(montar):
    return montar()


# ── 1. Evento sobe, cru, na ordem ────────────────────────────────────────────

def test_o_adapter_responde_o_ping_da_porta(com_porta):
    """Quem responde o pong é este adapter — não mais a placa das telas."""
    assert _ate(lambda: com_porta.porta.pongs_recebidos > 0)
    assert com_porta.porta.sessao_adapter == com_porta.modulo.serial_link.SESSAO


def test_o_primeiro_estado_sobe_depois_do_link_ok(com_porta):
    assert _ate(lambda: "estado" in com_porta.tipos())
    tipos = com_porta.tipos()
    assert tipos.index("porta_link_ok") < tipos.index("estado")


def test_evento_da_porta_sobe_cru_na_rota_da_porta(com_porta):
    m = com_porta
    assert _ate(lambda: "estado" in m.tipos())
    enviados = m.porta.passar_tag(UID)
    assert _ate(lambda: "pedido_acesso" in m.tipos())
    pedido = next(e for e in m.eventos() if e["tipo"] == "pedido_acesso")
    assert pedido == enviados[0], "o conteúdo não subiu como veio"
    assert pedido["uid"] == UID and pedido["origem"] == "porta"
    # Uma rota só: a do central para a porta.
    assert {c["url"] for c in m.cliente.posts} == {m.modulo.CENTRAL_URL + "/api/v1/eventos/porta"}


def test_os_eventos_sobem_na_ordem_em_que_a_placa_os_mandou(com_porta):
    """Abrir e fechar em rajada: o central tem de ver `porta_aberta` antes de
    `porta_fechada`, e cada `estado` no lugar dele — é a ordem que decide se a
    célula para e se retoma."""
    m = com_porta
    assert _ate(lambda: "estado" in m.tipos())
    esperado = []
    for evento in m.porta.abrir("frente") + m.porta.fechar("frente"):
        if evento["tipo"] != "estado":
            esperado.append(evento["tipo"])
    assert _ate(lambda: "porta_fechada" in m.tipos())
    sequencia = [t for t in m.tipos() if t in ("porta_aberta", "porta_fechada")]
    assert sequencia == esperado == ["porta_aberta", "porta_fechada"]


def test_heartbeat_repetido_nao_sobe_a_cada_500ms(com_porta):
    """Vinte heartbeats idênticos são UM `estado` no central — o resto é
    transporte, e o central não decide nada com a repetição."""
    assert _ate(lambda: "estado" in com_porta.tipos())
    time.sleep(0.6)            # ~12 heartbeats a 50 ms
    assert com_porta.tipos().count("estado") == 1


def test_estado_que_muda_sobe_na_hora(com_porta):
    assert _ate(lambda: "estado" in com_porta.tipos())
    com_porta.porta.abrir("frente")
    assert _ate(lambda: "porta_aberta" in com_porta.tipos())
    assert _ate(lambda: com_porta.tipos().count("estado") >= 2)


def test_link_que_a_placa_mandasse_nao_sobe(com_porta):
    """O link é decidido aqui. Um `porta_link_*` vindo da placa — firmware
    antigo — duplicaria a transição no central."""
    m = com_porta
    assert _ate(lambda: "estado" in m.tipos())
    m.porta.emitir({"origem": "porta", "tipo": "porta_link_perdido", "ts": "x"})
    m.porta.passar_tag(UID)
    assert _ate(lambda: "pedido_acesso" in m.tipos())
    assert "porta_link_perdido" not in m.tipos()


def test_evento_sem_tipo_e_descartado_sem_derrubar_a_ponte(com_porta):
    m = com_porta
    assert _ate(lambda: "estado" in m.tipos())
    m.porta.emitir({"sem_tipo": 1})
    m.porta.passar_tag(UID)
    assert _ate(lambda: "pedido_acesso" in m.tipos())


# ── 2. Comando desce com o ACK da porta ──────────────────────────────────────

def test_liberar_desce_e_volta_com_o_ack_da_porta(com_porta):
    m = com_porta
    m.porta.passar_tag(UID)
    resposta = m.chamar(m.modulo.cmd_porta_liberar(m.modulo.PortaLiberarReq(janela_ms=5000)))
    assert resposta["ok"] is True and resposta["porta"]["resp"] == "ok"
    assert m.porta.executados[-1] == {**m.porta.executados[-1],
                                      "cmd": "porta_liberar", "janela_ms": 5000}
    assert _ate(lambda: "liberada" in m.tipos())


def test_liberar_sem_pedido_e_502(com_porta):
    from fastapi import HTTPException
    m = com_porta
    with pytest.raises(HTTPException) as erro:
        m.chamar(m.modulo.cmd_porta_liberar(m.modulo.PortaLiberarReq(janela_ms=5000)))
    assert erro.value.status_code == 502


def test_placa_muda_e_503(montar):
    from fastapi import HTTPException
    m = montar(ack_timeout_s=0.3)
    m.porta.mudo = True
    with pytest.raises(HTTPException) as erro:
        m.chamar(m.modulo.cmd_porta_estado())
    assert erro.value.status_code == 503


def test_sem_link_iniciado_e_503(carregar_adapter):
    """O processo de pé sem a porta não finge ACK: o central autorizaria uma
    porta imaginária."""
    from fastapi import HTTPException
    modulo = carregar_adapter("porta", env={"PORTA_SERIAL_URL": "COM_QUALQUER"})
    with pytest.raises(HTTPException) as erro:
        asyncio.run(modulo.cmd_porta_estado())
    assert erro.value.status_code == 503


def test_negar_e_estado_descem(com_porta):
    m = com_porta
    m.porta.passar_tag(UID)
    m.chamar(m.modulo.cmd_porta_negar(m.modulo.PortaNegarReq(motivo="maquina_nao_confirmou_parada")))
    m.chamar(m.modulo.cmd_porta_estado())
    assert [e["cmd"] for e in m.porta.executados][-2:] == ["porta_negar", "porta_estado"]
    assert m.porta.executados[-2]["motivo"] == "maquina_nao_confirmou_parada"
    assert _ate(lambda: "liberacao_expirada" in m.tipos())


def test_janela_fora_da_faixa_e_422_na_borda(carregar_adapter):
    from pydantic import ValidationError
    modulo = carregar_adapter("porta")
    with pytest.raises(ValidationError):
        modulo.PortaLiberarReq(janela_ms=10)


# ── 3. O link: uma queda, uma linha; volta só com `estado` ───────────────────

class _LinkCaido:
    conectado = False


def test_usb_cai_vira_link_perdido_uma_vez_e_volta_so_com_estado(com_porta, monkeypatch):
    m = com_porta
    assert _ate(lambda: "estado" in m.tipos())
    real = m.modulo._link

    monkeypatch.setattr(m.modulo, "_link", _LinkCaido())
    m.chamar(m.modulo._vigiar_porta_uma_vez())
    m.chamar(m.modulo._vigiar_porta_uma_vez())        # a segunda rodada não repete
    assert m.tipos().count("porta_link_perdido") == 1

    antes_pedidos = len([e for e in m.porta.executados if e["cmd"] == "porta_estado"])
    monkeypatch.setattr(m.modulo, "_link", real)
    m.chamar(m.modulo._vigiar_porta_uma_vez())
    # Ao reabrir, o adapter PEDE um `estado` em vez de esperar o heartbeat.
    assert len([e for e in m.porta.executados if e["cmd"] == "porta_estado"]) == antes_pedidos + 1
    assert _ate(lambda: m.tipos().count("porta_link_ok") == 2)
    tipos = m.tipos()
    ultimo_ok = len(tipos) - 1 - tipos[::-1].index("porta_link_ok")
    assert "estado" in tipos[ultimo_ok:], "o link voltou sem um `estado` depois"


def test_cabo_puxado_de_verdade_vira_link_perdido(com_porta):
    """O mesmo, com a conexão REALMENTE fechada (a USB caindo): o
    `serial_link` vê a porta sumir, e a vigia transforma isso em UMA linha."""
    m = com_porta
    assert _ate(lambda: "estado" in m.tipos())
    m.porta.heartbeat_s = 3600          # depois da volta, só o `porta_estado` fala
    m.porta.derrubar_cabo()
    assert _ate(lambda: not m.modulo._link.conectado, esperando="o link notar a queda")
    m.chamar(m.modulo._vigiar_porta_uma_vez())
    assert m.tipos().count("porta_link_perdido") == 1
    assert _ate(lambda: m.modulo._link.conectado, esperando="o link reconectar")
    m.chamar(m.modulo._vigiar_porta_uma_vez())
    assert _ate(lambda: m.tipos().count("porta_link_ok") == 2)


def test_heartbeat_velho_vira_UM_link_perdido(com_porta):
    """A USB de pé e a placa muda: só a idade do heartbeat denuncia."""
    m = com_porta
    assert _ate(lambda: "estado" in m.tipos())
    m.modulo.PORTA_HEARTBEAT_MAX_S = 0.2
    m.porta.silenciar()
    time.sleep(0.4)
    m.chamar(m.modulo._vigiar_porta_uma_vez())
    m.chamar(m.modulo._vigiar_porta_uma_vez())
    assert m.tipos().count("porta_link_perdido") == 1

    m.porta.falar()
    assert _ate(lambda: m.tipos().count("porta_link_ok") == 2)


def test_health_publica_a_porta(com_porta):
    m = com_porta
    assert _ate(lambda: "estado" in m.tipos())
    corpo = m.chamar(m.modulo.health())
    porta = corpo["porta"]
    assert porta["link_ok"] is True and porta["usb_conectada"] in (True, None)
    assert porta["idade_heartbeat_s"] is not None and porta["idade_heartbeat_s"] < 2
    assert porta["ultimo_estado"]["estado"] == "FECHADA"
    assert corpo["serial"]["subsistema"] == "porta" and corpo["serial"]["conectado"]
    assert corpo["checks"]["placa-porta"] == "ok"


# ── 4. As duas recusas que são contrato ──────────────────────────────────────

def test_sem_PORTA_SERIAL_URL_o_servico_nao_sobe(carregar_adapter, monkeypatch):
    """A regra do README: cada placa tem a COM fixa, nenhuma é descoberta por
    varredura — varrer abriria as portas das outras quatro placas."""
    monkeypatch.delenv("PORTA_SERIAL_URL", raising=False)
    modulo = carregar_adapter("porta")
    assert modulo.SERIAL_URL == ""

    async def _subir():
        async with modulo.lifespan(modulo.app):
            pass

    with pytest.raises(RuntimeError, match="PORTA_SERIAL_URL"):
        asyncio.run(_subir())
    assert modulo._link is None, "um LinkSerial nasceu mesmo com a recusa"


def test_placa_errada_na_COM_e_cabo_trocado_e_a_celula_nao_anda(montar, caplog):
    """A balança ligada na COM da porta: o `serial_link` acusa "cabo trocado",
    e o link NUNCA é declarado ok — a balança não emite o `estado` da porta —,
    então a vigia o declara perdido. Link perdido é porta aberta."""
    balanca = PlacaWeight(intervalo_ping=0.1).iniciar()
    with caplog.at_level("ERROR"):
        m = montar(placa=balanca)
        assert _ate(lambda: any("cabo trocado" in r.getMessage() for r in caplog.records))
    aviso = next(r for r in caplog.records if "cabo trocado" in r.getMessage())
    assert "'weight'" in aviso.getMessage()

    m.modulo.PORTA_HEARTBEAT_MAX_S = 0.1
    time.sleep(0.3)
    m.chamar(m.modulo._vigiar_porta_uma_vez())
    assert "porta_link_ok" not in m.tipos()
    assert m.tipos() == ["porta_link_perdido"]


# ── 5. Serial NUNCA no event loop (AST) ──────────────────────────────────────
#
# A mesma varredura que `test_serial_link.py` faz nos adapters de transporte
# selecionável: `Serial.read()`/`write()` são bloqueantes, e uma leitura
# pendurada congelaria o adapter inteiro, inclusive o /ping.

METODOS_BLOQUEANTES = {"enviar_comando", "write", "flush", "read", "readline",
                       "read_until"}


def test_nenhuma_chamada_serial_bloqueante_dentro_de_async_def():
    arvore = ast.parse(MAIN.read_text(encoding="utf-8"))
    infracoes = []
    for no in ast.walk(arvore):
        if not isinstance(no, ast.AsyncFunctionDef):
            continue
        for interno in ast.walk(no):
            if (isinstance(interno, ast.Call) and isinstance(interno.func, ast.Attribute)
                    and interno.func.attr in METODOS_BLOQUEANTES):
                infracoes.append(f"{no.name}: {ast.unparse(interno.func)}()")
    assert not infracoes, infracoes


def test_o_envio_serial_passa_por_to_thread():
    """Guarda da guarda: o teste acima também passa se a chamada SUMIR."""
    fonte = MAIN.read_text(encoding="utf-8")
    assert len(re.findall(r"asyncio\.to_thread\(\s*_link\.enviar_comando", fonte)) >= 2


def test_o_adapter_so_tem_transporte_serial():
    """Não há simulador de porta. Um `PORTA_TRANSPORTE=http` que aparecesse
    teria de ACKar alguma coisa — e o central autorizaria uma porta
    imaginária."""
    fonte = MAIN.read_text(encoding="utf-8")
    assert "PORTA_TRANSPORTE" not in fonte and "_post_sim" not in fonte


def test_o_host_bat_exige_a_COM_e_sobe_na_8104():
    bat = (RAIZ_REPO / "porta-adapter" / "iniciar_host.bat").read_text(encoding="utf-8")
    assert "PORTA_SERIAL_URL" in bat
    assert re.search(r'if "%PORTA_SERIAL_URL%"=="" \(\s*\r?\n', bat), (
        "o .bat tem de SAIR quando a COM não foi definida")
    assert "exit /b 1" in bat
    codigo = "\n".join(l for l in bat.splitlines() if not l.strip().lower().startswith("rem"))
    assert not re.search(r"set PORTA_SERIAL_URL=COM\d", codigo), "COM chutada como default"
    assert "set PORTA_HTTP=8104" in bat
    assert "set CENTRAL_URL=http://localhost:8000" in bat
    req = (RAIZ_REPO / "porta-adapter" / "requirements.txt").read_text(encoding="utf-8")
    assert "pyserial" in req
