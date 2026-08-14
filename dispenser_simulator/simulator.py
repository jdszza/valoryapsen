"""
APSEN - Dispenser Simulator v3.0 (HTTP)
Hardware puro: recebe comandos do dispenser-adapter e reporta eventos de volta.
Toda lógica de negócio (fila, IA, roteamento) foi movida para o central-computer.

Endpoints (servidor HTTP):
  POST /executar/carregar  ← adapter comanda carregar slot
  POST /executar/dispensar ← adapter comanda dispensar (CNC já posicionada)
  POST /executar/limpar    ← adapter comanda limpeza de slot
  GET  /status             ← estado completo de todos os slots

Eventos (cliente HTTP via requests.post → adapter):
  tipo: "carregado"   → slot pronto para dispensar
  tipo: "dispensado"  → dispensa concluída
  tipo: "limpeza_ok"  → slot limpo
  tipo: "erro"        → qualquer falha
  tipo: "telemetria"  → temperatura periódica
  tipo: "status"      → snapshot periódico do slot; para o central vale só a
                        parte de estoque (medicamento/sku/categoria/quantidade).
                        Os campos de fluxo (status, os_id) do snapshot são
                        informativos: quem manda no fluxo é o orquestrador.
"""
import logging
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
    format="%(asctime)s [DISP-SIM] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ADAPTER_URL     = os.getenv("ADAPTER_URL",     "http://dispenser-adapter:8100")
T_CARGA_UNID    = float(os.getenv("T_CARGA_UNID",   "0.3"))
T_DISPENSA_UNID = float(os.getenv("T_DISPENSA_UNID", "0.5"))
PROB_ERRO_MECANICO = float(os.getenv("PROB_ERRO_MECANICO", "0.01"))  # falha mecânica (atolamento, etc)
NUM_SLOTS       = 6
TEMP_BASE       = {d: 22 + d * 0.5 for d in range(1, NUM_SLOTS + 1)}


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evento(payload: dict) -> bool:
    """Envia evento ao dispenser-adapter. Não lança exceção (fire-and-forget)."""
    try:
        r = requests.post(ADAPTER_URL + "/eventos", json=payload, timeout=5)
        return r.status_code < 300
    except Exception as exc:
        logger.warning("[HTTP] Falha ao enviar evento: %s", exc)
        return False


# ── Estado compartilhado ───────────────────────────────────────────────────────
_lock = threading.Lock()

# Per-slot locks: protegem check-and-set atômico de status em cada slot.
# Evitam race condition de dois POSTs simultâneos para o mesmo slot.
_slot_locks: dict[int, threading.Lock] = {d: threading.Lock() for d in range(1, NUM_SLOTS + 1)}

_estoque: dict = {
    d: {"medicamento": None, "sku": None, "categoria": None, "quantidade": 0}
    for d in range(1, NUM_SLOTS + 1)
}
_estado: dict = {
    d: {"status": "idle", "os_id": None, "qtd_alvo": 0, "qtd_dispensada": 0}
    for d in range(1, NUM_SLOTS + 1)
}


def _snapshot_slot(slot_id: int) -> dict:
    with _lock:
        est = dict(_estoque[slot_id])
        sts = dict(_estado[slot_id])
    return {
        "dispenser_id":   slot_id,
        "medicamento":    est["medicamento"],
        "sku":            est["sku"],
        "categoria":      est["categoria"],
        "quantidade":     est["quantidade"],
        "status":         sts["status"],
        "os_id":          sts["os_id"],
        "qtd_alvo":       sts["qtd_alvo"],
        "qtd_dispensada": sts["qtd_dispensada"],
        "ts":             _ts(),
    }


# ── FastAPI App ────────────────────────────────────────────────────────────────
app = FastAPI(title="APSEN Dispenser Simulator v3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


class CarregarReq(BaseModel):
    dispenser_id: int
    medicamento: str
    sku: str = ""
    categoria: str = ""
    quantidade: int
    os_id: str


class DispensarReq(BaseModel):
    dispenser_id: int
    os_id: str


class LimparReq(BaseModel):
    dispenser_id: int
    solicitado_por: str = "sistema"


# ── Handlers de hardware ───────────────────────────────────────────────────────

def _do_carregar(slot_id: int, medicamento: str, sku: str, categoria: str,
                 quantidade: int, os_id: str):
    """Executa fase de carregamento em background thread."""
    with _lock:
        estoque_atual = _estoque[slot_id]["quantidade"]
        # Atualiza medicamento no slot se vazio
        if _estoque[slot_id]["medicamento"] is None or estoque_atual == 0:
            _estoque[slot_id].update({
                "medicamento": medicamento,
                "sku":         sku,
                "categoria":   categoria,
            })

    to_load = max(0, quantidade - estoque_atual)

    if estoque_atual >= quantidade:
        # Residual suficiente — não precisa carregar
        logger.info("[DISP-%d] Residual suficiente: %d >= %d.", slot_id, estoque_atual, quantidade)
        with _lock:
            _estado[slot_id].update({
                "status":   "pronto",
                "os_id":    os_id,
                "qtd_alvo": quantidade,
            })
        _evento({
            "tipo":             "carregado",
            "dispenser_id":     slot_id,
            "os_id":            os_id,
            "medicamento":      medicamento,
            "sku":              sku,
            "categoria":        categoria,
            "quantidade_total": estoque_atual,
            "quantidade_residual": estoque_atual,
            "via_residual":     True,
            "ts":               _ts(),
        })
        return

    # Simula carregamento físico unidade por unidade
    logger.info("[DISP-%d] Carregando %d un. de '%s' | residual=%d",
                slot_id, to_load, medicamento, estoque_atual)

    with _lock:
        _estado[slot_id].update({
            "status":         "carregando",
            "os_id":          os_id,
            "qtd_alvo":       quantidade,
            "qtd_dispensada": 0,
        })

    for i in range(1, to_load + 1):
        time.sleep(T_CARGA_UNID)
        # Falha mecânica no carregamento (atolamento, sensor de posição)
        # Validação de qualidade do produto (SKU, integridade) é responsabilidade do vision-adapter
        if random.random() < PROB_ERRO_MECANICO:
            motivo = random.choice([
                f"Atolamento mecânico (un. {i})",
                f"Sensor de posição falhou (un. {i})",
                f"Unidade {i} não detectada pelo sensor",
            ])
            logger.error("[DISP-%d] ERRO mecânico carregamento un. %d: %s", slot_id, i, motivo)
            # Aborta a carga: nada mais se move neste slot. Solta o os_id junto com
            # o status para o slot não ficar preso a uma OS que já morreu — o
            # payload do evento abaixo usa o parâmetro `os_id`, não o estado.
            with _lock:
                _estado[slot_id].update({"status": "erro", "os_id": None})
            _evento({
                "tipo":         "erro",
                "dispenser_id": slot_id,
                "os_id":        os_id,
                "codigo_erro":  "erro_mecanico_carga",
                "descricao":    motivo,
                "ts":           _ts(),
            })
            return

    with _lock:
        total_final = estoque_atual + to_load
        _estoque[slot_id]["quantidade"] = total_final
        _estado[slot_id].update({"status": "pronto"})

    logger.info("[DISP-%d] PRONTO (estoque=%d).", slot_id, total_final)
    _evento({
        "tipo":               "carregado",
        "dispenser_id":       slot_id,
        "os_id":              os_id,
        "medicamento":        medicamento,
        "sku":                sku,
        "categoria":          categoria,
        "quantidade_total":   total_final,
        "quantidade_residual": total_final,
        "ts":                 _ts(),
    })


def _do_dispensar(slot_id: int, os_id: str):
    """Executa fase de dispensa em background thread. CNC já está posicionada."""
    with _lock:
        med       = _estoque[slot_id]["medicamento"] or ""
        quantidade = _estado[slot_id]["qtd_alvo"]

    logger.info("[DISP-%d] DISPENSANDO %d × '%s'", slot_id, quantidade, med)

    with _lock:
        _estado[slot_id]["status"] = "dispensando"

    dispensado = 0
    for i in range(1, quantidade + 1):
        time.sleep(T_DISPENSA_UNID)
        # Hardware puro: falha mecânica (atolamento, sensor de posição, etc)
        # Validação de qualidade (SKU, contagem) é responsabilidade do vision-adapter
        if random.random() < PROB_ERRO_MECANICO:
            logger.warning("[DISP-%d] Falha mecânica un. %d (atolamento/sensor)", slot_id, i)
        else:
            dispensado += 1

        with _lock:
            _estado[slot_id]["qtd_dispensada"] = dispensado

    with _lock:
        residual_final = max(0, _estoque[slot_id]["quantidade"] - quantidade)
        _estoque[slot_id]["quantidade"] = residual_final
        if residual_final == 0:
            _estoque[slot_id]["medicamento"] = None
            _estoque[slot_id]["sku"]         = None
            _estoque[slot_id]["categoria"]   = None
        # Estado terminal limpo: a dispensa acabou e o slot não pertence mais à OS.
        # Sem soltar o os_id aqui, _do_limpar() nunca mais aceitaria limpar o slot
        # (era ele o único caminho que zerava o campo — impasse permanente).
        # O os_id do evento vem do parâmetro da função, capturado na chamada, então
        # o payload abaixo continua carregando a OS correta.
        _estado[slot_id].update({
            "status":         "idle",
            "os_id":          None,
            "qtd_dispensada": dispensado,
        })

    logger.info("[DISP-%d] DISPENSADO: %d/%d × '%s' | RESIDUAL=%d",
                slot_id, dispensado, quantidade, med, residual_final)

    _evento({
        "tipo":                   "dispensado",
        "dispenser_id":           slot_id,
        "os_id":                  os_id,
        "medicamento":            med,
        "quantidade_dispensada":  dispensado,
        "quantidade_alvo":        quantidade,
        "falha_mecanica":         dispensado < quantidade,
        "motivo_falha":           None if dispensado >= quantidade else "falha_mecanica",
        "quantidade_residual":    residual_final,
        "ts":                     _ts(),
    })


def _do_limpar(slot_id: int, solicitado_por: str):
    """Executa limpeza física do slot."""
    with _lock:
        status_atual = _estado[slot_id]["status"]
        os_id_atual  = _estado[slot_id].get("os_id")
        # Bloqueia só o que tem peça se mexendo. "pronto", "concluido" e "erro" são
        # estados parados: limpar um slot com estoque encalhado é exatamente o
        # propósito do botão da IHM.
        em_operacao  = status_atual in ("carregando", "dispensando")

    if em_operacao:
        logger.warning("[DISP-%d] Limpeza recusada — em operação (status=%s).", slot_id, status_atual)
        _evento({
            "tipo":         "erro",
            "dispenser_id": slot_id,
            "os_id":        os_id_atual,   # lido dentro do lock, junto com o status
            "codigo_erro":  "limpeza_em_operacao",
            "descricao":    f"Dispenser {slot_id} em operação (status: {status_atual})",
            "ts":           _ts(),
        })
        return

    with _lock:
        med_anterior = _estoque[slot_id]["medicamento"]
        _estoque[slot_id] = {"medicamento": None, "sku": None, "categoria": None, "quantidade": 0}
        _estado[slot_id].update({"status": "limpo", "qtd_alvo": 0, "qtd_dispensada": 0, "os_id": None})

    logger.info("[DISP-%d] LIMPO (anterior: %s) | por: %s",
                slot_id, med_anterior or "vazio", solicitado_por)

    _evento({
        "tipo":              "limpeza_ok",
        "dispenser_id":      slot_id,
        "medicamento_limpo": med_anterior,
        "solicitado_por":    solicitado_por,
        "ts":                _ts(),
    })


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/ping")
def ping():
    return {"status": "ok", "service": "apsen-dispenser-simulator"}


@app.get("/status")
def status():
    # Sem lock aqui: _snapshot_slot() já trava por conta própria e _lock é um
    # Lock simples, não reentrante — segurá-lo antes da chamada travava a request
    # para sempre. Cada slot sai consistente consigo mesmo; não há snapshot
    # atômico dos 6, mesma garantia que _telemetria_loop() já assume.
    slots = [_snapshot_slot(slot_id) for slot_id in range(1, NUM_SLOTS + 1)]
    return {"slots": slots, "ts": _ts()}


@app.get("/status/{slot_id}")
def status_slot(slot_id: int):
    if slot_id not in range(1, NUM_SLOTS + 1):
        raise HTTPException(400, f"slot_id deve ser 1-{NUM_SLOTS}")
    return _snapshot_slot(slot_id)


@app.post("/executar/carregar")
def executar_carregar(req: CarregarReq):
    slot_id = req.dispenser_id
    if slot_id not in range(1, NUM_SLOTS + 1):
        raise HTTPException(400, "dispenser_id deve ser 1-6")

    # Per-slot lock: check-and-set atômico para evitar race condition
    # entre dois POST simultâneos para o mesmo slot.
    with _slot_locks[slot_id]:
        with _lock:
            sts = _estado[slot_id]["status"]
        if sts in ("carregando", "dispensando"):
            raise HTTPException(409, f"Dispenser {slot_id} ocupado (status: {sts})")
        # Marca como "carregando" dentro do lock — próximo request já vê o status
        with _lock:
            _estado[slot_id]["status"] = "carregando"

    threading.Thread(
        target=_do_carregar,
        args=(slot_id, req.medicamento, req.sku, req.categoria, req.quantidade, req.os_id),
        daemon=True,
        name=f"carregar-{slot_id}",
    ).start()

    return {"ok": True, "dispenser_id": slot_id, "msg": "Carregamento iniciado"}


@app.post("/executar/dispensar")
def executar_dispensar(req: DispensarReq):
    slot_id = req.dispenser_id
    if slot_id not in range(1, NUM_SLOTS + 1):
        raise HTTPException(400, "dispenser_id deve ser 1-6")

    with _slot_locks[slot_id]:
        with _lock:
            sts = _estado[slot_id]["status"]
        if sts != "pronto":
            raise HTTPException(409, f"Dispenser {slot_id} não está pronto (status: {sts})")
        with _lock:
            _estado[slot_id]["status"] = "dispensando"

    threading.Thread(
        target=_do_dispensar,
        args=(slot_id, req.os_id),
        daemon=True,
        name=f"dispensar-{slot_id}",
    ).start()

    return {"ok": True, "dispenser_id": slot_id, "msg": "Dispensa iniciada"}


@app.post("/executar/limpar")
def executar_limpar(req: LimparReq):
    slot_id = req.dispenser_id
    if slot_id not in range(1, NUM_SLOTS + 1):
        raise HTTPException(400, "dispenser_id deve ser 1-6")

    threading.Thread(
        target=_do_limpar,
        args=(slot_id, req.solicitado_por),
        daemon=True,
        name=f"limpar-{slot_id}",
    ).start()

    return {"ok": True, "dispenser_id": slot_id, "msg": "Limpeza iniciada"}


# ── Telemetria periódica ───────────────────────────────────────────────────────

def _telemetria_loop():
    while True:
        time.sleep(15)
        ts = _ts()
        with _lock:
            estados = {d: e["status"] for d, e in _estado.items()}

        for slot_id, sts in estados.items():
            em_uso = sts in ("carregando", "dispensando")
            valor  = round(
                TEMP_BASE[slot_id] + (4.0 if em_uso else 0.5) + random.uniform(-0.5, 0.5), 1
            )
            _evento({
                "tipo":          "telemetria",
                "dispenser_id":  slot_id,
                "componente":    f"dispenser_{slot_id}",
                "tipo_leitura":  "temperatura",
                "valor_c":       valor,
                "unidade":       "°C",
                "ts":            ts,
            })

        # Envia status de todos os slots
        for slot_id in range(1, NUM_SLOTS + 1):
            snap = _snapshot_slot(slot_id)
            snap["tipo"] = "status"
            _evento(snap)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    threading.Thread(target=_telemetria_loop, daemon=True, name="telemetria").start()
    logger.info(
        "Dispenser Simulator v3.1 | adapter=%s | %d slots | "
        "T_CARGA=%.1fs | T_DISP=%.1fs | PROB_MECANICO=%.0f%%",
        ADAPTER_URL, NUM_SLOTS, T_CARGA_UNID, T_DISPENSA_UNID,
        PROB_ERRO_MECANICO * 100,
    )
    uvicorn.run(app, host="0.0.0.0", port=8201, log_level="warning")
