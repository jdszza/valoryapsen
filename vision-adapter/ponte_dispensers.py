"""A ponte entre o central e a estação de visão dos dispensers — a parte pura.

A estação (`vision/visao`) não recebe comando: ela olha sem parar e julga cada
zona contra um CATÁLOGO que ela mesma busca (`GET /api/visao/catalogo`). Quem
sabe o que DEVERIA estar em cada slot é o central; quem sabe o que ESTÁ é a
estação; quem junta os dois é o adapter. Este módulo é a parte desse trabalho
que não depende de rede nem de relógio:

- a tabela de etiquetas (nome do central → QR/ArUco impressos);
- o catálogo da OS corrente, no formato que `Catalogo.de_itens` da estação
  aceita;
- o lado da câmera a partir do slot;
- a tradução do veredito da estação no evento `leitura_dispenser_*`.

Sem asyncio e sem HTTP de propósito: é o que deixa testar o catálogo contra o
`Catalogo.de_itens` de verdade e cada linha da tradução sem subir nada.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

CAM_ESQ = "dispenser_esq"
CAM_DIR = "dispenser_dir"

# O dicionário ArUco das etiquetas é o DICT_4X4_50: ids 0..49.
ARUCO_MAX = 49

# Número dos medicamentos que estão na tabela mas não em slot nenhum da OS.
# Não existe zona com esse número na imagem, e é isso que os faz úteis: ver
# `montar_catalogo`.
BASE_FANTASMA = 1000

MOTIVO_SEM_ETIQUETA = "sem etiqueta cadastrada"
MOTIVO_REPETIDO = "medicamento repetido em mais de um slot da OS"


def normalizar_nome(nome) -> str:
    """strip + upper + espaços colapsados — a única comparação de nome aqui."""
    return re.sub(r"\s+", " ", str(nome or "")).strip().upper()


def camera_do_slot(slot_id: int, num_slots: int) -> str:
    """Qual câmera de dispenser olha o slot: 1..N/2 à esquerda, o resto à direita.

    A mesma partição de `vision-simulator.camera_do_slot` e da geometria do
    orquestrador; `tests/test_vision_adapter_ponte.py` compara as duas funções.
    """
    return CAM_ESQ if slot_id <= max(1, num_slots // 2) else CAM_DIR


# ── Tabela de etiquetas ───────────────────────────────────────────────────────

def carregar_etiquetas(caminho: str | Path) -> list[dict]:
    """Lê e valida a tabela. Nunca levanta: item ruim sai com ERROR no log.

    Derrubar o adapter por uma linha errada na tabela pararia a câmera da mesa
    junto, que não tem nada a ver com etiqueta. Item descartado é um medicamento
    que a câmera dos dispensers não confere — e o slot dele vira falha com
    motivo próprio, visível no histórico.

    Cada item devolvido ganha `posicao` (1-based, na ordem do ARQUIVO): é ela
    que dá o número fantasma, e contar só os itens válidos faria o número de um
    medicamento mudar porque a linha de cima tem um erro de digitação.
    """
    try:
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
        brutos = dados.get("etiquetas", [])
        if not isinstance(brutos, list):
            raise ValueError("'etiquetas' não é uma lista")
    except (OSError, ValueError, AttributeError) as exc:
        logger.error("[ETIQUETAS] %s ilegível (%s) — nenhuma etiqueta carregada: "
                     "toda leitura de SKU vai sair como falha.", caminho, exc)
        return []

    validas: list[dict] = []
    vistos_nome: set[str] = set()
    vistos_qr: set[str] = set()
    vistos_aruco: set[int] = set()
    for posicao, bruto in enumerate(brutos, start=1):
        problema = None
        item = bruto if isinstance(bruto, dict) else {}
        if not isinstance(bruto, dict):
            problema = "não é um objeto"
        nome = normalizar_nome(item.get("nome"))
        qr = str(item.get("qr") or "").strip()
        aruco = item.get("aruco")
        if problema:
            pass
        elif not nome:
            problema = "nome vazio"
        elif not qr:
            problema = "qr vazio"
        elif not isinstance(aruco, int) or isinstance(aruco, bool):
            problema = f"aruco {aruco!r} não é inteiro"
        elif not 0 <= aruco <= ARUCO_MAX:
            problema = f"aruco {aruco} fora de 0..{ARUCO_MAX} (DICT_4X4_50)"
        elif nome in vistos_nome:
            problema = f"nome {item.get('nome')!r} repetido"
        elif qr in vistos_qr:
            problema = f"qr {qr!r} repetido"
        elif aruco in vistos_aruco:
            problema = f"aruco {aruco} repetido"
        if problema:
            logger.error("[ETIQUETAS] linha %d descartada (%s): %r", posicao, problema, item)
            continue
        vistos_nome.add(nome)
        vistos_qr.add(qr)
        vistos_aruco.add(aruco)
        validas.append({"nome": str(item["nome"]).strip(), "qr": qr,
                        "aruco": aruco, "posicao": posicao})
    return validas


# ── Catálogo da OS corrente ───────────────────────────────────────────────────

def montar_catalogo(slots: dict[int, str], etiquetas: list[dict]) -> dict:
    """O catálogo que as estações leem, no formato de `Catalogo.de_itens`.

    `slots` é {slot: nome do medicamento que o central mandou carregar} da OS
    corrente — SÓ dela: herdar slots da OS anterior poria o mesmo medicamento
    em dois dispensers, e a estação recusaria o catálogo inteiro.

    Nunca devolve um catálogo que a estação recusaria. O que não cabe vai para
    `incompletos`, e os demais slots seguem:
    - slot cujo medicamento não tem etiqueta: a estação não tem como
      reconhecê-lo;
    - medicamento em dois slots da mesma OS: a regra da estação é UM dispenser
      por medicamento, e mandá-lo duas vezes derrubaria o catálogo inteiro.

    FANTASMAS: todo medicamento da tabela que não está num slot entra também,
    com dispenser `1000 + posição`. Esses números não têm zona na imagem, e é
    exatamente isso que os torna úteis: uma caixa de DONAREN no D3 de uma OS
    que pediu RETEMIC é lida como "DONAREN, cujo dispenser é outro" —
    ERRO_POSICAO com o nome do que foi achado. Fora do catálogo, a mesma caixa
    daria NAO_CADASTRADO sem nome nenhum, e a trava diria que está errado sem
    dizer o quê.
    """
    por_nome = {normalizar_nome(e["nome"]): e for e in etiquetas}

    slots_por_etiqueta: dict[str, list[int]] = {}
    incompletos: list[dict] = []
    for slot in sorted(slots):
        etiqueta = por_nome.get(normalizar_nome(slots[slot]))
        if etiqueta is None:
            incompletos.append({"dispenser": slot, "nome": slots[slot],
                                "motivo": MOTIVO_SEM_ETIQUETA})
            continue
        slots_por_etiqueta.setdefault(etiqueta["qr"], []).append(slot)

    medicamentos: list[dict] = []
    usadas: set[str] = set()
    for etiqueta in etiquetas:
        dono = slots_por_etiqueta.get(etiqueta["qr"], [])
        if len(dono) > 1:
            for slot in dono:
                incompletos.append({
                    "dispenser": slot, "nome": slots[slot],
                    "motivo": f"{MOTIVO_REPETIDO} "
                              f"({', '.join(f'D{s}' for s in dono)})",
                })
            continue
        if len(dono) == 1:
            medicamentos.append(_item(etiqueta, dono[0], slots[dono[0]]))
            usadas.add(etiqueta["qr"])

    for etiqueta in etiquetas:
        if etiqueta["qr"] not in usadas:
            medicamentos.append(_item(etiqueta, BASE_FANTASMA + etiqueta["posicao"],
                                      etiqueta["nome"]))

    medicamentos.sort(key=lambda m: m["dispenser"])
    incompletos.sort(key=lambda i: i["dispenser"])
    return {"medicamentos": medicamentos, "incompletos": incompletos}


def _item(etiqueta: dict, dispenser: int, nome: str) -> dict:
    return {"qr": etiqueta["qr"], "nome": nome, "dispenser": dispenser,
            "aruco": etiqueta["aruco"], "unidades_por_caixa": 1}


def motivo_do_incompleto(catalogo: dict, slot: int) -> str | None:
    """Por que este slot ficou fora do catálogo — None se ele está dentro."""
    for item in catalogo.get("incompletos", []):
        if item["dispenser"] == slot:
            return item["motivo"]
    return None


# ── Leitura do /api/estado ────────────────────────────────────────────────────

def momento(linha: dict | None) -> datetime | None:
    """O `momento` da linha como datetime — o relógio da ESTAÇÃO, nunca o nosso."""
    if not linha or not linha.get("momento"):
        return None
    try:
        return datetime.fromisoformat(str(linha["momento"]))
    except ValueError:
        return None


def linha_do_slot(estado: dict, slot: int) -> dict | None:
    """A linha mais recente do slot em `{"estado": [...]}`.

    O banco da estação guarda uma linha por (estacao, dispenser), e uma linha de
    um nome de estação antigo — o `--estacao` mudou, ou a estação subiu uma vez
    sem ele e gravou com o nome da máquina — fica lá com o `momento` parado.
    Pegar a primeira em vez da mais recente leria essa linha morta.
    """
    candidatas = [l for l in (estado or {}).get("estado", [])
                  if l.get("dispenser") == slot and momento(l) is not None]
    return max(candidatas, key=momento) if candidatas else None


# ── Veredito da estação → evento do central ──────────────────────────────────

SKU_DESCONHECIDO = "desconhecido (codigo fora da tabela de etiquetas)"

_DIVERGENTES = ("ERRO_POSICAO", "DIVERGENCIA")

# O que fazer com NAO_CADASTRADO — código lido que não está no catálogo. Na
# estação, QR desconhecido é estado de erro e VENCE a ocorrência certa na mesma
# zona; a embalagem real traz cada vez mais QR impresso (bula digital), e
# qualquer QR alheio na zona travaria a OS com o medicamento CERTO. O ganho de
# tratá-lo como divergência é pequeno: caixa errada sem etiqueta e sem QR já sai
# VAZIO (falha). "falha" é o default; "divergencia" é o comportamento antigo.
NAO_CADASTRADO_FALHA = "falha"
NAO_CADASTRADO_DIVERGENCIA = "divergencia"
MODOS_NAO_CADASTRADO = (NAO_CADASTRADO_FALHA, NAO_CADASTRADO_DIVERGENCIA)
MOTIVO_CODIGO_DESCONHECIDO = "codigo_desconhecido_na_zona"


def traduzir(linha: dict, esperado: str,
             nao_cadastrado: str = NAO_CADASTRADO_FALHA) -> tuple[str, str | None]:
    """(tipo do evento, motivo de falha) para a linha do slot.

    Leitura incerta é FALHA, nunca divergência: divergência trava a OS e chama
    supervisor, falha só deixa de confirmar. Veredito que esta tabela não
    conhece cai em falha pelo mesmo motivo.
    """
    veredito = str(linha.get("veredito") or "")
    if veredito == "OK":
        if normalizar_nome(linha.get("medicamento")) == normalizar_nome(esperado):
            return "leitura_dispenser_ok", None
        logger.warning("[PONTE] estação deu OK com %r num slot que esperava %r — "
                       "tratado como divergência.", linha.get("medicamento"), esperado)
        return "leitura_dispenser_divergencia", None
    if veredito in _DIVERGENTES:
        return "leitura_dispenser_divergencia", None
    if veredito == "NAO_CADASTRADO":
        if nao_cadastrado == NAO_CADASTRADO_DIVERGENCIA:
            return "leitura_dispenser_divergencia", None
        return "leitura_dispenser_falha", MOTIVO_CODIGO_DESCONHECIDO
    if veredito == "VAZIO":
        return "leitura_dispenser_falha", "produto_nao_identificado"
    return "leitura_dispenser_falha", "leitura_inconclusiva"


# ── A janela de estabilidade ──────────────────────────────────────────────────
#
# O `/api/estado` da estação é a fotografia de UM frame: o veredito é gravado a
# cada `processar`, e o `frames_para_confirmar` dela governa só o alerta
# sonoro. Na zona vence o pior caso, então um único frame com um ArUco falso de
# id conhecido (e, com os fantasmas, todo medicamento etiquetado é conhecido),
# a mão do operador ou um reflexo vira ERRO_POSICAO — e a OS trava. Por isso o
# adapter colhe várias amostras, de `momento`s distintos, e decide sobre elas.

MOTIVO_INSTAVEL = "leitura_instavel"


def _rotulo(linha: dict) -> str:
    """Veredito + medicamento: o que identifica UMA leitura entre amostras."""
    veredito = str(linha.get("veredito") or "?")
    nome = normalizar_nome(linha.get("medicamento"))
    return f"{veredito}:{nome}" if nome else veredito


def decidir(amostras: list[dict], esperado: str,
            nao_cadastrado: str = NAO_CADASTRADO_FALHA
            ) -> tuple[str, str | None, dict, dict]:
    """(tipo, motivo, linha, contagens) para a janela de amostras do slot.

    - divergência só se a MESMA leitura divergente (veredito + medicamento)
      aparece em pelo menos 2/3 das amostras E na última;
    - ok se a maioria é OK com o medicamento esperado e nenhuma divergência se
      repete;
    - falha com o motivo de sempre se todas as amostras dão a mesma falha;
    - qualquer outra coisa: falha `leitura_instavel`.

    `linha` é a amostra que representa a decisão (vai no evento); `contagens`
    é {rótulo: quantas amostras} — o campo `amostras` do evento.
    """
    if not amostras:
        raise ValueError("decidir() sem amostras")
    traducoes = [traduzir(a, esperado, nao_cadastrado) for a in amostras]
    contagens = dict(Counter(_rotulo(a) for a in amostras))
    total = len(amostras)
    ultima, (tipo_ultima, _) = amostras[-1], traducoes[-1]

    divergentes = Counter(_rotulo(a) for a, (t, _) in zip(amostras, traducoes, strict=True)
                          if t == "leitura_dispenser_divergencia")
    if tipo_ultima == "leitura_dispenser_divergencia":
        if 3 * divergentes[_rotulo(ultima)] >= 2 * total:
            return "leitura_dispenser_divergencia", None, ultima, contagens

    oks = [a for a, (t, _) in zip(amostras, traducoes, strict=True)
           if t == "leitura_dispenser_ok"]
    repetida = any(n >= 2 for n in divergentes.values())
    if 2 * len(oks) > total and not repetida:
        return "leitura_dispenser_ok", None, oks[-1], contagens

    if len(set(traducoes)) == 1 and tipo_ultima == "leitura_dispenser_falha":
        return "leitura_dispenser_falha", traducoes[-1][1], ultima, contagens
    return "leitura_dispenser_falha", MOTIVO_INSTAVEL, ultima, contagens
