"""Bateria de testes da camada de integracao (secao 8 do documento).

    python integracao_apsen/testes/test_integracao.py

Nao precisa de webcam nem do PC central: as cenas sao sinteticas e o adapter
e um processo falso que sobe aqui dentro. O que cada bloco cobre:

  A. HTTP e maquina de estados — resposta imediata, idempotencia, slot
     invalido, exatamente um evento, acumulado por OS, os_id novo.
  B. Ponte real com a visao — a contagem de verdade rodando sobre cenas
     geradas, inclusive camera tampada e camera caida, mais a confianca.
  C. Cliente e rede — retentativa com o adapter fora do ar, e o caminho
     completo estacao -> adapter falso, que e de onde saem os payloads reais
     do relatorio.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

RAIZ = Path(__file__).resolve().parent.parent.parent      # .../visao_mesa
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from integracao_apsen import eventos as ev  # noqa: E402
from integracao_apsen.cliente import ClienteAdapter  # noqa: E402
from integracao_apsen.config import ConfigIntegracao  # noqa: E402
from integracao_apsen.contagem import ContadorMesa, Medicao  # noqa: E402
from integracao_apsen.servidor import criar_app  # noqa: E402
from visao_mesa import ConfigMesa  # noqa: E402

PORTA_FAKE = 8599


# --------------------------------------------------------------------------- #
# Dubles
# --------------------------------------------------------------------------- #
class ClienteEmMemoria:
    """Guarda os eventos em vez de mandar pela rede."""

    def __init__(self):
        self.eventos: list[dict] = []
        self.enviados = 0
        self.falhados = 0

    def enviar(self, evento: dict) -> bool:
        self.eventos.append(evento)
        self.enviados += 1
        return True

    def por_slot(self, slot_id: int) -> list[dict]:
        return [e for e in self.eventos if e.get("slot_id") == slot_id]


class ContadorRoteirizado:
    """Contador falso: devolve o roteiro, para testar o SERVIDOR sozinho."""

    def __init__(self, totais, atraso_s: float = 0.0):
        self.totais = list(totais)
        self.atraso_s = atraso_s
        self.chamadas = 0
        self.camera_pronta = True

    def medir(self, prazo=None) -> Medicao:
        self.chamadas += 1
        if self.atraso_s:
            time.sleep(self.atraso_s)
        if not self.totais:
            return Medicao(motivo="erro_interno")
        proximo = self.totais.pop(0)
        if isinstance(proximo, str):
            return Medicao(motivo=proximo)
        return Medicao(total=int(proximo), confianca=0.95,
                       detalhes={"cobertura": 0.99, "patamar": 40})

    def fechar(self) -> None:
        return None


class FonteRoteirizada:
    """Fonte de frames falsa: entrega as cenas na ordem, ou levanta erro."""

    def __init__(self, cenas):
        self.cenas = list(cenas)
        self.pronta = True
        self.descartes = 0

    def frame(self):
        atual = self.cenas[0] if len(self.cenas) == 1 else self.cenas.pop(0)
        if isinstance(atual, Exception):
            raise atual
        return atual.copy()

    def descartar(self) -> None:
        self.descartes += 1
        self.pronta = False

    def fechar(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Cenas
# --------------------------------------------------------------------------- #
def cena_com(n: int, semente: int = 7) -> np.ndarray:
    """Bancada com n embalagens em grade, bem separadas.

    Em grade, e nao em fila como o teste do pipeline: a bancada real tem doze
    caixinhas numa caixa de papelao, e uma fila de doze nao caberia no quadro.
    O vao de 25 mm e folgado de proposito — aqui se testa a INTEGRACAO, nao o
    corte de embalagens encostadas, que ja tem teste proprio.
    """
    rng = np.random.default_rng(semente)
    s = 3.0
    largura, altura = int(440 * s), int(300 * s)
    quadro = np.full((altura, largura, 3), 150, dtype=np.uint8)
    for i in range(n):
        coluna, linha = i % 4, i // 4
        x, y = 70.0 + coluna * 100.0, 70.0 + linha * 85.0
        rect = ((x * s, y * s), (75 * s, 40 * s), float(rng.uniform(-6, 6)))
        pontos = cv2.boxPoints(rect).astype(np.int32)
        cv2.fillConvexPoly(quadro, pontos, tuple(int(v) for v in rng.integers(235, 252, 3)))
        cv2.polylines(quadro, [pontos], True, (170, 170, 170), 1, cv2.LINE_AA)
    return np.clip(quadro.astype(np.int16) + rng.normal(0, 4, quadro.shape),
                   0, 255).astype(np.uint8)


def cfg_visao() -> ConfigMesa:
    cfg = ConfigMesa()
    cfg.fundo.modo = "mesa"
    cfg.fundo.escala_px_por_mm = 3.0
    cfg.conteudo.lock = True
    return cfg


def cfg_integracao(**mudancas) -> ConfigIntegracao:
    cfg = ConfigIntegracao(
        adapter_url="http://127.0.0.1:1",      # porta morta; o cliente e falso
        t_assentamento_s=0.0,
        t_max_processamento_s=20.0,
        frames_por_captura=3,
    )
    for chave, valor in mudancas.items():
        setattr(cfg, chave, valor)
    return cfg


def comando(slot: int, os_id: str, esperada: int, **extra) -> dict:
    corpo = {"slot_id": slot, "os_id": os_id, "quantidade_esperada": esperada,
             "posicao_x": 120.0, "posicao_y": 45.0, "injetar_falha": None}
    corpo.update(extra)
    return corpo


def esperar_eventos(cliente: ClienteEmMemoria, quantos: int, limite_s: float = 25.0):
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if len(cliente.eventos) >= quantos:
            time.sleep(0.05)       # deixa um eventual evento a mais aparecer
            return True
        time.sleep(0.02)
    return False


def conferir(condicao: bool, texto: str) -> int:
    print(f"  {'OK   ' if condicao else 'FALHA'}  {texto}")
    return 0 if condicao else 1


# --------------------------------------------------------------------------- #
# A. HTTP e maquina de estados
# --------------------------------------------------------------------------- #
def bloco_http() -> int:
    print("\n=== A. HTTP e maquina de estados ===")
    falhas = 0
    cliente = ClienteEmMemoria()
    # Contagem lenta de proposito: a resposta do comando NAO pode esperar por ela.
    contador = ContadorRoteirizado([2, 6, 7], atraso_s=1.5)
    app = criar_app(cfg_integracao(), contador=contador, cliente=cliente)

    with TestClient(app) as http:
        t0 = time.monotonic()
        r = http.get("/ping")
        falhas += conferir(r.status_code == 200 and r.json()["status"] == "ok"
                           and time.monotonic() - t0 < 3.0,
                           f"/ping responde {r.json()} em {(time.monotonic()-t0)*1000:.0f} ms")

        r = http.get("/status")
        corpo = r.json()
        falhas += conferir(r.status_code == 200 and corpo["num_slots"] == 8
                           and corpo["cameras"][0]["camera"] == "mesa",
                           "/status traz num_slots e a camera da mesa")

        t0 = time.monotonic()
        r = http.post("/executar/capturar/mesa", json=comando(1, "OS-A", 2))
        demora = time.monotonic() - t0
        falhas += conferir(r.status_code == 200 and demora < 1.0,
                           f"comando responde em {demora*1000:.0f} ms "
                           f"(contagem leva 1500 ms) -> {r.json().get('msg')}")

        # Idempotencia: o central reenvia quando a resposta demora.
        repetidas = [http.post("/executar/capturar/mesa", json=comando(1, "OS-A", 2))
                     for _ in range(2)]
        falhas += conferir(all(x.status_code == 200 for x in repetidas)
                           and all("ja em andamento" in x.json()["msg"] for x in repetidas),
                           "comando repetido responde 200 'ja em andamento'")

        falhas += conferir(esperar_eventos(cliente, 1), "evento chegou")
        falhas += conferir(len(cliente.eventos) == 1 and contador.chamadas == 1,
                           f"3 comandos iguais -> 1 captura e 1 evento "
                           f"(capturas={contador.chamadas}, eventos={len(cliente.eventos)})")

        # Sequencia da secao 8: 2, depois 4, depois 1; totais 2, 6, 7.
        http.post("/executar/capturar/mesa", json=comando(3, "OS-A", 4))
        falhas += conferir(esperar_eventos(cliente, 2), "2o evento chegou")
        http.post("/executar/capturar/mesa", json=comando(6, "OS-A", 1))
        falhas += conferir(esperar_eventos(cliente, 3), "3o evento chegou")

        detectadas = [e["quantidade_detectada"] for e in cliente.eventos]
        totais = [e.get("quantidade_total_caixa") for e in cliente.eventos]
        tipos = [e["tipo"] for e in cliente.eventos]
        falhas += conferir(detectadas == [2, 4, 1],
                           f"acumulado vira incremento: detectada={detectadas} (esperado [2, 4, 1])")
        falhas += conferir(totais == [2, 6, 7],
                           f"total bruto da caixa vai junto: {totais} (esperado [2, 6, 7])")
        falhas += conferir(tipos == [ev.TIPO_OK] * 3, f"tres eventos ok: {tipos}")

        # OS nova recomeca do zero.
        contador.totais = [3]
        http.post("/executar/capturar/mesa", json=comando(1, "OS-B", 3))
        falhas += conferir(esperar_eventos(cliente, 4), "evento da OS nova chegou")
        novo = cliente.eventos[-1]
        falhas += conferir(novo["os_id"] == "OS-B" and novo["quantidade_detectada"] == 3
                           and novo["tipo"] == ev.TIPO_OK,
                           f"os_id novo zera o acumulado: detectada={novo['quantidade_detectada']}")

        # Slot fora da faixa e corpo invalido.
        r = http.post("/executar/capturar/mesa", json=comando(99, "OS-C", 1))
        falhas += conferir(r.status_code == 400 and "slot_id" in r.json()["detail"],
                           f"slot_id 99 -> HTTP {r.status_code} {r.json()['detail']!r}")
        r = http.post("/executar/capturar/mesa", json={"os_id": "OS-C"})
        falhas += conferir(r.status_code == 422, f"corpo sem slot_id -> HTTP {r.status_code}")
        r = http.post("/executar/capturar/mesa",
                      json=comando(2, "OS-C", 1, campo_do_futuro="x"))
        falhas += conferir(r.status_code == 200, "campo extra no comando nao e erro")

        r = http.post("/executar/capturar/dispenser", json={"slot_id": 6, "os_id": "OS-C"})
        falhas += conferir(r.status_code == 501,
                           f"/executar/capturar/dispenser -> HTTP {r.status_code}")

        # Todos os eventos respeitam a tabela de tipos da secao 5.4.
        problemas = [p for e in cliente.eventos for p in ev.validar_evento(e)]
        falhas += conferir(not problemas, f"tipos de todos os eventos conferem {problemas or ''}")
    return falhas


def bloco_divergencia_e_regressao() -> int:
    print("\n=== A2. divergencia, regressao e injecao ===")
    falhas = 0
    cliente = ClienteEmMemoria()
    # Terceira captura ve 5 onde havia 6: alguem tirou uma caixinha.
    contador = ContadorRoteirizado([2, 6, 5])
    app = criar_app(cfg_integracao(), contador=contador, cliente=cliente)
    with TestClient(app) as http:
        http.post("/executar/capturar/mesa", json=comando(1, "OS-D", 2))
        esperar_eventos(cliente, 1)
        http.post("/executar/capturar/mesa", json=comando(3, "OS-D", 4))
        esperar_eventos(cliente, 2)
        http.post("/executar/capturar/mesa", json=comando(6, "OS-D", 1))
        esperar_eventos(cliente, 3)

    terceiro = cliente.eventos[2]
    falhas += conferir(terceiro["tipo"] == ev.TIPO_FALHA
                       and terceiro["motivo"] == "contagem_regrediu",
                       f"total caiu de 6 para 5 -> {terceiro['tipo']} "
                       f"({terceiro.get('motivo')})")

    # Divergencia de verdade: o slot soltou menos do que devia.
    cliente2 = ClienteEmMemoria()
    app2 = criar_app(cfg_integracao(), contador=ContadorRoteirizado([2, 5]),
                     cliente=cliente2)
    with TestClient(app2) as http:
        http.post("/executar/capturar/mesa", json=comando(1, "OS-E", 2))
        esperar_eventos(cliente2, 1)
        http.post("/executar/capturar/mesa", json=comando(3, "OS-E", 4))
        esperar_eventos(cliente2, 2)
    segundo = cliente2.eventos[1]
    falhas += conferir(segundo["tipo"] == ev.TIPO_DIVERGENCIA
                       and segundo["quantidade_detectada"] == 3
                       and segundo["delta"] == -1,
                       f"caiu 3 onde esperava 4 -> {segundo['tipo']} delta={segundo['delta']}")

    # Falha da visao nao pode virar divergencia.
    cliente3 = ClienteEmMemoria()
    app3 = criar_app(cfg_integracao(), contador=ContadorRoteirizado(["baixa_confianca"]),
                     cliente=cliente3)
    with TestClient(app3) as http:
        http.post("/executar/capturar/mesa", json=comando(1, "OS-F", 2))
        esperar_eventos(cliente3, 1)
    falha = cliente3.eventos[0]
    falhas += conferir(falha["tipo"] == ev.TIPO_FALHA
                       and falha["motivo"] == "baixa_confianca",
                       f"leitura incerta -> {falha['tipo']} ({falha['motivo']}), nao divergencia")

    # injetar_falha ignorado por padrao.
    cliente4 = ClienteEmMemoria()
    app4 = criar_app(cfg_integracao(), contador=ContadorRoteirizado([4]), cliente=cliente4)
    with TestClient(app4) as http:
        http.post("/executar/capturar/mesa",
                  json=comando(1, "OS-G", 4, injetar_falha="divergencia_mesa"))
        esperar_eventos(cliente4, 1)
    injetado = cliente4.eventos[0]
    falhas += conferir(injetado["tipo"] == ev.TIPO_OK
                       and not injetado.get("falha_injetada"),
                       "injetar_falha ignorado por padrao (reporta o que mediu)")

    # ... e obedecido quando ACEITAR_INJECAO=1.
    cliente5 = ClienteEmMemoria()
    app5 = criar_app(cfg_integracao(aceitar_injecao=True),
                     contador=ContadorRoteirizado([4]), cliente=cliente5)
    with TestClient(app5) as http:
        http.post("/executar/capturar/mesa",
                  json=comando(1, "OS-H", 4, injetar_falha="divergencia_mesa"))
        esperar_eventos(cliente5, 1)
    demo = cliente5.eventos[0]
    falhas += conferir(demo["tipo"] == ev.TIPO_DIVERGENCIA
                       and demo["quantidade_detectada"] == 3
                       and demo.get("falha_injetada") is True,
                       "ACEITAR_INJECAO=1 reproduz o simulador e marca falha_injetada")
    return falhas


def bloco_timeout() -> int:
    print("\n=== A3. estouro de tempo ===")
    falhas = 0
    cliente = ClienteEmMemoria()
    # Teto de 1 s contra uma contagem de 4 s: o vigia tem de mandar a falha
    # sem esperar a contagem terminar, e a contagem que chegar depois nao pode
    # virar um segundo evento.
    contador = ContadorRoteirizado([5], atraso_s=4.0)
    app = criar_app(cfg_integracao(t_max_processamento_s=1.0),
                    contador=contador, cliente=cliente)
    with TestClient(app) as http:
        http.post("/executar/capturar/mesa", json=comando(1, "OS-T", 5))
        falhas += conferir(esperar_eventos(cliente, 1, limite_s=3.0),
                           "o evento de timeout sai antes de a contagem acabar")
        primeiro = cliente.eventos[0] if cliente.eventos else {}
        falhas += conferir(primeiro.get("motivo") == "timeout_processamento",
                           f"motivo = {primeiro.get('motivo')!r}")
        time.sleep(4.0)      # a contagem lenta termina aqui
        falhas += conferir(len(cliente.eventos) == 1,
                           f"ainda um unico evento depois que a contagem voltou "
                           f"({len(cliente.eventos)})")
    return falhas


# --------------------------------------------------------------------------- #
# B. Ponte real com a visao
# --------------------------------------------------------------------------- #
def bloco_visao_real() -> int:
    print("\n=== B. ponte com a contagem de verdade ===")
    falhas = 0
    cfg = cfg_integracao()

    # Sequencia real: 2 caixinhas, depois 6, depois 7 na bancada.
    cenas = [cena_com(2), cena_com(6), cena_com(7)]
    cliente = ClienteEmMemoria()
    contador = ContadorMesa(cfg, cfg_mesa=cfg_visao(),
                            fonte=FonteRoteirizada([cenas[0]]))
    app = criar_app(cfg, contador=contador, cliente=cliente)
    with TestClient(app) as http:
        for indice, (slot, esperada) in enumerate([(1, 2), (3, 4), (6, 1)]):
            contador.fonte = FonteRoteirizada([cenas[indice]])
            http.post("/executar/capturar/mesa", json=comando(slot, "OS-R", esperada))
            esperar_eventos(cliente, indice + 1)

    detectadas = [e["quantidade_detectada"] for e in cliente.eventos]
    totais = [e.get("quantidade_total_caixa") for e in cliente.eventos]
    confiancas = [e["confianca"] for e in cliente.eventos]
    falhas += conferir(detectadas == [2, 4, 1] and totais == [2, 6, 7],
                       f"visao real na sequencia 2/6/7: detectada={detectadas} "
                       f"total={totais}")
    falhas += conferir(all(c >= 0.6 for c in confiancas),
                       f"confianca acima do minimo nas tres: {confiancas}")
    falhas += conferir(all(e["tipo"] == ev.TIPO_OK for e in cliente.eventos),
                       "as tres leituras batem com o esperado")

    # Camera tampada: quadro preto.
    preto = np.zeros((900, 1320, 3), np.uint8)
    cliente2 = ClienteEmMemoria()
    contador2 = ContadorMesa(cfg, cfg_mesa=cfg_visao(), fonte=FonteRoteirizada([preto]))
    app2 = criar_app(cfg, contador=contador2, cliente=cliente2)
    with TestClient(app2) as http:
        http.post("/executar/capturar/mesa", json=comando(1, "OS-TAMPADA", 3))
        esperar_eventos(cliente2, 1)
    tampada = cliente2.eventos[0]
    falhas += conferir(tampada["tipo"] == ev.TIPO_FALHA
                       and tampada["motivo"] == "obstrucao_visual",
                       f"camera tampada -> {tampada['tipo']} ({tampada.get('motivo')})")

    # Camera caiu no meio da captura.
    cliente3 = ClienteEmMemoria()
    contador3 = ContadorMesa(cfg, cfg_mesa=cfg_visao(),
                             fonte=FonteRoteirizada([RuntimeError("USB caiu")]))
    app3 = criar_app(cfg, contador=contador3, cliente=cliente3)
    with TestClient(app3) as http:
        http.post("/executar/capturar/mesa", json=comando(1, "OS-CAM", 3))
        esperar_eventos(cliente3, 1)
    caiu = cliente3.eventos[0]
    falhas += conferir(caiu["tipo"] == ev.TIPO_FALHA
                       and caiu["motivo"] == "camera_indisponivel",
                       f"camera caida -> {caiu['tipo']} ({caiu.get('motivo')})")
    falhas += conferir(contador3.fonte.descartes >= 1,
                       "o handle da camera e descartado para a proxima captura reabrir")

    # O gate de mudanca precisa estar desligado, senao os frames 2..N voltariam
    # em cache e a estabilidade seria fabricada.
    falhas += conferir(contador.cfg_mesa.desempenho.gate_mudanca is False,
                       "gate de mudanca desligado na estacao (medida independente por frame)")
    # O lock e desligado DURANTE a captura para nao varrer o limiar em todo
    # frame, mas tem de voltar ligado: senao a segunda captura herdaria o
    # limiar da luz de dez minutos atras.
    falhas += conferir(contador.cfg_mesa.conteudo.lock is True,
                       "lock restaurado depois da captura")

    # Maioria apertada nao pode virar numero. 3 frames, dois dizendo 4 e um
    # dizendo 5: a nota tem de cair abaixo do minimo.
    cfg_curto = cfg_integracao(frames_por_captura=3)
    cliente4 = ClienteEmMemoria()
    contador4 = ContadorMesa(cfg_curto, cfg_mesa=cfg_visao(),
                             fonte=FonteRoteirizada([cena_com(4), cena_com(4),
                                                     cena_com(5)]))
    app4 = criar_app(cfg_curto, contador=contador4, cliente=cliente4)
    with TestClient(app4) as http:
        http.post("/executar/capturar/mesa", json=comando(1, "OS-INSTAVEL", 4))
        esperar_eventos(cliente4, 1)
    instavel = cliente4.eventos[0]
    falhas += conferir(instavel["tipo"] == ev.TIPO_FALHA
                       and instavel["motivo"] == "baixa_confianca",
                       f"2 de 3 frames concordando -> {instavel['tipo']} "
                       f"({instavel.get('motivo')}, confianca {instavel['confianca']})")
    return falhas


# --------------------------------------------------------------------------- #
# C. Cliente, rede e payloads reais
# --------------------------------------------------------------------------- #
def bloco_rede() -> int:
    print("\n=== C. cliente HTTP e adapter falso ===")
    falhas = 0

    # Adapter fora do ar: 3 tentativas e segue vivo.
    morto = ClienteAdapter("http://127.0.0.1:1/eventos", espera_s=0.05, timeout_s=0.3)
    t0 = time.monotonic()
    entregue = morto.enviar(ev.evento_falha(1, "OS-X", 2, "erro_interno"))
    falhas += conferir(entregue is False and morto.falhados == 1,
                       f"adapter fora do ar: devolve False sem levantar excecao "
                       f"({(time.monotonic()-t0)*1000:.0f} ms)")

    registro_eventos = Path(tempfile.gettempdir()) / "eventos_estacao.jsonl"
    registro_eventos.unlink(missing_ok=True)
    processo = subprocess.Popen(
        [sys.executable, str(RAIZ / "integracao_apsen" / "fake_adapter.py"),
         "--porta", str(PORTA_FAKE), "--arquivo", str(registro_eventos)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        time.sleep(1.2)
        cfg = cfg_integracao(adapter_url=f"http://127.0.0.1:{PORTA_FAKE}")
        contador = ContadorMesa(cfg, cfg_mesa=cfg_visao(),
                                fonte=FonteRoteirizada([cena_com(4)]))
        app = criar_app(cfg, contador=contador)       # cliente HTTP de verdade
        with TestClient(app) as http:
            http.post("/executar/capturar/mesa", json=comando(2, "OS-REDE-001", 4))
            fim = time.monotonic() + 20
            while time.monotonic() < fim and not registro_eventos.exists():
                time.sleep(0.1)
            time.sleep(0.5)
        linhas = [json.loads(l) for l in
                  registro_eventos.read_text(encoding="utf-8").splitlines() if l.strip()]
        falhas += conferir(len(linhas) == 1,
                           f"o adapter falso recebeu {len(linhas)} evento(s) pela rede")
        if linhas:
            evento = linhas[0]
            falhas += conferir(evento["os_id"] == "OS-REDE-001"
                               and evento["tipo"] == ev.TIPO_OK
                               and evento["quantidade_detectada"] == 4,
                               f"payload real: {evento['tipo']} "
                               f"detectada={evento['quantidade_detectada']} "
                               f"confianca={evento['confianca']}")
            print("\n  payload recebido pelo adapter falso:")
            print("  " + json.dumps(evento, ensure_ascii=False, indent=2).replace("\n", "\n  "))
    finally:
        processo.terminate()
        processo.wait(timeout=5)
    return falhas


# --------------------------------------------------------------------------- #
def main() -> int:
    falhas = (bloco_http() + bloco_divergencia_e_regressao() + bloco_timeout()
              + bloco_visao_real() + bloco_rede())
    print(f"\n{'TUDO OK' if not falhas else f'{falhas} FALHA(S)'}")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
