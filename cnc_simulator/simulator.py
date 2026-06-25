"""
APSEN - CNC Simulator v3.0 (HTTP)
Hardware puro: recebe comandos de movimento do cnc-adapter e reporta eventos de volta.
Toda lógica de negócio (rota, sequenciamento, OS) foi movida para o central-computer.

Endpoints (servidor HTTP):
  POST /executar/mover    ← adapter comanda mover para dispenser X
  POST /executar/homing   ← adapter comanda retorno à HOME
  GET  /status            ← estado atual da CNC

Eventos (cliente HTTP via requests.post → adapter):
  tipo: "movendo"      → posição atual durante movimento
  tipo: "posicionado"  → chegou ao destino
  tipo: "retornando"   → em movimento para HOME
  tipo: "concluido"    → homing finalizado
  tipo: "erro"         → falha de movimento
  tipo: "telemetria"   → leituras de sensores
"""
import logging
import math
import os
import random
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CNC-SIM] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ADAPTER_URL     = os.getenv("ADAPTER_URL",    "http://cnc-adapter:8101")
VELOCIDADE_MM_S = float(os.getenv("VEL_MM_S",   "80"))   # mm/s
INTERVALO_PUB   = float(os.getenv("INTERVALO",  "0.5"))  # s entre publicações de posição

# Posições XY (mm) dos dispensers na mesa
POSICOES: dict[int, tuple[float, float]] = {
    1: (0.0,   0.0),
    2: (120.0, 0.0),
    3: (240.0, 0.0),
    4: (360.0, 0.0),
    5: (480.0, 0.0),
    6: (600.0, 0.0),
}
HOME: tuple[float, float] = (0.0, -50.0)

COMPONENTES_TEMP = [
    ("motor_eixo_x", 35, 55),
    ("motor_eixo_y", 33, 50),
    ("driver_x",     40, 70),
    ("driver_y",     38, 68),
    ("placa_cnc",    45, 65),
]
COMPONENTES_USO = [
    ("correia_eixo_x",    "desgaste"),
    ("fuso_eixo_y",       "desgaste"),
    ("rolamento_motor_x", "desgaste"),
    ("rolamento_motor_y", "desgaste"),
]


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evento(payload: dict) -> bool:
    """Envia evento ao cnc-adapter. Não lança exceção."""
    try:
        r = requests.post(ADAPTER_URL + "/eventos", json=payload, timeout=5)
        return r.status_code < 300
    except Exception as exc:
        logger.warning("[HTTP] Falha ao enviar evento: %s", exc)
        return False


# ── Estado da CNC ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_em_movimento = threading.Event()  # set=True enquanto CNC está em movimento
_movimento_lock = threading.Lock()  # protege check-and-set atômico de _em_movimento

_cnc_state = {
    "status":         "idle",
    "os_id":          None,
    "dispenser_alvo": None,
    "pos_x":          HOME[0],
    "pos_y":          HOME[1],
    "ciclo_atual":    0,
    "total_ciclos":   0,
    "horas_uso":      random.uniform(120, 800),
    "ciclos_total":   random.randint(5000, 50000),
}


# ── FastAPI App ────────────────────────────────────────────────────────────────
app = FastAPI(title="APSEN CNC Simulator v3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


class MoverReq(BaseModel):
    dispenser_alvo: int
    os_id: str
    posicao_x: float
    posicao_y: float
    ciclo_atual: int = 0
    total_ciclos: int = 0


class HomingReq(BaseModel):
    os_id: str


# ── Lógica de movimento ────────────────────────────────────────────────────────

def _mover_para(disp_id: int, alvo_x: float, alvo_y: float,
                os_id: str, ciclo: int, total: int):
    """Interpola posição até o destino, publicando cada passo."""
    with _lock:
        orig_x = _cnc_state["pos_x"]
        orig_y = _cnc_state["pos_y"]
        _cnc_state.update({
            "status":         "movendo",
            "os_id":          os_id,
            "dispenser_alvo": disp_id,
            "ciclo_atual":    ciclo,
            "total_ciclos":   total,
        })

    distancia = math.hypot(alvo_x - orig_x, alvo_y - orig_y)
    if distancia < 0.1:
        # Já está posicionado
        with _lock:
            _cnc_state["status"] = "posicionado"
        _evento({
            "tipo":           "posicionado",
            "os_id":          os_id,
            "dispenser_alvo": disp_id,
            "posicao_x":      round(alvo_x, 2),
            "posicao_y":      round(alvo_y, 2),
            "ciclo_atual":    ciclo,
            "total_ciclos":   total,
            "ts":             _ts(),
        })
        return

    duracao = max(1.0, distancia / VELOCIDADE_MM_S)
    passos  = max(3, int(duracao / INTERVALO_PUB))

    logger.info("[CNC] Movendo de (%.1f, %.1f) → (%.1f, %.1f) | %.0fmm | ~%.1fs | %d passos",
                orig_x, orig_y, alvo_x, alvo_y, distancia, duracao, passos)

    for i in range(1, passos + 1):
        t     = i / passos
        cur_x = orig_x + (alvo_x - orig_x) * t
        cur_y = orig_y + (alvo_y - orig_y) * t

        with _lock:
            _cnc_state["pos_x"] = cur_x
            _cnc_state["pos_y"] = cur_y

        _evento({
            "tipo":           "movendo",
            "os_id":          os_id,
            "dispenser_alvo": disp_id,
            "posicao_x":      round(cur_x, 2),
            "posicao_y":      round(cur_y, 2),
            "ciclo_atual":    ciclo,
            "total_ciclos":   total,
            "passo":          i,
            "total_passos":   passos,
            "progresso_pct":  round(t * 100, 1),
            "ts":             _ts(),
        })
        time.sleep(INTERVALO_PUB)

    with _lock:
        _cnc_state.update({
            "pos_x":  alvo_x,
            "pos_y":  alvo_y,
            "status": "posicionado",
        })

    logger.info("[CNC] POSICIONADA em D%d (%.1f, %.1f).", disp_id, alvo_x, alvo_y)
    _evento({
        "tipo":           "posicionado",
        "os_id":          os_id,
        "dispenser_alvo": disp_id,
        "posicao_x":      round(alvo_x, 2),
        "posicao_y":      round(alvo_y, 2),
        "ciclo_atual":    ciclo,
        "total_ciclos":   total,
        "ts":             _ts(),
    })


def _homing(os_id: str):
    """Move a CNC de volta para HOME e reporta conclusão."""
    with _lock:
        orig_x = _cnc_state["pos_x"]
        orig_y = _cnc_state["pos_y"]
        _cnc_state.update({
            "status":         "retornando",
            "dispenser_alvo": None,
        })

    distancia = math.hypot(HOME[0] - orig_x, HOME[1] - orig_y)
    duracao   = max(1.0, distancia / VELOCIDADE_MM_S)
    passos    = max(3, int(duracao / INTERVALO_PUB))

    logger.info("[CNC] HOMING: %.0fmm | ~%.1fs", distancia, duracao)

    for i in range(1, passos + 1):
        t     = i / passos
        cur_x = orig_x + (HOME[0] - orig_x) * t
        cur_y = orig_y + (HOME[1] - orig_y) * t

        with _lock:
            _cnc_state["pos_x"] = cur_x
            _cnc_state["pos_y"] = cur_y

        _evento({
            "tipo":      "retornando",
            "os_id":     os_id,
            "posicao_x": round(cur_x, 2),
            "posicao_y": round(cur_y, 2),
            "ts":        _ts(),
        })
        time.sleep(INTERVALO_PUB)

    with _lock:
        _cnc_state.update({
            "pos_x":       HOME[0],
            "pos_y":       HOME[1],
            "status":      "idle",
            "os_id":       None,
            "ciclo_atual": 0,
            "total_ciclos": 0,
            "ciclos_total": _cnc_state["ciclos_total"] + _cnc_state.get("total_ciclos", 0),
            "horas_uso":    _cnc_state["horas_uso"] + _cnc_state.get("total_ciclos", 0) * 0.01,
        })

    logger.info("[CNC] HOME atingida. OS %s concluída.", os_id)
    _evento({
        "tipo":      "concluido",
        "os_id":     os_id,
        "posicao_x": HOME[0],
        "posicao_y": HOME[1],
        "ts":        _ts(),
    })
    _em_movimento.clear()


def _thread_mover(disp_id: int, alvo_x: float, alvo_y: float,
                  os_id: str, ciclo: int, total: int):
    try:
        _mover_para(disp_id, alvo_x, alvo_y, os_id, ciclo, total)
    except Exception as exc:
        logger.error("[CNC] Erro em movimento: %s", exc, exc_info=True)
        with _lock:
            _cnc_state["status"] = "erro"
        _evento({
            "tipo":           "erro",
            "os_id":          os_id,
            "dispenser_alvo": disp_id,
            "codigo_erro":    "erro_movimento",
            "descricao":      str(exc),
            "ts":             _ts(),
        })
    finally:
        _em_movimento.clear()


def _thread_homing(os_id: str):
    try:
        _homing(os_id)
    except Exception as exc:
        logger.error("[CNC] Erro em homing: %s", exc, exc_info=True)
        with _lock:
            _cnc_state["status"] = "erro"
        _evento({
            "tipo":        "erro",
            "os_id":       os_id,
            "codigo_erro": "erro_homing",
            "descricao":   str(exc),
            "ts":          _ts(),
        })
        _em_movimento.clear()


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/ping")
def ping():
    return {"status": "ok", "service": "apsen-cnc-simulator"}


@app.get("/status")
def status():
    with _lock:
        return dict(_cnc_state) | {"ts": _ts()}


@app.post("/executar/mover")
def executar_mover(req: MoverReq):
    if req.dispenser_alvo not in POSICOES:
        raise HTTPException(400, f"dispenser_alvo deve ser 1-6, recebido: {req.dispenser_alvo}")

    # Verifica e seta _em_movimento atomicamente para evitar race condition
    with _movimento_lock:
        if _em_movimento.is_set():
            raise HTTPException(409, "CNC em movimento — aguarde posicionamento atual")
        _em_movimento.set()

    threading.Thread(
        target=_thread_mover,
        args=(req.dispenser_alvo, req.posicao_x, req.posicao_y,
              req.os_id, req.ciclo_atual, req.total_ciclos),
        daemon=True,
        name=f"cnc-mover-D{req.dispenser_alvo}",
    ).start()

    return {
        "ok": True,
        "msg": f"Movendo para D{req.dispenser_alvo}",
        "destino": {"x": req.posicao_x, "y": req.posicao_y},
    }


@app.post("/executar/homing")
def executar_homing(req: HomingReq):
    # Verifica e seta _em_movimento atomicamente para evitar race condition
    with _movimento_lock:
        if _em_movimento.is_set():
            raise HTTPException(409, "CNC em movimento — aguarde conclusão")
        _em_movimento.set()

    threading.Thread(
        target=_thread_homing,
        args=(req.os_id,),
        daemon=True,
        name="cnc-homing",
    ).start()

    return {"ok": True, "msg": "Homing iniciado", "home": {"x": HOME[0], "y": HOME[1]}}


# ── Telemetria periódica ───────────────────────────────────────────────────────

def _telemetria_loop():
    while True:
        time.sleep(30)
        with _lock:
            em_uso = _cnc_state["status"] not in ("idle",)
            horas  = _cnc_state["horas_uso"]
            ciclos = _cnc_state["ciclos_total"]

        ts = _ts()

        for comp, t_min, t_max in COMPONENTES_TEMP:
            base  = t_min + (t_max - t_min) * (0.75 if em_uso else 0.2)
            valor = round(base + random.uniform(-1.5, 1.5), 1)
            _evento({
                "tipo":         "telemetria",
                "componente":   comp,
                "tipo_leitura": "temperatura",
                "valor":        valor,
                "unidade":      "°C",
                "ts":           ts,
            })

        for comp, tipo in COMPONENTES_USO:
            _evento({
                "tipo":         "telemetria",
                "componente":   comp,
                "tipo_leitura": tipo,
                "valor":        round(min(100.0, horas / 10.0), 1),
                "unidade":      "%",
                "ts":           ts,
            })

        _evento({
            "tipo":         "telemetria",
            "componente":   "cnc_geral",
            "tipo_leitura": "horas_uso",
            "valor":        round(horas, 1),
            "unidade":      "h",
            "ts":           ts,
        })
        _evento({
            "tipo":         "telemetria",
            "componente":   "cnc_geral",
            "tipo_leitura": "ciclos",
            "valor":        ciclos,
            "unidade":      "ciclos",
            "ts":           ts,
        })


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    threading.Thread(target=_telemetria_loop, daemon=True, name="telemetria").start()
    logger.info(
        "CNC Simulator v3.0 | adapter=%s | vel=%.0fmm/s | HOME=(%.0f, %.0f)",
        ADAPTER_URL, VELOCIDADE_MM_S, HOME[0], HOME[1],
    )
    uvicorn.run(app, host="0.0.0.0", port=8200, log_level="warning")
