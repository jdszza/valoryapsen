"""
A porta NR-12 em campo fala TEXTO — este módulo a traduz para o vocabulário
de eventos que o central sempre recebeu (`docs/PROTOCOLO_SERIAL.md` §10).

PURO: sem asyncio, sem FastAPI, sem serial, sem httpx, sem relógio próprio.
O tempo entra por parâmetro (`agora`, monotônico; `ts`, o carimbo do evento),
e é isso que deixa a suíte encenar a porta inteira sem dormir.

Duas peças:

  * `interpretar_linha` — uma linha da USB do receptor vira `Linha` (um
    `EVT:<CODIGO>[;UID=<HEX>] | <texto>`, a linha dos fins de curso, ou o aviso
    de "nenhum byte" do receptor) ou None. Só o que vem antes de " | " é
    máquina; o texto é humano e pode mudar sem quebrar nada.
  * `ModeloPorta` — o estado da porta como ESTE lado o conhece. A porta não
    tem heartbeat e não responde a pergunta nenhuma (nada desce a ela), então
    o estado começa DESCONHECIDO e só fica conhecido com uma linha que o
    PROVE. Com ele desconhecido, nada sobe.

Os códigos são declarados em duas tuplas, e `tests/test_porta_texto.py`
confere que TODO `evento("X", ...)` do `porta/nr12_oficial/nr12_oficial.ino`
está em uma delas — nenhum código é ignorado por esquecimento.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# ── O formato da porta (`evento()` e `imprimirSensores()` do nr12_oficial) ────

PREFIXO_EVT = "EVT:"
SEP_UID = ";UID="
SEP_TEXTO = " | "

# A linha dos fins de curso: "   ES1(D4)=FECHADO  ES2(D18)=ABERTO". ES1 é o
# teto (abastecer remédio), ES2 a frente (a caixa sobre a balança).
SENSOR_TETO = "ES1(D4)"
SENSOR_FRENTE = "ES2(D18)"
NIVEL = {"FECHADO": "fechada", "ABERTO": "aberta"}

# O aviso que o RECEPTOR imprime sozinho enquanto nada chegou da porta desde o
# boot dele: o fio TX17→RX16 (ou o GND comum) sem dado nenhum.
AVISO_RECEPTOR_SEM_DADOS = "[aviso] Nenhum byte recebido no Serial2"

# O resto do que o receptor imprime por conta própria (banner, respostas dos
# comandos de bancada dele, linha longa despejada crua). Não sobe nada.
PREFIXOS_DO_RECEPTOR = (
    "=== RECEPTOR",
    "Serial2: RX=",
    "Aguardando mensagens do ESP32 da porta",
    "Comandos: ",
    ">> ",
    "[linha longa sem fim]",
    AVISO_RECEPTOR_SEM_DADOS,
)

# ── Os códigos ────────────────────────────────────────────────────────────────

# Sobem ao central. Todos PROVAM o estado da porta, menos `TAG_NEGADA`.
CODIGOS_QUE_SOBEM = (
    "BOOT_OK", "ALERTA_SEM_AUTORIZACAO", "PAUSA", "ALERTA_PORTA_ABERTA",
    "LIBERACAO_EXPIRADA", "PORTA_FECHADA", "REARME_OK", "TAG_NEGADA",
)
CODIGOS_QUE_NAO_PROVAM = ("TAG_NEGADA",)
# Cadastro de tag, feito na USB DA PORTA com notebook. Ficam no /health.
CODIGOS_DE_BANCADA = (
    "REG_INICIO", "REG_SALVA", "REG_JA_EXISTE", "REG_ERRO", "REG_CANCELADO",
    "REG_FIM",
)

# `JANELA_LIBERACAO_MS` gravado na porta. O adapter não a mede: declara.
JANELA_LIBERACAO_MS = 10000

_RE_EVT = re.compile(r"EVT:([A-Z0-9_]+)(?:;UID=([0-9A-Fa-f]+))?\s*$")
_RE_SENSORES = re.compile(
    re.escape(SENSOR_TETO) + r"=(FECHADO|ABERTO)\s+"
    + re.escape(SENSOR_FRENTE) + r"=(FECHADO|ABERTO)")


@dataclass(frozen=True)
class Linha:
    tipo: str                      # "evt" | "sensores" | "receptor_sem_dados"
    codigo: Optional[str] = None
    uid: Optional[str] = None
    teto: Optional[str] = None
    frente: Optional[str] = None


def interpretar_linha(linha: str) -> Optional[Linha]:
    """Uma linha da USB do receptor → `Linha`, ou None (não é máquina)."""
    texto = (linha or "").replace("\r", "").strip()
    if not texto or texto.startswith("[linha longa sem fim]"):
        # A linha longa é o receptor despejando 400 bytes sem `\n`: bytes se
        # perderam no meio, e um `EVT:` lá dentro não é confiável.
        return None
    if texto.startswith(AVISO_RECEPTOR_SEM_DADOS):
        return Linha("receptor_sem_dados")
    inicio = texto.find(PREFIXO_EVT)
    if inicio >= 0:
        # Lixo imprimível ANTES do `EVT:` (o filtro do receptor só descarta
        # byte não imprimível) é tolerado; o texto humano depois de " | ",
        # ignorado.
        maquina = texto[inicio:].split(SEP_TEXTO, 1)[0].strip()
        m = _RE_EVT.match(maquina)
        if not m:
            return None
        uid = m.group(2).upper() if m.group(2) else None
        return Linha("evt", codigo=m.group(1), uid=uid)
    m = _RE_SENSORES.search(texto)
    if m:
        return Linha("sensores", teto=NIVEL[m.group(1)], frente=NIVEL[m.group(2)])
    return None


def linha_do_receptor(linha: str) -> bool:
    """O receptor falando por conta própria (banner, `>> ...`). Não sobe nada
    e não conta como linha desconhecida."""
    texto = (linha or "").replace("\r", "").strip()
    return not texto or texto.startswith(PREFIXOS_DO_RECEPTOR)


# ── O modelo da porta ─────────────────────────────────────────────────────────

class ModeloPorta:
    """O estado da porta como este lado o conhece — e só o que uma linha provou.

    `aplicar` devolve os eventos a subir, na ORDEM do §10 e já no formato do
    central (`{"origem": "porta", "tipo": ..., ..., "ts": ...}`). O `estado`
    sobe depois de cada transição; o reenvio periódico é de quem chama.
    """

    def __init__(self, janela_ms: int = JANELA_LIBERACAO_MS):
        self.janela_ms = janela_ms
        self.conhecido = False
        self.estado: Optional[str] = None
        self.aguardando_rearme = False
        self.teto: Optional[str] = None
        self.frente: Optional[str] = None
        self.ultimo_uid: Optional[str] = None
        # `porta_aberta` esperando a linha ES que diz QUAL porta abriu.
        self.pendente: Optional[dict] = None
        # Diagnóstico para o /health.
        self.ultimo_codigo: Optional[str] = None
        self.ultimo_codigo_bancada: Optional[str] = None
        self.codigos_desconhecidos = 0
        self.receptor_sem_dados = False

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _ev(tipo: str, ts: Optional[str], **campos) -> dict:
        return {"origem": "porta", "tipo": tipo, **campos, "ts": ts or ""}

    def _fixar(self, estado: str, rearme: bool, fechadas: bool = True) -> None:
        self.conhecido = True
        self.estado = estado
        self.aguardando_rearme = rearme
        if fechadas:
            self.teto = self.frente = "fechada"

    def _abrir(self, estado: str, autorizada: bool, agora: float) -> None:
        # Qual porta abriu vem na linha ES, logo depois. Até lá as duas são
        # desconhecidas: o "fechada" de antes é certamente falso para uma delas.
        self.conhecido = True
        self.estado = estado
        self.teto = self.frente = None
        self.pendente = {"autorizada": autorizada, "desde": agora}

    def _abertas(self, ts: Optional[str], pendente: dict, portas: list) -> list[dict]:
        autorizada = pendente["autorizada"]
        if not portas:
            return [self._ev("porta_aberta", ts, porta=None, autorizada=autorizada)]
        return [self._ev("porta_aberta", ts, porta=p, autorizada=autorizada) for p in portas]

    # ── API ──────────────────────────────────────────────────────────────

    def estado_evento(self, ts: Optional[str] = None) -> Optional[dict]:
        """O `estado` sintetizado, ou None se o estado é desconhecido — ou
        ainda incompleto (um `porta_aberta` esperando a linha ES: um `estado`
        aberto sem dizer qual porta chegaria ao central ANTES do evento)."""
        if not self.conhecido or self.pendente is not None:
            return None
        return self._ev("estado", ts, fonte="adapter", estado=self.estado,
                        aguardando_rearme=self.aguardando_rearme,
                        teto=self.teto, frente=self.frente)

    def esquecer(self) -> None:
        """A USB do receptor caiu: o que se sabia da porta deixou de valer."""
        self.conhecido = False
        self.estado = None
        self.aguardando_rearme = False
        self.teto = self.frente = None
        self.pendente = None

    def vencer_pendente(self, agora: float, espera_s: float,
                        ts: Optional[str] = None) -> list[dict]:
        """`porta_aberta` sem a linha ES no prazo sobe com `porta: None`."""
        pendente = self.pendente
        if pendente is None or agora - pendente["desde"] < espera_s:
            return []
        self.pendente = None
        return self._abertas(ts, pendente, []) + [self.estado_evento(ts)]

    def aplicar(self, linha: Optional[Linha], agora: float,
                ts: Optional[str] = None) -> list[dict]:
        if linha is None:
            return []
        if linha.tipo == "receptor_sem_dados":
            self.receptor_sem_dados = True
            return []
        # Qualquer linha da porta prova que o fio porta→receptor tem dado.
        self.receptor_sem_dados = False
        if linha.tipo == "sensores":
            return self._sensores(linha, ts)
        return self._evt(linha, agora, ts)

    def _sensores(self, linha: Linha, ts: Optional[str]) -> list[dict]:
        if not self.conhecido:
            return []          # sem estado provado, a linha ES sozinha não diz nada
        pendente, self.pendente = self.pendente, None
        mudou = (linha.teto, linha.frente) != (self.teto, self.frente)
        self.teto, self.frente = linha.teto, linha.frente
        if pendente is not None:
            abertas = [p for p, v in (("teto", linha.teto), ("frente", linha.frente))
                       if v == "aberta"]
            return self._abertas(ts, pendente, abertas) + [self.estado_evento(ts)]
        return [self.estado_evento(ts)] if mudou else []

    def _evt(self, linha: Linha, agora: float, ts: Optional[str]) -> list[dict]:
        codigo = linha.codigo
        self.ultimo_codigo = codigo
        if codigo in CODIGOS_DE_BANCADA:
            self.ultimo_codigo_bancada = codigo
            return []
        if codigo not in CODIGOS_QUE_SOBEM:
            self.codigos_desconhecidos += 1
            return []
        if linha.uid:
            self.ultimo_uid = linha.uid
        uid = linha.uid

        if codigo == "TAG_NEGADA":
            return [self._ev("tag_negada", ts, uid=uid)] if self.conhecido else []

        # Um `porta_aberta` ainda esperando a linha ES quando chega OUTRO
        # código: ela não vem mais; sobe sem porta, antes do que vier agora.
        eventos: list[dict] = []
        if self.pendente is not None:
            eventos += self._abertas(ts, self.pendente, [])
            self.pendente = None

        if codigo == "BOOT_OK":
            self._fixar("FECHADA", rearme=False)
            eventos += [self._ev("boot", ts, teto=self.teto, frente=self.frente)]
        elif codigo == "ALERTA_SEM_AUTORIZACAO":
            self._abrir("ABERTA_SEM_AUTORIZACAO", autorizada=False, agora=agora)
            return eventos          # o resto sobe com a linha ES (ou no prazo)
        elif codigo == "ALERTA_PORTA_ABERTA":
            self._abrir("ABERTA_AUTORIZADA", autorizada=True, agora=agora)
            return eventos
        elif codigo == "PAUSA":
            # A porta só libera com as duas fechadas e sem rearme pendente.
            self._fixar("LIBERADA", rearme=False)
            eventos += [self._ev("pedido_acesso", ts, uid=uid, autorizada_pela_porta=True),
                        self._ev("liberada", ts, janela_ms=self.janela_ms)]
        elif codigo == "LIBERACAO_EXPIRADA":
            self._fixar("FECHADA", rearme=True)
            eventos += [self._ev("liberacao_expirada", ts, motivo="janela_expirou")]
        elif codigo == "PORTA_FECHADA":
            self._fixar("FECHADA", rearme=True)
            eventos += [self._ev("porta_fechada", ts)]
        elif codigo == "REARME_OK":
            # O `estado` FECHADA sem rearme ANTES do `rearme`: é ele que o
            # `_bloqueio_da_retomada` do central confere.
            self._fixar("FECHADA", rearme=False)
            return eventos + [self.estado_evento(ts), self._ev("rearme", ts, uid=uid)]
        return eventos + [self.estado_evento(ts)]

    def resumo(self) -> dict:
        """O modelo para o /health."""
        return {
            "conhecido": self.conhecido,
            "estado": self.estado,
            "aguardando_rearme": self.aguardando_rearme,
            "teto": self.teto,
            "frente": self.frente,
            "porta_aberta_pendente": self.pendente is not None,
            "ultimo_uid": self.ultimo_uid,
            "ultimo_codigo": self.ultimo_codigo,
        }
