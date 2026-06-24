"""
APSEN - Simulador CNC v2.1
===========================
Simula o firmware da mesa CNC que movimenta a caixa sob cada dispenser.

Fluxo completo:
  1. Recebe OS via apsen/os/nova
  2. Recebe atribuição da IA (apsen/ia/atribuicao) — sabe qual dispenser tem qual remédio
  3. IA da CNC calcula a melhor ordem de visita (cartonização — nearest-neighbor)
  4. Aguarda TODOS os dispensers da OS publicarem status "pronto"
  5. Mesa inicia: para cada dispenser na ordem otimizada:
       a. Publica "movendo" + log de cada passo (posição interpolada)
       b. Publica "posicionado" ao chegar
       c. Aguarda dispenser concluir a dispensa
       d. Avança para o próximo
  6. Ao finalizar todos: publica "concluido" e retorna para HOME

Tópicos publicados:
  apsen/cnc/status      → log de cada movimento + estado geral
  apsen/manut/temperatura
  apsen/manut/uso

Tópicos assinados:
  apsen/os/nova           — OS recebida da SAP
  apsen/ia/atribuicao     — atribuição IA: qual dispenser tem qual remédio
  apsen/dispenser/status  — aguarda "pronto" e "concluido" dos dispensers
"""

import json
import logging
import math
import os
import random
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CNC-SIM] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

MQTT_HOST       = os.getenv("MQTT_HOST",    "mosquitto")
MQTT_PORT       = int(os.getenv("MQTT_PORT", "1883"))
VELOCIDADE_MM_S = float(os.getenv("VEL_MM_S",   "80"))    # mm/s de movimento
INTERVALO_PUB   = float(os.getenv("INTERVALO",  "0.5"))   # s entre publicações de posição
TIMEOUT_PRONTO  = float(os.getenv("TIMEOUT_PRONTO", "180"))  # s aguardando todos prontos
TIMEOUT_DISP    = float(os.getenv("TIMEOUT_DISP",   "120"))  # s aguardando dispenser dispensar
RECONNECT_DELAY = 5

# Posições XY (mm) de cada dispenser na mesa
POSICOES: dict[int, tuple[float, float]] = {
    1: (0.0,   0.0),
    2: (120.0, 0.0),
    3: (240.0, 0.0),
    4: (360.0, 0.0),
    5: (480.0, 0.0),
    6: (600.0, 0.0),
}
HOME: tuple[float, float] = (0.0, -50.0)  # posição de repouso

# Componentes para telemetria de manutenção
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


def _nearest_neighbor(dispensers: list[int], start: tuple[float, float]) -> list[int]:
    """
    Calcula a ordem ótima de visita aos dispensers usando nearest-neighbor.
    Simula a IA de cartonização: minimiza a distância total percorrida.
    """
    restantes = list(dispensers)
    ordem     = []
    pos_atual  = start

    while restantes:
        # Escolhe o dispenser mais próximo da posição atual
        mais_proximo = min(
            restantes,
            key=lambda d: math.hypot(
                POSICOES[d][0] - pos_atual[0],
                POSICOES[d][1] - pos_atual[1],
            ),
        )
        ordem.append(mais_proximo)
        restantes.remove(mais_proximo)
        pos_atual = POSICOES[mais_proximo]

    return ordem


class CNCSimulator:
    def __init__(self):
        self.state          = "idle"
        self.os_id          = None
        self.os_itens       = []       # medicamentos da OS atual (raw)
        self.atribuicoes    = {}       # {dispenser_id: {...}} da IA
        self.ciclo_atual    = 0
        self.total_ciclos   = 0
        self.pos_x          = HOME[0]
        self.pos_y          = HOME[1]
        self.horas_uso      = random.uniform(120, 800)
        self.ciclos_total   = random.randint(5000, 50000)

        self._lock          = threading.Lock()
        # Dispensers que já enviaram "pronto" para a OS atual
        self._disp_prontos: set[int] = set()
        # Dispensers aguardados (IDs inteiros)
        self._disp_esperados: set[int] = set()
        # Evento: todos os dispensers prontos
        self._todos_prontos = threading.Event()
        # Evento: dispenser atual concluiu a dispensa
        self._disp_concluiu = threading.Event()
        self._disp_atual_id: int | None = None

        self.client = mqtt.Client(client_id="apsen-cnc-simulator-v21", clean_session=True)
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

    # ─────────────────────────────────────────────────── MQTT ──────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe("apsen/os/nova",          qos=1)
            client.subscribe("apsen/ia/atribuicao",    qos=0)
            client.subscribe("apsen/dispenser/status", qos=0)
            logger.info("CNC conectada ao broker. Aguardando OS...")
            self._pub_status()

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning(f"CNC desconectada (rc={rc}). Reconectando em {RECONNECT_DELAY}s...")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            topic   = msg.topic

            if topic == "apsen/os/nova":
                self._handle_os_nova(payload)

            elif topic == "apsen/ia/atribuicao":
                self._handle_ia_atribuicao(payload)

            elif topic == "apsen/dispenser/status":
                self._handle_dispenser_status(payload)

        except Exception as exc:
            logger.error(f"[CNC] Erro em mensagem: {exc}", exc_info=True)

    # ─────────────────────────────────────────────────── Handlers ──────────────

    def _handle_os_nova(self, payload: dict):
        with self._lock:
            if self.state != "idle":
                logger.warning(f"[CNC] OS recebida mas máquina em '{self.state}' — ignorando.")
                return
            self.os_id         = payload.get("os_id")
            self.os_itens      = payload.get("medicamentos", [])
            self.atribuicoes   = {}
            self._disp_prontos = set()
            self._disp_esperados = set()
            self._todos_prontos.clear()
            self.ciclo_atual   = 0
            self.total_ciclos  = len(self.os_itens)
            self.state         = "aguardando_atribuicao"

        logger.info(
            f"\n{'='*60}\n"
            f"  [CNC] OS RECEBIDA: {self.os_id}\n"
            f"  Medicamentos: {[(i['dispenser_id'], i['medicamento']) for i in self.os_itens]}\n"
            f"  Aguardando atribuição da IA...\n"
            f"{'='*60}"
        )
        self._pub_status(status="aguardando_atribuicao")

    def _handle_ia_atribuicao(self, payload: dict):
        os_id = payload.get("os_id")
        with self._lock:
            if os_id != self.os_id:
                return  # atribuição para outra OS
            atribuicoes = payload.get("atribuicoes", [])
            for a in atribuicoes:
                disp_id = int(a["dispenser_id"])
                self.atribuicoes[disp_id] = a
                self._disp_esperados.add(disp_id)
            self.state = "aguardando_abastecimento"

        # Calcula a ordem ótima de visita (cartonização)
        with self._lock:
            dispenser_ids = list(self._disp_esperados)
        ordem_otima = _nearest_neighbor(dispenser_ids, (self.pos_x, self.pos_y))

        logger.info(
            f"[CNC] IA: atribuição recebida para {len(dispenser_ids)} dispenser(s).\n"
            f"       Ordem ótima calculada (cartonização): {ordem_otima}"
        )
        with self._lock:
            self._ordem_visita = ordem_otima
            self.total_ciclos  = len(ordem_otima)

        self._pub_status(
            status="aguardando_abastecimento",
            extra={"ordem_visita": ordem_otima},
        )

        # Aguarda todos prontos em thread separada
        threading.Thread(
            target=self._aguardar_todos_prontos,
            daemon=True,
            name="aguardar-prontos",
        ).start()

    def _handle_dispenser_status(self, payload: dict):
        disp_id = payload.get("dispenser_id")
        status  = payload.get("status", "")
        os_id   = payload.get("os_id")

        with self._lock:
            # Dispenser sinalizou que está pronto para dispensar
            if (status == "pronto"
                    and isinstance(disp_id, int)
                    and disp_id in self._disp_esperados
                    and os_id == self.os_id):
                self._disp_prontos.add(disp_id)
                faltam = self._disp_esperados - self._disp_prontos
                logger.info(
                    f"[CNC] Dispenser {disp_id} PRONTO. "
                    f"Prontos: {len(self._disp_prontos)}/{len(self._disp_esperados)}. "
                    f"Faltam: {sorted(faltam) or 'nenhum'}"
                )
                if self._disp_prontos >= self._disp_esperados:
                    self._todos_prontos.set()

            # Dispenser concluiu a dispensa (CNC pode avançar)
            elif (status == "concluido"
                      and isinstance(disp_id, int)
                      and disp_id == self._disp_atual_id):
                self._disp_concluiu.set()

    # ─────────────────────────────────────── Processamento da OS ───────────────

    def _aguardar_todos_prontos(self):
        """Aguarda todos os dispensers reportarem 'pronto'. Então inicia a mesa."""
        logger.info(f"[CNC] Aguardando {len(self._disp_esperados)} dispenser(s) carregar...")
        ok = self._todos_prontos.wait(timeout=TIMEOUT_PRONTO)
        if not ok:
            logger.error("[CNC] Timeout aguardando dispensers carregarem!")
            self._pub_status(status="erro", extra={"mensagem": "Timeout aguardando abastecimento"})
            with self._lock:
                self.state = "idle"
            return

        logger.info("\n[CNC] TODOS OS DISPENSERS PRONTOS. Mesa iniciando!\n")
        self._processar_os()

    def _processar_os(self):
        """Visita cada dispenser na ordem otimizada e loga cada movimento."""
        with self._lock:
            ordem    = list(self._ordem_visita)
            os_id    = self.os_id
            total    = self.total_ciclos
            self.state = "em_operacao"

        self._pub_status(status="iniciando")
        time.sleep(0.5)

        for seq, disp_id in enumerate(ordem, start=1):
            with self._lock:
                self.ciclo_atual = seq
                med = self.atribuicoes.get(disp_id, {}).get("medicamento", "?")

            logger.info(
                f"\n[CNC] ──── Ciclo {seq}/{total} ────\n"
                f"       Destino: Dispenser {disp_id} ({med})\n"
                f"       Posição alvo: {POSICOES[disp_id]} mm"
            )

            # Mover para o dispenser (publica cada passo de posição)
            self._mover_para(disp_id, seq, total, os_id)

            # Posicionado — notifica dispenser que pode dispensar
            self._pub_status(status="posicionado", dispenser_alvo=disp_id)
            logger.info(f"[CNC] POSICIONADA no Dispenser {disp_id}. Aguardando dispensa...")

            # Aguarda dispenser concluir (timeout de segurança)
            with self._lock:
                self._disp_atual_id = disp_id
                self._disp_concluiu.clear()

            concluiu = self._disp_concluiu.wait(timeout=TIMEOUT_DISP)
            if not concluiu:
                logger.error(f"[CNC] Timeout aguardando Dispenser {disp_id} concluir!")
                self._pub_status(
                    status="erro",
                    dispenser_alvo=disp_id,
                    extra={"mensagem": f"Timeout dispenser {disp_id}"},
                )
                with self._lock:
                    self.state = "idle"
                return

            logger.info(f"[CNC] Dispenser {disp_id} concluído. Avançando.")

        # Todos visitados — retorna para HOME
        self._mover_para_home(os_id)
        self._pub_status(status="concluido")

        with self._lock:
            self.ciclos_total += total
            self.horas_uso    += total * 0.01  # ~36s por ciclo em média
            self.state         = "idle"
            self.os_id         = None
            self._ordem_visita = []

        logger.info(
            f"\n{'='*60}\n"
            f"  [CNC] OS CONCLUIDA. {total} dispenser(s) visitados.\n"
            f"  Retornando para HOME.\n"
            f"{'='*60}"
        )
        time.sleep(1.0)
        self._pub_status(status="idle")

    # ─────────────────────────────────────────── Movimento ─────────────────────

    def _mover_para(self, dispenser_id: int, ciclo: int, total: int, os_id: str):
        """Interpola posição até o dispenser, publicando cada passo."""
        alvo_x, alvo_y = POSICOES[dispenser_id]
        with self._lock:
            orig_x, orig_y = self.pos_x, self.pos_y

        distancia = math.hypot(alvo_x - orig_x, alvo_y - orig_y)
        if distancia < 0.1:
            return  # já está no lugar

        duracao = max(1.0, distancia / VELOCIDADE_MM_S)
        passos  = max(3, int(duracao / INTERVALO_PUB))

        logger.info(
            f"[CNC] Movendo de ({orig_x:.1f}, {orig_y:.1f}) → "
            f"({alvo_x:.1f}, {alvo_y:.1f}) | {distancia:.0f}mm | "
            f"~{duracao:.1f}s | {passos} passos"
        )

        for i in range(1, passos + 1):
            t     = i / passos
            cur_x = orig_x + (alvo_x - orig_x) * t
            cur_y = orig_y + (alvo_y - orig_y) * t

            with self._lock:
                self.pos_x = cur_x
                self.pos_y = cur_y

            self.client.publish(
                "apsen/cnc/status",
                json.dumps({
                    "status":         "movendo",
                    "os_id":          os_id,
                    "dispenser_alvo": dispenser_id,
                    "posicao_x":      round(cur_x, 2),
                    "posicao_y":      round(cur_y, 2),
                    "ciclo_atual":    ciclo,
                    "total_ciclos":   total,
                    "passo":          i,
                    "total_passos":   passos,
                    "progresso_pct":  round(t * 100, 1),
                    "timestamp":      datetime.now(timezone.utc).isoformat(),
                }),
                qos=0,
            )
            time.sleep(INTERVALO_PUB)

    def _mover_para_home(self, os_id: str):
        """Move de volta para a posição HOME."""
        with self._lock:
            orig_x, orig_y = self.pos_x, self.pos_y

        distancia = math.hypot(HOME[0] - orig_x, HOME[1] - orig_y)
        duracao   = max(1.0, distancia / VELOCIDADE_MM_S)
        passos    = max(3, int(duracao / INTERVALO_PUB))

        for i in range(1, passos + 1):
            t     = i / passos
            cur_x = orig_x + (HOME[0] - orig_x) * t
            cur_y = orig_y + (HOME[1] - orig_y) * t
            with self._lock:
                self.pos_x = cur_x
                self.pos_y = cur_y
            self.client.publish(
                "apsen/cnc/status",
                json.dumps({
                    "status":     "retornando",
                    "os_id":      os_id,
                    "posicao_x":  round(cur_x, 2),
                    "posicao_y":  round(cur_y, 2),
                    "timestamp":  datetime.now(timezone.utc).isoformat(),
                }),
                qos=0,
            )
            time.sleep(INTERVALO_PUB)

    # ─────────────────────────────────────── Publicação geral ──────────────────

    def _pub_status(self, status: str = None, dispenser_alvo: int = None,
                    extra: dict = None):
        with self._lock:
            sts   = status or self.state
            os_id = self.os_id
            px    = self.pos_x
            py    = self.pos_y
            ciclo = self.ciclo_atual
            total = self.total_ciclos

        payload = {
            "status":         sts,
            "os_id":          os_id,
            "dispenser_alvo": dispenser_alvo,
            "posicao_x":      round(px, 2),
            "posicao_y":      round(py, 2),
            "ciclo_atual":    ciclo,
            "total_ciclos":   total,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            payload.update(extra)

        self.client.publish("apsen/cnc/status", json.dumps(payload), qos=0, retain=True)

    # ─────────────────────────────────── Telemetria de manutenção ──────────────

    def _telemetria_loop(self):
        """Publica leituras de sensores a cada 30 segundos."""
        while True:
            time.sleep(30)
            with self._lock:
                em_uso = self.state not in ("idle",)
                horas  = self.horas_uso
                ciclos = self.ciclos_total

            ts = datetime.now(timezone.utc).isoformat()

            for comp, t_min, t_max in COMPONENTES_TEMP:
                base  = t_min + (t_max - t_min) * (0.75 if em_uso else 0.2)
                valor = round(base + random.uniform(-1.5, 1.5), 1)
                self.client.publish(
                    "apsen/manut/temperatura",
                    json.dumps({"componente": comp, "valor_c": valor, "timestamp": ts}),
                    qos=0,
                )

            for comp, tipo in COMPONENTES_USO:
                self.client.publish(
                    "apsen/manut/uso",
                    json.dumps({
                        "componente": comp,
                        "tipo":       tipo,
                        "valor":      round(min(100.0, horas / 10.0), 1),
                        "unidade":    "%",
                        "timestamp":  ts,
                    }),
                    qos=0,
                )

            self.client.publish(
                "apsen/manut/uso",
                json.dumps({
                    "componente": "cnc_geral",
                    "tipo":       "horas_uso",
                    "valor":      round(horas, 1),
                    "unidade":    "h",
                    "timestamp":  ts,
                }),
                qos=0,
            )
            self.client.publish(
                "apsen/manut/uso",
                json.dumps({
                    "componente": "cnc_geral",
                    "tipo":       "ciclos",
                    "valor":      ciclos,
                    "unidade":    "ciclos",
                    "timestamp":  ts,
                }),
                qos=0,
            )

    # ──────────────────────────────────────────────────── Main ─────────────────

    def run(self):
        logger.info(
            f"CNC Simulator v2.1 | broker={MQTT_HOST}:{MQTT_PORT} "
            f"| vel={VELOCIDADE_MM_S}mm/s | timeout_pronto={TIMEOUT_PRONTO}s"
        )
        self._ordem_visita = []
        threading.Thread(
            target=self._telemetria_loop, daemon=True, name="telemetria"
        ).start()

        while True:
            try:
                self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                self.client.loop_forever(retry_first_connection=True)
            except Exception as exc:
                logger.error(f"Erro MQTT: {exc}. Reconectando em {RECONNECT_DELAY}s...")
                time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    CNCSimulator().run()
