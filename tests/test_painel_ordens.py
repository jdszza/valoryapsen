"""O painel de bancada espelha o computador central — e só isso.

O que estes testes protegem é a regra de ouro da integração: **o central manda,
o painel espelha.** Ela se decompõe em quatro coisas que falham de formas
diferentes, e três delas falhariam em silêncio:

1. **O vocabulário de status é traduzido num lugar só.** `Erro` e `Cancelado`
   são valores novos no painel; onde o código compara status por igualdade, uma
   OS que a célula abortou some da tela sem erro nenhum no log.
2. **O espelho é idempotente e não move estoque.** Quem deu baixa foi a célula;
   repetir a baixa no SQLite do painel dobraria o consumo, e a fila de
   sincronização roda a cada 5 segundos.
3. **Ordem espelhada é só-leitura nos TRÊS caminhos** (web, API e serial). Um
   deles esquecido é uma porta por onde o painel volta a mentir sobre a planta.
4. **Central fora do ar não derruba a bancada.** O processo possui a porta
   serial do display e a tela que o operador está olhando.
"""
import json

import pytest


# ── Ordens de exemplo, no formato que o central devolve ───────────────────────

def _remota(os_id, status="aguardando", total_itens=2, descricao="Infecto",
            categoria="Antibiótico", criado="2026-09-09T14:30:12",
            concluida=None):
    return {
        "os_id": os_id,
        "descricao": descricao,
        "categoria": categoria,
        "status": status,
        "criado_em": criado,
        "concluida_em": concluida,
        "total_itens": total_itens,
    }


def _detalhe(os_id, itens):
    return {
        "os_id": os_id,
        "itens": [
            {"dispenser_id": i + 1, "medicamento": nome, "sku": f"SKU-{i}",
             "categoria": "Antibiótico", "quantidade_alvo": qtd,
             "quantidade_real": qtd, "status": "concluido"}
            for i, (nome, qtd) in enumerate(itens)
        ],
    }


OS_A = "OS-INFECTO-01-20260909T143012-A1B2C3"
OS_B = "OS-DOR-02-20260909T150000-D4E5F6"


@pytest.fixture
def painel(carregar_painel):
    """Painel com uma OS espelhada de dois itens, aguardando."""
    return carregar_painel(
        ordens_central=[_remota(OS_A)],
        detalhes_central={OS_A: _detalhe(OS_A, [("Amoxicilina 500mg", 10),
                                                ("Dipirona 500mg", 4)])},
    )


def _sincronizar(painel):
    conn = painel.conexao()
    try:
        return painel.modulo.sincronizar_ordens_central(conn)
    finally:
        conn.close()


# ── 1. O mapa de status, nos cinco valores ────────────────────────────────────

CINCO = [
    ("aguardando",   "Pendente"),
    ("em_andamento", "Em Processo"),
    ("concluida",    "Concluido"),
    ("erro",         "Erro"),
    ("cancelada",    "Cancelado"),
]


@pytest.mark.parametrize("central, painel_status", CINCO)
def test_mapa_de_status_cobre_o_vocabulario_do_central(carregar_painel, central, painel_status):
    p = carregar_painel(
        ordens_central=[_remota(OS_A, status=central, total_itens=1)],
        detalhes_central={OS_A: _detalhe(OS_A, [("Dipirona 500mg", 3)])},
    )
    _sincronizar(p)
    assert p.ordem(OS_A)["status"] == painel_status


def test_status_desconhecido_nao_vira_fila(carregar_painel):
    """Um status novo do central não pode aparecer como 'Pendente'.

    Seria pior que aparecer errado: o operador ficaria esperando a célula
    executar uma ordem que já terminou.
    """
    p = carregar_painel(
        ordens_central=[_remota(OS_A, status="hibernando", total_itens=1)],
        detalhes_central={OS_A: _detalhe(OS_A, [("Dipirona 500mg", 3)])},
    )
    _sincronizar(p)
    assert p.ordem(OS_A)["status"] == "Erro"


@pytest.mark.parametrize("central, painel_status", [("erro", "Erro"), ("cancelada", "Cancelado")])
def test_erro_e_cancelado_aparecem_na_lista_e_no_resumo(carregar_painel, central, painel_status):
    """Os dois valores NOVOS têm que atravessar as telas que filtram por status.

    Onde o painel comparava por igualdade sem `else`, uma OS abortada existia no
    banco e não era contada em lugar nenhum — o painel dizia que a planta estava
    em dia.
    """
    p = carregar_painel(
        ordens_central=[_remota(OS_A, status=central, total_itens=1)],
        detalhes_central={OS_A: _detalhe(OS_A, [("Dipirona 500mg", 3)])},
    )
    _sincronizar(p)
    p.logar()

    lista = p.cliente.get("/ordens")
    assert lista.status_code == 200
    assert OS_A in lista.get_data(as_text=True)

    filtrada = p.cliente.get(f"/ordens?status={painel_status}")
    assert OS_A in filtrada.get_data(as_text=True)

    resumo = p.api("get", "/api/resumo").get_json()
    chave = "com_erro" if painel_status == "Erro" else "canceladas"
    assert resumo[chave] == 1

    # E as telas que somam por status continuam renderizando.
    assert p.cliente.get("/").status_code == 200
    assert p.cliente.get("/relatorio").status_code == 200
    assert p.cliente.get("/kpis").status_code == 200


# ── 2. UPSERT idempotente ─────────────────────────────────────────────────────

def test_sincronizar_duas_vezes_nao_duplica_linha(painel):
    painel.central.ordens = [_remota(OS_A), _remota(OS_B, total_itens=1)]
    painel.central.detalhes[OS_B] = _detalhe(OS_B, [("Ibuprofeno 600mg", 5)])

    primeiro = _sincronizar(painel)
    segundo = _sincronizar(painel)

    assert primeiro["novas"] == 2
    assert segundo["novas"] == 0
    assert segundo["atualizadas"] == 2

    conn = painel.conexao()
    try:
        total = conn.execute(
            "SELECT COUNT(*) c FROM ordens WHERE numero_os IN (?,?)", (OS_A, OS_B)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert total == 2


def test_sincronizar_nao_baixa_estoque(painel):
    """Quem deu baixa foi a célula. O espelho não pode chamar o FEFO.

    Com a ordem chegando já concluída — que é o caso comum, já que o histórico
    do central traz o que terminou —, um espelho que passasse por
    `_set_status_by_numero_os` dispararia `processar_conclusao_ordem` a cada
    volta do laço de 5 segundos.
    """
    painel.central.ordens = [_remota(OS_A, status="concluida",
                                     concluida="2026-09-09T15:00:00")]

    def estoque():
        conn = painel.conexao()
        try:
            return conn.execute(
                "SELECT quantidade FROM medicamentos WHERE nome='Amoxicilina 500mg'"
            ).fetchone()["quantidade"]
        finally:
            conn.close()

    antes = estoque()
    _sincronizar(painel)
    _sincronizar(painel)
    _sincronizar(painel)

    assert painel.ordem(OS_A)["status"] == "Concluido"
    assert estoque() == antes

    conn = painel.conexao()
    try:
        consumos = conn.execute(
            "SELECT COUNT(*) c FROM ordem_lotes_consumidos WHERE numero_os=?", (OS_A,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert consumos == 0


def test_detalhe_so_e_buscado_quando_algo_mudou(painel):
    """Uma requisição por ordem a cada 5 segundos para reescrever o mesmo JSON."""
    _sincronizar(painel)
    assert painel.central.chamadas_de_detalhe() == [f"/os/{OS_A}"]

    painel.central.caminhos.clear()
    _sincronizar(painel)
    assert painel.central.chamadas_de_detalhe() == []

    # Status mudou: aí sim.
    painel.central.ordens = [_remota(OS_A, status="em_andamento")]
    _sincronizar(painel)
    assert painel.central.chamadas_de_detalhe() == [f"/os/{OS_A}"]


def test_itens_do_central_viram_o_formato_do_painel(painel):
    _sincronizar(painel)
    itens = json.loads(painel.ordem(OS_A)["itens"])
    assert itens == [{"med": "Amoxicilina 500mg", "qtd": 10},
                     {"med": "Dipirona 500mg", "qtd": 4}]


def test_data_do_central_perde_o_T_e_ganha_o_fuso(painel):
    """`data_criacao` é ordenada e comparada como TEXTO no painel inteiro.

    Duas coisas, e as duas produzem relatório errado sem dar erro:

    * guardar o ISO com 'T' faria a ordem espelhada ordenar depois de qualquer
      ordem local do mesmo dia (texto, e `'T' > ' '`), e nunca estourar o SLA
      de 4h dos KPIs;
    * o central carimba em **UTC**, e a coluna guarda hora **local**. Numa
      bancada no Brasil, toda OS espelhada nascia três horas no passado: ela
      ordenava antes de ordens locais mais velhas, e o SLA lhe creditava três
      horas a mais de duração.

    O esperado é CALCULADO, não escrito: um literal amarraria o teste ao fuso
    da máquina que o escreveu.
    """
    from datetime import datetime, timezone

    esperado = (datetime(2026, 9, 9, 14, 30, 12, tzinfo=timezone.utc)
                .astimezone().strftime("%Y-%m-%d %H:%M:%S"))
    _sincronizar(painel)

    gravado = painel.ordem(OS_A)["data_criacao"]
    assert "T" not in gravado
    assert gravado == esperado


def test_data_com_offset_explicito_e_respeitada(painel, monkeypatch):
    """Sem offset o valor é UTC (é o que o central manda); COM offset, ele
    manda. A regra não pode ser "some o fuso e assume o meu"."""
    from datetime import datetime, timezone

    normalizar = painel.modulo._normalizar_data
    esperado = (datetime(2026, 9, 9, 14, 30, 12, tzinfo=timezone.utc)
                .astimezone().strftime("%Y-%m-%d %H:%M:%S"))

    assert normalizar("2026-09-09T14:30:12+00:00") == esperado
    assert normalizar("2026-09-09T14:30:12Z") == esperado
    assert normalizar("2026-09-09T14:30:12") == esperado


def test_data_irreconhecivel_nao_para_o_espelho(painel):
    """Carimbo torto não pode levantar dentro do laço de sincronização: o
    espelho é uma thread, e a exceção dela não derruba "a sincronização" —
    derruba a thread."""
    normalizar = painel.modulo._normalizar_data

    assert normalizar("nao-e-data", "padrao") == "nao-e-data"
    assert normalizar("", "padrao") == "padrao"
    assert normalizar(None, "") != ""       # cai no agora


def test_ordem_sumida_do_central_nao_e_apagada(painel):
    _sincronizar(painel)
    painel.central.ordens = []
    _sincronizar(painel)
    assert painel.ordem(OS_A) is not None


# ── 3. Ordem local nunca é sobrescrita ────────────────────────────────────────

def test_ordem_local_nunca_e_tocada_pelo_espelho(painel):
    """Colisão de número: a linha local vence e o espelho a deixa em paz.

    A tela de criação de ordem do painel continua criando ordens locais, que o
    central ignora — as duas populações convivem na mesma tabela.
    """
    painel.criar_ordem_local(OS_A, [{"med": "Vitamina C 1g", "qtd": 7}], "Pausado")

    resumo = _sincronizar(painel)

    linha = painel.ordem(OS_A)
    assert resumo["ignoradas_locais"] == 1
    assert linha["origem"] == "local"
    assert linha["status"] == "Pausado"
    assert json.loads(linha["itens"]) == [{"med": "Vitamina C 1g", "qtd": 7}]


# ── 4. Os três caminhos de bloqueio ───────────────────────────────────────────

def test_web_recusa_mudar_status_de_ordem_espelhada(painel):
    _sincronizar(painel)
    painel.logar()
    linha = painel.ordem(OS_A)

    resposta = painel.cliente.post(
        f"/ordens/{linha['id']}/status/Em Processo", follow_redirects=True
    )

    assert resposta.status_code == 200
    assert "computador central" in resposta.get_data(as_text=True)
    assert painel.ordem(OS_A)["status"] == "Pendente"


def test_web_recusa_editar_e_excluir_ordem_espelhada(painel):
    _sincronizar(painel)
    painel.logar()
    oid = painel.ordem(OS_A)["id"]

    editar = painel.cliente.get(f"/ordens/{oid}/editar", follow_redirects=True)
    assert "computador central" in editar.get_data(as_text=True)

    excluir = painel.post_form(f"/ordens/{oid}/excluir", follow_redirects=True)
    assert "computador central" in excluir.get_data(as_text=True)
    assert painel.ordem(OS_A) is not None


def test_api_recusa_status_de_ordem_espelhada(painel):
    _sincronizar(painel)
    oid = painel.ordem(OS_A)["id"]

    resposta = painel.api(
        "put", f"/api/ordens/{oid}/status", json={"status": "Em Processo"}
    )

    assert resposta.status_code == 409
    assert resposta.get_json() == {"ok": False, "erro": "ordem do central"}
    assert painel.ordem(OS_A)["status"] == "Pendente"


def test_serial_recusa_set_status_de_ordem_espelhada(painel):
    _sincronizar(painel)
    conn = painel.conexao()
    try:
        ok, erro = painel.modulo._set_status_by_numero_os(conn, OS_A, "Em Processo")
    finally:
        conn.close()

    assert (ok, erro) == (False, "ordem do central")
    assert painel.ordem(OS_A)["status"] == "Pendente"


def test_os_tres_caminhos_continuam_funcionando_para_ordem_local(painel):
    """A recusa não pode ser um bloqueio geral: o painel ainda opera o que é seu."""
    painel.logar()
    oid = painel.criar_ordem_local("OS-0099", [{"med": "Vitamina C 1g", "qtd": 2}])

    # web
    painel.cliente.post(f"/ordens/{oid}/status/Em Processo", follow_redirects=True)
    assert painel.ordem("OS-0099")["status"] == "Em Processo"

    # API
    resposta = painel.api("put", f"/api/ordens/{oid}/status", json={"status": "Pausado"})
    assert resposta.status_code == 200
    assert painel.ordem("OS-0099")["status"] == "Pausado"

    # serial
    conn = painel.conexao()
    try:
        ok, erro = painel.modulo._set_status_by_numero_os(conn, "OS-0099", "Em Processo")
    finally:
        conn.close()
    assert (ok, erro) == (True, None)
    assert painel.ordem("OS-0099")["status"] == "Em Processo"


# ── 5. Escrita de dispenser vinda do display ──────────────────────────────────

DISPENSERS = [
    {"dispenser_id": i, "medicamento": nome, "categoria": "Antibiótico",
     "quantidade_atual": 40 + i, "capacidade": 60,
     "ultima_os_id": OS_A, "atualizado_em": "2026-09-09T14:00:00"}
    for i, nome in enumerate(
        ["Amoxicilina 500mg", "Paracetamol 750mg", "Ibuprofeno 600mg",
         "Dipirona 500mg", "Vitamina C 1g", "Omeprazol 20mg",
         "Losartana 50mg", "Metformina 850mg"], start=1)
]


@pytest.fixture
def painel_com_slots(carregar_painel):
    return carregar_painel(dispensers_central=DISPENSERS)


def test_dispensers_vem_do_central_pelo_dispenser_id(painel_com_slots):
    """O slot é o `dispenser_id` da célula, não a posição na tabela local.

    O arranjo antigo numerava por posição — batia com os 8 slots por
    coincidência, e a primeira exclusão de medicamento no cadastro local
    deslocaria a bancada inteira.
    """
    conn = painel_com_slots.conexao()
    try:
        dados = painel_com_slots.modulo._dispensers_data(conn)
    finally:
        conn.close()

    assert [d["slot"] for d in dados] == list(range(1, 9))
    assert [d["quantidade"] for d in dados] == [41, 42, 43, 44, 45, 46, 47, 48]
    assert all(d["capacidade"] == 60 for d in dados)
    # `minimo` continua saindo do cadastro local, casado por nome.
    assert all(d["minimo"] == 10 for d in dados)


def test_display_nao_escreve_em_slot_espelhado(painel_com_slots):
    """Quem manda no número é quem o mede — o mesmo motivo do dispenser sob a câmera."""
    modulo = painel_com_slots.modulo
    conn = painel_com_slots.conexao()
    try:
        sync = modulo._sync_dispensers_data(conn, [{"slot": 3, "quantidade": 1}])
        troca = modulo._set_dispenser_med_data(conn, 3, "Outra Coisa")
    finally:
        conn.close()

    assert sync == (False, modulo.MSG_SLOT_CENTRAL)
    assert troca == (False, modulo.MSG_SLOT_CENTRAL)
    # Recusa de SLOT não pode falar de ORDEM: quem lesse a mensagem iria
    # procurar o problema numa ordem de expedição, o único lugar onde ele não
    # está. As duas recusas existem e são distintas de propósito.
    assert modulo.MSG_SLOT_CENTRAL != modulo.MSG_ORDEM_CENTRAL


def test_display_escreve_quando_o_central_esta_fora(painel_com_slots):
    """As duas metades concordam: sem espelho de leitura, não há bloqueio de escrita."""
    painel_com_slots.central.fora_do_ar = True
    conn = painel_com_slots.conexao()
    try:
        ok, _ = painel_com_slots.modulo._sync_dispensers_data(
            conn, [{"slot": 1, "quantidade": 7}]
        )
        quantidade = conn.execute(
            "SELECT quantidade FROM medicamentos ORDER BY id LIMIT 1"
        ).fetchone()["quantidade"]
    finally:
        conn.close()

    assert ok is True
    assert quantidade == 7


# ── 6. Central fora do ar ─────────────────────────────────────────────────────

def test_sync_nao_levanta_com_o_central_fora(painel):
    painel.central.fora_do_ar = True
    resumo = _sincronizar(painel)      # não levanta: é o contrato do módulo
    assert resumo["vistas"] == 0


def test_backend_continua_respondendo_com_o_central_fora(painel):
    painel.central.fora_do_ar = True
    painel.logar()
    assert painel.cliente.get("/").status_code == 200
    assert painel.cliente.get("/ordens").status_code == 200
    assert painel.api("get", "/api/resumo").status_code == 200


def test_dispensers_caem_no_fallback_local(painel):
    """Tela em branco por rede caída é pior que estoque um pouco velho."""
    painel.central.fora_do_ar = True
    conn = painel.conexao()
    try:
        dados = painel.modulo._dispensers_data(conn)
        locais = painel.modulo._dispensers_data_local(conn)
    finally:
        conn.close()

    assert dados == locais
    assert dados, "o fallback local não pode devolver lista vazia"


def test_integracao_desligada_nao_faz_requisicao(carregar_painel):
    """`PAINEL_CENTRAL=0` devolve a bancada ao comportamento 100% local."""
    p = carregar_painel(ordens_central=[_remota(OS_A)],
                        env={"PAINEL_CENTRAL": "0"})
    # O duplo entra por cima de `_get`, então o corte precisa estar ANTES dele.
    assert p.modulo.central_client.INTEGRACAO_ATIVA is False


# ── 7. A lista que o display recebe ───────────────────────────────────────────

def test_display_recebe_pendentes_e_em_processo(painel):
    """Sem 'Em Processo', o operador não vê no display a ordem que está rodando."""
    painel.central.ordens = [_remota(OS_A, status="em_andamento"),
                             _remota(OS_B, status="aguardando", total_itens=1)]
    painel.central.detalhes[OS_B] = _detalhe(OS_B, [("Ibuprofeno 600mg", 5)])
    _sincronizar(painel)

    conn = painel.conexao()
    try:
        dados = painel.modulo._ordens_pendentes_data(conn)
    finally:
        conn.close()

    assert {d["numero_os"] for d in dados} == {OS_A, OS_B}
    assert {d["status"] for d in dados} == {"Em Processo", "Pendente"}


def test_display_recebe_no_maximo_cinco_ordens(painel):
    """MAX_ORDENS 5 no firmware, MAX_FILA_OS 5 no central: o teto casa."""
    painel.central.ordens = [
        _remota(f"OS-T{i:02d}-2026090{i}T120000-ABCDEF", total_itens=1,
                criado=f"2026-09-0{i}T12:00:00")
        for i in range(1, 8)
    ]
    for o in painel.central.ordens:
        painel.central.detalhes[o["os_id"]] = _detalhe(o["os_id"], [("Dipirona 500mg", 1)])
    _sincronizar(painel)

    conn = painel.conexao()
    try:
        dados = painel.modulo._ordens_pendentes_data(conn)
    finally:
        conn.close()

    assert len(dados) == painel.modulo.MAX_ORDENS_DISPLAY == 5


# ── 8. O que NÃO mudou ────────────────────────────────────────────────────────

def test_o_painel_nunca_escreve_no_central(painel):
    """O espelho é de mão única: `central_client` só tem GET.

    `PUT /ordens/{os_id}/status` grava a coluna `status` do banco do central sem
    falar com o orquestrador. Ligar um botão do painel nele mudaria a linha do
    banco enquanto a célula continua fazendo outra coisa.
    """
    fonte = (painel.modulo.central_client.__file__)
    with open(fonte, encoding="utf-8") as arquivo:
        texto = arquivo.read()
    for verbo in ("requests.post", "requests.put", "requests.patch", "requests.delete"):
        assert verbo not in texto, f"{verbo} não pode existir no cliente do central"


def test_a_criacao_de_ordem_local_continua_nascendo_local(painel):
    """A tela `/ordens/nova` cria ordens que o central ignora — de propósito."""
    painel.logar()
    resposta = painel.cliente.post(
        "/ordens/nova",
        data={"numero_os": "OS-0500", "destino": "Bancada", "prioridade": "Normal",
              "med[]": ["Vitamina C 1g"], "qtd[]": ["3"]},
        follow_redirects=True,
    )
    assert resposta.status_code == 200
    assert painel.ordem("OS-0500")["origem"] == "local"


# ── 9. O espelho tem que AVISAR o display, não só gravar no SQLite ────────────

def _pushes(painel):
    """Intercepta o que o backend manda ao display pela ponte serial."""
    enviados = []
    painel.modulo.push_to_display = lambda payload: enviados.append(payload)
    return enviados


def test_mudanca_de_status_espelhado_e_empurrada_ao_display(painel):
    """Gravar no banco não basta: o display precisa ser avisado.

    Regressão de um buraco real. O espelho grava o status por UPDATE direto,
    justamente para não cair em `_set_status_by_numero_os` (que dispararia
    baixa de estoque) — mas era essa função que avisava o display. E o display
    não descobre sozinho: `get_ordens` só monta ordem que ele ainda não
    conhece, e a fila que ele recebe traz apenas 'Pendente' e 'Em Processo'.

    Sem o aviso, uma OS que a célula concluiu ou abortou simplesmente SOME da
    lista servida, em vez de mudar de estado — e a ordem ficava congelada na
    tela do operador para sempre, no status em que entrou.
    """
    enviados = _pushes(painel)
    _sincronizar(painel)
    assert painel.ordem(OS_A)["status"] == "Pendente"
    assert enviados == [], "ordem nova o display recebe por get_ordens, sem push"

    painel.central.ordens = [_remota(OS_A, "em_andamento")]
    _sincronizar(painel)
    assert enviados[-1] == {"push": "ordem_status", "numero_os": OS_A,
                            "status": "Em Processo"}

    painel.central.ordens = [_remota(OS_A, "cancelada")]
    _sincronizar(painel)
    assert enviados[-1] == {"push": "ordem_status", "numero_os": OS_A,
                            "status": "Cancelado"}


def test_sincronizacao_sem_mudanca_nao_empurra_nada(painel):
    """O laço roda a cada CENTRAL_SYNC_S (5s). Push por ciclo seria push por ruído."""
    _sincronizar(painel)
    enviados = _pushes(painel)
    for _ in range(3):
        _sincronizar(painel)
    assert enviados == []


def test_ordem_local_nunca_gera_push_do_espelho(painel):
    """Colisão de número não vira aviso: a ordem local não é do central."""
    conn = painel.conexao()
    try:
        conn.execute(
            """INSERT INTO ordens (numero_os, itens, destino, prioridade,
                                   status, data_criacao, data_atualizacao, origem)
               VALUES (?,?,?,?,?,?,?,'local')""",
            (OS_A, "[]", "Bancada", "Normal", "Pendente",
             "2026-09-09 10:00:00", "2026-09-09 10:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    enviados = _pushes(painel)
    painel.central.ordens = [_remota(OS_A, "concluida")]
    resumo = _sincronizar(painel)

    assert resumo["ignoradas_locais"] == 1
    assert enviados == []
    assert painel.ordem(OS_A)["status"] == "Pendente"


# ── 10. O estoque dos slots não pode pagar a rede em toda pergunta ────────────

def test_dispensers_do_central_sao_cacheados_por_uma_janela_curta(painel_com_slots):
    """`get_dispensers` do display roda dentro da ponte serial, e ela tem pressa.

    O `serial_request` do firmware desiste em 800 ms; `CENTRAL_TIMEOUT_S` é 3 s.
    Sem cache, um central lento — de pé, mas sem responder — faz o display
    desistir antes de o backend ter a resposta, e o painel de estoque congela
    sem que nada no log aponte para o central.

    Um pedido do display também passa duas vezes por aqui (montar a lista e
    decidir o que é só-leitura), e as duas TÊM de concordar: metade da decisão
    sobre um central que respondeu e a outra sobre um que caiu deixaria o painel
    sem poder ler e sem poder escrever o mesmo slot.
    """
    modulo = painel_com_slots.modulo
    modulo._dispensers_cache["quando"] = 0.0   # cache frio, como no boot
    antes = painel_com_slots.central.caminhos.count("/dispensers/estado")

    conn = painel_com_slots.conexao()
    try:
        primeira = modulo._dispensers_data(conn)
        modulo._slots_espelhados()
        segunda = modulo._dispensers_data(conn)
    finally:
        conn.close()

    gastas = painel_com_slots.central.caminhos.count("/dispensers/estado") - antes
    assert gastas == 1, f"{gastas} requisições onde uma bastava"
    assert primeira == segunda


def test_cache_de_dispensers_expira(painel_com_slots):
    """Curto de propósito: estoque é o número que o operador confere na bancada.

    Cache sem expiração faria o painel mostrar para sempre o estoque de antes
    da primeira dispensação — pior que pagar a rede.
    """
    modulo = painel_com_slots.modulo
    conn = painel_com_slots.conexao()
    try:
        modulo._dispensers_data(conn)
        painel_com_slots.central.dispensers = [
            dict(d, quantidade_atual=0) for d in painel_com_slots.central.dispensers
        ]
        modulo._dispensers_cache["quando"] -= modulo.DISPENSERS_CACHE_S + 1
        depois = modulo._dispensers_data(conn)
    finally:
        conn.close()

    assert all(d["quantidade"] == 0 for d in depois)


# ── 11. O vocabulário de status é FECHADO ─────────────────────────────────────
#
# `/ordens/<id>/status/<status>` tirava o status da URL e o gravava como veio:
# `POST /ordens/1/status/Qualquer` escrevia "Qualquer" no banco e devolvia 302
# como se nada tivesse acontecido.
#
# O sintoma é o mesmo que a seção 1 deste arquivo cobre pelo lado do espelho, e
# é por isso que ele mora aqui: TODA tela do painel soma por igualdade de
# string — dashboard, /relatorio, /kpis, /api/resumo e a fila que o display
# recebe. Um status fora do vocabulário não derruba nada: a ordem continua no
# banco, aparece na listagem geral e some de todos os contadores. O painel passa
# a afirmar que a planta está em dia.
#
# O central já fazia a checagem no vocabulário dele (`alterar_status_os`); esta
# é a metade de cá. A lista é DERIVADA de `STATUS_CENTRAL_PARA_PAINEL` para que
# não possa ficar atrás do que o próprio espelho grava.

STATUS_ESPERADOS = {"Pendente", "Em Processo", "Pausado", "Concluido",
                    "Erro", "Cancelado"}


def test_a_lista_de_status_e_exatamente_a_do_painel(painel):
    assert painel.modulo.STATUS_VALIDOS == STATUS_ESPERADOS


def test_a_lista_cobre_tudo_que_o_espelho_pode_gravar(painel):
    """Derivada, e não escrita à mão: um status novo no central não pode virar
    uma escrita que os caminhos locais recusam."""
    traduzidos = set(painel.modulo.central_client.STATUS_CENTRAL_PARA_PAINEL.values())
    assert traduzidos <= painel.modulo.STATUS_VALIDOS
    assert painel.modulo.central_client.STATUS_DESCONHECIDO in painel.modulo.STATUS_VALIDOS


@pytest.mark.parametrize("invalido", [
    "Qualquer",        # o caso do relato
    "concluido",       # caixa errada — a comparação é por igualdade exata
    "Concluído",       # com acento: o painel grava sem
    "Em processo",     # quase certo, e por isso o mais provável de escapar
])
def test_web_recusa_status_fora_da_lista(painel, invalido):
    painel.logar()
    oid = painel.criar_ordem_local("OS-0101", [{"med": "Vitamina C 1g", "qtd": 2}])

    resposta = painel.cliente.post(f"/ordens/{oid}/status/{invalido}",
                                   follow_redirects=True)

    assert resposta.status_code == 200
    assert painel.ordem("OS-0101")["status"] == "Pendente"


def test_status_vazio_nem_chega_na_rota(painel):
    """`/ordens/1/status/` não casa com a regra — quem recusa é o roteador.

    Afirmado aqui para que o 404 fique registrado como o comportamento
    esperado, e não confundido com rota que sumiu.
    """
    painel.logar()
    oid = painel.criar_ordem_local("OS-0106", [{"med": "Vitamina C 1g", "qtd": 2}])

    assert painel.cliente.post(f"/ordens/{oid}/status/").status_code == 404
    assert painel.ordem("OS-0106")["status"] == "Pendente"


def test_api_recusa_status_fora_da_lista(painel):
    oid = painel.criar_ordem_local("OS-0102", [{"med": "Vitamina C 1g", "qtd": 2}])

    resposta = painel.api("put", f"/api/ordens/{oid}/status",
                          json={"status": "Qualquer"})

    assert resposta.status_code == 409
    assert resposta.get_json() == {"ok": False, "erro": "status invalido"}
    assert painel.ordem("OS-0102")["status"] == "Pendente"


def test_serial_recusa_status_fora_da_lista(painel):
    """O display manda o status como texto livre, como a API."""
    painel.criar_ordem_local("OS-0103", [{"med": "Vitamina C 1g", "qtd": 2}])

    conn = painel.conexao()
    try:
        ok, erro = painel.modulo._set_status_by_numero_os(conn, "OS-0103", "Qualquer")
    finally:
        conn.close()

    assert (ok, erro) == (False, "status invalido")
    assert painel.ordem("OS-0103")["status"] == "Pendente"


@pytest.mark.parametrize("status", sorted(STATUS_ESPERADOS))
def test_todo_status_da_lista_e_gravavel_pelos_tres_caminhos(painel, status):
    """A recusa não pode virar um bloqueio geral — inclusive de `Erro` e
    `Cancelado`, que são os valores novos e os que nenhum botão da web produz."""
    painel.logar()
    oid = painel.criar_ordem_local(f"OS-W-{status}",
                                   [{"med": "Vitamina C 1g", "qtd": 2}])

    painel.cliente.post(f"/ordens/{oid}/status/{status}", follow_redirects=True)
    assert painel.ordem(f"OS-W-{status}")["status"] == status

    conn = painel.conexao()
    try:
        ok, _ = painel.modulo._set_status_by_numero_os(conn, f"OS-W-{status}",
                                                       "Pendente")
    finally:
        conn.close()
    assert ok is True

    resposta = painel.api("put", f"/api/ordens/{oid}/status", json={"status": status})
    assert resposta.status_code == 200
    assert painel.ordem(f"OS-W-{status}")["status"] == status


def test_status_invalido_nao_chega_a_tocar_no_banco(painel):
    """A recusa vem ANTES do UPDATE: `data_atualizacao` não pode se mexer.

    Linha carimbada com a hora de uma escrita recusada manda quem investiga
    procurar uma alteração que nunca houve.
    """
    oid = painel.criar_ordem_local("OS-0104", [{"med": "Vitamina C 1g", "qtd": 2}])
    antes = dict(painel.ordem("OS-0104"))
    painel.logar()

    painel.cliente.post(f"/ordens/{oid}/status/Qualquer", follow_redirects=True)

    assert dict(painel.ordem("OS-0104")) == antes


def test_status_invalido_nao_sai_dos_contadores(painel):
    """O sintoma que fechava o ciclo: a ordem some de toda tela que soma.

    `/api/resumo` conta por igualdade de string, como o dashboard e os KPIs.
    Com a ordem recusada e mantida em "Pendente", ela continua sendo contada —
    que é a diferença entre uma recusa e um estado fantasma.
    """
    oid = painel.criar_ordem_local("OS-0105", [{"med": "Vitamina C 1g", "qtd": 2}])
    pendentes_antes = painel.api("get", "/api/resumo").get_json()["pendentes"]
    painel.logar()

    painel.cliente.post(f"/ordens/{oid}/status/Fantasma", follow_redirects=True)

    assert painel.api("get", "/api/resumo").get_json()["pendentes"] == pendentes_antes
