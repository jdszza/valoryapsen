# -*- coding: utf-8 -*-
"""O switch da visão no Triple Check — estado de runtime, sem FastAPI e sem banco.

Desligado, a visão computacional (câmera da mesa, câmeras dos dispensers e as
duas fotos da caixa no HOME) continua medindo e sendo registrada, mas o que ela
disser não trava a OS: vira o alarme `visao_ignorada`. Balança, dispenser e
segurança continuam decidindo como sempre. Ver CLAUDE.md, "Switch da visão no
Triple Check".

O orquestrador lê `chave.ativo` NO MOMENTO de cada veredito — nunca uma cópia
tirada no começo da OS —, e é isso que faz desligar no meio de uma OS valer já
no slot seguinte.

**Não é persistido, e isso é escolha** (a mesma da pausa do gerador): cada boot
nasce em `VISAO_TRIPLE_CHECK_PADRAO`, que é 1. Um "desligado" gravado em banco
sobreviveria à emergência que o motivou, e o sintoma — OS seguindo com a câmera
acusando, alarme atrás de alarme — é exatamente o que ninguém procura depois.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from config import settings

# Quem mudou o switch pela última vez quando ninguém mudou: o boot.
POR_BOOT = "boot"


def _agora() -> datetime:
    return datetime.now(timezone.utc)


class ChaveVisaoTC:
    """`ativo` = a visão conta no Triple Check. `desde`/`por`: a última mudança.

    Thread-safe: as rotas do console rodam em threads do FastAPI e o
    orquestrador lê do event loop. `ligar`/`desligar` devolvem True só quando o
    valor MUDOU — é por esse retorno que a rota decide se grava histórico
    (POST repetido é idempotente).
    """

    def __init__(self, ativo: bool = True, desde: Optional[datetime] = None,
                 por: str = POR_BOOT):
        self._lock = threading.Lock()
        self.ativo = bool(ativo)
        self.desde = desde or _agora()
        self.por = por

    def _definir(self, ativo: bool, por: str, agora: Optional[datetime]) -> bool:
        with self._lock:
            if self.ativo == ativo:
                return False
            self.ativo = ativo
            self.desde = agora or _agora()
            self.por = por
            return True

    def ligar(self, por: str, agora: Optional[datetime] = None) -> bool:
        return self._definir(True, por, agora)

    def desligar(self, por: str, agora: Optional[datetime] = None) -> bool:
        return self._definir(False, por, agora)

    def definir(self, ativo: bool, por: str, agora: Optional[datetime] = None) -> bool:
        return self.ligar(por, agora) if ativo else self.desligar(por, agora)

    def estado(self) -> dict:
        with self._lock:
            return {"ativo": self.ativo, "desde": self.desde.isoformat(), "por": self.por}


# A instância da planta. Nasce do `.env` a cada import, ou seja, a cada boot do
# central — e a suíte, que reimporta os módulos do central por teste, recebe uma
# nova a cada carga.
chave = ChaveVisaoTC(ativo=settings.VISAO_TRIPLE_CHECK_PADRAO)


def visao_conta() -> bool:
    """O que o Triple Check pergunta a cada veredito."""
    return chave.ativo
