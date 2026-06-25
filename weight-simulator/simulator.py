"""
APSEN - Weight Simulator v1.0 (HX711)
Simula a célula de carga HX711 instalada na mesa de dispensação.

A célula mede o peso acumulado na mesa após cada dispensa.
Usada para Triple Check: comparar peso medido × peso esperado (qtd × peso_unitario_g).

Fluxo:
  Central → POST /executar/tara         → zera a balança (tara antes da OS)
  Central → POST /executar/pesar        → captura leitura de peso na mesa
  Balança → POST /eventos (adapter)     → envia resultado ao central via weight-adapter

Eventos emitidos:
  tipo: "peso_ok"         — peso dentro da tolerância (±5%)
  tipo: "peso_divergencia" — peso fora da tolerância
  tipo: "tara_ok"         — tara realizada com sucesso
  tipo: "erro_sensor"     — sensor indisponível ou falha de comunicação
  tipo: "telemetria"      — heartbeat periódico com temperatura do sensor

Env vars:
  ADAPTER_URL             → URL do weight-adapter (default: http://weight-adapter:8103)
  TOLERANCIA_PERC         → tolerância de peso em % (default: 5.0)
  PROB_ERRO_SENSOR        → probabilidade de falha do sensor (default: 0.01)
  T_LEITURA               → tempo de estabilização da leitura em segundos (default: 1.5)
  RUIDO_G                 → ruído gaussiano em gramas (default: 2.0)
"""
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WEIGHT-SIM] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ADAPTER_URL      = os.getenv("ADAPTER_URL",      "http://weight-adapter:8103")
TOLERANCIA_PERC  = float(os.getenv("TOLERANCIA_PERC",  "5.0"))
PROB_ERRO_SENSOR = float(os.getenv("PROB_ERRO_SENSOR",  "0.01"))
T_LEITURA        = float(os.getenv("T_LEITURA",         "1.5"))
RUIDO_G          = float(os.getenv("RUIDO_G",           "2.0"))

# ── Estado interno ─────────────────────────────────────────────────────────────

_lock        = threading.Lock()
_peso_tara_g = 0.0   # offset da tara (zera a balança)
_peso_mesa_g = 0.0   # peso acumulado real na mesa (simulado)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evento(payload: dict) -> bool:
    try:
        r = requests.post(ADAPTER_URL + "/eventos", json=payload, timeout=5)
        return r.status_code < 300
    except Exception as exc:
        logger.warning("[HTTP] Falha ao enviar evento: %s", exc)
        return False


# ── FastAPI ────────────────────────────────────────────────────────────────────

app = FastAPI(title="APSEN Weight Simulator v1.0 (HX711)")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Modelos Pydantic ───────────────────────────────────────────────────────────

class TaraReq(BaseModel):
    os_id: str = ""


class PesarReq(BaseModel):
    os_id: str
    slot_id: int
    quantidade_esperada: int
    peso_unitario_g: float  # gramas por unidade (vem do catálogo de medicamentos)


# ── Handlers de hardware (background threads) ──────────────────────────────────

def _do_tara(os_id: str):
    """Executa a tara (zera a balança)."""
    time.sleep(0.5)  # tempo de estabilização

    if random.random() < PROB_ERRO_SENSOR:
        logger.warning("[HX711] Falha de comunicação durante tara.")
        _evento({
            "tipo":    "erro_sensor",
            "os_id":   os_id,
            "descricao": "Falha de comunicação HX711 durante tara",
            "ts":      _ts(),
        })
        return

    with _lock:
        global _peso_tara_g, _peso_mesa_g
        _peso_tara_g = _peso_mesa_g  # offset de tara = peso atual da mesa
        peso_offset = _peso_tara_g

    logger.info("[HX711] Tara realizada. Offset=%.1fg | OS=%s", peso_offset, os_id)
    _evento({
        "tipo":       "tara_ok",
        "os_id":      os_id,
        "peso_tara_g": peso_offset,
        "ts":         _ts(),
    })


def _do_pesar(os_id: str, slot_id: int, quantidade_esperada: int, peso_unitario_g: float):
    """Captura leitura de peso e calcula divergência."""
    time.sleep(T_LEITURA)  # aguarda estabilização do sensor

    if random.random() < PROB_ERRO_SENSOR:
        logger.warning("[HX711] Falha de sensor durante leitura.")
        _evento({
            "tipo":     "erro_sensor",
            "os_id":    os_id,
            "slot_id":  slot_id,
            "descricao": "Falha HX711: timeout ou ruído excessivo na leitura",
            "ts":       _ts(),
        })
        return

    peso_esperado_g = quantidade_esperada * peso_unitario_g

    # Simula o peso real na mesa: adiciona ruído gaussiano ao valor esperado
    with _lock:
        # Atualiza o peso simulado da mesa com os itens dispensados + ruído
        _peso_mesa_g += peso_esperado_g + random.gauss(0, RUIDO_G)
        peso_bruto_g = _peso_mesa_g
        peso_liquido_g = max(0.0, peso_bruto_g - _peso_tara_g)

    # Calcula desvio em relação ao esperado acumulado
    desvio_g   = peso_liquido_g - peso_esperado_g
    desvio_pct = (abs(desvio_g) / peso_esperado_g * 100.0) if peso_esperado_g > 0 else 0.0

    dentro_tolerancia = desvio_pct <= TOLERANCIA_PERC
    tipo = "peso_ok" if dentro_tolerancia else "peso_divergencia"

    logger.info(
        "[HX711] slot=%d | esperado=%.1fg | medido=%.1fg | desvio=%.1f%% | %s",
        slot_id, peso_esperado_g, peso_liquido_g, desvio_pct,
        "OK" if dentro_tolerancia else "DIVERGÊNCIA",
    )

    _evento({
        "tipo":                 tipo,
        "os_id":                os_id,
        "slot_id":              slot_id,
        "quantidade_esperada":  quantidade_esperada,
        "peso_unitario_g":      peso_unitario_g,
        "peso_esperado_g":      round(peso_esperado_g, 2),
        "peso_medido_g":        round(peso_liquido_g, 2),
        "desvio_g":             round(desvio_g, 2),
        "desvio_pct":           round(desvio_pct, 2),
        "tolerancia_pct":       TOLERANCIA_PERC,
        "dentro_tolerancia":    dentro_tolerancia,
        "ts":                   _ts(),
    })


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/ping")
def ping():
    return {"status": "ok", "service": "apsen-weight-simulator"}


@app.post("/executar/tara")
def executar_tara(req: TaraReq):
    """Zera a balança (offset de tara). Deve ser chamado antes de cada OS."""
    logger.info("[CMD] TARA ← OS=%s", req.os_id)
    threading.Thread(
        target=_do_tara,
        args=(req.os_id,),
        daemon=True,
        name="tara",
    ).start()
    return {"ok": True, "msg": "Tara iniciada"}


@app.post("/executar/pesar")
def executar_pesar(req: PesarReq):
    """Captura leitura de peso após dispensa de um slot."""
    if req.slot_id not in range(1, 7):
        raise HTTPException(400, "slot_id deve ser 1-6")
    if req.peso_unitario_g <= 0:
        raise HTTPException(400, "peso_unitario_g deve ser > 0")

    logger.info(
        "[CMD] PESAR slot=%d | qtd=%d | peso_unit=%.1fg | OS=%s",
        req.slot_id, req.quantidade_esperada, req.peso_unitario_g, req.os_id,
    )
    threading.Thread(
        target=_do_pesar,
        args=(req.os_id, req.slot_id, req.quantidade_esperada, req.peso_unitario_g),
        daemon=True,
        name=f"pesar-{req.slot_id}",
    ).start()
    return {"ok": True, "msg": "Pesagem iniciada"}


@app.get("/leitura")
def leitura_atual():
    """Retorna leitura bruta e líquida atuais (para debug)."""
    with _lock:
        bruto  = round(_peso_mesa_g, 2)
        tara   = round(_peso_tara_g, 2)
        liquido = round(max(0.0, _peso_mesa_g - _peso_tara_g), 2)
    return {"peso_bruto_g": bruto, "peso_tara_g": tara, "peso_liquido_g": liquido, "ts": _ts()}


@app.post("/reset")
def reset_estado():
    """Reseta o estado interno da balança (usado entre testes). Não exposto em produção."""
    global _peso_mesa_g, _peso_tara_g
    with _lock:
        _peso_mesa_g = 0.0
        _peso_tara_g = 0.0
    return {"ok": True, "msg": "Balança resetada"}


# ── Telemetria periódica ───────────────────────────────────────────────────────

def _telemetria_loop():
    """Envia heartbeat de temperatura do sensor a cada 60s."""
    while True:
        time.sleep(60)
        temp = round(24.0 + random.uniform(-1.5, 3.0), 1)
        with _lock:
            peso_atual = round(_peso_mesa_g, 2)
        _evento({
            "tipo":            "telemetria",
            "componente":      "hx711_balanca_mesa",
            "temperatura_c":   temp,
            "peso_atual_g":    peso_atual,
            "ts":              _ts(),
        })


threading.Thread(target=_telemetria_loop, daemon=True, name="telemetria-weight").start()

if __name__ == "__main__":
    import uvicorn
    logger.info(
        "Weight Simulator HX711 iniciado | ADAPTER=%s | TOLERANCIA=%.1f%% | "
        "T_LEITURA=%.1fs | PROB_ERRO=%.3f",
        ADAPTER_URL, TOLERANCIA_PERC, T_LEITURA, PROB_ERRO_SENSOR,
    )
    uvicorn.run(app, host="0.0.0.0", port=8203)
