"""
APSEN - Simulador de Dispensers v2.0
=====================================
Simula o firmware dos 6 dispensers (cada um com visão computacional e IA).

Fluxo por OS:
  1. OS recebida (apsen/os/nova)
  2. IA local define qual remédio vai em qual dispenser → publica apsen/ia/atribuicao
  3. FASE DE CARREGAMENTO:
     a. Cada dispenser envolvido inicia carregamento do remédio
     b. Visão computacional valida cada unidade carregada
     c. Se CV detecta erro → publica erro e PARA TUDO (toda a OS para)
     d. Quando carregamento concluído → publica "pronto" (dispenser_id + os_id)
  4. (CNC calcula melhor rota e aguarda todos "pronto")
  5. FASE DE DISPENSA:
     a. CNC publica "posicionado" no dispenser
     b. Dispenser inicia dispensa remédio a remédio
     c. Cada remédio validado pela IA (95% ok, 5% falha)
     d. Falha: publica alarme; operação continua tentando a próxima
     e. Concluído: publica status "concluido"

Tópicos publicados:
  apsen/ia/atribuicao           → {os_id, atribuicoes: [...], timestamp}
  apsen/dispenser/carregamento  → {os_id, dispenser_id, medicamento, status, motivo_falha}
  apsen/dispenser/pronto        → {os_id, dispenser_id, medicamento, quantidade_alvo}
  apsen/dispenser/evento        → por cada remédio dispensado
  apsen/dispenser/status        → status do dispenser (pronto/dispensando/concluido/erro)
  apsen/manut/temperatura       → telemetria dos dispensers

Tópicos assinados:
  apsen/os/nova       — recebe OS
  apsen/cnc/status    — detecta "posicionado" para iniciar dispensa
"""

import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DISP-SIM] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

MQTT_HOST       = os.getenv("MQTT_HOST",    "mosquitto")
MQTT_PORT       = int(os.getenv("MQTT_PORT", "1883"))
T_CARGA_UNID    = float(os.getenv("T_CARGA_UNID",   "0.3"))   # s por unidade carregada
T_DISPENSA_UNID = float(os.getenv("T_DISPENSA_UNID", "0.5"))  # s por unidade dispensada
PROB_FALHA_IA   = float(os.getenv("PROB_FALHA_IA",   "0.05")) # 5% de falha de validação
PROB_ERRO_CV    = float(os.getenv("PROB_ERRO_CV",    "0.03")) # 3% de erro de carregamento
RECONNECT_DELAY = 5

# Mapa fixo: dispenser_id → medicamento contido
MEDICAMENTOS_FIXOS = {
    1: "Metformina 500mg",
    2: "Atorvastatina 20mg",
    3: "Omeprazol 20mg",
    4: "Losartana 50mg",
    5: "Amlodipina 5mg",
    6: "Levotiroxina 25mcg",
}

# Temperaturas base de cada dispenser (°C)
TEMP_BASE = {d: 22 + d * 0.5 for d in range(1, 7)}


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class DispenserSimulator:
    def __init__(self):
        # Estado por dispenser
        self._estado: dict[int, dict] = {
            d: {
                "status":       "idle",
                "medicamento":  MEDICAMENTOS_FIXOS[d],
                "os_id":        None,
                "qtd_alvo":     0,
                "qtd_dispensada": 0,
            }
            for d in range(1, 7)
        }
        self._lock = threading.Lock()

        # OS em andamento
        self._os_ativa: dict | None = None
        # Dispensers atribuídos na OS atual {dispenser_id: {medicamento, quantidade}}
        self._atribuicoes: dict[int, dict] = {}
        # Flag: algum erro de CV → tudo para
        self._erro_ativo = False
        # Evento: dispenser N foi posicionado pela CNC
        self._posicionado_event: dict[int, threading.Event] = {
            d: threading.Event() for d in range(1, 7)
        }

        self.client = mqtt.Client(
            client_id="apsen-dispenser-simulator-v2", clean_session=True
        )
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

    # ─────────────────────────────────────────────── MQTT ──────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe("apsen/os/nova",    qos=1)
            client.subscribe("apsen/cnc/status", qos=0)
            logger.info("Dispenser Simulator conectado. Aguardando OS...")

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning(f"Desconectado (rc={rc}). Reconectando em {RECONNECT_DELAY}s...")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            topic   = msg.topic

            if topic == "apsen/os/nova":
                self._handle_os_nova(payload)

            elif topic == "apsen/cnc/status":
                self._handle_cnc_status(payload)

        except Exception as exc:
            logger.error(f"[DISP] Erro em mensagem: {exc}", exc_info=True)

    # ─────────────────────────────────────────────── Handlers ──────────────────

    def _handle_os_nova(self, payload: dict):
        with self._lock:
            if self._os_ativa:
                logger.warning("[DISP] OS já em andamento — ignorando nova OS.")
                return
            self._os_ativa    = payload
            self._erro_ativo  = False
            self._atribuicoes = {}
            for e in self._posicionado_event.values():
                e.clear()

        logger.info(f"[DISP] OS recebida: {payload.get('os_id')}")
        # Inicia fluxo em thread separada
        threading.Thread(
            target=self._processar_os, args=(payload,), daemon=True
        ).start()

    def _handle_cnc_status(self, payload: dict):
        status   = payload.get("status")
        disp_alv = payload.get("dispenser_alvo")

        if status == "posicionado" and disp_alv is not None:
            disp_id = int(disp_alv)
            if disp_id in self._posicionado_event:
                logger.info(f"[DISP-{disp_id}] CNC posicionada — iniciando dispensa!")
                self._posicionado_event[disp_id].set()

    # ─────────────────────────────────────────── Processamento da OS ───────────

    def _processar_os(self, os_payload: dict):
        os_id        = os_payload["os_id"]
        medicamentos = os_payload.get("medicamentos", [])

        # ── ETAPA 1: IA define atribuições ─────────────────────────────────────
        atribuicoes = []
        for item in medicamentos:
            disp_id = int(item["dispenser_id"])
            med_esperado = MEDICAMENTOS_FIXOS.get(disp_id, "Desconhecido")
            atribuicoes.append({
                "dispenser_id":  disp_id,
                "medicamento":   med_esperado,
                "quantidade":    item["quantidade"],
            })
            with self._lock:
                self._atribuicoes[disp_id] = {
                    "medicamento": med_esperado,
                    "quantidade":  item["quantidade"],
                }

        self.client.publish(
            "apsen/ia/atribuicao",
            json.dumps({
                "os_id":       os_id,
                "atribuicoes": atribuicoes,
                "timestamp":   _ts(),
            }),
            qos=1,
        )
        logger.info(
            f"[DISP-IA] Atribuição publicada para OS {os_id}: "
            f"{[(a['dispenser_id'], a['medicamento']) for a in atribuicoes]}"
        )

        # ── ETAPA 2: Carregamento paralelo de todos os dispensers ──────────────
        threads_carga = []
        for item in atribuicoes:
            t = threading.Thread(
                target=self._fase_carregamento,
                args=(os_id, item["dispenser_id"], item["medicamento"], item["quantidade"]),
                daemon=True,
            )
            threads_carga.append(t)
            t.start()

        for t in threads_carga:
            t.join()

        # Se houve erro durante carregamento, aborta
        with self._lock:
            if self._erro_ativo:
                logger.error(f"[DISP] OS {os_id} ABORTADA por erro de carregamento.")
                self._os_ativa = None
                return

        logger.info(f"[DISP] TODOS OS DISPENSERS PRONTOS para OS {os_id}!")

        # ── ETAPA 3: Dispensa (aguarda CNC posicionar em cada dispenser) ───────
        threads_disp = []
        for item in atribuicoes:
            t = threading.Thread(
                target=self._fase_dispensa,
                args=(os_id, item["dispenser_id"], item["medicamento"], item["quantidade"]),
                daemon=True,
            )
            threads_disp.append(t)
            t.start()

        for t in threads_disp:
            t.join()

        with self._lock:
            self._os_ativa = None
            self._atribuicoes = {}

        logger.info(f"[DISP] OS {os_id} finalizada.")

    # ─────────────────────────────────────── Fase 1: Carregamento ──────────────

    def _fase_carregamento(self, os_id: str, disp_id: int, medicamento: str, quantidade: int):
        """Simula visão computacional acompanhando o carregamento do dispenser."""
        logger.info(f"[DISP-{disp_id}] Iniciando carregamento: {medicamento} × {quantidade}")

        with self._lock:
            self._estado[disp_id].update({
                "status":   "carregando",
                "os_id":    os_id,
                "qtd_alvo": quantidade,
                "qtd_dispensada": 0,
            })

        # Publica início do carregamento
        self.client.publish(
            "apsen/dispenser/carregamento",
            json.dumps({
                "os_id":        os_id,
                "dispenser_id": disp_id,
                "medicamento":  medicamento,
                "status":       "iniciado",
                "timestamp":    _ts(),
            }),
            qos=0,
        )

        # Simula carregamento unidade a unidade com CV validando
        for i in range(1, quantidade + 1):
            with self._lock:
                if self._erro_ativo:
                    return  # outro dispenser com erro — para

            time.sleep(T_CARGA_UNID)

            # Verifica se erro de CV
            if random.random() < PROB_ERRO_CV:
                motivo = random.choice([
                    f"Medicamento errado detectado na unidade {i} (CV: código de barras inválido)",
                    f"Unidade {i} danificada detectada pela visão computacional",
                    f"Contaminação detectada na unidade {i}",
                ])
                logger.error(f"[DISP-{disp_id}] ERRO CV no carregamento: {motivo}")

                self.client.publish(
                    "apsen/dispenser/carregamento",
                    json.dumps({
                        "os_id":         os_id,
                        "dispenser_id":  disp_id,
                        "medicamento":   medicamento,
                        "status":        "erro",
                        "unidade":       i,
                        "motivo_falha":  motivo,
                        "timestamp":     _ts(),
                    }),
                    qos=1,
                )

                with self._lock:
                    self._erro_ativo = True
                    self._estado[disp_id]["status"] = "erro"

                # Publica status de erro
                self.client.publish(
                    "apsen/dispenser/status",
                    json.dumps({
                        "os_id":        os_id,
                        "dispenser_id": disp_id,
                        "status":       "erro",
                        "motivo":       motivo,
                        "timestamp":    _ts(),
                    }),
                    qos=1,
                )
                return

        # Carregamento concluído com sucesso
        logger.info(f"[DISP-{disp_id}] Carregamento OK — {quantidade} unidades de {medicamento}")

        self.client.publish(
            "apsen/dispenser/carregamento",
            json.dumps({
                "os_id":        os_id,
                "dispenser_id": disp_id,
                "medicamento":  medicamento,
                "status":       "ok",
                "quantidade":   quantidade,
                "timestamp":    _ts(),
            }),
            qos=0,
        )

        # Publica "pronto" — CNC pode incluir na rota
        with self._lock:
            self._estado[disp_id]["status"] = "pronto"

        self.client.publish(
            "apsen/dispenser/pronto",
            json.dumps({
                "os_id":          os_id,
                "dispenser_id":   disp_id,
                "medicamento":    medicamento,
                "quantidade_alvo": quantidade,
                "timestamp":      _ts(),
            }),
            qos=1,
        )
        self.client.publish(
            "apsen/dispenser/status",
            json.dumps({
                "os_id":        os_id,
                "dispenser_id": disp_id,
                "medicamento":  medicamento,
                "status":       "pronto",
                "timestamp":    _ts(),
            }),
            qos=1,
        )
        logger.info(f"[DISP-{disp_id}] PRONTO para dispensar.")

    # ─────────────────────────────────────── Fase 2: Dispensa ──────────────────

    def _fase_dispensa(self, os_id: str, disp_id: int, medicamento: str, quantidade: int):
        """Aguarda CNC posicionar e dispensa remédio a remédio."""
        logger.info(f"[DISP-{disp_id}] Aguardando CNC posicionar...")

        # Aguarda CNC chegar (sem timeout fixo — CNC tem seu próprio timeout)
        self._posicionado_event[disp_id].wait()

        with self._lock:
            if self._erro_ativo:
                return

        logger.info(f"[DISP-{disp_id}] CNC chegou. Iniciando dispensa de {quantidade} unidades de {medicamento}.")

        with self._lock:
            self._estado[disp_id]["status"] = "dispensando"

        self.client.publish(
            "apsen/dispenser/status",
            json.dumps({
                "os_id":        os_id,
                "dispenser_id": disp_id,
                "status":       "dispensando",
                "timestamp":    _ts(),
            }),
            qos=0,
        )

        dispensado = 0
        for i in range(1, quantidade + 1):
            with self._lock:
                if self._erro_ativo:
                    return

            time.sleep(T_DISPENSA_UNID)

            # IA valida remédio dispensado (95% ok)
            validado = random.random() >= PROB_FALHA_IA
            motivo_falha: str | None = None

            if not validado:
                motivo_falha = random.choice([
                    "Comprimido partido detectado pela IA",
                    "Peso fora do padrão — possível comprimido incompleto",
                    "Formato irregular detectado — lote possivelmente defeituoso",
                ])
                logger.warning(f"[DISP-{disp_id}] IA rejeitou unidade {i}: {motivo_falha}")
            else:
                dispensado += 1

            with self._lock:
                self._estado[disp_id]["qtd_dispensada"] = dispensado

            # Publica evento de cada remédio
            self.client.publish(
                "apsen/dispenser/evento",
                json.dumps({
                    "os_id":                os_id,
                    "dispenser_id":         disp_id,
                    "medicamento":          medicamento,
                    "seq":                  i,
                    "quantidade_dispensada": dispensado,
                    "quantidade_alvo":      quantidade,
                    "validado":             validado,
                    "motivo_falha":         motivo_falha,
                    "timestamp":            _ts(),
                }),
                qos=0,
            )

        # Dispensa concluída
        logger.info(
            f"[DISP-{disp_id}] DISPENSA CONCLUÍDA: "
            f"{dispensado}/{quantidade} unidades válidas de {medicamento}"
        )

        with self._lock:
            self._estado[disp_id]["status"] = "concluido"

        self.client.publish(
            "apsen/dispenser/status",
            json.dumps({
                "os_id":                os_id,
                "dispenser_id":         disp_id,
                "medicamento":          medicamento,
                "status":               "concluido",
                "quantidade_dispensada": dispensado,
                "quantidade_alvo":       quantidade,
                "timestamp":            _ts(),
            }),
            qos=1,
        )

    # ─────────────────────────────────── Telemetria de manutenção ──────────────

    def _telemetria_loop(self):
        """Publica temperaturas dos dispensers a cada 20 segundos."""
        while True:
            time.sleep(20)
            ts = _ts()
            with self._lock:
                estados = {d: e["status"] for d, e in self._estado.items()}

            for disp_id, status in estados.items():
                em_uso = status in ("carregando", "dispensando")
                base   = TEMP_BASE[disp_id]
                valor  = round(base + (4.0 if em_uso else 0.5) + random.uniform(-0.5, 0.5), 1)
                self.client.publish(
                    "apsen/manut/temperatura",
                    json.dumps({
                        "componente": f"dispenser_{disp_id}",
                        "valor_c":    valor,
                        "timestamp":  ts,
                    }),
                    qos=0,
                )

    # ──────────────────────────────────────────────────── Main ─────────────────

    def run(self):
        logger.info(
            f"Dispenser Simulator v2.0 | broker={MQTT_HOST}:{MQTT_PORT} "
            f"| 6 dispensers | prob_falha_ia={PROB_FALHA_IA:.0%} | prob_erro_cv={PROB_ERRO_CV:.0%}"
        )
        threading.Thread(
            target=self._telemetria_loop, daemon=True, name="telemetria"
        ).start()

        while True:
            try:
                self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                self.client.loop_forever(retry_first_connection=True)
            except Exception as exc:
                logger.error(f"Erro MQTT: {exc}. Reconectando em {RECONNECT_DELAY}s...")
                time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    DispenserSimulator().run()
