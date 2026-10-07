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
                 "posicao_x": 10.0, "posicao_y": -150.0, "injetar_falha": None},
    }]
