# -*- coding: utf-8 -*-
"""Placa falsa da mesa CNC — `sub: "cnc"`.

Ela NÃO tem mapa de posições: move-se para o par `posicao_x`/`posicao_y` que
vem no comando. A geometria da célula tem um dono só, e é o central (CLAUDE.md,
"A cópia do mapa no cnc_simulator não existe mais") — uma cópia no firmware não
daria erro, daria movimento para o lugar errado num slot só, que é o quadro de
uma falha mecânica.

Ver `docs/PROTOCOLO_SERIAL.md` §4.
"""
from __future__ import annotations

from .placa_base import PlacaFalsa, agora


class PlacaCNC(PlacaFalsa):
    SUBSISTEMA = "cnc"

    COMANDOS = {
        # `posicao_x`/`posicao_y` são obrigatórias no `mover` porque é delas que
        # a mesa se move; no `homing` são opcionais, como no contrato HTTP —
        # o firmware cai no HOME de fábrica quando não vêm.
        "mover":  ("dispenser_alvo", "os_id", "posicao_x", "posicao_y"),
        "homing": ("os_id",),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pos_x = -120.0
        self.pos_y = 0.0

    def eventos_para(self, cmd: str, campos: dict) -> list[dict]:
        if cmd == "mover":
            self.pos_x = campos["posicao_x"]
            self.pos_y = campos["posicao_y"]
            return [{
                "tipo": "posicionado",
                "os_id": campos["os_id"],
                "dispenser_alvo": campos["dispenser_alvo"],
                "posicao_x": self.pos_x,
                "posicao_y": self.pos_y,
                "ciclo_atual": campos.get("ciclo_atual", 0),
                "total_ciclos": campos.get("total_ciclos", 0),
                "ts": agora(),
            }]

        if cmd == "homing":
            self.pos_x = campos.get("posicao_x", -120.0)
            self.pos_y = campos.get("posicao_y", 0.0)
            return [{
                "tipo": "concluido",
                "os_id": campos["os_id"],
                "posicao_x": self.pos_x,
                "posicao_y": self.pos_y,
                "ts": agora(),
            }]

        return []

    # ── Trajetória ao vivo ───────────────────────────────────────────────────

    def movendo(self, os_id: str, alvo: int, x: float, y: float,
                passo: int = 1, total_passos: int = 10) -> dict:
        """Um passo da trajetória — periódico, e por isso sujeito ao filtro de
        repetição do adapter. Só `posicionado`, `concluido` e `erro` viram linha
        no banco do central."""
        return {
            "tipo": "movendo",
            "os_id": os_id,
            "dispenser_alvo": alvo,
            "posicao_x": x,
            "posicao_y": y,
            "passo": passo,
            "total_passos": total_passos,
            "progresso_pct": round(passo / total_passos * 100, 1),
            "ts": agora(),
        }
