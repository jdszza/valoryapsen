"""
APSEN - Simulador SAP
=====================
Simula o sistema ERP SAP publicando Ordens de Saída (OS) no MQTT.
Em produção, a SAP real publicará diretamente neste tópico.

Tópico publicado:
  apsen/os/nova → {os_id, descricao, medicamentos: [{dispenser_id, medicamento, quantidade}]}

Os 6 dispensers têm medicamentos fixos:
  1 → Metformina 500mg
  2 → Atorvastatina 20mg
  3 → Omeprazol 20mg
  4 → Losartana 50mg
  5 → Amlodipina 5mg
  6 → Levotiroxina 25mcg

Cada OS seleciona aleatoriamente 2 a 5 dispensers com quantidades variadas.
"""

import json
import logging
import os
import random
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SAP-SIM] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

MQTT_HOST       = os.getenv("MQTT_HOST",    "mosquitto")
MQTT_PORT       = int(os.getenv("MQTT_PORT", "1883"))
INTERVALO_OS    = float(os.getenv("INTERVALO_OS", "60"))  # segundos entre OS
RECONNECT_DELAY = 5

# Mapa fixo: dispenser_id → medicamento
MEDICAMENTOS = {
    1: "Metformina 500mg",
    2: "Atorvastatina 20mg",
    3: "Omeprazol 20mg",
    4: "Losartana 50mg",
    5: "Amlodipina 5mg",
    6: "Levotiroxina 25mcg",
}

# Kits pré-definidos para OS mais realistas
KITS = [
    {
        "descricao": "Kit Diabetes + Cardiovascular",
        "dispensers": [1, 2, 4],
        "quantidades": [30, 15, 30],
    },
    {
        "descricao": "Kit Hipertensão",
        "dispensers": [4, 5],
        "quantidades": [30, 30],
    },
    {
        "descricao": "Kit Gástrico + Tireoide",
        "dispensers": [3, 6],
        "quantidades": [20, 30],
    },
    {
        "descricao": "Kit Completo Cardiovascular",
        "dispensers": [2, 4, 5],
        "quantidades": [15, 30, 30],
    },
    {
        "descricao": "Kit Diabetes Completo",
        "dispensers": [1, 3, 6],
        "quantidades": [30, 20, 25],
    },
    {
        "descricao": "Kit Multivitamínico Sintético",
        "dispensers": [1, 2, 3, 4, 5, 6],
        "quantidades": [10, 10, 10, 10, 10, 10],
    },
]

_os_counter = 0


def _gerar_os() -> dict:
    global _os_counter
    _os_counter += 1

    kit = random.choice(KITS)
    ts  = datetime.now(timezone.utc)
    os_id = f"OS-SAP-{ts.strftime('%Y%m%d')}-{_os_counter:04d}"

    medicamentos = [
        {
            "dispenser_id": disp_id,
            "medicamento":  MEDICAMENTOS[disp_id],
            "quantidade":   kit["quantidades"][i],
        }
        for i, disp_id in enumerate(kit["dispensers"])
    ]

    return {
        "os_id":        os_id,
        "descricao":    kit["descricao"],
        "medicamentos": medicamentos,
        "criado_em":    ts.isoformat(),
        "origem":       "SAP-SIM",
    }


class SAPSimulator:
    def __init__(self):
        self.client = mqtt.Client(client_id="apsen-sap-simulator", clean_session=True)
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self._conectado = False

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._conectado = True
            logger.info(f"Conectado ao broker MQTT ({MQTT_HOST}:{MQTT_PORT})")
        else:
            logger.error(f"Falha na conexão MQTT: rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self._conectado = False
        if rc != 0:
            logger.warning(f"Desconectado (rc={rc}). Reconectando em {RECONNECT_DELAY}s...")

    def _publicar_os(self):
        os_data = _gerar_os()
        payload = json.dumps(os_data, ensure_ascii=False)
        self.client.publish("apsen/os/nova", payload, qos=1)

        logger.info(
            f"\n{'='*60}\n"
            f"  📋 ORDEM DE SAÍDA PUBLICADA\n"
            f"  OS ID:    {os_data['os_id']}\n"
            f"  Kit:      {os_data['descricao']}\n"
            f"  Itens:    {len(os_data['medicamentos'])} medicamento(s)\n"
            + "".join(
                f"    → Dispenser {m['dispenser_id']}: "
                f"{m['medicamento']} × {m['quantidade']}\n"
                for m in os_data['medicamentos']
            )
            + f"{'='*60}"
        )

    def run(self):
        logger.info(
            f"SAP Simulator iniciando | broker={MQTT_HOST}:{MQTT_PORT} "
            f"| intervalo={INTERVALO_OS}s entre OS"
        )

        while True:
            try:
                self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                self.client.loop_start()

                # Aguarda conexão
                timeout = 10
                while not self._conectado and timeout > 0:
                    time.sleep(0.5)
                    timeout -= 0.5

                if not self._conectado:
                    raise ConnectionError("Timeout ao conectar ao broker MQTT")

                # Publica primeira OS após 5s (aguarda outros serviços subirem)
                logger.info(f"Aguardando 5s antes da primeira OS...")
                time.sleep(5)

                while self._conectado:
                    self._publicar_os()
                    logger.info(f"Próxima OS em {INTERVALO_OS}s...")
                    time.sleep(INTERVALO_OS)

            except Exception as exc:
                logger.error(f"Erro: {exc}. Reconectando em {RECONNECT_DELAY}s...")
                try:
                    self.client.loop_stop()
                    self.client.disconnect()
                except Exception:
                    pass
                self._conectado = False
                time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    SAPSimulator().run()
