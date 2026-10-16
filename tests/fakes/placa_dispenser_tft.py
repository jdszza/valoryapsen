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

    # ── A porta NR-12, atrás desta placa (§9) ────────────────────────────────
    #
    # A placa real repassa a porta pela UART sem interpretar: evento e ACK
    # sobem como vieram, `porta_*` desce como veio e o ACK é da PORTA, e o
    # heartbeat é vigiado aqui. Esta é a mesma regra, encenada com o modelo
    # `PlacaPorta` no lugar do fio.

    placa_porta = None
    _porta_perdida = False
    _porta_ultimo_estado = 0.0

    def conectar_porta(self, porta, heartbeat_s: float = 0.1,
                       vigia_s: float = 0.3) -> "PlacaDispenserTFT":
        """Liga a porta falsa a esta placa e sobe o heartbeat + a vigia."""
        import time as _time
        # `placa_porta`, e não `porta`: na PlacaFalsa `porta` é a porta TCP.
        self.placa_porta = porta
        self.vigia_s = float(vigia_s)
        self._porta_ultimo_estado = _time.monotonic()
        self._porta_perdida = False

        def _laco():
            while not self._parar.wait(heartbeat_s):
                if self.placa_porta.viva:
                    self.da_porta([self.placa_porta.estado()])
                elif (not self._porta_perdida
                      and _time.monotonic() - self._porta_ultimo_estado > self.vigia_s):
                    self._porta_perdida = True
                    self.emitir({"origem": "porta", "tipo": "porta_link_perdido",
                                 "ts": agora()})

        self._subir(_laco, "placa-porta-heartbeat")
        return self

    def da_porta(self, eventos: list) -> None:
        """Eventos que a porta emitiu: sobem como vieram, e o `estado` alimenta
        a vigia. Porta muda (`viva=False`) não põe nada no fio."""
        import time as _time
        if self.placa_porta is None or not self.placa_porta.viva:
            return
        for evento in eventos:
            if evento.get("tipo") == "estado":
                self._porta_ultimo_estado = _time.monotonic()
                if self._porta_perdida:
                    self._porta_perdida = False
                    # A volta sai ANTES do `estado` que a provou.
                    self.emitir({"origem": "porta", "tipo": "porta_link_ok",
                                 "ts": agora()})
            self.emitir(evento)

    def _executar(self, cmd: str, mensagem: dict) -> None:
        if not cmd.startswith("porta_"):
            super()._executar(cmd, mensagem)
            return
        if self.mudo:
            return
        # A idempotência desta placa vale também para o que ela só repassa —
        # é o header dela que a aplica, antes de olhar o prefixo.
        self._adotar_sessao(mensagem)
        cmd_id = mensagem.get("cmd_id")
        if isinstance(cmd_id, int) and cmd_id <= self._ultimo_cmd_id:
            self._enviar({"resp": "ok", "cmd_id": cmd_id, "repetido": True})
            return
        if isinstance(cmd_id, int):
            self._ultimo_cmd_id = cmd_id
        if self.placa_porta is None or self._porta_perdida or not self.placa_porta.viva:
            # Recusa NA HORA: esperar o ack_timeout do adapter para descobrir
            # o que esta placa já sabe só gastaria o relógio do central.
            self._enviar({"resp": "erro", "cmd_id": cmd_id, "msg": "porta_sem_link"})
            return
        ack, eventos = self.placa_porta.executar(mensagem)
        self._enviar(ack)                 # o ACK é da PORTA
        self.da_porta(eventos)
