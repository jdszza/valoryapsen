# -*- coding: utf-8 -*-
"""O corpo que o orquestrador MANDA, validado pelo modelo que o adapter USA.

Este arquivo existe por causa de um bug que ficou de pé com a suíte inteira
verde: o `cmd_mover` do central passou a postar `{dispenser_alvo, os_id,
receita, ciclo_atual, total_ciclos}` e o `ComandoMoverReq` do cnc-adapter
continuou exigindo `posicao_x`/`posicao_y`. TODO comando de movimento virava
422 antes de tocar em qualquer transporte — com a mesa íntegra, o simulador
íntegro e nenhum teste vermelho.

Ele ficou invisível porque cada lado era testado contra o SEU próprio modelo.
O orquestrador tinha um duplo de `_post` que aceita qualquer dicionário; o
adapter tinha testes que construíam o corpo à mão, do jeito que o adapter
esperava. Duas metades coerentes consigo mesmas e incompatíveis entre si — e o
único lugar onde isso apareceria é o que nenhuma das duas exercitava: a junta.

Por isso aqui não se escreve payload nenhum. O teste CHAMA a função de comando
do orquestrador com o `_post` instrumentado, pega o corpo que ela montou, e o
entrega ao modelo Pydantic do adapter. Um campo renomeado de um lado, um campo
novo obrigatório do outro, e este arquivo fica vermelho na hora — que é o
serviço que ele presta. Para quem for acrescentar um comando: a forma é ligar
os dois lados reais, nunca reescrever o corpo aqui.

O `homing` entra pelo mesmo motivo, embora hoje mande um campo só: é a próxima
rota da mesa a ganhar campo, e o custo de já estar coberta é uma função.
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from conftest import NUM_SLOTS


# ── A junta: o que o orquestrador postou, validado pelo adapter ───────────────

def _corpo_postado(orq, coro) -> tuple[str, dict]:
    """Roda a corrotina de comando e devolve (url, corpo) que ela postou.

    O `AdapterFake` do conftest já grava toda chamada de `_post`; ler dali é o
    que garante que o corpo examinado é o que o código de produção monta, e não
    uma transcrição dele.
    """
    asyncio.run(coro)
    chamadas = orq.adapter.chamadas
    assert chamadas, "o comando não chegou a postar nada"
    ultima = chamadas[-1]
    return ultima["url"], ultima["payload"]


def test_o_corpo_do_mover_passa_no_modelo_do_cnc_adapter(carregar_orquestrador,
                                                         carregar_adapter):
    """A junta que o 422 atravessou sem que nada ficasse vermelho."""
    orq = carregar_orquestrador()
    adapter = carregar_adapter("cnc")

    url, corpo = _corpo_postado(
        orq, orq.modulo.cmd_mover(3, "OS-CONTRATO", "C", 1, 4))

    assert url.endswith("/comandos/mover")
    # Sem try/except: um ValidationError aqui É o resultado do teste, e o
    # relatório do pytest já nomeia o campo que faltou ou sobrou.
    req = adapter.ComandoMoverReq(**corpo)

    assert req.dispenser_alvo == 3
    assert req.os_id == "OS-CONTRATO"
    assert req.receita == "C"
    assert req.ciclo_atual == 1
    assert req.total_ciclos == 4


def test_o_corpo_do_homing_passa_no_modelo_do_cnc_adapter(carregar_orquestrador,
                                                          carregar_adapter):
    orq = carregar_orquestrador()
    adapter = carregar_adapter("cnc")

    url, corpo = _corpo_postado(orq, orq.modulo.cmd_homing("OS-CONTRATO"))

    assert url.endswith("/comandos/homing")
    req = adapter.ComandoHomingReq(**corpo)

    assert req.os_id == "OS-CONTRATO"


def test_o_mover_nao_manda_coordenada(carregar_orquestrador):
    """O endereço da mesa é o dispenser — a posição volta MEDIDA no evento.

    Um par de coordenadas descendo daqui seria um segundo mapa da célula, e os
    dois concordariam só enquanto ninguém regravasse um waypoint na bancada. A
    partir daí a mesa iria para onde o CENTRAL acha que o slot está, e o sintoma
    chegaria como divergência num slot só — o quadro de uma falha mecânica.
    """
    orq = carregar_orquestrador()

    _, corpo = _corpo_postado(
        orq, orq.modulo.cmd_mover(5, "OS-CONTRATO", "A", 2, 2))

    assert "posicao_x" not in corpo
    assert "posicao_y" not in corpo


# ── As recusas que o modelo do adapter faz, e por que são dele ────────────────

@pytest.mark.parametrize("receita", ["", "K", "AA", "c ", "1"],
                         ids=["vazia", "fora_da_faixa", "duas_letras",
                              "minuscula_com_espaco", "digito"])
def test_receita_fora_de_a_j_e_recusada_na_borda(carregar_adapter, receita):
    """A–J é a faixa da NVS da placa: dez slots, um por ordem padrão.

    Recusar aqui é 422 na hora. Deixar passar custaria a ida à placa para voltar
    com `receita_desconhecida` — o mesmo "não", depois de gastar o relógio que o
    orquestrador já tem correndo do outro lado.

    "c " entra na lista de propósito: o validador normaliza antes de julgar, e
    o teste que só passasse "K" não distinguiria normalizar de aceitar qualquer
    coisa.
    """
    adapter = carregar_adapter("cnc")

    if receita == "c ":
        assert adapter.ComandoMoverReq(
            dispenser_alvo=1, os_id="OS-1", receita=receita).receita == "C"
        return

    with pytest.raises(ValidationError):
        adapter.ComandoMoverReq(dispenser_alvo=1, os_id="OS-1", receita=receita)


@pytest.mark.parametrize("alvo", [0, NUM_SLOTS + 1])
def test_dispenser_fora_da_celula_e_recusado_na_borda(carregar_adapter, alvo):
    """Slot inexistente não pode virar comando enviado a um motor que não existe."""
    adapter = carregar_adapter("cnc")

    with pytest.raises(ValidationError):
        adapter.ComandoMoverReq(dispenser_alvo=alvo, os_id="OS-1", receita="A")


def test_coordenada_de_chamador_antigo_e_aceita_e_nao_repassada(carregar_adapter):
    """Aceitar custa nada; repassar custaria o que esta frente tirou do caminho.

    Um 422 num chamador que ainda mande `posicao_x` seria quebrar sem ganho. Já
    RELAYAR o par poria na linha serial um número que o firmware tem de ignorar
    — e "tem de ignorar" é exatamente como dois lados começam a discordar em
    silêncio.
    """
    adapter = carregar_adapter("cnc")

    req = adapter.ComandoMoverReq(dispenser_alvo=2, os_id="OS-1", receita="B",
                                  posicao_x=240.0, posicao_y=-150.0)

    assert req.posicao_x == 240.0        # aceito pelo modelo
    enviados: list[tuple] = []

    async def _fake(comando, payload):
        enviados.append((comando, payload))
        return {"ok": True}

    adapter._enviar = _fake
    asyncio.run(adapter.cmd_mover(req))

    assert enviados[0][0] == "mover"
    assert "posicao_x" not in enviados[0][1]
    assert "posicao_y" not in enviados[0][1]
