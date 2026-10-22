# -*- coding: utf-8 -*-
"""O porta-adapter em campo — só lê a USB do receptor e traduz (§10).

Desde 22/10/2026 a porta NR-12 são duas ESPs: a da porta (`nr12_oficial`), que
decide a tag sozinha e escreve TEXTO, e o receptor, que repassa as linhas na
USB. Para este adapter a porta é:

  1. uma ORIGEM de linhas, que viram os eventos do vocabulário de sempre
     (`porta_texto.py`) e sobem a `/api/v1/eventos/porta` na ordem;
  2. um LINK — a USB do receptor aberta E o estado da porta conhecido. O
     estado começa DESCONHECIDO e nada sobe até uma linha que o prove; com o
     link ok, o `estado` é sintetizado e reenviado a cada
     `PORTA_ESTADO_REENVIO_S`;
  3. **nada desce.** Zero bytes escritos na placa em TODOS os testes daqui, e
     `liberar`/`negar` respondem 409 em vez de um ACK falso.

Montado com o receptor falso de verdade, por `socket://`, e o cliente HTTP
duplado no lugar do central.
"""
import ast
import asyncio
import re
import threading
import time
from pathlib import Path

import pytest

from fakes.placa_receptor_porta import PlacaReceptorPorta
from test_serial_link import ClienteHTTPFake, _ate

pytest.importorskip("serial", reason="pyserial (tests/requirements-dev.txt)")

RAIZ_REPO = Path(__file__).resolve().parent.parent
PASTA = RAIZ_REPO / "porta-adapter"
MAIN = PASTA / "main.py"
LEITOR = PASTA / "leitor_linhas.py"
UID = "04A1B2C3"


class Montagem:
    def __init__(self, modulo, placa, loop, cliente, leitor):
        self.modulo = modulo
        self.placa = placa
        self.loop = loop
        self.cliente = cliente
        self.leitor = leitor
        self._linhas = 0

    def chamar(self, corotina, timeout: float = 5.0):
        return asyncio.run_coroutine_threadsafe(corotina, self.loop).result(timeout)

    def eventos(self) -> list:
        return [c["json"] for c in self.cliente.posts
                if c["url"].endswith("/api/v1/eventos/porta")]

    def tipos(self) -> list:
        return [e["tipo"] for e in self.eventos()]

    def vigiar(self):
        self.chamar(self.modulo._vigiar_porta_uma_vez())

    def drenar(self):
        """Espera o leitor ler tudo que a placa escreveu E o adapter tratar."""
        # Linha vazia (o banner do receptor tem duas) não chega ao callback.
        total = len([linha for linha in self.placa.linhas_enviadas if linha])
        assert _ate(lambda: self.leitor.estado()["linhas"] >= total,
                    esperando=f"o leitor ler {total} linhas")

        async def _vez():
            async with self.modulo._ordem():
                pass
        self.chamar(_vez())


@pytest.fixture
def montar(carregar_adapter):
    """porta-adapter + receptor falso, com o que a `lifespan` montaria — o
    event loop numa thread, o `_client` duplado e o `LeitorLinhas` —, menos o
    laço da vigia: os testes chamam `_vigiar_porta_uma_vez()` quando querem."""
    criados: list = []

    def _montar():
        placa = PlacaReceptorPorta(tags=(UID,)).iniciar()
        modulo = carregar_adapter("porta", env={"PORTA_SERIAL_URL": placa.url})
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
        leitor = modulo.leitor_linhas.LeitorLinhas(
            modulo.SERIAL_URL, modulo._linha_da_usb, reconexao_s=0.05)
        modulo._leitor = leitor
        leitor.iniciar()
        assert _ate(lambda: leitor.conectado and placa.conectada)
        m = Montagem(modulo, placa, loop, cliente, leitor)
        criados.append((m, thread))
        return m

    yield _montar

    for m, thread in criados:
        m.leitor.parar()
        m.placa.parar()
        m.loop.call_soon_threadsafe(m.loop.stop)
        thread.join(3)
        m.loop.close()
        # O contrato desta montagem inteira: o PC só LÊ.
        assert m.placa.bytes_recebidos == 0, "o adapter escreveu na USB do receptor"


@pytest.fixture
def com_porta(montar):
    return montar()


@pytest.fixture
def conhecida(com_porta):
    """Porta ligada (BOOT_OK): estado conhecido, link ok."""
    com_porta.placa.ligar_porta()
    com_porta.drenar()
    assert com_porta.tipos() == ["porta_link_ok", "boot", "estado"]
    com_porta.cliente.posts.clear()
    return com_porta


# ── 1. Estado desconhecido: nada sobe ────────────────────────────────────────

def test_nada_sobe_com_o_estado_desconhecido(com_porta):
    m = com_porta
    m.placa.ligar_receptor()
    m.placa.passar_tag("DEADBEEF")                     # TAG_NEGADA não prova nada
    m.placa.linha_crua("EVT:REG_INICIO | MODO REGISTRO")
    m.placa.linha_crua("   ES1(D4)=FECHADO  ES2(D18)=FECHADO  ")
    m.drenar()
    m.modulo.PORTA_ESTADO_REENVIO_S = 0.0
    m.vigiar()
    m.vigiar()
    assert m.tipos() == []
    assert m.modulo._porta["link_ok"] is None


def test_boot_ok_sobe_link_ok_antes_de_boot_e_estado(com_porta):
    m = com_porta
    m.placa.ligar_porta()
    m.drenar()
    assert m.tipos() == ["porta_link_ok", "boot", "estado"]
    estado = m.eventos()[-1]
    assert estado["fonte"] == "adapter" and estado["estado"] == "FECHADA"
    assert {c["url"] for c in m.cliente.posts} == {m.modulo.CENTRAL_URL + "/api/v1/eventos/porta"}


# ── 2. A tradução, pelo fio de verdade ───────────────────────────────────────

def test_pausa_sobe_pedido_autorizado_pela_porta_liberada_e_estado(com_porta):
    """A primeira tag também PROVA o estado: o link fica ok antes do pedido."""
    m = com_porta
    m.placa.passar_tag(UID)
    m.drenar()
    assert m.tipos() == ["porta_link_ok", "pedido_acesso", "liberada", "estado"]
    pedido = m.eventos()[1]
    assert pedido["uid"] == UID and pedido["autorizada_pela_porta"] is True
    assert m.eventos()[2]["janela_ms"] == 10000
    assert m.eventos()[3]["estado"] == "LIBERADA"


@pytest.mark.parametrize("porta", ["teto", "frente"])
def test_alerta_mais_linha_es_sobe_a_porta_certa(conhecida, porta):
    m = conhecida
    m.placa.abrir(porta)
    m.drenar()
    assert m.tipos() == ["porta_aberta", "estado"]
    aberta = m.eventos()[0]
    assert aberta["porta"] == porta and aberta["autorizada"] is False
    assert m.eventos()[1]["estado"] == "ABERTA_SEM_AUTORIZACAO"


def test_abrir_com_a_tag_e_autorizado(conhecida):
    m = conhecida
    m.placa.passar_tag(UID)
    m.placa.abrir("frente")
    m.drenar()
    aberta = next(e for e in m.eventos() if e["tipo"] == "porta_aberta")
    assert aberta["autorizada"] is True and aberta["porta"] == "frente"


def test_linha_es_atrasada_sobe_porta_none_no_prazo(conhecida):
    m = conhecida
    m.modulo.PORTA_ESPERA_SENSORES_S = 0.05
    m.placa.linha_crua("EVT:ALERTA_SEM_AUTORIZACAO | PORTA ABERTA SEM AUTORIZACAO!")
    m.drenar()
    m.vigiar()
    assert m.tipos() == [], "subiu antes do prazo da linha ES"
    time.sleep(0.1)
    m.vigiar()
    assert m.tipos() == ["porta_aberta", "estado"]
    assert m.eventos()[0]["porta"] is None


def test_rearme_sobe_o_estado_antes_do_rearme(conhecida):
    m = conhecida
    m.placa.passar_tag(UID)
    m.placa.expirar()
    m.drenar()
    m.cliente.posts.clear()
    m.placa.passar_tag(UID)
    m.drenar()
    assert m.tipos() == ["estado", "rearme"]
    assert m.eventos()[0]["estado"] == "FECHADA"
    assert m.eventos()[0]["aguardando_rearme"] is False


def test_fechar_entra_em_rearme(conhecida):
    m = conhecida
    m.placa.abrir("frente")
    m.placa.fechar("frente")
    m.drenar()
    assert m.tipos() == ["porta_aberta", "estado", "porta_fechada", "estado"]
    assert m.eventos()[-1]["aguardando_rearme"] is True


def test_reg_e_linhas_do_receptor_nao_sobem(conhecida):
    m = conhecida
    m.placa.ligar_receptor()
    for codigo in ("REG_INICIO", "REG_SALVA", "REG_FIM"):
        m.placa.linha_crua(f"EVT:{codigo};UID={UID} | bancada")
    m.placa.linha_crua(">> bytes=0  linhas=0  descartados=0  filtro=ON  NADA recebido ainda.")
    m.placa.linha_crua("algo que ninguem sabe o que e")
    m.drenar()
    assert m.tipos() == []
    porta = m.chamar(m.modulo.health())["porta"]
    assert porta["ultimo_codigo_bancada"] == "REG_FIM"
    assert porta["linhas_desconhecidas"] == 1


def test_lixo_antes_do_evt_e_crlf_sao_tolerados(conhecida):
    m = conhecida
    m.placa.linha_crua("~#EVT:TAG_NEGADA;UID=deadbeef | Tag nao cadastrada.")
    m.drenar()
    assert m.eventos() and m.eventos()[0]["uid"] == "DEADBEEF"


def test_linha_acima_do_teto_e_descartada_inteira_e_a_seguinte_sobrevive(conhecida):
    m = conhecida
    m.placa.linha_crua("EVT:TAG_NEGADA;UID=AA | " + "x" * 2000)
    m.placa.passar_tag("BB")
    # Sem `drenar()`: a linha longa nunca é contada como lida — é o ponto.
    assert _ate(lambda: "tag_negada" in m.tipos())
    assert [e["uid"] for e in m.eventos()] == ["BB"]
    assert m.leitor.estado()["descartadas"] == 1


# ── 3. O `estado` sintetizado ────────────────────────────────────────────────

def test_estado_e_reenviado_com_o_link_ok_e_nunca_com_o_estado_desconhecido(com_porta):
    m = com_porta
    m.modulo.PORTA_ESTADO_REENVIO_S = 0.1
    for _ in range(3):
        time.sleep(0.12)
        m.vigiar()
    assert m.tipos() == [], "estado de uma porta que ninguém conhece"

    m.placa.ligar_porta()
    m.drenar()
    antes = m.tipos().count("estado")
    m.vigiar()
    assert m.tipos().count("estado") == antes, "reenviou antes do intervalo"
    time.sleep(0.12)
    m.vigiar()
    assert m.tipos().count("estado") == antes + 1
    assert m.eventos()[-1]["fonte"] == "adapter"


def test_comando_estado_reenvia_o_modelo_sem_tocar_na_serial(com_porta):
    from fastapi import HTTPException
    m = com_porta
    with pytest.raises(HTTPException) as erro:
        m.chamar(m.modulo.cmd_porta_estado())
    assert erro.value.status_code == 503 and "passe a tag" in erro.value.detail

    m.placa.ligar_porta()
    m.drenar()
    m.cliente.posts.clear()
    resposta = m.chamar(m.modulo.cmd_porta_estado())
    assert resposta == {"ok": True, "enviado_a_placa": False}
    assert m.tipos() == ["estado"]


@pytest.mark.parametrize("nome", ["cmd_porta_liberar", "cmd_porta_negar"])
def test_liberar_e_negar_sao_409(conhecida, nome):
    from fastapi import HTTPException
    m = conhecida
    with pytest.raises(HTTPException) as erro:
        m.chamar(getattr(m.modulo, nome)())
    assert erro.value.status_code == 409 and "porta autônoma" in erro.value.detail


# ── 4. O link: a USB do receptor ─────────────────────────────────────────────

def test_usb_cai_vira_UM_link_perdido_e_o_modelo_esquece(conhecida):
    m = conhecida
    m.placa.derrubar_cabo()
    assert _ate(lambda: not m.leitor.conectado, esperando="o leitor notar a queda")
    m.vigiar()
    m.vigiar()
    assert m.tipos() == ["porta_link_perdido"]
    assert m.modulo._modelo.conhecido is False

    # Volta: nada sobe até uma linha que prove o estado.
    assert _ate(lambda: m.leitor.conectado and m.placa.conectada)
    m.modulo.PORTA_ESTADO_REENVIO_S = 0.0
    m.vigiar()
    m.placa.passar_tag("DEADBEEF")
    m.drenar()
    m.vigiar()
    assert m.tipos() == ["porta_link_perdido"]

    m.placa.passar_tag(UID)
    m.drenar()
    assert m.tipos()[1:] == ["porta_link_ok", "pedido_acesso", "liberada", "estado"]


def test_queda_e_volta_mais_rapidas_que_a_vigia_tambem_sao_link_perdido(conhecida):
    """A vigia roda a cada 0,25 s, e a USB pode cair e voltar entre duas
    rodadas. O que se sabia da porta é da conexão ANTERIOR: a primeira linha
    da conexão nova não pode ser aplicada sobre ele."""
    m = conhecida
    m.placa.derrubar_cabo()
    assert _ate(lambda: m.leitor.estado()["conexoes"] == 2 and m.placa.conectada)
    m.placa.passar_tag(UID)
    m.drenar()
    assert m.tipos() == ["porta_link_perdido", "porta_link_ok", "pedido_acesso",
                         "liberada", "estado"]


def test_health_publica_a_porta(conhecida):
    m = conhecida
    corpo = m.chamar(m.modulo.health())
    porta = corpo["porta"]
    assert porta["link_ok"] is True and porta["estado_conhecido"] is True
    assert porta["modelo"]["estado"] == "FECHADA"
    assert porta["idade_ultima_linha_porta_s"] is not None
    assert porta["receptor_sem_dados"] is False
    for campo in ("ultimo_codigo_bancada", "linhas_desconhecidas", "usb_conectada"):
        assert campo in porta
    assert corpo["serial"]["conectado"] is True and corpo["serial"]["url"] == m.placa.url
    assert corpo["checks"]["receptor-usb"] == "ok"
    assert corpo["checks"]["estado-porta"] == "conhecido"


def test_health_com_o_estado_desconhecido_e_o_aviso_do_receptor(com_porta):
    m = com_porta
    m.placa.linha_crua("[aviso] Nenhum byte recebido no Serial2 ate agora. Confira: "
                       "TX(17) da porta -> RX(16) daqui, GND comum, baud 115200.")
    m.drenar()
    corpo = m.chamar(m.modulo.health())
    assert corpo["checks"]["estado-porta"] == "desconhecido — passe a tag"
    assert corpo["porta"]["receptor_sem_dados"] is True
    assert corpo["status"] == "degradado"


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
    assert modulo._leitor is None, "um leitor nasceu mesmo com a recusa"


# ── 5. Guardas por AST: nada desce, nada bloqueia o loop ─────────────────────

ESCRITAS = {"write", "writelines", "flush", "enviar_comando", "send", "sendall"}


def _arvores():
    return {p.name: ast.parse(p.read_text(encoding="utf-8")) for p in sorted(PASTA.glob("*.py"))}


def test_nenhuma_escrita_serial_no_porta_adapter():
    """O PC só LÊ a USB do receptor. Um `write` aqui poderia pôr a porta em
    modo registro (`R`) ou mandar lixo ao Serial2 dela (`T`)."""
    arvores = _arvores()
    assert {"main.py", "leitor_linhas.py", "porta_texto.py"} <= set(arvores)
    infracoes = [f"{nome}: {ast.unparse(no.func)}()"
                 for nome, arvore in arvores.items() for no in ast.walk(arvore)
                 if isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)
                 and no.func.attr in ESCRITAS]
    assert not infracoes, infracoes
    assert not (PASTA / "serial_link.py").exists(), "o protocolo JSON/ACK voltou"


def test_a_guarda_de_escrita_enxerga_uma_escrita():
    """Guarda da guarda: a varredura acima tem de reprovar um `write`."""
    arvore = ast.parse("def f(conn):\n    conn.write(b'R')\n")
    chamadas = [n.func.attr for n in ast.walk(arvore)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert set(chamadas) & ESCRITAS == {"write"}


LEITURAS = {"read", "readline", "readlines", "read_until"}


def test_nenhuma_leitura_serial_dentro_de_async_def():
    arvore = ast.parse(MAIN.read_text(encoding="utf-8"))
    infracoes = []
    for no in ast.walk(arvore):
        if not isinstance(no, ast.AsyncFunctionDef):
            continue
        for interno in ast.walk(no):
            if (isinstance(interno, ast.Call) and isinstance(interno.func, ast.Attribute)
                    and interno.func.attr in LEITURAS):
                infracoes.append(f"{no.name}: {ast.unparse(interno.func)}()")
    assert not infracoes, infracoes
    # Guarda da guarda: a leitura existe, e mora no leitor (thread).
    assert "conn.read(" in LEITOR.read_text(encoding="utf-8")


def test_o_leitor_nao_conhece_asyncio():
    fonte = LEITOR.read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    assert not [n for n in ast.walk(arvore) if isinstance(n, ast.AsyncFunctionDef)]
    assert not re.search(r"^\s*(import|from)\s+asyncio", fonte, re.M)
    assert "run_coroutine_threadsafe" in MAIN.read_text(encoding="utf-8")


def test_o_tradutor_e_puro():
    fonte = (PASTA / "porta_texto.py").read_text(encoding="utf-8")
    for proibido in ("asyncio", "fastapi", "serial", "httpx", "import time"):
        assert not re.search(rf"^\s*(import|from)\s+{proibido}", fonte, re.M), proibido


def test_o_adapter_so_tem_transporte_serial():
    """Não há simulador de porta: fingir a porta faria o central confiar numa
    porta imaginária."""
    fonte = MAIN.read_text(encoding="utf-8")
    assert "PORTA_TRANSPORTE" not in fonte and "_post_sim" not in fonte
    for saiu in ("PORTA_ACK_TIMEOUT_S", "PORTA_HEARTBEAT_MAX_S", "serial_link",
                 "_COMANDOS_PORTA", "_enviar_porta"):
        assert saiu not in fonte, saiu


def test_o_host_bat_exige_a_COM_e_sobe_na_8104():
    bat = (PASTA / "iniciar_host.bat").read_text(encoding="utf-8")
    assert "PORTA_SERIAL_URL" in bat
    assert re.search(r'if "%PORTA_SERIAL_URL%"=="" \(\s*\r?\n', bat), (
        "o .bat tem de SAIR quando a COM não foi definida")
    assert "exit /b 1" in bat
    codigo = "\n".join(l for l in bat.splitlines() if not l.strip().lower().startswith("rem"))
    assert not re.search(r"set PORTA_SERIAL_URL=COM\d", codigo), "COM chutada como default"
    assert "set PORTA_HTTP=8104" in bat
    assert "set CENTRAL_URL=http://localhost:8000" in bat
    assert "PORTA_ACK_TIMEOUT_S" not in bat and "PORTA_HEARTBEAT_MAX_S" not in bat
    assert "RECEPTOR" in bat.upper()
    req = (PASTA / "requirements.txt").read_text(encoding="utf-8")
    assert "pyserial" in req and "leitor_linhas.py" in req
