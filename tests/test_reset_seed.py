# -*- coding: utf-8 -*-
"""
Reset da planta e seed de histórico de demonstração.

Duas features que só se parecem por viverem no mesmo painel do console. O que
elas têm em comum é serem as únicas ações destrutivas dele.

**Reset.** Cobre as três coisas que o distinguem de um `_estado = {}`: ele
comanda limpeza FÍSICA nos slots (o medicamento segue dentro do dispenser se
ninguém comandar), fecha no banco as OS que saíram da fila (senão
`get_ordem_ativa` anuncia para sempre uma OS que ninguém vai executar), e
RECUSA enquanto houver OS em execução (resetar por cima de uma OS viva produz
409 na limpeza, tara no meio de uma pesagem e um abort que ninguém provocou).

**Seed.** A geração é pura, então dá para cobrar coerência sem MySQL: as
quantidades das dispensas têm que bater com os itens, as datas ficam na janela
e no turno, o `os_id` se identifica como demonstração, e a série de sensores
precisa ter FORMA — nem ruído puro, nem linha reta.
"""
import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent
CENTRAL_DIR = RAIZ_REPO / "central-computer"

from conftest import NUM_SLOTS  # noqa: E402


def _item(med: str, qtd: int = 10) -> dict:
    return {"medicamento": med, "sku": f"{med[:3].upper()}-1", "categoria": "geral",
            "quantidade": qtd}


def _payload_os(os_id: str, *itens) -> dict:
    return {"os_id": os_id, "descricao": "teste",
            "medicamentos": list(itens) or [_item("Dipirona")]}


# ══════════════════════════════════════════════════════════════════════════════
# RESET
# ══════════════════════════════════════════════════════════════════════════════

def test_reset_comanda_limpeza_em_todos_os_slots(carregar_orquestrador):
    """O ponto da feature: limpar de VERDADE, não só na memória.

    Resetar `_estado["dispensers"]` deixaria o medicamento dentro do dispenser,
    e a etapa 1b da OS seguinte tomaria 409 ao mandar limpar um slot que ninguém
    liberou — o mesmo bug que a seção "Ciclo de vida de um slot" registra.
    """
    orq = carregar_orquestrador()

    relatorio = asyncio.run(orq.modulo.resetar_planta())

    limpezas = orq.adapter.comandos("/comandos/limpar")
    assert sorted(c["dispenser_id"] for c in limpezas) == list(range(1, NUM_SLOTS + 1))
    assert relatorio["slots_limpos"] == list(range(1, NUM_SLOTS + 1))
    assert relatorio["slots_com_falha"] == []


def test_reset_zera_o_estado_de_todos_os_slots(carregar_orquestrador):
    orq = carregar_orquestrador()
    orq.estado["dispensers"]["3"].update({
        "status": "pronto", "medicamento": "Dipirona", "quantidade": 7,
        "quantidade_residual": 7, "os_id": "OS-VELHA",
    })

    asyncio.run(orq.modulo.resetar_planta())

    slot = orq.estado["dispensers"]["3"]
    assert slot["status"] == "idle"
    assert slot["medicamento"] is None
    assert slot["quantidade"] == 0
    assert slot["quantidade_residual"] == 0
    assert slot["os_id"] is None


def test_reset_esvazia_a_fila_e_cancela_as_os_no_banco(carregar_orquestrador):
    """Linha "aguardando" órfã é pior que fila cheia.

    `salvar_ordem` roda ANTES do enfileiramento, então toda OS que espera tem
    linha no banco. Esvaziar só a fila em memória deixaria `get_ordem_ativa`
    caindo no fallback de "aguardando mais antiga" — e o `GET /os/ativa`
    anunciaria, para sempre, uma OS que ninguém vai executar.
    """
    orq = carregar_orquestrador()

    async def _cenario():
        for i in (1, 2, 3):
            await orq.modulo.enfileirar_os(_payload_os(f"OS-{i}"))
        return await orq.modulo.resetar_planta()

    relatorio = asyncio.run(_cenario())

    assert relatorio["os_canceladas"] == ["OS-1", "OS-2", "OS-3"]
    assert orq.modulo.fila_status()["tamanho"] == 0
    assert orq.estado["fila_os"] == []
    assert orq.estado["fila_tamanho"] == 0

    (chamada,) = orq.banco.chamadas_de("cancelar_ordens_pendentes")
    assert chamada["args"][0] == ["OS-1", "OS-2", "OS-3"]


def test_reset_preserva_o_objeto_da_fila(carregar_orquestrador):
    """Drenar com `get_nowait`, nunca trocar a `Queue`.

    O `loop_orquestrador` está pendurado em `await _os_queue.get()`. Substituir
    o objeto o deixaria esperando na fila ANTIGA: o consumidor pararia de
    consumir e a planta ficaria muda, sem erro em lugar nenhum — e o `maxsize`
    iria junto.
    """
    orq = carregar_orquestrador()
    fila_antes = orq.modulo._os_queue
    capacidade = fila_antes.maxsize

    async def _cenario():
        await orq.modulo.enfileirar_os(_payload_os("OS-1"))
        await orq.modulo.resetar_planta()
        # E a fila volta a aceitar, com o mesmo teto.
        return await orq.modulo.enfileirar_os(_payload_os("OS-2"))

    assert asyncio.run(_cenario()) is True
    assert orq.modulo._os_queue is fila_antes
    assert orq.modulo._os_queue.maxsize == capacidade


def test_reset_libera_a_trava(carregar_orquestrador):
    orq = carregar_orquestrador()

    async def _cenario():
        await orq.modulo._ativar_trava("OS-1", 3, "SKU errado em D3")
        # A trava é ativada por uma OS, mas o teste ativa-a solta: o que se
        # verifica aqui é que o reset não deixa trava para trás.
        await orq.modulo.resetar_planta()

    asyncio.run(_cenario())

    assert orq.modulo.get_trava_estado()["ativa"] is False
    assert orq.estado["trava"]["ativa"] is False


def test_reset_desarma_a_falha_injetada(carregar_orquestrador):
    """Gatilho armado é outro estado que sobrevive ao fim de uma OS.

    Deixá-lo armado faria a primeira OS depois do reset falhar "sozinha" — e
    quem resetou acabou de declarar que quer a bancada no estado de boot.
    """
    orq = carregar_orquestrador()
    orq.injecao.resetar()
    orq.injecao.armar("divergencia_peso", 2)

    asyncio.run(orq.modulo.resetar_planta())

    assert orq.injecao.armada() is None
    assert orq.estado["falha_armada"] is None


def test_reset_zera_a_balanca_e_manda_a_cnc_para_home(carregar_orquestrador):
    orq = carregar_orquestrador()

    relatorio = asyncio.run(orq.modulo.resetar_planta())

    assert orq.adapter.comandos("/comandos/tara")
    assert orq.adapter.comandos("/comandos/homing")
    assert relatorio["tara"] is True
    assert relatorio["homing"] is True


def test_tara_vem_depois_da_limpeza_dos_slots(carregar_orquestrador):
    """Ordem importa: zerar a balança antes de mexer na bancada faria o offset
    da tara contar o peso do que ainda estava lá."""
    orq = carregar_orquestrador()

    asyncio.run(orq.modulo.resetar_planta())

    urls = [c["url"] for c in orq.adapter.chamadas]
    ultima_limpeza = max(i for i, u in enumerate(urls) if u.endswith("/comandos/limpar"))
    tara = urls.index(next(u for u in urls if u.endswith("/comandos/tara")))
    assert tara > ultima_limpeza


def test_reset_recusa_com_os_em_execucao(carregar_orquestrador):
    """A decisão central da função.

    O orquestrador é um loop ÚNICO parado dentro de `_processar_os`. Resetar por
    cima manda `cmd_limpar` para um slot que está dispensando (409), zera a
    balança no meio de uma pesagem e aborta a OS por divergência que ninguém
    provocou. Recusar é explícito; reset pela metade é o estado mais difícil de
    diagnosticar que esta planta consegue produzir.
    """
    orq = carregar_orquestrador()
    orq.estado["os_ativa"] = {"os_id": "OS-EM-CURSO", "status": "em_andamento"}

    with pytest.raises(orq.modulo.ResetRecusado) as erro:
        asyncio.run(orq.modulo.resetar_planta())

    assert "OS-EM-CURSO" in str(erro.value)
    # E nada foi tocado: tudo ou nada.
    assert orq.adapter.chamadas == []


def test_recusa_com_trava_ativa_diz_o_que_fazer(carregar_orquestrador):
    """Mensagem diferente, porque a saída é diferente: aqui há um botão a
    clicar antes, e ele está ao lado do de resetar."""
    orq = carregar_orquestrador()
    orq.estado["os_ativa"] = {"os_id": "OS-TRAVADA"}

    async def _cenario():
        await orq.modulo._ativar_trava("OS-TRAVADA", 3, "SKU errado")
        with pytest.raises(orq.modulo.ResetRecusado) as erro:
            await orq.modulo.resetar_planta()
        return str(erro.value)

    mensagem = asyncio.run(_cenario())
    assert "trava" in mensagem.lower()


def test_reset_nao_apaga_historico_por_padrao(carregar_orquestrador):
    """Preservar é o caso comum: quase sempre se quer a bancada limpa COM o
    histórico de pé, que é o que dá forma ao dashboard."""
    orq = carregar_orquestrador()

    relatorio = asyncio.run(orq.modulo.resetar_planta())

    assert relatorio["historico"] is None
    assert orq.banco.chamadas_de("limpar_historico") == []


def test_reset_apaga_historico_quando_pedido(carregar_orquestrador):
    orq = carregar_orquestrador()

    asyncio.run(orq.modulo.resetar_planta(True))

    assert orq.banco.chamadas_de("limpar_historico")


def test_slot_que_nao_confirma_limpeza_aparece_no_relatorio(carregar_orquestrador):
    """Único desfecho que manda alguém olhar a bancada — não pode sumir no OK."""
    orq = carregar_orquestrador()
    orq.adapter.confirma_limpeza = False

    async def _cenario():
        # `TIMEOUT_LIMPEZA` curto: o duplo fica mudo de propósito e a função
        # espera o timeout inteiro em cada slot.
        orq.modulo.settings.TIMEOUT_LIMPEZA = 0.01
        return await orq.modulo.resetar_planta()

    relatorio = asyncio.run(_cenario())

    assert relatorio["slots_com_falha"] == list(range(1, NUM_SLOTS + 1))
    assert relatorio["slots_limpos"] == []
    # E o reset segue até o fim mesmo assim: a balança e a CNC não dependem do
    # dispenser, e parar no meio deixaria a bancada em estado misto.
    assert relatorio["homing"] is True


def test_reset_limpa_os_eventos_pendentes(carregar_orquestrador):
    """Chave órfã é vazamento de memória e, pior, uma notificação futura que
    casaria com a espera errada."""
    orq = carregar_orquestrador()
    orq.modulo.registrar_evento("OS-MORTA:dispensado:3")

    asyncio.run(orq.modulo.resetar_planta())

    assert orq.modulo._pending_events == {}
    assert orq.modulo._pending_data == {}


def test_os_seguinte_ao_reset_roda_normal(carregar_orquestrador):
    """O teste de que o reset não deixa a planta inutilizável."""
    orq = carregar_orquestrador()

    async def _cenario():
        await orq.modulo.resetar_planta()
        await orq.modulo._processar_os(_payload_os("OS-DEPOIS"))

    asyncio.run(_cenario())

    status = [c["args"] for c in orq.banco.chamadas_de("atualizar_status_ordem")]
    assert status[-1] == ("OS-DEPOIS", "concluida")


# ══════════════════════════════════════════════════════════════════════════════
# SEED DE HISTÓRICO
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def seed():
    """`seed_demo.py` por caminho. Módulo puro — importa `os_templates` e mais nada."""
    if str(CENTRAL_DIR) not in sys.path:
        sys.path.insert(0, str(CENTRAL_DIR))
    caminho = CENTRAL_DIR / "seed_demo.py"
    spec = importlib.util.spec_from_file_location("apsen_seed_demo", caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["apsen_seed_demo"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


AGORA = datetime(2026, 9, 11, 16, 40, tzinfo=timezone.utc)


def _gerar(seed, n=25, dias=7, catalogo=None):
    return seed.gerar_historico(n, catalogo, dias=dias, agora=AGORA, semente=7)


def test_gera_a_quantidade_pedida(seed):
    dados = _gerar(seed, n=25)
    assert len(dados["ordens"]) == 25


@pytest.mark.parametrize("n,dias", [(0, 7), (-3, 7), (999, 7), (10, 0), (10, 365)])
def test_pedido_fora_da_faixa_e_recusado(seed, n, dias):
    """422 na rota, não ajuste silencioso: quem pede 10 000 ordens digitou
    errado, e semear isso enche a tabela que o expurgo só limpa em
    `RETENCAO_DIAS` dias."""
    with pytest.raises(seed.SeedInvalido):
        seed.gerar_historico(n, {}, dias=dias, agora=AGORA)


def test_todo_os_id_se_identifica_como_demonstracao(seed):
    """Sem isso, dado fabricado e operação real ficam indistinguíveis em
    qualquer tela, log ou consulta SQL."""
    dados = _gerar(seed)
    for ordem in dados["ordens"]:
        assert ordem["os_id"].startswith(seed.PREFIXO_DEMO), ordem["os_id"]
        assert seed.e_demo(ordem["os_id"])
    assert not seed.e_demo("OS-URO-01-20260911T164000-ABCDEF")


def test_os_id_cabe_na_coluna(seed):
    """`ordens.os_id` é VARCHAR(60) e o prefixo acrescenta 5 caracteres."""
    dados = _gerar(seed, n=60)
    assert max(len(o["os_id"]) for o in dados["ordens"]) <= 60


def test_os_ids_sao_unicos(seed):
    """`ordens.os_id` é UNIQUE — id repetido viraria linha silenciosamente
    perdida pelo INSERT IGNORE, e o histórico sairia menor do que se pediu."""
    dados = _gerar(seed, n=120, dias=14)
    ids = [o["os_id"] for o in dados["ordens"]]
    assert len(set(ids)) == len(ids)


def test_ordens_saem_das_dez_padrao(seed, os_templates):
    """Histórico com medicamento inventado não casaria com o catálogo, e o
    relatório de dispensação apontaria para item que não existe."""
    dados = _gerar(seed)
    descricoes = {t["descricao"] for t in os_templates.listar()}
    for ordem in dados["ordens"]:
        assert ordem["descricao"] in descricoes


def test_quantidades_batem_com_os_templates(seed, os_templates):
    """A coerência que dá sentido ao histórico: dispensa, item e template
    contando a MESMA história."""
    dados = _gerar(seed)
    por_descricao = {t["descricao"]: t for t in os_templates.listar()}
    itens_por_os: dict = {}
    for item in dados["itens"]:
        itens_por_os.setdefault(item["os_id"], []).append(item)

    for ordem in dados["ordens"]:
        template = por_descricao[ordem["descricao"]]
        itens = sorted(itens_por_os[ordem["os_id"]], key=lambda i: i["dispenser_id"])
        assert len(itens) == len(template["itens"])
        for item, modelo in zip(itens, template["itens"]):
            assert item["medicamento"] == modelo["medicamento"]
            assert item["quantidade_alvo"] == modelo["quantidade"]


def test_dispensas_batem_com_os_itens(seed):
    dados = _gerar(seed)
    itens = {(i["os_id"], i["dispenser_id"]): i for i in dados["itens"]}
    assert len(dados["dispensas"]) == len(dados["itens"])
    for d in dados["dispensas"]:
        item = itens[(d["os_id"], d["dispenser_id"])]
        assert d["quantidade_alvo"] == item["quantidade_alvo"]
        assert d["quantidade_dispensada"] == item["quantidade_real"]
        assert d["medicamento"] == item["medicamento"]


def test_sku_vem_do_catalogo_real(seed):
    """Congelar SKU fabricado faria o relatório apontar para um produto que não
    existe em `medicamentos` — o mesmo motivo pelo qual `instanciar` resolve o
    SKU na hora."""
    dados_sem = _gerar(seed)
    assert all(i["sku"] == "" for i in dados_sem["itens"])

    catalogo = {"ALOIS 10MG": {"sku": "SKU-ALOIS-10", "categoria": "snc"}}
    dados_com = _gerar(seed, catalogo=catalogo)
    skus = {i["sku"] for i in dados_com["itens"] if i["medicamento"] == "ALOIS 10MG"}
    assert skus == {"SKU-ALOIS-10"}


def test_os_com_erro_conta_a_mesma_historia_no_item_e_no_alarme(seed):
    """Status "erro" com todas as dispensas fechando certo é pior que histórico
    nenhum: ensina a ler o painel errado."""
    dados = _gerar(seed, n=80, dias=10)
    com_erro = [o for o in dados["ordens"] if o["status"] == "erro"]
    assert com_erro, "nenhuma OS com erro — histórico onde nada dá errado não convence"
    assert len(dados["alarmes"]) == len(com_erro)

    itens_por_os: dict = {}
    for item in dados["itens"]:
        itens_por_os.setdefault(item["os_id"], []).append(item)

    for ordem in com_erro:
        curtos = [i for i in itens_por_os[ordem["os_id"]]
                  if i["quantidade_real"] < i["quantidade_alvo"]]
        assert len(curtos) == 1, ordem["os_id"]
        assert curtos[0]["status"] == "erro"

    for ordem in dados["ordens"]:
        if ordem["status"] == "concluida":
            assert all(i["quantidade_real"] == i["quantidade_alvo"]
                       for i in itens_por_os[ordem["os_id"]])


def test_alarmes_semeados_nascem_resolvidos(seed):
    """Alarme antigo em aberto faria o badge nascer com dezenas de pendências
    falsas — e a tela de necessidades existe justamente para listar o que ainda
    precisa de alguém."""
    dados = _gerar(seed, n=80, dias=10)
    assert dados["alarmes"]
    assert all(a["resolvido"] for a in dados["alarmes"])


def test_datas_ficam_na_janela_e_no_turno(seed):
    dados = _gerar(seed, n=80, dias=5)
    inicio = AGORA - timedelta(days=5)
    for ordem in dados["ordens"]:
        assert inicio <= ordem["criado_em"] <= AGORA, ordem["criado_em"]
        assert seed.HORA_INICIO <= ordem["criado_em"].hour < seed.HORA_FIM
        assert ordem["concluida_em"] > ordem["criado_em"]


def test_ordens_saem_ordenadas_no_tempo(seed):
    dados = _gerar(seed, n=40)
    momentos = [o["criado_em"] for o in dados["ordens"]]
    assert momentos == sorted(momentos)


def test_duracao_cresce_com_o_numero_de_slots(seed, os_templates):
    """Sem isso, a OS de 8 slots e a de 2 apareceriam com a mesma duração no
    relatório de tempo de ciclo."""
    dados = _gerar(seed, n=120, dias=14)
    tamanho = {t["descricao"]: len(t["itens"]) for t in os_templates.listar()}
    duracoes: dict = {}
    for ordem in dados["ordens"]:
        n = tamanho[ordem["descricao"]]
        duracoes.setdefault(n, []).append(
            (ordem["concluida_em"] - ordem["criado_em"]).total_seconds())

    medias = {n: sum(v) / len(v) for n, v in duracoes.items()}
    ordenadas = [medias[n] for n in sorted(medias)]
    assert ordenadas == sorted(ordenadas), medias


def test_dispensas_ficam_dentro_da_janela_da_os(seed):
    dados = _gerar(seed)
    janela = {o["os_id"]: (o["criado_em"], o["concluida_em"]) for o in dados["ordens"]}
    for d in dados["dispensas"]:
        inicio, fim = janela[d["os_id"]]
        assert inicio < d["ts"] <= fim, d["os_id"]


def test_leituras_cobrem_os_componentes_que_os_simuladores_emitem(seed):
    """Série sob outro nome não apareceria em tela nenhuma: o app de manutenção
    consulta `/manutencao/sensores/{componente}` pelo nome que está no banco."""
    dados = _gerar(seed)
    nomes = {l["componente"] for l in dados["leituras"]}
    for i in range(1, 9):
        assert f"dispenser_{i}" in nomes
    assert {"motor_eixo_x", "driver_y", "placa_cnc"} <= nomes
    assert {"camera_dispenser_esq", "camera_dispenser_dir", "camera_mesa"} <= nomes
    assert "hx711_balanca_mesa" in nomes


def test_serie_de_sensor_tem_forma(seed):
    """Nem ruído puro, nem linha reta — é o que a task pede e o que faz o
    gráfico dizer alguma coisa.

    Duas medidas, e cada uma reprova um dos dois extremos: a amplitude do ciclo
    diário tem que ser grande o bastante para aparecer (linha reta reprova), e
    a diferença entre pontos VIZINHOS tem que ser bem menor que essa amplitude
    (ruído puro reprova).
    """
    dados = _gerar(seed, dias=3)
    serie = [l for l in dados["leituras"] if l["componente"] == "driver_x"]
    serie.sort(key=lambda l: l["ts"])
    assert len(serie) > 100

    valores = [l["valor"] for l in serie]
    amplitude = max(valores) - min(valores)
    assert amplitude > 3.0, "série chapada — não desenha nada"

    saltos = [abs(b - a) for a, b in zip(valores, valores[1:])]
    salto_medio = sum(saltos) / len(saltos)
    assert salto_medio < amplitude / 4, "ruído domina — a forma some no chuvisco"

    # E a temperatura da tarde é maior que a da madrugada: é essa a forma.
    dia = [l["valor"] for l in serie if 12 <= l["ts"].hour < 17]
    noite = [l["valor"] for l in serie if 1 <= l["ts"].hour < 5]
    assert sum(dia) / len(dia) > sum(noite) / len(noite)


def test_semente_torna_a_geracao_reproduzivel(seed):
    """Existe para o teste poder afirmar o mesmo conteúdo duas vezes — não para
    a demonstração ser sempre igual."""
    a = _gerar(seed)
    b = _gerar(seed)
    assert [o["os_id"] for o in a["ordens"]] == [o["os_id"] for o in b["ordens"]]


def test_sem_semente_dois_seeds_diferem(seed):
    a = seed.gerar_historico(10, {}, dias=3, agora=AGORA)
    b = seed.gerar_historico(10, {}, dias=3, agora=AGORA)
    assert [o["os_id"] for o in a["ordens"]] != [o["os_id"] for o in b["ordens"]]


# ── O seed nunca roda sozinho ─────────────────────────────────────────────────

def test_nenhum_caminho_automatico_chama_o_seed():
    """A regra de segurança da feature, cobrada por varredura.

    Um seed que rodasse no boot transformaria o primeiro `docker compose up` de
    uma instalação de verdade em dado inventado no banco de produção. O único
    chamador permitido é a rota do console.
    """
    import ast

    arvore = ast.parse((CENTRAL_DIR / "main.py").read_text(encoding="utf-8"))
    chamadores: list = []

    class _Visita(ast.NodeVisitor):
        def __init__(self):
            self.funcao = None

        def visit_FunctionDef(self, no):
            anterior, self.funcao = self.funcao, no.name
            self.generic_visit(no)
            self.funcao = anterior

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, no):
            alvo = no.func
            nome = (alvo.attr if isinstance(alvo, ast.Attribute)
                    else alvo.id if isinstance(alvo, ast.Name) else "")
            if nome in ("semear_historico_demo", "gerar_historico"):
                chamadores.append(self.funcao)
            self.generic_visit(no)

    _Visita().visit(arvore)

    assert chamadores, "o seed sumiu de main.py"
    assert set(chamadores) == {"console_seed"}, chamadores


def test_database_nao_chama_o_seed_no_init():
    """Mesma regra do outro lado: `init_db` semeia catálogo e usuários, e
    ninguém pode acrescentar histórico fabricado a essa lista."""
    import ast

    fonte = (CENTRAL_DIR / "database.py").read_text(encoding="utf-8")
    depois_do_def = fonte.split("def init_db", 1)[1].split("\ndef ", 1)[0]
    assert "semear_historico_demo" not in depois_do_def

    # E `database.py` não IMPORTA `seed_demo`: a geração é pura e mora lá, a
    # persistência mora aqui. Citá-lo em comentário é o desejado — importá-lo é
    # que criaria o acoplamento que a separação existe para evitar.
    for no in ast.walk(ast.parse(fonte)):
        if isinstance(no, ast.Import):
            assert all(a.name != "seed_demo" for a in no.names)
        elif isinstance(no, ast.ImportFrom):
            assert no.module != "seed_demo"


def test_limpeza_de_historico_nao_toca_em_catalogo_nem_usuarios():
    """Catálogo e contas não são histórico. Apagar `medicamentos` junto faria
    o reset deixar a planta incapaz de instanciar qualquer OS."""
    import ast

    arvore = ast.parse((CENTRAL_DIR / "database.py").read_text(encoding="utf-8"))
    for no in arvore.body:
        alvos = no.targets if isinstance(no, ast.Assign) else []
        if any(isinstance(a, ast.Name) and a.id == "_TABELAS_HISTORICO" for a in alvos):
            tabelas = set(ast.literal_eval(no.value))
            break
    else:
        raise AssertionError("_TABELAS_HISTORICO não encontrado")

    assert tabelas == {"visao_leituras", "leituras_sensores", "cnc_eventos",
                       "alarmes", "dispensas", "os_itens", "ordens"}
    assert not tabelas & {"medicamentos", "usuarios", "log_manutencao",
                          "dispenser_estado"}
