"""`/api/resumo` ordena por prioridade — a REAL, não a alfabética.

A query que alimenta a lista de ordens ativas do painel fazia
`ORDER BY prioridade DESC`, e `prioridade` é texto: com os valores
Urgente / Alta / Normal / Baixa, o DESC alfabético produz

    Urgente → Normal → Baixa → Alta

"Alta" caía em ÚLTIMO, atrás de "Baixa". E nada acusava: a lista tinha as
mesmas ordens, só que na sequência errada, e a sequência errada chegava ao
operador da bancada sem erro em lugar nenhum. Hoje o ORDER BY é um `CASE`
explícito construído de `PRIORIDADES`, e prioridade que o banco não conheça
vai para o fim — nunca para o começo.
"""
import pytest


@pytest.fixture
def painel(carregar_painel):
    return carregar_painel()


def _inserir(painel, numero_os: str, prioridade: str, criado: str = "2026-01-01 00:00:00",
             status: str = "Pendente") -> None:
    conn = painel.conexao()
    try:
        conn.execute(
            """INSERT INTO ordens
               (numero_os, itens, destino, prioridade, status,
                data_criacao, data_atualizacao, origem, os_id_central)
               VALUES (?, '[]', 'Bancada', ?, ?, ?, ?, 'local', '')""",
            (numero_os, prioridade, status, criado, criado),
        )
        conn.commit()
    finally:
        conn.close()


def _limpar_ordens(painel) -> None:
    """O seed de demonstração já traz ordens; a ordem aqui tem que ser só a nossa."""
    conn = painel.conexao()
    try:
        conn.execute("DELETE FROM ordens")
        conn.commit()
    finally:
        conn.close()


def _ativas(painel) -> list[str]:
    resposta = painel.api("get", "/api/resumo")
    assert resposta.status_code == 200
    return [o["numero_os"] for o in resposta.get_json()["ordens_ativas"]]


# ── O caso que passava calado ─────────────────────────────────────────────────

def test_alta_vem_antes_de_baixa(painel):
    """Era ao contrário: `DESC` alfabético põe "Baixa" (B) acima de "Alta" (A)."""
    _limpar_ordens(painel)
    _inserir(painel, "OS-BAIXA", "Baixa")
    _inserir(painel, "OS-ALTA", "Alta")

    assert _ativas(painel) == ["OS-ALTA", "OS-BAIXA"]


def test_a_sequencia_inteira_e_urgente_alta_normal_baixa(painel):
    _limpar_ordens(painel)
    # Inseridas de propósito na ordem alfabética inversa (a que o bug produzia).
    _inserir(painel, "OS-U", "Urgente")
    _inserir(painel, "OS-N", "Normal")
    _inserir(painel, "OS-B", "Baixa")
    _inserir(painel, "OS-A", "Alta")

    assert _ativas(painel) == ["OS-U", "OS-A", "OS-N", "OS-B"]


def test_a_ordem_e_a_de_prioridades(painel):
    """A tupla é a fonte: o CASE sai dela, e o teste acima confere o resultado.
    Se alguém reordenar a tupla, este teste diz que a ordem mudou de propósito."""
    assert painel.modulo.PRIORIDADES == ("Urgente", "Alta", "Normal", "Baixa")


# ── Desempate e desconhecido ──────────────────────────────────────────────────

def test_mesma_prioridade_desempata_pela_mais_antiga(painel):
    _limpar_ordens(painel)
    _inserir(painel, "OS-NOVA", "Normal", criado="2026-01-02 00:00:00")
    _inserir(painel, "OS-VELHA", "Normal", criado="2026-01-01 00:00:00")

    assert _ativas(painel) == ["OS-VELHA", "OS-NOVA"]


def test_prioridade_desconhecida_vai_para_o_fim(painel):
    """Um valor que o banco não conheça não pode furar a fila de quem é urgente.

    O `ELSE` do CASE é o fim da lista: mais seguro deixar uma ordem esquisita
    esperando do que despachá-la na frente de uma Urgente.
    """
    _limpar_ordens(painel)
    _inserir(painel, "OS-ESQUISITA", "Critica")      # não existe no vocabulário
    _inserir(painel, "OS-BAIXA", "Baixa")
    _inserir(painel, "OS-URGENTE", "Urgente")

    assert _ativas(painel) == ["OS-URGENTE", "OS-BAIXA", "OS-ESQUISITA"]


def test_o_case_cobre_toda_prioridade_da_tupla(painel):
    """Guarda do helper: cada prioridade tem um `WHEN`, e o `ELSE` é o último lugar."""
    sql = painel.modulo._sql_ordem_prioridade()
    for indice, prioridade in enumerate(painel.modulo.PRIORIDADES):
        assert f"WHEN '{prioridade}' THEN {indice}" in sql
    assert sql.rstrip().endswith(f"ELSE {len(painel.modulo.PRIORIDADES)} END")
    assert "DESC" not in sql
