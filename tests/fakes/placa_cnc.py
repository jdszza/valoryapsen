# -*- coding: utf-8 -*-
"""Placa falsa da mesa CNC — `sub: "cnc"`.

Ela é endereçada pelo DISPENSER, não por coordenada, e REPORTA onde parou.

O roteiro da mesa é medido na bancada e gravado na placa, waypoint a waypoint,
indexado pelo dispenser; quem comanda diz apenas a QUAL slot ir. É o contrário
do que esta placa falsa encenava antes, e a inversão tem motivo: um par de
coordenadas vindo de fora seria um segundo mapa da célula, e os dois
concordariam só enquanto ninguém mexesse na mesa. O primeiro waypoint regravado
na bancada mandaria a mesa para onde o CENTRAL acha que D7 fica — divergência
num slot só, que é o quadro de uma falha mecânica.

Por isso `WAYPOINTS` abaixo é deliberadamente DIFERENTE do `POSICOES` do
orquestrador. Se fosse igual, toda asserção sobre a posição passaria por
concordância acidental — o central compararia o seu número com uma cópia do seu
número, e continuaria verde no dia em que ele voltasse a ignorar o que a máquina
informa. Diferente, a volta atrás aparece na primeira leitura.

Ver `docs/PROTOCOLO_SERIAL.md` §4.
"""
from __future__ import annotations

from .placa_base import PlacaFalsa, agora

# A "bancada" desta placa. Números próprios, medidos por ninguém — o que importa
# é que sejam DELA, estáveis entre execuções (um teste não pode depender de
# sorteio) e diferentes dos do central.
_X0_MM    = 42.0
_PASSO_MM = 88.0
_Y_MM     = 131.0
HOME_X_MM = -73.0
HOME_Y_MM = 6.0
# O centro operacional DESTA placa (o firmware o guarda na NVS). Número próprio,
# pelo mesmo motivo dos waypoints.
CENTRO_X_MM = 171.0
CENTRO_Y_MM = 9.0


def _waypoints(num_slots: int = 8) -> dict[int, tuple[float, float]]:
    fileira = max(1, num_slots // 2)
    return {
        slot: (_X0_MM + ((slot - 1) % fileira) * _PASSO_MM,
               (-1.0 if slot <= fileira else 1.0) * _Y_MM)
        for slot in range(1, num_slots + 1)
    }


WAYPOINTS: dict[int, tuple[float, float]] = _waypoints()


class PlacaCNC(PlacaFalsa):
    SUBSISTEMA = "cnc"

    COMANDOS = {
        # `mover` exige o ENDEREÇO (qual slot) e a OS. `receita` é aceita e
        # registrada, nunca exigida: o §4 a documenta como o que diz QUAL ordem
        # está rodando, e a mesa acha o waypoint sem ela.
        #
        # O `homing` aceita as coordenadas do contrato antigo e as IGNORA: o
        # HOME de uma mesa é o zero que o próprio homing estabelece contra os
        # fins de curso. Um par vindo de fora seria um segundo HOME, e os dois
        # concordariam só enquanto ninguém mexesse na máquina.
        #
        # `estado_celula` exige só `trava_ativa`: `trava_slot_id` pode vir nulo
        # (trava sem slot) e os outros dois são cosméticos para a mesa. Exigir
        # o que o documento marca como opcional faria a placa recusar um comando
        # que o adapter tem todo o direito de mandar.
        "mover":         ("dispenser_alvo", "os_id"),
        "homing":        ("os_id",),
        "estado_celula": ("trava_ativa",),
        # A parada da porta NR-12: sem campo obrigatório (`motivo` é log).
        "parar":         (),
        # O fim da OS: a caixa conferida vai ao centro gravado na placa.
        "centro":        ("os_id",),
    }

    # A mesa perdeu a origem numa parada da porta: `mover` vira `sem_homing`
    # até o próximo homing, como no firmware.
    sem_origem = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pos_x = HOME_X_MM
        self.pos_y = HOME_Y_MM
        self.trava_ativa = False
        # A mesa estava andando quando a trava chegou? O firmware guarda isso
        # (`trava_interrompeu_mover`) para decidir QUEM faz o homing, e a placa
        # falsa precisa do mesmo estado para produzir os mesmos eventos.
        self.movimento_em_curso = False
        # O homing falha (fim de curso que não dispara). Sem isto não dá para
        # encenar o caminho em que `concluido` NÃO pode sair.
        self.homing_falha = False

        # ── A mesa LENTA e a mesa MUDA ───────────────────────────────────────
        # Sem elas nenhum teste do ciclo por relógio consegue encenar o caso que
        # DEFINE o modelo — a placa que não confirma dentro do prazo.
        #
        # LENTA já existe na base: `atraso_evento` maior que
        # `CNC_TETO_TRAJETO_S + CNC_MARGEM_CHEGADA_S` faz o `posicionado` chegar
        # DEPOIS de o `dispensar` já ter saído. Um segundo botão só para a mesa
        # seria um número a mais dizendo o que este já diz.
        #
        # MUDA é o caso extremo, e o mais importante dos dois: nem chegada, nem
        # erro. Um `posicionado` perdido no encaminhamento (o `_post_central` do
        # adapter desiste depois de 3 tentativas) chega ao central idêntico à
        # mesa parada, e é justamente essa indistinguibilidade que o modelo por
        # relógio trata como normal em vez de como falha.
        self.mover_mudo = False
        # `codigo_erro` que o próximo `centro` emite no lugar da chegada (None =
        # chega). E quantas vezes a mesa CHEGOU ao centro — é contra este número
        # que se prova que um `cmd_id` repetido não move duas vezes.
        self.centro_erro: str | None = None
        self.no_centro = 0

    def _erro(self, campos: dict, codigo: str, descricao: str) -> dict:
        return {
            "tipo": "erro",
            "os_id": campos.get("os_id", ""),
            "dispenser_alvo": campos.get("dispenser_alvo", 0),
            "codigo_erro": codigo,
            "descricao": descricao,
            "ts": agora(),
        }

    def _homing(self, os_id: str) -> list[dict]:
        """O retorno ao HOME e o evento que ele produz — ou o que ele NÃO produz.

        `concluido` depois de um homing que falhou afirmaria que a mesa está no
        HOME, e ela está onde o eixo travou. O §4 do protocolo escreve a regra,
        e o firmware a viola​va nos dois caminhos de trava.
        """
        if self.homing_falha:
            # Como o firmware: sem o fim de curso, a origem não existe.
            self.sem_origem = True
            return [self._erro({"os_id": os_id}, "homing_falhou",
                               "um dos eixos nao achou o fim de curso no prazo")]
        self.sem_origem = False
        self.pos_x = HOME_X_MM
        self.pos_y = HOME_Y_MM
        return [{
            "tipo": "concluido",
            "os_id": os_id,
            "posicao_x": self.pos_x,
            "posicao_y": self.pos_y,
            "ts": agora(),
        }]

    def eventos_para(self, cmd: str, campos: dict) -> list[dict]:
        if cmd == "estado_celula":
            # **Na mesa, `estado_celula` NÃO é só pintura.** O comentário que
            # estava aqui foi copiado da placa das telas, onde ele é verdade —
            # e a diferença é justamente o que deixava passar os dois bugs que
            # o firmware tinha neste caminho.
            #
            # O que o firmware faz na TRANSIÇÃO para travada:
            #   * com um `mover` em curso, quem emite é o `cmdMover`
            #     interrompido — `erro` com `travado`, e depois o homing;
            #   * com a mesa PARADA, ela vai ao HOME por conta própria, que é
            #     onde o supervisor espera encontrá-la.
            #
            # A liberação não faz homing nenhum: a mesa já está no HOME desde a
            # ativação, e um segundo homing custaria segundos no exato momento
            # em que o supervisor acabou de liberar a produção.
            anterior = self.trava_ativa
            self.trava_ativa = bool(campos.get("trava_ativa"))
            if self.trava_ativa == anterior:
                return []           # idempotente: a segunda vez não refaz nada
            if not self.trava_ativa:
                return []

            os_id = campos.get("os_id", "") or ""
            eventos: list[dict] = []
            if self.movimento_em_curso:
                self.movimento_em_curso = False
                eventos.append(self._erro(
                    {"os_id": os_id,
                     "dispenser_alvo": campos.get("trava_slot_id", 0) or 0},
                    "travado",
                    "movimento interrompido pela trava do Triple Check"))
            return eventos + self._homing(os_id)

        if cmd == "mover":
            alvo = campos["dispenser_alvo"]
            # A trava vem antes de tudo: com ela ativa a mesa não vai a lugar
            # nenhum, e o motivo é a trava — não a faixa.
            if self.trava_ativa:
                return [self._erro(campos, "travado",
                                   "trava do Triple Check ativa: aguardando liberacao")]
            # Fora da faixa, a mesa não se move: o waypoint não existe. A placa
            # de verdade recusa no ACK; aqui o que importa é não INVENTAR uma
            # posição para um slot que a bancada não tem.
            if self.sem_origem:
                return [self._erro(campos, "sem_homing",
                                   "origem perdida na parada: mande homing antes")]
            if alvo not in WAYPOINTS:
                return [self._erro(campos, "dispenser_invalido",
                                   f"dispenser {alvo} sem waypoint gravado")]
            if self.mover_mudo:
                # Nem chegada, nem erro. É o silêncio que o modelo por relógio
                # trata como normal — e o único jeito de encená-lo.
                return []
            self.pos_x, self.pos_y = WAYPOINTS[alvo]
            return [{
                "tipo": "posicionado",
                "os_id": campos["os_id"],
                "dispenser_alvo": alvo,
                "destino": "dispenser",
                # A MEDIDA, não o alvo. É o único número desta linha que o
                # comando não trouxe — e é o que o central registra.
                "posicao_x": self.pos_x,
                "posicao_y": self.pos_y,
                "ciclo_atual": campos.get("ciclo_atual", 0),
                "total_ciclos": campos.get("total_ciclos", 0),
                "ts": agora(),
            }]

        if cmd == "homing":
            return self._homing(campos["os_id"])

        if cmd == "centro":
            # As regras do `mover`, com o destino gravado na placa. A placa de
            # verdade recusa no ACK; aqui, como no `mover`, o que importa é o
            # evento — e que um centro sem origem NÃO invente posição.
            os_id = campos["os_id"]
            alvo = {"os_id": os_id, "dispenser_alvo": 0}
            if self.trava_ativa:
                return [self._erro(alvo, "travado",
                                   "trava do Triple Check ativa: aguardando liberacao")]
            if self.sem_origem:
                return [self._erro(alvo, "sem_homing",
                                   "origem nunca estabelecida: mande homing antes do centro")]
            if self.centro_erro:
                # Encenação: o fim de curso disparou no caminho.
                return [self._erro(alvo, self.centro_erro,
                                   "fim de curso disparou durante a ida ao centro")]
            if self.mover_mudo:
                return []
            self.pos_x, self.pos_y = CENTRO_X_MM, CENTRO_Y_MM
            self.no_centro += 1
            return [{
                "tipo": "posicionado",
                "os_id": os_id,
                "dispenser_alvo": 0,
                "destino": "centro",
                "posicao_x": self.pos_x,
                "posicao_y": self.pos_y,
                "ciclo_atual": 0,
                "total_ciclos": 0,
                "ts": agora(),
            }]

        if cmd == "parar":
            # Como o firmware: com movimento em curso, a mesa para ONDE ESTÁ e
            # perde a origem (todo `mover` seguinte seria `sem_homing`); parada,
            # é só o ACK. Nenhum evento — quem parou foi o central.
            if self.movimento_em_curso:
                self.movimento_em_curso = False
                self.sem_origem = True
            return []

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
