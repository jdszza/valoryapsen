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


# ── Controles globais de demonstração ─────────────────────────────────────────
# Cópia deliberada do bloco que existe nos quatro simuladores e no
# `central-computer/config.py` — imagens separadas, sem módulo compartilhado.
# `tests/test_modo_apresentacao.py` compara as cinco cópias entre si.
#
# A CNC não sorteia falha nenhuma: `MODO_APRESENTACAO` não muda o comportamento
# dela. Ela lê a variável mesmo assim para que o log de startup diga qual modo
# está em vigor — um serviço calado sobre isso é um serviço sobre o qual se
# duvida na hora da apresentação.

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

# Aqui o fator entra DIVIDINDO, e é a única inversão de sinal do sistema: o
# fator multiplica TEMPO, e `VEL_MM_S` é o inverso de tempo. Fator 2.0 ("na
# metade da velocidade") tem que produzir uma CNC mais LENTA, ou seja, menos
# mm/s — multiplicar aqui faria a CNC acelerar justamente quando se pediu calma
# para narrar o movimento, e o sintoma seria a CNC chegando ao slot antes da
# frase que a anuncia.
VEL_MM_S_BASE   = float(os.getenv("VEL_MM_S",   "80"))   # mm/s, antes do fator
VELOCIDADE_MM_S = VEL_MM_S_BASE / FATOR_VELOCIDADE

# Piso de duração de um movimento. Escala junto: sem isso, um trajeto curto
# ficaria preso em 1 s enquanto todo o resto da célula desacelerou — a CNC seria
# a única peça fora do compasso.
DURACAO_MIN_S   = 1.0 * FATOR_VELOCIDADE

# `INTERVALO` NÃO escala: é a cadência de PUBLICAÇÃO da posição, não um tempo
# físico simulado. Desacelerar o movimento mantendo a cadência dá mais passos
# (trajetória mais suave na tela, que é o desejado); acelerar dá menos, com o
# piso de 3 passos que já existia.
INTERVALO_PUB   = float(os.getenv("INTERVALO",  "0.5"))  # s entre publicações de posição

if MODO_APRESENTACAO:
    logger.warning(
        "MODO APRESENTACAO LIGADO — a CNC não sorteia falha em nenhum modo; "
        "nada muda aqui além deste registro.")
if FATOR_VELOCIDADE != 1.0:
    logger.warning(
        "FATOR_VELOCIDADE=%.2f — CNC a %.1f mm/s (base %.1f).",
        FATOR_VELOCIDADE, VELOCIDADE_MM_S, VEL_MM_S_BASE)

NUM_SLOTS = int(os.getenv("NUM_SLOTS", "8"))

# ── O ROTEIRO DA BANCADA ──────────────────────────────────────────────────────
# O simulador continua SEM cópia do mapa do central — e agora por uma razão
# mais forte que antes. A mesa de verdade não recebe coordenadas: ela executa
# um roteiro MEDIDO nela, gravado waypoint a waypoint na bancada, e REPORTA
# onde parou. Este simulador faz o mesmo: tem a própria bancada e reporta a
# posição que alcançou.
#
# Os números abaixo são deliberadamente DIFERENTES dos de `orchestrator.
# POSICOES`, e é isso que os torna úteis. Se fossem iguais, toda asserção sobre
# a posição passaria por concordância acidental: o central compararia o seu
# número com uma cópia do seu número, e continuaria passando no dia em que ele
# voltasse a ignorar o que a máquina informa. Diferentes, qualquer volta atrás
# aparece na primeira leitura do histórico.
#
# Nenhum número daqui precisa CONCORDAR com o central — é justamente o ponto da
# decisão. O que precisa concordar (quantos slots existem) continua vindo do
# `NUM_SLOTS` declarado uma vez no compose.
_BANCADA_X0_MM   = float(os.getenv("BANCADA_X0_MM", "37.5"))
_BANCADA_PASSO_MM = float(os.getenv("BANCADA_PASSO_MM", "95.0"))
_BANCADA_Y_MM    = float(os.getenv("BANCADA_Y_MM", "128.0"))


def _bancada() -> dict[int, tuple[float, float]]:
    """Slot → posição medida. Determinístico: a bancada não muda entre boots."""
    fileira = max(1, NUM_SLOTS // 2)
    pontos: dict[int, tuple[float, float]] = {}
    for slot in range(1, NUM_SLOTS + 1):
        coluna = (slot - 1) % fileira
        lado = -1.0 if slot <= fileira else 1.0
        pontos[slot] = (_BANCADA_X0_MM + coluna * _BANCADA_PASSO_MM,
                        lado * _BANCADA_Y_MM)
    return pontos


BANCADA: dict[int, tuple[float, float]] = _bancada()

# Quais receitas estão GRAVADAS nesta "placa". Configurável para que a
# demonstração possa encenar a ordem cuja receita ninguém gravou — que é
# justamente o modo novo de a apresentação falhar, e o que o pré-voo pergunta.
RECEITAS_GRAVADAS = {
    letra.strip().upper()
    for letra in os.getenv("CNC_RECEITAS_GRAVADAS", "ABCDEFGHIJ").split(",")
    for letra in letra
    if letra.strip()
}

# HOME idem: o central manda as coordenadas no /executar/homing. Este par é só
# o fallback de quem chamar o endpoint sem elas (contrato antigo) e a posição
# em que a CNC nasce, antes do primeiro comando.
HOME: tuple[float, float] = (
    float(os.getenv("HOME_X", "-120")),
    float(os.getenv("HOME_Y", "0")),
)

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


def _recusar(os_id: str, dispenser_alvo: int, codigo: str, descricao: str) -> None:
    """Recusa que o orquestrador consegue LER.

    O 4xx sozinho aborta a OS na hora (é o que o `_post` do orquestrador faz com
    status que não vale retentar), mas não deixa rastro de POR QUÊ. O evento
    `erro` é o que vira alarme e fica no histórico — a mesma divisão de trabalho
    que o firmware faz entre o ACK negativo e o evento.
    """
    logger.warning("[CNC] mover recusado (%s): %s", codigo, descricao)
    _evento({
        "tipo":           "erro",
        "os_id":          os_id,
        "dispenser_alvo": dispenser_alvo,
        "codigo_erro":    codigo,
        "descricao":      descricao,
        "ts":             _ts(),
    })


# ── Estado da CNC ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_em_movimento = threading.Event()  # set=True enquanto CNC está em movimento
_movimento_lock = threading.Lock()  # protege check-and-set atômico de _em_movimento

# Trava do Triple Check, vinda do central por `/executar/estado-celula`. Enquanto
# ativa, `mover` é recusado com o mesmo `codigo_erro` que o firmware usa —
# vocabulário divergente aqui não daria erro, daria um alarme que ninguém sabe
# ler, e a diferença só apareceria com a placa montada.
_travada = False

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
    receita: str
    ciclo_atual: int = 0
    total_ciclos: int = 0


class HomingReq(BaseModel):
    os_id: str
    posicao_x: Optional[float] = None
    posicao_y: Optional[float] = None


class EstadoCelulaReq(BaseModel):
    """A trava do Triple Check chegando à mesa — mesmos campos do firmware."""
    trava_ativa: bool
    trava_slot_id: Optional[int] = None
    os_id: str = ""
    trava_resumo: str = ""


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

    duracao = max(DURACAO_MIN_S, distancia / VELOCIDADE_MM_S)
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


def _homing(os_id: str, destino: tuple[float, float] = HOME):
    """Move a CNC de volta para HOME e reporta conclusão.

    `destino` vem do comando (o central é quem sabe onde fica o HOME); o
    default cobre o chamador que não o informa.
    """
    with _lock:
        orig_x = _cnc_state["pos_x"]
        orig_y = _cnc_state["pos_y"]
        _cnc_state.update({
            "status":         "retornando",
            "dispenser_alvo": None,
        })

    distancia = math.hypot(destino[0] - orig_x, destino[1] - orig_y)
    duracao   = max(DURACAO_MIN_S, distancia / VELOCIDADE_MM_S)
    passos    = max(3, int(duracao / INTERVALO_PUB))

    logger.info("[CNC] HOMING: %.0fmm | ~%.1fs", distancia, duracao)

    for i in range(1, passos + 1):
        t     = i / passos
        cur_x = orig_x + (destino[0] - orig_x) * t
        cur_y = orig_y + (destino[1] - orig_y) * t

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
            "pos_x":       destino[0],
            "pos_y":       destino[1],
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
        "posicao_x": destino[0],
        "posicao_y": destino[1],
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


def _thread_homing(os_id: str, destino: tuple[float, float]):
    try:
        _homing(os_id, destino)
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


@app.post("/executar/estado-celula")
def executar_estado_celula(req: EstadoCelulaReq):
    """Guarda o estado e age só na TRANSIÇÃO, como o firmware.

    Receber `trava_ativa=true` duas vezes é inofensivo — a segunda não refaz
    nada. É o contrato do §6, e existe porque o central reenvia o aviso sempre
    que a trava muda, sem saber o que a placa já sabe.
    """
    global _travada
    anterior = _travada
    _travada = bool(req.trava_ativa)
    if _travada == anterior:
        return {"ok": True, "msg": "estado inalterado", "trava_ativa": _travada}

    if _travada:
        logger.warning("[CNC] TRAVA ATIVA (slot %s, OS %s): %s — mover recusado "
                       "ate a liberacao.", req.trava_slot_id, req.os_id,
                       req.trava_resumo)
    else:
        logger.info("[CNC] Trava liberada — mover aceito de novo.")
    return {"ok": True, "trava_ativa": _travada}


@app.post("/executar/mover")
def executar_mover(req: MoverReq):
    # A trava vem ANTES de tudo: com ela ativa a mesa não vai a lugar nenhum, e
    # o motivo da recusa é a trava, não a faixa nem a receita. Recusar aqui com
    # o mesmo `codigo_erro` do firmware é o que faz o alarme ser o mesmo com e
    # sem placa montada.
    if _travada:
        _recusar(req.os_id, req.dispenser_alvo, "travado",
                 "trava do Triple Check ativa: aguardando liberacao")
        raise HTTPException(409, "travado: trava do Triple Check ativa")

    # As recusas usam o MESMO `codigo_erro` do firmware (a tabela do topo de
    # `cnc/receitas_manuais.ino` e do §4 do protocolo). Vocabulário divergente
    # aqui não daria erro: daria um alarme que ninguém sabe ler, e a diferença
    # só apareceria com a placa montada, que é quando ninguém quer descobrir.
    #
    # Era o que estava acontecendo: este arquivo dizia "o MESMO codigo_erro" e
    # emitia `dispenser_sem_waypoint` onde o firmware emite `dispenser_invalido`.
    # O comentário foi escrito antes de o firmware existir, e ninguém conferiu
    # depois — é exatamente o modo de falhar que ele descreve.
    receita = (req.receita or "").strip().upper()
    if receita not in RECEITAS_GRAVADAS:
        # `receita_desconhecida` é o ÚNICO código que só existe deste lado, e
        # está documentado no §4 como tal. O firmware aceita qualquer receita
        # (ela é log, não endereço — quem acha a posição é `dispenser_pos`);
        # o simulador a valida porque é ele que encena, para a demonstração, a
        # ordem cujo roteiro ninguém gravou.
        _recusar(req.os_id, req.dispenser_alvo, "receita_desconhecida",
                 f"receita '{req.receita}' nao esta gravada nesta placa")
        raise HTTPException(400, f"receita_desconhecida: {req.receita}")

    if not 1 <= req.dispenser_alvo <= NUM_SLOTS:
        _recusar(req.os_id, req.dispenser_alvo, "dispenser_invalido",
                 f"dispenser_alvo {req.dispenser_alvo} fora da faixa 1-{NUM_SLOTS}")
        raise HTTPException(
            400, f"dispenser_alvo deve ser 1-{NUM_SLOTS}, recebido: {req.dispenser_alvo}"
        )

    # Verifica e seta _em_movimento atomicamente para evitar race condition
    with _movimento_lock:
        if _em_movimento.is_set():
            raise HTTPException(409, "CNC em movimento — aguarde posicionamento atual")
        _em_movimento.set()

    alvo_x, alvo_y = BANCADA[req.dispenser_alvo]
    threading.Thread(
        target=_thread_mover,
        args=(req.dispenser_alvo, alvo_x, alvo_y,
              req.os_id, req.ciclo_atual, req.total_ciclos),
        daemon=True,
        name=f"cnc-mover-D{req.dispenser_alvo}",
    ).start()

    return {
        "ok": True,
        "msg": f"Movendo para D{req.dispenser_alvo} (receita {receita})",
        "destino": {"x": alvo_x, "y": alvo_y},
    }


@app.post("/executar/homing")
def executar_homing(req: HomingReq):
    # Verifica e seta _em_movimento atomicamente para evitar race condition
    with _movimento_lock:
        if _em_movimento.is_set():
            raise HTTPException(409, "CNC em movimento — aguarde conclusão")
        _em_movimento.set()

    destino = (
        HOME[0] if req.posicao_x is None else req.posicao_x,
        HOME[1] if req.posicao_y is None else req.posicao_y,
    )
    threading.Thread(
        target=_thread_homing,
        args=(req.os_id, destino),
        daemon=True,
        name="cnc-homing",
    ).start()

    return {"ok": True, "msg": "Homing iniciado", "home": {"x": destino[0], "y": destino[1]}}


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
        "CNC Simulator v3.0 | adapter=%s | vel=%.0fmm/s | slots=1..%d | "
        "HOME=(%.0f, %.0f) | modo=%s | fator_velocidade=%.2f",
        ADAPTER_URL, VELOCIDADE_MM_S, NUM_SLOTS, HOME[0], HOME[1],
        "APRESENTACAO" if MODO_APRESENTACAO else "realista", FATOR_VELOCIDADE,
    )
    uvicorn.run(app, host="0.0.0.0", port=8200, log_level="warning")
