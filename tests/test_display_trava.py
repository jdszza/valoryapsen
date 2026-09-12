"""A trava do Triple Check no display de 7" — o lado do backend, sem placa.

Dois `cmd` novos (`get_trava`, `liberar_trava`), uma tag de `resp` (`trava`) e
um `push` (`trava`). O que estes testes prendem, além dos conjuntos fechados de
`test_protocolo_serial.py`:

  * `get_trava` responde com a tag e os campos do contrato, e cai num cache de
    2 s — o pedido roda DENTRO da ponte serial, e o `serial_request` do
    firmware desiste em 800 ms;
  * `liberar_trava` confere nome + PIN no backend (PIN errado não libera,
    perfil sem `trava_liberar` não libera), e a resposta nunca carrega o PIN;
  * o push `trava` sai da thread do espelho SÓ quando o estado muda;
  * o motivo atravessa o backend inteiro sem truncar — quem trunca, e com
    reticências, é o firmware (`MAX_MOTIVO_LEN`); e nenhuma resposta estoura
    o `s2_buf` de 4096 bytes.

O firmware não compila na suíte; o que se cobra dele aqui é o TEXTO: a
constante, o buffer dimensionado por ela e o `copy_trunc` aplicado ao motivo.
"""
import json
import re

import pytest

from test_protocolo_serial import FIRMWARE, ConexaoFake, _responder

TRAVA = {
    "ativa": True, "os_id": "OS-INFECTO-01-20260909T151145-9F0E7D", "slot_id": 3,
    "motivo": ("Triple Check FALHOU (2/3 fontes divergentes, limiar=1) — D3: "
               "dispenser: dispensou 9 de 10 esperados; balança: desvio=10.0%"),
}
SEM_TRAVA = {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""}
CONTA = {"PAINEL_CENTRAL_USER": "painel-bancada", "PAINEL_CENTRAL_SENHA": "segredo-servico"}


@pytest.fixture
def painel(carregar_painel):
    painel = carregar_painel(trava_central=TRAVA, env=CONTA)
    painel.modulo.central_comandos.esquecer_sessao()
    painel.modulo.central_comandos.requests.payload = {"token": "jwt-de-teste"}
    return painel


def _cadastrar(painel, nome: str, pin: str, perfil: str) -> None:
    conn = painel.conexao()
    try:
        conn.execute(
            "INSERT INTO operadores (nome, pin_hash, perfil, ativo, data_criacao) VALUES (?,?,?,1,?)",
            (nome, painel.modulo.gerar_pin_hash(pin), perfil, "2026-01-01"),
        )
        conn.commit()
    finally:
        conn.close()


def _liberacoes(painel) -> list:
    return [c for c in painel.modulo.central_comandos.requests.chamadas
            if c["metodo"] == "POST" and c["url"].endswith("/liberar-trava")]


def _pushes(painel) -> list:
    enviados = []
    painel.modulo.push_to_display = lambda payload: enviados.append(payload)
    return enviados


def _leituras_da_trava(painel) -> int:
    return painel.central.caminhos.count("/api/v1/trava")


# ── get_trava ─────────────────────────────────────────────────────────────────

def test_get_trava_responde_com_a_tag_e_os_campos_do_contrato(painel):
    bruto, resposta = _responder(painel, {"cmd": "get_trava"})

    assert resposta["resp"] == "trava"
    assert resposta["ativa"] is True
    assert resposta["os_id"] == TRAVA["os_id"]
    assert resposta["slot_id"] == 3
    assert resposta["motivo"] == TRAVA["motivo"]
    assert isinstance(resposta["ts"], int)
    assert bruto.endswith("\n") and bruto.count("\n") == 1


def test_get_trava_e_cacheado_por_uma_janela_curta(painel):
    """Dois pedidos seguidos do display: UMA leitura no central."""
    _responder(painel, {"cmd": "get_trava"})
    _responder(painel, {"cmd": "get_trava"})
    assert _leituras_da_trava(painel) == 1

    painel.modulo._trava_espelho["lida_em"] = 0.0        # janela vencida
    _responder(painel, {"cmd": "get_trava"})
    assert _leituras_da_trava(painel) == 2
    assert painel.modulo.TRAVA_CACHE_S == painel.modulo.DISPENSERS_CACHE_S == 2.0


def test_get_trava_com_central_fora_e_sem_leitura_previa_responde_sem_trava(painel):
    """O display não tem o que fazer com "desconhecido"; o push da primeira
    leitura de verdade corrige em segundos."""
    painel.central.fora_do_ar = True
    _, resposta = _responder(painel, {"cmd": "get_trava"})
    assert resposta["resp"] == "trava"
    assert resposta["ativa"] is False


def test_get_trava_com_central_fora_mantem_o_ultimo_estado_conhecido(painel):
    _responder(painel, {"cmd": "get_trava"})
    painel.central.fora_do_ar = True
    painel.modulo._trava_espelho["lida_em"] = 0.0
    _, resposta = _responder(painel, {"cmd": "get_trava"})
    assert resposta["ativa"] is True and resposta["slot_id"] == 3


# ── liberar_trava ─────────────────────────────────────────────────────────────

def test_pin_errado_nao_libera_e_nada_sai_para_o_central(painel):
    """O seed tem "Administrador" com PIN 1234."""
    bruto, resposta = _responder(painel, {"cmd": "liberar_trava",
                                          "nome": "Administrador", "pin": "0000"})
    assert resposta == {"resp": "ok", "ok": False, "msg": "nome ou PIN incorreto"}
    assert "0000" not in bruto
    assert _liberacoes(painel) == []


def test_nome_desconhecido_e_a_mesma_recusa_do_pin_errado(painel):
    """Não dizer QUAL dos dois errou: enumerar nomes pelo cabo é enumerar
    metade da credencial."""
    _, resposta = _responder(painel, {"cmd": "liberar_trava", "nome": "Ninguém", "pin": "1234"})
    assert resposta["msg"] == "nome ou PIN incorreto"


def test_supervisor_com_pin_certo_libera_em_nome_dele(painel):
    _cadastrar(painel, "Sup Teste", "9876", "Supervisor")

    bruto, resposta = _responder(painel, {"cmd": "liberar_trava",
                                          "nome": "Sup Teste", "pin": "9876"})

    assert resposta["resp"] == "ok" and resposta["ok"] is True
    assert "9876" not in bruto
    (chamada,) = _liberacoes(painel)
    assert chamada["json"] == {"em_nome_de": "Sup Teste"}
    conn = painel.conexao()
    try:
        (registro,) = conn.execute(
            "SELECT operador, perfil, acao FROM historico WHERE acao LIKE 'Liberar trava%'"
        ).fetchall()
    finally:
        conn.close()
    assert tuple(registro) == ("Sup Teste", "Supervisor", "Liberar trava (display)")


def test_admin_tambem_libera_pelo_display(painel):
    _, resposta = _responder(painel, {"cmd": "liberar_trava",
                                      "nome": "Administrador", "pin": "1234"})
    assert resposta["ok"] is True
    assert _liberacoes(painel)[0]["json"] == {"em_nome_de": "Administrador"}


@pytest.mark.parametrize("perfil", ["Operador", "PCP", "PCM"])
def test_perfil_sem_permissao_nao_libera_mesmo_com_o_pin_certo(painel, perfil):
    _cadastrar(painel, f"Pessoa {perfil}", "5555", perfil)

    _, resposta = _responder(painel, {"cmd": "liberar_trava",
                                      "nome": f"Pessoa {perfil}", "pin": "5555"})

    assert resposta["ok"] is False
    assert perfil in resposta["msg"]
    assert _liberacoes(painel) == []


def test_operador_desativado_nao_libera(painel):
    _cadastrar(painel, "Ex Sup", "1111", "Supervisor")
    conn = painel.conexao()
    try:
        conn.execute("UPDATE operadores SET ativo=0 WHERE nome='Ex Sup'")
        conn.commit()
    finally:
        conn.close()
    _, resposta = _responder(painel, {"cmd": "liberar_trava", "nome": "Ex Sup", "pin": "1111"})
    assert resposta["ok"] is False and _liberacoes(painel) == []


def test_sem_nome_ou_pin_recusa_sem_consultar_nada(painel):
    for corpo in ({"cmd": "liberar_trava"}, {"cmd": "liberar_trava", "nome": "X"},
                  {"cmd": "liberar_trava", "pin": "1"}):
        _, resposta = _responder(painel, corpo)
        assert resposta["ok"] is False
    assert _liberacoes(painel) == []


def test_sem_conta_de_servico_o_display_recebe_a_variavel_que_falta(carregar_painel):
    painel = carregar_painel(trava_central=TRAVA,
                             env={"PAINEL_CENTRAL_USER": None, "PAINEL_CENTRAL_SENHA": None})
    _, resposta = _responder(painel, {"cmd": "liberar_trava",
                                      "nome": "Administrador", "pin": "1234"})
    assert resposta["ok"] is False
    assert "PAINEL_CENTRAL_USER" in resposta["msg"]


def test_central_fora_do_ar_na_liberacao_vira_msg_e_nao_silencio(painel, monkeypatch):
    def _explode(*args, **kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(painel.modulo.central_comandos.requests, "post", _explode)

    conn = ConexaoFake()
    painel.modulo._handle_serial_message(conn, {"cmd": "liberar_trava",
                                                "nome": "Administrador", "pin": "1234"})
    (bruto,) = conn.escrito
    resposta = json.loads(bruto)
    assert resposta["resp"] == "ok" and resposta["ok"] is False
    assert "indispon" in resposta["msg"]


# ── push `trava` ──────────────────────────────────────────────────────────────

def test_push_sai_na_transicao_e_nao_na_repeticao(painel):
    enviados = _pushes(painel)

    painel.modulo.sincronizar_trava_central(avisar_display=True)
    painel.modulo.sincronizar_trava_central(avisar_display=True)
    painel.modulo.sincronizar_trava_central(avisar_display=True)
    assert enviados == [{"push": "trava", "ativa": True, "os_id": TRAVA["os_id"],
                         "slot_id": 3, "motivo": TRAVA["motivo"]}]

    painel.central.trava = SEM_TRAVA
    painel.modulo.sincronizar_trava_central(avisar_display=True)
    assert enviados[-1] == {"push": "trava", "ativa": False, "os_id": "",
                            "slot_id": None, "motivo": ""}
    assert len(enviados) == 2


def test_push_nao_sai_com_central_fora_do_ar(painel):
    enviados = _pushes(painel)
    painel.modulo.sincronizar_trava_central(avisar_display=True)
    painel.central.fora_do_ar = True
    painel.modulo.sincronizar_trava_central(avisar_display=True)
    assert len(enviados) == 1


def test_o_get_trava_do_display_nao_empurra_push(painel):
    """O display que perguntou já recebeu a resposta; push aqui seria eco."""
    enviados = _pushes(painel)
    _responder(painel, {"cmd": "get_trava"})
    assert enviados == []


# ── O motivo e o buffer do firmware ──────────────────────────────────────────

MOTIVO_400 = ("Triple Check FALHOU (3/3 fontes divergentes, limiar=1) — D8: "
              + "dispenser: dispensou 14 de 15 esperados; "
              + "câmera_mesa: detectou 14 de 15; "
              + "balança: desvio=6.7%; ") * 3


def test_motivo_de_400_caracteres_atravessa_o_backend_sem_truncar(carregar_painel):
    """Quem trunca é o firmware, com reticências. O backend passa inteiro: um
    corte aqui seria um segundo ponto de truncamento, e em silêncio."""
    trava = {**TRAVA, "motivo": MOTIVO_400}
    painel = carregar_painel(trava_central=trava, env=CONTA)
    assert len(MOTIVO_400) >= 400

    bruto, resposta = _responder(painel, {"cmd": "get_trava"})
    assert resposta["motivo"] == MOTIVO_400
    assert len(bruto.encode("utf-8")) < 4096

    enviados = _pushes(painel)
    # O `get_trava` acima já atualizou o espelho; zera para a leitura seguinte
    # ser uma transição de verdade e o push sair.
    painel.modulo._trava_espelho["atual"] = None
    painel.modulo.sincronizar_trava_central(avisar_display=True)
    assert enviados[0]["motivo"] == MOTIVO_400
    assert len((json.dumps(enviados[0]) + "\n").encode("utf-8")) < 4096


def _copy_trunc(cap: int, src: str) -> str:
    """Porte de `copy_trunc()` do firmware, para documentar o contrato aqui."""
    if len(src) < cap:
        return src
    keep = cap - 1
    return src[:keep - 3] + "..." if keep > 3 else src[:keep]


def test_o_firmware_dimensiona_o_motivo_por_constante_e_trunca_com_reticencias():
    fw = FIRMWARE.read_text(encoding="utf-8", errors="replace")
    assert re.search(r"#define MAX_MOTIVO_LEN 256\b", fw)
    assert "char motivo[MAX_MOTIVO_LEN];" in fw
    assert "copy_trunc(trava.motivo, sizeof(trava.motivo)" in fw
    # E o contrato de `copy_trunc` que ele herda: 400 caracteres cabem em 255
    # terminados em "...", nunca cortados em silêncio.
    resultado = _copy_trunc(256, MOTIVO_400)
    assert len(resultado) == 255 and resultado.endswith("...")
    assert _copy_trunc(256, TRAVA["motivo"]) == TRAVA["motivo"]


def test_o_firmware_espera_5s_no_liberar_trava_e_pede_get_trava_pela_tag_trava():
    fw = FIRMWARE.read_text(encoding="utf-8", errors="replace")
    # A DEFINIÇÃO, não o protótipo (que vem primeiro no arquivo).
    bloco = fw.rsplit("static bool liberar_trava_backend", 1)[1].split("\n}\n", 1)[0]
    assert 'doc["cmd"] = "liberar_trava"' in bloco
    assert re.search(r'serial_request\(payload,\s*"ok",\s*resp,\s*5000\)', bloco)
    assert re.search(r'serial_request\("\{\\"cmd\\":\\"get_trava\\"\}",\s*"trava"', fw)
    assert 'strcmp(push_type, "trava") == 0' in fw
