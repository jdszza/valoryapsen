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
        "parar":     (),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.estoque: dict[int, dict] = {}
        # `dispensa_lenta=True` encena a dispensa que DEMORA: o `dispensar` é
        # aceito e nada sai até `avancar(n)` contar unidades e um `parar`
        # chegar (ou `concluir()` encerrar). É o que deixa um teste parar "3 de
        # 5" — com o evento instantâneo de sempre não haveria meio para parar.
        self.dispensa_lenta = False
        self.em_curso: dict | None = None

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

        if cmd == "parar":
            # Sem dispensa em curso: só o ACK. Com: termina a unidade e para.
            if self.em_curso is None:
                return []
            curso, self.em_curso = self.em_curso, None
            self.estoque[curso["slot"]] = {
                "medicamento": curso["medicamento"], "sku": curso["sku"],
                "quantidade": curso["alvo"] - curso["caidas"],
            }
            return [{
                "tipo": "dispensado_parcial",
                "dispenser_id": curso["slot"],
                "os_id": curso["os_id"],
                "medicamento": curso["medicamento"],
                "quantidade_confirmada": curso["caidas"],
                "quantidade_pedida": curso["alvo"],
                "quantidade_residual": curso["alvo"] - curso["caidas"],
                "ts": agora(),
            }]

        if cmd == "dispensar" and self.dispensa_lenta:
            carga = self.estoque.get(slot, {"medicamento": None, "sku": None,
                                            "quantidade": 0})
            alvo = campos.get("quantidade") or carga["quantidade"]
            self.em_curso = {"slot": slot, "os_id": campos["os_id"], "alvo": alvo,
                             "medicamento": carga["medicamento"],
                             "sku": carga.get("sku"), "caidas": 0}
            return []

        if cmd == "dispensar":
            carga = self.estoque.pop(slot, {"medicamento": None, "quantidade": 0})
            alvo = campos.get("quantidade") or carga["quantidade"]
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

    def avancar(self, unidades: int) -> None:
        """Na dispensa lenta, `unidades` caíram e o IR as contou."""
        assert self.em_curso is not None, "nenhuma dispensa em curso"
        self.em_curso["caidas"] = min(self.em_curso["alvo"],
                                      self.em_curso["caidas"] + unidades)

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
