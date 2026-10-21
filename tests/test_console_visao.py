# -*- coding: utf-8 -*-
"""O switch "Visão no Triple Check" na página do console.

A página não roda num browser na suíte: o que se cobra é o CONTRATO entre ela
e o central — o elemento acessível existe, o JS fala com as duas rotas, e a
faixa que avisa a planta rodando sem a visão está lá para ser mostrada.
"""
from test_console import console, logado  # noqa: F401 — fixtures reaproveitadas


def _pagina(logado) -> str:  # noqa: F811
    resposta = logado.cliente.get("/console")
    assert resposta.status_code == 200
    return resposta.text


def test_pagina_tem_o_switch_acessivel(logado):  # noqa: F811
    html = _pagina(logado)
    assert 'id="chaveVisao"' in html
    assert 'role="switch"' in html
    assert 'aria-checked=' in html
    # <button>: Enter e Espaço funcionam sem código extra.
    assert '<button type="button" class="chave" id="chaveVisao"' in html


def test_js_le_e_muda_pelo_endpoint_do_switch(logado):  # noqa: F811
    html = _pagina(logado)
    assert 'fetch("/console/api/visao-triple-check"' in html            # GET
    assert '"/console/api/visao-triple-check", { ativo: alvo }' in html  # POST
    # O estado ao vivo vem do /ws que a página já escuta — sem timer novo.
    assert "st.visao_triple_check" in html
    assert html.count("setInterval(") == 0


def test_faixa_e_aviso_da_trava_estao_na_pagina(logado):  # noqa: F811
    html = _pagina(logado)
    assert "VISÃO FORA DO TRIPLE CHECK" in html
    assert "câmeras não travam a OS; balança e dispenser continuam travando" in html
    assert "leituras ignoradas nesta OS" in html
    assert "a trava atual continua; libere pelo botão de sempre" in html


def test_erro_do_backend_volta_o_switch_ao_estado_anterior(logado):  # noqa: F811
    """Sem browser, o que dá para cobrar é que o ramo de erro reaplica o valor
    anterior e que o switch fica desabilitado enquanto o POST não volta."""
    html = _pagina(logado)
    assert "aplicarVisaoTC({ ativo: anterior })" in html
    assert "chave.disabled = true;" in html
