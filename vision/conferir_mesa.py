"""Confere a estação da mesa ANTES de ela subir — `vision\\iniciar_mesa.bat` roda isto.

Nada do que se confere aqui dá erro na hora certa. `fundo.modo="manual"` com a
caixa andando conta no lugar errado com `confiavel=True` (medido: 8 caixinhas
contadas como 8, 6, 6 e 4 conforme a caixa andava); `PORTA` no default (8202)
colide com o vision-simulator; `ADAPTER_URL` no default é um IP de exemplo; o
venv sem fastapi faz o servidor morrer no import. Tudo isso aparece depois como
contagem errada, ou como janela que fecha — e este script existe para aparecer
ANTES, com o que fazer.

    python vision\\conferir_mesa.py        sai 1 se houver falha

A lógica é a função pura `conferir`; `main` só lê os arquivos reais. O
procedimento de bancada está em docs/BANCADA_VISAO.md (M1–M5).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conferencia import (ADAPTER_NO_HOST, ALERTA, FALHA, VISION, Problema,  # noqa: E402
                         imprimir, ler_cameras_do_bat, ler_env)

MESA = VISION / "visao_mesa"
ARQ_MESA_JSON = MESA / "config" / "mesa.json"
ARQ_ENV = MESA / "integracao_apsen" / ".env"
PYTHON_VENV = MESA / ".venv" / "Scripts" / "python.exe"

# 8212, e não a 8202 do default da estação: a 8202 é do vision-simulator, que
# continua no ar como rollback.
PORTA_ESPERADA = "8212"
PACOTES = ("fastapi", "uvicorn", "requests", "cv2", "cv2.aruco")

_SONDA_PACOTES = (
    "import importlib.util, json\n"
    "r = [n for n in ('fastapi', 'uvicorn', 'requests', 'cv2') "
    "if importlib.util.find_spec(n)]\n"
    "try:\n"
    "    import cv2\n"
    "    if hasattr(cv2, 'aruco'): r.append('cv2.aruco')\n"
    "except Exception: pass\n"
    "print(json.dumps(r))\n"
)


def _ligado(valor) -> bool:
    """A mesma leitura de `integracao_apsen.config._ligado`."""
    return str(valor or "0").strip().lower() in ("1", "true", "sim", "yes")


def _numero(valor, padrao: float) -> float:
    try:
        return float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return padrao


def conferir(mesa_json: dict, env: dict | None, pacotes: set[str] | None,
             cameras_dispensers: dict[str, str] | None = None,
             venv_erro: str | None = None) -> list[Problema]:
    """Os problemas da estação da mesa. Pura: recebe tudo já lido.

    `env` None = o `.env` da estação não existe. `pacotes` None = o venv da
    mesa não existe — ou existe e não executa, e aí `venv_erro` diz por quê (o
    caso de um venv copiado de outra máquina, que aponta para um Python que não
    existe aqui). `cameras_dispensers` são os CAMERA_ESQ/CAMERA_DIR do
    `iniciar_dispensers.bat` (vazio = escolha por nome, não dá para comparar).
    """
    problemas: list[Problema] = []
    fundo = mesa_json.get("fundo") or {}
    camera = mesa_json.get("camera") or {}
    m12 = "docs/BANCADA_VISAO.md, M1/M2"

    if fundo.get("modo") != "caixa":
        problemas.append(Problema(
            FALHA, f"fundo.modo = {fundo.get('modo')!r} — com a câmera fixa e a caixa "
                   f"andando, só \"caixa\" acha o fundo a cada foto; o resto conta no "
                   f"lugar errado sem avisar",
            f"Recalibre com o calibrar.py em modo \"caixa\" ({m12})."))
    if _numero(fundo.get("suavizacao", 0), 1) != 0:
        problemas.append(Problema(
            FALHA, f"fundo.suavizacao = {fundo.get('suavizacao')} — a foto de um slot "
                   f"reaproveita os cantos do slot anterior",
            f"Ponha fundo.suavizacao = 0 no mesa.json ({m12})."))
    if _numero(fundo.get("frames_validade", 0), 1) != 0:
        problemas.append(Problema(
            FALHA, f"fundo.frames_validade = {fundo.get('frames_validade')} — a foto "
                   f"de um slot reaproveita a homografia do slot anterior",
            f"Ponha fundo.frames_validade = 0 no mesa.json ({m12})."))
    if camera.get("autofoco") is True:
        problemas.append(Problema(
            ALERTA, "camera.autofoco = true — o foco caça a cada parada da mesa",
            "Fixe um foco médio que sirva às 8 paradas (NOTAS_CALIBRAGEM.md) e "
            "confira a nitidez nos eventos."))

    if env is None:
        problemas.append(Problema(
            FALHA, "vision\\visao_mesa\\integracao_apsen\\.env não existe — valeriam "
                   "PORTA=8202 (a do vision-simulator) e um ADAPTER_URL de exemplo",
            "Copie o .env.example para .env com os valores de "
            "docs/DEPLOY_WINDOWS.md (\".env da estação da mesa\")."))
    else:
        porta = env.get("PORTA", "8202").strip()
        if porta != PORTA_ESPERADA:
            problemas.append(Problema(
                FALHA, f"PORTA={porta} — a estação tem de escutar na {PORTA_ESPERADA}",
                f"Ponha PORTA={PORTA_ESPERADA} no .env da estação (a 8202 é do "
                f"vision-simulator)."))
        adapter = env.get("ADAPTER_URL", "http://192.168.0.10:8102").strip().rstrip("/")
        if adapter != ADAPTER_NO_HOST:
            problemas.append(Problema(
                FALHA, f"ADAPTER_URL={adapter} — os eventos não chegariam ao "
                       f"vision-adapter",
                f"Ponha ADAPTER_URL={ADAPTER_NO_HOST} no .env da estação."))
        host = env.get("HOST", "0.0.0.0").strip()
        if host != "0.0.0.0":
            problemas.append(Problema(
                FALHA, f"HOST={host} — o vision-adapter (em container) não alcança a "
                       f"estação",
                "Ponha HOST=0.0.0.0 no .env da estação."))
        if _ligado(env.get("ACEITAR_INJECAO", "0")):
            problemas.append(Problema(
                FALHA, "ACEITAR_INJECAO ligado — a estação devolveria um número sem "
                       "medir nem registrar o total, e a trava cairia no slot seguinte",
                "Ponha ACEITAR_INJECAO=0: com a câmera real, quem injeta é o "
                "vision-adapter."))
        validade = _numero(env.get("VALIDADE_OS_H", "2"), 2.0)
        if validade < 12:
            problemas.append(Problema(
                ALERTA, f"VALIDADE_OS_H={validade:g} — uma trava esperando supervisor "
                        f"além disso apaga o acumulado da OS",
                "Ponha VALIDADE_OS_H=24 no .env da estação."))
        assentamento = _numero(env.get("T_ASSENTAMENTO_S", "0.5"), 0.5)
        if assentamento < 1.0:
            problemas.append(Problema(
                ALERTA, f"T_ASSENTAMENTO_S={assentamento:g} — a mesa pode estar "
                        f"balançando na foto",
                "Comece com T_ASSENTAMENTO_S=1.0 (docs/BANCADA_VISAO.md, M3)."))

    if pacotes is None and venv_erro:
        problemas.append(Problema(
            FALHA, f"o venv da estação da mesa existe mas NÃO executa ({venv_erro}) "
                   f"— provavelmente criado em outra máquina",
            "Apague vision\\visao_mesa\\.venv e recrie: python -m venv .venv && "
            ".venv\\Scripts\\activate && pip install -r requirements.txt -r "
            "integracao_apsen\\requirements.txt"))
    elif pacotes is None:
        problemas.append(Problema(
            FALHA, "o venv da estação da mesa (vision\\visao_mesa\\.venv) não existe",
            "cd vision\\visao_mesa && python -m venv .venv && .venv\\Scripts\\activate "
            "&& pip install -r requirements.txt -r integracao_apsen\\requirements.txt"))
    else:
        faltam = [p for p in PACOTES if p not in pacotes]
        if faltam:
            problemas.append(Problema(
                FALHA, f"faltam no venv da mesa: {', '.join(faltam)} — o servidor "
                       f"morre no import",
                "No venv da mesa: pip install -r requirements.txt -r "
                "integracao_apsen\\requirements.txt"))

    indice = camera.get("indice")
    for lado, valor in (cameras_dispensers or {}).items():
        if str(valor).strip().isdigit() and indice is not None and int(valor) == int(indice):
            problemas.append(Problema(
                FALHA, f"camera.indice = {indice} da mesa é o mesmo CAMERA_{lado.upper()} "
                       f"do iniciar_dispensers.bat — duas estações na mesma webcam",
                "Escolha as câmeras por nome (deixe CAMERA_ESQ/CAMERA_DIR vazios) ou "
                "corrija o índice (docs/BANCADA_VISAO.md, D2)."))
    return problemas


def pacotes_do_venv(python: Path = PYTHON_VENV) -> tuple[set[str] | None, str | None]:
    """(pacotes, erro) do Python DO VENV DA MESA — não o do sistema.

    (None, None): o venv não existe. (None, erro): existe e não executa.
    """
    if not python.is_file():
        return None, None
    try:
        r = subprocess.run([str(python), "-c", _SONDA_PACOTES], capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip().splitlines()[-1:][0] if (
            r.stderr or r.stdout).strip() else f"código {r.returncode}"
    try:
        return set(json.loads(r.stdout.strip().splitlines()[-1])), None
    except (ValueError, IndexError):
        return None, f"saída inesperada: {r.stdout[:120]!r}"


def main() -> int:
    try:
        mesa_json = json.loads(ARQ_MESA_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return imprimir("Estação da mesa", [Problema(
            FALHA, f"{ARQ_MESA_JSON} ilegível ({exc})",
            "Rode o calibrar.py da estação da mesa.")])
    pacotes, venv_erro = pacotes_do_venv()
    problemas = conferir(mesa_json, ler_env(ARQ_ENV), pacotes, ler_cameras_do_bat(),
                         venv_erro)
    return imprimir("Estação da mesa", problemas)


if __name__ == "__main__":
    sys.exit(main())
