"""
APSEN - Simulador SAP v3.0
Gera Ordens de Saida (OS) aleatorias baseadas no catalogo real do banco de dados.
"""
import json
import logging
import os
import random
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SAP-SIM] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

MQTT_HOST           = os.getenv("MQTT_HOST",           "mosquitto")
MQTT_PORT           = int(os.getenv("MQTT_PORT",        1883))
BACKEND_URL         = os.getenv("BACKEND_URL",          "http://backend:8000")
INTERVALO_OS        = int(os.getenv("INTERVALO_OS",      90))
RELOAD_CATALOGO_MIN = int(os.getenv("RELOAD_CATALOGO_MIN", 30))
MIN_MEDS_POR_OS     = int(os.getenv("MIN_MEDS_POR_OS",  2))
MAX_MEDS_POR_OS     = int(os.getenv("MAX_MEDS_POR_OS",  6))
QTD_MIN             = int(os.getenv("QTD_MIN",          2))
QTD_MAX             = int(os.getenv("QTD_MAX",         15))


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Catalogo ──────────────────────────────────────────────────────────────────

def _carregar_catalogo(tentativas: int = 20) -> dict:
    """
    Busca medicamentos do backend e organiza por categoria.
    Retenta com espera progressiva para dar tempo ao DB popular os seeds.
    """
    url = BACKEND_URL + "/medicamentos"
    for i in range(tentativas):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            meds = resp.json()

            if not meds:
                espera = min(10, 3 + i * 2)
                logger.warning(
                    "Catalogo vazio (DB ainda inicializando?). "
                    "Tentativa %d/%d. Aguardando %ds...", i + 1, tentativas, espera
                )
                time.sleep(espera)
                continue

            catalogo = defaultdict(list)
            for m in meds:
                catalogo[m["categoria"]].append(m)

            catalogo = {
                cat: lista
                for cat, lista in catalogo.items()
                if len(lista) >= MIN_MEDS_POR_OS
            }

            total = sum(len(v) for v in catalogo.values())
            if total == 0:
                logger.warning("Nenhuma categoria valida ainda. Tentativa %d/%d.", i + 1, tentativas)
                time.sleep(5)
                continue

            logger.info("Catalogo carregado: %d medicamentos em %d categorias", total, len(catalogo))
            for cat, meds_cat in sorted(catalogo.items()):
                logger.info("  [%s] %d medicamentos", cat, len(meds_cat))
            return dict(catalogo)

        except Exception as exc:
            espera = min(15, 5 + i * 2)
            logger.warning("Catalogo: tentativa %d/%d falhou - %s. Aguardando %ds...",
                           i + 1, tentativas, exc, espera)
            time.sleep(espera)

    logger.error("Nao foi possivel carregar o catalogo. Encerrando.")
    raise SystemExit(1)


# ── Geracao de OS ─────────────────────────────────────────────────────────────

def _gerar_os(catalogo: dict) -> dict:
    """
    Gera uma OS aleatoria:
    - Escolhe categoria aleatoria ponderada pelo tamanho
    - Sorteia N medicamentos distintos (MIN_MEDS_POR_OS <= N <= MAX_MEDS_POR_OS)
    - Atribui quantidade aleatoria para cada
    """
    if not catalogo:
        raise ValueError("Catalogo de medicamentos esta vazio.")

    categorias = list(catalogo.keys())
    pesos      = [len(catalogo[c]) for c in categorias]
    categoria  = random.choices(categorias, weights=pesos, k=1)[0]

    meds_disponiveis = catalogo[categoria]
    n            = min(random.randint(MIN_MEDS_POR_OS, MAX_MEDS_POR_OS), len(meds_disponiveis))
    selecionados = random.sample(meds_disponiveis, n)

    ts_str      = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    uid_str     = str(uuid.uuid4())[:6].upper()
    os_id       = "OS-" + ts_str + "-" + uid_str
    paciente_id = "PAC-" + str(random.randint(10000, 99999))
    lote        = "LOTE-" + str(random.randint(1000, 9999))

    medicamentos = [
        {
            "medicamento": m["nome"],
            "sku":         m["sku"],
            "categoria":   m["categoria"],
            "quantidade":  random.randint(QTD_MIN, QTD_MAX),
        }
        for m in selecionados
    ]

    cat_desc  = selecionados[0].get("categoria_desc", categoria)
    descricao = cat_desc + " - " + paciente_id

    return {
        "os_id":          os_id,
        "descricao":      descricao,
        "categoria":      categoria,
        "categoria_desc": cat_desc,
        "lote":           lote,
        "paciente_id":    paciente_id,
        "medicamentos":   medicamentos,
        "criado_em":      _ts(),
        "origem":         "SAP_SIM_v3.0",
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logger.info("Aguardando backend (%s)...", BACKEND_URL)
    for i in range(40):
        try:
            requests.get(BACKEND_URL + "/ping", timeout=5)
            logger.info("Backend disponivel.")
            break
        except Exception:
            logger.info("  backend nao disponivel ainda (%d/40)...", i + 1)
            time.sleep(5)

    catalogo      = _carregar_catalogo()
    ultimo_reload = time.time()

    client = mqtt.Client(client_id="apsen-sap-simulator-v30")

    def on_connect(c, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT conectado - %s:%d", MQTT_HOST, MQTT_PORT)
        else:
            logger.error("MQTT falhou rc=%d", rc)

    client.on_connect = on_connect

    for attempt in range(30):
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            break
        except Exception as exc:
            logger.warning("Broker nao disponivel (%d/30): %s", attempt + 1, exc)
            time.sleep(3)
    else:
        logger.error("Nao foi possivel conectar ao broker MQTT.")
        return

    client.loop_start()
    time.sleep(3)

    total_meds = sum(len(v) for v in catalogo.values())
    logger.info("SAP Simulator v3.0 pronto - OS a cada %ds | %d medicamentos disponiveis",
                INTERVALO_OS, total_meds)

    while True:
        if time.time() - ultimo_reload > RELOAD_CATALOGO_MIN * 60:
            try:
                catalogo      = _carregar_catalogo(tentativas=3)
                ultimo_reload = time.time()
            except Exception as exc:
                logger.warning("Falha ao recarregar catalogo: %s. Usando anterior.", exc)

        try:
            if not catalogo:
                logger.warning("Catalogo vazio - recarregando antes de gerar OS...")
                catalogo      = _carregar_catalogo()
                ultimo_reload = time.time()

            os_payload = _gerar_os(catalogo)
            client.publish(
                "apsen/os/nova",
                json.dumps(os_payload, ensure_ascii=False),
                qos=1,
            )
            meds = os_payload["medicamentos"]
            itens_str = " | ".join(m["medicamento"] + " x" + str(m["quantidade"]) for m in meds)
            logger.info("[OS] %s | categoria=%s | %d medicamentos",
                        os_payload["os_id"], os_payload["categoria"], len(meds))
            logger.info("     %s", itens_str)
        except Exception as exc:
            logger.error("Erro ao gerar/publicar OS: %s", exc, exc_info=True)
            if "vazio" in str(exc) or "Catalogo" in str(exc):
                catalogo = {}

        time.sleep(INTERVALO_OS)


if __name__ == "__main__":
    main()
