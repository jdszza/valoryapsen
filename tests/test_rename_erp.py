# -*- coding: utf-8 -*-
"""O nome antigo do serviço de ordens não pode voltar ao repositório.

O serviço deixou de gerar o conteúdo das ordens quando as dez passaram a ser
fixas: hoje ele escolhe uma e a despacha, fazendo o papel do ERP que, na planta
real, emite as ordens de saída. O nome anterior descrevia uma geração que não
existe mais, e o rename foi total — diretório, serviço e container do compose,
targets do Makefile, prefixo de log, testes, comentários e documentação.

A guarda é a mesma de `test_rename_manut.py`, e pelo mesmo motivo: rename
espalhado por vinte arquivos volta pela borda — um merge que ressuscita um
comentário, um README copiado de versão antiga, um `log-<nome antigo>` novo no
Makefile. Nada disso quebra teste nenhum, e o sistema segue funcionando com dois
nomes para a mesma coisa, que é exatamente o estado que o rename foi feito para
acabar. Varredura, e não lista de arquivos: ela pega o arquivo que ainda nem
existe.

A agulha é montada por concatenação — escrita por extenso, este arquivo seria a
primeira vítima do teste que ele define.
"""
import subprocess
from pathlib import Path

import pytest
import yaml

RAIZ_REPO = Path(__file__).resolve().parent.parent

# Montadas em duas metades de propósito — ver o docstring do módulo.
AGULHAS = (
    "order" + "-generator",
    "order" + "_generator",
    "order" + "-gen",
    "order" + " generator",
    "order" + "_gen",
)

SERVICO = "erp-simulator"
CONTAINER = "apsen-erp-sim"


def _arquivos_versionados() -> list[str]:
    try:
        saida = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=RAIZ_REPO, capture_output=True, check=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"git indisponível para listar o índice: {exc}")
    return [caminho for caminho in saida.split("\0") if caminho]


def _e_binario(dados: bytes) -> bool:
    """Heurística do próprio git: NUL no conteúdo = binário, não se lê como texto."""
    return b"\x00" in dados[:8000]


@pytest.fixture(scope="module")
def versionados() -> list[str]:
    arquivos = _arquivos_versionados()
    assert arquivos, "git ls-files não devolveu nada — repositório lido errado?"
    return arquivos


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((RAIZ_REPO / "docker-compose.yml").read_text(encoding="utf-8"))


def test_nenhum_caminho_versionado_carrega_o_nome_antigo(versionados):
    """Diretório e arquivo de teste: o nome no disco conta como ocorrência."""
    culpados = [c for c in versionados
                if any(a in c.lower() for a in AGULHAS)]
    assert not culpados, (
        f"caminhos ainda com o nome antigo (use '{SERVICO}'): {culpados}"
    )


def test_nenhum_arquivo_versionado_menciona_o_nome_antigo(versionados):
    culpados = []
    for caminho in versionados:
        arquivo = RAIZ_REPO / caminho
        if not arquivo.is_file():        # entrada no índice sem arquivo no disco
            continue
        dados = arquivo.read_bytes()
        if _e_binario(dados):
            continue
        texto = dados.decode("utf-8", errors="replace")
        for n, linha in enumerate(texto.splitlines(), 1):
            baixa = linha.lower()
            if any(a in baixa for a in AGULHAS):
                culpados.append(f"{caminho}:{n}: {linha.strip()[:100]}")

    assert not culpados, (
        f"o nome antigo ainda aparece em {len(culpados)} linha(s) — troque por "
        f"'{SERVICO}':\n" + "\n".join(culpados)
    )


def test_a_varredura_realmente_enxerga_o_conteudo(versionados):
    """Guarda da guarda: um teste que não lê nada passa sempre.

    Se `_e_binario` ou a decodificação passarem a engolir os arquivos de texto,
    a varredura acima ficaria verde para sempre — inclusive com o nome antigo
    espalhado pelo repositório. Este teste prende o contrário: o NOVO nome tem
    que ser encontrado, e em mais de um arquivo.
    """
    achados = []
    for caminho in versionados:
        arquivo = RAIZ_REPO / caminho
        if not arquivo.is_file():
            continue
        dados = arquivo.read_bytes()
        if _e_binario(dados):
            continue
        if SERVICO in dados.decode("utf-8", errors="replace"):
            achados.append(caminho)

    assert len(achados) >= 5, (
        f"a varredura achou '{SERVICO}' em só {len(achados)} arquivo(s) — ela "
        f"provavelmente parou de ler o conteúdo: {achados}"
    )


def test_o_servico_existe_no_compose_com_o_nome_novo(compose):
    servicos = compose["services"]
    assert SERVICO in servicos
    assert servicos[SERVICO]["container_name"] == CONTAINER
    assert servicos[SERVICO]["build"] == f"./{SERVICO}"


def test_o_diretorio_foi_movido(versionados):
    """`git mv`, e não copiar-e-apagar: o histórico do arquivo tem que seguir."""
    assert f"{SERVICO}/simulator.py" in versionados
    assert f"{SERVICO}/Dockerfile" in versionados
    assert (RAIZ_REPO / SERVICO / "simulator.py").is_file()


def test_o_makefile_tem_o_target_novo():
    makefile = (RAIZ_REPO / "Makefile").read_text(encoding="utf-8")
    assert "log-erp:" in makefile
    assert f"--tail=$(LOG_LINES) {SERVICO}" in makefile


def test_o_prefixo_de_log_acompanhou_o_rename():
    """O log é onde o nome antigo mais sobrevive: ele não quebra nada e só
    aparece para quem está lendo `docker compose logs` às pressas."""
    fonte = (RAIZ_REPO / SERVICO / "simulator.py").read_text(encoding="utf-8")
    assert "[ERP-SIM]" in fonte


def test_a_rota_do_interruptor_NAO_foi_renomeada():
    """Rename de serviço não é rename de contrato.

    `/api/v1/gerador` e `_estado["gerador_pausado"]` ficaram como estavam — a
    mesma regra que manteve as rotas `/manutencao/*` no rename do app de
    manutenção. Renomeá-las de carona quebraria o dashboard e o console, que
    leem o campo, sem ganho nenhum de clareza para quem opera.
    """
    fonte_central = (RAIZ_REPO / "central-computer" / "main.py").read_text(encoding="utf-8")
    fonte_erp = (RAIZ_REPO / SERVICO / "simulator.py").read_text(encoding="utf-8")
    assert '"/api/v1/gerador"' in fonte_central
    assert '"/api/v1/gerador"' in fonte_erp
    assert '"gerador_pausado"' in fonte_central


def test_as_env_vars_do_servico_continuam_as_mesmas(compose):
    """Elas estão no `.env` de quem já roda a planta, e nenhuma carrega o nome
    antigo — renomeá-las seria quebrar instalação por estética."""
    env = compose["services"][SERVICO]["environment"]
    for chave in ("INTERVALO_OS", "ESPERA_FILA_CHEIA", "MAX_ESPERAS_FILA",
                  "RELOAD_CATALOGO_MIN", "ESPERA_PAUSA"):
        assert chave in env, chave
