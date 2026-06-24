"""
APSEN - Simulador SAP v2.3
Publica Ordens de Saída agrupadas por categoria terapêutica no MQTT.

IMPORTANTE — Arquitetura de dispensers:
  - O SAP NÃO define qual dispenser físico recebe cada remédio.
  - A OS contém apenas: medicamento + SKU + categoria + quantidade.
  - O sistema de dispensers (dispenser_simulator) decide qual slot usar
    baseado em estoque residual e disponibilidade de slots.

Catálogo APSEN (Desafio FIAP - Dimensões e Base FINAL.xlsx):
  SNC        : ALOIS 10MG, DONAREN 50MG, INSIT 75MG, ATENTAH 25MG
  Alimentício: LACTOSIL 10.000 FCC, LACTOSIL FLORA, PROBID, FLORACOL
  Cardiaco   : ZANIDIP 10MG, DOBEVEN 500MG, XAFAC 10MG
  Reumatol.  : ARPADOL 400MG, REUQUINOL 400MG, COLCHIS 0,5MG
  Infectologia: LEVOXIN 500MG, LECZA XR 500MG
"""
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [SAP-SIM] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MQTT_HOST    = os.getenv("MQTT_HOST",    "mosquitto")
MQTT_PORT    = int(os.getenv("MQTT_PORT", 1883))
INTERVALO_OS = int(os.getenv("INTERVALO_OS", 90))

# ── Catálogo completo de medicamentos APSEN ──────────────────────────────────
# Chave: nome do medicamento (como aparece no pedido)
CATALOGO = {
    # SNC / Neurologia / Psiquiatria
    "ALOIS 10MG":       {"sku": "ALOIS 10MG CX C/7 CP",             "categoria": "snc"},
    "DONAREN 50MG":     {"sku": "DONAREN 50MG CX C/5 CP",           "categoria": "snc"},
    "INSIT 75MG":       {"sku": "INSIT 75MG CX C/7 CAPS",           "categoria": "snc"},
    "ATENTAH 25MG":     {"sku": "ATENTAH 25MG CX C/10 CAPS",        "categoria": "snc"},
    "LENIX 50MG":       {"sku": "LENIX 50MG CX C/2 CP",             "categoria": "snc"},
    # Alimentício / Probióticos
    "LACTOSIL 10.000 FCC": {"sku": "LACTOSIL 10.000 FCC CX C/2 COMPRIMIDOS", "categoria": "alimenticio"},
    "LACTOSIL FLORA":   {"sku": "LACTOSIL FLORA CX C/2 CAPS",       "categoria": "alimenticio"},
    "PROBID":           {"sku": "PROBID CX C/2 CAPS",               "categoria": "alimenticio"},
    "FLORACOL":         {"sku": "FLORACOL CX C/2 CAPS",             "categoria": "alimenticio"},
    # Cardiologia / Vascular
    "ZANIDIP 10MG":     {"sku": "ZANIDIP 10MG C/5 CP",              "categoria": "cardiaco"},
    "DOBEVEN 500MG":    {"sku": "DOBEVEN 500MG CX C/10 CP",         "categoria": "cardiaco"},
    "XAFAC 10MG":       {"sku": "XAFAC 10MG CX C5 CP",              "categoria": "cardiaco"},
    # Reumatologia / Dor
    "ARPADOL 400MG":    {"sku": "ARPADOL 400MG CX C/5 CP",          "categoria": "reumatologia"},
    "REUQUINOL 400MG":  {"sku": "REUQUINOL 400MG CX C/15 CP",       "categoria": "reumatologia"},
    "COLCHIS 0,5MG":    {"sku": "COLCHIS 0,5MG CX C/15 CP",         "categoria": "reumatologia"},
    # Infectologia
    "LEVOXIN 500MG":    {"sku": "LEVOXIN 500MG CX C/3 CP",          "categoria": "infectologia"},
    "LECZA XR 500MG":   {"sku": "LECZA XR 500MG CX C/5 CP",         "categoria": "infectologia"},
}

# ── Kits terapêuticos ─────────────────────────────────────────────────────────
# Cada kit define: quais medicamentos compõem a OS e a faixa de quantidade.
# Não há referência a dispensers — o sistema de dispensers decide o roteamento.
KITS = [
    # ── SNC ──────────────────────────────────────────────────────────────────
    {
        "nome":        "Kit SNC Completo — ALOIS + DONAREN",
        "categoria":   "snc",
        "medicamentos": ["ALOIS 10MG", "DONAREN 50MG"],
        "qtd_range":   (5, 14),
    },
    {
        "nome":        "Kit SNC Ansiolítico — DONAREN + INSIT",
        "categoria":   "snc",
        "medicamentos": ["DONAREN 50MG", "INSIT 75MG"],
        "qtd_range":   (5, 10),
    },
    {
        "nome":        "Kit SNC TDAH — ATENTAH + LENIX",
        "categoria":   "snc",
        "medicamentos": ["ATENTAH 25MG", "LENIX 50MG"],
        "qtd_range":   (7, 14),
    },
    {
        "nome":        "Kit SNC — ALOIS 10MG (unitário)",
        "categoria":   "snc",
        "medicamentos": ["ALOIS 10MG"],
        "qtd_range":   (7, 21),
    },
    # ── Alimentício ───────────────────────────────────────────────────────────
    {
        "nome":        "Kit Alimentício Completo — LACTOSIL + FLORA",
        "categoria":   "alimenticio",
        "medicamentos": ["LACTOSIL 10.000 FCC", "LACTOSIL FLORA"],
        "qtd_range":   (2, 8),
    },
    {
        "nome":        "Kit Probiótico — PROBID + FLORACOL",
        "categoria":   "alimenticio",
        "medicamentos": ["PROBID", "FLORACOL"],
        "qtd_range":   (2, 6),
    },
    {
        "nome":        "Kit Intolerância à Lactose — LACTOSIL 10.000 (unitário)",
        "categoria":   "alimenticio",
        "medicamentos": ["LACTOSIL 10.000 FCC"],
        "qtd_range":   (2, 10),
    },
    # ── Cardiaco ──────────────────────────────────────────────────────────────
    {
        "nome":        "Kit Cardiovascular — ZANIDIP + DOBEVEN",
        "categoria":   "cardiaco",
        "medicamentos": ["ZANIDIP 10MG", "DOBEVEN 500MG"],
        "qtd_range":   (5, 10),
    },
    {
        "nome":        "Kit Cardiológico Completo — ZANIDIP + DOBEVEN + XAFAC",
        "categoria":   "cardiaco",
        "medicamentos": ["ZANIDIP 10MG", "DOBEVEN 500MG", "XAFAC 10MG"],
        "qtd_range":   (5, 10),
    },
    {
        "nome":        "Kit Anti-hipertensivo — XAFAC 10MG (unitário)",
        "categoria":   "cardiaco",
        "medicamentos": ["XAFAC 10MG"],
        "qtd_range":   (5, 15),
    },
    # ── Reumatologia ─────────────────────────────────────────────────────────
    {
        "nome":        "Kit Reumatologia — ARPADOL + REUQUINOL",
        "categoria":   "reumatologia",
        "medicamentos": ["ARPADOL 400MG", "REUQUINOL 400MG"],
        "qtd_range":   (5, 15),
    },
    {
        "nome":        "Kit Gota — COLCHIS + REUQUINOL",
        "categoria":   "reumatologia",
        "medicamentos": ["COLCHIS 0,5MG", "REUQUINOL 400MG"],
        "qtd_range":   (5, 15),
    },
    # ── Infectologia ─────────────────────────────────────────────────────────
    {
        "nome":        "Kit Antibiótico — LEVOXIN + LECZA XR",
        "categoria":   "infectologia",
        "medicamentos": ["LEVOXIN 500MG", "LECZA XR 500MG"],
        "qtd_range":   (3, 10),
    },
    # ── Multiterápicos ────────────────────────────────────────────────────────
    {
        "nome":        "Kit Neurológico + Suporte Intestinal",
        "categoria":   "snc+alimenticio",
        "medicamentos": ["ALOIS 10MG", "LACTOSIL FLORA"],
        "qtd_range":   (5, 10),
    },
    {
        "nome":        "Kit Cardiológico + Neurológico",
        "categoria":   "cardiaco+snc",
        "medicamentos": ["ZANIDIP 10MG", "DONAREN 50MG"],
        "qtd_range":   (5, 10),
    },
]


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gerar_os() -> dict:
    kit = random.choice(KITS)
    os_id = (
        f"OS-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        f"-{str(uuid.uuid4())[:6].upper()}"
    )
    qtd_min, qtd_max = kit["qtd_range"]

    # Monta lista de medicamentos SEM dispenser_id
    # O sistema de dispensers é responsável pelo roteamento físico
    medicamentos = []
    for med_nome in kit["medicamentos"]:
        info = CATALOGO[med_nome]
        medicamentos.append({
            "medicamento": med_nome,
            "sku":         info["sku"],
            "categoria":   info["categoria"],
            "quantidade":  random.randint(qtd_min, qtd_max),
        })

    paciente_id = f"PAC-{random.randint(10000, 99999)}"
    lote        = f"LOTE-{random.randint(1000, 9999)}"

    return {
        "os_id":        os_id,
        "descricao":    f"{kit['nome']} — {paciente_id}",
        "categoria":    kit["categoria"],
        "kit":          kit["nome"],
        "lote":         lote,
        "paciente_id":  paciente_id,
        "medicamentos": medicamentos,
        "criado_em":    _ts(),
        "origem":       "SAP_SIM_v2.3",
    }


def main():
    client = mqtt.Client(client_id="apsen-sap-simulator-v23")

    def on_connect(c, userdata, flags, rc):
        if rc == 0:
            logger.info(f"SAP Simulator v2.3 conectado ({MQTT_HOST}:{MQTT_PORT})")
        else:
            logger.error(f"Falha MQTT rc={rc}")

    client.on_connect = on_connect

    for attempt in range(30):
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            break
        except Exception as exc:
            logger.warning(f"Aguardando broker ({attempt+1}/30): {exc}")
            time.sleep(3)
    else:
        logger.error("Falha ao conectar ao broker após 30 tentativas.")
        return

    client.loop_start()
    time.sleep(5)

    logger.info(f"SAP Simulator v2.3 pronto — OS a cada {INTERVALO_OS}s")
    logger.info(f"{len(CATALOGO)} medicamentos no catálogo | {len(KITS)} kits disponíveis")
    logger.info("Dispensers: roteamento dinâmico (nenhum slot é fixo)")

    while True:
        try:
            os_payload = _gerar_os()
            client.publish("apsen/os/nova", json.dumps(os_payload, ensure_ascii=False), qos=1)
            itens_str = " | ".join(
                f"{m['medicamento']} ×{m['quantidade']}"
                for m in os_payload["medicamentos"]
            )
            logger.info(f"[OS] {os_payload['os_id']} — {os_payload['kit']}")
            logger.info(f"     Medicamentos: {itens_str}")
        except Exception as exc:
            logger.error(f"Erro ao publicar OS: {exc}", exc_info=True)

        time.sleep(INTERVALO_OS)


if __name__ == "__main__":
    main()
