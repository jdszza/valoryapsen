"""
APSEN - Order Generator v1.0 (era SAP Simulator)
Gera Ordens de Saída (OS) aleatórias e envia via HTTP para o central-computer.
Sem MQTT. Sem lógica de negócio — apenas geração e envio.
"""
import logging
import os
import random
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ORDER-GEN] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

CENTRAL_URL         = os.getenv("CENTRAL_URL",         "http://central-computer:8000")
INTERVALO_OS        = int(os.getenv("INTERVALO_OS",     "90"))
# Backpressure: quanto esperar quando a fila do central está cheia, e quantas
# vezes esperar antes de desistir do ciclo (e voltar a dormir INTERVALO_OS).
ESPERA_FILA_CHEIA   = int(os.getenv("ESPERA_FILA_CHEIA", "20"))
MAX_ESPERAS_FILA    = int(os.getenv("MAX_ESPERAS_FILA",  "15"))
RELOAD_CATALOGO_MIN = int(os.getenv("RELOAD_CATALOGO_MIN", "30"))
MIN_MEDS_POR_OS     = int(os.getenv("MIN_MEDS_POR_OS",  "2"))
MAX_MEDS_POR_OS     = int(os.getenv("MAX_MEDS_POR_OS",  "6"))
QTD_MIN             = int(os.getenv("QTD_MIN",          "2"))
QTD_MAX             = int(os.getenv("QTD_MAX",         "15"))


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Catálogo ───────────────────────────────────────────────────────────────────

def _carregar_catalogo(tentativas: int = 20) -> dict:
    url = CENTRAL_URL + "/medicamentos"
    for i in range(tentativas):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            meds = resp.json()

            if not meds:
                espera = min(10, 3 + i * 2)
                logger.warning("Catálogo vazio (DB ainda inicializando?). "
                               "Tentativa %d/%d. Aguardando %ds...", i + 1, tentativas, espera)
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
                logger.warning("Nenhuma categoria válida ainda. Tentativa %d/%d.", i + 1, tentativas)
                time.sleep(5)
                continue

            logger.info("Catálogo: %d medicamentos em %d categorias.", total, len(catalogo))
            return dict(catalogo)

        except Exception as exc:
            espera = min(15, 5 + i * 2)
            logger.warning("Catálogo tentativa %d/%d falhou: %s. Aguardando %ds...",
                           i + 1, tentativas, exc, espera)
            time.sleep(espera)

    logger.error("Impossível carregar catálogo. Encerrando.")
    raise SystemExit(1)


# ── Geração de OS ──────────────────────────────────────────────────────────────

def _gerar_os(catalogo: dict) -> dict:
    categorias = list(catalogo.keys())
    pesos      = [len(catalogo[c]) for c in categorias]
    categoria  = random.choices(categorias, weights=pesos, k=1)[0]

    meds_disp    = catalogo[categoria]
    n            = min(random.randint(MIN_MEDS_POR_OS, MAX_MEDS_POR_OS), len(meds_disp))
    selecionados = random.sample(meds_disp, n)

    ts_str      = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    uid_str     = str(uuid.uuid4())[:6].upper()
    os_id       = f"OS-{ts_str}-{uid_str}"
    paciente_id = f"PAC-{random.randint(10000, 99999)}"
    lote        = f"LOTE-{random.randint(1000, 9999)}"

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
    descricao = f"{cat_desc} - {paciente_id}"

    return {
        "os_id":          os_id,
        "descricao":      descricao,
        "categoria":      categoria,
        "categoria_desc": cat_desc,
        "lote":           lote,
        "paciente_id":    paciente_id,
        "medicamentos":   medicamentos,
        "criado_em":      _ts(),
        "origem":         "ORDER_GEN_v1.0",
    }


# ── Backpressure ───────────────────────────────────────────────────────────────

def _detalhe_fila(resp) -> str:
    """Resumo legível do corpo de um 429, para o log não virar adivinhação."""
    try:
        fila = (resp.json() or {}).get("fila", {})
        return f"({fila.get('tamanho', '?')}/{fila.get('capacidade', '?')} na fila)"
    except Exception:
        return ""

def _consultar_fila() -> dict | None:
    """Ocupação da fila do central, ou None se não deu para saber."""
    try:
        r = requests.get(CENTRAL_URL + "/api/v1/fila", timeout=5)
        if r.status_code < 300:
            return r.json()
        logger.warning("[FILA] Central respondeu %d ao consultar a fila.", r.status_code)
    except Exception as exc:
        logger.warning("[FILA] Não foi possível consultar a fila: %s", exc)
    return None


def _esperar_vaga_na_fila() -> bool:
    """Espera a fila abrir vaga. True se há espaço, False se desistiu do ciclo.

    Uma OS de 6 slots leva de 90 a 140s e o gerador posta a cada 90s: sem esta
    checagem, o excesso ia bater no 429 do central toda vez. Pior sob trava do
    Triple Check, quando o orquestrador para por tempo indeterminado.

    Duas decisões deliberadas:

      * fila indisponível (central reiniciando, endpoint fora) devolve True —
        seguir e deixar o POST decidir é melhor do que travar a geração por
        causa de uma consulta auxiliar;
      * a espera é limitada a `MAX_ESPERAS_FILA` ciclos de `ESPERA_FILA_CHEIA`
        segundos. Vencido o teto, desiste DESTE ciclo e volta ao laço normal —
        nada de `sleep` apertado e nada de sair do processo.
    """
    for tentativa in range(1, MAX_ESPERAS_FILA + 1):
        fila = _consultar_fila()
        if fila is None:
            return True
        if not fila.get("cheia"):
            return True
        logger.info(
            "[FILA] Cheia (%s/%s)%s — aguardando %ds (%d/%d).",
            fila.get("tamanho", "?"), fila.get("capacidade", "?"),
            " com TRAVA ATIVA" if fila.get("trava_ativa") else "",
            ESPERA_FILA_CHEIA, tentativa, MAX_ESPERAS_FILA,
        )
        time.sleep(ESPERA_FILA_CHEIA)
    logger.warning("[FILA] Continua cheia após %d esperas — pulando este ciclo.",
                   MAX_ESPERAS_FILA)
    return False


def _enviar_os(os_payload: dict) -> bool:
    """Envia OS ao central-computer via HTTP POST. Retorna True se aceita.

    Recusa NÃO é retentada, de propósito. O central recusa em dois casos
    (ANALISE_ARQUITETURAL §4.1): 409 quando a OS já está registrada e 503
    quando não consegue persistir. Reenviar a mesma OS daria 409 para sempre;
    no 503, encheria o log enquanto o banco está fora. Cada OS nasce com
    `os_id` próprio, então a próxima do ciclo já é outra OS — aqui basta logar
    e devolver False, que o `main()` ignora antes de dormir `INTERVALO_OS`.
    """
    try:
        r = requests.post(
            CENTRAL_URL + "/api/v1/ordens",
            json=os_payload,
            timeout=15,
        )
        if r.status_code < 300:
            resp = r.json()
            logger.info("[OS] %s aceita — posição na fila: %d",
                        os_payload["os_id"], resp.get("posicao_fila", "?"))
            return True
        elif r.status_code == 409:
            logger.warning("[OS] %s já registrada no central (409). Descartada.",
                           os_payload["os_id"])
            return False
        elif r.status_code == 503:
            logger.error("[OS] %s descartada: central sem persistência (503). "
                         "A OS NÃO foi processada.", os_payload["os_id"])
            return False
        elif r.status_code == 429:
            # Fila cheia: o central nem persistiu a OS. Descartar é seguro e é
            # o que evita laço — a próxima OS nasce com os_id novo depois da
            # espera do backpressure.
            logger.warning("[OS] %s descartada: fila do central cheia (429). %s",
                           os_payload["os_id"], _detalhe_fila(r))
            return False
        else:
            logger.error("[OS] Central rejeitou OS %s: %d — %s",
                         os_payload["os_id"], r.status_code, r.text[:200])
            return False
    except Exception as exc:
        logger.error("[OS] Falha ao enviar OS %s: %s", os_payload["os_id"], exc)
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    logger.info("Aguardando central-computer (%s)...", CENTRAL_URL)
    for i in range(40):
        try:
            r = requests.get(CENTRAL_URL + "/ping", timeout=5)
            if r.status_code < 300:
                logger.info("Central disponível.")
                break
        except Exception:
            pass
        logger.info("  central não disponível ainda (%d/40)...", i + 1)
        time.sleep(5)
    else:
        logger.error("Central não disponível após 40 tentativas. Encerrando.")
        raise SystemExit(1)

    catalogo      = _carregar_catalogo()
    ultimo_reload = time.time()

    total_meds = sum(len(v) for v in catalogo.values())
    logger.info("Order Generator v1.0 pronto — OS a cada %ds | %d medicamentos disponíveis",
                INTERVALO_OS, total_meds)

    while True:
        # Recarrega catálogo periodicamente
        if time.time() - ultimo_reload > RELOAD_CATALOGO_MIN * 60:
            try:
                catalogo      = _carregar_catalogo(tentativas=3)
                ultimo_reload = time.time()
            except Exception as exc:
                logger.warning("Falha ao recarregar catálogo: %s. Usando anterior.", exc)

        try:
            if not catalogo:
                catalogo      = _carregar_catalogo()
                ultimo_reload = time.time()

            # Backpressure ANTES de gerar: OS gerada e descartada só polui log
            # e queima os_id. Se a fila não abrir, este ciclo passa em branco.
            if not _esperar_vaga_na_fila():
                time.sleep(INTERVALO_OS)
                continue

            os_payload = _gerar_os(catalogo)

            meds      = os_payload["medicamentos"]
            itens_str = " | ".join(f"{m['medicamento']} x{m['quantidade']}" for m in meds)
            logger.info("[OS] %s | cat=%s | %d medicamentos",
                        os_payload["os_id"], os_payload["categoria"], len(meds))
            logger.info("     %s", itens_str)

            _enviar_os(os_payload)

        except Exception as exc:
            logger.error("Erro ao gerar/enviar OS: %s", exc, exc_info=True)

        time.sleep(INTERVALO_OS)


if __name__ == "__main__":
    main()
