"""Servidor HTTP da estacao de visao — fala o idioma do vision-simulator.

    python -m integracao_apsen.servidor
    python integracao_apsen/servidor.py --porta 8202

Tres garantias que este arquivo existe para dar, e que sao o que costuma
quebrar numa integracao assim:

1. RESPOSTA IMEDIATA. O comando de captura responde antes de qualquer foto. O
   central desiste da resposta em 5 s e reenvia o comando ate 3 vezes; se a
   resposta esperasse a contagem, a estacao receberia tres comandos para a
   mesma coisa e o acumulado por OS seria descontado tres vezes — e a
   divergencia apareceria no SLOT SEGUINTE, longe da causa.

2. EXATAMENTE UM EVENTO por comando aceito. Nenhum, e o central espera 30 s
   a toa. Dois, e ele decide duas vezes sobre o mesmo slot. Por isso todo
   caminho de saida passa por `EnvioUnico`, inclusive o estouro de tempo e as
   excecoes.

3. UMA CAPTURA DE CADA VEZ, na ordem de chegada. Uma fila e uma thread so.
   Duas capturas simultaneas disputariam a webcam (que nao e thread-safe) e o
   acumulado por OS.
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

RAIZ = Path(__file__).resolve().parent
if str(RAIZ.parent) not in sys.path:
    sys.path.insert(0, str(RAIZ.parent))

from integracao_apsen import eventos as ev  # noqa: E402
from integracao_apsen.cliente import ClienteAdapter  # noqa: E402
from integracao_apsen.config import ConfigIntegracao  # noqa: E402
from integracao_apsen.contagem import AcumuladoPorOS, ContadorMesa  # noqa: E402

registro = logging.getLogger("integracao.servidor")

SERVICO = "apsen-vision-station"
VERSAO_MODELO = "visao_mesa-opencv-classico"


# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #
class ComandoCaptura(BaseModel):
    # extra="allow": o contrato pode ganhar campos no futuro, e recusar um
    # comando por causa de um campo que a estacao nao conhece pararia a celula
    # por nada.
    model_config = ConfigDict(extra="allow")

    slot_id: int
    os_id: str
    quantidade_esperada: int = 0
    posicao_x: float | None = None
    posicao_y: float | None = None
    injetar_falha: str | None = None


@dataclass
class Tarefa:
    slot_id: int
    os_id: str
    quantidade_esperada: int
    posicao_x: float | None = None
    posicao_y: float | None = None
    injetar_falha: str | None = None
    recebida_em: float = field(default_factory=time.monotonic)

    @property
    def chave(self) -> tuple[str, int]:
        return (self.os_id, self.slot_id)


class EnvioUnico:
    """Deixa passar o PRIMEIRO evento de uma captura e bloqueia os demais.

    O vigia de tempo e a thread de captura podem terminar quase juntos; sem
    este porteiro os dois mandariam evento para o mesmo comando, e o central
    decidiria duas vezes sobre o mesmo slot — a segunda decisao chegando depois
    de a OS ja ter seguido em frente.
    """

    def __init__(self, cliente: ClienteAdapter, rotulo: str):
        self.cliente = cliente
        self.rotulo = rotulo
        self._trava = threading.Lock()
        self.enviado = False
        self.evento: dict | None = None

    def enviar(self, evento: dict) -> bool:
        with self._trava:
            if self.enviado:
                registro.debug("evento descartado (ja houve um) em %s", self.rotulo)
                return False
            self.enviado = True
            self.evento = evento
        problemas = ev.validar_evento(evento)
        if problemas:
            # Nao se cancela o envio: um evento imperfeito ainda e melhor que
            # silencio, que custa 30 s de espera ao central. Mas o erro vai para
            # o log em ERROR, porque e bug de formato nosso.
            registro.error("evento fora do contrato (%s): %s", self.rotulo, problemas)
        return self.cliente.enviar(evento)


# --------------------------------------------------------------------------- #
# Estacao
# --------------------------------------------------------------------------- #
class Estacao:
    def __init__(self, cfg: ConfigIntegracao, contador=None, cliente=None):
        self.cfg = cfg
        self.contador = contador if contador is not None else ContadorMesa(cfg)
        self.cliente = cliente if cliente is not None else ClienteAdapter(cfg.url_eventos)
        self.acumulado = AcumuladoPorOS(cfg.max_os_memoria, cfg.validade_os_h)
        self.fila: queue.Queue[Tarefa | None] = queue.Queue()
        self._vistas: OrderedDict[tuple[str, int], str] = OrderedDict()
        self._trava_vistas = threading.Lock()
        self._parar = threading.Event()
        self.trabalhador: threading.Thread | None = None
        self.contadores = {"recebidos": 0, "repetidos": 0, "ok": 0,
                           "divergencia": 0, "falha": 0}
        self.ultimo_evento: dict | None = None

    # ------------------------------------------------------------------
    def iniciar(self) -> "Estacao":
        self.trabalhador = threading.Thread(target=self._laco, name="captura",
                                            daemon=True)
        self.trabalhador.start()
        return self

    def parar(self, timeout: float = 5.0) -> None:
        self._parar.set()
        self.fila.put(None)
        if self.trabalhador is not None:
            self.trabalhador.join(timeout=timeout)
        self.contador.fechar()

    # ------------------------------------------------------------------
    def registrar_comando(self, tarefa: Tarefa) -> bool:
        """Enfileira. Devolve False se este (os_id, slot_id) ja foi visto.

        A chave e o par, nao so o os_id: a mesma OS manda uma captura por slot,
        e todas sao legitimas. O que nao pode repetir e o mesmo slot da mesma
        OS — isso e o central reenviando porque a resposta demorou.
        """
        with self._trava_vistas:
            if tarefa.chave in self._vistas:
                self.contadores["repetidos"] += 1
                registro.warning("comando repetido para os=%s slot=%s (estado: %s) "
                                 "— nao vou capturar de novo",
                                 tarefa.os_id, tarefa.slot_id,
                                 self._vistas[tarefa.chave])
                return False
            self._vistas[tarefa.chave] = "na_fila"
            while len(self._vistas) > 400:
                self._vistas.popitem(last=False)
        self.contadores["recebidos"] += 1
        self.fila.put(tarefa)
        return True

    def _marcar(self, tarefa: Tarefa, estado: str) -> None:
        with self._trava_vistas:
            if tarefa.chave in self._vistas:
                self._vistas[tarefa.chave] = estado

    # ------------------------------------------------------------------
    def _laco(self) -> None:
        while not self._parar.is_set():
            try:
                tarefa = self.fila.get(timeout=0.2)
            except queue.Empty:
                continue
            if tarefa is None:
                break
            try:
                self._processar(tarefa)
            except Exception:
                # Nada pode matar esta thread: sem ela, nenhuma captura
                # seguinte e atendida e a celula para sem dizer por que.
                registro.exception("erro nao tratado ao processar %s", tarefa.chave)
            finally:
                self.fila.task_done()

    def _processar(self, tarefa: Tarefa) -> None:
        self._marcar(tarefa, "processando")
        rotulo = f"os={tarefa.os_id} slot={tarefa.slot_id}"
        porteiro = EnvioUnico(self.cliente, rotulo)
        prazo = tarefa.recebida_em + self.cfg.t_max_processamento_s

        # Vigia: se a captura passar do teto, o evento de falha sai por aqui.
        # Ele nao consegue desbloquear uma leitura travada no driver da camera —
        # o que ele evita e o central ficar esperando os 30 s inteiros sem
        # noticia.
        vigia = threading.Timer(
            max(0.1, prazo - time.monotonic()),
            self._estourou, args=(tarefa, porteiro),
        )
        vigia.daemon = True
        vigia.start()
        try:
            self._executar_captura(tarefa, porteiro, prazo)
        finally:
            vigia.cancel()
            self._marcar(tarefa, "concluida")

    def _estourou(self, tarefa: Tarefa, porteiro: EnvioUnico) -> None:
        if porteiro.enviado:
            return
        registro.error("captura %s passou de %.1f s — mandando falha de timeout",
                       tarefa.chave, self.cfg.t_max_processamento_s)
        self._emitir(porteiro, ev.evento_falha(
            tarefa.slot_id, tarefa.os_id, tarefa.quantidade_esperada,
            motivo="timeout_processamento",
            posicao_x=tarefa.posicao_x, posicao_y=tarefa.posicao_y,
            extras={"versao_modelo": VERSAO_MODELO},
        ))

    # ------------------------------------------------------------------
    def _executar_captura(self, tarefa: Tarefa, porteiro: EnvioUnico,
                          prazo: float) -> None:
        inicio = time.monotonic()

        if tarefa.injetar_falha:
            if not self.cfg.aceitar_injecao:
                registro.warning("injetar_falha=%r IGNORADO (ACEITAR_INJECAO=0): "
                                 "a estacao real reporta o que a camera viu",
                                 tarefa.injetar_falha)
            else:
                self._injetar(tarefa, porteiro)
                return

        # Assentamento: o comando chega assim que o dispenser solta, e as
        # caixinhas ainda estao se mexendo. Fotografar agora mede borrao.
        espera = min(self.cfg.t_assentamento_s, max(0.0, prazo - time.monotonic()))
        if espera > 0:
            time.sleep(espera)

        medicao = self.contador.medir(prazo=prazo)
        extras = {
            "versao_modelo": VERSAO_MODELO,
            "tempo_total_ms": int((time.monotonic() - tarefa.recebida_em) * 1000),
            **medicao.detalhes,
        }

        if not medicao.valida:
            registro.warning("%s -> FALHA (%s) em %.0f ms", tarefa.chave,
                             medicao.motivo, (time.monotonic() - inicio) * 1000)
            self._emitir(porteiro, ev.evento_falha(
                tarefa.slot_id, tarefa.os_id, tarefa.quantidade_esperada,
                motivo=medicao.motivo, posicao_x=tarefa.posicao_x,
                posicao_y=tarefa.posicao_y, confianca=medicao.confianca,
                extras=extras,
            ))
            return

        total_agora = int(medicao.total)
        total_anterior = self.acumulado.anterior(tarefa.os_id)
        extras["quantidade_total_caixa"] = total_agora
        extras["quantidade_total_anterior"] = total_anterior

        if total_agora < total_anterior:
            # Caixa trocada, mexida, ou uma embalagem escondida por oclusao. O
            # acumulado NAO e atualizado de proposito: se foi oclusao, a proxima
            # captura volta a enxergar tudo e a conta se fecha sozinha. Atualizar
            # aqui gravaria o erro e contaminaria todos os slots seguintes.
            registro.error("%s -> contagem regrediu (%d < %d)", tarefa.chave,
                           total_agora, total_anterior)
            self._emitir(porteiro, ev.evento_falha(
                tarefa.slot_id, tarefa.os_id, tarefa.quantidade_esperada,
                motivo="contagem_regrediu", posicao_x=tarefa.posicao_x,
                posicao_y=tarefa.posicao_y, confianca=medicao.confianca,
                quantidade_detectada=0, extras=extras,
            ))
            return

        incremento = total_agora - total_anterior
        self.acumulado.registrar(tarefa.os_id, total_agora)
        evento = ev.evento_leitura(
            tarefa.slot_id, tarefa.os_id, tarefa.quantidade_esperada,
            incremento, medicao.confianca,
            extras={"posicao_x": tarefa.posicao_x, "posicao_y": tarefa.posicao_y,
                    **extras},
        )
        registro.info("%s -> %s detectada=%d (total da caixa %d, antes %d) "
                      "confianca=%.2f em %.0f ms", tarefa.chave, evento["tipo"],
                      incremento, total_agora, total_anterior, medicao.confianca,
                      (time.monotonic() - inicio) * 1000)
        self._emitir(porteiro, evento)

    def _injetar(self, tarefa: Tarefa, porteiro: EnvioUnico) -> None:
        """Modo demonstracao: reproduz a falha que o simulador provocava.

        Nao mede nada e NAO mexe no acumulado da OS — se mexesse, a demo
        estragaria a contagem dos slots seguintes de verdade.
        """
        registro.warning("ACEITAR_INJECAO ligado: reproduzindo %r sem medir",
                         tarefa.injetar_falha)
        detectada = max(0, int(tarefa.quantidade_esperada) - 1)
        evento = ev.evento_leitura(
            tarefa.slot_id, tarefa.os_id, tarefa.quantidade_esperada,
            detectada, 0.90,
            extras={"falha_injetada": True, "versao_modelo": VERSAO_MODELO,
                    "posicao_x": tarefa.posicao_x, "posicao_y": tarefa.posicao_y},
        )
        self._emitir(porteiro, evento)

    def _emitir(self, porteiro: EnvioUnico, evento: dict) -> None:
        if porteiro.enviado:
            return
        tipo = evento.get("tipo", "")
        porteiro.enviar(evento)
        self.ultimo_evento = evento
        if tipo == ev.TIPO_OK:
            self.contadores["ok"] += 1
        elif tipo == ev.TIPO_DIVERGENCIA:
            self.contadores["divergencia"] += 1
        else:
            self.contadores["falha"] += 1


# --------------------------------------------------------------------------- #
# Aplicacao HTTP
# --------------------------------------------------------------------------- #
def criar_app(cfg: ConfigIntegracao | None = None, contador=None,
              cliente=None) -> FastAPI:
    cfg = cfg or ConfigIntegracao.carregar()
    estacao = Estacao(cfg, contador=contador, cliente=cliente).iniciar()

    app = FastAPI(title="APSEN — estacao de visao da mesa", version="1.0.0")
    app.state.estacao = estacao
    app.state.cfg = cfg

    @app.get("/ping")
    def ping() -> dict:
        # Nao toca na camera de proposito: o adapter chama isto ao subir, e a
        # camera pode levar segundos para abrir. "Estou de pe" e "a camera esta
        # pronta" sao perguntas diferentes — a segunda e o /status.
        return {"status": "ok", "service": SERVICO}

    @app.get("/status")
    def status() -> dict:
        return {
            "service": "vision-station",
            "cameras": [{
                "camera": "mesa",
                "cobertura": "mesa de coleta (balanca HX711)",
                "pronta": estacao.contador.camera_pronta,
            }],
            "num_slots": cfg.num_slots,
            "ts": ev.agora_iso(),
            "fila": estacao.fila.qsize(),
            "contadores": dict(estacao.contadores),
            "envios": {"entregues": estacao.cliente.enviados,
                       "falhados": estacao.cliente.falhados},
            "acumulado_por_os": estacao.acumulado.instantaneo(),
            "config": cfg.resumo(),
        }

    @app.post("/executar/capturar/mesa")
    def capturar_mesa(comando: ComandoCaptura) -> dict:
        if not 1 <= comando.slot_id <= cfg.num_slots:
            raise HTTPException(status_code=400,
                                detail=f"slot_id deve ser 1-{cfg.num_slots}")
        tarefa = Tarefa(
            slot_id=comando.slot_id, os_id=comando.os_id,
            quantidade_esperada=int(comando.quantidade_esperada or 0),
            posicao_x=comando.posicao_x, posicao_y=comando.posicao_y,
            injetar_falha=comando.injetar_falha,
        )
        if not estacao.registrar_comando(tarefa):
            return {"ok": True, "camera": "mesa", "slot_id": comando.slot_id,
                    "msg": "captura ja em andamento"}
        return {"ok": True, "camera": "mesa", "slot_id": comando.slot_id,
                "msg": f"Processando scan da mesa (slot {comando.slot_id})"}

    @app.post("/executar/capturar/dispenser")
    def capturar_dispenser() -> dict:
        # 501 e a resposta certa, e nao um evento inventado: esta estacao olha a
        # caixa de coleta, nao a prateleira do dispenser, e nao le codigo de
        # produto. Mandar um leitura_dispenser_ok daqui seria afirmar que o SKU
        # confere sem ninguem ter lido SKU nenhum.
        raise HTTPException(status_code=501,
                            detail="estação de visão não faz leitura de SKU")

    @app.on_event("shutdown")
    def encerrar() -> None:
        estacao.parar()

    return app


def configurar_log(cfg: ConfigIntegracao) -> None:
    formato = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    manipuladores: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if cfg.arquivo_log:
        manipuladores.append(logging.FileHandler(cfg.arquivo_log, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format=formato, handlers=manipuladores)


def main() -> None:
    p = argparse.ArgumentParser(description="Estacao de visao da mesa (APSEN).")
    p.add_argument("--porta", type=int)
    p.add_argument("--host")
    p.add_argument("--adapter")
    p.add_argument("--imagem", help="usa uma foto no lugar da webcam (ensaio)")
    args = p.parse_args()

    cfg = ConfigIntegracao.carregar()
    if args.porta:
        cfg.porta = args.porta
    if args.host:
        cfg.host = args.host
    if args.adapter:
        cfg.adapter_url = args.adapter.rstrip("/")
    if args.imagem:
        cfg.imagem_fixa = args.imagem

    configurar_log(cfg)
    registro.info("estacao de visao da mesa subindo | %s", cfg.resumo())
    if cfg.imagem_fixa:
        registro.warning("MODO ENSAIO: lendo %s no lugar da webcam", cfg.imagem_fixa)

    import uvicorn
    uvicorn.run(criar_app(cfg), host=cfg.host, port=cfg.porta, log_level="warning")


if __name__ == "__main__":
    main()
