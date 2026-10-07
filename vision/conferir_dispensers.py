"""Confere a estação de UMA fileira de dispensers antes de ela subir.

    python vision\\conferir_dispensers.py esq|dir     sai 1 se houver falha

`vision\\iniciar_dispensers.bat` roda isto uma vez, antes do laço de reinício.
Cada regra cobre um modo de falhar que só apareceria depois, como trava falsa:

- **zonas não calibradas** — a pasta do lado nasce como cópia do modelo, com as
  zonas D1–D4 medidas em OUTRA bancada. QR de um dispenser dentro da zona "do
  vizinho" é ERRO_POSICAO, e ERRO_POSICAO trava a OS;
- **câmera escolhida por ninguém, ou a mesma de outra estação** — índice de
  webcam USB no Windows muda com a ordem de enumeração, e duas estações na
  mesma webcam julgam a mesma imagem como se fossem lados diferentes;
- **backend.url sem o lado** — sem `/estacoes/<lado>` o vision-adapter não tem
  como provar que esta estação buscou o catálogo da OS corrente.

A lógica é a função pura `conferir`; `main` só lê os arquivos reais. O
procedimento está em docs/BANCADA_VISAO.md (D1–D5).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conferencia import (ADAPTER_NO_HOST, FALHA, VISION, Problema,  # noqa: E402
                         imprimir, ler_cameras_do_bat)

SLOTS_DO_LADO = {"esq": {1, 2, 3, 4}, "dir": {5, 6, 7, 8}}
OUTRO_LADO = {"esq": "dir", "dir": "esq"}
# TEM de ser igual ao VISAO_DISP_INTERVALO_CATALOGO_S do vision-adapter.
INTERVALO_CATALOGO = 2.0


def url_do_lado(lado: str) -> str:
    """O `backend.url` desta estação: a estação monta `{url}/api/visao/catalogo`."""
    return f"{ADAPTER_NO_HOST}/estacoes/{lado}"


def _dispensers(zonas: dict | None) -> set:
    return {z.get("dispenser") for z in (zonas or {}).get("zonas", [])}


def _escolha(parametros: dict | None, camera_bat: str) -> tuple[str, str] | None:
    """("nome"|"indice", valor) da câmera que a estação vai abrir, ou None.

    O `.bat` passa `--camera` quando CAMERA_<lado> está preenchido, e o número
    ali vai DIRETO para o índice (pulando o nome gravado). Vazio, vale o que o
    `python src\\camera.py` gravou no parametros.json — por nome.
    """
    if camera_bat.strip():
        valor = camera_bat.strip()
        return ("indice", valor) if valor.isdigit() else ("nome", valor.lower())
    camera = (parametros or {}).get("camera") or {}
    if camera.get("nome"):
        return ("nome", str(camera["nome"]).lower())
    return None


def _mesma_camera(a: tuple[str, str] | None, b: tuple[str, str] | None) -> bool:
    if a is None or b is None or a[0] != b[0]:
        return False
    if a[0] == "nome":
        return a[1] in b[1] or b[1] in a[1]      # --camera aceita PARTE do nome
    return a[1] == b[1]


def conferir(lado: str, zonas: dict | None, zonas_modelo: dict | None,
             parametros: dict | None, cameras_bat: dict[str, str],
             parametros_outro: dict | None = None,
             mesa_json: dict | None = None) -> list[Problema]:
    """Os problemas da estação do `lado`. Pura: recebe tudo já lido."""
    problemas: list[Problema] = []
    pasta = f"vision\\visao_{lado}"
    d2 = "docs/BANCADA_VISAO.md, D2"

    if zonas is None:
        problemas.append(Problema(
            FALHA, f"{pasta}\\config\\zonas.json não existe",
            f"Na pasta {pasta}: python src\\calibrar.py ({d2})."))
    else:
        if zonas_modelo is not None and zonas == zonas_modelo:
            problemas.append(Problema(
                FALHA, f"{pasta}\\config\\zonas.json é idêntico ao do modelo "
                       f"(vision\\visao\\config) — as zonas não foram medidas nesta "
                       f"bancada, e QR na zona do vizinho trava a OS",
                f"Na pasta {pasta}: python src\\calibrar.py ({d2})."))
        achados = _dispensers(zonas)
        if achados != SLOTS_DO_LADO[lado]:
            esperado = "1 a 4" if lado == "esq" else "5 a 8"
            problemas.append(Problema(
                FALHA, f"zonas numeradas {sorted(achados, key=str)} — a estação "
                       f"{lado} cobre os dispensers {esperado}",
                f"Recalibre com o número REAL do slot ({esperado}) — {d2}."))

    escolha = _escolha(parametros, cameras_bat.get(lado, ""))
    if escolha is None:
        problemas.append(Problema(
            FALHA, "ninguém escolheu a câmera desta estação (parametros.json sem "
                   f"camera.nome e CAMERA_{lado.upper()} vazio no .bat)",
            f"Na pasta {pasta}: python src\\camera.py ({d2})."))
    else:
        outra = _escolha(parametros_outro, cameras_bat.get(OUTRO_LADO[lado], ""))
        if _mesma_camera(escolha, outra):
            problemas.append(Problema(
                FALHA, f"a câmera {escolha[1]!r} é a mesma da estação "
                       f"{OUTRO_LADO[lado]} — as duas julgariam a mesma imagem",
                f"Escolha câmeras diferentes com python src\\camera.py em cada "
                f"pasta ({d2}); webcams do mesmo modelo, cada uma na mesma porta USB."))
        indice_mesa = ((mesa_json or {}).get("camera") or {}).get("indice")
        if escolha[0] == "indice" and indice_mesa is not None \
                and int(escolha[1]) == int(indice_mesa):
            problemas.append(Problema(
                FALHA, f"o índice {escolha[1]} é o mesmo camera.indice da estação da "
                       f"mesa — duas estações na mesma webcam",
                f"Deixe CAMERA_{lado.upper()} vazio e escolha por nome com "
                f"python src\\camera.py ({d2})."))

    backend = (parametros or {}).get("backend") or {}
    url = str(backend.get("url", "")).rstrip("/")
    try:
        intervalo = float(backend.get("intervalo_catalogo", 0))
    except (TypeError, ValueError):
        intervalo = 0.0
    if backend.get("ativo") is not True or url != url_do_lado(lado) \
            or intervalo != INTERVALO_CATALOGO:
        problemas.append(Problema(
            FALHA, f"backend no parametros.json: ativo={backend.get('ativo')!r} "
                   f"url={url or '(vazia)'!r} intervalo_catalogo="
                   f"{backend.get('intervalo_catalogo')!r}",
            f"Em {pasta}\\config\\parametros.json: backend.ativo=true, "
            f"backend.url=\"{url_do_lado(lado)}\", backend.intervalo_catalogo="
            f"{INTERVALO_CATALOGO:g} (docs/BANCADA_VISAO.md, D3)."))
    return problemas


def _ler_json(caminho: Path) -> dict | None:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SLOTS_DO_LADO:
        print("Uso: python vision\\conferir_dispensers.py esq|dir")
        return 2
    lado = argv[1]
    pasta = VISION / f"visao_{lado}" / "config"
    outra = VISION / f"visao_{OUTRO_LADO[lado]}" / "config"
    problemas = conferir(
        lado,
        zonas=_ler_json(pasta / "zonas.json"),
        zonas_modelo=_ler_json(VISION / "visao" / "config" / "zonas.json"),
        parametros=_ler_json(pasta / "parametros.json"),
        cameras_bat=ler_cameras_do_bat(),
        parametros_outro=_ler_json(outra / "parametros.json"),
        mesa_json=_ler_json(VISION / "visao_mesa" / "config" / "mesa.json"),
    )
    return imprimir(f"Estação dos dispensers ({lado})", problemas)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
