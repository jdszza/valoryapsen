"""As quatro superfícies abertas do painel de bancada — e o que as fechou.

Todas moram em `painel_operador/backend/app.py`, e as quatro falhavam do mesmo
jeito: o painel funcionava perfeitamente, e nada no log dizia que qualquer um na
rede da fábrica tinha acesso de Admin.

1. **`GET /api/operadores` publicava o PIN de todo mundo.** O login web é
   nome + PIN, então a rota era a senha de administrador servida em JSON, sem
   autenticação. A rota saiu; o PIN virou hash; o display pergunta em vez de
   comparar.
2. **Todo o bloco `/api/*` era aberto** — dava para criar ordem, reescrever o
   estoque com baixa FEFO real e escrever no audit log. Hoje exige
   `X-API-Token`, e o teste **varre o `url_map`** em vez de conferir uma lista:
   rota nova nasce protegida ou reprova.
3. **`debug=True` em `0.0.0.0`** punha o console do Werkzeug — execução remota
   de Python — na rede, no processo que é dono da porta serial e do banco.
4. **A chave de sessão tinha default versionado**, o que deixa qualquer leitor
   do repositório assinar um cookie de Admin. Mesma regra do central:
   `APSEN_ENV=dev` ou o processo não sobe.
"""
import ast
import sqlite3

import pytest

from conftest import PAINEL_API_TOKEN


# Rotas /api/* de leitura, para os casos em que o método não importa.
ROTA_GET = "/api/resumo"


@pytest.fixture
def painel(carregar_painel):
    return carregar_painel()


# ── 1. O PIN não trafega, não é servido e não fica em claro ───────────────────

def test_rota_de_operadores_nao_existe_mais(painel):
    """404, e não "404 por enquanto": nenhuma regra pode servir operadores.

    Ela devolvia nome, perfil e PIN em claro de todos os ativos. Existia para o
    ESP32 buscar por HTTP, e o display fala por serial desde a migração — a
    rota era só superfície.
    """
    assert painel.api("get", "/api/operadores").status_code == 404

    regras = {str(r) for r in painel.modulo.app.url_map.iter_rules()}
    assert not [r for r in regras if "operadores" in r and r.startswith("/api")]


def test_banco_nao_guarda_pin_em_claro(painel):
    conn = painel.conexao()
    try:
        colunas = [c[1] for c in conn.execute("PRAGMA table_info(operadores)")]
        linhas = conn.execute("SELECT nome, pin_hash FROM operadores").fetchall()
    finally:
        conn.close()

    # A coluna `pin` não existe — nem vazia. Enquanto existisse, o próximo
    # INSERT que a esquecesse quebraria (ela era NOT NULL sem default), e a
    # tentação de "só voltar a preencher" continuaria de pé.
    assert "pin" not in colunas
    assert "pin_hash" in colunas

    for linha in linhas:
        assert linha["pin_hash"]
        # O PIN do seed está no README; o que não pode é estar no banco.
        assert "1234" not in linha["pin_hash"]
        assert "0001" not in linha["pin_hash"]


def test_login_web_funciona_com_o_operador_seed(painel):
    resposta = painel.cliente.post(
        "/login", data={"nome": "Administrador", "pin": "1234"},
        follow_redirects=False,
    )
    assert resposta.status_code == 302
    with painel.cliente.session_transaction() as sessao:
        assert sessao["perfil"] == "Admin"


# `"1234 "` não entra nesta lista: o formulário faz `.strip()` na entrada, como
# sempre fez, e `gerar_pin_hash` faz o mesmo na gravação. Espaço em volta de PIN
# digitado é acidente de teclado, não outra senha.
@pytest.mark.parametrize("pin", ["9999", "", "12345", "123"])
def test_login_web_recusa_pin_errado(painel, pin):
    resposta = painel.cliente.post(
        "/login", data={"nome": "Administrador", "pin": pin}, follow_redirects=True
    )
    assert "incorreto" in resposta.get_data(as_text=True).lower()
    with painel.cliente.session_transaction() as sessao:
        assert "op_id" not in sessao


def test_troca_de_pin_pelo_admin_grava_hash_e_passa_a_valer(painel):
    painel.logar()
    painel.cliente.post(
        "/admin/operadores/novo",
        data={"nome": "Fulano", "pin": "4321", "perfil": "PCP"},
        follow_redirects=True,
    )
    conn = painel.conexao()
    try:
        assert painel.modulo._validar_pin_data(conn, "4321")["nome"] == "Fulano"
        assert painel.modulo._validar_pin_data(conn, "4322") == {"ok": False}
    finally:
        conn.close()


def test_editar_operador_sem_informar_pin_mantem_o_atual(painel):
    """O formulário não tem mais como vir preenchido com o PIN — só há o hash.

    Exigi-lo em toda edição faria quem corrige um nome inventar um PIN novo, e
    PIN inventado por acaso é PIN anotado no monitor.
    """
    painel.logar()
    conn = painel.conexao()
    try:
        op_id = conn.execute(
            "SELECT id FROM operadores WHERE nome='Administrador'"
        ).fetchone()["id"]
    finally:
        conn.close()

    painel.cliente.post(
        f"/admin/operadores/{op_id}/editar",
        data={"nome": "Administrador", "pin": "", "perfil": "Admin", "ativo": "1"},
        follow_redirects=True,
    )

    conn = painel.conexao()
    try:
        assert painel.modulo._validar_pin_data(conn, "1234")["nome"] == "Administrador"
    finally:
        conn.close()


# ── O display pergunta; quem confere é o backend ──────────────────────────────

def test_get_operadores_do_display_nao_carrega_credencial(painel):
    conn = painel.conexao()
    try:
        dados = painel.modulo._operadores_data(conn)
    finally:
        conn.close()

    assert dados, "o display precisa dos nomes para o cabeçalho e o histórico"
    for operador in dados:
        # Nem `pin`, nem `pin_hash`, nem `rfid_uid`: hash de PIN de 4 dígitos no
        # cartão SD do display é o mesmo vazamento com mais passos — são 10 mil
        # candidatos, e hash só protege entrada que não dá para enumerar.
        assert set(operador) == {"nome", "perfil"}


def test_serial_valida_pin_e_diz_de_quem_ele_e(painel):
    conn = painel.conexao()
    try:
        assert painel.modulo._validar_pin_data(conn, "1234") == {
            "ok": True, "nome": "Administrador", "perfil": "Admin",
        }
        assert painel.modulo._validar_pin_data(conn, "9999") == {"ok": False}
        assert painel.modulo._validar_pin_data(conn, "") == {"ok": False}
    finally:
        conn.close()


def test_operador_desativado_perde_o_acesso_na_hora(painel):
    """O ganho que a validação no backend trouxe de carona.

    O display comparava contra a lista que tinha em mãos (e gravava no cartão
    SD): desativar alguém só valia no próximo `fetch_operadores_api`.
    """
    conn = painel.conexao()
    try:
        conn.execute("UPDATE operadores SET ativo=0 WHERE nome='Administrador'")
        conn.commit()
        assert painel.modulo._validar_pin_data(conn, "1234") == {"ok": False}
    finally:
        conn.close()


def test_cmd_serial_validar_operador_responde_com_a_tag_operador(painel):
    """O firmware espera `resp: "operador"` e descarta o resto.

    Responder com outra tag o deixaria travado até o timeout — que é o que o
    tratamento de erro de `_handle_serial_message` existe para evitar.
    """
    enviados = []

    class ConexaoFake:
        def write(self, dados):
            enviados.append(dados.decode())

        def flush(self):
            pass

    painel.modulo._handle_serial_message(
        ConexaoFake(), {"cmd": "validar_operador", "pin": "1234"}
    )
    assert enviados and '"resp": "operador"' in enviados[0]
    assert '"ok": true' in enviados[0]
    assert "1234" not in enviados[0]


# ── 2. Nenhuma rota /api/* sem token ──────────────────────────────────────────

def test_todas_as_rotas_api_exigem_token(painel):
    """Varredura do `url_map`, e não uma lista escrita à mão.

    São 16 rotas hoje. Uma lista aqui envelheceria na primeira rota nova — e o
    buraco seria justamente na rota que ninguém lembrou de revisar.
    """
    app = painel.modulo.app
    desprotegidas = [
        str(regra)
        for regra in app.url_map.iter_rules()
        if str(regra).startswith("/api/")
        and not getattr(app.view_functions[regra.endpoint], "api_token_required", False)
    ]
    assert desprotegidas == []


def test_api_sem_token_responde_401(painel):
    assert painel.cliente.get(ROTA_GET).status_code == 401


def test_api_com_token_errado_responde_401(painel):
    resposta = painel.cliente.get(ROTA_GET, headers={"X-API-Token": "outra-coisa"})
    assert resposta.status_code == 401


def test_api_com_token_responde_200(painel):
    resposta = painel.cliente.get(ROTA_GET, headers={"X-API-Token": PAINEL_API_TOKEN})
    assert resposta.status_code == 200


def test_token_com_caractere_fora_do_ascii_nao_vira_500(painel):
    """`hmac.compare_digest` levanta TypeError com `str` não-ASCII."""
    resposta = painel.cliente.get(ROTA_GET, headers={"X-API-Token": "tókèn"})
    assert resposta.status_code == 401


def test_escrita_de_estoque_sem_token_nao_toca_no_banco(painel):
    """A rota que mais custa: ela dá baixa FEFO de verdade."""
    conn = painel.conexao()
    try:
        antes = conn.execute(
            "SELECT COUNT(*) c FROM estoque_visao"
        ).fetchone()["c"]
    finally:
        conn.close()

    resposta = painel.cliente.post(
        "/api/visao/estoque",
        json={"estacao": "bancada-1",
              "leituras": [{"dispenser": 1, "sku": "MED-001", "caixas": 4}]},
    )
    assert resposta.status_code == 401

    conn = painel.conexao()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM estoque_visao"
        ).fetchone()["c"] == antes
    finally:
        conn.close()


def test_sem_a_variavel_a_api_responde_503_e_o_painel_sobe(carregar_painel):
    """Segredo ausente desliga o RECURSO, não o processo — como o `CONSOLE_SENHA`.

    Derrubar o boot levaria junto a ponte serial, ou seja, a tela que o operador
    está olhando, por causa de uma variável que a bancada talvez nem use. E é
    503 com a mensagem, não 404: quem vai ler a resposta é quem configurou
    errado.
    """
    p = carregar_painel(env={"APSEN_API_TOKEN": None})

    resposta = p.cliente.get(ROTA_GET, headers={"X-API-Token": PAINEL_API_TOKEN})
    assert resposta.status_code == 503
    assert "APSEN_API_TOKEN" in resposta.get_json()["erro"]

    # O resto do painel — o que o operador usa — continua de pé.
    p.logar()
    assert p.cliente.get("/").status_code == 200


def test_rotas_web_nao_ganharam_o_token(painel):
    """O decorator é do bloco /api/*; quem entra pelo navegador usa PIN."""
    painel.logar()
    assert painel.cliente.get("/ordens").status_code == 200


# ── 3. O console do Werkzeug nunca escuta a rede ──────────────────────────────

def test_debug_desligado_por_padrao(painel):
    assert painel.modulo.MODO_DEBUG is False


def test_debug_so_com_a_variavel(carregar_painel):
    assert carregar_painel(env={"APSEN_DEBUG": "1"}).modulo.MODO_DEBUG is True


def test_app_run_com_debug_so_escuta_localhost():
    """Varredura por AST do arquivo inteiro.

    `debug=True` sobe o console do Werkzeug, que executa Python arbitrário no
    processo dono da porta serial e do banco da bancada. Depurar da própria
    máquina é legítimo; expor isso à rede da fábrica não é, e essa é a única
    combinação sem uso bom. O teste lê o arquivo porque o que importa é a
    combinação escrita no código, e não o valor de uma variável em runtime.
    """
    from conftest import PAINEL_DIR

    arvore = ast.parse((PAINEL_DIR / "app.py").read_text(encoding="utf-8"))
    chamadas = [
        no for no in ast.walk(arvore)
        if isinstance(no, ast.Call)
        and isinstance(no.func, ast.Attribute) and no.func.attr == "run"
    ]
    assert chamadas, "o app.py não chama mais app.run — reveja este teste"

    for chamada in chamadas:
        kwargs = {k.arg: k.value for k in chamada.keywords}
        debug = kwargs.get("debug")
        ligado = isinstance(debug, ast.Constant) and debug.value is True
        if not ligado:
            continue
        host = kwargs.get("host")
        assert isinstance(host, ast.Constant) and host.value == "127.0.0.1"


# ── 4. Chave de sessão: a mesma regra do central ──────────────────────────────

CHAVE_BOA = "a" * 32


@pytest.mark.parametrize("chave", ["", None, "apsen-dev-2024-change-in-prod", "curta"])
def test_secret_fraca_derruba_o_boot_fora_de_dev(painel, chave):
    with pytest.raises(RuntimeError) as erro:
        painel.modulo.resolver_secret_key(chave or "", ambiente="prod")
    # A mensagem carrega a receita da chave: quem leu o erro já sabe o que fazer.
    assert "secrets.token_hex" in str(erro.value)


@pytest.mark.parametrize("chave", ["", "apsen-dev-2024-change-in-prod", "curta"])
def test_secret_fraca_e_tolerada_so_em_dev(painel, chave):
    """A saída de escape existe, é explícita e é visível — nunca o silêncio."""
    assert painel.modulo.resolver_secret_key(chave, ambiente="dev")


def test_secret_boa_passa_em_qualquer_ambiente(painel):
    for ambiente in ("prod", "dev", ""):
        assert painel.modulo.resolver_secret_key(CHAVE_BOA, ambiente) == CHAVE_BOA


def test_app_nao_sobe_com_a_secret_default_fora_de_dev(carregar_painel):
    """O import inteiro falha — não é warning, não é fallback."""
    with pytest.raises(RuntimeError):
        carregar_painel(env={"APSEN_SECRET": "apsen-dev-2024-change-in-prod",
                             "APSEN_ENV": "prod"})


def test_app_sobe_com_a_secret_default_em_dev(carregar_painel):
    p = carregar_painel(env={"APSEN_SECRET": "apsen-dev-2024-change-in-prod",
                             "APSEN_ENV": "dev"})
    assert p.modulo.app.secret_key


# ── Migração de um banco que já está na bancada ───────────────────────────────

SCHEMA_ANTIGO = """
    CREATE TABLE operadores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL,
        pin TEXT NOT NULL,
        perfil TEXT NOT NULL DEFAULT 'Operador',
        ativo INTEGER NOT NULL DEFAULT 1,
        data_criacao TEXT NOT NULL
    );
    INSERT INTO operadores (nome, pin, perfil, ativo, data_criacao)
        VALUES ('Chefe', '7777', 'Admin', 1, '2026-01-01 00:00:00'),
               ('Ex-funcionario', '8888', 'PCP', 0, '2026-01-01 00:00:00');
"""


@pytest.fixture
def painel_legado(tmp_path, carregar_painel):
    """Banco na forma anterior ao hash, no caminho que a fábrica vai usar."""
    conn = sqlite3.connect(tmp_path / "painel.db")
    conn.executescript(SCHEMA_ANTIGO)
    conn.commit()
    conn.close()
    return carregar_painel()


def test_migracao_converte_os_pins_gravados_e_apaga_a_coluna(painel_legado):
    """O banco da bancada já tem os PINs de todo mundo em claro, e um `.db` anda
    de pendrive. Não basta a escrita nova hashear: a migração converte o que
    está lá e depois se livra da coluna."""
    conn = painel_legado.conexao()
    try:
        colunas = [c[1] for c in conn.execute("PRAGMA table_info(operadores)")]
        assert "pin" not in colunas

        linhas = conn.execute(
            "SELECT nome, pin_hash, perfil, ativo FROM operadores ORDER BY id"
        ).fetchall()
        assert [l["nome"] for l in linhas] == ["Chefe", "Ex-funcionario"]
        # ids, perfil e o `ativo=0` sobrevivem à reconstrução da tabela.
        assert [l["ativo"] for l in linhas] == [1, 0]
        for linha in linhas:
            assert linha["pin_hash"] and "7777" not in linha["pin_hash"]

        # E o PIN de antes continua valendo: ninguém fica de fora da bancada.
        assert painel_legado.modulo._validar_pin_data(conn, "7777")["nome"] == "Chefe"
        # O inativo continua inativo depois de migrado.
        assert painel_legado.modulo._validar_pin_data(conn, "8888") == {"ok": False}
    finally:
        conn.close()


def test_migracao_e_idempotente(painel_legado):
    """`init_db()` roda a todo startup — a segunda passada não pode refazer nada."""
    painel_legado.modulo.init_db()
    painel_legado.modulo.init_db()

    conn = painel_legado.conexao()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM operadores").fetchone()["c"] == 2
        assert "pin" not in [c[1] for c in conn.execute("PRAGMA table_info(operadores)")]
        assert painel_legado.modulo._validar_pin_data(conn, "7777")["nome"] == "Chefe"
    finally:
        conn.close()


def test_migracao_nao_reintroduz_o_seed(painel_legado):
    """Banco com operador é banco em uso: o seed não pode voltar a existir."""
    conn = painel_legado.conexao()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM operadores WHERE nome='Administrador'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()
