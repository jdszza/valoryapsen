# -*- coding: utf-8 -*-
"""Todo ponteiro para documentação tem que resolver.

Havia um `ANALISE_ARQUITETURAL.md` neste repositório. Ele sumiu, e **quatro
arquivos continuaram apontando para ele** — `central-computer/main.py` (duas
vezes), `erp-simulator/simulator.py` e `tests/test_api_ordens.py` —, todos
mandando o leitor procurar "§4.1" para conhecer o contrato de recusa de uma OS.
Um quinto, `tests/test_manut_dispensers.py`, mandava ler "AUDITORIA.md 2, 3 e 4".

Nada quebrou, e é esse o problema. Ponteiro morto não levanta exceção nem
reprova teste: ele custa o tempo de quem foi procurar, e cobra esse tempo de
novo de cada pessoa que passar por ali. Pior que isso, ele ensina a duvidar da
documentação inteira — depois do segundo link que não leva a lugar nenhum,
ninguém mais confere o que o comentário promete, e aí um comentário que MENTE
sobre o contrato passa despercebido.

É a mesma família de `test_protocolo_serial.py` e `test_protocolo_placas.py`
(documento confrontado com o código) e de `test_rename_manut.py` (varredura de
`git ls-files` em vez de lista manual, para pegar o arquivo que ainda nem
existe). A diferença é que aqui não se compara conteúdo: só se cobra que o alvo
exista.

Três formas de apontar, três checagens:

  1. `.md` citado em código Python  → o arquivo existe;
  2. link markdown `[texto](x.md)`  → o arquivo existe;
  3. `README, "Nome da seção"`      → o README tem esse título.

A terceira é a que faltava quando o documento morreu: as referências foram
reescritas para apontar para SEÇÕES do README, e seção é justamente o que alguém
renomeia sem procurar quem cita.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

RAIZ_REPO = Path(__file__).resolve().parent.parent
README = RAIZ_REPO / "README.md"


def _versionados(*sufixos: str) -> list[Path]:
    """Só o que está no índice do git.

    Varrer o disco traria o `.venv` do painel de bancada junto — centenas de
    arquivos de terceiros, com referências a documentação que não é deste
    repositório. `git ls-files` é o mesmo recorte que `test_rename_manut.py`
    usa, e pelo mesmo motivo.
    """
    saida = subprocess.run(["git", "ls-files"], cwd=RAIZ_REPO,
                           capture_output=True, text=True, check=False)
    if saida.returncode != 0:
        pytest.skip("git indisponível")
    return [RAIZ_REPO / linha for linha in saida.stdout.splitlines()
            if linha.endswith(sufixos)]


def _ler(caminho: Path) -> str:
    return caminho.read_text(encoding="utf-8", errors="replace")


def _existe(referencia: str, origem: Path) -> bool:
    """O alvo resolve a partir da raiz do repo OU do diretório de quem cita."""
    return ((RAIZ_REPO / referencia).is_file()
            or (origem.parent / referencia).is_file())


# ══════════════════════════════════════════════════════════════════════════════
# 1. `.md` citado em código Python
# ══════════════════════════════════════════════════════════════════════════════

# Nome de arquivo markdown num comentário ou docstring. `docs/PROTOCOLO_SERIAL.md`
# e `CLAUDE.md` são os dois que o código realmente cita hoje.
_ARQUIVO_MD = re.compile(r"(?<![\w/.-])((?:[\w-]+/)*[\w.-]+\.md)\b")


def test_todo_md_citado_no_codigo_existe():
    """`(ver ANALISE_ARQUITETURAL.md)` num docstring foi exatamente isto."""
    mortos = []
    for arquivo in _versionados(".py"):
        for referencia in set(_ARQUIVO_MD.findall(_ler(arquivo))):
            if not _existe(referencia, arquivo):
                mortos.append(f"{arquivo.relative_to(RAIZ_REPO)} → {referencia}")
    assert not mortos, (
        f"código apontando para documento que não existe: {sorted(mortos)}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. Link markdown entre documentos
# ══════════════════════════════════════════════════════════════════════════════

_LINK_MD = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")


def test_todo_link_entre_documentos_resolve():
    """Link é promessa de navegação — e o leitor só descobre que ela é falsa
    depois de clicar."""
    mortos = []
    for arquivo in _versionados(".md"):
        for referencia in set(_LINK_MD.findall(_ler(arquivo))):
            if referencia.startswith(("http://", "https://")):
                continue
            if not _existe(referencia, arquivo):
                mortos.append(f"{arquivo.relative_to(RAIZ_REPO)} → {referencia}")
    assert not mortos, f"links markdown quebrados: {sorted(mortos)}"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Seção do README citada por nome
# ══════════════════════════════════════════════════════════════════════════════

_CITA_SECAO = re.compile(r'README,\s*(?:seção\s*)?"([^"]+)"')


def _normalizar(texto: str) -> str:
    """Uma citação pode estar quebrada em duas linhas de docstring.

    Sem isto, `README, "Contrato de entrada de\\n    uma OS"` seria comparado com
    a indentação dentro — e reprovaria uma seção que existe.
    """
    return re.sub(r"\s+", " ", texto).strip()


def _titulos(caminho: Path) -> set[str]:
    """Títulos do documento, ignorando o que está dentro de bloco de código.

    O `.env` de exemplo do README tem comentários shell começando com `#`
    (`# ── Banco ──`), e contá-los como título encheria o conjunto de nomes que
    ninguém pode citar — e faria uma citação errada casar por acidente.
    """
    titulos, dentro_de_codigo = set(), False
    for linha in _ler(caminho).splitlines():
        if linha.lstrip().startswith("```"):
            dentro_de_codigo = not dentro_de_codigo
            continue
        if not dentro_de_codigo and re.match(r"#{1,6}\s", linha):
            titulos.add(_normalizar(linha.lstrip("#")))
    return titulos


def test_toda_secao_do_readme_citada_por_nome_existe():
    """As referências órfãs foram reescritas para apontar para SEÇÕES do README.

    Seção é o que alguém renomeia sem procurar quem cita — foi assim que o
    ponteiro anterior morreu, e sem esta checagem o conserto teria a mesma
    validade que o problema.
    """
    titulos = _titulos(README)
    mortas = []
    for arquivo in _versionados(".py", ".md"):
        if arquivo == README:
            continue
        for secao in set(_CITA_SECAO.findall(_ler(arquivo))):
            if _normalizar(secao) not in titulos:
                mortas.append(f"{arquivo.relative_to(RAIZ_REPO)} → \"{secao}\"")
    assert not mortas, (
        f"seção citada que o README não tem: {sorted(mortas)}. "
        f"Títulos disponíveis: {sorted(titulos)}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4. Âncoras dentro do próprio documento
# ══════════════════════════════════════════════════════════════════════════════

_ANCORA = re.compile(r"\]\(#([^)]+)\)")


def _slug(titulo: str) -> str:
    """A regra do GitHub: minúsculas, pontuação fora, espaço vira hífen.

    Os acentos FICAM (`#visão-geral` é âncora válida), e é por isso que a
    remoção de pontuação usa `\\w` com Unicode em vez de `[a-z0-9]`.
    """
    limpo = re.sub(r"[`*]", "", titulo.strip().lower())
    limpo = re.sub(r"[^\w\s-]", "", limpo, flags=re.UNICODE)
    return re.sub(r"\s+", "-", limpo.strip())


@pytest.mark.parametrize("nome", ["README.md", "docs/PROTOCOLO_SERIAL.md",
                                  "docs/BANCADA.md"])
def test_o_indice_de_cada_documento_leva_a_algum_lugar(nome):
    """Índice que não navega é pior que documento sem índice: ele promete a
    estrutura e entrega uma rolagem."""
    caminho = RAIZ_REPO / nome
    if not caminho.is_file():
        pytest.skip(f"{nome} não está no disco")
    validas = {_slug(titulo) for titulo in _titulos(caminho)}
    quebradas = sorted({a for a in _ANCORA.findall(_ler(caminho))
                        if a not in validas})
    assert not quebradas, f"{nome}: âncoras que não existem: {quebradas}"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Guarda da guarda
# ══════════════════════════════════════════════════════════════════════════════
#
# Varredura por regex que para de casar deixa os quatro testes acima verdes para
# sempre — o mesmo risco que `test_protocolo_serial.py` carrega no último bloco.

def test_a_varredura_enxerga_as_referencias_que_existem():
    achados = sum(len(set(_ARQUIVO_MD.findall(_ler(a))))
                  for a in _versionados(".py"))
    assert achados >= 10, (
        f"a varredura achou só {achados} referências a `.md` no código — o "
        f"padrão provavelmente parou de casar e os testes viraram verdes "
        f"permanentes"
    )


def test_a_varredura_reprovaria_um_ponteiro_morto():
    """Mutação: o conserto de hoje tem que reprovar o erro de ontem."""
    assert not _existe("ANALISE_ARQUITETURAL.md", README)
    assert _ARQUIVO_MD.findall("(ver ANALISE_ARQUITETURAL.md):") == \
        ["ANALISE_ARQUITETURAL.md"]
    assert _LINK_MD.findall("[x](ANALISE_ARQUITETURAL.md)") == \
        ["ANALISE_ARQUITETURAL.md"]
    assert _CITA_SECAO.findall('README, "Seção Que Não Existe"') == \
        ["Seção Que Não Existe"]


def test_os_documentos_que_o_codigo_cita_existem_mesmo():
    """Piso nominal: se `docs/` sumir num merge, isto acusa antes do leitor."""
    for esperado in ("README.md", "docs/PROTOCOLO_SERIAL.md", "TASKS.md"):
        assert (RAIZ_REPO / esperado).is_file(), esperado
