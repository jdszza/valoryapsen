"""Adapter falso: ocupa o lugar do vision-adapter para testar a estacao.

    python integracao_apsen/fake_adapter.py            # porta 8102
    python integracao_apsen/fake_adapter.py --porta 9102 --arquivo eventos.jsonl

Aceita POST /eventos, imprime o JSON recebido e responde
`{"ok": true, "encaminhado": true}` — a mesma resposta do adapter de verdade.
Com `--arquivo`, grava um evento por linha, que e o que vira a secao de
payloads reais do relatorio.

Nao depende do FastAPI de proposito: so a biblioteca padrao. Assim da para
rodar o teste de ponta a ponta numa maquina onde a instalacao da estacao ainda
nao foi feita.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

ARQUIVO = None
RECUSAR = False


class Manipulador(BaseHTTPRequestHandler):
    def _responder(self, codigo: int, corpo: dict) -> None:
        dados = json.dumps(corpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_POST(self) -> None:  # noqa: N802  (nome exigido por BaseHTTPRequestHandler)
        if self.path.rstrip("/") != "/eventos":
            self._responder(404, {"detail": "rota desconhecida"})
            return
        tamanho = int(self.headers.get("Content-Length", 0))
        bruto = self.rfile.read(tamanho).decode("utf-8", "replace")
        try:
            evento = json.loads(bruto)
        except json.JSONDecodeError:
            self._responder(400, {"detail": "json invalido"})
            return

        print(f"\n--- evento recebido {datetime.now(timezone.utc).isoformat()}")
        print(json.dumps(evento, ensure_ascii=False, indent=2))
        if ARQUIVO:
            with open(ARQUIVO, "a", encoding="utf-8") as saida:
                saida.write(json.dumps(evento, ensure_ascii=False) + "\n")

        if RECUSAR:
            # Para ensaiar o caminho de erro da estacao: ela tem de retentar e
            # continuar de pe.
            self._responder(500, {"detail": "adapter recusando de proposito"})
            return
        self._responder(200, {"ok": True, "encaminhado": True})

    def do_GET(self) -> None:  # noqa: N802
        self._responder(200, {"status": "ok", "service": "fake-vision-adapter"})

    def log_message(self, formato, *args) -> None:
        return None     # o print acima ja mostra o que interessa


def main() -> None:
    global ARQUIVO, RECUSAR
    p = argparse.ArgumentParser()
    p.add_argument("--porta", type=int, default=8102)
    p.add_argument("--arquivo", help="grava os eventos, um JSON por linha")
    p.add_argument("--recusar", action="store_true",
                   help="responde 500 sempre, para testar a retentativa")
    args = p.parse_args()
    ARQUIVO, RECUSAR = args.arquivo, args.recusar

    servidor = HTTPServer(("0.0.0.0", args.porta), Manipulador)
    print(f"adapter falso escutando em http://0.0.0.0:{args.porta}/eventos"
          + (f"  (gravando em {args.arquivo})" if args.arquivo else "")
          + ("  [RECUSANDO: responde 500]" if args.recusar else ""))
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrado")


if __name__ == "__main__":
    main()
