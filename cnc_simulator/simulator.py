"""
APSEN - Simulador CNC de Contagem de Medicamentos
=================================================
Simula o firmware da máquina CNC, publicando contagem em tempo real via MQTT.

Estado da máquina:
  idle    → aguardando comando
  running → contando remédios
  paused  → pausado (aguarda resume)
  alarm   → alarme ativo (aguarda reset)

Comandos recebidos (apsen/cmd/#):
  apsen/cmd/lote   → {lote_id, meta, produto}  — novo lote, auto-start em 2s
  apsen/cmd/status → {cmd: "start"|"pause"|"stop"}
  apsen/cmd/reset  → {} — reseta contagem e sai do alarme

Publicações:
  apsen/contagem  → {valor, velocidade}            — a cada INTERVALO_PUB segundos
  apsen/status    → {status, alarme, evento?}       — em mudanças de estado
  apsen/lote      → {lote_id, meta, produto}        — ao receber cmd/lote (confirma)
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
    format="%(asctime)s [CNC] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ── Configuração ───────────────────────────────────────────────────────────────
MQTT_HOST        = os.getenv("MQTT_HOST",        "mosquitto")
MQTT_PORT        = int(os.getenv("MQTT_PORT",    "1883"))
VELOCIDADE_BASE  = float(os.getenv("VEL_BASE",   "120"))   # unidades/min em regime estável
INTERVALO_PUB    = float(os.getenv("INTERVALO",  "1.0"))   # segundos entre publicações
WARMUP_SEG       = float(os.getenv("WARMUP",     "2.0"))   # segundos de aquecimento antes de contar
RECONNECT_DELAY  = 5   # segundos antes de tentar reconectar


class CNCSimulator:
    """Máquina de estados da CNC + cliente MQTT."""

    def __init__(self):
        self.state      = "idle"
        self.lote_id    = "LOTE-INICIAL"
        self.produto    = "Produto APSEN"
        self.meta       = 1000
        self.contagem   = 0
        self.velocidade = 0.0
        self.alarme     = None
        self._lock      = threading.Lock()

        self.client = mqtt.Client(client_id="apsen-cnc-simulator", clean_session=True)
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

    # ── MQTT callbacks ─────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe("apsen/cmd/#", qos=1)
            logger.info("Conectado ao broker MQTT. Subscrito em apsen/cmd/#")
            self._publish_status()
        else:
            logger.error(f"Falha na conexão MQTT: rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning(f"Desconectado do broker (rc={rc}). Reconectando em {RECONNECT_DELAY}s...")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            topic   = msg.topic
            logger.info(f"← {topic}: {payload}")

            with self._lock:
                if topic == "apsen/cmd/lote":
                    self._handle_lote(payload)

                elif topic == "apsen/cmd/status":
                    self._handle_status_cmd(payload.get("cmd", ""))

                elif topic == "apsen/cmd/reset":
                    self._handle_reset()

        except json.JSONDecodeError as e:
            logger.warning(f"Payload inválido em {msg.topic}: {e}")
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {e}", exc_info=True)

    # ── Handlers de comando ────────────────────────────────────────────────────

    def _handle_lote(self, payload: dict):
        """Novo lote recebido — armazena info, confirma via apsen/lote e
        agenda auto-start após WARMUP_SEG segundos (simula aquecimento)."""
        self.lote_id  = payload.get("lote_id", self.lote_id)
        self.meta     = int(payload.get("meta",    self.meta))
        self.produto  = payload.get("produto", self.produto)
        self.contagem = 0
        self.velocidade = 0.0
        self.alarme   = None
        self.state    = "idle"

        logger.info(f"Novo lote recebido: {self.lote_id} | meta={self.meta} | produto={self.produto}")

        # Publica apsen/lote para o backend atualizar o estado interno
        self.client.publish(
            "apsen/lote",
            json.dumps({
                "lote_id": self.lote_id,
                "meta":    self.meta,
                "produto": self.produto,
            }),
            qos=1,
        )
        self._publish_status()

        # Auto-start após aquecimento (chama fora do lock via timer)
        threading.Timer(WARMUP_SEG, self._auto_start).start()

    def _auto_start(self):
        """Inicia contagem automaticamente após aquecimento."""
        with self._lock:
            if self.state == "idle":   # só inicia se ainda parado (não foi stopado manualmente)
                self.state = "running"
                logger.info(f"Máquina AQUECIDA — iniciando contagem do lote {self.lote_id}")
        self._publish_status()

    def _handle_status_cmd(self, cmd: str):
        prev = self.state
        if cmd == "start":
            if self.state in ("idle", "paused"):
                self.state = "running"
        elif cmd == "pause":
            if self.state == "running":
                self.state = "paused"
                self.velocidade = 0.0
        elif cmd == "stop":
            self.state = "idle"
            self.velocidade = 0.0
        else:
            logger.warning(f"Comando desconhecido: {cmd}")
            return

        if self.state != prev:
            logger.info(f"Estado: {prev} → {self.state} (cmd={cmd})")
            # Publica fora do lock via thread para não deadlock
            threading.Thread(target=self._publish_status, daemon=True).start()

    def _handle_reset(self):
        prev_state = self.state
        self.contagem   = 0
        self.velocidade = 0.0
        if self.state == "alarm":
            self.state  = "idle"
            self.alarme = None
        logger.info(f"Reset: contagem zerada. Estado: {prev_state} → {self.state}")
        threading.Thread(target=self._publish_status, daemon=True).start()

    # ── Publicações MQTT ───────────────────────────────────────────────────────

    def _publish_status(self, extra: dict = None):
        with self._lock:
            payload = {"status": self.state, "alarme": self.alarme}
        if extra:
            payload.update(extra)
        self.client.publish("apsen/status", json.dumps(payload), qos=0, retain=True)
        logger.debug(f"→ apsen/status: {payload}")

    def _publish_contagem(self):
        with self._lock:
            payload = {
                "valor":      self.contagem,
                "velocidade": round(self.velocidade, 1),
            }
        self.client.publish("apsen/contagem", json.dumps(payload), qos=0)

    # ── Loop de contagem ───────────────────────────────────────────────────────

    def _counting_loop(self):
        """Roda em daemon thread. A cada INTERVALO_PUB segundos,
        incrementa a contagem se estiver no estado 'running'."""
        ramp_ciclos = 0  # ciclos desde que entrou em 'running' (para ramp-up)

        while True:
            time.sleep(INTERVALO_PUB)

            with self._lock:
                state = self.state

            if state == "running":
                ramp_ciclos += 1
                # Ramp-up: velocidade cresce nos primeiros 5 ciclos
                fator_ramp = min(1.0, ramp_ciclos / 5.0)
                # Variação aleatória ±15%
                variacao = random.uniform(0.85, 1.15)
                vel = VELOCIDADE_BASE * fator_ramp * variacao

                # Incremento por intervalo: vel(un/min) / 60 * INTERVALO_PUB(s)
                incremento = max(1, round(vel / 60.0 * INTERVALO_PUB * 60))

                with self._lock:
                    restante = self.meta - self.contagem
                    if restante <= 0:
                        # Já chegou na meta (pode ter chegado entre ciclos)
                        self._concluir_lote()
                        ramp_ciclos = 0
                        continue

                    if incremento >= restante:
                        # Última parcela — bate exatamente na meta
                        self.contagem   = self.meta
                        self.velocidade = vel
                        concluido = True
                    else:
                        self.contagem   += incremento
                        self.velocidade  = vel
                        concluido = False

                self._publish_contagem()

                if concluido:
                    with self._lock:
                        self._concluir_lote()
                    ramp_ciclos = 0

            elif state == "paused":
                ramp_ciclos = 0
                self._publish_contagem()  # mantém dashboard atualizado mesmo pausado

            else:
                # idle ou alarm — não conta
                ramp_ciclos = 0

    def _concluir_lote(self):
        """Chamado dentro do _lock quando contagem == meta."""
        self.state      = "idle"
        self.velocidade = 0.0
        logger.info(
            f"✓ Lote {self.lote_id} CONCLUÍDO: {self.contagem}/{self.meta} unidades produzidas."
        )
        # Publica fora do lock
        detalhe = f"Lote {self.lote_id} concluído: {self.meta} unidades produzidas."
        threading.Thread(
            target=self._publish_status,
            kwargs={"extra": {"evento": "lote_concluido", "detalhe": detalhe}},
            daemon=True,
        ).start()

    # ── Entrada ───────────────────────────────────────────────────────────────

    def run(self):
        logger.info(
            f"Iniciando CNC Simulator | broker={MQTT_HOST}:{MQTT_PORT} "
            f"| vel_base={VELOCIDADE_BASE} un/min | intervalo={INTERVALO_PUB}s"
        )

        # Thread de contagem (daemon — termina quando o processo principal terminar)
        threading.Thread(target=self._counting_loop, daemon=True, name="cnc-counter").start()

        # Conecta e mantém loop MQTT (reconexão automática)
        while True:
            try:
                self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                self.client.loop_forever(retry_first_connection=True)
            except Exception as exc:
                logger.error(f"Erro no loop MQTT: {exc}. Reconectando em {RECONNECT_DELAY}s...")
                time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    CNCSimulator().run()
