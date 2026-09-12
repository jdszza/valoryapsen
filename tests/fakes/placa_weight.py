# -*- coding: utf-8 -*-
"""Placa falsa da balança HX711 — `sub: "weight"`.

O `pesar` leva DUAS quantidades, e confundi-las inverte o teste: a mesa cresce
pela REAL e o desvio é medido contra a ESPERADA. Quando a mesa crescia pelo
esperado, a balança comparava o valor consigo mesma e ficava cega a qualquer
falha de dispensa (CLAUDE.md, "O comando de pesagem leva DUAS quantidades").

Ver `docs/PROTOCOLO_SERIAL.md` §5.
"""
from __future__ import annotations

from .placa_base import PlacaFalsa, agora

TOLERANCIA_PCT = 5.0


class PlacaWeight(PlacaFalsa):
    SUBSISTEMA = "weight"

    COMANDOS = {
        "tara":  ("os_id",),
        "pesar": ("os_id", "slot_id", "quantidade_esperada", "peso_unitario_g"),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mesa_g = 0.0
        self.tara_g = 0.0
        self.anterior_g = 0.0

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

        return []

    # ── Telemetria periódica ─────────────────────────────────────────────────

    def telemetria(self, temperatura_c: float = 24.5) -> dict:
        return {
            "tipo": "telemetria",
            "componente": "hx711_balanca_mesa",
            "temperatura_c": temperatura_c,
            "peso_atual_g": round(self.mesa_g, 2),
            "ts": agora(),
        }
