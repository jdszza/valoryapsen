"""Ensaio da ponte vision-adapter ↔ estação real dos dispensers, sem câmera.

Roda à mão, fora da suíte (precisa de OpenCV e leva minutos):

    python vision/ensaio_dispensers.py
    python vision/ensaio_dispensers.py --python-estacao vision\\visao\\.venv\\Scripts\\python.exe

O que ele prova é o único ponto da ponte que só tinha sido LIDO no código: que
a estação de verdade, rodando, troca o veredito quando o catálogo que o adapter
serve muda de uma OS para a outra.

  1. copia vision/visao para uma pasta temporária e gera lá a cena sintética
     (vídeo com MIOSAN, FLANCOX, MECLIN e LONIUM nos dispensers 1..4) — só a
     CÓPIA tem o parametros.json ajustado; o código da visão não é tocado;
  2. sobe um central falso (grava os eventos) e o vision-adapter de verdade,
     com VISAO_DISPENSER_FONTE=estacao;
  3. sobe a estação de verdade lendo o vídeo;
  4. OS-A pede os quatro medicamentos onde eles estão → espera 4 ok;
  5. OS-B pede FLANCOX no D1 e MIOSAN no D2 (trocados) → espera divergência
     nos dois. Se não vier, a premissa da ponte é falsa.

O adapter roda com o Python que roda este script (precisa de fastapi/uvicorn);
a estação, com `--python-estacao` (precisa de opencv/qrcode/numpy) — por
padrão, o mesmo.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
VISAO = RAIZ / "vision" / "visao"
ADAPTER = RAIZ / "vision-adapter"

OS_A = {1: "MIOSAN 5MG", 2: "FLANCOX 500MG", 3: "MECLIN 25MG", 4: "LONIUM 40MG"}
OS_B = {1: "FLANCOX 500MG", 2: "MIOSAN 5MG"}


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed_do_central() -> dict[str, str]:
    arvore = ast.parse((RAIZ / "central-computer" / "database.py").read_text(encoding="utf-8"))
    for no in arvore.body:
        if isinstance(no, ast.Assign) and any(
                getattr(a, "id", None) == "_MEDICAMENTOS_SEED" for a in no.targets):
            return {nome: sku for nome, sku, *_ in ast.literal_eval(no.value)}
    raise SystemExit("_MEDICAMENTOS_SEED não encontrado")


# ── Central falso ─────────────────────────────────────────────────────────────

class CentralFalso:
    def __init__(self, seed: dict[str, str]):
        self.eventos: list[dict] = []
        self.trava = threading.Lock()
        medicamentos = [{"nome": n, "sku": s} for n, s in seed.items()]
        dono = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, corpo, codigo=200):
                dados = json.dumps(corpo).encode()
                self.send_response(codigo)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(dados)))
                self.end_headers()
                self.wfile.write(dados)

            def do_GET(self):
                if self.path.startswith("/medicamentos"):
                    return self._json(medicamentos)
                return self._json({"status": "ok"})   # /ping

            def do_POST(self):
                corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/v1/eventos/visao":
                    with dono.trava:
                        dono.eventos.append(corpo)
                self._json({"ok": True})

            def log_message(self, *_):
                pass

        self.porta = porta_livre()
        self.servidor = ThreadingHTTPServer(("127.0.0.1", self.porta), Handler)
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()

    def da_os(self, os_id: str) -> dict[int, dict]:
        with self.trava:
            return {e["slot_id"]: e for e in self.eventos
                    if e.get("os_id") == os_id and e.get("tipo", "").startswith("leitura_dispenser")}


# ── Utilitários ───────────────────────────────────────────────────────────────

def http_json(url: str, corpo: dict | None = None, timeout: float = 5.0):
    dados = None if corpo is None else json.dumps(corpo).encode()
    req = urllib.request.Request(url, data=dados, headers={"Content-Type": "application/json"},
                                 method="GET" if corpo is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def esperar(cond, prazo: float, passo: float = 0.5) -> bool:
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(passo)
    return False


def disparar_os(adapter_url: str, os_id: str, slots: dict[int, str], seed: dict) -> None:
    """Os comandos de todos os slots em paralelo — como o `gather` do passo 3b."""
    def um(item):
        slot, nome = item
        return http_json(f"{adapter_url}/comandos/capturar/dispenser", {
            "slot_id": slot, "os_id": os_id, "medicamento_esperado": nome,
            "sku_esperado": seed.get(nome, ""), "quantidade_esperada": 3})
    with ThreadPoolExecutor(len(slots)) as ex:
        for resposta in ex.map(um, slots.items()):
            print(f"    comando → {resposta}")


def mostrar(eventos: dict[int, dict]) -> None:
    for slot in sorted(eventos):
        e = eventos[slot]
        print(f"    D{slot}: {e['tipo']:<32} veredito_estacao={e.get('veredito_estacao')} "
              f"medicamento_lido={e.get('medicamento_lido')} sku_lido={e.get('sku_lido')} "
              f"motivo={e.get('motivo')}")


# ── O ensaio ──────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--python-estacao", default=sys.executable)
    p.add_argument("--frames", type=int, default=2000)
    p.add_argument("--manter", action="store_true", help="não apaga a pasta temporária")
    p.add_argument("--video", help="reaproveita um cena.mp4 já gerado (poupa minutos)")
    args = p.parse_args()
    # O console do Windows (cp1252) não imprime "→" nem "—", e um print que
    # levanta no meio do ensaio derruba a estação junto, sem resultado.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    seed = seed_do_central()
    tmp = Path(tempfile.mkdtemp(prefix="ensaio_dispensers_"))
    copia = tmp / "visao"
    processos: list[subprocess.Popen] = []
    logs: list = []
    print(f"[1] copiando vision/visao para {copia}")
    shutil.copytree(VISAO, copia, ignore=shutil.ignore_patterns(
        "dados", "logs", ".venv", "__pycache__"))

    try:
        if args.video:
            # As zonas da cena são fixas (4 dispensers em linha): gravá-las a
            # partir de uma foto da mesma cena basta.
            video = Path(args.video).resolve()
            print(f"[1] reaproveitando {video}; gravando só as zonas da cena")
            subprocess.run([args.python_estacao, "tests/cena_sintetica.py", "--salvar-zonas",
                            "--saida", str(tmp / "cena.png")], cwd=copia, check=True)
        else:
            video = tmp / "cena.mp4"
            print(f"[1] gerando a cena sintética ({args.frames} frames) — leva minutos")
            t0 = time.monotonic()
            subprocess.run([args.python_estacao, "tests/cena_sintetica.py", "--video", str(video),
                            "--frames", str(args.frames), "--salvar-zonas"],
                           cwd=copia, check=True)
            print(f"    pronto em {time.monotonic() - t0:.0f} s")

        central = CentralFalso(seed)
        central_url = f"http://127.0.0.1:{central.porta}"
        porta_adapter, porta_estacao = porta_livre(), porta_livre()
        adapter_url = f"http://127.0.0.1:{porta_adapter}"
        estacao_url = f"http://127.0.0.1:{porta_estacao}"

        parametros = copia / "config" / "parametros.json"
        cfg = json.loads(parametros.read_text(encoding="utf-8"))
        cfg["backend"].update({"ativo": True, "url": adapter_url, "intervalo_catalogo": 2})
        parametros.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[1] parametros.json da cópia: backend.url={adapter_url} intervalo_catalogo=2")

        print(f"[2] central falso em {central_url}; vision-adapter em {adapter_url}")
        env = {**os.environ, "CENTRAL_URL": central_url, "VISION_SIM_URL": central_url,
               "VISAO_DISPENSER_FONTE": "estacao", "VISAO_DISP_ESQ_URL": estacao_url,
               "VISAO_DISP_DIR_URL": estacao_url, "NUM_SLOTS": "8",
               "VISAO_DISP_INTERVALO_CATALOGO_S": "2", "VISAO_DISP_ASSENTAMENTO_S": "2"}
        log_adapter = open(tmp / "adapter.log", "w", encoding="utf-8")
        logs.append(log_adapter)
        processos.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--port", str(porta_adapter)],
            cwd=ADAPTER, env=env, stdout=log_adapter, stderr=subprocess.STDOUT))
        if not esperar(lambda: http_json(adapter_url + "/ping"), 30):
            raise SystemExit("vision-adapter não subiu — ver adapter.log")

        print(f"[3] estação real em {estacao_url}, lendo o vídeo")
        log_estacao = open(tmp / "estacao.log", "w", encoding="utf-8")
        logs.append(log_estacao)
        estacao = subprocess.Popen(
            [args.python_estacao, "-u", "src/estacao.py", "--video", str(video),
             "--sem-janela", "--porta", str(porta_estacao), "--estacao", "esq"],
            cwd=copia, stdout=log_estacao, stderr=subprocess.STDOUT)
        processos.append(estacao)
        if not esperar(lambda: http_json(estacao_url + "/api/saude"), 60):
            raise SystemExit("estação não subiu — ver estacao.log")

        ok = True
        print("[4] OS-A: os quatro onde estão — espero 4 leitura_dispenser_ok")
        disparar_os(adapter_url, "OS-A", OS_A, seed)
        esperar(lambda: len(central.da_os("OS-A")) == 4, 40)
        eventos = central.da_os("OS-A")
        mostrar(eventos)
        passo4 = (len(eventos) == 4 and
                  all(e["tipo"] == "leitura_dispenser_ok" for e in eventos.values()))
        print(f"    PASSO 4: {'OK' if passo4 else 'FALHOU'}")
        ok &= passo4

        print("[5] OS-B: FLANCOX no D1 e MIOSAN no D2 (trocados) — espero divergência nos dois")
        disparar_os(adapter_url, "OS-B", OS_B, seed)
        esperar(lambda: len(central.da_os("OS-B")) == 2, 40)
        eventos = central.da_os("OS-B")
        mostrar(eventos)
        passo5 = (len(eventos) == 2 and
                  all(e["tipo"] == "leitura_dispenser_divergencia" for e in eventos.values()))
        print(f"    PASSO 5: {'OK' if passo5 else 'FALHOU — a premissa da ponte é falsa'}")
        ok &= passo5

        if estacao.poll() is not None:
            print("    (a estação terminou o vídeo durante o ensaio)")
        print("\n--- catálogo que a estação aplicou (estacao.log) ---")
        for linha in (tmp / "estacao.log").read_text(encoding="utf-8", errors="replace").splitlines():
            if "catalogo" in linha.lower() or "D1 =" in linha or "backend Apsen" in linha:
                print("   ", linha)
        print(f"\nRESULTADO: {'TUDO OK' if ok else 'FALHOU'}")
        return 0 if ok else 1
    finally:
        for proc in processos:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for log in logs:
            log.close()
        if args.manter:
            print(f"(pasta mantida: {tmp})")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
