"""Envio do evento para o vision-adapter, com retentativa.

Regra que vale mais que o resto deste arquivo: **o envio nunca pode derrubar a
estacao nem atrasar a proxima captura**. Se o adapter estiver fora do ar, a
celula continua operando e o central trata a ausencia do evento como timeout
daquela camera — ruim, mas previsto. Uma excecao escapando daqui levaria junto
a thread de captura e a estacao pararia de responder a TODAS as capturas
seguintes, o que e muito pior.
"""

from __future__ import annotations

import json
import logging
import time

import requests

registro = logging.getLogger("integracao.cliente")

TENTATIVAS = 3
ESPERA_ENTRE_S = 1.0
TIMEOUT_S = 5.0


class ClienteAdapter:
    def __init__(self, url_eventos: str, tentativas: int = TENTATIVAS,
                 espera_s: float = ESPERA_ENTRE_S, timeout_s: float = TIMEOUT_S):
        self.url_eventos = url_eventos
        self.tentativas = tentativas
        self.espera_s = espera_s
        self.timeout_s = timeout_s
        # Sessao reaproveita a conexao TCP. Numa OS de oito slots sao oito
        # eventos em poucos segundos; sem isso e um handshake novo em cada um.
        self.sessao = requests.Session()
        self.enviados = 0
        self.falhados = 0

    def enviar(self, evento: dict) -> bool:
        """Tenta ate `tentativas` vezes. Devolve True se o adapter aceitou.

        Nunca levanta excecao.
        """
        rotulo = (f"{evento.get('tipo')} os={evento.get('os_id')} "
                  f"slot={evento.get('slot_id')}")
        for tentativa in range(1, self.tentativas + 1):
            try:
                resposta = self.sessao.post(
                    self.url_eventos, json=evento, timeout=self.timeout_s,
                    headers={"Content-Type": "application/json"},
                )
            except requests.RequestException as erro:
                registro.warning("envio %s falhou (%d/%d): %s",
                                 rotulo, tentativa, self.tentativas, erro)
            else:
                if 200 <= resposta.status_code < 300:
                    # O adapter pode responder encaminhado: false quando ELE nao
                    # conseguiu falar com o central. Mesmo assim nao se reenvia:
                    # ele ja retentou 3x do lado dele, e um reenvio nosso viraria
                    # evento duplicado se a primeira tiver chegado.
                    self.enviados += 1
                    corpo = _corpo_curto(resposta)
                    if '"encaminhado": false' in corpo or "'encaminhado': False" in corpo:
                        registro.warning("adapter aceitou mas NAO encaminhou %s: %s",
                                         rotulo, corpo)
                    else:
                        registro.info("evento entregue: %s", rotulo)
                    return True
                if 400 <= resposta.status_code < 500:
                    # 4xx e payload invalido: retentar so repete o erro. Isto e
                    # bug de formato nosso, entao o corpo da resposta vai para o
                    # log inteiro — e a unica pista de qual campo o central
                    # recusou.
                    self.falhados += 1
                    registro.error("adapter recusou %s com HTTP %d: %s | payload=%s",
                                   rotulo, resposta.status_code, _corpo_curto(resposta),
                                   json.dumps(evento, ensure_ascii=False))
                    return False
                registro.warning("adapter respondeu HTTP %d em %s (%d/%d)",
                                 resposta.status_code, rotulo, tentativa, self.tentativas)
            if tentativa < self.tentativas:
                time.sleep(self.espera_s)

        self.falhados += 1
        registro.error("evento NAO entregue depois de %d tentativas: %s | payload=%s",
                       self.tentativas, rotulo, json.dumps(evento, ensure_ascii=False))
        return False


def _corpo_curto(resposta, limite: int = 300) -> str:
    try:
        texto = resposta.text or ""
    except Exception:
        return "<corpo ilegivel>"
    return texto[:limite]
