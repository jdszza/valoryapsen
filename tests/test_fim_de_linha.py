"""Fim de linha é decidido pelo `.gitattributes`, não pela máquina de quem commita.

Sem ele, quem decide é o `core.autocrlf` de cada máquina. Um commit feito de
onde a conversão está desligada grava CRLF no índice e reescreve arquivos
inteiros — o `git blame` passa a apontar tudo para um commit só, e o diff de
mil linhas aparece com dez mil. O `Makefile` com CRLF quebra receita de `make`.

Do outro lado, `.bat` PRECISA chegar ao disco em CRLF: o `cmd.exe` lê um `.bat`
LF errado quando há rótulo e `goto`, e os laços de reinício das estações de
visão usam os dois. No índice ele fica LF, como todo texto — quem escreve o
CRLF é o checkout, pelo atributo `eol=crlf`. Por isso o teste cobra o
ATRIBUTO do `.bat`, e não os bytes do índice.

A leitura é do ÍNDICE (`git ls-files --eol`), nunca do disco: no Windows o
disco pode estar em CRLF legitimamente. E é varredura de `git ls-files`, sem
lista de arquivos — o arquivo que ainda não existe também é coberto.
"""
import subprocess
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent

BINARIOS = (".png", ".jpg", ".jpeg", ".pdf", ".bin")
SCRIPTS_WINDOWS = (".bat", ".cmd")


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=RAIZ_REPO, capture_output=True, check=True,
            text=True, encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"git indisponível: {exc}")


@pytest.fixture(scope="module")
def eol_do_indice() -> dict[str, str]:
    """{caminho: estado no índice} — `lf`, `crlf`, `mixed`, `-text` ou `none`."""
    estados = {}
    for linha in _git("ls-files", "--eol", "-z").split("\0"):
        if not linha:
            continue
        info, _, caminho = linha.partition("\t")
        estados[caminho] = info.split()[0].removeprefix("i/")
    assert estados, "git ls-files não devolveu nada — repositório lido errado?"
    return estados


def test_nenhum_texto_tem_crlf_no_indice(eol_do_indice):
    culpados = sorted(c for c, e in eol_do_indice.items() if e in ("crlf", "mixed"))
    assert not culpados, (
        f"arquivos com CRLF no índice: {culpados} — rode `git add --renormalize .`"
    )


def test_bat_sai_do_checkout_em_crlf(eol_do_indice):
    scripts = [c for c in eol_do_indice if c.lower().endswith(SCRIPTS_WINDOWS)]
    assert scripts, "nenhum .bat versionado — a varredura não achou o que cobrar"
    saida = _git("check-attr", "eol", "--", *scripts)
    sem_crlf = [linha for linha in saida.splitlines() if not linha.endswith(": eol: crlf")]
    assert not sem_crlf, f".bat sem `eol=crlf` no .gitattributes: {sem_crlf}"


def test_binario_nao_e_tratado_como_texto(eol_do_indice):
    """Conversão de fim de linha num PNG corrompe o arquivo sem erro nenhum."""
    binarios = [c for c in eol_do_indice if c.lower().endswith(BINARIOS)]
    assert binarios, "nenhum binário versionado — a varredura não achou o que cobrar"
    saida = _git("check-attr", "text", "--", *binarios)
    como_texto = [linha for linha in saida.splitlines() if not linha.endswith(": text: unset")]
    assert not como_texto, f"binários sem `binary` no .gitattributes: {como_texto}"
