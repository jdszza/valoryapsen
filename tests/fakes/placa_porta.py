# -*- coding: utf-8 -*-
"""Placa falsa da porta NR-12 — `sub: "porta"`, atrás da placa das telas.

A porta não tem porta no mini PC: ela fala por UART com a placa das 8 telas,
que repassa as linhas pela USB dela (`docs/PROTOCOLO_SERIAL.md` §9). Por isso
esta classe NÃO é uma `PlacaFalsa` com socket próprio: ela é o MODELO da
máquina de estados do firmware (`porta/porta_nr12/porta_nr12.ino`), e quem a
coloca no fio é a `PlacaDispenserTFT`, exatamente como a placa real.

Ela é o documento executável contra o qual o firmware da porta foi escrito,
o mesmo papel das outras placas falsas. Os nomes de evento, os campos e o
vocabulário de estado são os do §9, e `tests/test_protocolo_porta.py` os
confronta com o documento.

Encenação (o que a bancada faz com as mãos):

    porta.passar_tag("04A1B2C3")   -> pedido_acesso / rearme / tag_negada
    porta.abrir("frente")          -> porta_aberta
    porta.fechar("frente")         -> porta_fechada (quando as duas fecham)
    porta.expirar()                -> o relógio da porta venceu (janela ou
                                      espera pelo central)
    porta.silenciar() / falar()    -> o heartbeat some / volta (cabo, placa)
"""
from __future__ import annotations

from .placa_base import agora

ESTADOS = ("FECHADA", "AGUARDANDO_CENTRAL", "LIBERADA",
           "ABERTA_AUTORIZADA", "ABERTA_SEM_AUTORIZACAO")
JANELA_LIBERACAO_MS = 10000
JANELA_MIN_MS = 1000
JANELA_MAX_MS = 60000


class PlacaPorta:
    SUBSISTEMA = "porta"

    # cmd -> campos obrigatórios. `porta_estado` não leva nenhum.
    COMANDOS = {
        "porta_liberar": ("janela_ms",),
        "porta_negar":   ("motivo",),
        "porta_estado":  (),
    }

    def __init__(self, tags=("04A1B2C3",), teto_fechada=True, frente_fechada=True):
        self.tags = set(tags)
        self.teto_fechada = teto_fechada
        self.frente_fechada = frente_fechada
        self.estado_atual = "FECHADA" if (teto_fechada and frente_fechada) \
            else "ABERTA_SEM_AUTORIZACAO"
        self.aguardando_rearme = False
        self.solenoide = False
        self.janela_ms = JANELA_LIBERACAO_MS
        # O heartbeat: `False` encena cabo solto ou placa travada.
        self.viva = True
        self.executados: list[dict] = []
        self._ultimo_cmd_id = 0
        self.sessao_adapter = None

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
            solenoide=self.solenoide,
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
        if novo != "LIBERADA":
            self.solenoide = False      # garantirSolenoide()

    # ── O que a bancada faz ─────────────────────────────────────────────────

    def passar_tag(self, uid: str) -> list[dict]:
        if uid not in self.tags:
            return [self._evento("tag_negada", uid=uid)]
        if self.estado_atual != "FECHADA":
            return []
        if self.aguardando_rearme:
            self.aguardando_rearme = False
            return [self._evento("rearme", uid=uid), self.estado()]
        self._mudar("AGUARDANDO_CENTRAL")
        return [self._evento("pedido_acesso", uid=uid), self.estado()]

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
        return [self._evento("porta_aberta", porta=porta, autorizada=autorizada),
                self.estado()]

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
            return [self._evento("porta_fechada"), self.estado()]
        return [self.estado()]

    def expirar(self) -> list[dict]:
        """O relógio da porta venceu: a janela, ou a espera pelo central."""
        if self.estado_atual == "AGUARDANDO_CENTRAL":
            self._mudar("FECHADA")
            return [self.estado(),
                    self._evento("liberacao_expirada", motivo="central_nao_respondeu")]
        if self.estado_atual == "LIBERADA" and self.fechada:
            self.aguardando_rearme = True
            self._mudar("FECHADA")
            return [self.estado(),
                    self._evento("liberacao_expirada", motivo="janela_expirou")]
        return []

    def silenciar(self) -> None:
        self.viva = False

    def falar(self) -> None:
        self.viva = True

    # ── Comandos do central (ACK idempotente, como toda placa) ─────────────

    def executar(self, mensagem: dict) -> tuple[dict, list[dict]]:
        """(ACK, eventos) para uma linha de comando que desceu pela UART."""
        cmd = mensagem.get("cmd")
        cmd_id = mensagem.get("cmd_id")
        sessao = mensagem.get("sessao")
        if isinstance(sessao, int) and sessao > 0:
            if self.sessao_adapter is not None and sessao != self.sessao_adapter:
                self._ultimo_cmd_id = 0
            self.sessao_adapter = sessao
        if cmd not in self.COMANDOS:
            return {"resp": "erro", "cmd_id": cmd_id, "msg": "comando desconhecido"}, []
        if isinstance(cmd_id, int) and cmd_id <= self._ultimo_cmd_id:
            return {"resp": "ok", "cmd_id": cmd_id, "repetido": True}, []
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
            self.solenoide = True
            return ok, [self._evento("liberada", janela_ms=self.janela_ms), self.estado()]

        # porta_negar
        if self.estado_atual not in ("AGUARDANDO_CENTRAL", "LIBERADA"):
            return {"resp": "erro", "cmd_id": cmd_id, "msg": "sem_pedido_pendente"}, []
        self._mudar("FECHADA")
        return ok, [self.estado(),
                    self._evento("liberacao_expirada",
                                 motivo=campos.get("motivo") or "negado_pelo_central")]
