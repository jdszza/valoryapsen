"""Excluir medicamento no painel: FK ligada, e desativar em vez de apagar.

Duas coisas quebradas que se somavam, e nenhuma das duas aparecia na tela.

**As FKs declaradas não valiam.** No SQLite o `foreign_keys` nasce DESLIGADO em
toda conexão nova, e `get_db` não o ligava — então o `REFERENCES medicamentos(id)`
de `lotes` era decoração: o banco aceitava a linha filha sem pai e não reclamava
nunca.

**E `excluir_medicamento` fazia `DELETE` incondicional.** Duas consequências:

  * *órfãos*. Lote, baixa de lote, genealogia de ordem (`ordem_lotes_consumidos`)
    e leitura da estação de visão apontam para `medicamento_id`, e a linha
    apontada sumia. Num painel cuja razão de existir é rastreabilidade, o
    registro de QUAL lote saiu em QUAL ordem passava a apontar para o vazio;
  * *renumeração da bancada inteira*. `_slot_para_id` e `_dispensers_data_local`
    numeram o slot pela POSIÇÃO da linha (`i + 1`), não pelo id. Apagado o
    medicamento da posição 3, o que era D4 vira D3, D5 vira D4, e daí em
    diante — todo o estoque do display muda de slot, sem erro e sem log.

O conserto tem as duas metades, e a segunda é a que importa mais: ligar a FK
protege `lotes` (a única com `REFERENCES`), e desativar protege as outras três
— além de ser o que mantém a POSIÇÃO de cada linha parada onde está.
"""
import sqlite3

import pytest

MED = "Dipirona 500mg"


@pytest.fixture
def painel(carregar_painel):
    return carregar_painel()


def _med_id(painel, nome: str = MED) -> int:
    conn = painel.conexao()
    try:
        return conn.execute(
            "SELECT id FROM medicamentos WHERE nome=?", (nome,)
        ).fetchone()["id"]
    finally:
        conn.close()


def _linha(painel, med_id: int):
    conn = painel.conexao()
    try:
        return conn.execute(
            "SELECT * FROM medicamentos WHERE id=?", (med_id,)
        ).fetchone()
    finally:
        conn.close()


def _sem_historico(painel) -> int:
    """Um medicamento recém-cadastrado: sem lote, sem baixa, sem ordem."""
    conn = painel.conexao()
    try:
        cur = conn.execute(
            "INSERT INTO medicamentos (nome, quantidade, capacidade, minimo) "
            "VALUES ('Recem Cadastrado 1mg', 10, 60, 5)"
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _com_lote(painel, med_id: int) -> int:
    conn = painel.conexao()
    try:
        painel.modulo._registrar_entrada_lote(
            conn, med_id, "L-001", "2030-01-01", 50, "Fornecedor", "NF-1",
            "2026-01-01", "Teste", "Admin",
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM lotes WHERE medicamento_id=?", (med_id,)
        ).fetchone()["id"]
    finally:
        conn.close()


def _excluir(painel, med_id: int):
    painel.logar()
    return painel.post_form(f"/medicamentos/{med_id}/excluir")


# ── A FK passou a valer ───────────────────────────────────────────────────────

def test_conexao_nasce_com_as_fks_ligadas(painel):
    """O PRAGMA é POR CONEXÃO. Ligado em outro lugar, a próxima conexão o perde
    — e toda conexão do painel sai de `get_db`."""
    conn = painel.conexao()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_delete_de_medicamento_com_lote_e_recusado_pelo_banco(painel):
    """A prova de que o `REFERENCES` de `lotes` deixou de ser decoração.

    Este é o SQL cru, sem passar pela rota: mesmo que alguém escreva um DELETE
    novo em outro lugar, o banco agora recusa em vez de deixar o lote órfão.
    """
    med_id = _med_id(painel)
    _com_lote(painel, med_id)

    conn = painel.conexao()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM medicamentos WHERE id=?", (med_id,))
    finally:
        conn.close()


# ── A rota: desativa quando há o que perder ───────────────────────────────────

def test_medicamento_com_lote_e_desativado_e_nao_apagado(painel):
    med_id = _med_id(painel)
    lote_id = _com_lote(painel, med_id)

    _excluir(painel, med_id)

    linha = _linha(painel, med_id)
    assert linha is not None, "a linha foi apagada — histórico órfão de volta"
    assert linha["ativo"] == 0

    conn = painel.conexao()
    try:
        lote = conn.execute("SELECT * FROM lotes WHERE id=?", (lote_id,)).fetchone()
        assert lote is not None and lote["medicamento_id"] == med_id
    finally:
        conn.close()


def test_genealogia_de_ordem_tambem_segura_o_medicamento(painel):
    """`ordem_lotes_consumidos` não tem FK declarada — a lista de tabelas do
    `_tem_historico` é o que a protege, e é por isso que a decisão não pode
    ficar só com o banco."""
    med_id = _med_id(painel)
    conn = painel.conexao()
    try:
        conn.execute(
            """INSERT INTO ordem_lotes_consumidos
               (ordem_id, numero_os, medicamento_id, medicamento_nome,
                lote_id, lote, quantidade, data)
               VALUES (1, 'OS-1', ?, ?, NULL, 'SEM LOTE REGISTRADO', 5,
                       '2026-01-01 00:00:00')""",
            (med_id, MED),
        )
        conn.commit()
    finally:
        conn.close()

    _excluir(painel, med_id)

    assert _linha(painel, med_id)["ativo"] == 0


def test_leitura_de_visao_tambem_segura_o_medicamento(painel):
    med_id = _med_id(painel)
    conn = painel.conexao()
    try:
        conn.execute(
            """INSERT INTO estoque_visao
               (momento, estacao, dispenser, medicamento_id, medicamento_nome,
                unidades, acao)
               VALUES ('2026-01-01 00:00:00', 'est1', 1, ?, ?, 10, 'aplicado')""",
            (med_id, MED),
        )
        conn.commit()
    finally:
        conn.close()

    _excluir(painel, med_id)

    assert _linha(painel, med_id)["ativo"] == 0


def test_baixa_de_lote_tambem_segura_o_medicamento(painel):
    med_id = _med_id(painel)
    conn = painel.conexao()
    try:
        conn.execute(
            """INSERT INTO lotes_baixas
               (lote_id, medicamento_id, medicamento_nome, lote, quantidade,
                motivo, operador, data)
               VALUES (NULL, ?, ?, 'L-9', 3, 'Vencido', 'Teste',
                       '2026-01-01 00:00:00')""",
            (med_id, MED),
        )
        conn.commit()
    finally:
        conn.close()

    _excluir(painel, med_id)

    assert _linha(painel, med_id)["ativo"] == 0


def test_medicamento_sem_historico_e_apagado_de_verdade(painel):
    """Desativar SEMPRE deixaria lixo permanente na tela: o cadastro errado de
    cinco minutos atrás não tem histórico a preservar."""
    med_id = _sem_historico(painel)

    _excluir(painel, med_id)

    assert _linha(painel, med_id) is None


def test_fk_desconhecida_cai_na_desativacao_em_vez_de_500(painel, monkeypatch):
    """Tabela nova com FK que `_tem_historico` não conhece: o operador não pode
    receber um stack trace por causa de um `REFERENCES` que ninguém listou."""
    med_id = _sem_historico(painel)
    monkeypatch.setattr(painel.modulo, "_tem_historico", lambda conn, mid: False)

    conn_original = painel.modulo.get_db

    def _com_fk_extra():
        conn = conn_original()
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tabela_nova (
                   id INTEGER PRIMARY KEY,
                   medicamento_id INTEGER NOT NULL REFERENCES medicamentos(id)
               )"""
        )
        return conn

    monkeypatch.setattr(painel.modulo, "get_db", _com_fk_extra)
    conn = _com_fk_extra()
    try:
        conn.execute("INSERT INTO tabela_nova (medicamento_id) VALUES (?)", (med_id,))
        conn.commit()
    finally:
        conn.close()

    resposta = _excluir(painel, med_id)

    assert resposta.status_code == 302
    monkeypatch.undo()
    assert _linha(painel, med_id)["ativo"] == 0


# ── A bancada não se mexe ─────────────────────────────────────────────────────

def _slots(painel) -> dict:
    """slot -> nome, pelo caminho LOCAL (o central fica fora do ar de propósito:
    é ele que numera por posição, e é essa numeração que estava em risco)."""
    conn = painel.conexao()
    try:
        return {d["slot"]: d["nome"]
                for d in painel.modulo._dispensers_data_local(conn)}
    finally:
        conn.close()


def test_numeracao_dos_slots_nao_muda_depois_de_excluir(painel):
    """O estrago silencioso: apagar a linha da posição 3 fazia D4 virar D3.

    O sintoma seria estoque trocado de slot — o número certo embaixo do
    medicamento errado —, e nada no log.
    """
    antes = _slots(painel)
    alvo = antes[3]
    med_id = _med_id(painel, alvo)
    _com_lote(painel, med_id)

    _excluir(painel, med_id)

    depois = _slots(painel)
    assert depois == antes, "a bancada inteira andou de posição"


def test_slot_para_id_continua_apontando_para_as_mesmas_linhas(painel):
    """O outro lado da mesma moeda: é este mapa que decide em QUAL linha o
    `sync_dispensers` do display escreve o estoque medido."""
    conn = painel.conexao()
    try:
        antes = painel.modulo._slot_para_id(conn)
    finally:
        conn.close()

    med_id = _med_id(painel)
    _com_lote(painel, med_id)
    _excluir(painel, med_id)

    conn = painel.conexao()
    try:
        assert painel.modulo._slot_para_id(conn) == antes
    finally:
        conn.close()


def test_excluir_sem_historico_ainda_renumera_e_isso_e_aceito(painel):
    """Honestidade sobre o limite: o `DELETE` que sobrou continua deslocando a
    lista. Ele só acontece para linha SEM histórico, e um medicamento
    recém-cadastrado entra no fim (id maior) — nenhuma posição antes dele se
    move."""
    antes = _slots(painel)
    novo = _sem_historico(painel)

    _excluir(painel, novo)

    assert _slots(painel) == antes


# ── O que `ativo=0` muda, e o que não muda ────────────────────────────────────

def _opcoes_do_seletor(painel) -> str:
    """Só o `<select name="medicamento_id">` da tela de entrada de lote.

    A página inteira não serve: ela também lista as últimas entradas, onde o
    nome aparece porque o lote existe — e é exatamente o que se quer preservar.
    """
    pagina = painel.cliente.get("/lotes/entrada").get_data(as_text=True)
    inicio = pagina.index('<select name="medicamento_id"')
    return pagina[inicio:pagina.index("</select>", inicio)]


def test_desativado_sai_do_seletor_de_entrada_de_lote(painel):
    """Dar entrada de lote novo num item retirado do catálogo é criar estoque
    para o que a operação já decidiu não usar."""
    med_id = _med_id(painel)
    _com_lote(painel, med_id)
    painel.logar()
    assert MED in _opcoes_do_seletor(painel)     # antes de excluir, está lá

    _excluir(painel, med_id)

    assert MED not in _opcoes_do_seletor(painel)


def test_desativado_mantem_o_saldo_que_ja_tinha(painel):
    """Desativar é tirar do catálogo, não apagar o que está na prateleira: o
    espelho do central e a estação de visão continuam medindo aquele slot."""
    med_id = _med_id(painel)
    _com_lote(painel, med_id)
    antes = _linha(painel, med_id)["quantidade"]

    _excluir(painel, med_id)

    assert _linha(painel, med_id)["quantidade"] == antes


def test_reativar_devolve_o_medicamento_ao_catalogo(painel):
    """Desativar sem caminho de volta seria uma porta de uma folha."""
    med_id = _med_id(painel)
    _com_lote(painel, med_id)
    _excluir(painel, med_id)

    painel.logar()
    painel.cliente.post(f"/medicamentos/{med_id}/reativar")

    assert _linha(painel, med_id)["ativo"] == 1
    assert MED in _opcoes_do_seletor(painel)


def test_tela_de_medicamentos_mostra_o_inativo_marcado(painel):
    """Some da tela = some da memória de quem opera, e a linha continua
    ocupando um slot na bancada."""
    med_id = _med_id(painel)
    _com_lote(painel, med_id)
    _excluir(painel, med_id)

    painel.logar()
    pagina = painel.cliente.get("/medicamentos").get_data(as_text=True)

    assert MED in pagina
    assert "Inativo" in pagina
    assert f"/medicamentos/{med_id}/reativar" in pagina


# ── Schema ────────────────────────────────────────────────────────────────────

def test_banco_de_versao_anterior_ganha_a_coluna(carregar_painel, tmp_path):
    """`CREATE TABLE IF NOT EXISTS` não repara tabela que já existe: coluna nova
    exige as DUAS entradas, a do CREATE e o ALTER de reparo."""
    banco = tmp_path / "antigo.db"
    conn = sqlite3.connect(banco)
    conn.executescript("""
        CREATE TABLE medicamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT UNIQUE NOT NULL,
            quantidade INTEGER NOT NULL DEFAULT 60,
            capacidade INTEGER NOT NULL DEFAULT 60
        );
        INSERT INTO medicamentos (nome) VALUES ('Legado 1mg');
    """)
    conn.commit()
    conn.close()

    painel = carregar_painel(env={"APSEN_DB": str(banco)})

    conn = painel.conexao()
    try:
        linha = conn.execute(
            "SELECT * FROM medicamentos WHERE nome='Legado 1mg'"
        ).fetchone()
    finally:
        conn.close()
    assert linha["ativo"] == 1, "medicamento de banco antigo nasceu desativado"
