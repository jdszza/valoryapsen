# -*- coding: utf-8 -*-
"""Placa falsa da porta NR-12 — `sub: "porta"`, com USB PRÓPRIA.

A porta é uma placa dedicada, como a balança e a CNC: fala o contrato pela USB
dela, com o `porta-adapter` do outro lado (`docs/PROTOCOLO_SERIAL.md` §9). Por
isso esta classe é uma `PlacaFalsa` de verdade, com socket próprio
(`socket://`), e não mais um modelo pendurado na placa das telas — o desenho
em que a porta viajava por UART dentro da porta das telas saiu.

Ela é o documento executável contra o qual o firmware da porta
(`porta/porta_nr12/porta_nr12.ino`) foi escrito, o mesmo papel das outras
placas falsas. Os nomes de evento, os campos e o vocabulário de estado são os
do §9, e `tests/test_protocolo_porta.py` os confronta com o documento.

Encenação (o que a bancada faz com as mãos). Cada uma devolve os eventos que
produziu E os põe no fio, se houver alguém conectado:

    porta.passar_tag("04A1B2C3")   -> pedido_acesso / rearme / tag_negada
    porta.abrir("frente")          -> porta_aberta
    porta.fechar("frente")         -> porta_fechada (quando as duas fecham)
    porta.expirar()                -> o relógio da porta venceu (janela ou
                                      espera pelo central)
    porta.silenciar() / falar()    -> a placa trava e para de falar (sem
                                      heartbeat, sem ACK, sem ping) / volta;
                                      a USB continua de pé
    porta.derrubar_cabo()          -> a USB cai (herdado da PlacaFalsa)

O heartbeat — um `estado` a cada `heartbeat_s` — só sai com alguém conectado:
abrir a porta não reinicia a placa (DTR/RTS desligados), então ela não faz
boot de novo a cada conexão; o primeiro `estado` depois da volta é o heartbeat
seguinte, ou a resposta ao `porta_estado` que o adapter pede ao reabrir.
"""
from __future__ import annotations

from .placa_base import PlacaFalsa, agora

ESTADOS = ("FECHADA", "AGUARDANDO_CENTRAL", "LIBERADA",
           "ABERTA_AUTORIZADA", "ABERTA_SEM_AUTORIZACAO")
JANELA_LIBERACAO_MS = 10000
JANELA_MIN_MS = 1000
JANELA_MAX_MS = 60000


class PlacaPorta(PlacaFalsa):
    SUBSISTEMA = "porta"

    # cmd -> campos que o comando leva. Nenhum é obrigatório NA PLACA: o
    # firmware usa a janela padrão quando `janela_ms` não vem, e o motivo
    # padrão quando `motivo` não vem — por isso `executar` não recusa campo
    # ausente, ao contrário da `PlacaFalsa` genérica.
    COMANDOS = {
        "porta_liberar": ("janela_ms",),
        "porta_negar":   ("motivo",),
        "porta_estado":  (),
    }

    def __init__(self, tags=("04A1B2C3",), teto_fechada=True, frente_fechada=True,
                 heartbeat_s: float = 0.5, **kwargs):
        super().__init__(**kwargs)
        self.tags = set(tags)
        self.teto_fechada = teto_fechada
        self.frente_fechada = frente_fechada
        self.estado_atual = "FECHADA" if (teto_fechada and frente_fechada) \
            else "ABERTA_SEM_AUTORIZACAO"
        self.aguardando_rearme = False
        self.janela_ms = JANELA_LIBERACAO_MS
        self.heartbeat_s = float(heartbeat_s)
        # `False` encena a placa travada: nada sai pelo fio, nem ACK.
        self.viva = True

    # ── Ciclo de vida ───────────────────────────────────────────────────────

    def iniciar(self) -> "PlacaPorta":
        super().iniciar()
        self._subir(self._bater, "placa-porta-heartbeat")
        return self

    def _bater(self) -> None:
        while not self._parar.wait(self.heartbeat_s):
            if self.conectada:
                self.emitir(self.estado())

    def _enviar(self, objeto: dict) -> None:
        # Placa travada não põe NADA no fio — nem o ping, nem o ACK. É o que
        # separa "a placa parou de falar" (a vigia do heartbeat pega) de "a USB
        # caiu" (`derrubar_cabo`).
        if not self.viva:
            return
        super()._enviar(objeto)

    def _no_fio(self, eventos: list[dict]) -> list[dict]:
        for evento in eventos:
            self.emitir(evento)
        return eventos

    # ── Eventos (todos com `origem: porta`) ─────────────────────────────────

    def _evento(self, tipo: str, **campos) -> dict:
        return {"origem": "porta", "tipo": tipo, **campos, "ts": agora()}

    def estado(self) -> dict:
        return self._evento(
            "estado",
            teto="fechada" if self.teto_fechada else "aberta",
            frente="fechada" if self.frente_fechada else "aberta",
            estado=self.estado_atual,
            aguardando_rearme=self.aguardando_rearme,
        )

    def boot(self) -> dict:
        return self._evento("boot",
                            teto="fechada" if self.teto_fechada else "aberta",
                            frente="fechada" if self.frente_fechada else "aberta")

    @property
    def fechada(self) -> bool:
        return self.teto_fechada and self.frente_fechada

    def _mudar(self, novo: str) -> None:
        assert novo in ESTADOS, novo
        self.estado_atual = novo

    @property
    def led_pode_abrir(self) -> bool:
        """O LED "pode abrir" segue o estado: aceso só em LIBERADA."""
        return self.estado_atual == "LIBERADA"

    # ── O que a bancada faz ─────────────────────────────────────────────────

    def passar_tag(self, uid: str) -> list[dict]:
        if uid not in self.tags:
            return self._no_fio([self._evento("tag_negada", uid=uid)])
        if self.estado_atual != "FECHADA":
            return []
        if self.aguardando_rearme:
            self.aguardando_rearme = False
            return self._no_fio([self._evento("rearme", uid=uid), self.estado()])
        self._mudar("AGUARDANDO_CENTRAL")
        return self._no_fio([self._evento("pedido_acesso", uid=uid), self.estado()])

    def abrir(self, porta: str) -> list[dict]:
        assert porta in ("teto", "frente"), porta
        if porta == "teto":
            if not self.teto_fechada:
                return []
            self.teto_fechada = False
        else:
            if not self.frente_fechada:
                return []
            self.frente_fechada = False
        autorizada = self.estado_atual in ("LIBERADA", "ABERTA_AUTORIZADA")
        self._mudar("ABERTA_AUTORIZADA" if autorizada else "ABERTA_SEM_AUTORIZACAO")
        return self._no_fio([
            self._evento("porta_aberta", porta=porta, autorizada=autorizada),
            self.estado()])

    def fechar(self, porta: str) -> list[dict]:
        if porta == "teto":
            self.teto_fechada = True
        else:
            self.frente_fechada = True
        if self.fechada and self.estado_atual in ("ABERTA_AUTORIZADA",
                                                  "ABERTA_SEM_AUTORIZACAO"):
            # NR-12: fechar NÃO religa sozinho.
            self.aguardando_rearme = True
            self._mudar("FECHADA")
            return self._no_fio([self._evento("porta_fechada"), self.estado()])
        return self._no_fio([self.estado()])

    def expirar(self) -> list[dict]:
        """O relógio da porta venceu: a janela, ou a espera pelo central."""
        if self.estado_atual == "AGUARDANDO_CENTRAL":
            self._mudar("FECHADA")
            return self._no_fio([
                self.estado(),
                self._evento("liberacao_expirada", motivo="central_nao_respondeu")])
        if self.estado_atual == "LIBERADA" and self.fechada:
            self.aguardando_rearme = True
            self._mudar("FECHADA")
            return self._no_fio([
                self.estado(),
                self._evento("liberacao_expirada", motivo="janela_expirou")])
        return []

    def silenciar(self) -> None:
        self.viva = False

    def falar(self) -> None:
        self.viva = True

    # ── Comandos do central (ACK idempotente, como toda placa) ─────────────

    def _executar(self, cmd: str, mensagem: dict) -> None:
        """O caminho do fio: ACK primeiro, eventos depois — como a base."""
        if self.mudo:
            return
        ack, eventos = self.executar(mensagem)
        self._enviar(ack)
        if not eventos:
            return
        if self.atraso_evento <= 0:
            self._no_fio(eventos)
            return
        self._subir(lambda: self._emitir_depois(eventos),
                    f"placa-porta-evt-{mensagem.get('cmd_id')}")

    def executar(self, mensagem: dict) -> tuple[dict, list[dict]]:
        """(ACK, eventos) para uma linha de comando. Não põe nada no fio.

        Separado de `_executar` para que a máquina de estados seja testável
        sem socket (`tests/test_protocolo_porta.py`, seção 2).
        """
        cmd = mensagem.get("cmd")
        cmd_id = mensagem.get("cmd_id")
        # A sessão ANTES da idempotência — a ordem do header do firmware.
        self._adotar_sessao(mensagem)
        if cmd not in self.COMANDOS:
            return {"resp": "erro", "cmd_id": cmd_id, "msg": "comando desconhecido"}, []
        if isinstance(cmd_id, int) and cmd_id <= self._ultimo_cmd_id:
            return {"resp": "ok", "cmd_id": cmd_id, "repetido": True}, []
        if self.recusar:
            return {"resp": "erro", "cmd_id": cmd_id,
                    "msg": "recusado pela placa (encenação)"}, []
        if isinstance(cmd_id, int):
            self._ultimo_cmd_id = cmd_id
        campos = {k: v for k, v in mensagem.items()
                  if k not in ("cmd", "cmd_id", "sessao")}
        self.executados.append({"cmd": cmd, "cmd_id": cmd_id, **campos})
        ok = {"resp": "ok", "cmd_id": cmd_id}

        if cmd == "porta_estado":
            return ok, [self.estado()]

        if cmd == "porta_liberar":
            if self.estado_atual != "AGUARDANDO_CENTRAL":
                return {"resp": "erro", "cmd_id": cmd_id, "msg": "sem_pedido_pendente"}, []
            janela = campos.get("janela_ms") or JANELA_LIBERACAO_MS
            self.janela_ms = max(JANELA_MIN_MS, min(JANELA_MAX_MS, int(janela)))
            self._mudar("LIBERADA")
            return ok, [self._evento("liberada", janela_ms=self.janela_ms), self.estado()]

        # porta_negar
        if self.estado_atual not in ("AGUARDANDO_CENTRAL", "LIBERADA"):
            return {"resp": "erro", "cmd_id": cmd_id, "msg": "sem_pedido_pendente"}, []
        self._mudar("FECHADA")
        return ok, [self.estado(),
                    self._evento("liberacao_expirada",
                                 motivo=campos.get("motivo") or "negado_pelo_central")]

    def eventos_para(self, cmd: str, campos: dict) -> list[dict]:
        # Nunca chamado: `_executar` desta placa não passa pela base. Existe
        # porque a base o declara abstrato.
        return []
