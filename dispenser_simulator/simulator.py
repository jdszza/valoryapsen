"""
APSEN - Simulador de Dispensers v2.3
======================================
ARQUITETURA — Slots dinâmicos (nenhum dispenser é fixo):

  - O SAP publica a OS com medicamentos (sem dispenser_id).
  - Este simulador decide qual slot físico (1-6) recebe cada remédio:
      1. Verifica se algum slot já tem esse medicamento com residual → usa ele
      2. Se não, pega o primeiro slot vazio (quantidade == 0 / medicamento == None)
      3. Se não há slots disponíveis → publica alerta e rejeita a OS
  - Dispensers NÃO se limpam sozinhos; residual persiste entre OS.
  - Limpeza manual via IHM → MQTT apsen/dispenser/limpar.

Publicações:
  apsen/ia/atribuicao           → mapeamento slot → medicamento decidido pelo sistema
  apsen/dispenser/carregamento  → progresso CV por slot
  apsen/dispenser/pronto        → slot pronto para dispensar
  apsen/dispenser/evento        → cada remédio dispensado (inclui quantidade_residual)
  apsen/dispenser/status        → estado atual de cada slot (com medicamento, qtd, etc.)
  apsen/dispenser/limpeza_ok    → confirmação de limpeza manual
  apsen/manut/temperatura       → telemetria

Subscrições:
  apsen/os/nova              — recebe OS da SAP
  apsen/cnc/status           — detecta posicionado / erro da CNC
  apsen/dispenser/limpar     — limpeza manual solicitada pela IHM
"""

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
NUM_SLOTS       = 6  # slots físicos de dispenser (1..6)

# Temperaturas base por slot (para telemetria)
TEMP_BASE = {d: 22 + d * 0.5 for d in range(1, NUM_SLOTS + 1)}


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class DispenserSimulator:
    def __init__(self):
        # ── Estoque por slot — dinâmico ──────────────────────────────────────
        # Cada slot pode conter qualquer medicamento.
        # Slot "vazio" = medicamento is None e quantidade == 0.
        self._estoque: dict[int, dict] = {
            d: {
                "medicamento": None,  # nome do medicamento atual no slot
                "sku":         None,
                "categoria":   None,
                "quantidade":  0,     # unidades residuais disponíveis
            }
            for d in range(1, NUM_SLOTS + 1)
        }

        # ── Estado operacional por slot ──────────────────────────────────────
        self._estado: dict[int, dict] = {
            d: {
                "status":         "idle",
                "os_id":          None,
                "qtd_alvo":       0,
                "qtd_dispensada": 0,
            }
            for d in range(1, NUM_SLOTS + 1)
        }

        self._lock            = threading.Lock()
        self._os_ativa: dict | None = None
        self._atribuicoes: list[dict] = []   # [{"dispenser_id": int, "medicamento": str, ...}]
        self._erro_ativo      = False
        self._abort_flag      = False

        self._posicionado_event: dict[int, threading.Event] = {
            d: threading.Event() for d in range(1, NUM_SLOTS + 1)
        }

        self.client = mqtt.Client(
            client_id="apsen-dispenser-sim-v23", clean_session=True
        )
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

    # ── MQTT ─────────────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe("apsen/os/nova",          qos=1)
            client.subscribe("apsen/cnc/status",       qos=0)
            client.subscribe("apsen/dispenser/limpar", qos=1)
            logger.info("Dispenser Simulator v2.3 conectado. Slots dinâmicos ATIVO.")
            self._publicar_estado_todos()

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning(f"Desconectado (rc={rc}). Reconectando em {RECONNECT_DELAY}s...")

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
            logger.error(f"[DISP] Erro em mensagem: {exc}", exc_info=True)

    # ── Publicar estado atual de todos os slots ───────────────────────────────

    def _publicar_estado_slot(self, slot_id: int):
        """Publica estado completo de um slot no MQTT (para o dashboard exibir)."""
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

    # ── Roteamento dinâmico de slots ──────────────────────────────────────────

    def _atribuir_slots(self, medicamentos: list[dict]) -> list[dict] | None:
        """
        Para cada medicamento da OS, decide qual slot físico será usado.
        Regra:
          1. Slot que já tem esse medicamento com residual > 0 (reaproveitamento)
          2. Slot vazio (medicamento is None ou quantidade == 0)
        Retorna lista de atribuições ou None se não houver slots suficientes.
        """
        atribuicoes = []
        slots_reservados: set[int] = set()

        with self._lock:
            for item in medicamentos:
                med = item["medicamento"]
                sku = item.get("sku", "")
                cat = item.get("categoria", "")
                qtd = item["quantidade"]

                slot_escolhido = None

                # Passo 1 — slot com mesmo medicamento e estoque residual
                for slot_id in range(1, NUM_SLOTS + 1):
                    if slot_id in slots_reservados:
                        continue
                    est = self._estoque[slot_id]
                    if est["medicamento"] == med and est["quantidade"] > 0:
                        slot_escolhido = slot_id
                        logger.info(
                            f"[DISP-IA] {med} → slot D{slot_id} "
                            f"(residual={est['quantidade']})"
                        )
                        break

                # Passo 2 — slot vazio
                if slot_escolhido is None:
                    for slot_id in range(1, NUM_SLOTS + 1):
                        if slot_id in slots_reservados:
                            continue
                        est = self._estoque[slot_id]
                        if est["medicamento"] is None or est["quantidade"] == 0:
                            # Registra medicamento no slot
                            self._estoque[slot_id].update({
                                "medicamento": med,
                                "sku":         sku,
                                "categoria":   cat,
                            })
                            slot_escolhido = slot_id
                            logger.info(
                                f"[DISP-IA] {med} → slot D{slot_id} (vazio)"
                            )
                            break

                if slot_escolhido is None:
                    logger.error(
                        f"[DISP-IA] Sem slot disponível para '{med}'! "
                        f"Slots em uso: {slots_reservados}"
                    )
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

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _handle_os_nova(self, payload: dict):
        with self._lock:
            if self._os_ativa:
                logger.warning(
                    f"[DISP] OS em andamento ({self._os_ativa.get('os_id')}) "
                    f"— ignorando {payload.get('os_id')}."
                )
                return
            self._os_ativa   = payload
            self._erro_ativo = False
            self._abort_flag = False
            self._atribuicoes = []
            for e in self._posicionado_event.values():
                e.clear()

        logger.info(f"[DISP] OS recebida: {payload.get('os_id')} | {payload.get('kit', '')}")
        threading.Thread(
            target=self._processar_os, args=(payload,), daemon=True
        ).start()

    def _handle_cnc_status(self, payload: dict):
        sts      = payload.get("status")
        disp_alv = payload.get("dispenser_alvo")
        cnc_os   = payload.get("os_id")

        if sts == "posicionado" and disp_alv is not None:
            slot_id = int(disp_alv)
            if slot_id in self._posicionado_event:
                logger.info(f"[DISP-{slot_id}] CNC posicionada — iniciando dispensa!")
                self._posicionado_event[slot_id].set()
        elif sts == "erro":
            with self._lock:
                os_id_ativo = self._os_ativa.get("os_id") if self._os_ativa else None
            if cnc_os and cnc_os == os_id_ativo:
                logger.error(f"[DISP] CNC reportou erro para OS {cnc_os} — abortando.")
                threading.Thread(target=self._abortar_os, daemon=True).start()

    def _handle_limpar(self, payload: dict):
        """Limpeza manual solicitada pela IHM. Zera o slot e libera para outro medicamento."""
        slot_id = payload.get("dispenser_id")
        if not slot_id or slot_id not in range(1, NUM_SLOTS + 1):
            logger.warning(f"[DISP] Limpar: dispenser_id inválido={slot_id}")
            return

        with self._lock:
            em_operacao = (
                self._os_ativa is not None
                and any(a["dispenser_id"] == slot_id for a in self._atribuicoes)
            )
            if em_operacao:
                logger.warning(
                    f"[DISP-{slot_id}] Limpeza solicitada mas slot está em operação. "
                    "Aguardar a OS finalizar."
                )

            med_anterior = self._estoque[slot_id]["medicamento"]
            # Zera slot — libera para qualquer remédio futuro
            self._estoque[slot_id] = {
                "medicamento": None,
                "sku":         None,
                "categoria":   None,
                "quantidade":  0,
            }
            self._estado[slot_id].update({
                "status":         "limpo",
                "qtd_alvo":       0,
                "qtd_dispensada": 0,
            })

        logger.info(
            f"[DISP-{slot_id}] Limpeza manual — slot liberado "
            f"(medicamento anterior: {med_anterior or 'vazio'}) "
            f"| solicitante: {payload.get('solicitado_por', '?')}"
        )

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

    # ── Abort ─────────────────────────────────────────────────────────────────

    def _abortar_os(self):
        with self._lock:
            os_id = self._os_ativa.get("os_id") if self._os_ativa else "?"
            self._os_ativa    = None
            self._atribuicoes = []
            self._erro_ativo  = False
            self._abort_flag  = True
            for e in self._posicionado_event.values():
                e.set()
            for d in self._estado.values():
                d["status"]         = "idle"
                d["os_id"]          = None
                d["qtd_alvo"]       = 0
                d["qtd_dispensada"] = 0

        logger.warning(f"[DISP] OS {os_id} abortada — todos os slots voltaram a idle.")
        self._publicar_estado_todos()

    # ── Processamento da OS ───────────────────────────────────────────────────

    def _processar_os(self, os_payload: dict):
        os_id        = os_payload["os_id"]
        medicamentos = os_payload.get("medicamentos", [])

        # Etapa 1 — Roteamento dinâmico de slots (decisão do sistema de dispensers)
        atribuicoes = self._atribuir_slots(medicamentos)

        if atribuicoes is None:
            logger.error(
                f"[DISP] OS {os_id} REJEITADA — "
                "não há slots disponíveis para todos os medicamentos."
            )
            self.client.publish(
                "apsen/dispenser/status",
                json.dumps({
                    "os_id":     os_id,
                    "status":    "sem_slot_disponivel",
                    "timestamp": _ts(),
                }),
                qos=1,
            )
            with self._lock:
                self._os_ativa = None
            return

        with self._lock:
            self._atribuicoes = atribuicoes

        # Publica atribuição decidida pelo sistema de dispensers
        self.client.publish(
            "apsen/ia/atribuicao",
            json.dumps({
                "os_id":      os_id,
                "atribuicoes": atribuicoes,
                "timestamp":  _ts(),
            }),
            qos=1,
        )
        log_str = " | ".join(
            f"D{a['dispenser_id']}←{a['medicamento']}×{a['quantidade']}"
            for a in atribuicoes
        )
        logger.info(f"[DISP-IA] Roteamento OS {os_id}: {log_str}")

        # Atualiza estado dos slots envolvidos
        with self._lock:
            for a in atribuicoes:
                self._estado[a["dispenser_id"]].update({
                    "os_id":    os_id,
                    "qtd_alvo": a["quantidade"],
                })
        for a in atribuicoes:
            self._publicar_estado_slot(a["dispenser_id"])

        # Etapa 2 — Carregamento paralelo (com persistência de residual)
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
            logger.error(f"[DISP] OS {os_id} encerrada na fase de carregamento.")
            with self._lock:
                self._os_ativa = None
            return

        logger.info(f"[DISP] Todos os slots prontos — OS {os_id}!")

        # Etapa 3 — Dispensa paralela (aguarda CNC posicionar para cada slot)
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
            for a in atribuicoes:
                self._estado[a["dispenser_id"]]["os_id"] = None

        logger.info(f"[DISP] OS {os_id} finalizada.")
        for a in atribuicoes:
            self._publicar_estado_slot(a["dispenser_id"])

    # ── Fase 1: Carregamento com persistência de residual ─────────────────────

    def _fase_carregamento(
        self, os_id: str, slot_id: int, medicamento: str, sku: str, quantidade: int
    ):
        with self._lock:
            estoque_atual = self._estoque[slot_id]["quantidade"]

        to_load = max(0, quantidade - estoque_atual)

        if estoque_atual >= quantidade:
            logger.info(
                f"[DISP-{slot_id}] Residual suficiente: "
                f"{estoque_atual} >= {quantidade} — sem carregamento."
            )
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
            logger.info(
                f"[DISP-{slot_id}] Carregando {to_load} un. de {medicamento} "
                f"| residual={estoque_atual}"
            )
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
                        f"Código de barras inválido (unidade {i})",
                        f"Unidade {i} danificada (CV)",
                        f"Contaminação detectada (unidade {i})",
                    ])
                    logger.error(f"[DISP-{slot_id}] ERRO CV unidade {i}: {motivo}")
                    self.client.publish(
                        "apsen/dispenser/carregamento",
                        json.dumps({
                            "os_id":         os_id,
                            "dispenser_id":  slot_id,
                            "medicamento":   medicamento,
                            "status":        "erro",
                            "unidade":       i,
                            "motivo_falha":  motivo,
                            "timestamp":     _ts(),
                        }),
                        qos=1,
                    )
                    with self._lock:
                        self._erro_ativo              = True
                        self._estado[slot_id]["status"] = "erro"
                    self._publicar_estado_slot(slot_id)
                    return

            # Carregamento concluído — atualiza quantidade no slot
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

        # Slot pronto
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
        logger.info(f"[DISP-{slot_id}] PRONTO (estoque={qtd_residual}).")

    # ── Fase 2: Dispensa ──────────────────────────────────────────────────────

    def _fase_dispensa(self, os_id: str, slot_id: int, medicamento: str, quantidade: int):
        logger.info(f"[DISP-{slot_id}] Aguardando CNC posicionar (timeout={TIMEOUT_CNC_POS}s)...")

        chegou = self._posicionado_event[slot_id].wait(timeout=TIMEOUT_CNC_POS)
        with self._lock:
            abort = self._abort_flag

        if not chegou or abort:
            logger.error(
                f"[DISP-{slot_id}] "
                + ("Timeout aguardando CNC." if not chegou else "Abortado.")
            )
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
                    "Peso fora do padrão",
                    "Formato irregular (IA)",
                ])
                logger.warning(f"[DISP-{slot_id}] IA rejeitou unidade {i}: {motivo_falha}")
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

        # Atualiza residual definitivo
        with self._lock:
            residual_final = max(0, self._estoque[slot_id]["quantidade"] - quantidade)
            self._estoque[slot_id]["quantidade"] = residual_final
            self._estado[slot_id].update({
                "status":         "concluido",
                "qtd_dispensada": dispensado,
            })

        logger.info(
            f"[DISP-{slot_id}] CONCLUÍDO: {dispensado}/{quantidade} × {medicamento} "
            f"| RESIDUAL={residual_final}"
        )

        # Publica evento final (para o backend persistir)
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

    # ── Telemetria ─────────────────────────────────────────────────────────────

    def _telemetria_loop(self):
        while True:
            time.sleep(15)
            ts = _ts()
            with self._lock:
                estados = {d: e["status"] for d, e in self._estado.items()}

            for slot_id, sts in estados.items():
                em_uso = sts in ("carregando", "dispensando")
                valor  = round(
                    TEMP_BASE[slot_id]
                    + (4.0 if em_uso else 0.5)
                    + random.uniform(-0.5, 0.5),
                    1,
                )
                self.client.publish(
                    "apsen/manut/temperatura",
                    json.dumps({
                        "componente": f"dispenser_{slot_id}",
                        "valor_c":    valor,
                        "timestamp":  ts,
                    }),
                    qos=0,
                )

            # Republica estado de todos os slots a cada ciclo de telemetria
            # (garante que o dashboard fique atualizado mesmo após reconexão)
            self._publicar_estado_todos()

    # ── Main ───────────────────────────────────────────────────────────────────

    def run(self):
        logger.info(
            f"Dispenser Simulator v2.3 | broker={MQTT_HOST}:{MQTT_PORT} | "
            f"{NUM_SLOTS} slots dinâmicos | "
            f"PROB_IA={PROB_FALHA_IA:.0%} | PROB_CV={PROB_ERRO_CV:.0%} | "
            f"TIMEOUT_CNC={TIMEOUT_CNC_POS}s"
        )
        threading.Thread(target=self._telemetria_loop, daemon=True, name="telemetria").start()

        while True:
            try:
                self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                self.client.loop_forever(retry_first_connection=True)
            except Exception as exc:
                logger.error(f"Erro MQTT: {exc}. Reconectando em {RECONNECT_DELAY}s...")
                time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    DispenserSimulator().run()
