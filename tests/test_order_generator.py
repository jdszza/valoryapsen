"""Backpressure do order-generator.

O gerador posta uma OS a cada `INTERVALO_OS` (90s) e a planta leva de 90 a 140s
por OS — sob trava do Triple Check, tempo indeterminado. Antes de gerar, ele
consulta `GET /api/v1/fila`; o que estes testes prendem é o comportamento nas
bordas, porque as duas maneiras de errar aqui são graves: laço apertado
(martelando o central) e processo encerrado (planta sem OS até alguém notar).
"""
import pytest


@pytest.fixture
def gerador(carregar_simulador):
    """Order-generator com `requests` duplado e esperas de 0s (teste rápido)."""
    return carregar_simulador(
        "order-generator/simulator.py",
        env={"ESPERA_FILA_CHEIA": "0", "MAX_ESPERAS_FILA": "3"},
    )


def _responder_fila(gerador, **campos):
    gerador.requests.status_code = 200
    gerador.requests.payload = {"tamanho": 0, "capacidade": 5, "disponivel": 5,
                                "cheia": False, "os_ativa": False,
                                "trava_ativa": False, **campos}


def test_fila_com_vaga_libera_na_primeira_consulta(gerador):
    _responder_fila(gerador, tamanho=2, disponivel=3)

    assert gerador.modulo._esperar_vaga_na_fila() is True
    assert len(gerador.chamadas) == 1


def test_fila_cheia_espera_e_desiste_do_ciclo(gerador):
    """Desistir do ciclo é o que evita o laço apertado — e não encerra nada."""
    _responder_fila(gerador, tamanho=5, disponivel=0, cheia=True, trava_ativa=True)

    assert gerador.modulo._esperar_vaga_na_fila() is False
    # Uma consulta por espera, e nem uma a mais: o teto é MAX_ESPERAS_FILA.
    assert len(gerador.chamadas) == 3


def test_vaga_que_abre_no_meio_da_espera_interrompe_o_ciclo_de_espera(gerador):
    _responder_fila(gerador, tamanho=5, disponivel=0, cheia=True)

    chamadas = []
    original = gerador.requests.get

    def _get(url, **kwargs):
        chamadas.append(url)
        if len(chamadas) == 2:            # a fila esvaziou entre as consultas
            _responder_fila(gerador, tamanho=1, disponivel=4)
        return original(url, **kwargs)

    gerador.requests.get = _get

    assert gerador.modulo._esperar_vaga_na_fila() is True
    assert len(chamadas) == 2


def test_central_fora_do_ar_nao_trava_a_geracao(gerador):
    """Consulta auxiliar quebrada não pode parar a planta — o POST decide."""
    gerador.requests.status_code = 503

    assert gerador.modulo._esperar_vaga_na_fila() is True
    assert len(gerador.chamadas) == 1


def test_429_e_descartado_sem_retentar(gerador):
    """Recusa não vira reenvio: a próxima OS já nasce com `os_id` novo."""
    gerador.requests.status_code = 429
    gerador.requests.payload = {"erro": "fila_cheia",
                                "fila": {"tamanho": 5, "capacidade": 5}}

    aceita = gerador.modulo._enviar_os({"os_id": "OS-1", "medicamentos": []})

    assert aceita is False
    assert len(gerador.chamadas) == 1          # uma tentativa, nenhuma retentativa
