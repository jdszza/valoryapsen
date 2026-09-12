# -*- coding: utf-8 -*-
"""Placa falsa das 8 telas TFT — `sub: "dispenser_tft"`.

A SEGUNDA placa do dispenser-adapter, numa SEGUNDA porta. Acionar os 8
mecanismos, desenhar 8 telas e manter a serial não cabe num ESP só; o adapter
que já vê todo comando que desce e todo evento que sobe do slot é quem
espelha o que cada tela mostra — um tft-adapter separado obrigaria o central a
mandar a mesma informação duas vezes, e duas cópias divergem.

Ela não emite evento de resultado: `slot` e `estado_celula` só PINTAM. O que
sobe são `telemetria` (telas vivas, brilho) e `erro` (uma tela que não
respondeu), e nenhum dos dois vai ao central — ficam no adapter, em log e no
`/health`. Ver `docs/PROTOCOLO_SERIAL.md` §6.
"""
from __future__ import annotations

from .placa_base import PlacaFalsa, agora


class PlacaDispenserTFT(PlacaFalsa):
    SUBSISTEMA = "dispenser_tft"

    # cmd -> campos obrigatórios. `trava_slot_id` pode vir nulo (trava sem
    # slot), mas a CHAVE tem que vir: é ela que diz a cada tela se "é este
    # slot" ou "é outro".
    COMANDOS = {
        "slot": ("dispenser_id", "medicamento", "sku", "categoria",
                 "quantidade_alvo", "quantidade_dispensada",
                 "quantidade_residual", "status", "os_id"),
        "estado_celula": ("trava_ativa", "trava_slot_id", "os_id", "trava_resumo"),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # O que cada tela está mostrando — é contra isto que se prova que o
        # adapter espelhou a transição certa.
        self.telas: dict[int, dict] = {}
        self.celula: dict = {"trava_ativa": False, "trava_slot_id": None,
                             "os_id": "", "trava_resumo": ""}

    def eventos_para(self, cmd: str, campos: dict) -> list[dict]:
        if cmd == "slot":
            self.telas[campos["dispenser_id"]] = dict(campos)
        elif cmd == "estado_celula":
            self.celula = dict(campos)
        # Pintar não produz resultado: o ACK já disse "aceitei", e não há
        # "terminei" a esperar — as telas são cosméticas para o fluxo.
        return []

    # ── O que a placa emite por conta própria ────────────────────────────────

    def telemetria(self, telas_ok: int = 8, brilho_pct: int = 80) -> dict:
        return {
            "tipo": "telemetria",
            "telas_ok": telas_ok,
            "brilho_pct": brilho_pct,
            "ts": agora(),
        }

    def erro(self, slot: int = 3, codigo: str = "tela_sem_resposta") -> dict:
        return {
            "tipo": "erro",
            "dispenser_id": slot,
            "codigo_erro": codigo,
            "descricao": f"tela D{slot} nao respondeu ao redesenho",
            "ts": agora(),
        }
