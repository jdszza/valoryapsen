"""Escrita no computador central — a ÚNICA, e ela é nomeada.

O painel de bancada ESPELHA o central: `central_client.py` só tem `GET`, há
teste que reprova um `requests.post` lá dentro, e o motivo está no docstring
dele — o caminho de escrita óbvio (`PUT /ordens/{os_id}/status`) grava a coluna
do banco sem falar com o orquestrador, e um espelho que escreve é um espelho
que mente.

Este módulo existe SEPARADO porque a liberação da trava do Triple Check é a
única exceção a essa regra — deliberada, nomeada e num arquivo em que dá para
ver todas as escritas de uma vez. O espelho de ordens e estoque continua de mão
única; nada aqui toca em ordem, slot ou status. O que este arquivo faz, e é só
isto:

  * autentica no central com uma CONTA DE SERVIÇO (`PAINEL_CENTRAL_USER` /
    `PAINEL_CENTRAL_SENHA`, role `supervisor` no central), guarda o JWT (o
    central emite com 8 h) e refaz o login quando ele expira ou o central
    responde 401;
  * chama `POST /api/v1/admin/liberar-trava` com `em_nome_de` = o nome do
    supervisor que digitou o PIN aqui na bancada. Sem isso toda liberação vinda
    do painel apareceria no log do central com o nome da conta de serviço, e o
    rastro de QUEM liberou — o ponto inteiro de existir uma trava — se
    perderia.

Quem acrescentar uma segunda escrita acrescenta AQUI, com o motivo — nunca em
`central_client.py`.

**Variáveis ausentes = funcionalidade desligada, com mensagem clara.** É a
mesma divisão do `APSEN_API_TOKEN`: sem a conta de serviço, só a liberação da
trava fica sem dono; o processo, a ponte serial e o espelho sobem iguais.
Derrubar o painel inteiro por uma variável que a bancada talvez nem use
levaria junto a tela que o operador está olhando.

**Nenhuma função levanta.** Quem chama daqui é a thread que serve a tela que o
operador está olhando (a rota web) e, depois, a ponte serial do display. Uma
exceção de rede que suba não derruba "a liberação" — derruba a thread. Toda
função devolve `(ok, mensagem)`, e a mensagem é para o operador ler.
"""
import os
import threading
import time

import requests

import central_client

PAINEL_CENTRAL_USER = os.environ.get("PAINEL_CENTRAL_USER", "").strip()
PAINEL_CENTRAL_SENHA = os.environ.get("PAINEL_CENTRAL_SENHA", "")

# O central emite o JWT com 8 h. Renovar um pouco ANTES evita a última
# liberação do turno cair justamente no 401 da expiração — que é retentado uma
# vez, mas retentar custa um login a mais no exato momento em que a produção
# está parada esperando.
JWT_VALIDADE_S = 8 * 3600
JWT_MARGEM_S = 30 * 60

MSG_DESLIGADO = ("liberação de trava pelo painel DESLIGADA: defina "
                 "PAINEL_CENTRAL_USER e PAINEL_CENTRAL_SENHA (conta de serviço "
                 "com role supervisor no central)")
MSG_INTEGRACAO_OFF = "PAINEL_CENTRAL=0 — o painel está sem integração com o central"

_token_lock = threading.Lock()
_token = {"jwt": None, "obtido_em": 0.0}


def habilitado() -> bool:
    """A conta de serviço está configurada E a integração está ligada?"""
    return bool(PAINEL_CENTRAL_USER and PAINEL_CENTRAL_SENHA) and central_client.INTEGRACAO_ATIVA


def motivo_desligado() -> str:
    """A frase que a tela mostra no lugar do botão quando `habilitado()` é False."""
    if not central_client.INTEGRACAO_ATIVA:
        return MSG_INTEGRACAO_OFF
    return MSG_DESLIGADO


# ── Sessão na conta de serviço ────────────────────────────────────────────────

def _login() -> str | None:
    """Um JWT novo, ou None. Nunca levanta; a razão vai para o log."""
    url = f"{central_client.CENTRAL_URL}/auth/login"
    try:
        resposta = requests.post(
            url, json={"username": PAINEL_CENTRAL_USER, "senha": PAINEL_CENTRAL_SENHA},
            timeout=central_client.CENTRAL_TIMEOUT_S,
        )
        if resposta.status_code != 200:
            print(f"[central] login da conta de serviço respondeu HTTP {resposta.status_code}")
            return None
        token = (resposta.json() or {}).get("token")
        if not token:
            print("[central] login da conta de serviço veio sem token")
            return None
        return token
    except Exception as exc:
        print(f"[central] login da conta de serviço indisponível: {exc}")
        return None


def _jwt(forcar: bool = False) -> str | None:
    """O JWT em cache, renovado quando expirou ou quando `forcar` (após 401)."""
    with _token_lock:
        vivo = (_token["jwt"] is not None
                and (time.time() - _token["obtido_em"]) < (JWT_VALIDADE_S - JWT_MARGEM_S))
        if vivo and not forcar:
            return _token["jwt"]
        novo = _login()
        _token["jwt"] = novo
        _token["obtido_em"] = time.time() if novo else 0.0
        return novo


def esquecer_sessao() -> None:
    """Zera o cache do JWT (teste e troca de credencial em tempo de execução)."""
    with _token_lock:
        _token["jwt"] = None
        _token["obtido_em"] = 0.0


# ── A escrita ─────────────────────────────────────────────────────────────────

def _post_liberar(token: str, em_nome_de: str):
    return requests.post(
        f"{central_client.CENTRAL_URL}/api/v1/admin/liberar-trava",
        json={"em_nome_de": em_nome_de},
        headers={"Authorization": f"Bearer {token}"},
        timeout=central_client.CENTRAL_TIMEOUT_S,
    )


def liberar_trava(em_nome_de: str) -> tuple[bool, str]:
    """Pede ao central para liberar a trava, em nome de quem digitou o PIN.

    Devolve `(ok, mensagem)`. `ok=False` cobre tudo que não é 200 — sem
    credencial, central fora, 401 mesmo depois de refazer o login, 403 (a
    conta de serviço não tem a role), 409 (não havia trava) — e a mensagem diz
    qual. Nunca levanta.
    """
    if not habilitado():
        return False, motivo_desligado()
    nome = (em_nome_de or "").strip() or "operador do painel"

    token = _jwt()
    if token is None:
        return False, "central indisponível ou credenciais da conta de serviço recusadas"

    try:
        resposta = _post_liberar(token, nome)
        if resposta.status_code == 401:
            # Token vencido ou revogado (senha trocada, conta desativada e
            # reativada): UM login novo e UMA repetição — não um laço.
            token = _jwt(forcar=True)
            if token is None:
                return False, "central recusou a conta de serviço (401) e o novo login falhou"
            resposta = _post_liberar(token, nome)
    except Exception as exc:
        return False, f"central indisponível: {exc}"

    status = resposta.status_code
    if status == 200:
        return True, f"trava liberada em nome de {nome}"
    if status == 401:
        return False, "central recusou a conta de serviço (401)"
    if status == 403:
        return False, ("conta de serviço sem permissão para liberar (403): "
                       "no central, ela precisa da role supervisor ou admin")
    if status == 409:
        return False, "nenhuma trava ativa no central (409)"
    return False, f"central respondeu HTTP {status}"
