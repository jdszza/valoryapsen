"""Guarda do docker-compose.yml.

Só o `mysql` tinha healthcheck, e os `depends_on` dos adapters e das interfaces
eram da forma curta — que garante ORDEM DE START, não prontidão. Na prática o
central e os adapters subiam antes das dependências e ficavam retentando em
silêncio: o boot "funcionava" só porque cada serviço tem laço de retry próprio.

Estes testes valem por leitura estática (PyYAML), sem Docker: prendem o que
some fácil num merge — healthcheck removido, ciclo de dependência e porta
duplicada.
"""
from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"

# O erp-simulator é um worker de laço: não expõe porta nem endpoint, e
# ninguém depende dele. Ver o comentário no próprio compose.
SEM_HEALTHCHECK = {"erp-simulator"}


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def servicos(compose) -> dict:
    return compose["services"]


def test_compose_e_yaml_valido(compose):
    assert compose["services"], "nenhum serviço declarado"


def test_version_obsoleta_foi_removida(compose):
    """`version` é ignorado pelo Compose v2 e só gera warning."""
    assert "version" not in compose


# ── Healthchecks ──────────────────────────────────────────────────────────────

def test_todo_servico_de_aplicacao_tem_healthcheck(servicos):
    faltando = [
        nome for nome, s in servicos.items()
        if nome not in SEM_HEALTHCHECK and not s.get("healthcheck")
    ]
    assert not faltando, f"serviços sem healthcheck: {faltando}"


def test_healthcheck_nao_depende_de_curl(servicos):
    """As imagens são python:slim — não têm curl nem wget."""
    for nome, s in servicos.items():
        teste = s.get("healthcheck", {}).get("test", [])
        texto = " ".join(teste) if isinstance(teste, list) else str(teste)
        assert "curl" not in texto and "wget" not in texto, (
            f"{nome}: healthcheck usa binário que não existe na imagem"
        )


def test_healthcheck_dos_adapters_usa_ping_e_nao_health(servicos):
    """`/health` fica "degradado" se o vizinho cair — derrubaria em cascata.

    Ele também devolve 200 nesse estado, então nem funcionaria como portão sem
    interpretar o corpo. O healthcheck responde por ESTE processo; o diagnóstico
    de upstream continua em /health, para quem for olhar.
    """
    for nome, s in servicos.items():
        if not nome.endswith("-adapter"):
            continue
        texto = " ".join(s["healthcheck"]["test"])
        assert "/ping" in texto, f"{nome}: healthcheck deveria usar /ping"
        assert "/health" not in texto, (
            f"{nome}: healthcheck em /health encadeia a saúde do vizinho"
        )


@pytest.mark.parametrize("campo", ["interval", "timeout", "retries", "start_period"])
def test_healthcheck_tem_os_tempos_definidos(servicos, campo):
    """Sem `start_period`, o serviço nasce unhealthy e trava quem espera por ele."""
    for nome, s in servicos.items():
        hc = s.get("healthcheck")
        if hc:
            assert campo in hc, f"{nome}: healthcheck sem `{campo}`"


# ── Dependências ──────────────────────────────────────────────────────────────

def _dependencias(servico: dict) -> dict:
    """`depends_on` normalizado para {nome: condicao}."""
    dep = servico.get("depends_on") or {}
    if isinstance(dep, list):
        return {nome: "service_started" for nome in dep}
    return {nome: cfg.get("condition", "service_started") for nome, cfg in dep.items()}


def test_nao_existe_dependencia_circular(servicos):
    """O central comanda os adapters, mas NÃO depende deles no compose.

    Ele tolera adapter fora do ar (o `_post` retenta); declarar a dependência
    fecharia o ciclo adapter → central → adapter e o compose recusaria subir.
    """
    grafo = {nome: set(_dependencias(s)) for nome, s in servicos.items()}
    visitando, concluidos = set(), set()

    def _visitar(nome, caminho):
        if nome in concluidos:
            return
        assert nome not in visitando, f"ciclo em depends_on: {' → '.join(caminho + [nome])}"
        visitando.add(nome)
        for vizinho in grafo.get(nome, ()):
            _visitar(vizinho, caminho + [nome])
        visitando.discard(nome)
        concluidos.add(nome)

    for nome in grafo:
        _visitar(nome, [])


def test_dependencias_reais_esperam_prontidao(servicos):
    """`service_started` só garante ordem de criação, não que o serviço responda."""
    fracas = {
        f"{nome} → {alvo}"
        for nome, s in servicos.items()
        for alvo, condicao in _dependencias(s).items()
        if condicao == "service_started"
    }
    assert not fracas, f"depends_on sem condition: service_healthy: {sorted(fracas)}"


def test_toda_dependencia_aponta_para_servico_existente(servicos):
    for nome, s in servicos.items():
        for alvo in _dependencias(s):
            assert alvo in servicos, f"{nome} depende de serviço inexistente: {alvo}"


def test_central_nao_depende_dos_adapters(servicos):
    """Regressão nominal do ciclo que a Task 14 poderia introduzir."""
    dependencias = set(_dependencias(servicos["central-computer"]))
    assert not {d for d in dependencias if d.endswith("-adapter")}


# ── Portas ────────────────────────────────────────────────────────────────────

def test_toda_porta_publicada_e_unica(servicos):
    publicadas = {}
    for nome, s in servicos.items():
        for mapeamento in s.get("ports", []):
            porta_host = str(mapeamento).split(":")[0].strip('"')
            assert porta_host not in publicadas, (
                f"porta {porta_host} publicada por {publicadas[porta_host]} e {nome}"
            )
            publicadas[porta_host] = nome
    assert publicadas, "nenhuma porta publicada — compose lido errado?"


def test_healthcheck_aponta_para_a_porta_publicada(servicos):
    """Healthcheck na porta errada passa a vida toda vermelho (ou verde à toa)."""
    for nome, s in servicos.items():
        hc = s.get("healthcheck")
        portas = s.get("ports")
        if not hc or not portas or nome == "mysql":
            continue
        porta_container = str(portas[0]).split(":")[-1].strip('"')
        assert f":{porta_container}/" in " ".join(hc["test"]), (
            f"{nome}: healthcheck não usa a porta {porta_container}"
        )


# ── Renome para manut_web ─────────────────────────────────────────────────────
# A antiga interface homem-máquina virou o app de MANUTENÇÃO E OPERAÇÃO: mudou o
# nome, não a função. Estes testes prendem os três pontos que um rename esquece
# — o nome do serviço, o do container e a porta — para que o `make log-manut`, o
# `docker compose logs manut_web` e o bookmark do gestor continuem valendo.
#
# O nome antigo é montado por concatenação: escrito por extenso, este arquivo
# reprovaria em `test_rename_manut.py`, que varre o repositório inteiro.
NOME_ANTIGO = "i" + "hm_web"

def test_o_servico_de_manutencao_se_chama_manut_web(servicos):
    assert "manut_web" in servicos, (
        f"serviço manut_web ausente; serviços: {sorted(servicos)}"
    )


def test_nenhum_servico_conserva_o_nome_antigo(servicos):
    assert NOME_ANTIGO not in servicos, (
        f"o serviço {NOME_ANTIGO} foi renomeado para manut_web"
    )


def test_manut_web_publica_a_porta_8051(servicos):
    """A porta é o que o gestor tem no bookmark — o rename não a move."""
    portas = [str(p) for p in servicos["manut_web"].get("ports", [])]
    assert "8051:8051" in portas, f"manut_web não publica 8051: {portas}"


def test_manut_web_constroi_do_diretorio_manut_web(servicos):
    assert servicos["manut_web"]["build"] == "./manut_web"


def test_container_de_manutencao_se_chama_apsen_manut(servicos):
    assert servicos["manut_web"]["container_name"] == "apsen-manut"


# ── Transporte serial dos três adapters com firmware ──────────────────────────
# O dispenser, a CNC e a balança ganharam um segundo transporte (a porta USB do
# firmware). O default continua sendo o simulador HTTP, e é isso que estes
# testes prendem: ligar o serial tem que ser uma decisão explícita de quem opera
# a bancada, nunca o efeito colateral de um merge.

ADAPTERS_SERIAIS = {
    "dispenser-adapter": "DISPENSER",
    "cnc-adapter":       "CNC",
    "weight-adapter":    "WEIGHT",
}


@pytest.mark.parametrize("servico,prefixo", sorted(ADAPTERS_SERIAIS.items()))
def test_adapter_serial_declara_as_quatro_variaveis(servicos, servico, prefixo):
    """As quatro andam juntas: sem a URL não há porta, sem o baud a linha vira
    lixo, e sem o prazo do ACK o endpoint não sabe quando desistir."""
    ambiente = servicos[servico]["environment"]
    for sufixo in ("TRANSPORTE", "SERIAL_URL", "SERIAL_BAUD", "ACK_TIMEOUT_S"):
        assert f"{prefixo}_{sufixo}" in ambiente, f"{servico}: falta {sufixo}"


@pytest.mark.parametrize("servico,prefixo", sorted(ADAPTERS_SERIAIS.items()))
def test_o_default_do_transporte_e_http(servicos, servico, prefixo):
    """"http" é o default para que a suíte, o CI e a demonstração em Docker não
    mudem de resultado por causa desta feature."""
    valor = str(servicos[servico]["environment"][f"{prefixo}_TRANSPORTE"])
    assert valor.endswith(":-http}"), (
        f"{servico}: o transporte default deveria ser http, e é {valor}"
    )


def test_nenhum_servico_mapeia_device_de_verdade(servicos):
    """`devices:` fica COMENTADO até a placa existir.

    Mapear `/dev/ttyUSB0` sem a placa plugada impede o serviço de subir — e
    serviço que não sobe trava, por `depends_on`, tudo que espera por ele. O
    default é o transporte HTTP, então o bloco não tem o que fazer ativo.
    """
    com_device = [nome for nome, s in servicos.items() if s.get("devices")]
    assert not com_device, (
        f"serviços com `devices:` ativo: {com_device}. O exemplo fica comentado."
    )


@pytest.mark.parametrize("servico", sorted(ADAPTERS_SERIAIS))
def test_o_exemplo_de_devices_esta_escrito_no_bloco_do_servico(servico):
    """Comentado, mas PRESENTE: quem for ligar a placa no mini PC não deveria
    ter de descobrir a sintaxe em outro lugar — e o comentário é onde fica
    escrito que ele só vale no Linux."""
    texto = COMPOSE.read_text(encoding="utf-8")
    bloco = texto.split(f"\n  {servico}:", 1)[1].split("\n  # ──", 1)[0]
    assert "# devices:" in bloco, f"{servico}: sem o exemplo de devices"
    assert "ttyUSB" in bloco and "LINUX" in bloco.upper()


def test_o_vision_adapter_ficou_fora_da_migracao_serial(servicos):
    """A visão continua por HTTP, e a ausência é decisão registrada."""
    ambiente = servicos["vision-adapter"]["environment"]
    assert not [c for c in ambiente if "SERIAL" in c or "TRANSPORTE" in c]
