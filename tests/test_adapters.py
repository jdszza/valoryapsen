"""O caminho de VOLTA dos adapters ao central também retenta.

Os quatro adapters são a ponte bidirecional entre o orquestrador e os
simuladores. A ida — orquestrador → adapter — sempre teve retry (`_post` do
`orchestrator.py`, 3 tentativas). A volta não: `_post_central` postava UMA vez
e, em falha, só logava.

Quem paga por um evento perdido não é o adapter. O orquestrador fica bloqueado
em `aguardar_evento` esperando o `dispensado` daquele slot, estoura
`TIMEOUT_DISPENSA` e aborta uma OS cujo hardware fez tudo certo — com o
agravante registrado em CLAUDE.md ("Ciclo de vida de um slot de dispenser"): OS
abortada precisa devolver slot ao pool, e abortar é rotina nesta célula.

Um `503` de um central reiniciando dura segundos; a OS que ele mata dura o
turno. Daí a política ser a mesma dos dois lados, inclusive no critério do que
NÃO se retenta: 4xx é recusa determinística, e insistir só encheria o log com a
mesma linha três vezes.

Nenhum teste faz HTTP: os adapters guardam o `httpx.AsyncClient` em `_client`,
criado na `lifespan`, e é ele que os testes substituem.
"""
import asyncio

import pytest

from conftest import ADAPTERS

NOMES = [nome for nome, _, _ in ADAPTERS]


class RespostaHTTPFake:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = ""


class ClienteFake:
    """Duplo do `httpx.AsyncClient` que os adapters guardam em `_client`.

    `respostas` é a sequência devolvida a cada POST, na ordem. Um item `int` vira
    status; um item `Exception` é levantado (central inacessível). Esgotada a
    lista, o último item se repete — assim um teste de "central fora do ar o
    tempo todo" não precisa saber quantas tentativas existem.
    """

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas: list[dict] = []

    async def post(self, url, json=None, timeout=None):
        self.chamadas.append({"url": url, "json": json, "timeout": timeout})
        item = (self.respostas[len(self.chamadas) - 1]
                if len(self.chamadas) <= len(self.respostas)
                else self.respostas[-1])
        if isinstance(item, BaseException):
            raise item
        return RespostaHTTPFake(item)


def _enviar(modulo, respostas):
    cliente = ClienteFake(respostas)
    modulo._client = cliente
    ok = asyncio.run(modulo._post_central({"tipo": "dispensado"}))
    return ok, cliente


# ── O caso que matava a OS ────────────────────────────────────────────────────

@pytest.mark.parametrize("nome", NOMES)
def test_evento_perdido_por_falha_de_rede_e_reenviado(carregar_adapter, nome):
    """O bug: uma falha de rede e o evento sumia para sempre.

    O orquestrador não repergunta nada — ele espera o evento. Sem reenvio, a OS
    inteira morre por `TIMEOUT_DISPENSA` de um dispenser que dispensou.
    """
    modulo = carregar_adapter(nome)

    ok, cliente = _enviar(modulo, [ConnectionError("connection refused"), 200])

    assert ok is True
    assert len(cliente.chamadas) == 2


@pytest.mark.parametrize("nome", NOMES)
def test_central_reiniciando_503_e_retentado(carregar_adapter, nome):
    """503 é o central subindo — dura segundos; a OS que ele mata dura o turno."""
    modulo = carregar_adapter(nome)

    ok, cliente = _enviar(modulo, [503, 503, 200])

    assert ok is True
    assert len(cliente.chamadas) == 3


@pytest.mark.parametrize("nome", NOMES)
def test_desiste_depois_do_teto_sem_levantar(carregar_adapter, nome):
    """Nunca lança: quem chama é um handler que precisa responder ao simulador,
    e derrubar essa resposta não traria o evento de volta."""
    modulo = carregar_adapter(nome)

    ok, cliente = _enviar(modulo, [TimeoutError("timed out")])

    assert ok is False
    assert len(cliente.chamadas) == modulo._TENTATIVAS_EVENTO


# ── O que NÃO se retenta ──────────────────────────────────────────────────────

@pytest.mark.parametrize("nome", NOMES)
@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_recusa_determinista_nao_e_retentada(carregar_adapter, nome, status):
    """Payload que não passa na validação não passa na terceira tentativa.

    Retentar aqui gasta o dobro do `TIMEOUT_EVENT` e produz três linhas
    idênticas no log para um erro de contrato que aconteceu uma vez.
    """
    modulo = carregar_adapter(nome)

    ok, cliente = _enviar(modulo, [status])

    assert ok is False
    assert len(cliente.chamadas) == 1


@pytest.mark.parametrize("nome", NOMES)
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_status_transitorio_vale_retentar(carregar_adapter, nome, status):
    """5xx é o lado de lá falhando; 408/429 é ele pedindo para esperar."""
    modulo = carregar_adapter(nome)

    assert modulo._vale_retentar(status) is True


@pytest.mark.parametrize("nome", NOMES)
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_status_definitivo_nao_vale_retentar(carregar_adapter, nome, status):
    modulo = carregar_adapter(nome)

    assert modulo._vale_retentar(status) is False


# ── O caminho feliz não pode ter ficado mais caro ─────────────────────────────

@pytest.mark.parametrize("nome,pasta,rota", ADAPTERS)
def test_sucesso_de_primeira_posta_uma_vez_na_rota_certa(carregar_adapter,
                                                         nome, pasta, rota):
    """Cada adapter tem a SUA rota de evento no central — trocá-las mandaria a
    leitura da balança para o handler de visão, que a ignoraria em silêncio."""
    modulo = carregar_adapter(nome)

    ok, cliente = _enviar(modulo, [200])

    assert ok is True
    assert len(cliente.chamadas) == 1
    assert cliente.chamadas[0]["url"] == modulo.CENTRAL_URL + rota
    assert cliente.chamadas[0]["timeout"] == modulo.TIMEOUT_EVENT


# ── A política é a MESMA dos dois lados ───────────────────────────────────────

def test_os_quatro_adapters_usam_a_mesma_politica(carregar_adapter):
    """Quatro cópias do mesmo helper divergem no primeiro ajuste, e a que ficar
    para trás é a do adapter que ninguém está depurando naquele dia."""
    valores = {}
    for nome in NOMES:
        modulo = carregar_adapter(nome)
        valores[nome] = (
            modulo._TENTATIVAS_EVENTO,
            modulo._ESPERA_ENTRE_TENTATIVAS_S,
            tuple(modulo._vale_retentar(s) for s in (409, 422, 500, 503, 408, 429)),
        )

    assert len(set(valores.values())) == 1, valores


def test_criterio_bate_com_o_do_orquestrador(carregar_adapter,
                                             carregar_orquestrador):
    """Ida e volta decidem igual. Se um lado passasse a retentar 409 e o outro
    não, o mesmo `limpeza_em_operacao` custaria 2s num sentido e 0 no outro —
    e quem lesse o log veria dois tempos para a mesma recusa.
    """
    adapter = carregar_adapter("dispenser")
    orq = carregar_orquestrador()

    for status in (400, 404, 409, 422, 408, 429, 500, 502, 503, 504):
        assert adapter._vale_retentar(status) is orq.modulo._vale_retentar(status), status
