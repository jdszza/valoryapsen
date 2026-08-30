"""A sigla antiga da interface não pode voltar ao repositório.

O que era a interface homem-máquina de chão de fábrica virou o app
de MANUTENÇÃO E OPERAÇÃO, o painel onde o gestor acompanha as necessidades do
sistema. O rename foi total: diretório, serviço do compose, container, targets
do Makefile, testes, comentários e documentação.

Rename espalhado por vinte arquivos volta pela borda: um merge que ressuscita um
comentário, um README copiado de uma versão antiga, um novo `log-<sigla>` no
Makefile. Nada disso quebra teste nenhum — o sistema segue funcionando com dois
nomes para a mesma coisa, que é exatamente o estado que o rename foi feito para
acabar. Por isso a guarda é uma varredura, e não uma lista de arquivos: ela pega
o arquivo que ainda nem existe.

A varredura cobre os arquivos VERSIONADOS (`git ls-files`), conteúdo e caminho.
Fora do índice ficam `.git`, os `__pycache__`, o `.env` e os volumes do MySQL —
nenhum deles é coisa que alguém revise num PR.

A própria agulha é montada por concatenação: escrita por extenso, este arquivo
seria a primeira vítima do teste que ele define.
"""
import subprocess
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent

# Montada em duas metades de propósito — ver o docstring do módulo.
AGULHA = "i" + "hm"

SUBSTITUTA = "manut"


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


def test_nenhum_caminho_versionado_carrega_a_sigla_antiga(versionados):
    """Diretório, arquivo de teste, asset: o nome no disco conta como ocorrência."""
    culpados = [c for c in versionados if AGULHA in c.lower()]
    assert not culpados, (
        f"caminhos ainda com '{AGULHA}' (use '{SUBSTITUTA}'): {culpados}"
    )


def test_nenhum_arquivo_versionado_menciona_a_sigla_antiga(versionados):
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
            if AGULHA in linha.lower():
                culpados.append(f"{caminho}:{n}: {linha.strip()[:100]}")

    assert not culpados, (
        f"'{AGULHA}' ainda aparece em {len(culpados)} linha(s) — troque por "
        f"'{SUBSTITUTA}':\n" + "\n".join(culpados)
    )


def test_o_firmware_esp32_saiu_do_repositorio(versionados):
    """Falava MQTT com um broker que não existe no compose desde a migração REST."""
    culpados = [c for c in versionados if c.endswith(".ino") or "esp32" in c.lower()]
    assert not culpados, f"firmware Arduino ainda versionado: {culpados}"


def test_a_varredura_realmente_enxerga_o_conteudo(versionados, tmp_path):
    """Guarda da guarda: um teste que não lê nada passa sempre.

    Se `_e_binario` ou a decodificação passarem a engolir os arquivos de texto,
    os dois testes acima ficam verdes com o repositório inteiro sujo. Esta
    checagem prova que a leitura chega ao conteúdo, procurando uma palavra que
    o rename tornou obrigatória.
    """
    lidos = 0
    for caminho in versionados:
        arquivo = RAIZ_REPO / caminho
        if arquivo.is_file() and not _e_binario(arquivo.read_bytes()):
            if SUBSTITUTA in arquivo.read_text(encoding="utf-8", errors="replace").lower():
                lidos += 1
    assert lidos >= 5, f"só {lidos} arquivo(s) com '{SUBSTITUTA}' — a varredura não está lendo"
