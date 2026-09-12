"""
APSEN - Weight Simulator v1.0 (HX711)
Simula a célula de carga HX711 instalada na mesa de dispensação.

A célula mede o peso acumulado na mesa após cada dispensa.
Usada para Triple Check: comparar peso medido × peso esperado (qtd × peso_unitario_g).

O comando de pesagem carrega DUAS quantidades, e a distinção é o que dá à
balança poder de detectar falha de dispensa (ver `_do_pesar`):
  `quantidade_esperada` → alvo da OS, base do peso ESPERADO (e do desvio);
  `quantidade_real`     → o que o dispenser soltou, base do peso que a mesa ganha.

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
  T_TARA                  → tempo de estabilização da tara em segundos (default: 0.5)
  MODO_APRESENTACAO       → 1 zera PROB_ERRO_SENSOR e RUIDO_G (demo determinística)
  FATOR_VELOCIDADE        → multiplica T_LEITURA e T_TARA (default: 1.0)
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
    format="%(asctime)s [WEIGHT-SIM] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ADAPTER_URL      = os.getenv("ADAPTER_URL",      "http://weight-adapter:8103")
# Faixa de slots aceita. Vem da MESMA env var do central e dos demais
# simuladores: um valor por serviço viraria divergência silenciosa.
NUM_SLOTS = int(os.getenv("NUM_SLOTS", "8"))
TOLERANCIA_PERC  = float(os.getenv("TOLERANCIA_PERC",  "5.0"))


# ── Controles globais de demonstração ─────────────────────────────────────────
# Cópia deliberada do bloco que existe nos quatro simuladores e no
# `central-computer/config.py` — imagens separadas, sem módulo compartilhado.
# `tests/test_modo_apresentacao.py` compara as cinco cópias entre si.

def _modo_apresentacao() -> bool:
    """Modo demonstração: o acaso é desligado, o sistema roda determinístico.

    Aceita as grafias que alguém digita no `.env` sem pensar ("1", "true",
    "sim", "on"); qualquer outra coisa é falso. Nunca levanta: um valor
    estranho aqui não pode impedir o simulador de subir.
    """
    return os.getenv("MODO_APRESENTACAO", "0").strip().lower() in (
        "1", "true", "yes", "sim", "on",
    )


def _fator_velocidade() -> float:
    """Multiplicador de TODOS os tempos simulados. Faixa válida: 0.1..10.

    0.5 roda a demo no dobro da velocidade; 2.0, na metade, para explicar cada
    etapa com calma. Fora da faixa cai em 1.0 com warning, em vez de virar
    comportamento silencioso: 0 faria toda etapa terminar instantaneamente
    (sem nada para mostrar) e um valor enorme travaria a apresentação inteira
    em cima do primeiro slot.
    """
    bruto = os.getenv("FATOR_VELOCIDADE", "1.0")
    try:
        valor = float(bruto)
    except ValueError:
        valor = 0.0
    if not 0.1 <= valor <= 10.0:
        logger.warning(
            "FATOR_VELOCIDADE=%r fora da faixa 0.1..10 — usando 1.0.", bruto)
        return 1.0
    return valor


MODO_APRESENTACAO = _modo_apresentacao()
FATOR_VELOCIDADE  = _fator_velocidade()


def _prob(valor: float) -> float:
    """Probabilidade de falha ALEATÓRIA — zerada em modo apresentação.

    Só o acaso passa por aqui. Falha injetada de propósito é outro caminho e
    continua valendo com o modo ligado.
    """
    return 0.0 if MODO_APRESENTACAO else valor


def _tempo(valor: float) -> float:
    """Tempo simulado escalado pelo fator de velocidade."""
    return valor * FATOR_VELOCIDADE


PROB_ERRO_SENSOR = _prob(float(os.getenv("PROB_ERRO_SENSOR",  "0.01")))
T_LEITURA        = _tempo(float(os.getenv("T_LEITURA",         "1.5")))
# Tempo de estabilização da tara. Era um 0.5 literal dentro de `_do_tara`;
# virou constante para escalar junto com o resto — um passo do ciclo que não
# acompanhasse o fator seria justamente o que dessincroniza a narração da demo.
T_TARA           = _tempo(float(os.getenv("T_TARA",            "0.5")))

# RUIDO_G é a ÚNICA fonte de aleatoriedade que também passa por `_prob`, e ela
# não é uma probabilidade: é o σ do ruído gaussiano da célula de carga. Entra
# aqui porque é uma fonte de FALHA de verdade, não enfeite — com σ=2 g contra
# um esperado de 100 g (quantidade mínima × peso unitário padrão), a tolerância
# de 5% fica a 2,5σ e uma OS de vários slots tem alguns por cento de chance de
# uma `peso_divergencia` sem causa nenhuma. Zerá-la é o que faz "modo
# apresentação" significar determinístico, e não apenas "com menos sorteios".
RUIDO_G          = _prob(float(os.getenv("RUIDO_G",           "2.0")))

# ── Injeção de falha (DEMONSTRAÇÃO) ───────────────────────────────────────────
# Único valor de `injetar_falha` que este simulador reconhece. O nome é o mesmo
# de `central-computer/injecao.TIPO_DIVERGENCIA_PESO`, e `tests/test_injecao.py`
# cobra a igualdade: uma string divergente aqui viraria gatilho armado que nunca
# dispara — sem erro em lugar nenhum.
INJECAO_DIVERGENCIA_PESO = "divergencia_peso"
# Quanto a leitura injetada erra, em pontos percentuais ALÉM da tolerância. 5 pp
# põe o desvio em ~10% contra uma tolerância de 5%: inequivocamente fora, sem
# ser um número absurdo que denunciaria a simulação na tela.
INJECAO_EXCESSO_PP = 5.0

if MODO_APRESENTACAO:
    logger.warning(
        "MODO APRESENTACAO LIGADO — sem falha de sensor e sem ruído na célula "
        "de carga (PROB_ERRO_SENSOR e RUIDO_G forçados a 0).")
if FATOR_VELOCIDADE != 1.0:
    logger.warning("FATOR_VELOCIDADE=%.2f — tempos simulados escalados.",
                   FATOR_VELOCIDADE)


# ── Estado interno ─────────────────────────────────────────────────────────────

_lock           = threading.Lock()
_peso_tara_g    = 0.0   # offset da tara (zera a balança)
_peso_mesa_g    = 0.0   # peso acumulado real na mesa (simulado)
_peso_anterior_g = 0.0  # peso líquido na última leitura — para calcular delta por slot


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
    quantidade_esperada: int              # alvo da OS — base do peso ESPERADO
    quantidade_real: Optional[int] = None  # o que o dispenser soltou — vira peso na mesa
    peso_unitario_g: float  # gramas por unidade (vem do catálogo de medicamentos)
    # Caminho de DEMONSTRAÇÃO, ausente em toda pesagem normal. Ver o bloco
    # "Injeção de falha" acima e `central-computer/injecao.py`.
    injetar_falha: Optional[str] = None


# ── Handlers de hardware (background threads) ──────────────────────────────────

def _do_tara(os_id: str):
    """Executa a tara (zera a balança)."""
    global _peso_tara_g, _peso_anterior_g

    time.sleep(T_TARA)  # tempo de estabilização

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
        _peso_tara_g    = _peso_mesa_g  # offset de tara = peso atual da mesa
        _peso_anterior_g = 0.0          # zera delta — nova OS começa do zero
        peso_offset     = _peso_tara_g

    logger.info("[HX711] Tara realizada. Offset=%.1fg | OS=%s", peso_offset, os_id)
    _evento({
        "tipo":       "tara_ok",
        "os_id":      os_id,
        "peso_tara_g": peso_offset,
        "ts":         _ts(),
    })


def _do_pesar(os_id: str, slot_id: int, quantidade_esperada: int, peso_unitario_g: float,
              quantidade_real: Optional[int] = None,
              injetar_falha: Optional[str] = None):
    """Captura leitura de peso e calcula divergência.

    `quantidade_esperada` é o alvo da OS; `quantidade_real` é o que o dispenser
    reportou ter soltado. A mesa ganha peso pelo REAL, o desvio é medido contra
    o ESPERADO — é dessa diferença que a divergência emerge, como numa célula
    de carga de verdade.

    Antes a mesa crescia pelo peso esperado, então a balança comparava o valor
    consigo mesmo: uma falha mecânica que soltasse 8 de 10 unidades continuava
    "pesando" 10. A fonte 3 do Triple Check só divergia por ruído gaussiano
    (σ=2 g contra ≥100 g esperados — praticamente nunca), o que reduzia o
    Triple Check a um double check.

    `None` mantém o contrato antigo (real = esperada), para chamador que não
    tenha a contagem do dispenser.

    `injetar_falha` é o caminho de DEMONSTRAÇÃO: ele desloca a LEITURA, não a
    massa na mesa — é uma balança errando, não medicamento sumindo. Ver o ramo
    marcado mais abaixo.
    """
    global _peso_mesa_g, _peso_anterior_g

    if quantidade_real is None:
        quantidade_real = quantidade_esperada

    # ── Injeção: decidida ANTES do sorteio do sensor ─────────────────────────
    inj_divergencia = injetar_falha == INJECAO_DIVERGENCIA_PESO
    if injetar_falha and not inj_divergencia:
        logger.warning("[HX711] Injeção desconhecida ignorada: %r", injetar_falha)
    elif inj_divergencia:
        logger.warning(
            "[HX711] ⚡ INJEÇÃO ARMADA (demonstração) slot=%d → divergência de "
            "peso. NÃO é falha real da célula de carga.", slot_id,
        )

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

    peso_esperado_g = quantidade_esperada * peso_unitario_g   # alvo da OS
    peso_real_g     = quantidade_real * peso_unitario_g       # o que caiu na mesa

    # A balança está sob a mesa da CNC: mede o peso TOTAL acumulado desde a tara.
    # Para validar cada slot individualmente, comparamos o DELTA (incremento desta
    # dispensa) com o peso esperado do slot — não o acumulado total.
    with _lock:
        _peso_mesa_g   += peso_real_g + random.gauss(0, RUIDO_G)
        peso_bruto_g    = _peso_mesa_g
        peso_liquido_g  = max(0.0, peso_bruto_g - _peso_tara_g)      # total desde tara
        peso_delta_g    = max(0.0, peso_liquido_g - _peso_anterior_g) # incremento deste slot
        _peso_anterior_g = peso_liquido_g                             # salva para próximo slot

    # ── Ramo de INJEÇÃO (demonstração) ───────────────────────────────────────
    # Desloca só a LEITURA deste slot, depois de `_peso_anterior_g` ter sido
    # salvo: a massa acumulada na mesa continua correta e o slot SEGUINTE pesa
    # normal. Fazer a mesa perder peso de verdade seria simular medicamento
    # evaporando, e contaminaria a leitura seguinte — o one-shot deixaria de
    # ser one-shot sem ninguém notar.
    desvio_injetado_g = 0.0
    if inj_divergencia and peso_esperado_g > 0:
        desvio_injetado_g = -peso_esperado_g * (TOLERANCIA_PERC + INJECAO_EXCESSO_PP) / 100.0
        peso_delta_g = max(0.0, peso_delta_g + desvio_injetado_g)

    # Desvio: delta medido vs peso esperado do slot
    desvio_g   = peso_delta_g - peso_esperado_g
    desvio_pct = (abs(desvio_g) / peso_esperado_g * 100.0) if peso_esperado_g > 0 else 0.0

    dentro_tolerancia = desvio_pct <= TOLERANCIA_PERC
    tipo = "peso_ok" if dentro_tolerancia else "peso_divergencia"

    logger.info(
        "[HX711] slot=%d | qtd esp/real=%d/%d | esperado=%.1fg | delta=%.1fg | "
        "acum=%.1fg | desvio=%.1f%% | %s",
        slot_id, quantidade_esperada, quantidade_real, peso_esperado_g, peso_delta_g,
        peso_liquido_g, desvio_pct,
        "OK" if dentro_tolerancia else "DIVERGÊNCIA",
    )

    _evento({
        "tipo":                 tipo,
        "os_id":                os_id,
        "slot_id":              slot_id,
        "quantidade_esperada":  quantidade_esperada,
        "quantidade_real":      quantidade_real,
        "peso_unitario_g":      peso_unitario_g,
        "peso_esperado_g":      round(peso_esperado_g, 2),
        "peso_medido_g":        round(peso_delta_g, 2),      # delta deste slot
        "peso_acumulado_g":     round(peso_liquido_g, 2),    # total na mesa (informativo)
        "desvio_g":             round(desvio_g, 2),
        "desvio_pct":           round(desvio_pct, 2),
        "tolerancia_pct":       TOLERANCIA_PERC,
        "dentro_tolerancia":    dentro_tolerancia,
        "falha_injetada":       desvio_injetado_g != 0.0,
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
    if not 1 <= req.slot_id <= NUM_SLOTS:
        raise HTTPException(400, f"slot_id deve ser 1-{NUM_SLOTS}")
    if req.peso_unitario_g <= 0:
        raise HTTPException(400, "peso_unitario_g deve ser > 0")
    if req.quantidade_real is not None and req.quantidade_real < 0:
        raise HTTPException(400, "quantidade_real não pode ser negativa")

    logger.info(
        "[CMD] PESAR slot=%d | qtd esperada=%d real=%s | peso_unit=%.1fg | OS=%s",
        req.slot_id, req.quantidade_esperada,
        req.quantidade_esperada if req.quantidade_real is None else req.quantidade_real,
        req.peso_unitario_g, req.os_id,
    )
    threading.Thread(
        target=_do_pesar,
        args=(req.os_id, req.slot_id, req.quantidade_esperada, req.peso_unitario_g,
              req.quantidade_real, req.injetar_falha),
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
    global _peso_mesa_g, _peso_tara_g, _peso_anterior_g
    with _lock:
        _peso_mesa_g = 0.0
        _peso_tara_g = 0.0
        _peso_anterior_g = 0.0  # sem isto o delta do 1º slot pós-reset vem negativo/zerado
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
        "T_LEITURA=%.1fs | PROB_ERRO=%.3f | RUIDO=%.1fg | "
        "modo=%s | fator_velocidade=%.2f",
        ADAPTER_URL, TOLERANCIA_PERC, T_LEITURA, PROB_ERRO_SENSOR, RUIDO_G,
        "APRESENTACAO" if MODO_APRESENTACAO else "realista", FATOR_VELOCIDADE,
    )
    uvicorn.run(app, host="0.0.0.0", port=8203)
