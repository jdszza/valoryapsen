"""O que os dois conferidores da bancada compartilham.

`conferir_mesa.py` e `conferir_dispensers.py` rodam ANTES de cada estação subir,
pelo `.bat` dela, cada um com o Python do venv da sua estação. Só biblioteca
padrão aqui: os dois venvs são diferentes (opencv-contrib na mesa, opencv na dos
dispensers) e nenhum deles tem nada deste repositório instalado.

Este arquivo, como os dois conferidores, NÃO está em `vision/visao/` nem em
`vision/visao_mesa/`: o código das estações não é editado por este repositório
(`tests/test_visao_intocada.py`). O que se confere é a CONFIGURAÇÃO delas.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple

VISION = Path(__file__).resolve().parent

FALHA = "falha"
ALERTA = "alerta"

# O que o vision-adapter publica no host (porta 8102 do compose).
ADAPTER_NO_HOST = "http://127.0.0.1:8102"


class Problema(NamedTuple):
    nivel: str      # falha | alerta
    texto: str      # o que está errado
    acao: str       # o que fazer — a mesma regra do pré-voo: vermelho diz o que fazer


def ler_env(caminho: Path) -> dict | None:
    """Um `.env` simples (CHAVE=valor), lido como a estação da mesa o lê.

    None quando o arquivo não existe — e isso é um problema por si só.
    """
    if not caminho.is_file():
        return None
    env = {}
    for bruta in caminho.read_text(encoding="utf-8").splitlines():
        linha = bruta.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        env[chave.strip()] = valor.strip().strip('"').strip("'")
    return env


def cameras_do_bat(texto: str) -> dict[str, str]:
    """{"esq": ..., "dir": ...} das linhas `set CAMERA_ESQ=` / `set CAMERA_DIR=`
    do `iniciar_dispensers.bat`. Valor vazio é "use a câmera gravada por nome"."""
    cameras = {}
    for lado in ("esq", "dir"):
        achado = re.search(rf"^\s*set\s+CAMERA_{lado.upper()}=(.*)$", texto,
                           re.IGNORECASE | re.MULTILINE)
        cameras[lado] = achado.group(1).strip() if achado else ""
    return cameras


def ler_cameras_do_bat() -> dict[str, str]:
    bat = VISION / "iniciar_dispensers.bat"
    return cameras_do_bat(bat.read_text(encoding="utf-8")) if bat.is_file() else {}


def imprimir(titulo: str, problemas: list[Problema]) -> int:
    """Imprime e devolve o código de saída: 1 com qualquer falha."""
    # Console em code page antiga não derruba o conferidor por um acento.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    print(f"=== {titulo} ===")
    if not problemas:
        print("  tudo certo.")
    for p in problemas:
        print(f"  [{p.nivel.upper()}] {p.texto}")
        print(f"           -> {p.acao}")
    falhas = sum(1 for p in problemas if p.nivel == FALHA)
    alertas = len(problemas) - falhas
    print(f"--- {falhas} falha(s), {alertas} alerta(s)")
    sys.stdout.flush()
    return 1 if falhas else 0
