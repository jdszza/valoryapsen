"""Leitura do computador central — a única porta de entrada de dado remoto.

O painel de bancada ESPELHA o central: ele mostra as ordens e o estoque dos
slots que a célula está executando, e não comanda nada. Por isso este módulo só
tem GETs. Não existe aqui — nem deve passar a existir — nenhuma função que
escreva no central: o caminho de escrita (`PUT /ordens/{os_id}/status`) grava a
coluna `status` do banco sem falar com o orquestrador, então um botão do painel
ligado nele mudaria a linha do banco enquanto a célula continua fazendo outra
coisa. Espelho que escreve é espelho que mente.

**Nenhuma função levanta.** Timeout, conexão recusada, JSON inválido e status
diferente de 200 viram log e retorno vazio (`None` / `[]`). Não é preguiça de
tratar erro: quem chama daqui é o processo que possui a porta serial do display
e serve a tela que o operador está olhando. Uma exceção de rede que suba não
derruba "a sincronização" — derruba a thread, e com ela o painel inteiro.

Um único ponto de I/O, como o `_fetch` do dashboard: `_get` faz a requisição e
quem chama recebe dado já pronto.
"""
import os

import requests


def _flag(nome: str, padrao: str) -> bool:
    return os.environ.get(nome, padrao).strip().lower() not in ("0", "false", "nao", "no")


def _num(nome: str, padrao: float) -> float:
    """Valor inválido cai no padrão em vez de derrubar o import.

    O painel roda na bancada, iniciado por um `.bat`. `CENTRAL_TIMEOUT_S=três`
    não pode ser a diferença entre ter e não ter painel.
    """
    bruto = os.environ.get(nome, "")
    try:
        valor = float(bruto)
    except ValueError:
        if bruto:
            print(f"[central] {nome}={bruto!r} inválido — usando {padrao}")
        return padrao
    return valor if valor > 0 else padrao


CENTRAL_URL = os.environ.get("CENTRAL_URL", "http://localhost:8000").rstrip("/")
CENTRAL_TIMEOUT_S = _num("CENTRAL_TIMEOUT_S", 3.0)
CENTRAL_SYNC_S = _num("CENTRAL_SYNC_S", 5.0)

# `PAINEL_CENTRAL=0` desliga a integração inteira: nenhuma requisição sai e o
# painel volta a se comportar como antes desta integração, 100% local. A
# bancada precisa funcionar com o central desligado — em feira, em treinamento
# e no dia em que o Docker não sobe.
INTEGRACAO_ATIVA = _flag("PAINEL_CENTRAL", "1")


# ── Vocabulário de status ─────────────────────────────────────────────────────

# A ÚNICA tradução entre os dois vocabulários. Central à esquerda, painel à
# direita. Espalhar essa correspondência por rota e template é o que faz um
# status novo aparecer traduzido em uma tela e cru na seguinte.
STATUS_CENTRAL_PARA_PAINEL = {
    "aguardando":   "Pendente",
    "em_andamento": "Em Processo",
    "concluida":    "Concluido",
    "erro":         "Erro",
    "cancelada":    "Cancelado",
}

# Onde cai um status que o central passe a emitir e este mapa ainda não conheça.
# "Pendente" seria pior: um estado terminal desconhecido apareceria como fila,
# e o operador ficaria esperando a célula executar uma ordem que já acabou.
STATUS_DESCONHECIDO = "Erro"


def traduzir_status(status_central: str) -> str:
    bruto = (status_central or "").strip().lower()
    traduzido = STATUS_CENTRAL_PARA_PAINEL.get(bruto)
    if traduzido is None:
        print(f"[central] status desconhecido {status_central!r} — tratado como {STATUS_DESCONHECIDO}")
        return STATUS_DESCONHECIDO
    return traduzido


# ── Transporte ────────────────────────────────────────────────────────────────

def _get(caminho: str, params: dict | None = None):
    """GET no central. Devolve o JSON decodificado ou `None` em qualquer falha.

    O `except Exception` é deliberado e é o contrato do módulo: a lista de
    exceções possíveis atravessa `requests`, `urllib3`, `socket`, `ssl` e o
    decodificador de JSON, e basta uma escapar para levar junto a thread de
    sincronização. Ver o docstring do módulo.
    """
    if not INTEGRACAO_ATIVA:
        return None
    url = f"{CENTRAL_URL}{caminho}"
    try:
        resposta = requests.get(url, params=params, timeout=CENTRAL_TIMEOUT_S)
        if resposta.status_code != 200:
            print(f"[central] {caminho} respondeu HTTP {resposta.status_code}")
            return None
        return resposta.json()
    except Exception as exc:
        print(f"[central] {caminho} indisponível: {exc}")
        return None


def disponivel() -> bool:
    """O central responde agora? Usado para decidir entre espelho e fallback."""
    return _get("/estado") is not None


def listar_ordens(limite: int = 20) -> list:
    dados = _get("/os/historico", {"limite": limite})
    return dados if isinstance(dados, list) else []


def ordem_detalhe(os_id: str) -> dict | None:
    dados = _get(f"/os/{os_id}")
    return dados if isinstance(dados, dict) else None


def ordem_ativa() -> dict | None:
    dados = _get("/os/ativa")
    if not isinstance(dados, dict):
        return None
    ativa = dados.get("os_ativa")
    return ativa if isinstance(ativa, dict) else None


def dispensers_estado() -> list:
    """Uma linha por slot físico da célula: quem manda no estoque é quem o mede."""
    dados = _get("/dispensers/estado")
    return dados if isinstance(dados, list) else []


def trava_estado() -> dict | None:
    """A trava do Triple Check, como o central a publica (`GET /api/v1/trava`,
    sem autenticação). `None` = central sem responder — que é diferente de
    "sem trava", e quem chama precisa distinguir os dois.

    A LEITURA mora aqui, junto com os outros GETs. A ESCRITA (liberar a trava)
    não mora, de propósito: ela é a única exceção ao espelho de mão única e
    vive sozinha em `central_comandos.py`, onde dá para ver todas as escritas
    de uma vez.
    """
    dados = _get("/api/v1/trava")
    return dados if isinstance(dados, dict) else None
