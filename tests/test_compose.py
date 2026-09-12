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


def test_a_segunda_porta_do_dispenser_adapter_declara_as_quatro_variaveis(servicos):
    """As 8 telas TFT são a SEGUNDA porta do dispenser-adapter (subsistema
    `dispenser_tft`), e o default "http" significa "sem telas" — o adapter
    exatamente como era."""
    ambiente = servicos["dispenser-adapter"]["environment"]
    for sufixo in ("TRANSPORTE", "SERIAL_URL", "SERIAL_BAUD", "ACK_TIMEOUT_S"):
        assert f"DISPENSER_TFT_{sufixo}" in ambiente, f"falta DISPENSER_TFT_{sufixo}"
    assert str(ambiente["DISPENSER_TFT_TRANSPORTE"]).endswith(":-http}")


# ── Profiles: planta simulada × célula montada (Windows) ──────────────────────
# O mini PC da célula roda Windows, e o Docker Desktop não repassa COM para
# container: os três adapters seriais rodam no host. No compose eles e os quatro
# simuladores ficam atrás do profile `simulado`; com `COMPOSE_PROFILES=simulado`
# no .env (o modelo do README) `docker compose up` sobe os 13 serviços como
# sempre, e sem a linha sobem só os que rodam em container na bancada.

SIMULADO = {"dispenser-adapter", "cnc-adapter", "weight-adapter",
            "dispenser-simulator", "cnc-simulator", "weight-simulator"}
SEMPRE_EM_CONTAINER = {"mysql", "central-computer", "vision-adapter", "vision-simulator",
                       "erp-simulator", "dashboard", "manut_web"}
README = COMPOSE.parent / "README.md"


def test_o_que_a_bancada_substitui_esta_no_profile_simulado(servicos):
    for nome in sorted(SIMULADO):
        assert servicos[nome].get("profiles") == ["simulado"], nome


def test_o_que_roda_em_container_na_bancada_nao_tem_profile(servicos):
    """A visão fica: é HTTP, e o simulador serve a bancada até a estação real."""
    assert set(servicos) == SIMULADO | SEMPRE_EM_CONTAINER
    for nome in sorted(SEMPRE_EM_CONTAINER):
        assert not servicos[nome].get("profiles"), nome


def test_o_profile_de_producao_nao_quebra_a_topologia(servicos):
    """Sem o profile, nenhum serviço que sobe depende OBRIGATORIAMENTE de um que
    não sobe — senão o compose recusaria subir a célula montada inteira."""
    for nome in sorted(SEMPRE_EM_CONTAINER):
        dep = servicos[nome].get("depends_on") or {}
        for alvo, cfg in (dep.items() if isinstance(dep, dict) else ((a, {}) for a in dep)):
            if alvo in SIMULADO:
                assert cfg.get("required") is False, (
                    f"{nome} exige {alvo}, que na célula montada roda no host"
                )
                assert cfg.get("condition") == "service_healthy", (
                    f"{nome} → {alvo}: com o profile ligado a prontidão continua valendo"
                )


def test_o_erp_ainda_espera_os_adapters_na_planta_simulada(servicos):
    """`required: false` afrouxa a AUSÊNCIA, não a prontidão: com o profile
    ligado, a primeira OS do boot continua esperando os quatro adapters."""
    dep = servicos["erp-simulator"]["depends_on"]
    for adapter in ("dispenser-adapter", "cnc-adapter", "weight-adapter", "vision-adapter"):
        assert dep[adapter]["condition"] == "service_healthy", adapter
    assert dep["vision-adapter"].get("required", True) is True


def test_o_env_de_exemplo_liga_o_profile_simulado():
    """Sem a linha, `docker compose up` numa máquina de desenvolvimento subiria
    a stack sem adapters — e o ERP avisaria em vez de recusar."""
    texto = README.read_text(encoding="utf-8")
    assert "COMPOSE_PROFILES=simulado" in texto


# ── A robustez do `--profile simulado` explícito ──────────────────────────────
#
# Depender só de `COMPOSE_PROFILES=simulado` no `.env` tem um furo real: quem
# já tem um `.env` de ANTES desta variável existir (uma bancada em produção,
# um script de CI, um clone antigo) roda `docker compose up -d` e recebe,
# **silenciosamente**, só os 7 serviços que não têm profile — a planta
# simulada não sobe, sem erro nenhum dizendo por quê. Medido com
# `docker compose config --services`: sem a variável, 7 serviços; com ela, 13.
#
# A correção não é confiar no `.env`: é o `--profile simulado` na própria
# LINHA DE COMANDO — do `make up` e do Quickstart do README —, que ativa o
# profile não importa o que esteja (ou não) escrito no `.env` de quem está
# rodando. Os testes abaixo prendem essa linha nos dois lugares.

MAKEFILE = COMPOSE.parent / "Makefile"


def test_make_up_ativa_o_profile_simulado_na_linha_de_comando():
    """`make up` não pode depender do `.env` ter a variável — o comando em si
    tem que carregar `--profile simulado`."""
    linhas = MAKEFILE.read_text(encoding="utf-8").splitlines()
    inicio = linhas.index("up:")
    fim = linhas.index("up-bancada:")
    bloco = "\n".join(linhas[inicio:fim])
    assert "docker compose --profile simulado up -d" in bloco


def test_make_up_bancada_zera_o_profile_mesmo_com_env_setado():
    """O caminho inverso: `up-bancada` tem que FORÇAR o profile desligado,
    mesmo que o `.env` (copiado da planta simulada) tenha a variável ligada."""
    linhas = MAKEFILE.read_text(encoding="utf-8").splitlines()
    inicio = linhas.index("up-bancada:")
    fim = next(i for i in range(inicio + 1, len(linhas)) if not linhas[i].strip())
    bloco = "\n".join(linhas[inicio:fim])
    assert "COMPOSE_PROFILES=" in bloco and "docker compose up -d" in bloco


def test_o_quickstart_do_readme_ativa_o_profile_na_linha_de_comando():
    """O primeiro comando que alguém copia e cola não pode depender de ter
    copiado o `.env` inteiro certinho antes."""
    texto = README.read_text(encoding="utf-8")
    assert "docker compose --profile simulado up -d --build" in texto


def test_nenhum_comando_geral_de_up_no_readme_ficou_sem_o_profile():
    """Todo `docker compose up` de escopo GERAL (sem nomear um serviço
    específico) no README carrega `--profile simulado` — nomear um serviço
    único (ex.: `central-computer`, que não tem profile) não precisa, porque
    esse caminho nunca dependeu do profile para funcionar."""
    texto = README.read_text(encoding="utf-8")
    linhas_sem_profile = [
        linha for linha in texto.splitlines()
        if "docker compose up -d" in linha
        and "--profile simulado" not in linha
        and "central-computer" not in linha
    ]
    assert not linhas_sem_profile, linhas_sem_profile


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
