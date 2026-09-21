"""
APSEN - Computador Central v3.2
Orquestrador ativo: recebe OS do erp-simulator, comanda adapters,
consolida eventos, persiste no DB, serve dashboard/manutenção via REST +
WebSocket.

Comunicação: REST/HTTP/WebSocket — sem MQTT.

Novidades v3.2:
  - Weight adapter + HX711 simulator (balança de mesa)
  - Triple Check: valida dispenser × câmera_mesa × balança após cada dispensa
    → 2+ divergências ativam trava de emergência (bloqueia OS)
  - GET /api/v1/trava — estado da trava
  - POST /api/v1/admin/liberar-trava — libera trava (role admin ou supervisor;
    `em_nome_de` opcional registra quem liberou pela bancada)
  - GET /api/v1/visao/historico — histórico de leituras CV
  - POST /api/v1/eventos/peso — eventos da balança HX711

Endpoints de entrada (dos adapters e erp-simulator):
  POST /api/v1/ordens              ← erp-simulator
  POST /api/v1/eventos/dispenser   ← dispenser-adapter
  POST /api/v1/eventos/cnc         ← cnc-adapter
  POST /api/v1/eventos/visao       ← vision-adapter
  POST /api/v1/eventos/peso        ← weight-adapter

Endpoints de leitura (dashboard, manut_web):
  GET  /estado, /os/*, /dispensers/estado, /medicamentos, ...
  GET  /api/v1/trava
  GET  /api/v1/visao/historico
  WS   /ws

Console de operação (interface própria do central, ver `console.py`):
  GET  /console                    ← página; exige cookie de sessão
  GET/POST /console/login          ← senha própria (CONSOLE_SENHA)
  POST /console/api/*              ← disparo de OS, pausa do gerador, trava
  GET/POST /console/api/injecao    ← falha armada para demonstração (`injecao.py`)
  POST /console/api/reset          ← devolve a bancada ao estado de boot
  POST /console/api/seed           ← histórico FABRICADO (`seed_demo.py`)
  GET  /console/prevoo             ← tela de pré-voo (`prevoo.py`)
  GET  /api/v1/gerador             ← flag de pausa que o erp-simulator consulta
"""
import asyncio
import collections
import copy
import csv
import io
import itertools
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import (Depends, FastAPI, HTTPException, Request, status,
                     WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

import httpx

import console
import injecao
import necessidades
import prevoo
import seed_demo
import orchestrator as orch
import os_templates
from auth import criar_token, decodificar_token, verificar_senha
from config import settings, validar_secret_key
from database import (
    ROLES_VALIDAS,
    atualizar_item_os, atualizar_status_ordem,
    get_historico_visao, salvar_leitura_visao,
    atualizar_usuario, criar_usuario, expurgar_dados_antigos, fechar_pool,
    get_alarmes, get_alarmes_por_os, get_cnc_recentes, get_dispensas, get_dispensas_recentes,
    get_dispensers_estado,
    get_historico_ordens, get_historico_sensor, get_log_manutencao,
    get_ordem_ativa, get_ordem_por_id, get_total_alarmes_ativos, get_ultimas_leituras,
    get_usuario, get_usuarios,
    fechar_os_orfas,
    init_db, limpar_dispenser_estado, listar_categorias, listar_medicamentos,
    resolver_alarme,
    salvar_alarme, salvar_cnc_evento, salvar_dispensa, salvar_dispenser_estado,
    diagnosticar,
    salvar_leitura_sensor, salvar_manutencao, salvar_ordem,
    semear_historico_demo,
    toggle_usuario_ativo,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [CENTRAL] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Estado em memória ──────────────────────────────────────────────────────────
_estado = {
    "cnc": {
        "status":         "idle",
        "os_id":          None,
        "dispenser_alvo": None,
        "posicao_x":      0.0,
        "posicao_y":      0.0,
        "ciclo_atual":    0,
        "total_ciclos":   0,
    },
    "os_ativa":     None,
    "dispensers": {
        str(i): {
            "status":                "idle",
            "medicamento":           None,
            "sku":                   None,
            "categoria":             None,
            "quantidade":            0,
            "quantidade_alvo":       0,
            "quantidade_dispensada": 0,
            "quantidade_residual":   0,
            "os_id":                 None,
        }
        for i in range(1, settings.NUM_SLOTS + 1)
    },
    "fila_os":       [],
    "fila_tamanho":  0,
    # Teto de OS esperando (MAX_FILA_OS). Vai no payload para o dashboard saber
    # quando a fila encostou no limite — o KPI mostra só a contagem, e usa este
    # número para acender o alerta de fila cheia e explicá-lo no tooltip.
    "fila_capacidade": settings.MAX_FILA_OS,
    # Publicação do interruptor do erp-simulator, cuja fonte é
    # `console._pausado` (escrito só por `console.definir_pausa`). Vive no
    # snapshot pelo mesmo motivo da trava: o console precisa ver a pausa ao
    # vivo, e o `/ws` já entrega o snapshot a cada transição — publicar aqui
    # evita um polling só para um booleano.
    "gerador_pausado": False,
    # Modo de demonstração em vigor. Publicado no snapshot pelo mesmo motivo da
    # trava e da pausa do gerador: quem opera precisa ver na tela, ao vivo, se o
    # acaso está desligado. Um painel que não diz isso faz a demo sem imprevisto
    # passar por hardware perfeito — e, no dia seguinte, faz alguém procurar
    # defeito na planta porque "ontem não falhava".
    "modo_apresentacao": settings.MODO_APRESENTACAO,
    "fator_velocidade":  settings.FATOR_VELOCIDADE,
    # Gatilho de falha armado no console, ou `None`. É PUBLICAÇÃO: a fonte é
    # `injecao._armada`, e quem escreve aqui é o mesmo par de funções que arma
    # e que consome. Vive no snapshot pelo mesmo motivo da trava — o console
    # tem que mostrar o gatilho ao vivo, e quem explica a planta tem que ver o
    # gatilho sumir no instante em que a falha saiu.
    "falha_armada": None,
    "atribuicao_ia": [],
    # Derivado do banco por _contar_alarmes_ativos() — nunca incrementado à mão.
    "alarmes_ativos": 0,
    # Trava de Triple Check (bloqueio de OS até intervenção de supervisor)
    "trava": {
        "ativa":   False,
        "os_id":   None,
        "slot_id": None,
        "motivo":  "",
    },
    # Última leitura da balança HX711.
    #
    # Dois relógios convivem aqui, e eles NÃO se misturam. `ultima_leitura`,
    # `slot_id`, `peso_medido_g`, `peso_esperado_g`, `desvio_pct` e `ts` são a
    # última PESAGEM de slot (tara_ok / peso_ok / peso_divergencia) — é o que o
    # Triple Check e o cartão da balança leem. `peso_atual_g` / `peso_atual_ts`
    # são a leitura CONTÍNUA que a placa manda como `telemetria`, entre uma
    # pesagem e outra. Telemetria só escreve no seu próprio par de campos:
    # deixá-la encostar nos da pesagem faria o peso ao vivo apagar o veredito
    # da última dispensa — a regra "fontes de verdade" do README, aplicada à
    # balança.
    "peso": {
        "ultima_leitura":  None,
        "slot_id":         None,
        "ts":              None,
        "peso_atual_g":    None,
        "peso_atual_ts":   None,
    },
    # Última leitura de cada uma das TRÊS câmeras (atualizado por
    # _handle_evento_visao). Uma câmera por fileira de dispensers — que é o que
    # deixa "a câmera da esquerda parou de ler" visível no painel em vez de
    # virar uma sequência de falhas em D1..D4 — e a da mesa, sobre a balança.
    # As chaves são "camera_" + o valor do campo `camera` do evento.
    "visao": {
        "camera_dispenser_esq": {
            "ultima_leitura": None,   # tipo do último evento
            "slot_id":        None,
            "match_sku":      None,
            "confianca":      None,
            "ts":             None,
        },
        "camera_dispenser_dir": {
            "ultima_leitura": None,
            "slot_id":        None,
            "match_sku":      None,
            "confianca":      None,
            "ts":             None,
        },
        "camera_mesa": {
            "ultima_leitura":      None,
            "slot_id":             None,
            "quantidade_detectada": None,
            "quantidade_esperada":  None,
            "confianca":           None,
            "ts":                  None,
        },
    },
}
_lock = threading.Lock()
_log_eventos: collections.deque = collections.deque(maxlen=100)

# ── Event loop global (set na lifespan) ───────────────────────────────────────
_loop: Optional[asyncio.AbstractEventLoop] = None


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inteiro(valor, padrao: int = 0) -> int:
    """O que veio no JSON, como int — nunca uma exceção.

    Os payloads dos adapters atravessam SEM interpretação (é o contrato), então
    um campo numérico pode chegar `null`, string ou float. Quem formata com
    `:+d` ou faz aritmética com ele precisa de um int, e levantar aqui
    transforma um campo torto em 500 na rota de eventos — que o adapter retenta,
    e o evento que ele trazia se perde de vez.
    """
    try:
        return int(valor)
    except (TypeError, ValueError):
        return padrao


def _log(tipo: str, msg: str, dados: dict = None):
    _log_eventos.appendleft({"tipo": tipo, "msg": msg, "dados": dados or {}, "ts": _ts()})


# ── WebSocket Manager ──────────────────────────────────────────────────────────
class WSManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        msg = json.dumps(data, default=str)
        dead = []
        # `list(...)`: há um `await` dentro do laço, e nesse ponto o loop pode
        # rodar o handler de outro cliente — que entra ou sai da lista. Iterar
        # a lista viva levanta `RuntimeError: list changed size during
        # iteration` no meio do broadcast, e aí NINGUÉM recebe o resto do
        # snapshot: um cliente conectando derrubaria a atualização de todos os
        # outros. É a mesma razão do `deepcopy` em `get_estado`.
        for ws in list(self.active):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


_ws_manager = WSManager()
_broadcast_q: asyncio.Queue = asyncio.Queue()


async def _broadcast_worker():
    while True:
        data = await _broadcast_q.get()
        await _ws_manager.broadcast(data)


def _enfileirar_broadcast(data: dict):
    """Thread-safe: pode ser chamado de qualquer thread."""
    if _loop and not _loop.is_closed():
        _loop.call_soon_threadsafe(_broadcast_q.put_nowait, data)


# ── Throttle do broadcast ─────────────────────────────────────────────────────
# Cada broadcast custa um `copy.deepcopy` do estado inteiro + serialização JSON
# + envio para todo cliente conectado. O `movendo` da CNC chega a cada 0.5s
# durante todo o movimento, e a telemetria são 12 eventos a cada 15s só do
# dispenser: pagar o preço completo por evento desses é desperdício puro, já
# que o conteúdo muda pouco e ninguém perde informação vendo 2 quadros por
# segundo em vez de 30.
#
# Eventos de alta frequência passam por `prioritario=False`: se o último envio
# foi há menos de BROADCAST_MIN_INTERVALO_MS, só marcam pendência e o
# `_broadcast_flusher` manda o snapshot corrente no próximo tique. Transição de
# verdade — trava, fim de OS, alarme, carga, dispensa — sai na hora, sem
# throttle, porque atraso ali é atraso de decisão do operador.
_BROADCAST_MIN_INTERVALO_S = settings.BROADCAST_MIN_INTERVALO_MS / 1000.0
_throttle_lock = threading.Lock()
_ultimo_broadcast: float = 0.0
_broadcast_pendente: bool = False

# Tipos periódicos, emitidos pelo equipamento independentemente de qualquer OS.
_TIPOS_ALTA_FREQUENCIA = frozenset({"movendo", "telemetria", "status"})

# Amostragem de trajetória: 1 em N "movendo" vira linha em `cnc_eventos`.
_contador_movendo = itertools.count(1)


def _amostrar_movendo() -> bool:
    """True quando este "movendo" é o escolhido da amostra (0 = nunca)."""
    n = settings.CNC_AMOSTRAGEM_MOVENDO
    return n > 0 and next(_contador_movendo) % n == 0


def _broadcast_estado(prioritario: bool = True):
    """Publica o estado. `prioritario=False` respeita o throttle."""
    global _ultimo_broadcast, _broadcast_pendente

    with _throttle_lock:
        agora = time.monotonic()
        if not prioritario and (agora - _ultimo_broadcast) < _BROADCAST_MIN_INTERVALO_S:
            _broadcast_pendente = True
            return
        _ultimo_broadcast = agora
        _broadcast_pendente = False

    with _lock:
        snap = copy.deepcopy(_estado)
    _enfileirar_broadcast({"tipo": "estado", **snap})


async def _broadcast_flusher():
    """Envia o snapshot que o throttle segurou — no máximo um por intervalo.

    Sem isso, a última posição antes de uma pausa ficaria retida até o próximo
    evento: o dashboard mostraria a CNC parada onde ela não está mais.
    """
    while True:
        await asyncio.sleep(_BROADCAST_MIN_INTERVALO_S)
        with _throttle_lock:
            pendente = _broadcast_pendente
        if pendente:
            _broadcast_estado()


# ── Helpers DB async ──────────────────────────────────────────────────────────
#
# Escrita perdida em silêncio foi o que escondeu o `medicamento` nulo por tempo
# demais: `salvar_dispensa` levava None numa coluna NOT NULL, o INSERT falhava
# com 1048, e o único rastro era um `warning` no meio de centenas de linhas de
# evento. Nem toda escrita pesa o mesmo, e a diferença não é de gravidade
# abstrata — é de quem conserta:
#
#   - telemetria e `dispenser_estado` são MEDIÇÕES REPETIDAS: o valor seguinte
#     chega em 15s e reescreve a linha. Perder uma é perder um quadro;
#   - `salvar_dispensa` e `atualizar_item_os` são o rastro NOMINAL de quem
#     recebeu o quê — fatos que acontecem UMA vez e que ninguém reemite. A
#     linha perdida some do relatório da OS (`GET /os/{os_id}`, CSV/XLSX), que
#     é o documento de maior valor que este sistema produz, e o item fica com o
#     progresso errado para sempre.
#
# Por isso as duas de baixo viram alarme: o que não pode continuar é a planta
# seguir dispensando enquanto o banco recusa justamente as escritas que provam
# o que ela dispensou.
def _escrita_critica(fn) -> str | None:
    """Nome da escrita, se ela for uma das duas críticas; senão, None.

    Comparação por IDENTIDADE, como `_abriu_alarme` já faz, e não por
    `fn.__name__`: o nome é do objeto que chegou, e quem instrumentar ou
    duplicar `salvar_dispensa` — a suíte faz isso — passaria a entregar aqui
    uma função com outro nome. A regra deixaria de valer justamente onde ela é
    exercitada, e sem vermelho nenhum.
    """
    if fn is salvar_dispensa:
        return "salvar_dispensa"
    if fn is atualizar_item_os:
        return "atualizar_item_os"
    return None


async def _db(fn, *args) -> bool:
    """Executa função síncrona de DB em threadpool — não bloqueia o event loop.

    Devolve False quando a escrita falhou.
    """
    try:
        await asyncio.to_thread(fn, *args)
        return True
    except Exception as exc:
        critica = _escrita_critica(fn)
        if critica is None:
            logger.warning("[DB] %s(%s): %s", getattr(fn, "__name__", fn),
                           args[:2], exc)
            return False

        # O payload INTEIRO no log, e não `args[:2]`: quem for reconstruir a
        # linha à mão precisa dos números, não do os_id e do slot.
        logger.error("[DB] FALHA CRITICA: %s(%r) — %s", critica, args, exc)
        # O alarme é outra escrita no MESMO banco: com o MySQL fora ele falha
        # junto, e tudo bem — o log já registrou. O que não pode acontecer é
        # essa segunda falha derrubar o handler, que é o único caminho de volta
        # do adapter ao orquestrador. Daí o try/except próprio; e ele NÃO
        # chama `_db` de novo, para não recursar sobre a mesma indisponibilidade.
        try:
            await asyncio.to_thread(
                salvar_alarme, "central", "persistencia_falhou",
                f"{critica} falhou: {exc} | args={args!r}"[:500])
        except Exception as exc_alarme:
            logger.error("[DB] o alarme de persistencia_falhou também falhou: %s",
                         exc_alarme)
        else:
            # Alarme aberto FORA das `db_tasks`: `_abriu_alarme` não o vê, e o
            # badge ficaria até `_ALARMES_TTL_S` sem ele.
            await _atualizar_alarmes_ativos(forcar=True)
        return False


# ── Alarmes ativos: derivado do banco, com cache curto ────────────────────────
# `alarmes_ativos` já foi um contador em memória incrementado nos handlers. Ele
# só subia: resolver um alarme pelo app de manutenção não o baixava, e um
# restart o zerava mesmo com alarmes abertos no banco. Agora o valor é sempre uma leitura de
# `get_total_alarmes_ativos()`.
#
# O número vai em TODO payload de `_broadcast_estado()`, que roda a cada evento
# — só a telemetria periódica são NUM_SLOTS eventos a cada 15s (8 na célula
# de duas fileiras), mais CNC, visão e peso.
# Uma query por evento seria desperdício, então a leitura é cacheada por
# `_ALARMES_TTL_S`.
#
# Por que TTL, e não invalidar só quando o central cria/resolve um alarme: o
# central NÃO é o único a escrever na tabela. `orchestrator.py` grava alarmes
# de trava do Triple Check, de abort de OS e de falha de limpeza chamando
# `salvar_alarme` direto, sem passar por aqui — um cache invalidado apenas
# pelos caminhos deste módulo ficaria permanentemente atrasado em relação a
# eles. O TTL converge sozinho, seja quem for que escreveu. Os pontos que o
# central conhece (handler que abriu alarme, resolução pelo app de manutenção,
# startup) passam `forcar=True` para não fazer o badge esperar a janela.
_ALARMES_TTL_S = 5.0
_alarmes_lock = threading.Lock()
_alarmes_cache = {"valor": 0, "lido_em": None}   # lido_em None = nunca lido


def _alarmes_em_cache() -> Optional[int]:
    """Valor cacheado ainda válido, ou None se nunca lido / expirado."""
    with _alarmes_lock:
        lido_em = _alarmes_cache["lido_em"]
        if lido_em is None or (time.monotonic() - lido_em) >= _ALARMES_TTL_S:
            return None
        return _alarmes_cache["valor"]


def _contar_alarmes_ativos(forcar: bool = False) -> int:
    """Relê o total de alarmes abertos e publica em `_estado`.

    Bloqueante (toca o banco): chamar de endpoint síncrono ou via
    `asyncio.to_thread`. Nunca chamar com `_lock` em mãos — a função o adquire.
    """
    if not forcar:
        cacheado = _alarmes_em_cache()
        if cacheado is not None:
            return cacheado

    try:
        total = int(get_total_alarmes_ativos() or 0)
    except Exception as exc:
        # Banco fora do ar não pode derrubar o handler do evento: mantém o
        # último valor conhecido e tenta de novo na próxima chamada.
        logger.warning("[DB] get_total_alarmes_ativos: %s", exc)
        with _alarmes_lock:
            return _alarmes_cache["valor"]

    with _alarmes_lock:
        _alarmes_cache.update({"valor": total, "lido_em": time.monotonic()})
    with _lock:
        _estado["alarmes_ativos"] = total
    return total


async def _atualizar_alarmes_ativos(forcar: bool = False) -> int:
    """Versão para o event loop: só vai ao threadpool se o cache não servir."""
    if not forcar:
        cacheado = _alarmes_em_cache()
        if cacheado is not None:
            return cacheado
    return await asyncio.to_thread(_contar_alarmes_ativos, forcar)


def _abriu_alarme(db_tasks: list) -> bool:
    """Algum dos writes deste evento é um alarme novo?

    Lido das próprias `db_tasks` em vez de uma flag por handler: ponto de
    alarme novo entra na conta sozinho, sem depender de alguém lembrar.
    """
    return any(fn is salvar_alarme for fn, _ in db_tasks)


# ── Handlers de eventos vindos dos adapters ────────────────────────────────────

async def _handle_evento_dispenser(payload: dict):
    """
    Processa eventos do dispenser-adapter e notifica o orquestrador.
    Atualização de estado (rápida, sob _lock) é síncrona.
    Escrita em DB é assíncrona (asyncio.to_thread) — não bloqueia o loop.
    Tipos: status, carregado, dispensado, erro, limpeza_ok, telemetria

    Divisão de responsabilidade (README, "Fontes de verdade"):
      - o simulador é fonte de verdade sobre HARDWARE/ESTOQUE
        (medicamento, sku, categoria, quantidade);
      - o orquestrador é fonte de verdade sobre FLUXO
        (status da etapa e os_id em execução).
    O evento "status" é telemetria periódica (a cada 15s, para todos os slots)
    e só pode tocar na primeira categoria. Os eventos de transição — carregado,
    dispensado, limpeza_ok, erro — é que movem o fluxo.
    """
    tipo    = payload.get("tipo", "status")
    disp_id = payload.get("dispenser_id")
    os_id   = payload.get("os_id")

    if disp_id is None:
        return

    disp_key = str(disp_id)
    db_tasks = []  # (fn, args) a executar fora do lock

    # ── Atualiza estado em memória (síncrono, sem await) ──────────────────
    with _lock:
        if disp_key not in _estado["dispensers"]:
            return
        d = _estado["dispensers"][disp_key]

        if tipo == "status":
            # Telemetria periódica: só estoque. Escrever "status"/"os_id" aqui
            # desfazia, a cada 15s, o reset de fim de OS do orquestrador — o
            # slot voltava a "concluido" para sempre e a limpeza dava 409.
            #
            # E `null` num periódico é "não informado", não "não tem". O
            # firmware esvazia o slot e emite `status` com medicamento nulo
            # ANTES do `dispensado` que fecha a etapa: apagar o nome aqui
            # fazia a dispensa chegar sem medicamento, e `dispensas.medicamento`
            # é VARCHAR(100) NOT NULL — o INSERT falhava com 1048 e a linha da
            # dispensa simplesmente não existia. Quem apaga o nome é a
            # TRANSIÇÃO (`dispensado` com resíduo 0, `limpeza_ok`), que sabe
            # que o slot esvaziou de verdade; o periódico só confirma.
            qty = payload.get("quantidade", 0)
            for campo in ("medicamento", "sku", "categoria"):
                valor = payload.get(campo)
                if valor is not None:
                    d[campo] = valor
            d["quantidade"] = qty
            # A LINHA do banco, essa sim, segue a quantidade: slot vazio não
            # guarda medicamento em `dispenser_estado`.
            med_db = d.get("medicamento") if (qty or 0) > 0 else None
            cat_db = d.get("categoria") if (qty or 0) > 0 else None
            # ultima_os_id vem do fluxo que o central conhece, não do payload.
            db_tasks.append((salvar_dispenser_estado,
                             (int(disp_id), qty or 0, d["os_id"], med_db, cat_db)))

        elif tipo == "carregado":
            med = payload.get("medicamento")
            qty = payload.get("quantidade_total", payload.get("quantidade", 0))
            d.update({
                "status":      "pronto",
                "medicamento": med,
                "quantidade":  qty,
                "os_id":       os_id,
            })
            db_tasks.append((salvar_dispenser_estado,
                             (int(disp_id), qty or 0, os_id,
                              med if (qty or 0) > 0 else None,
                              payload.get("categoria"))))

        elif tipo == "dispensado":
            qtd_disp  = payload.get("quantidade_dispensada", 0)
            qtd_alvo  = payload.get("quantidade_alvo", 0)
            residual  = payload.get("quantidade_residual", 0)
            # Dispenser envia 'falha_mecanica' (bool) — 'validado' é derivado
            falha_mec = payload.get("falha_mecanica", False)
            validado  = not falha_mec and (qtd_disp >= qtd_alvo)
            falha     = payload.get("motivo_falha")
            # `or`, e não `d.get(chave, default)`: o default do `get` NÃO
            # entra quando a chave existe com valor None — que é exatamente o
            # caminho normal, o do slot que acabou de esvaziar. Sem isto,
            # `salvar_dispensa` recebia None numa coluna NOT NULL e a linha
            # nunca era gravada: o relatório da OS saía sem a dispensa que ela
            # de fato fez, e o único rastro era um warning.
            med       = (d.get("medicamento") or payload.get("medicamento")
                         or "(desconhecido)")

            d.update({
                "status":                "concluido" if qtd_disp >= qtd_alvo else "dispensando",
                "quantidade_dispensada": qtd_disp,
                "quantidade_residual":   residual,
                "quantidade":            residual,
            })
            if residual == 0:
                d["medicamento"] = None
                d["sku"]         = None
                d["categoria"]   = None

            med_db = d["medicamento"] if residual > 0 else None
            cat_db = d.get("categoria") if residual > 0 else None
            db_tasks.append((salvar_dispensa,
                             (os_id, disp_id, med, qtd_disp, qtd_alvo, validado, falha)))
            db_tasks.append((salvar_dispenser_estado,
                             (int(disp_id), residual, os_id, med_db, cat_db)))
            if os_id:
                status_item = "concluido" if qtd_disp >= qtd_alvo else "em_andamento"
                db_tasks.append((atualizar_item_os, (os_id, disp_id, qtd_disp, status_item)))
            if not validado:
                db_tasks.append((salvar_alarme,
                                 (f"dispenser_{disp_id}", "falha_validacao",
                                  falha or f"Remédio rejeitado D{disp_id}")))

        elif tipo == "erro":
            d["status"] = "erro"
            descricao = payload.get("descricao", f"Erro no dispenser {disp_id}")
            db_tasks.append((salvar_alarme,
                             (f"dispenser_{disp_id}",
                              payload.get("codigo_erro", "erro"), descricao)))
            _log("alarme", f"ERRO D{disp_id}: {descricao}")

        elif tipo == "limpeza_ok":
            d.update({"status": "limpo", "medicamento": None, "sku": None,
                      "categoria": None, "quantidade": 0, "os_id": None})
            db_tasks.append((limpar_dispenser_estado, (int(disp_id),)))
            _log("limpeza_ok", f"D{disp_id} limpo.")

        elif tipo == "telemetria":
            componente = payload.get("componente", f"dispenser_{disp_id}")
            valor      = payload.get("valor_c", payload.get("valor", 0.0))
            unidade    = payload.get("unidade", "°C")
            tipo_leit  = payload.get("tipo_leitura", "temperatura")
            db_tasks.append((salvar_leitura_sensor,
                             (componente, tipo_leit, valor, unidade)))

    # ── Escreve no DB (async, fora do lock) ───────────────────────────────
    for fn, args in db_tasks:
        await _db(fn, *args)

    # Alarme recém-gravado entra na conta agora; sem alarme, a releitura só
    # acontece se o cache de _ALARMES_TTL_S tiver expirado. Vale para os quatro
    # handlers — o valor sai daqui direto para o `_broadcast_estado()` do fim.
    await _atualizar_alarmes_ativos(forcar=_abriu_alarme(db_tasks))

    # ── Notifica orquestrador (no event loop, seguro) ─────────────────────
    if os_id:
        if tipo == "carregado":
            orch.notificar_evento(f"{os_id}:carregado:{disp_id}", payload)
        elif tipo == "dispensado":
            orch.notificar_evento(f"{os_id}:dispensado:{disp_id}", payload)
        elif tipo == "erro":
            orch.notificar_evento(f"{os_id}:carregado:{disp_id}", {**payload, "tipo": "erro"})
            orch.notificar_evento(f"{os_id}:dispensado:{disp_id}", {**payload, "tipo": "erro"})

    # Limpeza é operação de slot, não de OS: o payload de "limpeza_ok" não traz
    # os_id, então a chave é só o dispenser. O "erro" também entra aqui — é
    # como o simulador recusa a limpeza (codigo_erro=limpeza_em_operacao) e o
    # orquestrador não pode ficar esperando uma confirmação que não vem.
    if tipo == "limpeza_ok":
        orch.notificar_evento(f"limpeza:{disp_id}", payload)
    elif tipo == "erro":
        orch.notificar_evento(f"limpeza:{disp_id}", {**payload, "tipo": "erro"})

    _log(f"disp_{tipo}", f"D{disp_id}: {tipo}", payload)
    # "status" e "telemetria" são os 12 eventos periódicos a cada 15s; as
    # transições (carregado, dispensado, erro, limpeza_ok) saem na hora.
    _broadcast_estado(prioritario=tipo not in _TIPOS_ALTA_FREQUENCIA)


async def _handle_evento_cnc(payload: dict):
    """
    Processa eventos do cnc-adapter.
    Estado atualizado síncronamente sob _lock; DB writes via asyncio.to_thread.
    Tipos: movendo, posicionado, concluido, erro, retornando, telemetria
    """
    tipo       = payload.get("tipo", "movendo")
    os_id      = payload.get("os_id")
    disp_alvo  = payload.get("dispenser_alvo")
    pos_x      = payload.get("posicao_x", 0.0)
    pos_y      = payload.get("posicao_y", 0.0)
    ciclo      = payload.get("ciclo_atual", 0)
    total      = payload.get("total_ciclos", 0)
    db_tasks   = []

    with _lock:
        # Telemetria é leitura de SENSOR: não traz posição, nem alvo, nem
        # ciclo. Passando pelo `update` de baixo, os defaults do `payload.get`
        # (None e 0.0) entravam no snapshot como se fossem medida — a cada
        # leitura a mesa "voltava" para (0,0) com `dispenser_alvo` nulo, entre
        # dois eventos de movimento. E é a posição publicada que
        # `prevoo.itens_celula` compara com o HOME. O handler do dispenser já
        # separava a periódica do fluxo; este não separava.
        if tipo == "telemetria":
            componente = payload.get("componente", "cnc")
            valor      = payload.get("valor", 0.0)
            unidade    = payload.get("unidade", "°C")
            tipo_leit  = payload.get("tipo_leitura", "temperatura")
            db_tasks.append((salvar_leitura_sensor,
                             (componente, tipo_leit, valor, unidade)))
        else:
            _estado["cnc"].update({
                "status":         tipo,
                "os_id":          os_id,
                "dispenser_alvo": disp_alvo,
                "posicao_x":      pos_x,
                "posicao_y":      pos_y,
                "ciclo_atual":    ciclo,
                "total_ciclos":   total,
            })
            # `concluido` NÃO fecha OS travada, e este é o caso NORMAL do
            # Triple Check, não uma borda: a trava dispara entre dois ciclos,
            # com a mesa parada. O firmware então faz homing por conta própria
            # — é onde o supervisor espera encontrar a mesa — e emite
            # `concluido` endereçado ao `trava_os_id`, que é a OS que acabou de
            # travar. O dashboard, o `/estado` e o `/ws` passavam a mostrar
            # CONCLUÍDA a OS que está parada esperando um supervisor: a tela que
            # a pessoa que vai liberar a trava está olhando.
            #
            # A guarda mora AQUI, e não no firmware, por duas razões. Quem manda
            # no FLUXO é o central (README, "Fontes de verdade") — a mesa
            # reporta o que ela fez, não o que a OS virou. E a placa em campo
            # precisaria ser regravada; esta linha vale na bancada que já está
            # montada.
            if (tipo == "concluido" and os_id and _estado["os_ativa"]
                    and _estado["os_ativa"].get("os_id") == os_id):
                # `get_trava_estado` lê globais do orquestrador e NÃO pega
                # `_lock` — pode ser chamada daqui de dentro.
                if orch.get_trava_estado()["ativa"]:
                    logger.info(
                        "[CNC] `concluido` de %s ignorado para o status da OS: "
                        "a trava do Triple Check está ativa (é o retorno ao HOME).",
                        os_id,
                    )
                else:
                    _estado["os_ativa"]["status"] = "concluida"

            # Só TRANSIÇÃO vira linha. "movendo" chega a cada 0.5s durante todo
            # o movimento — eram centenas de linhas por OS para descrever uma
            # trajetória que o dashboard já mostra ao vivo e que ninguém
            # consulta depois. Quem quiser rastro grava amostrado
            # (CNC_AMOSTRAGEM_MOVENDO).
            if tipo in ("posicionado", "concluido", "erro"):
                db_tasks.append((salvar_cnc_evento,
                                 (os_id, tipo, disp_alvo, pos_x, pos_y, ciclo, total)))
            elif tipo == "movendo" and _amostrar_movendo():
                db_tasks.append((salvar_cnc_evento,
                                 (os_id, tipo, disp_alvo, pos_x, pos_y, ciclo, total)))

            if tipo == "erro":
                descricao = payload.get("descricao", "Erro desconhecido na CNC")
                db_tasks.append((salvar_alarme,
                                 ("cnc", payload.get("codigo_erro", "erro_cnc"),
                                  descricao)))
                _log("alarme", f"ERRO CNC: {descricao}")

    # DB fora do lock
    for fn, args in db_tasks:
        await _db(fn, *args)

    await _atualizar_alarmes_ativos(forcar=_abriu_alarme(db_tasks))

    # Notifica orquestrador
    if os_id and disp_alvo is not None:
        if tipo == "posicionado":
            orch.notificar_evento(f"{os_id}:posicionado:{disp_alvo}", payload)
        elif tipo == "erro":
            orch.notificar_evento(f"{os_id}:posicionado:{disp_alvo}",
                                  {**payload, "tipo": "erro"})

    if tipo == "telemetria":
        # Mesma razão do bloco acima: a linha de log alimenta `/log/eventos`, e
        # "CNC telemetria | DNone | (0.0,0.0)" é a mesma posição inventada,
        # só que na tela em vez do snapshot.
        _log("cnc_telemetria",
             f"CNC {payload.get('componente', 'cnc')}="
             f"{payload.get('valor', 0.0)}{payload.get('unidade', '°C')}")
    else:
        _log(f"cnc_{tipo}", f"CNC {tipo} | D{disp_alvo} | ({pos_x:.1f},{pos_y:.1f})")
    _broadcast_estado(prioritario=tipo not in _TIPOS_ALTA_FREQUENCIA)


# As duas câmeras que cobrem as fileiras de dispensers. A da mesa não entra
# aqui: o tipo do evento (leitura_mesa_*) já a identifica sozinho.
_CAMERAS_DISPENSER = ("dispenser_esq", "dispenser_dir")
_LADO = {"dispenser_esq": "esquerda", "dispenser_dir": "direita"}


def _camera_dispenser(camera: str, slot_id) -> str:
    """Qual das duas câmeras de dispenser produziu a leitura.

    O valor normal vem do próprio evento — quem sabe a geometria é o
    simulador/driver, não o central. O fallback pelo slot cobre o contrato
    ANTIGO (camera="dispenser", de quando a célula tinha uma fileira só) e o
    payload sem o campo: sem ele, a leitura não acharia chave de estado e
    sumiria do painel e do histórico, sem erro nenhum no log.
    """
    if camera in _CAMERAS_DISPENSER:
        return camera
    return ("dispenser_dir" if (slot_id or 0) > orch.SLOTS_POR_FILEIRA
            else "dispenser_esq")


async def _handle_evento_visao(payload: dict):
    """
    Processa eventos do vision-adapter (três câmeras).
    Tipos (câmeras de dispenser): leitura_dispenser_ok, leitura_dispenser_falha, leitura_dispenser_divergencia
    Tipos (câmera da mesa):       leitura_mesa_ok, leitura_mesa_falha, leitura_mesa_divergencia
    Tipo  (telemetria):           telemetria (temperatura dos componentes de câmera)

    O TIPO diz o que foi lido; o campo `camera` diz QUEM leu. As duas câmeras de
    dispenser emitem os mesmos tipos — separá-las por tipo obrigaria o
    orquestrador e o Triple Check a conhecer o lado da bancada, e eles se
    importam com o resultado, não com qual lente olhou.

    Estado atualizado síncronamente sob _lock.
    Alarmes gerados assincronamente via asyncio.to_thread.
    Orquestrador notificado via notificar_evento (call_soon_threadsafe).
    """
    tipo    = payload.get("tipo", "")
    camera  = payload.get("camera", "")
    slot_id = payload.get("slot_id")
    os_id   = payload.get("os_id")
    db_tasks = []

    with _lock:
        # ── Câmeras dos Dispensers (esquerda e direita) ────────────────────
        if tipo in (
            "leitura_dispenser_ok",
            "leitura_dispenser_falha",
            "leitura_dispenser_divergencia",
        ):
            cam = _camera_dispenser(camera, slot_id)
            _estado["visao"][f"camera_{cam}"].update({
                "ultima_leitura": tipo,
                "slot_id":        slot_id,
                "match_sku":      payload.get("match_sku"),
                "confianca":      payload.get("confianca"),
                "ts":             payload.get("ts"),
            })

            # Persiste leitura no histórico (todos os tipos de câmera dispenser)
            db_tasks.append((salvar_leitura_visao, (
                os_id, cam, slot_id, tipo,
                payload.get("sku_esperado"), payload.get("sku_lido"),
                payload.get("match_sku"), payload.get("confianca"),
                None, None, payload.get("motivo"),
            )))

            if tipo == "leitura_dispenser_falha":
                descricao = (f"Falha câmera {_LADO[cam]} slot {slot_id}: "
                             f"{payload.get('motivo', 'desconhecido')}")
                db_tasks.append((salvar_alarme,
                                 (f"camera_{cam}_{slot_id}",
                                  "falha_leitura_dispenser", descricao)))
                _log("alarme", descricao)

            elif tipo == "leitura_dispenser_divergencia":
                descricao = (f"SKU incorreto slot {slot_id}: "
                             f"esperado={payload.get('sku_esperado','?')} "
                             f"lido={payload.get('sku_lido','?')}")
                db_tasks.append((salvar_alarme,
                                 (f"camera_{cam}_{slot_id}",
                                  "divergencia_sku", descricao)))
                _log("alarme", descricao)

        # ── Câmera da Mesa (sobre a balança) ───────────────────────────────
        elif tipo in (
            "leitura_mesa_ok",
            "leitura_mesa_falha",
            "leitura_mesa_divergencia",
        ):
            _estado["visao"]["camera_mesa"].update({
                "ultima_leitura":       tipo,
                "slot_id":              slot_id,
                "quantidade_detectada": payload.get("quantidade_detectada"),
                "quantidade_esperada":  payload.get("quantidade_esperada"),
                "confianca":            payload.get("confianca"),
                "ts":                   payload.get("ts"),
            })

            # Persiste leitura no histórico (todos os tipos de câmera mesa)
            db_tasks.append((salvar_leitura_visao, (
                os_id, "mesa", slot_id, tipo,
                None, None, None, payload.get("confianca"),
                payload.get("quantidade_esperada"), payload.get("quantidade_detectada"),
                payload.get("motivo"),
            )))

            if tipo == "leitura_mesa_falha":
                descricao = (f"Câmera mesa não detectou produto slot {slot_id}: "
                             f"{payload.get('motivo', 'desconhecido')}")
                db_tasks.append((salvar_alarme,
                                 (f"camera_mesa_{slot_id}",
                                  "falha_deteccao_mesa", descricao)))
                _log("alarme", descricao)

            elif tipo == "leitura_mesa_divergencia":
                # `:+d` exige INT, e o que chega aqui é JSON de um adapter: um
                # `null` ou um `10.0` derrubava o endpoint com 500 — e aí o
                # adapter retenta 3×, então o mesmo evento derruba a rota três
                # vezes e o orquestrador nunca é notificado da divergência que
                # o evento veio contar. A divergência de contagem é uma das
                # três fontes do Triple Check: perdê-la por causa de uma
                # formatação é perder a trava.
                det = _inteiro(payload.get("quantidade_detectada"))
                esp = _inteiro(payload.get("quantidade_esperada"))
                descricao = (f"Contagem incorreta slot {slot_id}: "
                             f"esperado={esp} detectado={det} "
                             f"(Δ={det - esp:+d})")
                db_tasks.append((salvar_alarme,
                                 (f"camera_mesa_{slot_id}",
                                  "divergencia_contagem", descricao)))
                _log("alarme", descricao)

        # ── Telemetria das câmeras ──────────────────────────────────────────
        elif tipo == "telemetria":
            componente = payload.get("componente", "camera_sistema")
            valor      = payload.get("valor", 0.0)
            unidade    = payload.get("unidade", "°C")
            tipo_leit  = payload.get("tipo_leitura", "temperatura")
            db_tasks.append((salvar_leitura_sensor,
                             (componente, tipo_leit, valor, unidade)))

    # ── DB fora do lock ────────────────────────────────────────────────────
    for fn, args in db_tasks:
        await _db(fn, *args)

    await _atualizar_alarmes_ativos(forcar=_abriu_alarme(db_tasks))

    # ── Notifica orquestrador ──────────────────────────────────────────────
    if os_id and slot_id is not None:
        # Dispenser: qualquer resultado (ok, falha, divergência) desbloqueia o await
        if tipo.startswith("leitura_dispenser_"):
            orch.notificar_evento(f"{os_id}:visao_dispenser:{slot_id}", payload)
        # Mesa: idem
        elif tipo.startswith("leitura_mesa_"):
            orch.notificar_evento(f"{os_id}:visao_mesa:{slot_id}", payload)

    _log(f"visao_{tipo}", f"cam={camera} slot={slot_id}", payload)
    _broadcast_estado(prioritario=tipo not in _TIPOS_ALTA_FREQUENCIA)


async def _handle_evento_peso(payload: dict):
    """
    Processa eventos do weight-adapter (balança HX711).
    Tipos: tara_ok, peso_ok, peso_divergencia, erro_sensor, telemetria
    """
    tipo    = payload.get("tipo", "")
    slot_id = payload.get("slot_id")
    os_id   = payload.get("os_id")
    db_tasks = []

    with _lock:
        if tipo in ("peso_ok", "peso_divergencia", "tara_ok"):
            _estado["peso"].update({
                "ultima_leitura":   tipo,
                "slot_id":          slot_id,
                "peso_medido_g":    payload.get("peso_medido_g"),
                "peso_esperado_g":  payload.get("peso_esperado_g"),
                "desvio_pct":       payload.get("desvio_pct"),
                "ts":               payload.get("ts"),
            })

        if tipo == "peso_divergencia":
            descricao = (
                f"Divergência de peso slot {slot_id}: "
                f"esperado={(payload.get('peso_esperado_g') or 0):.1f}g "
                f"medido={(payload.get('peso_medido_g') or 0):.1f}g "
                f"(desvio={(payload.get('desvio_pct') or 0):.1f}%)"
            )
            db_tasks.append((salvar_alarme,
                             (f"balanca_{slot_id}", "divergencia_peso", descricao)))
            _log("alarme", descricao)

        elif tipo == "erro_sensor":
            descricao = f"Sensor HX711 falhou: {payload.get('descricao', '')}"
            db_tasks.append((salvar_alarme,
                             ("balanca", "erro_sensor_peso", descricao)))

        elif tipo == "telemetria":
            componente = payload.get("componente", "hx711_balanca_mesa")
            db_tasks.append((salvar_leitura_sensor,
                             (componente, "temperatura", payload.get("temperatura_c", 0), "°C")))
            # `peso_atual_g` atravessava a placa, o adapter e o endpoint — e
            # morria aqui: só a temperatura era gravada, e `_estado["peso"]`
            # só mudava nas pesagens de slot. O peso ao vivo nunca chegava ao
            # dashboard nem ao banco, e é o dado que a bancada mais quer ver
            # entre uma pesagem e outra.
            #
            # Vai para o banco no mesmo padrão da temperatura (uma linha em
            # `leituras_sensores`, tipo "peso") e para o estado num par de
            # campos PRÓPRIO. Os campos da pesagem (`ultima_leitura`,
            # `peso_medido_g`, `peso_esperado_g`, `desvio_pct`, `ts`) ficam
            # intocados: telemetria não pode desfazer o fluxo.
            peso_atual = payload.get("peso_atual_g")
            if peso_atual is not None:
                db_tasks.append((salvar_leitura_sensor,
                                 (componente, "peso", peso_atual, "g")))
                _estado["peso"]["peso_atual_g"]  = peso_atual
                _estado["peso"]["peso_atual_ts"] = payload.get("ts")

    for fn, args in db_tasks:
        await _db(fn, *args)

    await _atualizar_alarmes_ativos(forcar=_abriu_alarme(db_tasks))

    # Notifica orquestrador (para tara e pesagem de slots)
    if os_id:
        if tipo == "tara_ok":
            orch.notificar_evento(f"{os_id}:tara", payload)
        elif tipo in ("peso_ok", "peso_divergencia", "erro_sensor") and slot_id is not None:
            orch.notificar_evento(f"{os_id}:peso:{slot_id}", payload)

    _log(f"peso_{tipo}", f"slot={slot_id}", payload)
    _broadcast_estado(prioritario=tipo not in _TIPOS_ALTA_FREQUENCIA)


# ── Expurgo periódico do histórico ────────────────────────────────────────────

async def _loop_expurgo():
    """Apaga `cnc_eventos` e `leituras_sensores` além da retenção.

    Roda no startup (banco herdado de versão sem expurgo pode chegar grande) e
    depois a cada `EXPURGO_INTERVALO_HORAS`. Falha de expurgo é registrada e
    ignorada: é manutenção, não pode derrubar a operação.
    """
    intervalo = max(settings.EXPURGO_INTERVALO_HORAS, 0.01) * 3600
    while True:
        try:
            removidos = await asyncio.to_thread(
                expurgar_dados_antigos, settings.RETENCAO_DIAS
            )
            if any(removidos.values()):
                _log("expurgo", f"Histórico expurgado (> {settings.RETENCAO_DIAS}d): "
                                + ", ".join(f"{t}={n}" for t, n in removidos.items()))
        except Exception as exc:
            logger.warning("[DB] Expurgo falhou: %s", exc)
        await asyncio.sleep(intervalo)


# ── Ordens padrão ──────────────────────────────────────────────────────────────

def _catalogo_por_nome(medicamentos: list) -> dict:
    """Catálogo indexado por nome — a chave com que os templates citam itens."""
    return {m["nome"]: m for m in (medicamentos or []) if m.get("nome")}


async def _diagnosticar_templates() -> tuple[list, dict | None]:
    """Diagnóstico das 10 ordens padrão: `(problemas, catálogo)`.

    Duas checagens, e a segunda só vale se o banco respondeu: estrutura (faixa
    de itens, quantidades, duplicidade) e existência de cada medicamento na
    tabela `medicamentos`. Catálogo indisponível volta como `None` — e não como
    `{}` — para ninguém ler "nenhum problema" como "os templates estão válidos".

    Devolve o catálogo junto porque o endpoint precisa dele para enriquecer os
    itens: separá-los custaria uma segunda varredura da tabela por chamada.
    """
    problemas = os_templates.validar_estrutura(num_slots=settings.NUM_SLOTS)
    try:
        catalogo = _catalogo_por_nome(await asyncio.to_thread(listar_medicamentos))
    except Exception as exc:
        logger.warning("[TEMPLATES] Catálogo indisponível (%s) — "
                       "validação contra `medicamentos` não foi feita.", exc)
        return problemas, None

    if not catalogo:
        return problemas, None
    return problemas + os_templates.validar_contra_catalogo(catalogo), catalogo


async def _fechar_os_orfas_no_boot() -> None:
    """Fecha em `erro` toda OS deixada em `em_andamento` por um processo morto.

    Um alarme POR LINHA, e não um agregado: a tela de necessidades agrupa por
    fonte, e o `os_id` é o que permite ir ao relatório daquela OS e ver até
    onde ela chegou. Banco fora não derruba o boot — a reconciliação roda de
    novo no startup seguinte, e até lá o pior é o sintoma que já existia.
    """
    try:
        orfas = await asyncio.to_thread(fechar_os_orfas)
    except Exception as exc:
        logger.warning("[BOOT] Não foi possível reconciliar OS órfãs (%s) — "
                       "`GET /os/ativa` pode anunciar uma OS de um processo "
                       "anterior até o próximo startup.", exc)
        return

    if not orfas:
        return

    logger.error("[BOOT] %d OS estavam em 'em_andamento' de um processo anterior "
                 "e foram fechadas em 'erro': %s", len(orfas), ", ".join(orfas))
    _log("os_orfa_no_boot",
         f"{len(orfas)} OS de um processo anterior fechada(s) em erro")
    for os_id in orfas:
        await _db(salvar_alarme, "central", "os_orfa_no_boot",
                  f"OS {os_id} ficou em 'em_andamento' de um central que caiu no "
                  f"meio do ciclo e foi fechada em 'erro' no boot. O que ela "
                  f"chegou a dispensar está em `dispensas`.")
    await _atualizar_alarmes_ativos(forcar=True)


async def _validar_templates_no_boot() -> None:
    problemas, catalogo = await _diagnosticar_templates()
    if problemas:
        logger.error("[TEMPLATES] %d problema(s) nas ordens padrão:\n  - %s",
                     len(problemas), "\n  - ".join(problemas))
        _log("templates_invalidos",
             f"{len(problemas)} problema(s) nas 10 ordens padrão — ver log do central")
    elif catalogo is not None:
        logger.info("[TEMPLATES] %d ordens padrão válidas contra o catálogo.",
                    len(os_templates.TEMPLATES))


# ── FastAPI Lifespan ───────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    # FIX: get_running_loop() em vez de get_event_loop() (deprecado em Python ≥3.10)
    _loop = asyncio.get_running_loop()

    await asyncio.to_thread(init_db)

    # Alarmes abertos sobrevivem ao processo: sem esta leitura o badge nasce em
    # 0 depois de todo restart, com o banco cheio de alarmes por resolver.
    await _atualizar_alarmes_ativos(forcar=True)

    # Injeta loop no orquestrador para notificar_evento thread-safe
    orch.inicializar(_estado, _lock, _broadcast_estado, _loop)

    # Segredo fraco derruba o boot (fora de APSEN_ENV=dev). O valor default é
    # público neste repositório: com ele qualquer um forja um JWT role=admin.
    validar_secret_key(settings.SECRET_KEY, settings.APSEN_ENV)

    # OS que ficaram em `em_andamento` são de um central que morreu no meio do
    # ciclo — o orquestrador é um loop ÚNICO, então no boot não existe OS em
    # execução por definição. Elas não somem sozinhas, e `get_ordem_ativa`
    # prefere `em_andamento ORDER BY criado_em ASC`: a órfã mais ANTIGA vira "a
    # OS ativa" para sempre, no `GET /os/ativa`, no app de manutenção e no
    # espelho do painel de bancada.
    await _fechar_os_orfas_no_boot()

    # As 10 ordens padrão contra o catálogo REAL, agora que o banco está de pé.
    # Aqui é WARNING, não queda: o central serve a planta inteira e derrubá-lo
    # por um nome de medicamento digitado errado no template trocaria um
    # problema de demonstração por um de produção. Quem RECUSA disparar uma OS
    # inválida é o erp-simulator, no startup dele, e o console vê o mesmo
    # diagnóstico em `GET /api/v1/ordens/templates`.
    await _validar_templates_no_boot()

    task_broadcast = asyncio.create_task(_broadcast_worker())
    task_flusher   = asyncio.create_task(_broadcast_flusher())
    task_orch      = asyncio.create_task(orch.loop_orquestrador())
    task_expurgo   = asyncio.create_task(_loop_expurgo())

    if settings.MODO_APRESENTACAO:
        logger.warning(
            "MODO APRESENTACAO LIGADO — os simuladores não sorteiam falha. "
            "O Triple Check só trava por falha INJETADA de propósito.")
    if settings.FATOR_VELOCIDADE != 1.0:
        logger.warning(
            "FATOR_VELOCIDADE=%.2f — timeouts de orquestração escalados por "
            "%.2f (carregamento=%.0fs, dispensa=%.0fs).",
            settings.FATOR_VELOCIDADE, max(1.0, settings.FATOR_VELOCIDADE),
            settings.TIMEOUT_CARREGAMENTO, settings.TIMEOUT_DISPENSA)

    logger.info("Computador Central APSEN v3.1 iniciado.")
    yield

    for tarefa in (task_broadcast, task_flusher, task_orch, task_expurgo):
        tarefa.cancel()
    await orch.encerrar()
    # As conexões do pool sobrevivem a cada operação de propósito; no shutdown
    # não há mais operação seguinte, e deixá-las abertas faz o MySQL segurar os
    # slots até o `wait_timeout` — reinício em laço encosta no max_connections.
    await asyncio.to_thread(fechar_pool)


_TAGS_META = [
    {"name": "Saúde",
     "description": "Liveness. É o alvo do healthcheck do compose."},
    {"name": "Ordens de Serviço",
     "description": "Ciclo de vida da OS: entrada, fila, consulta e relatório. A entrada tem três recusas de contrato (409/429/503) e nenhuma é retentada pelo gerador."},
    {"name": "Eventos dos Adapters",
     "description": "Telemetria e transições vindas dos 4 adapters. Não é API de operador: quem chama são os adapters, sem autenticação."},
    {"name": "Estado e Telemetria",
     "description": "Estado corrente e históricos. `GET /estado` devolve o mesmo snapshot que o WebSocket `/ws` publica."},
    {"name": "Catálogo",
     "description": "Medicamentos e categorias disponíveis."},
    {"name": "Autenticação",
     "description": "Login e identidade. O token vale 8h."},
    {"name": "Manutenção",
     "description": "Sensores, log, alarmes e limpeza de dispenser. Exige JWT de técnico."},
    {"name": "Triple Check",
     "description": "Trava de emergência. 1 fonte divergente já trava (`TRIPLE_CHECK_MIN_DIVERGENCIAS`); liberar exige role admin ou supervisor."},
    {"name": "Usuários",
     "description": "Gestão de técnicos. Exige role admin."},
]

app = FastAPI(
    title="APSEN Computador Central",
    version="3.1.0",
    lifespan=lifespan,
    openapi_tags=_TAGS_META,
    description="""Orquestrador do sistema APSEN de contagem e dispensação de medicamentos.

**Autenticação** — as rotas de Manutenção, Usuários e Triple Check usam
`Authorization: Bearer <jwt>`, obtido em `POST /auth/login`. Use o botão
**Authorize** acima. A role vem do BANCO a cada requisição, não do token:
técnico desativado perde acesso na hora, sem esperar as 8h de expiração.

**O que NÃO está nesta página** — o endpoint `WS /ws`, que publica o
snapshot de estado a cada transição. OpenAPI não descreve WebSocket; o
payload é o mesmo de `GET /estado`.

**Corpos de resposta** — a maioria das rotas ainda não declara
`response_model`, então o *Example Value* aparece vazio. Os códigos de erro
documentados, esses, correspondem ao que o código realmente levanta.
""",
)
# Só o dashboard e o app de manutenção falam com o central pelo navegador.
# `*` num serviço autenticado deixa qualquer página aberta no browser do
# técnico disparar requisição em nome dele.
app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS,
                   allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

_bearer = HTTPBearer(auto_error=False)


# ── Revalidação do usuário a cada requisição ──────────────────────────────────
# O JWT era a palavra final: `_get_tecnico` decodificava e pronto. Um técnico
# desativado seguia com acesso total até o token expirar — 8 horas —, e uma
# role rebaixada continuava valendo pelo mesmo tempo. Como o token é assinado,
# nem dá para "editar" o que já foi emitido: a fonte de verdade tem que ser o
# banco, consultado a cada requisição.
#
# Cache de AUTH_CACHE_TTL_S (30s) para não virar uma query por request: é o
# atraso máximo entre desativar alguém e o acesso cair. As mutações que o
# próprio central conhece (desativar, ativar, trocar role) invalidam a entrada
# na hora, então na prática o atraso só existe para mudança feita fora da API.
_cache_usuarios: dict[str, tuple[float, dict]] = {}
_cache_usuarios_lock = threading.Lock()


def _invalidar_cache_usuario(username: str) -> None:
    with _cache_usuarios_lock:
        _cache_usuarios.pop(username, None)


def _usuario_valido(username: str) -> Optional[dict]:
    """Usuário ATIVO do banco, ou None. `get_usuario` já filtra `ativo=1`."""
    agora = time.monotonic()
    with _cache_usuarios_lock:
        entrada = _cache_usuarios.get(username)
        if entrada and (agora - entrada[0]) < settings.AUTH_CACHE_TTL_S:
            return entrada[1]

    try:
        usuario = get_usuario(username)
    except Exception as exc:
        # Banco fora do ar não pode virar acesso liberado.
        logger.error("[AUTH] Falha ao revalidar '%s': %s", username, exc)
        return None

    with _cache_usuarios_lock:
        _cache_usuarios[username] = (time.monotonic(), usuario)
    return usuario


def _get_tecnico(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token ausente")
    payload = decodificar_token(creds.credentials)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")

    usuario = _usuario_valido(payload.get("sub", ""))
    if not usuario:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Usuário inativo ou inexistente")
    # A role vem do BANCO, não do token: ela pode ter mudado desde a emissão,
    # e um token forjado traria a role que o portador quisesse.
    return {**payload, "role": usuario.get("role", "manutencao")}


def _get_admin(user=Depends(_get_tecnico)):
    if user.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requer perfil admin")
    return user


# Portão da trava do Triple Check. É um portão à PARTE, e não um afrouxamento
# de `_get_admin`: a gestão de usuários continua só admin. O docstring de
# `liberar_trava` prometia "admin ou supervisor" desde sempre, e a role
# "supervisor" não existia em lugar nenhum do central — a documentação mentia.
# Hoje ela existe (ver `database.ROLES_VALIDAS`), e é o perfil de quem chega à
# bancada, libera e vai embora: o supervisor da operação, autenticado por PIN
# no painel de bancada e no display de 7", que chamam este endpoint por uma
# conta de serviço com esta role.
#
# A role continua vindo do BANCO a cada requisição (`_get_tecnico`), nunca do
# token.
_ROLES_QUE_LIBERAM_TRAVA = frozenset({"admin", "supervisor"})


def _get_supervisor_ou_admin(user=Depends(_get_tecnico)):
    if user.get("role") not in _ROLES_QUE_LIBERAM_TRAVA:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Requer perfil admin ou supervisor")
    return user


def _validar_role(role: str) -> None:
    """400, e não gravado: role desconhecida criaria um usuário sem acesso a
    nada e sem erro em lugar nenhum. O vocabulário mora em `database.py`.

    Compara com a TUPLA `ROLES_VALIDAS`, e não com `database.role_valida`: a
    suíte troca toda função de `database` por um duplo que devolve None, e
    uma validação que passasse por função viraria 400 para toda role — o teste
    de "role válida é aceita" pegaria, mas só depois de meia hora de espanto.
    """
    if not isinstance(role, str) or role not in ROLES_VALIDAS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"role inválida: {role!r}. Válidas: {', '.join(ROLES_VALIDAS)}",
        )


# ── Pydantic Models ────────────────────────────────────────────────────────────
class LoginReq(BaseModel):
    username: str
    senha: str


class ManutencaoReq(BaseModel):
    tipo: str
    componente: str
    descricao: str


class StatusOSReq(BaseModel):
    status: str


class UsuarioReq(BaseModel):
    username: str
    senha: str
    nome_completo: str
    role: str = "manutencao"


class UsuarioUpdateReq(BaseModel):
    nome_completo: Optional[str] = None
    role: Optional[str] = None
    nova_senha: Optional[str] = None


class LiberarTravaReq(BaseModel):
    """Corpo OPCIONAL de `POST /api/v1/admin/liberar-trava`.

    `em_nome_de` é quem de fato liberou, quando a chamada vem por uma conta de
    serviço — o painel de bancada e o display de 7" autenticam o supervisor por
    PIN e falam com o central com a conta deles. Sem este campo toda liberação
    vinda da bancada apareceria no log com o nome da conta de serviço, e o
    rastro de QUEM liberou — que é o ponto inteiro de existir uma trava — se
    perderia.
    """
    em_nome_de: Optional[str] = None


# Teto do `em_nome_de`. O valor vai para o log e para `log_manutencao.tecnico`
# (VARCHAR(100)), ao lado do username: 60 deixa folga para os dois.
EM_NOME_DE_MAX = 60


def _sanitizar_em_nome_de(valor) -> Optional[str]:
    """Nome de quem liberou, pronto para o log e para o banco — ou None.

    Caractere não imprimível (quebra de linha, tab, controle) vira espaço e o
    espaço é colapsado: uma quebra de linha aqui faria o nome se passar por
    DUAS linhas de log, e a segunda por uma entrada que ninguém escreveu. O
    corte em `EM_NOME_DE_MAX` é truncamento, não recusa: um nome longo demais
    não pode ser o que impede um supervisor de liberar a produção.
    """
    if not isinstance(valor, str):
        return None
    limpo = "".join(ch if ch.isprintable() else " " for ch in valor)
    limpo = " ".join(limpo.split())
    return limpo[:EM_NOME_DE_MAX] or None


def _identificar_liberacao(sub: str, em_nome_de=None) -> str:
    """O `liberado_por` gravado: `sub`, ou `sub (em nome de X)`.

    Sem `em_nome_de` o resultado é exatamente o de antes deste campo existir:
    só o username de quem chamou.
    """
    nome = _sanitizar_em_nome_de(em_nome_de)
    return f"{sub} (em nome de {nome})" if nome else sub


class NovaOSReq(BaseModel):
    os_id: str
    descricao: str = ""
    categoria: str = ""
    medicamentos: list
    # Qual das dez ordens padrão é esta. Ele já vinha no corpo (o gerador e o
    # console montam por `os_templates.instanciar`), mas o modelo o descartava
    # — e `req.model_dump()` é o que vai para a fila. Sem o campo aqui, o
    # orquestrador não teria como saber qual receita a mesa deve executar, e
    # toda OS abortaria com `receita_nao_mapeada`.
    # Vazio é legítimo: uma OS criada fora dos templates. Quem decide o que
    # fazer com isso é o orquestrador, que a recusa com motivo claro.
    template_id: str = ""


class EventoDispenserReq(BaseModel):
    tipo: str
    dispenser_id: Optional[int] = None
    os_id: Optional[str] = None

    model_config = {"extra": "allow"}


class EventoCNCReq(BaseModel):
    tipo: str
    os_id: Optional[str] = None
    dispenser_alvo: Optional[int] = None

    model_config = {"extra": "allow"}


class EventoVisionReq(BaseModel):
    tipo: str
    camera: str                     # "dispenser_esq" | "dispenser_dir" | "mesa" | "sistema"
    slot_id: Optional[int] = None
    os_id: Optional[str] = None

    model_config = {"extra": "allow"}


class EventoPesoReq(BaseModel):
    tipo: str                       # tara_ok | peso_ok | peso_divergencia | erro_sensor | telemetria
    os_id: Optional[str] = None
    slot_id: Optional[int] = None

    model_config = {"extra": "allow"}


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — RECEBIMENTO (adapters e erp-simulator)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/v1/ordens", tags=["Ordens de Serviço"],
    responses={
        409: {"description": "OS já registrada (`os_duplicada`). O reenvio NÃO é reprocessado."},
        429: {"description": "Fila cheia (`fila_cheia`). Nada é persistido — a recusa vem ANTES do INSERT."},
        503: {"description": "Banco indisponível (`persistencia_indisponivel`). A OS não entra na fila."},
    })
async def receber_ordem(req: NovaOSReq):
    """Recebe nova OS do erp-simulator.

    **Nenhuma OS é enfileirada sem linha no banco.** A fila é o que dispensa
    medicamento; o banco é o que registra o que foi dispensado. Aceitar uma
    sem a outra produz os dois piores resultados do sistema:

      - `salvar_ordem` devolvendo False (INSERT IGNORE não inseriu) significa
        OS já registrada. Enfileirar de novo processa a MESMA OS duas vezes —
        dose dobrada no leito. Vira 409 `os_duplicada`.
      - `salvar_ordem` levantando significa banco fora do ar. Antes isso só era
        logado e a OS seguia para a fila: os medicamentos sairiam do dispenser
        sem linha em `ordens`/`os_itens`, então `atualizar_item_os` e
        `atualizar_status_ordem` não achariam o que atualizar e o relatório
        (`GET /os/{os_id}`, CSV/XLSX) sairia vazio — dispensa sem rastro, que é
        justamente o que um sistema de medicação não pode fazer. Vira 503, e a
        OS não entra na fila.

    Os corpos seguem a forma `{"erro": ..., "os_id": ...}` do contrato, e por
    isso são `JSONResponse` — `HTTPException` embrulharia tudo em `detail`.
    O contrato inteiro (200/409/429/503) está declarado no `responses=` logo
    acima — é dali que ele sai no Swagger — e explicado no README, seção
    "Contrato de entrada de uma OS". Esta função é a ÚNICA porta de entrada:
    o disparo manual do console chama ela, não uma cópia.
    Em todos os casos o erp-simulator apenas loga e segue para a próxima OS
    no ciclo seguinte (`_enviar_os` devolve False e ninguém retenta), então não
    há laço de reenvio nem processo derrubado.

    A terceira recusa é de fila cheia (429): o gerador posta mais rápido do que
    a planta processa, e sob trava do Triple Check o orquestrador para até um
    humano liberar. Ela vem ANTES de `salvar_ordem` — OS recusada não gera
    linha no banco, senão o "aguardando" órfão viraria a OS ativa aos olhos de
    `get_ordem_ativa`.
    """
    os_id        = req.os_id
    medicamentos = req.medicamentos

    if not os_id or not medicamentos:
        raise HTTPException(400, "os_id e medicamentos são obrigatórios")

    if not orch.ha_vaga_na_fila():
        fila = orch.fila_status()
        logger.warning("[API] OS %s recusada — fila cheia (%d/%d).",
                       os_id, fila["tamanho"], fila["capacidade"])
        _log("os_recusada", f"OS {os_id} recusada: fila cheia "
                            f"({fila['tamanho']}/{fila['capacidade']})")
        return JSONResponse(
            status_code=429,
            content={
                "erro":        "fila_cheia",
                "os_id":       os_id,
                "fila":        fila,
                "mensagem":    (f"Fila de OS no limite ({fila['tamanho']}/"
                                f"{fila['capacidade']}). Tente novamente mais tarde."),
            },
            headers={"Retry-After": str(int(settings.TIMEOUT_DISPENSA))},
        )

    try:
        inserida = await asyncio.to_thread(
            salvar_ordem, os_id, req.descricao, medicamentos, req.model_dump()
        )
    except Exception as exc:
        logger.error("[DB] Erro ao salvar OS %s: %s — OS recusada.", os_id, exc)
        _log("os_recusada", f"OS {os_id} recusada: persistência indisponível")
        return JSONResponse(
            status_code=503,
            content={"erro": "persistencia_indisponivel", "os_id": os_id},
        )

    if not inserida:
        logger.warning("[API] OS %s já registrada — recusada (duplicada).", os_id)
        _log("os_duplicada", f"OS {os_id} recusada: já registrada")
        return JSONResponse(
            status_code=409,
            content={"erro": "os_duplicada", "os_id": os_id},
        )

    with _lock:
        posicao_fila = len(_estado["fila_os"]) + (1 if _estado["os_ativa"] else 0)

    if not await orch.enfileirar_os(req.model_dump()):
        # Corrida perdida: a vaga conferida acima sumiu entre a checagem e o
        # enfileiramento. A OS já foi persistida, e uma linha "aguardando" que
        # ninguém vai processar seria devolvida por `get_ordem_ativa` como a OS
        # ativa — daí o fechamento em "cancelada".
        try:
            await asyncio.to_thread(atualizar_status_ordem, os_id, "cancelada")
        except Exception as exc:
            logger.error("[DB] cancelar OS %s não enfileirada: %s", os_id, exc)
        fila = orch.fila_status()
        return JSONResponse(
            status_code=429,
            content={"erro": "fila_cheia", "os_id": os_id, "fila": fila,
                     "mensagem": "Fila de OS ficou cheia durante o registro."},
            headers={"Retry-After": str(int(settings.TIMEOUT_DISPENSA))},
        )

    _log("os_nova", f"OS {os_id} recebida — {len(medicamentos)} medicamento(s)")
    logger.info("[API] OS %s recebida.", os_id)
    _broadcast_estado()

    return {"aceita": True, "os_id": os_id, "posicao_fila": posicao_fila + 1}


@app.post("/api/v1/eventos/dispenser", tags=["Eventos dos Adapters"])
async def evento_dispenser(req: EventoDispenserReq):
    """Recebe eventos normalizados do dispenser-adapter."""
    await _handle_evento_dispenser(req.model_dump())
    return {"ok": True}


@app.post("/api/v1/eventos/cnc", tags=["Eventos dos Adapters"])
async def evento_cnc(req: EventoCNCReq):
    """Recebe eventos normalizados do cnc-adapter."""
    await _handle_evento_cnc(req.model_dump())
    return {"ok": True}


@app.post("/api/v1/eventos/visao", tags=["Eventos dos Adapters"])
async def evento_visao(req: EventoVisionReq):
    """Recebe resultados de captura do vision-adapter (câmera dispenser e câmera mesa)."""
    await _handle_evento_visao(req.model_dump())
    return {"ok": True}


@app.post("/api/v1/eventos/peso", tags=["Eventos dos Adapters"])
async def evento_peso(req: EventoPesoReq):
    """Recebe leituras de pesagem do weight-adapter (balança HX711)."""
    await _handle_evento_peso(req.model_dump())
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — DASHBOARD (sem autenticação, read-only)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/ping", tags=["Saúde"])
def ping():
    return {"status": "ok", "service": "apsen-central-computer"}


@app.get("/estado", tags=["Estado e Telemetria"])
def get_estado():
    # Endpoint síncrono (roda em threadpool), então a releitura pode bloquear.
    # Sujeita ao mesmo cache dos handlers: o polling do dashboard não vira uma
    # query por request.
    _contar_alarmes_ativos()
    with _lock:
        # deepcopy, não dict(): a cópia rasa devolve os MESMOS dicionários
        # aninhados (dispensers, cnc, visao, peso, trava). A serialização
        # acontece depois do `with`, já sem o lock, e um evento chegando nesse
        # intervalo muda o dicionário durante a iteração do serializador
        # ("dictionary changed size during iteration"). O mesmo padrão de
        # `_broadcast_estado`.
        return copy.deepcopy(_estado)


@app.get("/api/v1/fila", tags=["Ordens de Serviço"])
def get_fila():
    """Ocupação da fila de OS — endpoint de backpressure do erp-simulator.

    O `/estado` já traz `fila_tamanho`, mas serve o snapshot inteiro (os 8 slots,
    CNC, visão, peso, trava) e ainda passa pelo contador de alarmes. Quem só
    precisa saber se cabe mais uma OS não deveria pagar por isso a cada ciclo —
    daí este endpoint de dois números. `fila_capacidade` continua no `/estado`
    para o dashboard mostrar a escala junto do resto.
    """
    return orch.fila_status()


@app.get("/api/v1/gerador", tags=["Ordens de Serviço"])
def get_gerador():
    """Interruptor do erp-simulator — ele consulta ANTES de cada envio.

    É assim que o console pausa a geração automática sem tocar no container do
    gerador. A alternativa seria o central falar com o daemon do Docker (socket
    montado, privilégio de administrador da máquina, acoplamento novo entre o
    central e o runtime que o hospeda) para não dispensar medicamento por
    alguns minutos. Um booleano no caminho HTTP que os dois já usam resolve o
    mesmo problema, e o gerador volta a produzir no instante em que o console
    despausa — sem esperar container subir.

    Sem autenticação, como `/api/v1/fila`: quem lê é um serviço interno da rede
    `apsen-net`, que não tem JWT. Quem ESCREVE é só o console
    (`POST /console/api/gerador`), atrás da sessão dele.

    O estado não é persistido: restart do central retoma o automático. Ver
    `console.py` para o porquê.
    """
    return console.gerador_status()


@app.get("/api/v1/ordens/templates", tags=["Ordens de Serviço"])
async def get_templates_ordens():
    """As 10 Ordens de Saída padrão, com os itens enriquecidos pelo catálogo.

    Fonte única das ordens fixas (`os_templates.py`). O erp-simulator lê daqui
    em vez de manter a própria cópia — se cada um tivesse a sua, o console
    listaria uma coisa e a planta dispensaria outra, sem erro em lugar nenhum.
    A justificativa completa está no topo de `os_templates.py`.

    `problemas` é diagnóstico, não erro: um medicamento renomeado no catálogo
    aparece aqui em vez de virar OS com `sku` vazio. Com o banco fora do ar a
    lista sai só com os problemas ESTRUTURAIS e `catalogo_carregado=false` —
    lista vazia sem catálogo não significa "está tudo certo".
    """
    problemas, catalogo = await _diagnosticar_templates()

    templates = os_templates.listar()
    for template in templates:
        for item in template["itens"]:
            med = (catalogo or {}).get(item["medicamento"], {})
            item["sku"]         = med.get("sku", "")
            item["categoria"]   = med.get("categoria", template["categoria"])
            item["no_catalogo"] = item["medicamento"] in (catalogo or {})
        template["total_itens"] = len(template["itens"])

    return {
        "templates":          templates,
        "total":              len(templates),
        "num_slots":          settings.NUM_SLOTS,
        "catalogo_carregado": catalogo is not None,
        "problemas":          problemas,
    }


# ── Teto do `limite` das rotas de histórico ───────────────────────────────────
#
# O parâmetro ia CRU para o `LIMIT %s` das queries. Dois estragos, e o primeiro
# era visível:
#
#   * `?limite=-1` vira `LIMIT -1`, que é erro de SINTAXE no MySQL (1064). O
#     central devolvia 500 numa rota de leitura, e o 1064 ainda passa pela
#     classificação de `init_db` como "schema inválido" se aparecer no boot;
#   * `?limite=99999999` é aceito e varre a tabela inteira — em `dispensas` e
#     `cnc_eventos`, que são as de maior cardinalidade, uma requisição sozinha
#     ocupa o pool de conexões e a memória do processo por bastante tempo.
#
# Um helper só, e não `Query(ge=1, le=...)` por rota: a validação do FastAPI
# responderia 422 a quem hoje recebe dados, e o valor esquisito quase sempre vem
# de um dashboard montando a URL, não de alguém pedindo o erro. Clamp devolve a
# página que a pessoa queria; 422 devolve uma tela vazia.
#
# O teto é o que `/api/v1/visao/historico` já praticava sozinho — ele passou a
# usar o mesmo helper, para não haver dois números.
_LIMITE_MAX = 500


def _limite(valor: int, maximo: int = _LIMITE_MAX) -> int:
    """Encaixa `valor` em 1..`maximo`. Nunca devolve 0 nem negativo."""
    try:
        valor = int(valor)
    except (TypeError, ValueError):
        return 1
    return max(1, min(valor, maximo))


@app.get("/os/ativa", tags=["Ordens de Serviço"])
def os_ativa():
    ordem = get_ordem_ativa()
    if not ordem:
        return {"os_ativa": None}
    return {"os_ativa": ordem}


@app.get("/os/historico", tags=["Ordens de Serviço"])
def os_historico(limite: int = 50):
    return get_historico_ordens(_limite(limite))


@app.get("/os/{os_id}", tags=["Ordens de Serviço"],
    responses={
        404: {"description": "OS não encontrada."},
    })
def os_detalhe(os_id: str):
    ordem = get_ordem_por_id(os_id)
    if not ordem:
        raise HTTPException(404, "OS não encontrada")
    return ordem


@app.get("/api/v1/relatorio/os/{os_id}", tags=["Ordens de Serviço"],
    # Devolve BYTES DE ARQUIVO, não JSON — o spec dizia `application/json`.
    # E não existe 400 aqui: qualquer `formato` diferente de `xlsx` cai no
    # CSV, inclusive valor inválido (`formato=pdf` responde 200 text/csv).
    response_class=StreamingResponse,
    responses={
        200: {"description": "CSV (default) ou XLSX, conforme `formato`.",
              "content": {"text/csv": {}, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}}},
        401: {"description": "Token ausente ou inválido."},
        404: {"description": "OS não encontrada."},
    })
async def relatorio_os(
    os_id: str,
    formato: str = "csv",
    user=Depends(_get_tecnico),
):
    """
    Exporta relatório completo de uma OS em CSV ou XLSX.

    Autenticação **só** por `Authorization: Bearer <jwt>`. O `?token=` que
    existia aqui servia para o navegador baixar direto de uma âncora do app de
    manutenção, o que punha o JWT no histórico do navegador, no `Referer` e no
    log de acesso deste serviço. Hoje quem baixa é o processo do app de
    manutenção (server-side, dentro da rede Docker) e devolve os bytes ao gestor
    pelo `dcc.Download` — nenhum cliente precisa mais de token na URL.

    E quem confere o token é `_get_tecnico`, como em todo o resto do central —
    não um `decodificar_token` local. A diferença não é de estilo: o JWT vale 8
    horas e é assinado, então não dá para "editar" o que já foi emitido; a
    palavra final é o banco (ver CLAUDE.md, "O JWT não é a palavra final"). Com
    a decodificação solta, desativar um técnico tirava dele o app de manutenção
    inteiro e deixava de pé justamente a rota que exporta o histórico de
    dispensação nominal da OS — a de maior valor para quem acabou de perder o
    acesso.
    """
    # Buscar dados em paralelo
    os_data, dispensas_data, visao_data, alarmes_data = await asyncio.gather(
        asyncio.to_thread(get_ordem_por_id, os_id),
        asyncio.to_thread(get_dispensas, os_id, 200),
        asyncio.to_thread(get_historico_visao, os_id, 200),
        asyncio.to_thread(get_alarmes_por_os, os_id),
    )
    if not os_data:
        raise HTTPException(status_code=404, detail=f"OS '{os_id}' não encontrada.")

    if formato.lower() == "xlsx":
        return await _gerar_xlsx(os_id, os_data, dispensas_data, visao_data, alarmes_data)
    return _gerar_csv(os_id, os_data, dispensas_data, visao_data, alarmes_data)


def _gerar_csv(os_id, os_data, dispensas_data, visao_data, alarmes_data) -> StreamingResponse:
    """Gera CSV com múltiplas seções separadas por linha em branco."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    # Cabeçalho OS
    writer.writerow(["=== ORDEM DE SAÍDA ==="])
    writer.writerow(["OS ID", "Status", "Categoria", "Descrição", "Criado em", "Atualizado em"])
    writer.writerow([
        os_data.get("os_id", ""), os_data.get("status", ""),
        os_data.get("categoria", ""), os_data.get("descricao", ""),
        str(os_data.get("criado_em", ""))[:19],
        str(os_data.get("atualizado_em", ""))[:19],
    ])
    writer.writerow([])

    # Dispensas
    writer.writerow(["=== DISPENSAS ==="])
    writer.writerow(["Dispenser", "Medicamento", "Qtd Dispensada", "Qtd Alvo", "Validado", "Horário"])
    for d in (dispensas_data or []):
        writer.writerow([
            f"D{d.get('dispenser_id','')}",
            d.get("medicamento", ""),
            d.get("quantidade_dispensada", ""),
            d.get("quantidade_alvo", ""),
            "SIM" if d.get("validado") else "NÃO",
            str(d.get("ts", ""))[:19],
        ])
    writer.writerow([])

    # Visão computacional
    writer.writerow(["=== LEITURAS DE VISÃO COMPUTACIONAL ==="])
    writer.writerow(["Câmera", "Slot", "Tipo", "SKU Esperado", "SKU Lido", "Match", "Confiança", "Qtd Det.", "Qtd Esp.", "Horário"])
    for v in (visao_data or []):
        writer.writerow([
            v.get("camera", ""), f"D{v.get('slot_id','')}",
            v.get("tipo", ""),
            v.get("sku_esperado", ""), v.get("sku_lido", ""),
            "SIM" if v.get("match_sku") == 1 else ("NÃO" if v.get("match_sku") == 0 else "—"),
            f"{(v.get('confianca') or 0)*100:.1f}%",
            v.get("qtd_detectada", ""), v.get("qtd_esperada", ""),
            str(v.get("criado_em", ""))[:19],
        ])
    writer.writerow([])

    # Alarmes
    writer.writerow(["=== ALARMES ==="])
    writer.writerow(["Fonte", "Tipo", "Descrição", "Horário"])
    for a in (alarmes_data or []):
        writer.writerow([
            a.get("fonte", ""), a.get("tipo", ""),
            a.get("descricao", ""),
            str(a.get("ts", ""))[:19],
        ])

    buf.seek(0)
    filename = f"relatorio_{os_id}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


async def _gerar_xlsx(os_id, os_data, dispensas_data, visao_data, alarmes_data) -> StreamingResponse:
    """Gera XLSX com múltiplas abas — requer openpyxl."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="openpyxl não instalado no servidor. Use formato=csv.",
        )

    wb = openpyxl.Workbook()

    def _header_style(ws, row, cols):
        fill = PatternFill("solid", start_color="1F4E79", end_color="1F4E79")
        font = Font(bold=True, color="FFFFFF")
        for col, val in enumerate(cols, start=1):
            c = ws.cell(row=row, column=col, value=val)
            c.fill = fill
            c.font = font
            c.alignment = Alignment(horizontal="center")

    # Aba: OS
    ws_os = wb.active
    ws_os.title = "OS"
    _header_style(ws_os, 1, ["OS ID", "Status", "Categoria", "Descrição", "Criado em", "Atualizado em"])
    ws_os.append([
        os_data.get("os_id", ""), os_data.get("status", ""),
        os_data.get("categoria", ""), os_data.get("descricao", ""),
        str(os_data.get("criado_em", ""))[:19], str(os_data.get("atualizado_em", ""))[:19],
    ])

    # Aba: Dispensas
    ws_d = wb.create_sheet("Dispensas")
    _header_style(ws_d, 1, ["Dispenser", "Medicamento", "Qtd Dispensada", "Qtd Alvo", "Validado", "Horário"])
    for d in (dispensas_data or []):
        ws_d.append([
            f"D{d.get('dispenser_id','')}", d.get("medicamento", ""),
            d.get("quantidade_dispensada", ""), d.get("quantidade_alvo", ""),
            "SIM" if d.get("validado") else "NÃO",
            str(d.get("ts", ""))[:19],
        ])

    # Aba: Visão
    ws_v = wb.create_sheet("Visão CV")
    _header_style(ws_v, 1, ["Câmera", "Slot", "Tipo", "SKU Esp.", "SKU Lido", "Match", "Conf.", "Qtd Det.", "Qtd Esp.", "Horário"])
    for v in (visao_data or []):
        ws_v.append([
            v.get("camera", ""), f"D{v.get('slot_id','')}",
            v.get("tipo", ""), v.get("sku_esperado", ""), v.get("sku_lido", ""),
            "SIM" if v.get("match_sku") == 1 else ("NÃO" if v.get("match_sku") == 0 else "—"),
            f"{(v.get('confianca') or 0)*100:.1f}%",
            v.get("qtd_detectada", ""), v.get("qtd_esperada", ""),
            str(v.get("criado_em", ""))[:19],
        ])

    # Aba: Alarmes
    ws_a = wb.create_sheet("Alarmes")
    _header_style(ws_a, 1, ["Fonte", "Tipo", "Descrição", "Horário"])
    for a in (alarmes_data or []):
        ws_a.append([
            a.get("fonte", ""), a.get("tipo", ""),
            a.get("descricao", ""), str(a.get("ts", ""))[:19],
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"relatorio_{os_id}.xlsx"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/v1/visao/historico", tags=["Estado e Telemetria"])
async def visao_historico(os_id: str = None, limite: int = 100):
    """Retorna histórico de leituras de visão computacional. Filtra por os_id se fornecido."""
    rows = await asyncio.to_thread(get_historico_visao, os_id, _limite(limite))
    return {"leituras": rows, "total": len(rows)}


@app.get("/medicamentos", tags=["Catálogo"])
def get_medicamentos(categoria: str = None):
    return listar_medicamentos(categoria)


@app.get("/medicamentos/categorias", tags=["Catálogo"])
def get_categorias():
    return listar_categorias()


@app.get("/dispensas", tags=["Estado e Telemetria"])
def dispensas(os_id: str = None, limite: int = 100):
    limite = _limite(limite)
    if os_id:
        return get_dispensas(os_id, limite)
    return get_dispensas_recentes(limite)


@app.get("/dispensers/estado", tags=["Estado e Telemetria"])
def dispensers_estado():
    return get_dispensers_estado()


@app.get("/cnc/historico", tags=["Estado e Telemetria"])
def cnc_historico(limite: int = 50):
    return get_cnc_recentes(_limite(limite))


@app.get("/alarmes", tags=["Estado e Telemetria"])
def alarmes(resolvido: bool = False, limite: int = 50):
    return get_alarmes(resolvido=resolvido, limite=_limite(limite))


@app.get("/log/eventos", tags=["Estado e Telemetria"])
def log_eventos(limite: int = 50):
    # Não vai a banco, mas `[:−1]` devolveria "todos menos o último" — um
    # recorte que ninguém pediu. O mesmo helper das rotas de histórico.
    return list(_log_eventos)[:_limite(limite)]


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — MANUTENÇÃO E OPERAÇÃO (requer JWT)
# ══════════════════════════════════════════════════════════════════════════════

def _origem_login(request: Request, username: str) -> str:
    """Chave do freio de força bruta do login: IP **e** username.

    Só o IP puniria o turno inteiro por causa de um técnico que erra a senha —
    a bancada fala com o central por um NAT só, e atrás de proxy todo mundo cai
    no mesmo `client.host`. Só o username deixaria qualquer um trancar a conta
    alheia de fora, que é negação de serviço disfarçada de proteção.

    O par não cobre varredura de MUITOS usernames a partir de um IP: cada
    combinação tem o próprio balde. Cobrir isso pede um segundo balde, por IP e
    com teto mais alto — o que este freio resolve é o caso que existe aqui, que
    é adivinhar a senha de uma conta conhecida (`admin` está no README).

    O prefixo separa o balde do console, que usa o mesmo dicionário: sem ele,
    errar a senha do console gastaria tentativa de quem faz login na API.
    """
    ip = request.client.host if request.client else "desconhecida"
    return f"login:{ip}|{username}"


@app.post("/auth/login", tags=["Autenticação"],
    responses={
        401: {"description": "Credenciais inválidas."},
        429: {"description": "Tentativas demais desta origem para este usuário."},
    })
def login(req: LoginReq, request: Request):
    """Emite o JWT de 8h. Com freio de força bruta, o mesmo do console.

    O console já tinha o freio e esta rota não — sendo que ela é a que dá o
    token de `admin`, o username do seed está documentado no README e a resposta
    não tem custo nenhum para quem tenta. Reaproveitar `console.registrar_falha`
    /`bloqueado` em vez de escrever um segundo contador vale pelo motivo de
    sempre: duas implementações da mesma regra divergem no primeiro ajuste, e a
    que fica para trás é a que ninguém está olhando.
    """
    origem = _origem_login(request, req.username)
    espera = console.bloqueado(origem)
    if espera:
        logger.warning("[AUTH] Tentativas demais para '%s' — bloqueado por %ds.",
                       req.username, int(espera))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Tentativas demais. Aguarde e tente novamente.",
            headers={"Retry-After": str(int(espera))},
        )

    user = get_usuario(req.username)
    if not user or not verificar_senha(req.senha, user["senha_hash"]):
        console.registrar_falha(origem)
        logger.warning("[AUTH] Credenciais inválidas para '%s'.", req.username)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciais inválidas")

    # Quem sabe a senha não é varredura: zerar o contador evita que um técnico
    # que errou três vezes e acertou na quarta fique a uma tentativa do bloqueio
    # pelo resto da janela.
    console.limpar_falhas(origem)
    role  = user.get("role", "manutencao")
    token = criar_token(user["username"], user["nome_completo"], role)
    return {
        "token":    token,
        "username": user["username"],
        "nome":     user["nome_completo"],
        "role":     role,
    }


@app.get("/auth/me", tags=["Autenticação"])
def me(user=Depends(_get_tecnico)):
    return {"username": user["sub"], "nome": user["nome"], "role": user.get("role")}


@app.put("/ordens/{os_id}/status", tags=["Ordens de Serviço"])
def alterar_status_os(os_id: str, req: StatusOSReq, user=Depends(_get_tecnico)):
    validos = {"aguardando", "em_andamento", "concluida", "erro", "cancelada"}
    if req.status not in validos:
        raise HTTPException(400, f"Status inválido. Use: {validos}")
    atualizar_status_ordem(os_id, req.status)
    _log("os_status", f"OS {os_id} → {req.status} por {user['sub']}")
    return {"ok": True, "os_id": os_id, "status": req.status}


@app.get("/manutencao/necessidades", tags=["Manutenção"])
async def manut_necessidades(user=Depends(_get_tecnico)):
    """O que precisa de atenção, numa lista priorizada. Ver `necessidades.py`.

    **Por que um agregado, e não seis chamadas do app de manutenção.** A tela de
    necessidades é a PRIMEIRA aba, ou seja, a que fica aberta — e o app repolla a
    cada `POLL_MS` (5s), por cliente conectado. Montá-la com
    `/api/v1/trava` + `/manutencao/alarmes` + `/manutencao/sensores` +
    `/dispensers/estado` + `/os/historico` + `/api/v1/fila` seriam seis
    requisições por tique por gestor com a tela aberta, e as seis atravessariam
    a rede para responder uma pergunta só.

    É o mesmo raciocínio da seção "O dashboard tem UM ponto de I/O" do
    CLAUDE.md, e o custo aqui é ainda menor: três das seis fontes saem do
    `_estado` em memória (trava, fila, dispensers) e as outras três viram um
    `gather` de consultas independentes.

    A DECISÃO da lista — o que é pendência, em que ordem, com que limiar — não
    mora aqui: mora em `necessidades.py`, que é puro. Este endpoint só junta os
    fatos.
    """
    with _lock:
        snapshot = copy.deepcopy(_estado)

    # As três consultas ao banco em paralelo: são independentes, e esperar uma
    # depois da outra somaria as latências numa rota que é repollada.
    alarmes, ordens, leituras = await asyncio.gather(
        asyncio.to_thread(get_alarmes, False, 100),
        asyncio.to_thread(get_historico_ordens, 50),
        asyncio.to_thread(get_ultimas_leituras),
        return_exceptions=True,
    )
    # Banco fora não pode apagar a tela: trava, fila e resíduo vivem em memória
    # e continuam valendo. Perder a parte que veio do banco é muito melhor que
    # perder a trava ativa, que é o item que mais importa desta lista — e é por
    # isso que o `return_exceptions=True` não é opcional aqui.
    faltou = False
    for nome, valor in (("alarmes", alarmes), ("ordens", ordens),
                        ("sensores", leituras)):
        if isinstance(valor, BaseException):
            faltou = True
            logger.warning("[NECESSIDADES] %s indisponível: %s", nome, valor)

    def _ou_vazio(valor):
        return [] if isinstance(valor, BaseException) else (valor or [])

    resultado = necessidades.montar(
        trava=snapshot.get("trava"),
        fila=orch.fila_status(),
        alarmes=_ou_vazio(alarmes),
        leituras=_ou_vazio(leituras),
        dispensers=snapshot.get("dispensers"),
        os_ativa=snapshot.get("os_ativa"),
        ordens=_ou_vazio(ordens),
    )
    # A tela precisa saber que está incompleta: "nada pendente" com o banco
    # fora seria a afirmação mais perigosa que este endpoint pode fazer.
    resultado["banco_disponivel"] = not faltou
    return resultado


@app.get("/manutencao/sensores", tags=["Manutenção"])
def manut_sensores(user=Depends(_get_tecnico)):
    return get_ultimas_leituras()


@app.get("/manutencao/sensores/{componente}", tags=["Manutenção"])
def manut_sensor_hist(componente: str, tipo: str = "temperatura", limite: int = 60,
                      user=Depends(_get_tecnico)):
    return get_historico_sensor(componente, tipo, _limite(limite))


@app.get("/manutencao/log", tags=["Manutenção"])
def manut_log(limite: int = 100, user=Depends(_get_tecnico)):
    return get_log_manutencao(_limite(limite))


@app.post("/manutencao/log", tags=["Manutenção"])
def manut_registrar(req: ManutencaoReq, user=Depends(_get_tecnico)):
    return salvar_manutencao(req.tipo, req.componente, req.descricao, user["sub"])


@app.get("/manutencao/alarmes", tags=["Manutenção"])
def manut_alarmes(resolvido: bool = False, limite: int = 100, user=Depends(_get_tecnico)):
    return get_alarmes(resolvido=resolvido, limite=_limite(limite))


@app.put("/manutencao/alarmes/{alarme_id}/resolver", tags=["Manutenção"])
def manut_resolver_alarme(alarme_id: int, user=Depends(_get_tecnico)):
    resolver_alarme(alarme_id)
    # Único caminho que ABAIXA o total — o que o contador manual nunca fez.
    # `forcar` porque o técnico está olhando o badge: esperar o TTL aqui é
    # exatamente o que faz o número parecer travado.
    total = _contar_alarmes_ativos(forcar=True)
    _log("alarme_resolvido", f"Alarme {alarme_id} resolvido por {user['sub']}")
    _broadcast_estado()
    return {"ok": True, "alarmes_ativos": total}


# ── Triple Check — trava de emergência ────────────────────────────────────────

@app.get("/api/v1/trava", tags=["Triple Check"])
def get_trava():
    """Retorna estado atual da trava de Triple Check."""
    return orch.get_trava_estado()


@app.post("/api/v1/admin/liberar-trava", tags=["Triple Check"],
    responses={
        401: {"description": "Token ausente, inválido, ou usuário desativado no banco."},
        403: {"description": "Requer role admin ou supervisor (a role vem do banco, não do token)."},
        409: {"description": "Nenhuma trava ativa no momento."},
    })
async def liberar_trava(req: Optional[LiberarTravaReq] = None,
                        user=Depends(_get_supervisor_ou_admin)):
    """
    Libera a trava de Triple Check. Exige role admin ou supervisor.
    A OS retoma de onde parou após a liberação.

    Corpo opcional `{"em_nome_de": "<nome>"}`: quando a chamada vem por uma
    conta de serviço (painel de bancada, display de 7"), é o nome do
    supervisor que digitou o PIN — o `liberado_por` gravado vira
    `"<conta> (em nome de <nome>)"`. Sem o campo, o comportamento é o de
    sempre: só o username de quem chamou.
    """
    liberado_por = _identificar_liberacao(
        user["sub"], req.em_nome_de if req is not None else None,
    )
    if not await _liberar_trava(liberado_por):
        raise HTTPException(status_code=409, detail="Nenhuma trava ativa no momento.")
    return {"ok": True, "liberado_por": liberado_por}


async def _liberar_trava(liberado_por: str) -> bool:
    """Liberação da trava: soltar o evento, limpar o snapshot, publicar.

    Extraído porque existem DOIS portões para a mesma ação — este endpoint
    (JWT de admin ou supervisor, usado pelo app de manutenção e pelo painel de
    bancada) e o console de operação, que tem sessão própria. Cada um
    autentica do seu jeito; o que acontece depois tem que ser idêntico, e duas
    cópias divergiriam justamente no passo fácil de esquecer — o
    `_estado["trava"]` que o dashboard lê. Sem ele, a faixa vermelha
    continuaria na tela com a OS já rodando.

    Devolve False quando não havia trava ativa; quem chama traduz para o 409.
    """
    trava = orch.get_trava_estado()
    if not await asyncio.to_thread(orch.liberar_trava, liberado_por):
        return False
    _log("trava", f"Trava liberada por {liberado_por}")
    with _lock:
        _estado["trava"] = {"ativa": False, "os_id": None, "slot_id": None, "motivo": ""}
    _broadcast_estado()
    # A trilha de QUEM liberou vai para `log_manutencao`, que é o registro de
    # quem mexeu no equipamento (o mesmo do reset e da limpeza manual). Até
    # aqui a liberação só existia numa linha de log de processo — e é ela, não
    # a ativação, que diz quem assumiu a responsabilidade pela OS que seguiu.
    # `tecnico` é VARCHAR(100); o `liberado_por` cabe por construção
    # (`EM_NOME_DE_MAX`), e o corte é só cinto sobre suspensório.
    await _db(
        salvar_manutencao, "trava_liberada", "triple_check",
        f"Trava da OS {trava.get('os_id')} (slot D{trava.get('slot_id')}) "
        f"liberada por {liberado_por}. Motivo: {trava.get('motivo') or ''}",
        liberado_por[:100],
    )
    return True


@app.post("/manutencao/dispensers/{dispenser_id}/limpar", tags=["Manutenção"],
    responses={
        400: {"description": "`dispenser_id` fora da faixa de slots da célula."},
        401: {"description": "Token ausente, inválido, ou usuário desativado no banco."},
        409: {"description": "OS em andamento, ou slot em operação física (`carregando`/`dispensando`)."},
        503: {"description": "Dispenser-adapter indisponível."},
    })
async def manut_limpar_dispenser(dispenser_id: int, user=Depends(_get_tecnico)):
    """Envia comando de limpeza ao dispenser-adapter. Bloqueado se slot está em operação."""
    if not 1 <= dispenser_id <= settings.NUM_SLOTS:
        raise HTTPException(400, f"dispenser_id deve ser 1-{settings.NUM_SLOTS}")

    with _lock:
        d_info   = _estado["dispensers"].get(str(dispenser_id), {})
        d_status = d_info.get("status", "idle")
        os_ativa = _estado["os_ativa"]

    # Bloqueio 1: OS ativa — nenhum dispenser pode ser limpo durante uma OS
    if os_ativa:
        os_atual = (os_ativa or {}).get("os_id", "?")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Limpeza bloqueada: OS {os_atual} em andamento. "
            "Aguarde a conclusão da ordem de serviço.",
        )
    # Bloqueio 2: dispenser em operação ativa.
    # "concluido" NÃO entra: o slot já terminou a dispensa e pode ter residual
    # encalhado — limpar esse resto é justamente o propósito do botão do app
    # de manutenção. (O simulador aplica a mesma regra em _do_limpar.)
    STATUS_BLOQUEADOS = {"carregando", "pronto", "dispensando", "aguardando_carga"}
    if d_status in STATUS_BLOQUEADOS:
        med = d_info.get("medicamento", f"Dispenser {dispenser_id}")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Dispenser {dispenser_id} ({med}) em operação (status: '{d_status}'). "
            "Aguarde o sistema finalizar antes de limpar.",
        )

    ok = await orch.cmd_limpar(dispenser_id, user["sub"])
    if not ok:
        raise HTTPException(503, "Dispenser-adapter indisponível")

    await asyncio.to_thread(
        salvar_manutencao,
        "limpeza_dispenser",
        f"dispenser_{dispenser_id}",
        f"Limpeza manual por {user['sub']}",
        user["sub"],
    )
    _log("limpeza_solicitada", f"Limpeza D{dispenser_id} por {user['sub']}")
    return {"ok": True, "dispenser_id": dispenser_id,
            "msg": "Comando enviado. Aguardando confirmação do dispenser."}


@app.get("/manutencao/usuarios", tags=["Usuários"])
def listar_usuarios(user=Depends(_get_admin)):
    return get_usuarios()


@app.post("/manutencao/usuarios", tags=["Usuários"],
    responses={400: {"description": "role fora de `ROLES_VALIDAS` — nada é gravado."}})
def criar_novo_usuario(req: UsuarioReq, user=Depends(_get_admin)):
    _validar_role(req.role)
    resultado = criar_usuario(req.username, req.senha, req.nome_completo, req.role)
    if not resultado.get("ok"):
        raise HTTPException(409, resultado.get("erro", "Erro ao criar usuário"))
    return resultado


@app.put("/manutencao/usuarios/{username}", tags=["Usuários"])
def editar_usuario(username: str, req: UsuarioUpdateReq, user=Depends(_get_admin)):
    if req.role is not None:
        _validar_role(req.role)
    resultado = atualizar_usuario(username, req.nome_completo, req.role, req.nova_senha)
    if not resultado.get("ok"):
        raise HTTPException(400, resultado.get("erro", "Erro ao atualizar"))
    # Role nova precisa valer AGORA, não daqui a AUTH_CACHE_TTL_S.
    _invalidar_cache_usuario(username)
    return resultado


@app.put("/manutencao/usuarios/{username}/desativar", tags=["Usuários"])
def desativar_usuario(username: str, user=Depends(_get_admin)):
    if username == user["sub"]:
        raise HTTPException(400, "Não pode desativar a própria conta")
    resultado = toggle_usuario_ativo(username, False)
    # Desativação é a operação em que o atraso do cache mais custa: é o botão
    # que o supervisor aperta quando quer alguém FORA agora.
    _invalidar_cache_usuario(username)
    return resultado


@app.put("/manutencao/usuarios/{username}/ativar", tags=["Usuários"])
def ativar_usuario(username: str, user=Depends(_get_admin)):
    resultado = toggle_usuario_ativo(username, True)
    _invalidar_cache_usuario(username)
    return resultado


# ══════════════════════════════════════════════════════════════════════════════
# CONSOLE DE OPERAÇÃO — interface própria do central, em /console
# ══════════════════════════════════════════════════════════════════════════════
#
# Rota discreta: não é linkada do dashboard nem do app de manutenção, e todas
# as rotas daqui levam `include_in_schema=False` — o console não aparece no
# `/docs` nem no `openapi.json`.
#
# A senha, a sessão assinada e o freio de força bruta vivem em `console.py`,
# que é o módulo sem FastAPI e, por isso, o que dá para testar chamando função.
# Aqui ficam só as rotas.
#
# **O console não é um caminho paralelo de entrada de OS.** `console_disparar`
# monta o corpo com `os_templates.instanciar` — o mesmo que o gerador usa — e
# CHAMA `receber_ordem`, a função do `POST /api/v1/ordens`. Fila cheia, OS
# duplicada e banco fora do ar respondem no console exatamente o que respondem
# ao gerador, porque é o mesmo código respondendo. Reimplementar o caminho aqui
# faria as duas portas divergirem no primeiro ajuste de contrato — e a que
# ficaria para trás é justamente a que um humano usa sob pressão.

_ERRO_SENHA = "Senha incorreta."
_ERRO_MUITAS_TENTATIVAS = ("Muitas tentativas seguidas. Aguarde um minuto antes "
                           "de tentar de novo.")


class ConsoleDisparoReq(BaseModel):
    template_id: str


class ConsoleGeradorReq(BaseModel):
    pausado: bool


class ConsoleInjecaoReq(BaseModel):
    tipo: str
    slot_id: int


class ConsoleResetReq(BaseModel):
    # Default FALSO nos dois: o corpo que chega sem campo nenhum faz a coisa
    # menos destrutiva possível. Preservar histórico é o caso comum — quase
    # sempre se quer a bancada limpa COM o histórico de pé, que é o que dá
    # forma ao dashboard.
    limpar_historico: bool = False


class ConsoleSeedReq(BaseModel):
    n_ordens: int = 40
    dias: int = 7


def _console_indisponivel() -> JSONResponse:
    """Resposta única para console desabilitado — 503, não 404.

    As duas escondem o console de quem não tem a senha e nenhuma das duas o
    abre; a diferença só aparece para o outro leitor, o operador que configurou
    errado. 404 manda essa pessoa procurar o erro na URL, no build ou no proxy;
    503 dizendo "defina CONSOLE_SENHA" encerra o assunto numa linha.
    """
    return JSONResponse(
        status_code=503,
        content={"erro": "console_desabilitado",
                 "mensagem": console.motivo_indisponivel()},
    )


def _console_origem(request: Request) -> str:
    """Chave do freio de força bruta. Atrás de proxy todos caem no mesmo balde —
    o que endurece o freio, nunca o afrouxa."""
    return request.client.host if request.client else "desconhecida"


async def _console_senha_enviada(request: Request) -> str:
    """Campo `senha` do formulário de login, lido sem `python-multipart`.

    O `request.form()` do Starlette exige a biblioteca mesmo para
    `application/x-www-form-urlencoded` — ele checa a dependência antes de
    decidir o parser. Como o formulário do console tem um campo, `parse_qsl` da
    stdlib resolve, e o central não ganha dependência nova para ler uma senha.

    O corpo é lido inteiro em memória, então tem teto: um POST gigante nesta
    rota é ou engano ou abuso, e nos dois casos a senha não estaria lá.
    """
    limite = 4096
    try:
        if int(request.headers.get("content-length") or 0) > limite:
            return ""
    except ValueError:
        return ""

    bruto = (await request.body())[:limite].decode("utf-8", "replace")
    for chave, valor in parse_qsl(bruto, keep_blank_values=True):
        if chave == "senha":
            return valor
    return ""


def _console_tem_sessao(request: Request) -> bool:
    return console.sessao_valida(request.cookies.get(console.COOKIE_SESSAO))


def _console_exigir_sessao(request: Request) -> None:
    """Portão das rotas `/console/api/*`. 503 antes de 401: sem `CONSOLE_SENHA`
    não existe sessão possível, e responder 401 sugeriria que existe."""
    if not console.habilitado():
        raise HTTPException(503, console.motivo_indisponivel())
    if not _console_tem_sessao(request):
        raise HTTPException(401, "Sessão do console ausente ou expirada.")


def _publicar_gerador(status_gerador: dict) -> None:
    """Publica o interruptor no snapshot e avisa quem está com a tela aberta.

    A fonte é `console._pausado`; isto é a publicação, na mesma ordem de
    `_ativar_trava` — primeiro o estado real, depois o que a tela lê. Transição
    de operador não passa pelo throttle: quem clicou precisa ver o efeito.
    """
    with _lock:
        _estado["gerador_pausado"] = status_gerador["pausado"]
    _broadcast_estado()


def _publicar_injecao() -> None:
    """Espelha o gatilho armado no snapshot e avisa quem está com a tela aberta.

    Mesma função que o orquestrador chama ao CONSUMIR o gatilho
    (`orchestrator._publicar_injecao`, que faz o mesmo sobre o mesmo `_estado`).
    Duas escritas, uma regra: a fonte é sempre `injecao.armada()`, nunca um
    valor montado pelo chamador — é o que impede a tela de mostrar armado o que
    já foi consumido.
    """
    with _lock:
        _estado["falha_armada"] = injecao.armada()
    _broadcast_estado()


# ── Sessão ────────────────────────────────────────────────────────────────────

@app.get("/console", include_in_schema=False)
def console_pagina(request: Request):
    if not console.habilitado():
        return _console_indisponivel()
    if not _console_tem_sessao(request):
        return RedirectResponse("/console/login", status_code=303)
    return HTMLResponse(console.pagina_console())


@app.get("/console/login", include_in_schema=False)
def console_login_form(request: Request):
    if not console.habilitado():
        return _console_indisponivel()
    if _console_tem_sessao(request):
        return RedirectResponse("/console", status_code=303)
    return HTMLResponse(console.pagina_login())


@app.post("/console/login", include_in_schema=False)
async def console_login(request: Request):
    """Confere a senha e emite o cookie de sessão.

    O corpo é lido por `_console_senha_enviada`, não por `Form(...)` nem por
    `request.form()`: os dois exigiriam `python-multipart` — dependência nova
    no central para ler um único campo de texto.
    """
    if not console.habilitado():
        return _console_indisponivel()

    origem = _console_origem(request)
    espera = console.bloqueado(origem)
    if espera:
        logger.warning("[CONSOLE] Tentativas demais de %s — bloqueado por %ds.",
                       origem, int(espera))
        return HTMLResponse(console.pagina_login(_ERRO_MUITAS_TENTATIVAS),
                            status_code=429,
                            headers={"Retry-After": str(int(espera))})

    if not console.senha_confere(await _console_senha_enviada(request)):
        console.registrar_falha(origem)
        logger.warning("[CONSOLE] Senha incorreta (origem %s).", origem)
        return HTMLResponse(console.pagina_login(_ERRO_SENHA), status_code=401)

    console.limpar_falhas(origem)
    _log("console", "Sessão do console de operação aberta")
    logger.info("[CONSOLE] Sessão aberta (origem %s).", origem)

    resposta = RedirectResponse("/console", status_code=303)
    resposta.set_cookie(
        console.COOKIE_SESSAO, console.criar_sessao(),
        max_age=console.duracao_cookie_s(),
        httponly=True,          # a senha nunca vai ao cliente e o JS não lê o cookie
        samesite="lax",
        path=console.COOKIE_PATH,
        # Ligado sozinho quando a página vier por TLS. Fixar True quebraria o
        # console em http://localhost, que é como a planta é demonstrada; fixar
        # False mandaria o cookie em claro num deploy https.
        secure=request.url.scheme == "https",
    )
    return resposta


@app.post("/console/logout", include_in_schema=False)
def console_logout(request: Request):
    if not console.habilitado():
        return _console_indisponivel()
    resposta = RedirectResponse("/console/login", status_code=303)
    resposta.delete_cookie(console.COOKIE_SESSAO, path=console.COOKIE_PATH)
    return resposta


# ── Ações ─────────────────────────────────────────────────────────────────────

@app.post("/console/api/disparar", include_in_schema=False)
async def console_disparar(request: Request, req: ConsoleDisparoReq):
    """Dispara UMA das ordens padrão, pelo mesmo caminho do erp-simulator.

    O `sku` sai do catálogo na hora, como no gerador: congelá-lo no template
    criaria a chance de ele divergir de `medicamentos`, e o sintoma seria uma
    `leitura_dispenser_divergencia` num slot só — indistinguível de um
    medicamento realmente trocado. Por isso catálogo indisponível RECUSA o
    disparo (503) em vez de instanciar com SKU vazio: sem SKU a câmera não tem
    o que comparar, e o Triple Check perde uma das três fontes em silêncio.
    """
    _console_exigir_sessao(request)

    template = os_templates.por_id(req.template_id)
    if not template:
        return JSONResponse(
            status_code=404,
            content={"erro": "template_desconhecido", "template_id": req.template_id,
                     "mensagem": f"Não existe ordem padrão '{req.template_id}'."},
        )

    _, catalogo = await _diagnosticar_templates()
    if catalogo is None:
        return JSONResponse(
            status_code=503,
            content={"erro": "catalogo_indisponivel", "template_id": req.template_id,
                     "mensagem": ("Catálogo de medicamentos indisponível — a OS "
                                  "sairia sem SKU para a câmera comparar.")},
        )

    faltando = os_templates.validar_contra_catalogo(catalogo, [template])
    if faltando:
        return JSONResponse(
            status_code=422,
            content={"erro": "template_invalido", "template_id": req.template_id,
                     "problemas": faltando, "mensagem": "; ".join(faltando)},
        )

    corpo = os_templates.instanciar(template, catalogo)
    logger.info("[CONSOLE] Disparo manual de %s → %s",
                template["template_id"], corpo["os_id"])

    # A MESMA função do POST /api/v1/ordens. Ela devolve dict (aceita) ou
    # JSONResponse (409/429/503) — os dois seguem para o console como vieram,
    # com o código de status intacto, que é o que a tela precisa mostrar.
    resultado = await receber_ordem(NovaOSReq(**corpo))
    if isinstance(resultado, JSONResponse):
        return resultado
    return {**resultado, "template_id": template["template_id"],
            "descricao": corpo["descricao"]}


@app.post("/console/api/gerador", include_in_schema=False)
def console_gerador(request: Request, req: ConsoleGeradorReq):
    """Pausa ou retoma o erp-simulator (ver `GET /api/v1/gerador`)."""
    _console_exigir_sessao(request)
    status_gerador = console.definir_pausa(req.pausado)
    _publicar_gerador(status_gerador)
    _log("console", "Gerador automático "
                    + ("pausado" if req.pausado else "retomado") + " pelo console")
    return {"ok": True, **status_gerador}


@app.post("/console/api/liberar-trava", include_in_schema=False)
async def console_liberar_trava(request: Request):
    """Libera a trava do Triple Check sem passar pelo app de manutenção.

    Mesma consequência do endpoint de admin — é literalmente a mesma função
    (`_liberar_trava`). O que muda é o portão: lá um JWT com role admin, aqui a
    sessão do console. A confirmação em dois passos é exigida do lado da
    página: liberar trava é a única ação destrutiva daqui.
    """
    _console_exigir_sessao(request)
    if not await _liberar_trava("console"):
        return JSONResponse(
            status_code=409,
            content={"erro": "sem_trava",
                     "mensagem": "Nenhuma trava ativa no momento."},
        )
    return {"ok": True, "liberado_por": "console"}


# ── Injeção de falha sob demanda ──────────────────────────────────────────────
#
# O gatilho mora em `injecao.py` e viaja no comando que o orquestrador já
# manda; estas rotas só armam, desarmam e listam. Quem consome — e portanto
# quem desarma de verdade — é `orchestrator._injecao_para`, no momento em que o
# comando é montado. Ver `injecao.py` para por que a fonte é uma só.

@app.get("/console/api/injecao", include_in_schema=False)
def console_injecao_catalogo(request: Request):
    """Tipos que dá para armar + o gatilho em vigor.

    O console monta o seletor com o que vem daqui, em vez de trazer a lista no
    HTML: uma segunda lista ofereceria um dia um tipo que nenhum simulador
    entende, e o sintoma seria um gatilho armado que nunca dispara.
    """
    _console_exigir_sessao(request)
    return {
        "tipos":     injecao.catalogo(),
        "armada":    injecao.armada(),
        "num_slots": settings.NUM_SLOTS,
    }


@app.post("/console/api/injecao", include_in_schema=False)
def console_injecao_armar(request: Request, req: ConsoleInjecaoReq):
    """Arma a próxima falha. Substitui o gatilho anterior, se houver."""
    _console_exigir_sessao(request)
    try:
        gatilho = injecao.armar(req.tipo, req.slot_id)
    except injecao.InjecaoInvalida as exc:
        return JSONResponse(
            status_code=422,
            content={"erro": "injecao_invalida", "mensagem": str(exc)},
        )
    _publicar_injecao()
    _log("console", f"Falha armada para demonstração: {req.tipo} no slot "
                    f"D{req.slot_id}")
    return {"ok": True, "armada": gatilho}


@app.post("/console/api/injecao/desarmar", include_in_schema=False)
def console_injecao_desarmar(request: Request):
    """Desarma sem consumir. 409 quando não havia nada armado."""
    _console_exigir_sessao(request)
    anterior = injecao.desarmar()
    _publicar_injecao()
    if not anterior:
        return JSONResponse(
            status_code=409,
            content={"erro": "sem_injecao",
                     "mensagem": "Nenhuma falha armada no momento."},
        )
    _log("console", f"Falha armada cancelada: {anterior['tipo']} no slot "
                    f"D{anterior['slot_id']}")
    return {"ok": True, "desarmada": anterior}


# ── Reset da planta e seed de histórico ───────────────────────────────────────
#
# As duas ações destrutivas do console, e as únicas que a página confirma em
# dois passos junto com a liberação de trava.
#
# O SEED É DADO FABRICADO e nunca roda sozinho: não há chamada dele no
# `init_db`, no `lifespan` nem em healthcheck nenhum — só aqui, atrás da sessão
# do console e da confirmação. Ver `seed_demo.py`.

@app.post("/console/api/reset", include_in_schema=False)
async def console_reset(request: Request, req: ConsoleResetReq):
    """Devolve a bancada ao estado de boot, comandando limpeza REAL nos slots.

    Recusa com 409 enquanto houver OS em execução — ver `resetar_planta` para
    por que a alternativa (resetar por cima) produz o estado mais difícil de
    diagnosticar que esta planta consegue gerar.
    """
    _console_exigir_sessao(request)
    try:
        relatorio = await orch.resetar_planta(req.limpar_historico)
    except orch.ResetRecusado as exc:
        return JSONResponse(
            status_code=409,
            content={"erro": "os_em_execucao", "mensagem": str(exc)},
        )

    await _atualizar_alarmes_ativos(forcar=True)
    _log("console", "Reset da planta pelo console"
                    + (" (histórico APAGADO)" if req.limpar_historico else
                       " (histórico preservado)"))
    _broadcast_estado()
    return {"ok": True, **relatorio}


@app.post("/console/api/seed", include_in_schema=False)
async def console_seed(request: Request, req: ConsoleSeedReq):
    """Semeia histórico de DEMONSTRAÇÃO — ordens, dispensas, alarmes, leituras.

    O catálogo real é consultado antes: sem ele o histórico sairia com SKU
    vazio, e um relatório de dispensação sem SKU é justamente o documento que
    não serve para nada. Mesma recusa (503) do disparo manual, pelo mesmo
    motivo.
    """
    _console_exigir_sessao(request)

    _, catalogo = await _diagnosticar_templates()
    if catalogo is None:
        return JSONResponse(
            status_code=503,
            content={"erro": "catalogo_indisponivel",
                     "mensagem": ("Catálogo de medicamentos indisponível — o "
                                  "histórico sairia sem SKU.")},
        )
    # O catálogo já vem indexado por nome, no mesmo formato que
    # `os_templates.instanciar` consome — não há segunda consulta a fazer.
    try:
        dados = seed_demo.gerar_historico(req.n_ordens, catalogo, dias=req.dias)
    except seed_demo.SeedInvalido as exc:
        return JSONResponse(
            status_code=422,
            content={"erro": "seed_invalido", "mensagem": str(exc)},
        )

    try:
        resumo = await asyncio.to_thread(semear_historico_demo, dados)
    except Exception as exc:
        logger.error("[SEED] falha ao gravar: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"erro": "persistencia_indisponivel", "mensagem": str(exc)},
        )

    await _atualizar_alarmes_ativos(forcar=True)
    _log("console", f"Histórico de DEMONSTRAÇÃO semeado: {resumo['ordens']} OS "
                    f"em {req.dias} dia(s)")
    _broadcast_estado()
    return {"ok": True, "demonstracao": True, "prefixo": seed_demo.PREFIXO_DEMO,
            **resumo}


# ── Pré-voo: "está tudo de pé para apresentar?" ───────────────────────────────
#
# A regra do arquivo `prevoo.py` vale aqui: a tela não pode travar por causa de
# um serviço morto, que é justamente o caso em que ela é mais necessária. Todas
# as sondas correm em `gather` com timeout curto, e o conjunto tem um teto
# próprio — o tempo total é o da sonda mais lenta, não a soma das treze.

@app.get("/console/prevoo", include_in_schema=False)
def console_prevoo_pagina(request: Request):
    """Página do pré-voo. Tela própria, e não mais um painel no console.

    O console é a mesa de OPERAÇÃO, usada durante a apresentação; o pré-voo é
    lido cinco minutos ANTES e não volta a ser aberto. Misturar os dois poria
    quatro dezenas de linhas de conferência entre o operador e o botão de
    disparar.
    """
    if not console.habilitado():
        return _console_indisponivel()
    if not _console_tem_sessao(request):
        return RedirectResponse("/console/login", status_code=303)
    return HTMLResponse(console.pagina_prevoo())


async def _prevoo_banco() -> dict | None:
    """Fatos do banco, ou `None` se ele não respondeu.

    O `None` é o que faz o relatório mostrar UM item vermelho em vez de quatro:
    com o MySQL fora, dizer também "schema incompleto" e "catálogo vazio" seria
    derivar três diagnósticos da mesma causa.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(diagnosticar, settings.NUM_SLOTS),
            timeout=prevoo.TIMEOUT_SONDA_S,
        )
    except Exception as exc:
        logger.warning("[PREVOO] banco indisponível: %s", exc)
        return None


@app.get("/console/api/prevoo", include_in_schema=False)
async def console_prevoo(request: Request):
    """O relatório, item a item. Nunca levanta por causa de um serviço fora."""
    _console_exigir_sessao(request)

    with _lock:
        snapshot = copy.deepcopy(_estado)

    async with httpx.AsyncClient() as cliente:
        # Banco e sondas HTTP juntos: o banco é I/O como as outras, e esperá-lo
        # antes somaria o tempo dele ao da sonda mais lenta.
        try:
            servicos, banco = await asyncio.wait_for(
                asyncio.gather(prevoo.sondar_servicos(cliente), _prevoo_banco()),
                timeout=prevoo.TIMEOUT_TOTAL_S,
            )
        except asyncio.TimeoutError:
            logger.error("[PREVOO] conjunto de sondas estourou %.0fs.",
                         prevoo.TIMEOUT_TOTAL_S)
            return JSONResponse(
                status_code=200,
                content={
                    "itens": [{
                        "id": "prevoo", "grupo": "Serviços",
                        "titulo": "Verificação", "estado": prevoo.FALHA,
                        "detalhe": f"As sondas não terminaram em "
                                   f"{prevoo.TIMEOUT_TOTAL_S:.0f}s",
                        "acao": "O event loop do central está saturado. "
                                "Verifique `docker compose logs central-computer` "
                                "e considere reiniciá-lo.",
                    }],
                    "resumo": {"pronto": False,
                               "contagem": {prevoo.FALHA: 1}, "total": 1},
                },
            )

    problemas, catalogo = await _diagnosticar_templates()
    fila = orch.fila_status()

    itens = (
        [prevoo.item_central()]
        + servicos
        + [prevoo.item_gerador(bool(snapshot.get("gerador_pausado")))]
        + prevoo.itens_banco(banco)
        + [prevoo.item_templates(problemas, len(os_templates.listar()),
                                 catalogo is not None)]
        + [prevoo.item_fila(fila["tamanho"], fila["capacidade"])]
        + prevoo.itens_celula(snapshot, orch.HOME)
        + prevoo.itens_modo(settings.MODO_APRESENTACAO, settings.FATOR_VELOCIDADE,
                            injecao.armada(), len(_ws_manager.active))
    )
    return {"itens": itens, "resumo": prevoo.resumo(itens)}


# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """Snapshot na conexão e, daí em diante, o que `_broadcast_estado` publicar.

    **O `finally` é a correção, e o que ele conserta é um vazamento LENTO.**
    Só `WebSocketDisconnect` era tratado, e ela é uma das saídas: `CancelledError`
    no shutdown, um erro de rede no `send_text` do snapshot, um cliente que morre
    de outro jeito — qualquer uma delas deixava o `WebSocket` na lista do
    manager para sempre. A partir daí todo broadcast tentava escrever nele e
    pagava a exceção, e cada reconexão de um dashboard acrescentava mais um.
    O `broadcast` até removia o morto pelo `except`, mas só quando o envio
    levantava; o que nunca mais fosse escrito ficava.

    O `connect` fica FORA do try: se o `accept()` falhar não há nada a
    desconectar, e chamar `disconnect` sobre um cliente que nunca entrou na
    lista esconderia o erro do accept.
    """
    await _ws_manager.connect(ws)
    try:
        with _lock:
            # deepcopy pelo mesmo motivo de `get_estado`: o json.dumps abaixo
            # roda fora do lock e percorreria os dicionários vivos.
            snap = copy.deepcopy(_estado)
        await ws.send_text(json.dumps({"tipo": "estado", **snap}, default=str))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_manager.disconnect(ws)
