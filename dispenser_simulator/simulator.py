"""
APSEN - Simulador de Dispensers v2.1
=====================================
Fluxo por OS:
  1. OS recebida → IA atribui dispensers
  2. FASE CARREGAMENTO: CV valida unidade a unidade
     - Erro CV → publica erro + abort imediato + avisa CNC
  3. FASE DISPENSA: aguarda CNC posicionar (com timeout)
     - Timeout → abort + retorna para idle
     - CNC erro → abort
  4. Dispensa remédio a remédio com validação IA

Publicações:
  apsen/ia/atribuicao           → IA mapeia dispenser → remédio
  apsen/dispenser/carregamento  → progresso do CV
  apsen/dispenser/pronto        → dispenser pronto para dispensar
  apsen/dispenser/evento        → cada remédio dispensado
  apsen/dispenser/status        → estado atual do dispenser
  apsen/manut/temperatura       → telemetria

Subscrições:
  apsen/os/nova    — recebe OS da SAP
  apsen/cnc/status — detecta posicionado / erro da CNC
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
T_CARGA_UNID    = float(os.getenv("T_CARGA_UNID",   "0.3"))
T_DISPENSA_UNID = float(os.getenv("T_DISPENSA_UNID", "0.5"))
PROB_FALHA_IA   = float(os.getenv("PROB_FALHA_IA",   "0.05"))
PROB_ERRO_CV    = float(os.getenv("PROB_ERRO_CV",    "0.01"))  # 1%: ~10% chance por OS
TIMEOUT_CNC_POS = float(os.getenv("TIMEOUT_CNC_POS", "240"))   # s aguardando CNC posicionar
RECONNECT_DELAY = 5

MEDICAMENTOS_FIXOS = {
    1: "Metformina 500mg",
    2: "Atorvastatina 20mg",
    3: "Omeprazol 20mg",
    4: "Losartana 50mg",
    5: "Amlodipina 5mg",
    6: "Levotiroxina 25mcg",
}
TEMP_BASE = {d: 22 + d * 0.5 for d in range(1, 7)}


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class DispenserSimulator:
    def __init__(self):
        self._estado: dict[int, dict] = {
            d: {
                "status":         "idle",
                "medicamento":    MEDICAMENTOS_FIXOS[d],
                "os_id":          None,
                "qtd_alvo":       0,
                "qtd_dispensada": 0,
            }
            for d in range(1, 7)
        }
        self._lock          = threading.Lock()
        self._os_ativa: dict | None = None
        self._atribuicoes: dict[int, dict] = {}
        self._erro_ativo    = False   # CV error flag (loading phase)
        self._abort_flag    = False   # abort all phases (CNC error / timeout)

        # Por dispenser: sinalizado quando CNC posiciona
        self._posicionado_event: dict[int, threading.Event] = {
            d: threading.Event() for d in range(1, 7)
        }

        self.client = mqtt.Client(
            client_id="apsen-dispenser-sim-v21", clean_session=True
        )
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

    # ── MQTT ─────────────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe("apsen/os/nova",    qos=1)
            client.subscribe("apsen/cnc/status", qos=0)
            logger.info("Dispenser Simulator v2.1 conectado. Aguardando OS...")

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

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _handle_os_nova(self, payload: dict):
        with self._lock:
            if self._os_ativa:
                logger.warning(
                    f"[DISP] OS em andamento ({self._os_ativa.get('os_id')}) "
                    f"— ignorando {payload.get('os_id')}."
                )
                return
            self._os_ativa   = payload
            self._erro_ativo = False
            self._abort_flag = False
            self._atribuicoes = {}
            for e in self._posicionado_event.values():
                e.clear()

        logger.info(f"[DISP] OS recebida: {payload.get('os_id')}")
        threading.Thread(
            target=self._processar_os, args=(payload,), daemon=True
        ).start()

    def _handle_cnc_status(self, payload: dict):
        status   = payload.get("status")
        disp_alv = payload.get("dispenser_alvo")
        cnc_os   = payload.get("os_id")

        if status == "posicionado" and disp_alv is not None:
            disp_id = int(disp_alv)
            if disp_id in self._posicionado_event:
                logger.info(f"[DISP-{disp_id}] CNC posicionada — iniciando dispensa!")
                self._posicionado_event[disp_id].set()

        elif status == "erro":
            # CNC reportou erro — verificar se é para a OS ativa
            with self._lock:
                os_id_ativo = self._os_ativa.get("os_id") if self._os_ativa else None
            if cnc_os and cnc_os == os_id_ativo:
                logger.error(f"[DISP] CNC reportou erro para OS {cnc_os} — abortando.")
                threading.Thread(target=self._abortar_os, daemon=True).start()

    # ── Abort ─────────────────────────────────────────────────────────────────

    def _abortar_os(self):
        """Reseta todo o estado e libera threads em espera."""
        with self._lock:
            os_id = self._os_ativa.get("os_id") if self._os_ativa else "?"
            self._os_ativa   = None
            self._atribuicoes = {}
            self._erro_ativo = False
            self._abort_flag = True
            # Unblock all waiting dispense threads
            for e in self._posicionado_event.values():
                e.set()
            # Reset estado dos dispensers
            for d in self._estado.values():
                d["status"] = "idle"
                d["os_id"]  = None
                d["qtd_alvo"] = 0
                d["qtd_dispensada"] = 0

        logger.warning(f"[DISP] OS {os_id} abortada — voltando para idle.")
        ts = _ts()
        for d in range(1, 7):
            self.client.publish(
                "apsen/dispenser/status",
                json.dumps({"dispenser_id": d, "status": "idle", "timestamp": ts}),
                qos=0,
            )

    # ── Processamento da OS ───────────────────────────────────────────────────

    def _processar_os(self, os_payload: dict):
        os_id        = os_payload["os_id"]
        medicamentos = os_payload.get("medicamentos", [])

        # ── Etapa 1: IA define atribuições ───────────────────────────────────
        atribuicoes = []
        for item in medicamentos:
            disp_id = int(item["dispenser_id"])
            med     = MEDICAMENTOS_FIXOS.get(disp_id, "Desconhecido")
            atribuicoes.append({
                "dispenser_id": disp_id,
                "medicamento":  med,
                "quantidade":   item["quantidade"],
            })
            with self._lock:
                self._atribuicoes[disp_id] = {"medicamento": med, "quantidade": item["quantidade"]}

        self.client.publish(
            "apsen/ia/atribuicao",
            json.dumps({"os_id": os_id, "atribuicoes": atribuicoes, "timestamp": _ts()}),
            qos=1,
        )
        logger.info(
            f"[DISP-IA] Atribuição publicada: "
            f"{[(a['dispenser_id'], a['medicamento']) for a in atribuicoes]}"
        )

        # ── Etapa 2: Carregamento paralelo ────────────────────────────────────
        threads_carga = [
            threading.Thread(
                target=self._fase_carregamento,
                args=(os_id, a["dispenser_id"], a["medicamento"], a["quantidade"]),
                daemon=True,
            )
            for a in atribuicoes
        ]
        for t in threads_carga:
            t.start()
        for t in threads_carga:
            t.join()

        # Verificar se houve erro de CV ou abort
        with self._lock:
            erro   = self._erro_ativo
            abort  = self._abort_flag

        if erro or abort:
            logger.error(
                f"[DISP] OS {os_id} encerrada na fase de carregamento "
                f"(CV_erro={erro}, abort={abort})."
            )
            with self._lock:
                self._os_ativa = None
            return

        logger.info(f"[DISP] TODOS OS DISPENSERS PRONTOS para OS {os_id}!")

        # ── Etapa 3: Dispensa (aguarda CNC) ──────────────────────────────────
        threads_disp = [
            threading.Thread(
                target=self._fase_dispensa,
                args=(os_id, a["dispenser_id"], a["medicamento"], a["quantidade"]),
                daemon=True,
            )
            for a in atribuicoes
        ]
        for t in threads_disp:
            t.start()
        for t in threads_disp:
            t.join()

        with self._lock:
            self._os_ativa    = None
            self._atribuicoes = {}
            self._abort_flag  = False

        logger.info(f"[DISP] OS {os_id} finalizada.")

    # ── Fase 1: Carregamento ──────────────────────────────────────────────────

    def _fase_carregamento(self, os_id: str, disp_id: int, medicamento: str, quantidade: int):
        logger.info(f"[DISP-{disp_id}] Iniciando carregamento: {medicamento} × {quantidade}")

        with self._lock:
            self._estado[disp_id].update({
                "status": "carregando", "os_id": os_id,
                "qtd_alvo": quantidade, "qtd_dispensada": 0,
            })

        self.client.publish(
            "apsen/dispenser/carregamento",
            json.dumps({
                "os_id": os_id, "dispenser_id": disp_id,
                "medicamento": medicamento, "status": "iniciado", "timestamp": _ts(),
            }),
            qos=0,
        )

        for i in range(1, quantidade + 1):
            with self._lock:
                if self._erro_ativo or self._abort_flag:
                    return

            time.sleep(T_CARGA_UNID)

            if random.random() < PROB_ERRO_CV:
                motivo = random.choice([
                    f"Medicamento errado detectado na unidade {i} (CV: código de barras inválido)",
                    f"Unidade {i} danificada pela visão computacional",
                    f"Contaminação detectada na unidade {i}",
                ])
                logger.error(f"[DISP-{disp_id}] ERRO CV unidade {i}: {motivo}")

                # Publica erro de carregamento
                self.client.publish(
                    "apsen/dispenser/carregamento",
                    json.dumps({
                        "os_id": os_id, "dispenser_id": disp_id,
                        "medicamento": medicamento, "status": "erro",
                        "unidade": i, "motivo_falha": motivo, "timestamp": _ts(),
                    }),
                    qos=1,
                )
                self.client.publish(
                    "apsen/dispenser/status",
                    json.dumps({
                        "os_id": os_id, "dispenser_id": disp_id,
                        "status": "erro", "motivo": motivo, "timestamp": _ts(),
                    }),
                    qos=1,
                )

                with self._lock:
                    self._erro_ativo = True
                    self._estado[disp_id]["status"] = "erro"
                return

        # Carregamento OK ─────────────────────────────────────────────────────
        logger.info(f"[DISP-{disp_id}] Carregamento OK — {quantidade} × {medicamento}")

        with self._lock:
            self._estado[disp_id]["status"] = "pronto"

        self.client.publish(
            "apsen/dispenser/carregamento",
            json.dumps({
                "os_id": os_id, "dispenser_id": disp_id, "medicamento": medicamento,
                "status": "ok", "quantidade": quantidade, "timestamp": _ts(),
            }),
            qos=0,
        )
        self.client.publish(
            "apsen/dispenser/pronto",
            json.dumps({
                "os_id": os_id, "dispenser_id": disp_id,
                "medicamento": medicamento, "quantidade_alvo": quantidade, "timestamp": _ts(),
            }),
            qos=1,
        )
        self.client.publish(
            "apsen/dispenser/status",
            json.dumps({
                "os_id": os_id, "dispenser_id": disp_id, "medicamento": medicamento,
                "status": "pronto", "timestamp": _ts(),
            }),
            qos=1,
        )
        logger.info(f"[DISP-{disp_id}] PRONTO para dispensar.")

    # ── Fase 2: Dispensa ──────────────────────────────────────────────────────

    def _fase_dispensa(self, os_id: str, disp_id: int, medicamento: str, quantidade: int):
        logger.info(f"[DISP-{disp_id}] Aguardando CNC posicionar (timeout={TIMEOUT_CNC_POS}s)...")

        chegou = self._posicionado_event[disp_id].wait(timeout=TIMEOUT_CNC_POS)

        # Verificar abort (pode ter sido setado por _abortar_os liberando o evento)
        with self._lock:
            abort = self._abort_flag

        if not chegou or abort:
            logger.error(
                f"[DISP-{disp_id}] "
                + ("Timeout aguardando CNC." if not chegou else "Abortado.")
                + " Encerrando dispensa."
            )
            if not chegou:
                # Timeout: inicia abort do sistema inteiro
                threading.Thread(target=self._abortar_os, daemon=True).start()
            return

        with self._lock:
            self._estado[disp_id]["status"] = "dispensando"

        self.client.publish(
            "apsen/dispenser/status",
            json.dumps({
                "os_id": os_id, "dispenser_id": disp_id,
                "status": "dispensando", "timestamp": _ts(),
            }),
            qos=0,
        )

        dispensado = 0
        for i in range(1, quantidade + 1):
            with self._lock:
                if self._abort_flag:
                    return

            time.sleep(T_DISPENSA_UNID)

            validado     = random.random() >= PROB_FALHA_IA
            motivo_falha = None

            if not validado:
                motivo_falha = random.choice([
                    "Comprimido partido detectado pela IA",
                    "Peso fora do padrão — comprimido incompleto",
                    "Formato irregular detectado",
                ])
                logger.warning(f"[DISP-{disp_id}] IA rejeitou unidade {i}: {motivo_falha}")
            else:
                dispensado += 1

            with self._lock:
                self._estado[disp_id]["qtd_dispensada"] = dispensado

            self.client.publish(
                "apsen/dispenser/evento",
                json.dumps({
                    "os_id": os_id, "dispenser_id": disp_id,
                    "medicamento": medicamento, "seq": i,
                    "quantidade_dispensada": dispensado, "quantidade_alvo": quantidade,
                    "validado": validado, "motivo_falha": motivo_falha,
                    "timestamp": _ts(),
                }),
                qos=0,
            )

        # Concluído ───────────────────────────────────────────────────────────
        logger.info(
            f"[DISP-{disp_id}] CONCLUÍDO: {dispensado}/{quantidade} × {medicamento}"
        )
        with self._lock:
            self._estado[disp_id]["status"] = "concluido"

        self.client.publish(
            "apsen/dispenser/status",
            json.dumps({
                "os_id": os_id, "dispenser_id": disp_id, "medicamento": medicamento,
                "status": "concluido",
                "quantidade_dispensada": dispensado, "quantidade_alvo": quantidade,
                "timestamp": _ts(),
            }),
            qos=1,
        )

    # ── Telemetria ─────────────────────────────────────────────────────────────

    def _telemetria_loop(self):
        while True:
            time.sleep(20)
            ts = _ts()
            with self._lock:
                estados = {d: e["status"] for d, e in self._estado.items()}

            for disp_id, status in estados.items():
                em_uso = status in ("carregando", "dispensando")
                valor  = round(
                    TEMP_BASE[disp_id]
                    + (4.0 if em_uso else 0.5)
                    + random.uniform(-0.5, 0.5),
                    1,
                )
                self.client.publish(
                    "apsen/manut/temperatura",
                    json.dumps({"componente": f"dispenser_{disp_id}",
                                "valor_c": valor, "timestamp": ts}),
                    qos=0,
                )

    # ── Main ───────────────────────────────────────────────────────────────────

    def run(self):
        logger.info(
            f"Dispenser Simulator v2.1 | broker={MQTT_HOST}:{MQTT_PORT} "
            f"| prob_falha_ia={PROB_FALHA_IA:.0%} "
            f"| prob_erro_cv={PROB_ERRO_CV:.0%} "
            f"| timeout_cnc_pos={TIMEOUT_CNC_POS}s"
        )
        threading.Thread(target=self._telemetria_loop, daemon=True, name="telemetria").start()

        while True:
            try:
                self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                self.client.loop_forever(retry_first_connection=True)
            except Exception as exc:
                logger.error(f"Erro MQTT: {exc}. Reconectando em {RECONNECT_DELAY}s...")
                time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    DispenserSimulator().run()
