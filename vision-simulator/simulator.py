"""
APSEN - Vision Simulator v1.0
Simula as TRÊS câmeras independentes da célula automatizada.

CÂMERAS DOS DISPENSERS (cam_id="dispenser_esq" e "dispenser_dir")
  Uma por fileira: a célula é um corredor com metade dos slots de cada lado, e
  cada câmera cobre a sua fileira (D1-D4 à esquerda, D5-D8 à direita).
  Responsáveis por:
  - Leitura de QR Code, DataMatrix e código de barras
  - Identificação e confirmação do produto carregado em cada slot
  - Detecção de embalagem danificada ou produto incorreto

  Qual das duas olha um slot é decidido AQUI, por `camera_do_slot()`: o lado é
  característica física da bancada, não escolha de quem comanda. O adapter e o
  central mandam só o `slot_id`.

  Eventos gerados (os MESMOS nas duas câmeras — o lado vem do campo `camera`):
    leitura_dispenser_ok   → SKU confirmado, produto correto
    leitura_dispenser_falha → falha de leitura (câmera, iluminação, código sujo)
    leitura_dispenser_divergencia → leitura ok mas SKU não bate com esperado

CÂMERA DA MESA / DA BALANÇA (cam_id="mesa")
  Posicionada sobre a mesa de coleta, onde fica a célula de carga HX711.
  Responsável por:
  - Localização dos produtos dispensados
  - Contagem visual das unidades na mesa
  - Validação de posição (produto caiu fora da zona de coleta?)

  Eventos gerados:
    leitura_mesa_ok         → produtos detectados, contagem correta
    leitura_mesa_falha      → câmera não detectou nada
    leitura_mesa_divergencia → contagem detectada difere da esperada

Todas as operações de captura rodam em background threads e enviam
resultado via POST /eventos → vision-adapter → central-computer.

Probabilidades e tempos controlados por variáveis de ambiente. As três
primeiras valem para AS DUAS câmeras de dispenser; o sufixo `_ESQ`/`_DIR`
sobrescreve uma delas (para simular uma câmera com problema em campo):

  PROB_FALHA_LEITURA_DISPENSER  0.02  câmera do dispenser não consegue ler
  PROB_DIVERGENCIA_DISPENSER    0.02  leu mas SKU errado (produto trocado)
  T_SCAN_DISPENSER              1.5   segundos para processar scan do dispenser
    ... _ESQ / _DIR                   sobrescrevem só a câmera daquele lado

  PROB_FALHA_LEITURA_MESA       0.02  câmera da mesa não detecta produto
  PROB_DIVERGENCIA_MESA         0.02  detecta produto mas contagem errada
  T_SCAN_MESA                   2.0   segundos para processar scan da mesa
"""
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from typing import NamedTuple

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [VISION-SIM] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ADAPTER_URL = os.getenv("ADAPTER_URL", "http://vision-adapter:8102")
# Faixa de slots aceita. Vem da MESMA env var do central e dos demais
# simuladores: um valor por serviço viraria divergência silenciosa.
NUM_SLOTS = int(os.getenv("NUM_SLOTS", "8"))
# Metade dos slots por fileira — mesma partição da geometria do orquestrador
# (`orchestrator._gerar_posicoes`), que é o que faz D1..D4 caírem na câmera da
# esquerda e D5..D8 na da direita sem tabela à parte.
SLOTS_POR_FILEIRA = max(1, NUM_SLOTS // 2)

# ── Identificadores das três câmeras ──────────────────────────────────────────
# O campo `camera` de todo evento carrega um destes valores. "mesa" é a câmera
# da BALANÇA (sobre a mesa de coleta, onde está o HX711); o nome foi mantido
# porque já existe gravado em `visao_leituras.camera`.
CAM_ESQ  = "dispenser_esq"
CAM_DIR  = "dispenser_dir"
CAM_MESA = "mesa"


class ConfigCamera(NamedTuple):
    """Parâmetros de simulação de uma câmera."""
    nome:             str     # valor do campo `camera` nos eventos
    rotulo:           str     # prefixo de log
    prob_falha:       float
    prob_divergencia: float
    t_scan:           float


def _cfg_float(base: str, sufixo: str, default: str) -> float:
    """Valor da câmera específica, caindo no padrão compartilhado.

    `PROB_FALHA_LEITURA_DISPENSER` configura as DUAS câmeras de dispenser;
    `PROB_FALHA_LEITURA_DISPENSER_ESQ` sobrescreve só a da esquerda. Assim o
    caso comum continua sendo uma variável, e simular UMA câmera ruim não
    obriga a duplicar toda a configuração.
    """
    padrao = os.getenv(base, default)
    return float(os.getenv(f"{base}_{sufixo}", padrao))


def _config_dispenser(nome: str, sufixo: str, rotulo: str) -> ConfigCamera:
    return ConfigCamera(
        nome             = nome,
        rotulo           = rotulo,
        prob_falha       = _cfg_float("PROB_FALHA_LEITURA_DISPENSER", sufixo, "0.02"),
        prob_divergencia = _cfg_float("PROB_DIVERGENCIA_DISPENSER",   sufixo, "0.02"),
        t_scan           = _cfg_float("T_SCAN_DISPENSER",             sufixo, "1.5"),
    )


CAMERAS_DISPENSER: dict[str, ConfigCamera] = {
    CAM_ESQ: _config_dispenser(CAM_ESQ, "ESQ", "CAM-ESQ"),
    CAM_DIR: _config_dispenser(CAM_DIR, "DIR", "CAM-DIR"),
}

# A câmera da mesa é uma só — não há lado a sobrescrever.
CAMERA_MESA = ConfigCamera(
    nome             = CAM_MESA,
    rotulo           = "CAM-MESA",
    prob_falha       = float(os.getenv("PROB_FALHA_LEITURA_MESA", "0.02")),
    prob_divergencia = float(os.getenv("PROB_DIVERGENCIA_MESA",   "0.02")),
    t_scan           = float(os.getenv("T_SCAN_MESA",             "2.0")),
)


def camera_do_slot(slot_id: int) -> str:
    """Qual câmera de dispenser enxerga o slot — ÚNICA fonte dessa relação.

    Ids 1..N/2 estão na fileira esquerda e N/2+1..N na direita, exatamente
    como em `orchestrator._gerar_posicoes`. Derivar do slot (em vez de receber
    a câmera no comando) é o que impede o central de apontar a câmera errada
    para um slot: quem comanda não precisa saber a geometria da bancada.
    """
    return CAM_ESQ if slot_id <= SLOTS_POR_FILEIRA else CAM_DIR


def config_do_slot(slot_id: int) -> ConfigCamera:
    """Parâmetros da câmera que cobre o slot."""
    return CAMERAS_DISPENSER[camera_do_slot(slot_id)]


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


# ── Câmeras dos Dispensers (uma por fileira) ──────────────────────────────────

def _do_capturar_dispenser(slot_id: int, os_id: str, sku_esperado: str,
                            medicamento_esperado: str, quantidade_esperada: int):
    """
    Simula o processamento da câmera de dispenser do lado do slot:
    1. Escolhe a câmera pelo slot (esquerda ou direita)
    2. Delay de processamento (iluminação + leitura óptica)
    3. Decide se a leitura falha, acerta ou diverge
    4. Envia evento ao adapter, marcado com a câmera que olhou
    """
    cam = config_do_slot(slot_id)
    logger.info("[%s] Iniciando scan slot=%d | sku=%s", cam.rotulo, slot_id, sku_esperado)
    time.sleep(cam.t_scan)

    ts = _ts()

    # Caso 1: falha de hardware (câmera, iluminação, código sujo/danificado)
    if random.random() < cam.prob_falha:
        motivo = random.choice([
            "camera_obstruida",
            "iluminacao_insuficiente",
            "codigo_danificado",
            "timeout_leitura",
        ])
        logger.warning("[%s] FALHA leitura slot=%d: %s", cam.rotulo, slot_id, motivo)
        _evento({
            "tipo":          "leitura_dispenser_falha",
            "camera":        cam.nome,
            "slot_id":       slot_id,
            "os_id":         os_id,
            "motivo":        motivo,
            "sku_esperado":  sku_esperado,
            "confianca":     0.0,
            "ts":            ts,
        })
        return

    # Caso 2: leitura bem-sucedida mas SKU divergente (produto errado)
    if random.random() < cam.prob_divergencia:
        sku_simulado = f"APSEN-ERRADO-{random.randint(100,999)}"
        logger.error(
            "[%s] DIVERGÊNCIA slot=%d | esperado=%s | lido=%s",
            cam.rotulo, slot_id, sku_esperado, sku_simulado,
        )
        _evento({
            "tipo":             "leitura_dispenser_divergencia",
            "camera":           cam.nome,
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
        "[%s] OK slot=%d | sku=%s | confiança=%.1f%%",
        cam.rotulo, slot_id, sku_esperado, confianca * 100,
    )
    _evento({
        "tipo":              "leitura_dispenser_ok",
        "camera":            cam.nome,
        "slot_id":           slot_id,
        "os_id":             os_id,
        "sku_esperado":      sku_esperado,
        "sku_lido":          sku_esperado,
        "medicamento_lido":  medicamento_esperado,
        "match_sku":         True,
        "confianca":         confianca,
        "ts":                ts,
    })


# ── Câmera da Mesa (sobre a balança HX711) ────────────────────────────────────

def _do_capturar_mesa(slot_id: int, os_id: str, quantidade_esperada: int,
                       posicao_x: float, posicao_y: float):
    """
    Simula o processamento da câmera da mesa de coleta (a da balança):
    1. Delay de processamento (análise de imagem, contagem)
    2. Decide se não detecta nada, conta corretamente ou diverge na contagem
    3. Envia evento ao adapter
    """
    cam = CAMERA_MESA
    logger.info(
        "[%s] Iniciando scan slot=%d | qtd_esp=%d | pos=(%.1f,%.1f)",
        cam.rotulo, slot_id, quantidade_esperada, posicao_x, posicao_y,
    )
    time.sleep(cam.t_scan)

    ts = _ts()

    # Caso 1: câmera não detecta produto (produto fora da zona, obstrução)
    if random.random() < cam.prob_falha:
        motivo = random.choice([
            "produto_fora_zona_coleta",
            "obstrucao_visual",
            "camera_desalinhada",
            "produto_nao_detectado",
        ])
        logger.warning("[%s] FALHA leitura slot=%d: %s", cam.rotulo, slot_id, motivo)
        _evento({
            "tipo":               "leitura_mesa_falha",
            "camera":             cam.nome,
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
    if random.random() < cam.prob_divergencia:
        # Simula contagem levemente errada (±1 a ±2 unidades)
        delta    = random.choice([-2, -1, 1, 2])
        detectado = max(0, quantidade_esperada + delta)
        logger.warning(
            "[%s] DIVERGÊNCIA slot=%d | esperado=%d | detectado=%d",
            cam.rotulo, slot_id, quantidade_esperada, detectado,
        )
        _evento({
            "tipo":                "leitura_mesa_divergencia",
            "camera":              cam.nome,
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
        "[%s] OK slot=%d | detectado=%d/%d | confiança=%.1f%%",
        cam.rotulo, slot_id, quantidade_esperada, quantidade_esperada, confianca * 100,
    )
    _evento({
        "tipo":                "leitura_mesa_ok",
        "camera":              cam.nome,
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
    coberturas = [
        (CAMERAS_DISPENSER[CAM_ESQ], f"D1-D{SLOTS_POR_FILEIRA}"),
        (CAMERAS_DISPENSER[CAM_DIR], f"D{SLOTS_POR_FILEIRA + 1}-D{NUM_SLOTS}"),
        (CAMERA_MESA,                "mesa de coleta (balança HX711)"),
    ]
    return {
        "service": "vision-simulator",
        "cameras": [
            {
                "camera":           cam.nome,
                "cobertura":        cobertura,
                "prob_falha":       cam.prob_falha,
                "prob_divergencia": cam.prob_divergencia,
                "t_scan":           cam.t_scan,
            }
            for cam, cobertura in coberturas
        ],
        "num_slots": NUM_SLOTS,
        "ts":        _ts(),
    }


@app.post("/executar/capturar/dispenser")
def executar_capturar_dispenser(req: CapturarDispenserReq):
    """
    Inicia captura assíncrona da câmera de dispenser do lado do slot.
    O resultado é enviado via POST /eventos → adapter → central.
    """
    if not 1 <= req.slot_id <= NUM_SLOTS:
        raise HTTPException(400, f"slot_id deve ser 1-{NUM_SLOTS}")

    cam = camera_do_slot(req.slot_id)

    threading.Thread(
        target=_do_capturar_dispenser,
        args=(req.slot_id, req.os_id, req.sku_esperado,
              req.medicamento_esperado, req.quantidade_esperada),
        daemon=True,
        name=f"vision-disp-{req.slot_id}",
    ).start()

    logger.info("[CAM-DISP] Captura iniciada slot=%d cam=%s OS=%s",
                req.slot_id, cam, req.os_id)
    return {
        "ok":      True,
        "camera":  cam,
        "slot_id": req.slot_id,
        "msg":     f"Processando scan do dispenser {req.slot_id} ({cam})",
    }


@app.post("/executar/capturar/mesa")
def executar_capturar_mesa(req: CapturarMesaReq):
    """
    Inicia captura assíncrona da câmera da mesa (a da balança).
    O resultado é enviado via POST /eventos → adapter → central.
    """
    if not 1 <= req.slot_id <= NUM_SLOTS:
        raise HTTPException(400, f"slot_id deve ser 1-{NUM_SLOTS}")

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
        "camera":  CAM_MESA,
        "slot_id": req.slot_id,
        "msg":     f"Processando scan da mesa (slot {req.slot_id})",
    }


# ── Telemetria periódica ───────────────────────────────────────────────────────

# Um componente por câmera física, mais o processador que roda os três fluxos.
COMPONENTES_TELEMETRIA = [
    ("camera_dispenser_esq", 35, 50),
    ("camera_dispenser_dir", 35, 50),
    ("camera_mesa",          33, 48),
    ("processador_visao",    45, 70),
]


def _telemetria_loop():
    """Envia leituras de temperatura dos componentes de câmera a cada 60s."""
    import time as _time
    while True:
        _time.sleep(60)
        ts = _ts()
        for comp, t_min, t_max in COMPONENTES_TELEMETRIA:
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

    _esq = CAMERAS_DISPENSER[CAM_ESQ]
    _dir = CAMERAS_DISPENSER[CAM_DIR]
    logger.info(
        "Vision Simulator v1.0 | adapter=%s | %d slots (%d por fileira)\n"
        "  CAM-ESQ  (D1-D%d):   falha=%.0f%% div=%.0f%% scan=%.1fs\n"
        "  CAM-DIR  (D%d-D%d):   falha=%.0f%% div=%.0f%% scan=%.1fs\n"
        "  CAM-MESA (balança): falha=%.0f%% div=%.0f%% scan=%.1fs",
        ADAPTER_URL, NUM_SLOTS, SLOTS_POR_FILEIRA,
        SLOTS_POR_FILEIRA,
        _esq.prob_falha * 100, _esq.prob_divergencia * 100, _esq.t_scan,
        SLOTS_POR_FILEIRA + 1, NUM_SLOTS,
        _dir.prob_falha * 100, _dir.prob_divergencia * 100, _dir.t_scan,
        CAMERA_MESA.prob_falha * 100, CAMERA_MESA.prob_divergencia * 100,
        CAMERA_MESA.t_scan,
    )
    uvicorn.run(app, host="0.0.0.0", port=8202, log_level="warning")
