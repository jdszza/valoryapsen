# -*- coding: utf-8 -*-
"""Placa falsa dos 8 dispensers — `sub: "dispenser"`.

Uma porta para os oito slots: o `dispenser_id` vai no comando. Os nomes de campo
são os mesmos do payload HTTP de hoje, e isso não é preguiça — o evento é
repassado CRU ao central, então renomear no serial obrigaria o adapter a
traduzir, e uma tradução é o lugar onde os dois lados divergem depois.

Ver `docs/PROTOCOLO_SERIAL.md` §3.
"""
from __future__ import annotations

from .placa_base import PlacaFalsa, agora


class PlacaDispenser(PlacaFalsa):
    SUBSISTEMA = "dispenser"

    # cmd -> campos obrigatórios. `injetar_falha` é opcional de propósito: no
    # caminho normal ele nem aparece no corpo, e a placa não pode exigi-lo.
    COMANDOS = {
        "carregar":  ("dispenser_id", "medicamento", "sku", "categoria",
                      "quantidade", "os_id"),
        "dispensar": ("dispenser_id", "os_id"),
        "limpar":    ("dispenser_id", "solicitado_por"),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.estoque: dict[int, dict] = {}

    def eventos_para(self, cmd: str, campos: dict) -> list[dict]:
        slot = campos.get("dispenser_id")

        if cmd == "carregar":
            self.estoque[slot] = {
                "medicamento": campos["medicamento"],
                "sku": campos["sku"],
                "quantidade": campos["quantidade"],
            }
            return [{
                "tipo": "carregado",
                "dispenser_id": slot,
                "os_id": campos["os_id"],
                "medicamento": campos["medicamento"],
                "sku": campos["sku"],
                "categoria": campos["categoria"],
                "quantidade_total": campos["quantidade"],
                "quantidade_residual": 0,
                "via_residual": False,
                "ts": agora(),
            }]

        if cmd == "dispensar":
            carga = self.estoque.pop(slot, {"medicamento": None, "quantidade": 0})
            alvo = carga["quantidade"]
            # `injetar_falha` atravessou o adapter sem interpretação e chega
            # aqui; a placa a trata num ramo isolado, ANTES de qualquer sorteio.
            # Falha mecânica solta UMA unidade a menos — o número sai do
            # CLAUDE.md, não de gosto: 1/15 já passa da tolerância de 5%.
            injetada = campos.get("injetar_falha") == "falha_mecanica"
            solto = max(0, alvo - 1) if injetada else alvo
            return [{
                "tipo": "dispensado",
                "dispenser_id": slot,
                "os_id": campos["os_id"],
                "medicamento": carga["medicamento"],
                "quantidade_dispensada": solto,
                "quantidade_alvo": alvo,
                "falha_mecanica": solto < alvo,
                "motivo_falha": None if solto >= alvo else "falha_mecanica",
                "quantidade_residual": 0,
                "falha_injetada": injetada,
                "ts": agora(),
            }]

        if cmd == "limpar":
            anterior = self.estoque.pop(slot, {}).get("medicamento")
            # `limpeza_ok` NÃO carrega `os_id`: limpeza é operação de slot, e a
            # chave de espera do orquestrador é `limpeza:{dispenser_id}`.
            return [{
                "tipo": "limpeza_ok",
                "dispenser_id": slot,
                "medicamento_limpo": anterior,
                "solicitado_por": campos["solicitado_por"],
                "ts": agora(),
            }]

        return []

    # ── Telemetria periódica ─────────────────────────────────────────────────

    def telemetria(self, slot: int, valor_c: float = 27.4) -> dict:
        """Uma leitura de temperatura de um slot, no formato do simulador.

        Duas chamadas com o mesmo valor produzem eventos idênticos a menos do
        `ts` — é assim que se prova que o adapter descarta a repetição em vez de
        encaminhá-la ao central.
        """
        return {
            "tipo": "telemetria",
            "dispenser_id": slot,
            "componente": f"dispenser_{slot}",
            "tipo_leitura": "temperatura",
            "valor_c": valor_c,
            "unidade": "°C",
            "ts": agora(),
        }
