"""
APSEN - Simulador de Dispensers v2.3
Slots dinamicos: nenhum dispenser e fixo a um medicamento.
"""
import collections
import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DISP-SIM] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

MQTT_HOST       = os.getenv("MQTT_HOST",    "mosquitto")
MQTT_PORT       = int(os.getenv("MQTT_PORT", "1883"))
T_CARGA_UNID    = float(os.getenv("T_CARGA_UNID",   "0.3"))
T_DISPENSA_UNID = float(os.getenv("T_DISPENSA_UNID", "0.5"))
PROB_FALHA_IA   = float(os.getenv("PROB_FALHA_IA",   "0.05"))
PROB_ERRO_CV    = float(os.getenv("PROB_ERRO_CV",    "0.01"))
TIMEOUT_CNC_POS = float(os.getenv("TIMEOUT_CNC_POS", "240"))
RECONNECT_DELAY = 5
NUM_SLOTS       = 6

TEMP_BASE = {d: 22 + d * 0.5 for d in range(1, NUM_SLOTS + 1)}


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class DispenserSimulator:
    def __init__(self):
        self._estoque: dict = {
            d: {"medicamento": None, "sku": None, "categoria": None, "quantidade": 0}
            for d in range(1, NUM_SLOTS + 1)
        }
        self._estado: dict = {
            d: {"status": "idle", "os_id": None, "qtd_alvo": 0, "qtd_dispensada": 0}
            for d in range(1, NUM_SLOTS + 1)
        }
        self._lock            = threading.Lock()
        self._os_queue        = collections.deque()
        self._os_ativa        = None
        self._processando     = False
        self._atribuicoes     = []
        self._erro_ativo      = False
        self._abort_flag      = False
        self._posicionado_event = {d: threading.Event() for d in range(1, NUM_SLOTS + 1)}

        self.client = mqtt.Client(client_id="apsen-dispenser-sim-v23", clean_session=True)
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

    # -- MQTT ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe("apsen/os/nova",          qos=1)
            client.subscribe("apsen/cnc/status",       qos=0)
            client.subscribe("apsen/dispenser/limpar", qos=1)
            logger.info("Dispenser Simulator v2.3 conectado. Slots dinamicos ATIVO.")
            self._publicar_estado_todos()

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning("Desconectado (rc=%d). Reconectando em %ds...", rc, RECONNECT_DELAY)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            topic   = msg.topic
            if topic == "apsen/os/nova":
                self._handle_os_nova(payload)
            elif topic == "apsen/cnc/status":
                self._handle_cnc_status(payload)
            elif topic == "apsen/dispenser/limpar":
                self._handle_limpar(payload)
        except Exception as exc:
            logger.error("[DISP] Erro em mensagem: %s", exc, exc_info=True)

    # -- Estado ----------------------------------------------------------------

    def _publicar_estado_slot(self, slot_id: int):
        with self._lock:
            est = self._estoque[slot_id]
            sts = self._estado[slot_id]
        self.client.publish(
            "apsen/dispenser/status",
            json.dumps({
                "dispenser_id":   slot_id,
                "medicamento":    est["medicamento"],
                "sku":            est["sku"],
                "categoria":      est["categoria"],
                "quantidade":     est["quantidade"],
                "status":         sts["status"],
                "os_id":          sts["os_id"],
                "qtd_alvo":       sts["qtd_alvo"],
                "qtd_dispensada": sts["qtd_dispensada"],
                "timestamp":      _ts(),
            }),
            qos=0,
        )

    def _publicar_estado_todos(self):
        for slot_id in range(1, NUM_SLOTS + 1):
            self._publicar_estado_slot(slot_id)

    # -- Roteamento dinamico de slots ------------------------------------------

    def _atribuir_slots(self, medicamentos: list) -> list | None:
        atribuicoes      = []
        slots_reservados = set()

        with self._lock:
            for item in medicamentos:
                med = item["medicamento"]
                sku = item.get("sku", "")
                cat = item.get("categoria", "")
                qtd = item["quantidade"]
                slot_escolhido = None

                # Passo 1: slot com mesmo medicamento e residual
                for slot_id in range(1, NUM_SLOTS + 1):
                    if slot_id in slots_reservados:
                        continue
                    est = self._estoque[slot_id]
                    if est["medicamento"] == med and est["quantidade"] > 0:
                        slot_escolhido = slot_id
                        logger.info("[DISP-IA] %s -> slot D%d (residual=%d)",
                                    med, slot_id, est["quantidade"])
                        break

                # Passo 2: slot vazio
                if slot_escolhido is None:
                    for slot_id in range(1, NUM_SLOTS + 1):
                        if slot_id in slots_reservados:
                            continue
                        est = self._estoque[slot_id]
                        if est["medicamento"] is None or est["quantidade"] == 0:
                            self._estoque[slot_id].update({
                                "medicamento": med,
                                "sku":         sku,
                                "categoria":   cat,
                            })
                            slot_escolhido = slot_id
                            logger.info("[DISP-IA] %s -> slot D%d (vazio)", med, slot_id)
                            break

                if slot_escolhido is None:
                    logger.error("[DISP-IA] Sem slot disponivel para '%s'! Slots em uso: %s",
                                 med, slots_reservados)
                    return None

                slots_reservados.add(slot_escolhido)
                atribuicoes.append({
                    "dispenser_id": slot_escolhido,
                    "medicamento":  med,
                    "sku":          sku,
                    "categoria":    cat,
                    "quantidade":   qtd,
                })

        return atribuicoes

    # -- Handlers --------------------------------------------------------------

    def _handle_os_nova(self, payload: dict):
        os_id = payload.get("os_id", "?")
        with self._lock:
            self._os_queue.append(payload)
            posicao        = len(self._os_queue)
            ja_processando = self._processando

        if posicao > 1 or ja_processando:
            logger.info("[DISP] OS %s enfileirada (posicao %d). Aguardando OS atual.",
                        os_id, posicao)
            self.client.publish(
                "apsen/os/fila",
                json.dumps({"os_id": os_id, "posicao": posicao,
                            "status": "aguardando_fila", "timestamp": _ts()}),
                qos=0,
            )
        else:
            logger.info("[DISP] OS %s recebida - iniciando processamento.", os_id)

        if not ja_processando:
            threading.Thread(target=self._processar_fila, daemon=True).start()

    def _processar_fila(self):
        while True:
            with self._lock:
                if not self._os_queue:
                    self._processando = False
                    return
                if self._processando:
                    return
                payload           = self._os_queue.popleft()
                self._processando = True
            self._processar_os_unica(payload)

    def _handle_cnc_status(self, payload: dict):
        sts      = payload.get("status")
        disp_alv = payload.get("dispenser_alvo")
        cnc_os   = payload.get("os_id")

        if sts == "posicionado" and disp_alv is not None:
            slot_id = int(disp_alv)
            if slot_id in self._posicionado_event:
                logger.info("[DISP-%d] CNC posicionada - iniciando dispensa!", slot_id)
                self._posicionado_event[slot_id].set()
        elif sts == "erro":
            with self._lock:
                os_id_ativo = self._os_ativa.get("os_id") if self._os_ativa else None
            if cnc_os and cnc_os == os_id_ativo:
                logger.error("[DISP] CNC reportou erro para OS %s - abortando.", cnc_os)
                threading.Thread(target=self._abortar_os, daemon=True).start()

    def _handle_limpar(self, payload: dict):
        slot_id = payload.get("dispenser_id")
        if not slot_id or slot_id not in range(1, NUM_SLOTS + 1):
            logger.warning("[DISP] Limpar: dispenser_id invalido=%s", slot_id)
            return

        with self._lock:
            em_operacao  = (
                self._os_ativa is not None
                and any(a["dispenser_id"] == slot_id for a in self._atribuicoes)
            )
            status_atual = self._estado[slot_id].get("status", "idle")
            bloqueado    = em_operacao or status_atual in ("carregando", "pronto", "dispensando")

        if bloqueado:
            os_id = (self._os_ativa or {}).get("os_id", "?")
            logger.warning("[DISP-%d] Limpeza RECUSADA - slot em operacao (status=%s) na OS %s.",
                           slot_id, status_atual, os_id)
            self.client.publish(
                "apsen/dispenser/limpeza_erro",
                json.dumps({
                    "dispenser_id": slot_id,
                    "erro":         "em_operacao",
                    "status_atual": status_atual,
                    "os_id":        os_id,
                    "mensagem":     (
                        "Dispenser " + str(slot_id) + " esta realizando contagem "
                        "(OS " + str(os_id) + " - status: " + str(status_atual) + "). "
                        "Aguarde a OS finalizar."
                    ),
                    "timestamp":    _ts(),
                }),
                qos=1,
            )
            return

        with self._lock:
            med_anterior = self._estoque[slot_id]["medicamento"]
            self._estoque[slot_id] = {
                "medicamento": None, "sku": None, "categoria": None, "quantidade": 0
            }
            self._estado[slot_id].update({
                "status": "limpo", "qtd_alvo": 0, "qtd_dispensada": 0
            })

        logger.info("[DISP-%d] Limpeza executada (anterior: %s) | solicitante: %s",
                    slot_id, med_anterior or "vazio", payload.get("solicitado_por", "?"))
        self.client.publish(
            "apsen/dispenser/limpeza_ok",
            json.dumps({
                "dispenser_id":      slot_id,
                "medicamento_limpo": med_anterior,
                "solicitado_por":    payload.get("solicitado_por"),
                "timestamp":         _ts(),
            }),
            qos=1,
        )
        self._publicar_estado_slot(slot_id)

    # -- Abort -----------------------------------------------------------------

    def _abortar_os(self):
        with self._lock:
            os_id             = self._os_ativa.get("os_id") if self._os_ativa else "?"
            self._os_ativa    = None
            self._atribuicoes = []
            self._erro_ativo  = False
            self._abort_flag  = True
            self._processando = False
            for e in self._posicionado_event.values():
                e.set()
            for d in self._estado.values():
                d["status"]         = "idle"
                d["os_id"]          = None
                d["qtd_alvo"]       = 0
                d["qtd_dispensada"] = 0

        logger.warning("[DISP] OS %s abortada - todos os slots idle.", os_id)
        self.client.publish(
            "apsen/os/concluida",
            json.dumps({"os_id": os_id, "status": "abortada", "timestamp": _ts()}),
            qos=1,
        )
        self._publicar_estado_todos()
        threading.Thread(target=self._processar_fila, daemon=True).start()

    # -- Processamento da OS ---------------------------------------------------

    def _processar_os_unica(self, os_payload: dict):
        os_id        = os_payload["os_id"]
        medicamentos = os_payload.get("medicamentos", [])

        with self._lock:
            self._os_ativa    = os_payload
            self._erro_ativo  = False
            self._abort_flag  = False
            self._atribuicoes = []
            for e in self._posicionado_event.values():
                e.clear()

        logger.info("[DISP] Iniciando OS %s | %d medicamentos", os_id, len(medicamentos))

        atribuicoes = self._atribuir_slots(medicamentos)

        if atribuicoes is None:
            logger.error("[DISP] OS %s REJEITADA - sem slots disponiveis.", os_id)
            self.client.publish(
                "apsen/dispenser/status",
                json.dumps({"os_id": os_id, "status": "sem_slot_disponivel", "timestamp": _ts()}),
                qos=1,
            )
            with self._lock:
                self._os_ativa    = None
                self._processando = False
            threading.Thread(target=self._processar_fila, daemon=True).start()
            return

        with self._lock:
            self._atribuicoes = atribuicoes

        self.client.publish(
            "apsen/ia/atribuicao",
            json.dumps({"os_id": os_id, "atribuicoes": atribuicoes, "timestamp": _ts()}),
            qos=1,
        )
        log_str = " | ".join(
            "D" + str(a["dispenser_id"]) + "<-" + a["medicamento"] + "x" + str(a["quantidade"])
            for a in atribuicoes
        )
        logger.info("[DISP-IA] Roteamento OS %s: %s", os_id, log_str)

        with self._lock:
            for a in atribuicoes:
                self._estado[a["dispenser_id"]].update({
                    "os_id":    os_id,
                    "qtd_alvo": a["quantidade"],
                })
        for a in atribuicoes:
            self._publicar_estado_slot(a["dispenser_id"])

        # Fase 2: Carregamento paralelo
        threads_carga = [
            threading.Thread(
                target=self._fase_carregamento,
                args=(os_id, a["dispenser_id"], a["medicamento"], a["sku"], a["quantidade"]),
                daemon=True,
            )
            for a in atribuicoes
        ]
        for t in threads_carga:
            t.start()
        for t in threads_carga:
            t.join()

        with self._lock:
            erro  = self._erro_ativo
            abort = self._abort_flag

        if erro or abort:
            logger.error("[DISP] OS %s encerrada na fase de carregamento.", os_id)
            with self._lock:
                self._os_ativa    = None
                self._atribuicoes = []
                self._processando = False
            self.client.publish(
                "apsen/os/concluida",
                json.dumps({"os_id": os_id, "status": "erro_carregamento", "timestamp": _ts()}),
                qos=1,
            )
            threading.Thread(target=self._processar_fila, daemon=True).start()
            return

        logger.info("[DISP] Todos os slots prontos - OS %s!", os_id)

        # Fase 3: Dispensa paralela
        threads_disp = [
            threading.Thread(
                target=self._fase_dispensa,
                args=(os_id, a["dispenser_id"], a["medicamento"], a["quantidade"]),
                daemon=True,
            )
            for a in atribuicoes
        ]
        for t in threads_disp:
            t.start()
        for t in threads_disp:
            t.join()

        with self._lock:
            self._os_ativa    = None
            self._atribuicoes = []
            self._abort_flag  = False
            self._processando = False
            for a in atribuicoes:
                self._estado[a["dispenser_id"]]["os_id"] = None

        logger.info("[DISP] OS %s finalizada.", os_id)
        for a in atribuicoes:
            self._publicar_estado_slot(a["dispenser_id"])

        fila_restante = 0
        with self._lock:
            fila_restante = len(self._os_queue)
        self.client.publish(
            "apsen/os/concluida",
            json.dumps({
                "os_id":         os_id,
                "status":        "concluida",
                "fila_restante": fila_restante,
                "timestamp":     _ts(),
            }),
            qos=1,
        )

        if fila_restante > 0:
            logger.info("[DISP] Fila: %d OS aguardando. Processando proxima...", fila_restante)
            threading.Thread(target=self._processar_fila, daemon=True).start()

    # -- Fase 1: Carregamento --------------------------------------------------

    def _fase_carregamento(self, os_id: str, slot_id: int, medicamento: str,
                           sku: str, quantidade: int):
        with self._lock:
            estoque_atual = self._estoque[slot_id]["quantidade"]

        to_load = max(0, quantidade - estoque_atual)

        if estoque_atual >= quantidade:
            logger.info("[DISP-%d] Residual suficiente: %d >= %d - sem carregamento.",
                        slot_id, estoque_atual, quantidade)
            self.client.publish(
                "apsen/dispenser/carregamento",
                json.dumps({
                    "os_id":                os_id,
                    "dispenser_id":         slot_id,
                    "medicamento":          medicamento,
                    "sku":                  sku,
                    "status":               "ok_residual",
                    "quantidade_carregada": 0,
                    "quantidade_residual":  estoque_atual,
                    "quantidade_total":     estoque_atual,
                    "timestamp":            _ts(),
                }),
                qos=0,
            )
        else:
            logger.info("[DISP-%d] Carregando %d un. de %s | residual=%d",
                        slot_id, to_load, medicamento, estoque_atual)
            with self._lock:
                self._estado[slot_id].update({
                    "status":         "carregando",
                    "os_id":          os_id,
                    "qtd_alvo":       quantidade,
                    "qtd_dispensada": 0,
                })
            self._publicar_estado_slot(slot_id)

            self.client.publish(
                "apsen/dispenser/carregamento",
                json.dumps({
                    "os_id":        os_id,
                    "dispenser_id": slot_id,
                    "medicamento":  medicamento,
                    "sku":          sku,
                    "status":       "iniciado",
                    "a_carregar":   to_load,
                    "residual":     estoque_atual,
                    "timestamp":    _ts(),
                }),
                qos=0,
            )

            for i in range(1, to_load + 1):
                with self._lock:
                    if self._erro_ativo or self._abort_flag:
                        return
                time.sleep(T_CARGA_UNID)

                if random.random() < PROB_ERRO_CV:
                    motivo = random.choice([
                        "Codigo de barras invalido (unidade " + str(i) + ")",
                        "Unidade " + str(i) + " danificada (CV)",
                        "Contaminacao detectada (unidade " + str(i) + ")",
                    ])
                    logger.error("[DISP-%d] ERRO CV unidade %d: %s", slot_id, i, motivo)
                    self.client.publish(
                        "apsen/dispenser/carregamento",
                        json.dumps({
                            "os_id":        os_id,
                            "dispenser_id": slot_id,
                            "medicamento":  medicamento,
                            "status":       "erro",
                            "unidade":      i,
                            "motivo_falha": motivo,
                            "timestamp":    _ts(),
                        }),
                        qos=1,
                    )
                    with self._lock:
                        self._erro_ativo               = True
                        self._estado[slot_id]["status"] = "erro"
                    self._publicar_estado_slot(slot_id)
                    return

            with self._lock:
                self._estoque[slot_id]["quantidade"] = estoque_atual + to_load

            self.client.publish(
                "apsen/dispenser/carregamento",
                json.dumps({
                    "os_id":                os_id,
                    "dispenser_id":         slot_id,
                    "medicamento":          medicamento,
                    "sku":                  sku,
                    "status":               "ok",
                    "quantidade_carregada": to_load,
                    "quantidade_total":     estoque_atual + to_load,
                    "timestamp":            _ts(),
                }),
                qos=0,
            )

        with self._lock:
            self._estado[slot_id]["status"] = "pronto"
            qtd_residual = self._estoque[slot_id]["quantidade"]

        self.client.publish(
            "apsen/dispenser/pronto",
            json.dumps({
                "os_id":               os_id,
                "dispenser_id":        slot_id,
                "medicamento":         medicamento,
                "sku":                 sku,
                "quantidade_alvo":     quantidade,
                "quantidade_residual": qtd_residual,
                "timestamp":           _ts(),
            }),
            qos=1,
        )
        self._publicar_estado_slot(slot_id)
        logger.info("[DISP-%d] PRONTO (estoque=%d).", slot_id, qtd_residual)

    # -- Fase 2: Dispensa ------------------------------------------------------

    def _fase_dispensa(self, os_id: str, slot_id: int, medicamento: str, quantidade: int):
        logger.info("[DISP-%d] Aguardando CNC posicionar (timeout=%ds)...",
                    slot_id, TIMEOUT_CNC_POS)

        chegou = self._posicionado_event[slot_id].wait(timeout=TIMEOUT_CNC_POS)
        with self._lock:
            abort = self._abort_flag

        if not chegou or abort:
            logger.error("[DISP-%d] %s", slot_id,
                         "Timeout aguardando CNC." if not chegou else "Abortado.")
            if not chegou:
                threading.Thread(target=self._abortar_os, daemon=True).start()
            return

        with self._lock:
            self._estado[slot_id]["status"] = "dispensando"
        self._publicar_estado_slot(slot_id)

        dispensado = 0
        for i in range(1, quantidade + 1):
            with self._lock:
                if self._abort_flag:
                    return
            time.sleep(T_DISPENSA_UNID)

            validado     = random.random() >= PROB_FALHA_IA
            motivo_falha = None

            if not validado:
                motivo_falha = random.choice([
                    "Comprimido partido (IA)",
                    "Peso fora do padrao",
                    "Formato irregular (IA)",
                ])
                logger.warning("[DISP-%d] IA rejeitou unidade %d: %s", slot_id, i, motivo_falha)
            else:
                dispensado += 1

            with self._lock:
                self._estado[slot_id]["qtd_dispensada"] = dispensado
                residual_prov = max(0, self._estoque[slot_id]["quantidade"] - i)

            self.client.publish(
                "apsen/dispenser/evento",
                json.dumps({
                    "os_id":                 os_id,
                    "dispenser_id":          slot_id,
                    "medicamento":           medicamento,
                    "seq":                   i,
                    "quantidade_dispensada": dispensado,
                    "quantidade_alvo":       quantidade,
                    "validado":              validado,
                    "motivo_falha":          motivo_falha,
                    "quantidade_residual":   residual_prov,
                    "timestamp":             _ts(),
                }),
                qos=0,
            )

        with self._lock:
            residual_final = max(0, self._estoque[slot_id]["quantidade"] - quantidade)
            self._estoque[slot_id]["quantidade"] = residual_final
            self._estado[slot_id].update({
                "status":         "concluido",
                "qtd_dispensada": dispensado,
            })

        logger.info("[DISP-%d] CONCLUIDO: %d/%d x %s | RESIDUAL=%d",
                    slot_id, dispensado, quantidade, medicamento, residual_final)

        self.client.publish(
            "apsen/dispenser/evento",
            json.dumps({
                "os_id":                 os_id,
                "dispenser_id":          slot_id,
                "medicamento":           medicamento,
                "seq":                   quantidade,
                "quantidade_dispensada": dispensado,
                "quantidade_alvo":       quantidade,
                "validado":              True,
                "quantidade_residual":   residual_final,
                "timestamp":             _ts(),
            }),
            qos=1,
        )
        self._publicar_estado_slot(slot_id)

    # -- Telemetria ------------------------------------------------------------

    def _telemetria_loop(self):
        while True:
            time.sleep(15)
            ts = _ts()
            with self._lock:
                estados = {d: e["status"] for d, e in self._estado.items()}

            for slot_id, sts in estados.items():
                em_uso = sts in ("carregando", "dispensando")
                valor  = round(
                    TEMP_BASE[slot_id] + (4.0 if em_uso else 0.5) + random.uniform(-0.5, 0.5),
                    1,
                )
                self.client.publish(
                    "apsen/manut/temperatura",
                    json.dumps({
                        "componente": "dispenser_" + str(slot_id),
                        "valor_c":    valor,
                        "timestamp":  ts,
                    }),
                    qos=0,
                )

            self._publicar_estado_todos()

    # -- Main ------------------------------------------------------------------

    def run(self):
        logger.info(
            "Dispenser Simulator v2.3 | broker=%s:%d | %d slots dinamicos | "
            "PROB_IA=%.0f%% | PROB_CV=%.0f%% | TIMEOUT_CNC=%ds",
            MQTT_HOST, MQTT_PORT, NUM_SLOTS,
            PROB_FALHA_IA * 100, PROB_ERRO_CV * 100, TIMEOUT_CNC_POS,
        )
        threading.Thread(target=self._telemetria_loop, daemon=True, name="telemetria").start()

        while True:
            try:
                self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                self.client.loop_forever(retry_first_connection=True)
            except Exception as exc:
                logger.error("Erro MQTT: %s. Reconectando em %ds...", exc, RECONNECT_DELAY)
                time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    DispenserSimulator().run()
