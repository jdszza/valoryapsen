"""Ensaio da câmera da mesa de ponta a ponta, sem câmera e sem CNC.

Roda à mão, fora da suíte (precisa de OpenCV com os módulos contrib, como a
estação da mesa):

    python vision/ensaio_mesa.py

O que ele prova é a regra da câmera que nem sempre vê a caixa inteira, com as
três peças de verdade no caminho — nenhuma decisão é tomada por este script:

  - o ORQUESTRADOR do central decide o que fotografar e o que esperar
    (VISAO_MESA_POSICOES), e o Triple Check decide se trava;
  - o VISION-ADAPTER real (subprocesso uvicorn) leva o comando e traz o evento;
  - a ESTAÇÃO DA MESA real (`vision/visao_mesa/integracao_apsen`) conta com a
    visão de verdade, sobre as cenas sintéticas do próprio teste dela
    (`cena_com`/`cfg_visao`, importadas, não copiadas).

Só o dispenser, a mesa CNC e a balança são duplos — os da suíte
(`tests/conftest.py`): eles não são o que está em ensaio.

A OS: D1=2, D2=1, D3=2, D4=1, com D2 FORA do mapa de posições visíveis. A caixa
física vai a 2, 3, 5 e 6, mas a câmera vê 2, (sem foto), 5 e 5 — a última
caixinha cai escondida. Esperado:
  D1 ok; D2 sem comando; D3 ok com esperado 3 e slots_cobertos [2,3];
  D4 divergência a menos com a balança ok → alarme, NÃO trava.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
MESA = RAIZ / "vision" / "visao_mesa"
CENTRAL = RAIZ / "central-computer"
TESTES = RAIZ / "tests"
ADAPTER = RAIZ / "vision-adapter"

# A OS do ensaio: (medicamento, quantidade) — o orquestrador atribui D1..D4.
ITENS = [("Dipirona", 2), ("Paracetamol", 1), ("Ibuprofeno", 2), ("Amoxicilina", 1)]
POSICOES_VISIVEIS = "1,3,4"          # D2 fora: a caixa não aparece inteira ali
ESCONDIDOS = {4}                      # a unidade de D4 cai atrás da parede da caixa
OS_ID = "OS-ENSAIO-MESA"


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def esperar_ping(url: str, prazo: float = 60.0) -> bool:
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            time.sleep(0.3)
    return False


# ── A caixa de coleta: o que existe e o que a câmera vê ───────────────────────

class Caixa:
    def __init__(self):
        self.total = 0
        self.visivel = 0

    def cair(self, slot: int, quantidade: int) -> None:
        self.total += quantidade
        if slot not in ESCONDIDOS:
            self.visivel += quantidade


class FonteDaCaixa:
    """Fonte de frames da estação: a cena com o que está VISÍVEL na caixa agora."""

    def __init__(self, caixa: Caixa, cena_com):
        self.caixa = caixa
        self.cena_com = cena_com
        self.pronta = True

    def frame(self):
        return self.cena_com(self.caixa.visivel)

    def descartar(self) -> None:
        return None

    def fechar(self) -> None:
        return None


# ── Central falso: só o que o main.py faz com o evento da mesa ────────────────

def subir_central_falso(orq, eventos: list) -> int:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._ok({"status": "ok"})

        def do_POST(self):
            corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/v1/eventos/visao":
                eventos.append(corpo)
                if str(corpo.get("tipo", "")).startswith("leitura_mesa_"):
                    # O mesmo aviso de `main._handle_evento_visao`.
                    orq.notificar_evento(f"{corpo['os_id']}:visao_mesa:{corpo['slot_id']}", corpo)
            self._ok({"ok": True})

        def _ok(self, corpo):
            dados = json.dumps(corpo).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(dados)))
            self.end_headers()
            self.wfile.write(dados)

        def log_message(self, *_):
            pass

    porta = porta_livre()
    servidor = ThreadingHTTPServer(("127.0.0.1", porta), Handler)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    return porta


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for caminho in (MESA, MESA / "src", TESTES, CENTRAL):
        sys.path.insert(0, str(caminho))

    # As cenas e a configuração de visão do PRÓPRIO teste da estação.
    from integracao_apsen.testes.test_integracao import cena_com, cfg_integracao, cfg_visao
    from integracao_apsen.contagem import ContadorMesa
    from integracao_apsen.servidor import criar_app
    import conftest
    import httpx
    import pytest
    import uvicorn

    porta_estacao, porta_adapter = porta_livre(), porta_livre()
    estacao_url = f"http://127.0.0.1:{porta_estacao}"
    adapter_url = f"http://127.0.0.1:{porta_adapter}"

    # ── O orquestrador de verdade ─────────────────────────────────────────────
    os.environ.update({"VISAO_MESA_POSICOES": POSICOES_VISIVEIS, "VISAO_MESA_FINAL": "",
                       "VISION_ADAPTER_URL": adapter_url, "APSEN_ENV": "dev"})
    spec = importlib.util.spec_from_file_location("ensaio_orchestrator", CENTRAL / "orchestrator.py")
    orq = importlib.util.module_from_spec(spec)
    sys.modules["ensaio_orchestrator"] = orq
    spec.loader.exec_module(orq)
    mp = pytest.MonkeyPatch()
    for campo in ("CNC_TETO_TRAJETO_S", "CNC_MARGEM_CHEGADA_S",
                  "DISPENSA_S_POR_UNIDADE", "DISPENSA_FOLGA_S"):
        mp.setattr(orq.settings, campo, 0.0)       # o relógio da mesa não está em ensaio
    banco = conftest.BancoFake()
    banco.instalar(orq, mp)

    eventos: list = []
    porta_central = subir_central_falso(orq, eventos)
    central_url = f"http://127.0.0.1:{porta_central}"

    # ── A estação da mesa de verdade, contando cenas sintéticas ───────────────
    caixa = Caixa()
    cfg = cfg_integracao(adapter_url=adapter_url, porta=porta_estacao)
    contador = ContadorMesa(cfg, cfg_mesa=cfg_visao(), fonte=FonteDaCaixa(caixa, cena_com))
    servidor_estacao = uvicorn.Server(uvicorn.Config(
        criar_app(cfg, contador=contador), host="127.0.0.1", port=porta_estacao,
        log_level="warning"))
    threading.Thread(target=servidor_estacao.run, daemon=True).start()
    if not esperar_ping(estacao_url + "/ping"):
        raise SystemExit("estação da mesa não subiu")

    # ── O vision-adapter de verdade ───────────────────────────────────────────
    env = {**os.environ, "CENTRAL_URL": central_url, "VISION_SIM_URL": estacao_url,
           "VISAO_MESA_FONTE": "estacao", "VISAO_DISPENSER_FONTE": "simulador",
           "PYTHONIOENCODING": "utf-8"}
    log_adapter = open(Path(os.environ.get("TEMP", ".")) / "ensaio_mesa_adapter.log", "w",
                       encoding="utf-8")
    adapter = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(porta_adapter)],
        cwd=ADAPTER, env=env, stdout=log_adapter, stderr=subprocess.STDOUT)

    try:
        if not esperar_ping(adapter_url + "/ping"):
            raise SystemExit("vision-adapter não subiu")
        print(f"estação da mesa {estacao_url} | vision-adapter {adapter_url} | "
              f"central falso {central_url}")
        print(f"VISAO_MESA_POSICOES={POSICOES_VISIVEIS} (D2 fora do mapa)\n")

        duplo = conftest.AdapterFake(orq)
        post_real = orq._post
        fotos: list[dict] = []
        travas: list[str] = []
        quantidade_do_slot: dict[int, int] = {}

        async def post(url, payload, timeout=10.0):
            if url.endswith("/comandos/capturar/mesa"):
                fotos.append(payload)                  # o que o CENTRAL mandou
                return await post_real(url, payload, timeout)
            if url.endswith("/comandos/carregar"):
                quantidade_do_slot[payload["dispenser_id"]] = payload["quantidade"]
            if url.endswith("/comandos/dispensar"):
                slot = payload["dispenser_id"]
                caixa.cair(slot, quantidade_do_slot[slot])
            return await duplo.post(url, payload, timeout)

        def broadcast():
            if orq._estado["trava"]["ativa"]:
                travas.append(orq._estado["trava"]["motivo"])
                orq.liberar_trava("ensaio")          # registra e não deixa pendurar

        async def rodar():
            orq.inicializar(conftest._estado_zerado(), threading.Lock(), broadcast,
                            asyncio.get_running_loop())
            orq._post = post
            orq._client = httpx.AsyncClient()
            try:
                await orq._processar_os({
                    "os_id": OS_ID, "descricao": "ensaio da mesa",
                    "template_id": conftest.TEMPLATE_PADRAO,
                    "medicamentos": [{"medicamento": m, "sku": f"{m[:3].upper()}-1",
                                      "categoria": "geral", "quantidade": q}
                                     for m, q in ITENS],
                })
            finally:
                await orq._client.aclose()

        asyncio.run(rodar())

        # ── O que aconteceu ───────────────────────────────────────────────────
        print("Fotos que o central pediu:")
        for f in fotos:
            print(f"  D{f['slot_id']}: quantidade_esperada={f['quantidade_esperada']} "
                  f"slots_cobertos={f['slots_cobertos']} quantidade_slot={f['quantidade_slot']}")
        print("Eventos que a estação devolveu (pelo vision-adapter):")
        da_mesa = {e["slot_id"]: e for e in eventos if e["tipo"].startswith("leitura_mesa_")}
        for slot, e in sorted(da_mesa.items()):
            print(f"  D{slot}: {e['tipo']:<26} detectada={e['quantidade_detectada']} "
                  f"de {e['quantidade_esperada']} (total na caixa "
                  f"{e.get('quantidade_total_caixa')}, confiança {e['confianca']})")
        alarmes = [c["args"][1] for c in banco.chamadas_de("salvar_alarme")]
        status = [c["args"][1] for c in banco.chamadas_de("atualizar_status_ordem")]
        print(f"Alarmes: {alarmes}")
        print(f"Travas: {travas or 'nenhuma'}")
        print(f"Status gravados: {status}")
        print(f"Caixa: {caixa.total} unidades, {caixa.visivel} visíveis\n")

        def conferir(cond, texto):
            print(f"  {'OK   ' if cond else 'FALHA'}  {texto}")
            return cond

        por_slot = {f["slot_id"]: f for f in fotos}
        resultado = all([
            conferir([f["slot_id"] for f in fotos] == [1, 3, 4], "fotos só de D1, D3 e D4 (D2 sem comando)"),
            conferir(da_mesa.get(1, {}).get("tipo") == "leitura_mesa_ok", "D1 ok"),
            conferir(por_slot.get(3, {}).get("quantidade_esperada") == 3
                     and por_slot.get(3, {}).get("slots_cobertos") == [2, 3],
                     "D3 pedido com esperado 3 e slots_cobertos [2,3]"),
            conferir(da_mesa.get(3, {}).get("tipo") == "leitura_mesa_ok", "D3 ok"),
            conferir(da_mesa.get(4, {}).get("tipo") == "leitura_mesa_divergencia"
                     and da_mesa[4]["quantidade_detectada"] < da_mesa[4]["quantidade_esperada"],
                     "D4 divergência a menos"),
            conferir("contagem_camera_abaixo" in alarmes, "alarme contagem_camera_abaixo"),
            conferir(not travas, "nenhuma trava"),
            conferir(status[-1:] == ["concluida"], "OS concluída"),
        ])
        print(f"\nRESULTADO: {'TUDO OK' if resultado else 'FALHOU'}")
        return 0 if resultado else 1
    finally:
        adapter.terminate()
        try:
            adapter.wait(timeout=10)
        except subprocess.TimeoutExpired:
            adapter.kill()
        log_adapter.close()
        servidor_estacao.should_exit = True
        contador.fechar()


if __name__ == "__main__":
    raise SystemExit(main())
