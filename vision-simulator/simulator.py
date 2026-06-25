"""
APSEN - Vision Simulator v1.0
Simula duas câmeras independentes da célula automatizada.

CÂMERA DOS DISPENSERS (cam_id="dispenser")
  Posicionada sobre os dispensers. Responsável por:
  - Leitura de QR Code, DataMatrix e código de barras
  - Identificação e confirmação do produto carregado em cada slot
  - Detecção de embalagem danificada ou produto incorreto

  Eventos gerados:
    leitura_dispenser_ok   → SKU confirmado, produto correto
    leitura_dispenser_falha → falha de leitura (câmera, iluminação, código sujo)
    leitura_dispenser_divergencia → leitura ok mas SKU não bate com esperado

CÂMERA DA MESA (cam_id="mesa")
  Posicionada sobre a área de coleta. Responsável por:
  - Localização dos produtos dispensados
  - Contagem visual das unidades na mesa
  - Validação de posição (produto caiu fora da zona de coleta?)

  Eventos gerados:
    leitura_mesa_ok         → produtos detectados, contagem correta
    leitura_mesa_falha      → câmera não detectou nada
    leitura_mesa_divergencia → contagem detectada difere da esperada

Todas as operações de captura rodam em background threads e enviam
resultado via POST /eventos → vision-adapter → central-computer.

Probabilidades controladas por variáveis de ambiente:
  PROB_FALHA_LEITURA_DISPENSER  0.02  câmera do dispenser não consegue ler
  PROB_DIVERGENCIA_DISPENSER    0.02  leu mas SKU errado (produto trocado)
  PROB_FALHA_LEITURA_MESA       0.02  câmera da mesa não detecta produto
  PROB_DIVERGENCIA_MESA         0.02  detecta produto mas contagem errada
  T_SCAN_DISPENSER              1.5   segundos para processar scan do dispenser
  T_SCAN_MESA                   2.0   segundos para processar scan da mesa
"""
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [VISION-SIM] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ADAPTER_URL = os.getenv("ADAPTER_URL", "http://vision-adapter:8102")

# Probabilidades de falha (controláveis por env var para testes)
PROB_FALHA_LEITURA_DISPENSER = float(os.getenv("PROB_FALHA_LEITURA_DISPENSER", "0.02"))
PROB_DIVERGENCIA_DISPENSER   = float(os.getenv("PROB_DIVERGENCIA_DISPENSER",   "0.02"))
PROB_FALHA_LEITURA_MESA      = float(os.getenv("PROB_FALHA_LEITURA_MESA",      "0.02"))
PROB_DIVERGENCIA_MESA        = float(os.getenv("PROB_DIVERGENCIA_MESA",        "0.02"))

# Tempos de processamento simulados
T_SCAN_DISPENSER = float(os.getenv("T_SCAN_DISPENSER", "1.5"))
T_SCAN_MESA      = float(os.getenv("T_SCAN_MESA",      "2.0"))


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evento(payload: dict) -> bool:
    """Envia evento ao vision-adapter. Não lança exceção."""
    try:
        r = requests.post(ADAPTER_URL + "/eventos", json=payload, timeout=5)
        return r.status_code < 300
    except Exception as exc:
        logger.warning("[HTTP] Falha ao enviar evento: %s", exc)
        return False


# ── FastAPI App ────────────────────────────────────────────────────────────────

app = FastAPI(title="APSEN Vision Simulator v1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Pydantic Models ────────────────────────────────────────────────────────────

class CapturarDispenserReq(BaseModel):
    slot_id: int
    os_id: str
    sku_esperado: str = ""
    medicamento_esperado: str = ""
    quantidade_esperada: int = 0


class CapturarMesaReq(BaseModel):
    slot_id: int
    os_id: str
    quantidade_esperada: int = 0
    posicao_x: float = 0.0
    posicao_y: float = 0.0


# ── Câmera dos Dispensers ─────────────────────────────────────────────────────

def _do_capturar_dispenser(slot_id: int, os_id: str, sku_esperado: str,
                            medicamento_esperado: str, quantidade_esperada: int):
    """
    Simula o processamento da câmera do dispenser:
    1. Delay de processamento (iluminação + leitura óptica)
    2. Decide se a leitura falha, acerta ou diverge
    3. Envia evento ao adapter
    """
    logger.info("[CAM-DISP] Iniciando scan slot=%d | sku=%s", slot_id, sku_esperado)
    time.sleep(T_SCAN_DISPENSER)

    ts = _ts()

    # Caso 1: falha de hardware (câmera, iluminação, código sujo/danificado)
    if random.random() < PROB_FALHA_LEITURA_DISPENSER:
        motivo = random.choice([
            "camera_obstruida",
            "iluminacao_insuficiente",
            "codigo_danificado",
            "timeout_leitura",
        ])
        logger.warning("[CAM-DISP] FALHA leitura slot=%d: %s", slot_id, motivo)
        _evento({
            "tipo":          "leitura_dispenser_falha",
            "camera":        "dispenser",
            "slot_id":       slot_id,
            "os_id":         os_id,
            "motivo":        motivo,
            "sku_esperado":  sku_esperado,
            "confianca":     0.0,
            "ts":            ts,
        })
        return

    # Caso 2: leitura bem-sucedida mas SKU divergente (produto errado)
    if random.random() < PROB_DIVERGENCIA_DISPENSER:
        sku_simulado = f"APSEN-ERRADO-{random.randint(100,999)}"
        logger.error(
            "[CAM-DISP] DIVERGÊNCIA slot=%d | esperado=%s | lido=%s",
            slot_id, sku_esperado, sku_simulado,
        )
        _evento({
            "tipo":             "leitura_dispenser_divergencia",
            "camera":           "dispenser",
            "slot_id":          slot_id,
            "os_id":            os_id,
            "sku_esperado":     sku_esperado,
            "sku_lido":         sku_simulado,
            "medicamento_lido": f"Medicamento desconhecido ({sku_simulado})",
            "match_sku":        False,
            "confianca":        round(random.uniform(0.88, 0.96), 3),
            "ts":               ts,
        })
        return

    # Caso 3: leitura bem-sucedida e SKU correto (happy path — 96%+ dos casos)
    confianca = round(random.uniform(0.93, 0.999), 3)
    logger.info(
        "[CAM-DISP] OK slot=%d | sku=%s | confiança=%.1f%%",
        slot_id, sku_esperado, confianca * 100,
    )
    _evento({
        "tipo":              "leitura_dispenser_ok",
        "camera":            "dispenser",
        "slot_id":           slot_id,
        "os_id":             os_id,
        "sku_esperado":      sku_esperado,
        "sku_lido":          sku_esperado,
        "medicamento_lido":  medicamento_esperado,
        "match_sku":         True,
        "confianca":         confianca,
        "ts":                ts,
    })


# ── Câmera da Mesa ─────────────────────────────────────────────────────────────

def _do_capturar_mesa(slot_id: int, os_id: str, quantidade_esperada: int,
                       posicao_x: float, posicao_y: float):
    """
    Simula o processamento da câmera da mesa:
    1. Delay de processamento (análise de imagem, contagem)
    2. Decide se não detecta nada, conta corretamente ou diverge na contagem
    3. Envia evento ao adapter
    """
    logger.info(
        "[CAM-MESA] Iniciando scan slot=%d | qtd_esp=%d | pos=(%.1f,%.1f)",
        slot_id, quantidade_esperada, posicao_x, posicao_y,
    )
    time.sleep(T_SCAN_MESA)

    ts = _ts()

    # Caso 1: câmera não detecta produto (produto fora da zona, obstrução)
    if random.random() < PROB_FALHA_LEITURA_MESA:
        motivo = random.choice([
            "produto_fora_zona_coleta",
            "obstrucao_visual",
            "camera_desalinhada",
            "produto_nao_detectado",
        ])
        logger.warning("[CAM-MESA] FALHA leitura slot=%d: %s", slot_id, motivo)
        _evento({
            "tipo":               "leitura_mesa_falha",
            "camera":             "mesa",
            "slot_id":            slot_id,
            "os_id":              os_id,
            "motivo":             motivo,
            "quantidade_esperada": quantidade_esperada,
            "quantidade_detectada": 0,
            "posicao_x":          posicao_x,
            "posicao_y":          posicao_y,
            "confianca":          0.0,
            "ts":                 ts,
        })
        return

    # Caso 2: detecta produto mas conta errado (divergência de quantidade)
    if random.random() < PROB_DIVERGENCIA_MESA:
        # Simula contagem levemente errada (±1 a ±2 unidades)
        delta    = random.choice([-2, -1, 1, 2])
        detectado = max(0, quantidade_esperada + delta)
        logger.warning(
            "[CAM-MESA] DIVERGÊNCIA slot=%d | esperado=%d | detectado=%d",
            slot_id, quantidade_esperada, detectado,
        )
        _evento({
            "tipo":                "leitura_mesa_divergencia",
            "camera":              "mesa",
            "slot_id":             slot_id,
            "os_id":               os_id,
            "quantidade_esperada": quantidade_esperada,
            "quantidade_detectada": detectado,
            "delta":               detectado - quantidade_esperada,
            "posicao_x_detectada": round(posicao_x + random.uniform(-2.0, 2.0), 2),
            "posicao_y_detectada": round(posicao_y + random.uniform(-2.0, 2.0), 2),
            "confianca":           round(random.uniform(0.80, 0.92), 3),
            "ts":                  ts,
        })
        return

    # Caso 3: detecção bem-sucedida, contagem correta (happy path)
    confianca = round(random.uniform(0.91, 0.999), 3)
    logger.info(
        "[CAM-MESA] OK slot=%d | detectado=%d/%d | confiança=%.1f%%",
        slot_id, quantidade_esperada, quantidade_esperada, confianca * 100,
    )
    _evento({
        "tipo":                "leitura_mesa_ok",
        "camera":              "mesa",
        "slot_id":             slot_id,
        "os_id":               os_id,
        "quantidade_esperada": quantidade_esperada,
        "quantidade_detectada": quantidade_esperada,
        "delta":               0,
        "posicao_x_detectada": round(posicao_x + random.uniform(-1.0, 1.0), 2),
        "posicao_y_detectada": round(posicao_y + random.uniform(-1.0, 1.0), 2),
        "confianca":           confianca,
        "ts":                  ts,
    })


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/ping")
def ping():
    return {"status": "ok", "service": "apsen-vision-simulator"}


@app.get("/status")
def status():
    return {
        "service":    "vision-simulator",
        "cameras":    ["dispenser", "mesa"],
        "prob_falha_dispenser": PROB_FALHA_LEITURA_DISPENSER,
        "prob_div_dispenser":   PROB_DIVERGENCIA_DISPENSER,
        "prob_falha_mesa":      PROB_FALHA_LEITURA_MESA,
        "prob_div_mesa":        PROB_DIVERGENCIA_MESA,
        "t_scan_dispenser":     T_SCAN_DISPENSER,
        "t_scan_mesa":          T_SCAN_MESA,
        "ts":                   _ts(),
    }


@app.post("/executar/capturar/dispenser")
def executar_capturar_dispenser(req: CapturarDispenserReq):
    """
    Inicia captura assíncrona da câmera do dispenser.
    O resultado é enviado via POST /eventos → adapter → central.
    """
    if req.slot_id not in range(1, 7):
        from fastapi import HTTPException
        raise HTTPException(400, "slot_id deve ser 1-6")

    threading.Thread(
        target=_do_capturar_dispenser,
        args=(req.slot_id, req.os_id, req.sku_esperado,
              req.medicamento_esperado, req.quantidade_esperada),
        daemon=True,
        name=f"vision-disp-{req.slot_id}",
    ).start()

    logger.info("[CAM-DISP] Captura iniciada slot=%d OS=%s", req.slot_id, req.os_id)
    return {
        "ok":      True,
        "camera":  "dispenser",
        "slot_id": req.slot_id,
        "msg":     f"Processando scan do dispenser {req.slot_id}",
    }


@app.post("/executar/capturar/mesa")
def executar_capturar_mesa(req: CapturarMesaReq):
    """
    Inicia captura assíncrona da câmera da mesa.
    O resultado é enviado via POST /eventos → adapter → central.
    """
    if req.slot_id not in range(1, 7):
        from fastapi import HTTPException
        raise HTTPException(400, "slot_id deve ser 1-6")

    threading.Thread(
        target=_do_capturar_mesa,
        args=(req.slot_id, req.os_id, req.quantidade_esperada,
              req.posicao_x, req.posicao_y),
        daemon=True,
        name=f"vision-mesa-{req.slot_id}",
    ).start()

    logger.info("[CAM-MESA] Captura iniciada slot=%d OS=%s", req.slot_id, req.os_id)
    return {
        "ok":      True,
        "camera":  "mesa",
        "slot_id": req.slot_id,
        "msg":     f"Processando scan da mesa (slot {req.slot_id})",
    }


# ── Telemetria periódica ───────────────────────────────────────────────────────

def _telemetria_loop():
    """Envia leituras de temperatura dos componentes de câmera a cada 60s."""
    import time as _time
    cameras = [
        ("camera_dispenser", 35, 50),
        ("camera_mesa",      33, 48),
        ("processador_visao", 45, 70),
    ]
    while True:
        _time.sleep(60)
        ts = _ts()
        for comp, t_min, t_max in cameras:
            valor = round(t_min + random.uniform(0, t_max - t_min) * 0.6, 1)
            _evento({
                "tipo":          "telemetria",
                "camera":        "sistema",
                "componente":    comp,
                "tipo_leitura":  "temperatura",
                "valor":         valor,
                "unidade":       "°C",
                "ts":            ts,
            })


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    threading.Thread(target=_telemetria_loop, daemon=True, name="telemetria").start()

    logger.info(
        "Vision Simulator v1.0 | adapter=%s\n"
        "  CAM_DISP: falha=%.0f%% div=%.0f%% scan=%.1fs\n"
        "  CAM_MESA: falha=%.0f%% div=%.0f%% scan=%.1fs",
        ADAPTER_URL,
        PROB_FALHA_LEITURA_DISPENSER * 100, PROB_DIVERGENCIA_DISPENSER * 100, T_SCAN_DISPENSER,
        PROB_FALHA_LEITURA_MESA * 100, PROB_DIVERGENCIA_MESA * 100, T_SCAN_MESA,
    )
    uvicorn.run(app, host="0.0.0.0", port=8202, log_level="warning")
