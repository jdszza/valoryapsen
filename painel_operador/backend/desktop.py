"""Lancador desktop do backend Apsen: sobe o Flask numa thread e abre uma
janela nativa (pywebview / WebView2) apontando para ele.

Este arquivo e o ponto de entrada do apsen.exe (ver build_exe.ps1).
Para desenvolvimento continue usando `python app.py` — o fluxo nao mudou.
"""
import socket
import threading
import time

import webview

from app import app, iniciar_workers

PORTA_PADRAO = 5000


def _porta_livre(porta):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", porta)) != 0


def main():
    porta = PORTA_PADRAO
    if not _porta_livre(porta):
        # Ja tem algo na 5000 (ex: instancia de dev aberta) — usa uma efemera.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            porta = s.getsockname()[1]

    # Sem reloader aqui, entao a env var WERKZEUG_RUN_MAIN que o app.py checa
    # nunca existe — as threads de fundo sobem por este processo, uma unica vez.
    iniciar_workers()

    threading.Thread(
        target=lambda: app.run(
            host="127.0.0.1", port=porta, debug=False, use_reloader=False
        ),
        daemon=True,
    ).start()

    deadline = time.time() + 15
    while time.time() < deadline and _porta_livre(porta):
        time.sleep(0.2)

    webview.create_window(
        "Apsen", f"http://127.0.0.1:{porta}", width=1280, height=800
    )
    # Bloqueia ate a janela fechar; as threads (Flask, serial) sao daemon e
    # morrem junto com o processo.
    webview.start()


if __name__ == "__main__":
    main()
