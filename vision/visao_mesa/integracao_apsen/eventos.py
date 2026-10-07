"""Montagem dos eventos que a estacao devolve ao PC central.

Um unico lugar monta os tres eventos, e ele converte os tipos na marra
(`int(...)`, `float(...)`). Nao e paranoia: o contrato diz `quantidade_detectada`
int, nunca float, e um `3.0` no lugar de `3` chega ao central como outro valor —
numpy devolve `np.int64`, divisao devolve float, e nenhum dos dois sobrevive
intacto a um json.dumps. Centralizando a conversao, nenhum caminho de codigo
consegue emitir um evento fora do contrato.

`delta` tambem e calculado aqui, e nao por quem chama, pela mesma razao: e a
unica forma de garantir que ele e sempre detectada - esperada, inclusive nos
caminhos de falha, onde e facil esquecer.
"""

from __future__ import annotations

from datetime import datetime, timezone

CAMERA_MESA = "mesa"

TIPO_OK = "leitura_mesa_ok"
TIPO_DIVERGENCIA = "leitura_mesa_divergencia"
TIPO_FALHA = "leitura_mesa_falha"
TIPOS_MESA = (TIPO_OK, TIPO_DIVERGENCIA, TIPO_FALHA)

# Vocabulario do documento de integracao. Outro texto curto em snake_case e
# aceito pelo central, mas sair desta lista sem motivo dificulta o filtro do
# log de quem for diagnosticar a celula depois.
MOTIVOS = (
    "produto_nao_detectado",
    "obstrucao_visual",
    "camera_desalinhada",
    "produto_fora_zona_coleta",
    "camera_indisponivel",
    "baixa_confianca",
    "timeout_processamento",
    "contagem_regrediu",
    "erro_interno",
)


def agora_iso() -> str:
    """ISO 8601 em UTC COM fuso.

    Com fuso porque o central guarda eventos de varias maquinas: um horario sem
    fuso obriga quem le o log a adivinhar de qual relogio ele veio, e numa
    investigacao de divergencia a ordem dos eventos e metade da resposta.
    """
    return datetime.now(timezone.utc).isoformat()


def _base(tipo: str, slot_id, os_id, quantidade_esperada) -> dict:
    return {
        "tipo": tipo,
        "camera": CAMERA_MESA,
        "slot_id": int(slot_id),
        # str() e nao f-string: o os_id volta byte a byte como veio, e e ele que
        # acorda o orquestrador do central. Mudou, o evento se perde e a OS
        # espera o timeout inteiro sem ninguem saber por que.
        "os_id": str(os_id),
        "quantidade_esperada": int(quantidade_esperada),
    }


def evento_leitura(slot_id, os_id, quantidade_esperada, quantidade_detectada,
                   confianca: float, extras: dict | None = None) -> dict:
    """Evento de leitura bem-sucedida: `ok` ou `divergencia`, decidido aqui.

    Quem chama NAO escolhe o tipo. A regra e aritmetica — detectada igual a
    esperada e ok, diferente e divergencia — e deixar a escolha espalhada pelo
    codigo e como nasce um `ok` com delta diferente de zero.
    """
    detectada = int(quantidade_detectada)
    esperada = int(quantidade_esperada)
    evento = _base(TIPO_OK if detectada == esperada else TIPO_DIVERGENCIA,
                   slot_id, os_id, esperada)
    evento.update({
        "quantidade_detectada": detectada,
        "delta": detectada - esperada,
        "confianca": round(float(confianca), 3),
        "ts": agora_iso(),
    })
    if extras:
        evento.update(extras)
    return evento


def evento_falha(slot_id, os_id, quantidade_esperada, motivo: str,
                 posicao_x=None, posicao_y=None, confianca: float = 0.0,
                 quantidade_detectada: int = 0, falha_injetada: bool = False,
                 extras: dict | None = None) -> dict:
    """Nao consegui contar. NAO trava a OS — a camera nao confirmou, mas
    tambem nao contradisse."""
    evento = _base(TIPO_FALHA, slot_id, os_id, quantidade_esperada)
    evento.update({
        "motivo": str(motivo),
        "quantidade_detectada": int(quantidade_detectada),
        "delta": int(quantidade_detectada) - int(quantidade_esperada),
        "posicao_x": None if posicao_x is None else float(posicao_x),
        "posicao_y": None if posicao_y is None else float(posicao_y),
        "confianca": round(float(confianca), 3),
        "falha_injetada": bool(falha_injetada),
        "ts": agora_iso(),
    })
    if extras:
        evento.update(extras)
    return evento


def evento_telemetria(componente: str, tipo_leitura: str, valor: float,
                      unidade: str) -> dict:
    return {
        "tipo": "telemetria",
        "camera": "sistema",
        "componente": str(componente),
        "tipo_leitura": str(tipo_leitura),
        "valor": float(valor),
        "unidade": str(unidade),
        "ts": agora_iso(),
    }


def validar_evento(evento: dict) -> list[str]:
    """Confere o evento contra a tabela de tipos do contrato (secao 5.4).

    Devolve a lista de problemas (vazia = ok). Roda nos testes e tambem antes
    de cada envio: se um dia um caminho novo montar um evento errado, o erro
    aparece no log da estacao, com nome e sobrenome, em vez de virar
    comportamento estranho do outro lado da rede.
    """
    problemas = []
    tipo = evento.get("tipo")
    if tipo == "telemetria":
        return problemas
    if tipo not in TIPOS_MESA:
        problemas.append(f"tipo invalido: {tipo!r}")
    if evento.get("camera") != CAMERA_MESA:
        problemas.append(f"camera deveria ser 'mesa', veio {evento.get('camera')!r}")
    for campo in ("slot_id", "quantidade_esperada", "quantidade_detectada", "delta"):
        valor = evento.get(campo)
        # bool e subclasse de int em Python; True passaria por um isinstance
        # ingenuo e viraria 1 no JSON.
        if not isinstance(valor, int) or isinstance(valor, bool):
            problemas.append(f"{campo} deveria ser int, veio {type(valor).__name__}")
    if not isinstance(evento.get("os_id"), str) or not evento.get("os_id"):
        problemas.append("os_id deveria ser string nao vazia")
    confianca = evento.get("confianca")
    if not isinstance(confianca, (int, float)) or not 0.0 <= float(confianca) <= 1.0:
        problemas.append(f"confianca fora de 0..1: {confianca!r}")
    esperada, detectada = evento.get("quantidade_esperada"), evento.get("quantidade_detectada")
    if isinstance(esperada, int) and isinstance(detectada, int) \
            and evento.get("delta") != detectada - esperada:
        problemas.append("delta nao e detectada - esperada")
    ts = evento.get("ts", "")
    if not isinstance(ts, str) or ("+" not in ts and not ts.endswith("Z")):
        problemas.append(f"ts sem fuso horario: {ts!r}")
    if tipo == TIPO_OK and evento.get("delta") != 0:
        problemas.append("evento 'ok' com delta diferente de zero")
    if tipo == TIPO_DIVERGENCIA and evento.get("delta") == 0:
        problemas.append("evento 'divergencia' com delta zero")
    if tipo == TIPO_FALHA and not evento.get("motivo"):
        problemas.append("evento de falha sem motivo")
    return problemas
