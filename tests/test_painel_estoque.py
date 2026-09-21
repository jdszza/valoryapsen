"""Bloquear um lote tem que TIRAR o saldo dele do estoque dispensável.

O painel guarda o estoque em dois lugares: `lotes.quantidade`, linha a linha
com validade e fornecedor, e `medicamentos.quantidade`, o agregado que toda
tela mostra e que `verificar_estoque_ordem` consulta para liberar uma ordem.

`bloquear_lote` mexia só em `lotes.status`. O agregado seguia contando o saldo
bloqueado, e a partir daí a cadeia inteira funcionava — sem erro em lugar
nenhum:

    bloqueia o lote            → lotes.status='Bloqueado', agregado intacto
    verificar_estoque_ordem    → vê o agregado, libera a ordem
    consumir_fefo              → procura lote 'Ativo', não acha
                               → cai no ramo do resíduo
                               → grava "SEM LOTE REGISTRADO"
                               → debita o agregado assim mesmo

Resultado: o medicamento sai, e sai justamente do lote que a tela dizia ter
bloqueado — sem genealogia, que é o oposto do que a tela promete. Num painel
cuja razão de existir é rastreabilidade de lote, é o pior modo de falhar:
bloquear "funciona" na tela e não protege nada.

O conserto não foi derivar o agregado de `SUM(lotes)` — `medicamentos.quantidade`
é um número MEDIDO (visão, espelho do central, cadastro), não uma soma de lotes,
e estoque sem lote cadastrado é legítimo. O bloco de comentário no topo da
seção de lotes em `app.py` registra o porquê. O que os dois números ganharam
foi uma invariante declarada — o agregado conta só saldo DISPENSÁVEL — e um
dono único, `_mover_saldo_lote`, por onde passa toda transição de status.
"""
from datetime import date, timedelta

import pytest


MED = "Dipirona 500mg"


@pytest.fixture
def painel(carregar_painel):
    return carregar_painel()


# ── Aparato: um medicamento com estoque 100% rastreado por lote ───────────────

def _zerar_medicamento(painel, nome: str = MED) -> int:
    """Deixa o medicamento com agregado 0 e sem lote — a folha em branco.

    O seed de demonstração cria lotes e ordens concluídas; partir dele faria
    cada teste afirmar números que dependem da data de hoje.
    """
    conn = painel.conexao()
    try:
        med_id = conn.execute(
            "SELECT id FROM medicamentos WHERE nome=?", (nome,)
        ).fetchone()["id"]
        conn.execute("DELETE FROM lotes WHERE medicamento_id=?", (med_id,))
        conn.execute("UPDATE medicamentos SET quantidade=0 WHERE id=?", (med_id,))
        conn.commit()
        return med_id
    finally:
        conn.close()


def daqui_a(dias: int) -> str:
    """Validade RELATIVA a hoje, e nunca uma data escrita à mão.

    Data literal no fixture envelhece sozinha, e o vermelho chega meses depois
    parecendo bug de código: `L-VELHO` vencia em 2026-03-01 e, a partir daquele
    dia, os testes de BLOQUEIO passaram a depender de um lote que a varredura de
    vencidos tira de circulação — o assunto errado, no arquivo errado. Quem
    testa vencimento pede um número negativo aqui, de propósito.
    """
    return (date.today() + timedelta(days=dias)).strftime("%Y-%m-%d")


def _entrada(painel, med_id: int, lote: str, qtd: int, validade: str) -> int:
    """Entrada de lote pelo caminho de produção — ele já soma no agregado."""
    conn = painel.conexao()
    try:
        painel.modulo._registrar_entrada_lote(
            conn, med_id, lote, validade, qtd, "Fornecedor", "NF-1",
            daqui_a(-365), "Teste", "Admin",   # data de FABRICAÇÃO
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM lotes WHERE medicamento_id=? AND lote=?", (med_id, lote)
        ).fetchone()["id"]
    finally:
        conn.close()


def _agregado(painel, med_id: int) -> int:
    conn = painel.conexao()
    try:
        return conn.execute(
            "SELECT quantidade FROM medicamentos WHERE id=?", (med_id,)
        ).fetchone()["quantidade"]
    finally:
        conn.close()


def _lote(painel, lote_id: int):
    conn = painel.conexao()
    try:
        return conn.execute("SELECT * FROM lotes WHERE id=?", (lote_id,)).fetchone()
    finally:
        conn.close()


def _saldo_ativo(painel, med_id: int) -> int:
    """SUM(lotes ativos) — a soma com que o agregado tem que concordar."""
    conn = painel.conexao()
    try:
        return conn.execute(
            "SELECT COALESCE(SUM(quantidade), 0) AS s FROM lotes "
            "WHERE medicamento_id=? AND status='Ativo'",
            (med_id,),
        ).fetchone()["s"]
    finally:
        conn.close()


def _bloquear(painel, lote_id: int):
    painel.logar()
    return painel.cliente.post(f"/lotes/{lote_id}/bloquear")


@pytest.fixture
def med_com_dois_lotes(painel):
    """40 unidades em dois lotes: L-VELHO (15, vence antes) e L-NOVO (25).

    Os dois VÁLIDOS — "velho" aqui quer dizer "vence primeiro", que é o que o
    FEFO ordena. Vencido é outro assunto, e tem bloco próprio no fim deste
    arquivo.
    """
    med_id = _zerar_medicamento(painel)
    velho = _entrada(painel, med_id, "L-VELHO", 15, daqui_a(30))
    novo  = _entrada(painel, med_id, "L-NOVO",  25, daqui_a(365))
    assert _agregado(painel, med_id) == 40
    return med_id, velho, novo


# ── 1. O saldo acompanha o status ─────────────────────────────────────────────

def test_bloquear_tira_o_saldo_do_agregado(painel, med_com_dois_lotes):
    med_id, velho, _ = med_com_dois_lotes

    _bloquear(painel, velho)

    assert _lote(painel, velho)["status"] == "Bloqueado"
    assert _agregado(painel, med_id) == 25


def test_desbloquear_devolve_o_saldo(painel, med_com_dois_lotes):
    """O botão é um toggle: a volta tem que ser exata, ou o estoque derrete."""
    med_id, velho, _ = med_com_dois_lotes

    _bloquear(painel, velho)
    _bloquear(painel, velho)

    assert _lote(painel, velho)["status"] == "Ativo"
    assert _agregado(painel, med_id) == 40


def test_bloquear_e_desbloquear_muitas_vezes_nao_acumula(painel, med_com_dois_lotes):
    """`_mover_saldo_lote` soma e subtrai — não reconcilia.

    Um toggle que somasse duas vezes ou esquecesse de subtrair uma produziria
    estoque do nada, e o número só ficaria visivelmente errado depois de vários
    cliques — quando ninguém mais liga um ao outro.
    """
    med_id, velho, _ = med_com_dois_lotes

    for _ in range(5):
        _bloquear(painel, velho)
        assert _agregado(painel, med_id) == 25
        _bloquear(painel, velho)
        assert _agregado(painel, med_id) == 40


def test_o_agregado_concorda_com_a_soma_dos_lotes_ativos(painel, med_com_dois_lotes):
    """A invariante, afirmada diretamente.

    Este medicamento tem 100% do estoque rastreado por lote, então aqui as duas
    contas TÊM que bater. (Não é verdade em geral: estoque sem lote cadastrado
    é legítimo e é por isso que o agregado não é derivado — ver o cabeçalho.)
    """
    med_id, velho, novo = med_com_dois_lotes

    for lote_id in (velho, novo, velho):
        _bloquear(painel, lote_id)
        assert _agregado(painel, med_id) == _saldo_ativo(painel, med_id)


# ── 2. O caso que passava calado: a dispensa do lote bloqueado ────────────────

def _ordem_de(painel, numero_os: str, qtd: int) -> int:
    return painel.criar_ordem_local(numero_os, [{"med": MED, "qtd": qtd}])


def _concluir(painel, ordem_id: int):
    painel.logar()
    return painel.cliente.post(f"/ordens/{ordem_id}/status/Concluido")


def _genealogia(painel, numero_os: str) -> list:
    conn = painel.conexao()
    try:
        return conn.execute(
            "SELECT lote, quantidade FROM ordem_lotes_consumidos WHERE numero_os=? "
            "ORDER BY id", (numero_os,)
        ).fetchall()
    finally:
        conn.close()


def test_ordem_que_so_cabe_no_lote_bloqueado_e_recusada(painel, med_com_dois_lotes):
    """O teste do sintoma inteiro, do bloqueio até a recusa.

    Com os 25 do L-NOVO bloqueados, restam 15 dispensáveis. Uma ordem de 30
    cabia no agregado velho (40) e era liberada; hoje ela para em
    `verificar_estoque_ordem`, que é onde a decisão pertence.
    """
    med_id, _, novo = med_com_dois_lotes
    _bloquear(painel, novo)
    ordem_id = _ordem_de(painel, "OS-BLOQ", 30)

    painel.logar()
    resposta = painel.cliente.post(f"/ordens/{ordem_id}/status/Em Processo",
                                   follow_redirects=True)

    assert resposta.status_code == 200
    assert painel.ordem("OS-BLOQ")["status"] == "Pendente"
    assert _agregado(painel, med_id) == 15


def test_lote_bloqueado_nao_e_consumido_como_sem_lote_registrado(painel,
                                                                 med_com_dois_lotes):
    """O ramo do resíduo era a porta dos fundos por onde o lote bloqueado saía.

    Concluir a ordem chama `consumir_fefo`. Com o L-VELHO bloqueado, FEFO tem
    que pular para o L-NOVO — e NÃO gravar "SEM LOTE REGISTRADO", que é a
    assinatura de uma dispensa sem genealogia.
    """
    med_id, velho, _ = med_com_dois_lotes
    _bloquear(painel, velho)
    ordem_id = _ordem_de(painel, "OS-FEFO", 10)

    _concluir(painel, ordem_id)

    consumos = [(r["lote"], r["quantidade"]) for r in _genealogia(painel, "OS-FEFO")]
    assert consumos == [("L-NOVO", 10)]
    assert _lote(painel, velho)["quantidade"] == 15   # intocado
    assert _agregado(painel, med_id) == 15            # 25 ativos − 10 consumidos


def test_lote_bloqueado_nao_e_o_primeiro_da_fila_fefo(painel, med_com_dois_lotes):
    """Sem bloqueio, o L-VELHO vem primeiro — é o que o bloqueio tem de inverter.

    Sem esta contraprova, o teste acima passaria também se o FEFO estivesse
    simplesmente ignorando a validade.
    """
    _, _, _ = med_com_dois_lotes
    ordem_id = _ordem_de(painel, "OS-SEM-BLOQ", 10)

    _concluir(painel, ordem_id)

    consumos = [(r["lote"], r["quantidade"]) for r in _genealogia(painel, "OS-SEM-BLOQ")]
    assert consumos == [("L-VELHO", 10)]


# ── 3. A baixa manual não pode descontar duas vezes ───────────────────────────

def test_baixa_de_lote_ativo_desconta_do_agregado(painel, med_com_dois_lotes):
    med_id, velho, _ = med_com_dois_lotes
    painel.logar()

    painel.cliente.post(f"/lotes/{velho}/baixa", data={"motivo": "Avariado"})

    assert _lote(painel, velho)["status"] == "Baixado"
    assert _agregado(painel, med_id) == 25


def test_baixa_de_lote_bloqueado_nao_desconta_de_novo(painel, med_com_dois_lotes):
    """O outro lado de "dois números que precisam concordar".

    O saldo do lote bloqueado já saiu do agregado no bloqueio. Descontá-lo
    outra vez na baixa apagaria do estoque unidades que estão na prateleira — e
    o caminho bloquear→baixar é o normal: bloqueia-se para investigar, baixa-se
    quando se confirma a perda.
    """
    med_id, velho, _ = med_com_dois_lotes
    _bloquear(painel, velho)
    assert _agregado(painel, med_id) == 25
    painel.logar()

    painel.cliente.post(f"/lotes/{velho}/baixa", data={"motivo": "Vencido"})

    assert _lote(painel, velho)["status"] == "Baixado"
    assert _agregado(painel, med_id) == 25
    assert _agregado(painel, med_id) == _saldo_ativo(painel, med_id)


def test_lote_baixado_nao_volta_a_ativo_pelo_botao_de_bloquear(painel,
                                                               med_com_dois_lotes):
    """Lote que acabou não é lote bloqueado, e o toggle não os confunde."""
    med_id, velho, _ = med_com_dois_lotes
    painel.logar()
    painel.cliente.post(f"/lotes/{velho}/baixa", data={"motivo": "Avariado"})

    _bloquear(painel, velho)

    assert _lote(painel, velho)["status"] == "Baixado"
    assert _agregado(painel, med_id) == 25


# ── 4. O que NÃO mudou ────────────────────────────────────────────────────────

def test_entrada_de_lote_continua_somando_no_agregado(painel):
    med_id = _zerar_medicamento(painel)

    _entrada(painel, med_id, "L-1", 30, daqui_a(365))

    assert _agregado(painel, med_id) == 30
    assert _agregado(painel, med_id) == _saldo_ativo(painel, med_id)


def test_estoque_sem_lote_continua_dispensavel(painel):
    """É por isso que o agregado não pode ser derivado de SUM(lotes).

    Estoque legado — sem lote cadastrado — é real, e o ramo
    "SEM LOTE REGISTRADO" de `consumir_fefo` existe para cobri-lo. Derivar o
    agregado faria este medicamento ler zero e recusaria toda ordem sobre uma
    prateleira fisicamente cheia.
    """
    med_id = _zerar_medicamento(painel)
    conn = painel.conexao()
    try:
        conn.execute("UPDATE medicamentos SET quantidade=50 WHERE id=?", (med_id,))
        conn.commit()
    finally:
        conn.close()
    ordem_id = _ordem_de(painel, "OS-LEGADO", 10)

    _concluir(painel, ordem_id)

    consumos = [(r["lote"], r["quantidade"])
                for r in _genealogia(painel, "OS-LEGADO")]
    assert consumos == [("SEM LOTE REGISTRADO", 10)]
    assert _agregado(painel, med_id) == 40


# ── 4. Lote VENCIDO: o FEFO o preferia, por construção ───────────────────────
#
# FEFO é "vence primeiro, sai primeiro". Sem filtro de validade, um lote já
# vencido é, por definição, o que vence primeiro de TODOS — então ele não era
# apenas aceito: era escolhido na frente do lote bom. E `verificar_estoque_ordem`
# não ajudava, porque consulta `medicamentos.quantidade`, que somava o saldo
# vencido junto.
#
# O único freio era manual: `lotes_proximos_vencimento` pinta o alerta no
# dashboard e alguém precisa lembrar de dar `baixa_lote`. Na bancada ninguém
# lembrou por dois meses — seis lotes vencidos em 26/07, 286 unidades ainda
# dispensáveis.

def _desvios(painel, numero_os: str = None) -> list:
    conn = painel.conexao()
    try:
        if numero_os is None:
            return conn.execute("SELECT * FROM desvios").fetchall()
        return conn.execute(
            "SELECT * FROM desvios WHERE numero_os=?", (numero_os,)
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def med_com_vencido_e_valido(painel):
    """40 unidades: L-VENCIDO (15, venceu ontem) e L-VALIDO (25)."""
    med_id = _zerar_medicamento(painel)
    vencido = _entrada(painel, med_id, "L-VENCIDO", 15, daqui_a(-1))
    valido = _entrada(painel, med_id, "L-VALIDO", 25, daqui_a(365))
    return med_id, vencido, valido


def test_o_fefo_consome_o_valido_e_nao_o_vencido(painel, med_com_vencido_e_valido):
    """O teste que o achado pede: vencido + válido ⇒ sai o VÁLIDO."""
    _, _, _ = med_com_vencido_e_valido
    ordem_id = _ordem_de(painel, "OS-VENC", 10)

    _concluir(painel, ordem_id)

    consumos = [(r["lote"], r["quantidade"]) for r in _genealogia(painel, "OS-VENC")]
    assert consumos == [("L-VALIDO", 10)]


def test_o_vencido_sai_do_saldo_dispensavel(painel, med_com_vencido_e_valido):
    """A metade que faz `verificar_estoque_ordem` parar de mentir: o agregado
    deixa de contar o que não pode sair."""
    med_id, vencido, _ = med_com_vencido_e_valido

    conn = painel.conexao()
    try:
        painel.modulo.expirar_lotes_vencidos(conn)
        conn.commit()
    finally:
        conn.close()

    assert _lote(painel, vencido)["status"] == "Vencido"
    assert _agregado(painel, med_id) == 25


def test_a_varredura_e_idempotente(painel, med_com_vencido_e_valido):
    """`_mover_saldo_lote` soma e subtrai, não reconcilia: uma segunda passada
    que achasse o mesmo lote zeraria um estoque que existe."""
    med_id, _, _ = med_com_vencido_e_valido

    for _ in range(3):
        conn = painel.conexao()
        try:
            painel.modulo.expirar_lotes_vencidos(conn)
            conn.commit()
        finally:
            conn.close()

    assert _agregado(painel, med_id) == 25


def test_ordem_que_so_cabe_no_vencido_e_recusada(painel, med_com_vencido_e_valido):
    """Com o agregado corrigido, a ordem não chega nem a começar — que é o
    lugar certo de recusar. Antes ela era liberada, caía no ramo do resíduo e
    o medicamento saía do lote vencido sem genealogia nenhuma."""
    med_id, _, _ = med_com_vencido_e_valido
    ordem_id = _ordem_de(painel, "OS-SO-VENCIDO", 30)   # 30 > 25 válidos

    painel.logar()
    painel.cliente.post(f"/ordens/{ordem_id}/status/Em Processo")

    assert painel.ordem("OS-SO-VENCIDO")["status"] == "Pendente"
    assert _agregado(painel, med_id) == 25


def test_consumo_sem_lote_valido_abre_desvio(painel):
    """Consumir sem lote rastreável sempre foi legítimo (estoque anterior ao
    cadastro de lotes existe), mas desde que o vencido deixou de ser escolhido
    ele é TAMBÉM o sintoma de "só restava vencido na prateleira" — e num painel
    de rastreabilidade isso não pode passar calado."""
    med_id = _zerar_medicamento(painel)
    _entrada(painel, med_id, "L-SO-VENCIDO", 40, daqui_a(-1))
    conn = painel.conexao()
    try:
        # O agregado é reposto à mão: a ordem precisa ser LIBERADA para que a
        # dispensa aconteça e o resíduo apareça. É o estado da bancada real —
        # agregado inflado por um lote que venceu enquanto ninguém olhava.
        conn.execute("UPDATE medicamentos SET quantidade=40 WHERE id=?", (med_id,))
        conn.commit()
    finally:
        conn.close()
    ordem_id = _ordem_de(painel, "OS-DESVIO", 10)

    _concluir(painel, ordem_id)

    consumos = [(r["lote"], r["quantidade"]) for r in _genealogia(painel, "OS-DESVIO")]
    assert consumos == [("SEM LOTE REGISTRADO", 10)]
    tipos = [d["tipo"] for d in _desvios(painel, "OS-DESVIO")]
    assert "Consumo sem lote válido" in tipos


def test_o_vencido_continua_no_alerta_do_dashboard(painel, med_com_vencido_e_valido):
    """Filtrar o alerta por 'Ativo' faria o lote SUMIR da tela no dia seguinte
    ao vencimento — exatamente quando ele mais precisa de alguém. O saldo já
    não conta; o que falta é a baixa, e ela é manual."""
    _, _, _ = med_com_vencido_e_valido
    conn = painel.conexao()
    try:
        painel.modulo.expirar_lotes_vencidos(conn)
        conn.commit()
        alertados = [l["lote"] for l in
                     painel.modulo.lotes_proximos_vencimento(conn, dias=30)]
    finally:
        conn.close()

    assert "L-VENCIDO" in alertados


def test_lote_sem_validade_nao_vence(painel):
    """Controle: validade vazia é estoque legado, não estoque vencido.
    Tratá-la como vencida zeraria a prateleira de quem nunca cadastrou data."""
    med_id = _zerar_medicamento(painel)
    _entrada(painel, med_id, "L-SEM-DATA", 20, "")

    conn = painel.conexao()
    try:
        painel.modulo.expirar_lotes_vencidos(conn)
        conn.commit()
    finally:
        conn.close()

    assert _agregado(painel, med_id) == 20


# ── 5. O corpo do sync de dispensers vem de fora e nada o conferia ───────────
#
# `PUT /api/dispensers/sync` e o `cmd` do display entregavam o JSON direto ao
# `for`: `None` derruba com TypeError, um dicionário solto itera as CHAVES e
# `i.get` estoura, item sem `quantidade` chegava ao `UPDATE` com KeyError. Todos
# viram 500 numa rota que ESCREVE estoque — e é por ela que o display reporta o
# que contou.

@pytest.mark.parametrize("corpo", [None, {}, "texto", 42, [None], ["x"],
                                   [{"slot": 1}], [{"quantidade": 5}],
                                   [{"slot": "1", "quantidade": 5}],
                                   [{"slot": 1, "quantidade": -3}],
                                   [{"slot": True, "quantidade": 5}]])
def test_corpo_torto_nao_derruba_o_sync(painel, corpo):
    conn = painel.conexao()
    try:
        ok, erro = painel.modulo._sync_dispensers_data(conn, corpo)
    finally:
        conn.close()

    assert isinstance(ok, bool)      # não levantou


def test_item_torto_no_meio_nao_descarta_os_bons(painel):
    """Quem manda aqui é uma ponte serial: derrubar dez slots bons por causa de
    um campo faltando num deles deixaria o estoque do display e o do painel
    divergindo, sem ninguém saber qual está certo."""
    med_id = _zerar_medicamento(painel)
    conn = painel.conexao()
    try:
        slot = next(s for s, m in painel.modulo._slot_para_id(conn).items()
                    if m == med_id)
        ok, _ = painel.modulo._sync_dispensers_data(
            conn, [{"slot": slot, "quantidade": 42}, {"quantidade": 7}, None])
        assert ok
    finally:
        conn.close()

    assert _agregado(painel, med_id) == 42


def test_a_rota_responde_400_e_nao_500(painel):
    resposta = painel.api("put", "/api/dispensers/sync", json={"slot": 1})

    assert resposta.status_code == 400
