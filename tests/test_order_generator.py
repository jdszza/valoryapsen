"""Ordens padrão e backpressure do order-generator.

Duas famílias de teste vivem aqui porque as duas prendem o mesmo processo:

**As 10 ordens padrão.** O conteúdo de cada OS deixou de ser sorteado; o que o
gerador sorteia é qual TEMPLATE dispara. Os invariantes presos aqui são os que,
se quebrarem, quebram justamente na frente da banca: um medicamento que não
existe no catálogo (a OS sai com `sku` vazio e a câmera do dispenser não tem o
que comparar), uma ordem com mais itens que dispensers (abortada com `sem_slot`
depois de ocupar a fila) e — o mais silencioso — dois disparos do mesmo template
com o mesmo `os_id`, que o central recusaria com 409 `os_duplicada`.

**O backpressure.** O gerador posta uma OS a cada `INTERVALO_OS` (90s) e a
planta leva de 90 a 140s por OS — sob trava do Triple Check, tempo
indeterminado. Antes de disparar, ele consulta `GET /api/v1/fila`; o que estes
testes prendem é o comportamento nas bordas, porque as duas maneiras de errar
aqui são graves: laço apertado (martelando o central) e processo encerrado
(planta sem OS até alguém notar).
"""
import re

import pytest

from conftest import NUM_SLOTS


# ══════════════════════════════════════════════════════════════════════════════
# AS 10 ORDENS PADRÃO (central-computer/os_templates.py)
# ══════════════════════════════════════════════════════════════════════════════

def _catalogo_do_seed():
    """Nomes do catálogo REAL, lidos do `_MEDICAMENTOS_SEED` do `database.py`.

    Por texto e não por import: `database.py` importa `config` e o PyMySQL, e
    esta suíte não sobe nenhum dos dois só para conferir uma lista de nomes. O
    que importa é que a fonte seja o seed de verdade — uma lista copiada para
    dentro do teste concordaria consigo mesma para sempre.
    """
    from conftest import RAIZ_REPO

    src = (RAIZ_REPO / "central-computer" / "database.py").read_text(encoding="utf-8")
    bloco = src.split("_MEDICAMENTOS_SEED = [", 1)[1].split("\n]", 1)[0]
    return set(re.findall(r'^\s*\("([^"]+)"', bloco, re.M))


def test_existem_dez_ordens_padrao(os_templates):
    assert len(os_templates.TEMPLATES) == 10
    ids = [t["template_id"] for t in os_templates.TEMPLATES]
    assert len(set(ids)) == 10, f"template_id duplicado em {ids}"


def test_toda_ordem_tem_entre_2_e_num_slots_itens(os_templates):
    """O teto é físico: mais itens que dispensers não tem como ser atribuído."""
    for template in os_templates.TEMPLATES:
        n = len(template["itens"])
        assert 2 <= n <= NUM_SLOTS, f"{template['template_id']} tem {n} itens"


def test_quantidades_dentro_da_faixa(os_templates):
    for template in os_templates.TEMPLATES:
        for item in template["itens"]:
            assert os_templates.QTD_MIN <= item["quantidade"] <= os_templates.QTD_MAX, (
                f"{template['template_id']}: {item['medicamento']} "
                f"x{item['quantidade']}"
            )


def test_nenhum_item_duplicado_dentro_da_mesma_ordem(os_templates):
    """Item repetido viraria dois slots com o mesmo medicamento na mesma OS."""
    for template in os_templates.TEMPLATES:
        nomes = [item["medicamento"] for item in template["itens"]]
        assert len(nomes) == len(set(nomes)), f"{template['template_id']}: {nomes}"


def test_toda_ordem_tem_descricao_legivel(os_templates):
    """Sem descrição a OS aparece sem rótulo no dashboard e no console."""
    for template in os_templates.TEMPLATES:
        assert template["descricao"].strip()
        assert template["nome"].strip()


def test_ao_menos_uma_ordem_ocupa_a_celula_inteira(os_templates):
    """A demonstração precisa de um caso de rota completa em serpentina."""
    tamanhos = [len(t["itens"]) for t in os_templates.TEMPLATES]
    assert NUM_SLOTS in tamanhos, f"nenhuma ordem com {NUM_SLOTS} itens: {tamanhos}"


def test_ao_menos_uma_ordem_curta(os_templates):
    """E de um caso curto, que fecha rápido quando o tempo aperta."""
    assert min(len(t["itens"]) for t in os_templates.TEMPLATES) <= 3


def test_ao_menos_duas_ordens_compartilham_um_medicamento(os_templates):
    """É o que faz o reaproveitamento de residual do `atribuir_slots` aparecer.

    Sem medicamento em comum entre ordens, o passo 1 (slot com o mesmo
    medicamento e residual > 0) nunca dispara e toda OS recarrega tudo.
    """
    ocorrencias: dict[str, set[str]] = {}
    for template in os_templates.TEMPLATES:
        for item in template["itens"]:
            ocorrencias.setdefault(item["medicamento"], set()).add(
                template["template_id"])

    compartilhados = {m: ids for m, ids in ocorrencias.items() if len(ids) >= 2}
    assert compartilhados, "nenhum medicamento aparece em duas ordens diferentes"


def test_todo_medicamento_existe_no_catalogo_real(os_templates):
    """Nome inventado é o erro mais provável de aparecer em cima da hora."""
    assert os_templates.validar_contra_catalogo(_catalogo_do_seed()) == []


def test_estrutura_valida_contra_a_celula_real(os_templates):
    assert os_templates.validar_estrutura(num_slots=NUM_SLOTS) == []


# ── Validação: o que ela precisa REPROVAR ─────────────────────────────────────

def test_validacao_reprova_medicamento_inexistente(os_templates):
    catalogo = _catalogo_do_seed() - {"ALOIS 10MG"}

    problemas = os_templates.validar_contra_catalogo(catalogo)

    assert len(problemas) == 1
    assert "ALOIS 10MG" in problemas[0]


def test_validacao_reprova_template_com_mais_itens_que_slots(os_templates):
    grande = {
        "template_id": "OS-GRANDE",
        "descricao":   "excede a célula",
        "categoria":   "snc",
        "itens": [{"medicamento": f"M{i}", "quantidade": 2}
                  for i in range(NUM_SLOTS + 1)],
    }

    problemas = os_templates.validar_estrutura([grande], num_slots=NUM_SLOTS)

    assert any("OS-GRANDE" in p and str(NUM_SLOTS) in p for p in problemas)


def test_validacao_reprova_quantidade_fora_da_faixa_e_item_repetido(os_templates):
    ruim = {
        "template_id": "OS-RUIM",
        "descricao":   "duas coisas erradas",
        "categoria":   "snc",
        "itens": [
            {"medicamento": "ALOIS 10MG", "quantidade": 99},
            {"medicamento": "ALOIS 10MG", "quantidade": 3},
        ],
    }

    problemas = os_templates.validar_estrutura([ruim], num_slots=NUM_SLOTS)

    assert any("99" in p for p in problemas)
    assert any("mais de uma vez" in p for p in problemas)


# ── os_id: fixo é o template, não a chave ─────────────────────────────────────

def test_dois_disparos_do_mesmo_template_geram_os_id_diferentes(os_templates):
    """`ordens.os_id` é UNIQUE: id repetido viraria 409 no segundo disparo."""
    template = os_templates.TEMPLATES[0]

    ids = {os_templates.instanciar(template)["os_id"] for _ in range(50)}

    assert len(ids) == 50


def test_os_id_carrega_o_template_e_cabe_em_60_caracteres(os_templates):
    for template in os_templates.TEMPLATES:
        os_id = os_templates.instanciar(template)["os_id"]
        assert os_id.startswith(template["template_id"] + "-")
        assert len(os_id) <= os_templates.TAM_MAX_OS_ID, os_id


def test_instanciar_resolve_sku_do_catalogo_nao_do_template(os_templates):
    """SKU congelado no template envelheceria em silêncio — e o SKU é o que a
    câmera do dispenser compara."""
    template = os_templates.por_id("OS-URO-01")
    catalogo = {"RETEMIC 5MG": {"sku": "SKU-NOVO", "categoria": "urologia"}}

    payload = os_templates.instanciar(template, catalogo)

    itens = {m["medicamento"]: m for m in payload["medicamentos"]}
    assert itens["RETEMIC 5MG"]["sku"] == "SKU-NOVO"
    # Item fora do catálogo não ganha SKU inventado.
    assert itens["UNOPROST 2MG"]["sku"] == ""


def test_listar_devolve_copia(os_templates):
    """O endpoint enriquece os itens com o catálogo; mutar o original faria a
    segunda chamada devolver a primeira já enriquecida."""
    copia = os_templates.listar()
    copia[0]["itens"][0]["medicamento"] = "MEXIDO"

    assert os_templates.TEMPLATES[0]["itens"][0]["medicamento"] != "MEXIDO"


# ══════════════════════════════════════════════════════════════════════════════
# ORDER-GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def gerador(carregar_simulador):
    """Order-generator com `requests` duplado e esperas de 0s (teste rápido)."""
    return carregar_simulador(
        "order-generator/simulator.py",
        env={"ESPERA_FILA_CHEIA": "0", "MAX_ESPERAS_FILA": "3"},
    )


def _templates_e_catalogo(os_templates):
    """Templates como o endpoint os serve, e o catálogo como o gerador o indexa."""
    templates = os_templates.listar()
    catalogo = {
        nome: {"nome": nome, "sku": f"SKU-{nome}", "categoria": "generica"}
        for nome in os_templates.nomes_usados()
    }
    return templates, catalogo


# ── Sorteio e instanciação ────────────────────────────────────────────────────

def test_gerador_instancia_os_id_unico_por_disparo(gerador, os_templates):
    templates, catalogo = _templates_e_catalogo(os_templates)
    template = templates[0]

    ids = {gerador.modulo._instanciar_os(template, catalogo)["os_id"]
           for _ in range(50)}

    assert len(ids) == 50
    assert all(i.startswith(template["template_id"] + "-") for i in ids)
    assert all(len(i) <= 60 for i in ids)


def test_gerador_preserva_itens_e_quantidades_do_template(gerador, os_templates):
    """O template é fixo: o que varia entre disparos é só a chave primária."""
    templates, catalogo = _templates_e_catalogo(os_templates)
    template = next(t for t in templates if len(t["itens"]) == NUM_SLOTS)

    a = gerador.modulo._instanciar_os(template, catalogo)
    b = gerador.modulo._instanciar_os(template, catalogo)

    assert a["os_id"] != b["os_id"]
    assert a["medicamentos"] == b["medicamentos"]
    assert [m["medicamento"] for m in a["medicamentos"]] == \
           [i["medicamento"] for i in template["itens"]]
    assert [m["quantidade"] for m in a["medicamentos"]] == \
           [i["quantidade"] for i in template["itens"]]


def test_gerador_sorteia_o_template_e_nao_o_conteudo(gerador, os_templates):
    """O acaso que sobrou é QUAL ordem dispara — em 200 sorteios as 10 aparecem."""
    templates, _ = _templates_e_catalogo(os_templates)

    sorteados = {gerador.modulo._sortear_template(templates)["template_id"]
                 for _ in range(200)}

    assert sorteados == {t["template_id"] for t in templates}


# ── Validação no startup do gerador ───────────────────────────────────────────

def test_gerador_reprova_medicamento_fora_do_catalogo(gerador, os_templates):
    templates, catalogo = _templates_e_catalogo(os_templates)
    catalogo.pop("ALOIS 10MG")

    problemas = gerador.modulo._problemas_dos_templates(templates, catalogo)

    assert problemas, "medicamento inexistente passou pela validação"
    assert all("ALOIS 10MG" in p for p in problemas)


def test_gerador_reprova_template_maior_que_a_celula(gerador, os_templates):
    _, catalogo = _templates_e_catalogo(os_templates)
    grande = {
        "template_id": "OS-GRANDE",
        "descricao":   "excede a célula",
        "itens": [{"medicamento": "ALOIS 10MG", "quantidade": 2}] * (NUM_SLOTS + 1),
    }

    problemas = gerador.modulo._problemas_dos_templates([grande], catalogo)

    assert any("OS-GRANDE" in p and "dispensers" in p for p in problemas)


def test_gerador_descarta_o_template_quebrado_e_mantem_os_outros(gerador, os_templates):
    """Uma ordem com nome errado não pode tirar as outras nove do ar."""
    templates, catalogo = _templates_e_catalogo(os_templates)
    catalogo.pop("ALOIS 10MG")
    quebrados = {t["template_id"] for t in templates
                 if any(i["medicamento"] == "ALOIS 10MG" for i in t["itens"])}

    validos = gerador.modulo._validar_templates(templates, catalogo)

    assert quebrados, "fixture perdeu o pressuposto: ninguém usa ALOIS 10MG"
    assert {t["template_id"] for t in validos} == \
           {t["template_id"] for t in templates} - quebrados


def test_gerador_encerra_quando_nenhuma_ordem_sobra(gerador, os_templates):
    """Sem ordem disparável, acordar a cada INTERVALO_OS sem nada a enviar
    esconderia o motivo — encerrar deixa o erro no log de boot."""
    templates, _ = _templates_e_catalogo(os_templates)

    with pytest.raises(SystemExit):
        gerador.modulo._validar_templates(templates, {})


def test_gerador_aceita_os_dez_templates_com_o_catalogo_real(gerador, os_templates):
    catalogo = {nome: {"nome": nome, "sku": f"SKU-{nome}", "categoria": "x"}
                for nome in _catalogo_do_seed()}

    validos = gerador.modulo._validar_templates(os_templates.listar(), catalogo)

    assert len(validos) == 10


def test_gerador_le_os_templates_do_central(gerador, os_templates):
    """Fonte única: o gerador não tem cópia própria das ordens."""
    gerador.requests.payload = {"templates": os_templates.listar(),
                                "total": 10, "problemas": []}

    templates = gerador.modulo._carregar_templates(tentativas=1)

    assert len(templates) == 10
    assert gerador.chamadas[0]["url"].endswith("/api/v1/ordens/templates")


# ── Backpressure ──────────────────────────────────────────────────────────────

def _responder_fila(gerador, **campos):
    gerador.requests.status_code = 200
    gerador.requests.payload = {"tamanho": 0, "capacidade": 5, "disponivel": 5,
                                "cheia": False, "os_ativa": False,
                                "trava_ativa": False, **campos}


def test_fila_com_vaga_libera_na_primeira_consulta(gerador):
    _responder_fila(gerador, tamanho=2, disponivel=3)

    assert gerador.modulo._esperar_vaga_na_fila() is True
    assert len(gerador.chamadas) == 1


def test_fila_cheia_espera_e_desiste_do_ciclo(gerador):
    """Desistir do ciclo é o que evita o laço apertado — e não encerra nada."""
    _responder_fila(gerador, tamanho=5, disponivel=0, cheia=True, trava_ativa=True)

    assert gerador.modulo._esperar_vaga_na_fila() is False
    # Uma consulta por espera, e nem uma a mais: o teto é MAX_ESPERAS_FILA.
    assert len(gerador.chamadas) == 3


def test_vaga_que_abre_no_meio_da_espera_interrompe_o_ciclo_de_espera(gerador):
    _responder_fila(gerador, tamanho=5, disponivel=0, cheia=True)

    chamadas = []
    original = gerador.requests.get

    def _get(url, **kwargs):
        chamadas.append(url)
        if len(chamadas) == 2:            # a fila esvaziou entre as consultas
            _responder_fila(gerador, tamanho=1, disponivel=4)
        return original(url, **kwargs)

    gerador.requests.get = _get

    assert gerador.modulo._esperar_vaga_na_fila() is True
    assert len(chamadas) == 2


def test_central_fora_do_ar_nao_trava_a_geracao(gerador):
    """Consulta auxiliar quebrada não pode parar a planta — o POST decide."""
    gerador.requests.status_code = 503

    assert gerador.modulo._esperar_vaga_na_fila() is True
    assert len(gerador.chamadas) == 1


def test_429_e_descartado_sem_retentar(gerador):
    """Recusa não vira reenvio: o próximo disparo já nasce com `os_id` novo."""
    gerador.requests.status_code = 429
    gerador.requests.payload = {"erro": "fila_cheia",
                                "fila": {"tamanho": 5, "capacidade": 5}}

    aceita = gerador.modulo._enviar_os({"os_id": "OS-1", "medicamentos": []})

    assert aceita is False
    assert len(gerador.chamadas) == 1          # uma tentativa, nenhuma retentativa
