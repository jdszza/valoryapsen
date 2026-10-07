# -*- coding: utf-8 -*-
"""A fonte de cada câmera, e a telemetria inventada que ela faz descartar.

Na célula montada a câmera da mesa passa a ser a estação real
(`vision/visao_mesa`), e o vision-simulator continua no ar como rollback —
mandando, a cada 60 s, temperatura das três câmeras e do "processador de
visão". Nenhuma estação real emite telemetria hoje. Então, para uma câmera que
já é real, toda telemetria que chega foi inventada pelo simulador, e o central
a gravaria em `leituras_sensores` ao lado do dado de verdade.

`VISAO_MESA_FONTE` e `VISAO_DISPENSER_FONTE` dizem qual câmera é real. Com as
duas em "simulador" — o default —, o adapter tem de ser exatamente o de antes:
mesmo payload ao central, mesma resposta ao simulador.

Nenhum teste faz HTTP: o `_client` do adapter é trocado por um duplo, como em
`test_adapters.py`.
"""
import asyncio
import json
import logging

import pytest

VARIAVEIS = ("VISAO_MESA_FONTE", "VISAO_DISPENSER_FONTE", "VISION_SIM_URL")


class _Resposta:
    def __init__(self, status_code: int = 200, corpo: dict | None = None):
        self.status_code = status_code
        self.text = ""
        self._corpo = corpo or {"ok": True}

    def json(self):
        return self._corpo


class _ClienteFake:
    def __init__(self):
        self.chamadas: list[dict] = []

    async def post(self, url, json=None, timeout=None):
        self.chamadas.append({"url": url, "json": json})
        return _Resposta()


@pytest.fixture
def adapter(carregar_adapter, monkeypatch):
    """Carrega o vision-adapter com as fontes pedidas e um cliente HTTP falso.

    O ambiente de quem roda a suíte não pode decidir o resultado: as três
    variáveis saem antes, e só volta o que o teste passar.
    """
    def _carregar(mesa: str | None = None, dispenser: str | None = None):
        for nome in VARIAVEIS:
            monkeypatch.delenv(nome, raising=False)
        env = {}
        if mesa is not None:
            env["VISAO_MESA_FONTE"] = mesa
        if dispenser is not None:
            env["VISAO_DISPENSER_FONTE"] = dispenser
        modulo = carregar_adapter("vision", env)
        modulo._client = _ClienteFake()
        return modulo
    return _carregar


def _receber(modulo, evento: dict) -> dict:
    return asyncio.run(modulo.receber_evento(modulo.EventoVisionReq(**evento)))


def _telemetria(componente: str) -> dict:
    """O evento exatamente como `vision-simulator._telemetria_loop` o monta."""
    return {"tipo": "telemetria", "camera": "sistema", "componente": componente,
            "tipo_leitura": "temperatura", "valor": 33.4, "unidade": "°C",
            "ts": "2026-10-07T12:00:00+00:00"}


COMPONENTES = ("camera_mesa", "camera_dispenser_esq", "camera_dispenser_dir",
               "processador_visao")


# ── Leitura das variáveis ─────────────────────────────────────────────────────

def test_sem_variavel_as_duas_fontes_sao_o_simulador(adapter):
    modulo = adapter()
    assert (modulo.VISAO_MESA_FONTE, modulo.VISAO_DISPENSER_FONTE) == \
        ("simulador", "simulador")
    assert modulo.VISION_SIM_URL == "http://vision-simulator:8202"


def test_grafia_digitada_no_env_e_aceita(adapter):
    modulo = adapter(mesa=" Estacao ", dispenser="SIMULADOR")
    assert (modulo.VISAO_MESA_FONTE, modulo.VISAO_DISPENSER_FONTE) == \
        ("estacao", "simulador")


@pytest.mark.parametrize("valor", ["estação", "real", "camera", ""])
def test_fonte_invalida_cai_no_simulador_com_erro_no_log(adapter, caplog, valor):
    """O lado que não descarta nada: um erro de digitação deixa passar a
    telemetria inventada — visível no histórico — em vez de calar uma câmera."""
    with caplog.at_level(logging.ERROR):
        modulo = adapter(mesa=valor)

    assert modulo.VISAO_MESA_FONTE == "simulador"
    erros = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("VISAO_MESA_FONTE" in r.getMessage() for r in erros), caplog.text


# ── O filtro de telemetria ────────────────────────────────────────────────────

# (fonte da mesa, fonte dos dispensers) → componentes cuja telemetria é
# inventada. Escrita por extenso, e não derivada da regra: é a tabela que a
# regra tem de cumprir.
DESCARTADOS = {
    ("simulador", "simulador"): set(),
    ("estacao",   "simulador"): {"camera_mesa", "processador_visao"},
    ("simulador", "estacao"):   {"camera_dispenser_esq", "camera_dispenser_dir",
                                 "processador_visao"},
    ("estacao",   "estacao"):   set(COMPONENTES),
}


@pytest.mark.parametrize("componente", COMPONENTES)
@pytest.mark.parametrize("mesa,dispenser", sorted(DESCARTADOS))
def test_telemetria_de_camera_real_e_descartada(adapter, mesa, dispenser, componente):
    modulo = adapter(mesa=mesa, dispenser=dispenser)

    resposta = _receber(modulo, _telemetria(componente))

    if componente in DESCARTADOS[(mesa, dispenser)]:
        assert resposta == {"ok": True, "encaminhado": False,
                            "descartado": "telemetria_simulada"}
        assert modulo._client.chamadas == []
    else:
        assert resposta == {"ok": True, "encaminhado": True}
        assert len(modulo._client.chamadas) == 1


def test_componente_desconhecido_nunca_e_descartado(adapter):
    """O filtro descarta o que SABE ser do simulador. Um componente novo — a
    telemetria que uma estação real venha a mandar — passa."""
    modulo = adapter(mesa="estacao", dispenser="estacao")

    resposta = _receber(modulo, _telemetria("camera_mesa_estacao_real"))

    assert resposta == {"ok": True, "encaminhado": True}
    assert len(modulo._client.chamadas) == 1


@pytest.mark.parametrize("mesa,dispenser", sorted(DESCARTADOS))
def test_leitura_nunca_e_filtrada(adapter, mesa, dispenser):
    """Só telemetria passa pelo filtro: as leituras são o resultado das câmeras."""
    modulo = adapter(mesa=mesa, dispenser=dispenser)

    for evento in LEITURAS:
        _receber(modulo, evento)

    assert len(modulo._client.chamadas) == len(LEITURAS)


# ── Com as duas fontes no simulador, o adapter é o de antes ──────────────────

LEITURAS = [
    {"tipo": "leitura_dispenser_ok", "camera": "dispenser_esq", "slot_id": 3,
     "os_id": "OS-1", "sku_esperado": "X", "sku_lido": "X",
     "medicamento_lido": "MIOSAN 5MG", "match_sku": True, "confianca": 0.97,
     "ts": "2026-10-07T12:00:00+00:00"},
    {"tipo": "leitura_dispenser_falha", "camera": "dispenser_dir", "slot_id": 7,
     "os_id": "OS-1", "motivo": "camera_obstruida", "sku_esperado": "X",
     "confianca": 0.0, "falha_injetada": False, "ts": "2026-10-07T12:00:00+00:00"},
    {"tipo": "leitura_dispenser_divergencia", "camera": "dispenser_dir",
     "slot_id": 6, "os_id": "OS-1", "sku_esperado": "X", "sku_lido": "Y",
     "medicamento_lido": "Y", "match_sku": False, "confianca": 0.9,
     "falha_injetada": False, "ts": "2026-10-07T12:00:00+00:00"},
    # O evento da estação da mesa real, com os campos extras que ela manda.
    {"tipo": "leitura_mesa_divergencia", "camera": "mesa", "slot_id": 2,
     "os_id": "OS-1", "quantidade_esperada": 4, "quantidade_detectada": 3,
     "delta": -1, "confianca": 0.93, "ts": "2026-10-07T12:00:00+00:00",
     "quantidade_total_caixa": 7, "quantidade_total_anterior": 4,
     "cobertura": 0.91, "posicao_x": None, "posicao_y": None},
    {"tipo": "leitura_mesa_ok", "camera": "mesa", "slot_id": 1, "os_id": "OS-1",
     "quantidade_esperada": 4, "quantidade_detectada": 4, "delta": 0,
     "confianca": 0.95, "ts": "2026-10-07T12:00:00+00:00"},
    {"tipo": "leitura_mesa_falha", "camera": "mesa", "slot_id": 5, "os_id": "OS-1",
     "quantidade_esperada": 2, "motivo": "imagem_fora_de_foco",
     "quantidade_detectada": 0, "delta": -2, "confianca": 0.0,
     "falha_injetada": False, "ts": "2026-10-07T12:00:00+00:00"},
]


@pytest.mark.parametrize("evento", LEITURAS + [_telemetria(c) for c in COMPONENTES],
                         ids=lambda e: f"{e['tipo']}:{e.get('componente', e['camera'])}")
def test_com_tudo_no_simulador_o_central_recebe_o_mesmo_payload(adapter, evento):
    """Byte a byte: o que chega ao central é o `model_dump()` do evento — o que
    o adapter mandava antes de existir filtro —, e a resposta ao simulador é a
    de sempre."""
    modulo = adapter()
    antes = json.dumps(modulo.EventoVisionReq(**evento).model_dump(), ensure_ascii=False)

    resposta = _receber(modulo, evento)

    assert resposta == {"ok": True, "encaminhado": True}
    assert len(modulo._client.chamadas) == 1
    chamada = modulo._client.chamadas[0]
    assert chamada["url"] == modulo.CENTRAL_URL + "/api/v1/eventos/visao"
    assert json.dumps(chamada["json"], ensure_ascii=False) == antes


# ── O comando da mesa não muda de caminho ─────────────────────────────────────

@pytest.mark.parametrize("mesa", ["simulador", "estacao"])
def test_captura_da_mesa_vai_sempre_para_vision_sim_url(adapter, mesa):
    """Com a estação real, VISION_SIM_URL É o endereço dela: a fonte da mesa só
    decide a telemetria, nunca o destino do comando."""
    modulo = adapter(mesa=mesa)
    req = modulo.CapturarMesaReq(slot_id=2, os_id="OS-1", quantidade_esperada=3,
                                 posicao_x=10.0, posicao_y=-150.0)

    asyncio.run(modulo.capturar_mesa(req))

    assert modulo._client.chamadas == [{
        "url": modulo.VISION_SIM_URL + "/executar/capturar/mesa",
        "json": {"slot_id": 2, "os_id": "OS-1", "quantidade_esperada": 3,
                 "posicao_x": 10.0, "posicao_y": -150.0, "slots_cobertos": None,
                 "quantidade_slot": None, "injetar_falha": None},
    }]


# ══════════════════════════════════════════════════════════════════════════════
# Quem está do outro lado de VISION_SIM_URL
# ══════════════════════════════════════════════════════════════════════════════

class _Upstream:
    """O que responde em VISION_SIM_URL: `/ping`, `/status` e a captura."""
    def __init__(self, servico: str | None = "apsen-vision-station",
                 acumulado: dict | None = None, status_no_ar: bool = True):
        self.servico = servico
        self.acumulado = acumulado if acumulado is not None else {}
        self.status_no_ar = status_no_ar
        self.chamadas: list[dict] = []

    async def get(self, url, timeout=None):
        if url.endswith("/ping"):
            if self.servico is None:
                raise ConnectionError("fora do ar")
            return _Resposta(corpo={"status": "ok", "service": self.servico})
        if url.endswith("/status"):
            if not self.status_no_ar:
                raise ConnectionError("fora do ar")
            return _Resposta(corpo={"cameras": [{"pronta": True}], "fila": 0,
                                    "envios": {"falhados": 0},
                                    "acumulado_por_os": self.acumulado})
        raise ConnectionError(url)

    async def post(self, url, json=None, timeout=None):
        self.chamadas.append({"url": url, "json": json})
        return _Resposta()


def _com_upstream(modulo, upstream):
    modulo._client = upstream
    return upstream


@pytest.mark.parametrize("fonte,servico,confere", [
    ("estacao",   "apsen-vision-station",   True),
    ("estacao",   "apsen-vision-simulator", False),
    ("simulador", "apsen-vision-simulator", True),
    ("simulador", "apsen-vision-station",   False),
    ("estacao",   None,                     None),
    ("simulador", None,                     None),
])
def test_identidade_do_upstream(adapter, fonte, servico, confere):
    modulo = adapter(mesa=fonte)
    _com_upstream(modulo, _Upstream(servico))

    assert asyncio.run(modulo._conferir_upstream()) is confere
    assert modulo._identidade_mesa["confere"] is confere


def _req_mesa(modulo, slot=2, os_id="OS-1", **extra):
    return modulo.CapturarMesaReq(slot_id=slot, os_id=os_id, quantidade_esperada=3,
                                  **extra)


def test_captura_da_mesa_recusada_so_com_divergencia_confirmada(adapter):
    """Câmera real contando de mentira é pior que câmera ausente."""
    from fastapi import HTTPException
    modulo = adapter(mesa="estacao")
    upstream = _com_upstream(modulo, _Upstream("apsen-vision-simulator"))
    asyncio.run(modulo._conferir_upstream())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(modulo.capturar_mesa(_req_mesa(modulo)))

    assert exc.value.status_code == 503
    assert "VISAO_MESA_FONTE=estacao" in exc.value.detail
    assert upstream.chamadas == []


@pytest.mark.parametrize("servico", [None, "apsen-vision-station"])
def test_identidade_desconhecida_ou_certa_segue(adapter, servico):
    modulo = adapter(mesa="estacao")
    upstream = _com_upstream(modulo, _Upstream(servico))
    asyncio.run(modulo._conferir_upstream())

    asyncio.run(modulo.capturar_mesa(_req_mesa(modulo)))

    assert len(upstream.chamadas) == 1


def test_health_publica_fontes_identidade_e_etiquetas(adapter):
    modulo = adapter(mesa="estacao", dispenser="estacao")
    _com_upstream(modulo, _Upstream("apsen-vision-simulator"))

    saude = asyncio.run(modulo.health())

    assert saude["status"] == "degradado"
    assert saude["fontes"] == {"mesa": "estacao", "dispensers": "estacao"}
    assert saude["identidade_mesa"]["confere"] is False
    assert saude["comandos_mesa_para"] == modulo.VISION_SIM_URL
    assert saude["etiquetas"]["quantidade"] == len(modulo.ETIQUETAS) >= 4
    assert saude["estacao_mesa"] == {"pronta": True, "fila": 0, "envios_falhados": 0}
    assert set(saude["busca_catalogo_idade_s"]) == {"esq", "dir"}


def test_lifespan_nao_espera_o_upstream(adapter, monkeypatch):
    """Na célula, o Docker sobe ANTES das estações do host: esperar aqui deixava
    o adapter sem servir por até ~150 s, e o compose o marcava unhealthy."""
    modulo = adapter()

    async def para_sempre(*_a, **_k):
        # Um Event que ninguém seta, e não `sleep`: a suíte acelera o sleep.
        await asyncio.Event().wait()
    monkeypatch.setattr(modulo, "_wait_for_upstream", para_sempre)
    monkeypatch.setattr(modulo, "_vigiar_upstream", para_sempre)

    async def cenario():
        contexto = modulo.lifespan(modulo.app)
        await asyncio.wait_for(contexto.__aenter__(), timeout=2.0)
        assert len(modulo._tarefas_fundo) == 2
        await contexto.__aexit__(None, None, None)

    asyncio.run(cenario())


# ── Número do .env que não derruba o import ──────────────────────────────────

@pytest.mark.parametrize("bruto,esperado,erro", [
    ("2,5", 2.5, False),
    ("7", 7.0, False),
    ("", 20.0, True),
    ("abc", 20.0, True),
    ("0.1", 20.0, True),         # abaixo da faixa
    ("500", 20.0, True),         # acima da faixa
    ("nan", 20.0, True),
])
def test_cfg_float(adapter, monkeypatch, caplog, bruto, esperado, erro):
    modulo = adapter()
    monkeypatch.setenv("X_TESTE", bruto)
    with caplog.at_level(logging.ERROR):
        assert modulo._cfg_float("X_TESTE", 20.0, 0.5, 120.0) == esperado
    assert ("X_TESTE" in caplog.text) is erro


def test_virgula_no_env_nao_derruba_o_adapter(carregar_adapter, monkeypatch):
    for nome in VARIAVEIS:
        monkeypatch.delenv(nome, raising=False)
    modulo = carregar_adapter("vision", {"TIMEOUT_CMD": "2,5", "VISAO_DISP_PRAZO_S": "x"})
    assert modulo.TIMEOUT_CMD == 2.5
    assert modulo.VISAO_DISP_PRAZO_S == 20.0


def test_prazo_maior_que_o_timeout_do_central_avisa(carregar_adapter, monkeypatch, caplog):
    for nome in VARIAVEIS:
        monkeypatch.delenv(nome, raising=False)
    with caplog.at_level(logging.WARNING):
        carregar_adapter("vision", {"VISAO_DISP_PRAZO_S": "40"})
    assert "TIMEOUT_VISAO_DISPENSER" in caplog.text


def test_captura_da_mesa_repassa_os_campos_informativos(adapter):
    modulo = adapter()
    upstream = _com_upstream(modulo, _Upstream("apsen-vision-simulator"))
    asyncio.run(modulo.capturar_mesa(_req_mesa(modulo, slots_cobertos=[1, 2],
                                               quantidade_slot=2)))
    corpo = upstream.chamadas[0]["json"]
    assert (corpo["slots_cobertos"], corpo["quantidade_slot"]) == ([1, 2], 2)


# ══════════════════════════════════════════════════════════════════════════════
# A estação da mesa sem a memória da OS
# ══════════════════════════════════════════════════════════════════════════════

def _leitura_mesa(tipo, slot, os_id="OS-1", esperado=3, detectado=3):
    return {"tipo": tipo, "camera": "mesa", "slot_id": slot, "os_id": os_id,
            "quantidade_esperada": esperado, "quantidade_detectada": detectado,
            "ts": "2026-10-07T12:00:00+00:00"}


def _encaminhados(upstream):
    return [c["json"] for c in upstream.chamadas if c["url"].endswith("/eventos/visao")]


@pytest.mark.parametrize("acumulado,status_no_ar,marca", [
    ({}, True, True),                    # a estação esqueceu a OS
    ({"OS-1": 3}, True, False),          # ela ainda conhece
    ({}, False, False),                  # /status fora: não bloqueia, não marca
])
def test_memoria_perdida_marca_ressincronizar(adapter, acumulado, status_no_ar, marca):
    modulo = adapter(mesa="estacao")
    upstream = _com_upstream(modulo, _Upstream(acumulado=acumulado,
                                               status_no_ar=status_no_ar))
    _receber(modulo, _leitura_mesa("leitura_mesa_ok", 1))          # registrou

    asyncio.run(modulo.capturar_mesa(_req_mesa(modulo, slot=2)))
    _receber(modulo, _leitura_mesa("leitura_mesa_divergencia", 2, detectado=6))

    ultimo = _encaminhados(upstream)[-1]
    assert ultimo.get("ressincronizar", False) is marca


def test_os_sem_leitura_anterior_nem_consulta_o_status(adapter):
    modulo = adapter(mesa="estacao")
    upstream = _com_upstream(modulo, _Upstream(acumulado={}))
    asyncio.run(modulo.capturar_mesa(_req_mesa(modulo, slot=1, os_id="OS-NOVA")))
    _receber(modulo, _leitura_mesa("leitura_mesa_ok", 1, os_id="OS-NOVA"))
    assert "ressincronizar" not in _encaminhados(upstream)[-1]


def test_com_o_simulador_nada_disso_acontece(adapter):
    modulo = adapter(mesa="simulador")
    upstream = _com_upstream(modulo, _Upstream("apsen-vision-simulator", acumulado={}))
    _receber(modulo, _leitura_mesa("leitura_mesa_ok", 1))
    asyncio.run(modulo.capturar_mesa(_req_mesa(modulo, slot=2)))
    _receber(modulo, _leitura_mesa("leitura_mesa_divergencia", 2, detectado=6))
    assert "ressincronizar" not in _encaminhados(upstream)[-1]


def test_memoria_de_os_e_limitada(adapter):
    modulo = adapter(mesa="estacao")
    _com_upstream(modulo, _Upstream())
    for i in range(modulo._MAX_OS_MEMORIA + 10):
        _receber(modulo, _leitura_mesa("leitura_mesa_ok", 1, os_id=f"OS-{i}"))
    assert len(modulo._mesa_registrou) == modulo._MAX_OS_MEMORIA
    assert "OS-0" not in modulo._mesa_registrou


# ══════════════════════════════════════════════════════════════════════════════
# Injeção de falha com a câmera REAL da mesa
# ══════════════════════════════════════════════════════════════════════════════

def test_injecao_na_mesa_real_nao_vai_a_estacao_e_reescreve_o_evento(adapter):
    """A estação mede e REGISTRA o total de verdade; o adapter acrescenta uma."""
    modulo = adapter(mesa="estacao")
    upstream = _com_upstream(modulo, _Upstream())

    asyncio.run(modulo.capturar_mesa(_req_mesa(modulo, slot=2,
                                               injetar_falha="divergencia_mesa")))
    assert upstream.chamadas[0]["json"]["injetar_falha"] is None

    _receber(modulo, _leitura_mesa("leitura_mesa_ok", 2, esperado=3, detectado=3))
    evento = _encaminhados(upstream)[-1]
    assert (evento["tipo"], evento["quantidade_detectada"], evento["delta"],
            evento["falha_injetada"], evento["quantidade_detectada_real"]) == \
        ("leitura_mesa_divergencia", 4, 1, True, 3)
    # E é one-shot: a foto seguinte sai como veio.
    _receber(modulo, _leitura_mesa("leitura_mesa_ok", 3))
    assert _encaminhados(upstream)[-1]["tipo"] == "leitura_mesa_ok"


def test_injecao_na_mesa_real_sobre_falha_encaminha_a_falha(adapter):
    modulo = adapter(mesa="estacao")
    upstream = _com_upstream(modulo, _Upstream())
    asyncio.run(modulo.capturar_mesa(_req_mesa(modulo, slot=2,
                                               injetar_falha="divergencia_mesa")))
    falha = {**_leitura_mesa("leitura_mesa_falha", 2), "motivo": "imagem_fora_de_foco"}

    _receber(modulo, falha)

    evento = _encaminhados(upstream)[-1]
    assert evento["tipo"] == "leitura_mesa_falha"
    assert "falha_injetada" not in evento or evento["falha_injetada"] is not True


def test_injecao_na_mesa_simulada_continua_indo_ao_simulador(adapter):
    modulo = adapter(mesa="simulador")
    upstream = _com_upstream(modulo, _Upstream("apsen-vision-simulator"))
    asyncio.run(modulo.capturar_mesa(_req_mesa(modulo, injetar_falha="divergencia_mesa")))
    assert upstream.chamadas[0]["json"]["injetar_falha"] == "divergencia_mesa"
