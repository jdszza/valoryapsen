# -*- coding: utf-8 -*-
"""Placa falsa da balança HX711 — `sub: "weight"`.

O `pesar` leva DUAS quantidades, e confundi-las inverte o teste: a mesa cresce
pela REAL e o desvio é medido contra a ESPERADA. Quando a mesa crescia pelo
esperado, a balança comparava o valor consigo mesma e ficava cega a qualquer
falha de dispensa (CLAUDE.md, "O comando de pesagem leva DUAS quantidades").

Esta placa tem DUAS vozes, porque o firmware de verdade
(`weight/balanca2_3/balanca2_3.ino`) tem duas: a da OS — `tara_ok`, `peso_ok`,
`peso_divergencia`, `erro_sensor`, `telemetria`, que o adapter encaminha ao
central — e a da BANCADA — `boot`, `peso`, `contagem`, `cfg`, `estado`,
`tara_balanca`, `erro_balanca`, que param no adapter e saem por `GET /balanca`.
A segunda existe porque a balança é uma balança de bancada antes de ser um
periférico da célula, e porque com o transporte serial ligado o adapter é o
dono da porta: ninguém mais abre o Monitor Serial para configurar o peso
unitário.

Ver `docs/PROTOCOLO_SERIAL.md` §5.
"""
from __future__ import annotations

from .placa_base import PlacaFalsa, agora

TOLERANCIA_PCT = 5.0

# Vocabulário da máquina de contagem do firmware (`CountState`/`CountResult`).
# Está aqui, e não escrito à mão em cada método, porque é o que o adapter e o
# painel de bancada leem: um nome que divirja do firmware não dá erro, dá uma
# tela que nunca sai de "DESCONHECIDO".
ESTADOS = ("IDLE", "AWAITING_TARA", "AWAITING_DEPOSIT", "TRANSIENT",
           "COUNTING", "DONE")
STATUS_CONTAGEM = ("OK", "UNDER_TOLERANCE", "OVER_TOLERANCE", "UNSTABLE",
                   "INVALID_WEIGHT", "NO_UNIT_WEIGHT")


class PlacaWeight(PlacaFalsa):
    SUBSISTEMA = "weight"

    COMANDOS = {
        "tara":  ("os_id",),
        "pesar": ("os_id", "slot_id", "quantidade_esperada", "peso_unitario_g"),
    }

    # Só no serial: o weight-simulator não tem balança para configurar, então
    # estes não entram em `_ROTAS_SIM` nem na tabela de comandos do documento.
    COMANDOS_BANCADA = {
        "peso_unitario":   ("valor_g",),
        "tara_recipiente": (),
        "tara_canais":     (),
        "contar":          (),
        "config":          (),
        "stream":          (),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mesa_g = 0.0
        self.tara_g = 0.0
        self.anterior_g = 0.0

        # A configuração que o firmware guarda na NVS. Os defaults são os
        # mesmos do `CountConfig` da 2.2.
        self.uw_g = 10.0
        self.tara_recipiente_g = 0.0
        self.tol_g = 1.5
        self.min_itens = 1
        self.max_itens = 9999
        self.sreads = 3
        self.sthres_g = 0.5
        self.estado = "IDLE"
        self.stream_ligado = True

    def eventos_para(self, cmd: str, campos: dict) -> list[dict]:
        if cmd == "tara":
            self.tara_g = self.mesa_g
            self.anterior_g = 0.0
            return [{
                "tipo": "tara_ok",
                "os_id": campos["os_id"],
                "peso_tara_g": round(self.tara_g, 2),
                "ts": agora(),
            }]

        if cmd == "pesar":
            esperada = campos["quantidade_esperada"]
            # `quantidade_real` ausente cai na esperada — é o contrato antigo,
            # preservado para quem não tenha a contagem do dispenser em mãos.
            real = campos.get("quantidade_real")
            real = esperada if real is None else real
            unitario = campos["peso_unitario_g"]

            peso_esperado = esperada * unitario
            self.mesa_g += real * unitario
            liquido = max(0.0, self.mesa_g - self.tara_g)
            delta = max(0.0, liquido - self.anterior_g)
            self.anterior_g = liquido

            # A injeção desloca a LEITURA, não a massa: a mesa segue com o peso
            # certo e o slot seguinte pesa normal. Tirar peso de verdade faria o
            # one-shot deixar de ser one-shot sem ninguém notar.
            injetada = campos.get("injetar_falha") == "divergencia_peso"
            if injetada and peso_esperado > 0:
                delta = max(0.0, delta - peso_esperado * (TOLERANCIA_PCT + 3.0) / 100.0)

            desvio_g = delta - peso_esperado
            desvio_pct = (abs(desvio_g) / peso_esperado * 100.0) if peso_esperado else 0.0
            dentro = desvio_pct <= TOLERANCIA_PCT

            return [{
                "tipo": "peso_ok" if dentro else "peso_divergencia",
                "os_id": campos["os_id"],
                "slot_id": campos["slot_id"],
                "quantidade_esperada": esperada,
                "quantidade_real": real,
                "peso_unitario_g": unitario,
                "peso_esperado_g": round(peso_esperado, 2),
                "peso_medido_g": round(delta, 2),
                "peso_acumulado_g": round(liquido, 2),
                "desvio_g": round(desvio_g, 2),
                "desvio_pct": round(desvio_pct, 2),
                "tolerancia_pct": TOLERANCIA_PCT,
                "dentro_tolerancia": dentro,
                "falha_injetada": injetada,
                "ts": agora(),
            }]

        return self._eventos_bancada(cmd, campos)

    # ── A voz de BANCADA ─────────────────────────────────────────────────────
    #
    # Nenhum destes vai ao central: eles param no adapter (`_EVENTOS_BANCADA`).
    # A conversa que eles cobrem é a do operador com a balança — peso ao vivo,
    # contagem por peso, configuração — e o central não tem endpoint para ela.

    def _eventos_bancada(self, cmd: str, campos: dict) -> list[dict]:
        if cmd == "peso_unitario":
            self.uw_g = float(campos["valor_g"])
            return [self.cfg()]

        if cmd == "tara_recipiente":
            # O firmware ACKa, entra em AWAITING_TARA e só mede no `stepCounting`
            # seguinte. Aqui os dois passos saem juntos: o que o adapter precisa
            # ver é a SEQUÊNCIA, não o intervalo entre eles.
            self.tara_recipiente_g = self.mesa_g
            return [self.evento_estado("AWAITING_TARA"),
                    self.tara_balanca("recipiente", self.tara_recipiente_g),
                    self.cfg(),
                    self.evento_estado("AWAITING_DEPOSIT")]

        if cmd == "tara_canais":
            # Tara de HARDWARE: zera os offsets do HX711 e grava na NVS. Não é
            # a `tara` da OS, que só move o zero lógico da mesa — confundir as
            # duas é tarar com peso em cima, permanentemente.
            return [self.tara_balanca("canais", None)]

        if cmd == "contar":
            if self.uw_g <= 0:
                return [self.erro_balanca("configure o peso unitario primeiro",
                                          "contar")]
            liquido = max(0.0, self.mesa_g - self.tara_recipiente_g)
            exata = liquido / self.uw_g
            return [self.evento_estado("TRANSIENT"),
                    self.evento_estado("COUNTING"),
                    self.contagem(self.mesa_g, liquido, exata),
                    self.evento_estado("DONE")]

        if cmd == "config":
            return [self.cfg()]

        if cmd == "stream":
            self.stream_ligado = bool(campos.get("on", True))
            return [self.cfg()]

        return []

    def boot(self, fw: str = "2.3") -> dict:
        return {
            "tipo": "boot",
            "fw": fw,
            "canais_ativos": [True, True, True, False],
            "uw_g": round(self.uw_g, 2),
            "ts": agora(),
        }

    def peso(self, estavel: bool = True, sat: bool = False) -> dict:
        """O stream periódico, a 5 Hz no firmware.

        `canais_g` traz `null` no canal INATIVO, e não zero: zero é uma
        leitura, e a bancada tem um canal desligado de propósito.
        """
        return {
            "tipo": "peso",
            "total_g": round(self.mesa_g, 2),
            "canais_g": [round(self.mesa_g / 3.0, 2)] * 3 + [None],
            "sat": sat,
            "estavel": estavel,
            "ts": agora(),
        }

    def cfg(self) -> dict:
        return {
            "tipo": "cfg",
            "uw_g": round(self.uw_g, 2),
            "tara_g": round(self.tara_recipiente_g, 2),
            "tol_g": round(self.tol_g, 2),
            "min": self.min_itens,
            "max": self.max_itens,
            "sreads": self.sreads,
            "sthres_g": round(self.sthres_g, 2),
            "ts": agora(),
        }

    def evento_estado(self, estado: str) -> dict:
        assert estado in ESTADOS, estado
        self.estado = estado
        return {"tipo": "estado", "estado": estado, "ts": agora()}

    def contagem(self, total_g: float, liquido_g: float, exata: float,
                 status: str = "OK") -> dict:
        assert status in STATUS_CONTAGEM, status
        return {
            "tipo": "contagem",
            "total_g": round(total_g, 2),
            "liquido_g": round(liquido_g, 2),
            "exata": round(exata, 3),
            "contagem": int(round(exata)),
            "status": status,
            "aceite": status == "OK",
            "ts": agora(),
        }

    def tara_balanca(self, alvo: str, valor_g: float | None) -> dict:
        assert alvo in ("canais", "recipiente"), alvo
        return {
            "tipo": "tara_balanca",
            "alvo": alvo,
            "valor_g": None if valor_g is None else round(valor_g, 2),
            "ts": agora(),
        }

    def erro_balanca(self, msg: str, cmd: str | None = None) -> dict:
        return {"tipo": "erro_balanca", "msg": msg, "cmd": cmd, "ts": agora()}

    # ── Telemetria periódica ─────────────────────────────────────────────────

    def telemetria(self, temperatura_c: float = 24.5) -> dict:
        return {
            "tipo": "telemetria",
            "componente": "hx711_balanca_mesa",
            "temperatura_c": temperatura_c,
            "peso_atual_g": round(self.mesa_g, 2),
            "ts": agora(),
        }
