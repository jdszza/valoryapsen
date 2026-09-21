# -*- coding: utf-8 -*-
"""A faixa dos campos é conferida NA BORDA — nos três adapters, não em um.

O `cnc-adapter` já validava `dispenser_alvo` e `receita` com 422 na hora, e o
comentário do `_receita_valida` já dizia por quê: deixar passar custaria a ida
à ponta de lá para voltar com o mesmo "não", depois de gastar o relógio que o
orquestrador tem correndo. O `dispenser-adapter` e o `weight-adapter` não
validavam nada — e o primeiro sequer lia `NUM_SLOTS`.

Cada campo torto vira um modo de falhar diferente, e todos longe daqui:

  * **slot fora da faixa, transporte HTTP** — o simulador recusa com 4xx e o
    `_post_sim` traduz para 502: "a ponta de lá recusou", quando quem estava
    errado era o payload desta ponta. O técnico vai olhar o simulador;
  * **slot fora da faixa, transporte SERIAL** — pior: o comando vai e volta
    pelo cabo para receber ACK negativo, queimando o prazo do ACK num comando
    que nunca poderia dar certo;
  * **`quantidade` negativa no `carregar`** — desce até o firmware, que grava
    `sl.quantidade` negativa; daí em diante o `residual` do `dispensado` mente
    para o central, sem erro em lugar nenhum;
  * **`peso_unitario_g` igual a 0** — o peso esperado vira 0 g e a balança
    deixa de poder divergir. A terceira fonte do Triple Check sai de campo em
    silêncio, que é o oposto de uma trava.

`tests/test_contrato_cnc.py` cobre o lado da mesa, que já estava feito; este
arquivo cobre os dois que faltavam e cobra que os três leiam a MESMA faixa.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from conftest import NUM_SLOTS


# ── O dispenser-adapter ───────────────────────────────────────────────────────

def _carregar_bom(adapter, **troca):
    campos = {"dispenser_id": 1, "medicamento": "Dipirona", "sku": "DIP-1",
              "categoria": "analgesico", "quantidade": 5, "os_id": "OS-1"}
    campos.update(troca)
    return adapter.ComandoCarregarReq(**campos)


@pytest.mark.parametrize("slot", [0, -3, NUM_SLOTS + 1, 99])
def test_carregar_recusa_slot_fora_da_celula(carregar_adapter, slot):
    adapter = carregar_adapter("dispenser")

    with pytest.raises(ValidationError):
        _carregar_bom(adapter, dispenser_id=slot)


@pytest.mark.parametrize("slot", [0, -3, NUM_SLOTS + 1])
def test_dispensar_recusa_slot_fora_da_celula(carregar_adapter, slot):
    adapter = carregar_adapter("dispenser")

    with pytest.raises(ValidationError):
        adapter.ComandoDispensarReq(dispenser_id=slot, os_id="OS-1")


@pytest.mark.parametrize("slot", [0, -3, NUM_SLOTS + 1])
def test_limpar_recusa_slot_fora_da_celula(carregar_adapter, slot):
    """`limpar` entra junto: é ela que o reset da planta dispara nos NUM_SLOTS,
    e um slot fora da faixa ali vira falha de reset num slot que não existe."""
    adapter = carregar_adapter("dispenser")

    with pytest.raises(ValidationError):
        adapter.ComandoLimparReq(dispenser_id=slot)


@pytest.mark.parametrize("qtd", [0, -1, 10_000])
def test_carregar_recusa_quantidade_impossivel(carregar_adapter, qtd):
    adapter = carregar_adapter("dispenser")

    with pytest.raises(ValidationError):
        _carregar_bom(adapter, quantidade=qtd)


def test_o_comando_normal_continua_passando(carregar_adapter):
    """Controle: validar demais recusaria o caminho de produção, e o sintoma
    seria a planta inteira parada por 422 em todo carregamento."""
    adapter = carregar_adapter("dispenser")

    req = _carregar_bom(adapter, dispenser_id=NUM_SLOTS, quantidade=15)

    assert (req.dispenser_id, req.quantidade) == (NUM_SLOTS, 15)


def test_a_faixa_do_dispenser_sai_de_NUM_SLOTS(carregar_adapter):
    """Faixa escrita à mão é a mensagem de erro que manda quem lê o log
    procurar o problema no lugar errado. A variável é declarada UMA vez no
    compose e lida por todos."""
    adapter = carregar_adapter("dispenser", env={"NUM_SLOTS": "4"})

    assert adapter.NUM_SLOTS == 4
    with pytest.raises(ValidationError):
        _carregar_bom(adapter, dispenser_id=5)


def test_a_mensagem_de_erro_carrega_a_faixa(carregar_adapter):
    adapter = carregar_adapter("dispenser")

    with pytest.raises(ValidationError, match=f"1-{NUM_SLOTS}"):
        adapter.ComandoDispensarReq(dispenser_id=0, os_id="OS-1")


# ── O weight-adapter ──────────────────────────────────────────────────────────

def _pesar_bom(adapter, **troca):
    campos = {"os_id": "OS-1", "slot_id": 1, "quantidade_esperada": 10,
              "peso_unitario_g": 50.0}
    campos.update(troca)
    return adapter.PesarReq(**campos)


@pytest.mark.parametrize("slot", [0, -3, NUM_SLOTS + 1])
def test_pesar_recusa_slot_fora_da_celula(carregar_adapter, slot):
    adapter = carregar_adapter("weight")

    with pytest.raises(ValidationError):
        _pesar_bom(adapter, slot_id=slot)


@pytest.mark.parametrize("peso", [0, -1, 0.0])
def test_pesar_recusa_peso_unitario_nao_positivo(carregar_adapter, peso):
    """0 g zera o peso ESPERADO, e com ele a capacidade de a balança divergir."""
    adapter = carregar_adapter("weight")

    with pytest.raises(ValidationError):
        _pesar_bom(adapter, peso_unitario_g=peso)


# `PesoUnitarioReq.valor_g` NÃO ganhou validador de modelo, e a ausência é
# decisão: quem recusa `valor_g <= 0` é o handler `bancada_peso_unitario`, e
# `tests/test_balanca_serial.py` já cobre isso — junto com a prova de que o
# comando não chega à placa. Duplicar a regra no modelo mudaria o LUGAR da
# recusa e deixaria a do handler como código morto.


@pytest.mark.parametrize("qtd", [-1, 10_000])
def test_pesar_recusa_quantidade_impossivel(carregar_adapter, qtd):
    adapter = carregar_adapter("weight")

    with pytest.raises(ValidationError):
        _pesar_bom(adapter, quantidade_esperada=qtd)


def test_quantidade_real_ausente_continua_valendo(carregar_adapter):
    """O contrato antigo: sem `quantidade_real`, o caminho inteiro cai em
    `quantidade_esperada`. `None` não é valor fora da faixa."""
    adapter = carregar_adapter("weight")

    req = _pesar_bom(adapter, quantidade_real=None)

    assert req.quantidade_real is None


def test_quantidade_real_zero_e_aceita(carregar_adapter):
    """Zero aqui é um FATO: a falha mecânica em que nada caiu na mesa. Recusá-lo
    faria o caso mais grave ser o único que não chega à balança."""
    adapter = carregar_adapter("weight")

    assert _pesar_bom(adapter, quantidade_real=0).quantidade_real == 0


def test_a_pesagem_normal_continua_passando(carregar_adapter):
    """Controle, pelo mesmo motivo do outro adapter."""
    adapter = carregar_adapter("weight")

    req = _pesar_bom(adapter, slot_id=NUM_SLOTS, quantidade_real=9,
                     peso_unitario_g=0.5)

    assert (req.slot_id, req.quantidade_real, req.peso_unitario_g) == (
        NUM_SLOTS, 9, 0.5)


# ── Os três lêem a mesma faixa ────────────────────────────────────────────────

@pytest.mark.parametrize("sub", ["dispenser", "cnc", "weight"])
def test_os_tres_adapters_leem_NUM_SLOTS_do_ambiente(carregar_adapter, sub):
    """O número tem de ser o MESMO nos cinco serviços que o usam. Um adapter
    com a faixa fixa recusaria o slot 7 numa célula de 8 — e a mensagem
    mandaria o técnico procurar o problema na bancada."""
    adapter = carregar_adapter(sub, env={"NUM_SLOTS": "6"})

    assert adapter.NUM_SLOTS == 6


# ══════════════════════════════════════════════════════════════════════════════
# Transporte serial ligado sem porta fixada RECUSA subir
# ══════════════════════════════════════════════════════════════════════════════
#
# Com a URL vazia o `serial_link` varre TODAS as portas, e cada sondagem abre a
# porta por até ~9,5 s. Na célula montada são CINCO placas e cinco processos:
# enquanto um segura a COM da CNC para conferir, o cnc-adapter toma
# `ACCESS_DENIED` na própria. Não dá erro — dá boot não-determinístico, em que
# uma placa às vezes simplesmente não é achada, e o log de cada processo mostra
# só a metade dele.
#
# Recusar é a resposta certa AQUI: o adapter existe para falar com uma porta, e
# sem ela não faz nada. Serviço que não sobe trava, por `depends_on`, quem
# espera por ele — o que se quer com a bancada mal configurada, em vez de uma OS
# morrendo por timeout num slot íntegro. O painel de bancada faz o OPOSTO, e a
# diferença está registrada em `tests/test_painel_porta.py`.

VARIAVEL = {"dispenser": "DISPENSER_SERIAL_URL", "cnc": "CNC_SERIAL_URL",
            "weight": "WEIGHT_SERIAL_URL"}


@pytest.mark.parametrize("sub", sorted(VARIAVEL))
def test_serial_sem_url_recusa_subir(carregar_adapter, sub):
    adapter = carregar_adapter(sub)

    with pytest.raises(RuntimeError, match=VARIAVEL[sub]):
        adapter._exigir_url("serial", "", VARIAVEL[sub])


@pytest.mark.parametrize("sub", sorted(VARIAVEL))
def test_transporte_http_nao_exige_url(carregar_adapter, sub):
    """Controle, e o que garante que a suíte, o CI e a demonstração em Docker
    não mudam de resultado: o default é `http`, e ali não há porta nenhuma."""
    adapter = carregar_adapter(sub)

    adapter._exigir_url("http", "", VARIAVEL[sub])      # não pode levantar


@pytest.mark.parametrize("sub", sorted(VARIAVEL))
def test_serial_com_url_sobe(carregar_adapter, sub):
    adapter = carregar_adapter(sub)

    adapter._exigir_url("serial", "COM4", VARIAVEL[sub])


def test_a_segunda_porta_do_dispenser_tambem_e_exigida(carregar_adapter):
    """As telas TFT são a segunda porta do MESMO adapter, e ela varre igual."""
    import ast
    import inspect

    adapter = carregar_adapter("dispenser")
    fonte = inspect.getsource(adapter.lifespan)
    variaveis = {no.value for no in ast.walk(ast.parse(fonte))
                 if isinstance(no, ast.Constant) and isinstance(no.value, str)}

    assert "DISPENSER_SERIAL_URL" in variaveis
    assert "DISPENSER_TFT_SERIAL_URL" in variaveis
