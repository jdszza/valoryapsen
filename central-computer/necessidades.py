"""
APSEN — Necessidades: o que precisa de atenção, numa lista priorizada.

O app de :8051 deixou de ser o painel de consulta de chão de fábrica e virou o do
GESTOR DA OPERAÇÃO — quem abre a tela para saber o que fazer, não para consultar
um componente específico. Com dez abas independentes, descobrir isso exigia
visitar todas: a trava está numa, os alarmes em outra, o resíduo dos dispensers
numa terceira. Esta é a lista que responde de uma vez.

A prioridade é a do IMPACTO, não a da gravidade abstrata
────────────────────────────────────────────────────────
1. **Trava do Triple Check** — bloqueia a produção AGORA. O loop do orquestrador
   é único: enquanto ela não for liberada, nenhuma OS anda e a fila enche por
   trás. Nada mais nesta lista tem esse efeito.
2. **Fila no teto** — a planta está recusando OS com 429. É consequência
   frequente do item 1, e aparece logo abaixo dele por isso.
3. **Alarmes abertos** — algo já falhou e ninguém fechou o assunto.
4. **Componentes fora da faixa** — vai falhar, ainda não falhou.
5. **Dispensers com resíduo parado** — estoque imobilizado; a OS seguinte
   resolve sozinha se precisar do mesmo medicamento, mas o slot conta como
   ocupado no `atribuir_slots`.
6. **OS em erro nas últimas horas** — já aconteceu e acabou; é diagnóstico,
   não pendência.

Ordenar por gravidade "de manutenção" poria desgaste de correia acima de trava
ativa, e a tela deixaria de responder à pergunta que a motivou.

Cada linha leva a uma ABA
─────────────────────────
O campo `aba` é o id do item da sidebar, e é o que torna esta tela um ponto de
PARTIDA e não mais um relatório. Uma lista que diz "há 3 alarmes" e não leva a
lugar nenhum obriga o gestor a refazer a navegação que a tela existe para
poupar. `tests/test_necessidades.py` compara os ids emitidos aqui contra a
sidebar do `manut_web/app.py` — aba inexistente é um clique que não faz nada.

Este módulo é puro
──────────────────
Sem FastAPI, sem `database`, sem HTTP: recebe os fatos e devolve a lista. Mesma
separação de `prevoo.py` e `seed_demo.py`, e pelo mesmo motivo — dá para testar
a prioridade e os limiares sem subir nada.
"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Severidades. São três porque a tela pinta três cores; um quarto nível pediria
# uma cor que o tema não tem e que ninguém saberia ler.
CRITICO, ATENCAO, INFO = "critico", "atencao", "info"

# Ordem de prioridade. A posição nesta tupla é a chave de ordenação, e é por
# isso que ela é uma tupla e não um dicionário: a ordem É o dado.
PRIORIDADE = (
    "trava",
    "fila",
    "alarme",
    "componente",
    "residuo",
    "os_erro",
)

# ── Limiares ──────────────────────────────────────────────────────────────────
# Os dois primeiros são os MESMOS que o app de manutenção já usa para pintar de
# vermelho (`_cor_disp`, `_cor_desgaste`, e o 65 do render de temperatura). Um
# limiar próprio aqui faria a tela de necessidades listar um componente que a
# aba de temperaturas mostra em verde — e quem visse os dois duvidaria dos dois.
TEMP_CRITICA_C     = 65.0
TEMP_ATENCAO_C     = 50.0
DESGASTE_CRITICO   = 80.0
DESGASTE_ATENCAO   = 60.0

# Resíduo que vale mencionar. Abaixo disso o slot está praticamente vazio e
# listá-lo seria ruído: o `atribuir_slots` reaproveita ou limpa sem custo.
RESIDUO_MINIMO = 1

# Fila: a partir de que ocupação ela vira pendência. 100% é recusa (429); o
# aviso vem antes, porque avisar só quando já está recusando é avisar tarde.
FILA_ATENCAO_FRACAO = 0.6

# Janela de "últimas horas" para OS em erro. Curta: o objetivo é "aconteceu
# agora, ainda dá para investigar", não estatística — para isso existe a aba de
# ordens, com o histórico inteiro.
JANELA_OS_ERRO_H = 12

# Teto por CATEGORIA, não da lista inteira. Vinte alarmes abertos viram vinte
# linhas e empurram tudo abaixo deles para fora da tela — inclusive a OS em erro
# que talvez os explique. O que passa do teto vira uma linha de "e mais N".
MAX_POR_CATEGORIA = 6


def _item(categoria: str, severidade: str, titulo: str, detalhe: str,
          aba: str, acao: str, chave: str = "") -> dict:
    return {
        "categoria":  categoria,
        "severidade": severidade,
        "titulo":     titulo,
        "detalhe":    detalhe,
        "aba":        aba,
        "acao":       acao,
        "chave":      chave or f"{categoria}:{titulo}",
    }


def _quando(ts) -> datetime | None:
    """Timestamp do banco (ISO, com ou sem fuso) em `datetime` com UTC.

    Devolve `None` em vez de levantar: um `ts` estranho numa linha não pode
    derrubar a tela inteira — ela é justamente a que se abre quando algo já
    está errado.
    """
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        quando = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return quando if quando.tzinfo else quando.replace(tzinfo=timezone.utc)


# ── 1. Trava ──────────────────────────────────────────────────────────────────

def _itens_trava(trava: dict | None) -> list[dict]:
    trava = trava or {}
    if not trava.get("ativa"):
        return []
    slot = trava.get("slot_id")
    return [_item(
        "trava", CRITICO,
        f"Trava do Triple Check ativa — slot D{slot}" if slot else
        "Trava do Triple Check ativa",
        f"OS {trava.get('os_id', '?')}: {trava.get('motivo', 'sem motivo registrado')}",
        "trava",
        "Confira o medicamento no slot e libere a trava. Enquanto ela estiver "
        "ativa, NENHUMA OS anda — o orquestrador é um loop único.",
        chave="trava",
    )]


# ── 2. Fila ───────────────────────────────────────────────────────────────────

def _itens_fila(fila: dict | None) -> list[dict]:
    fila = fila or {}
    tamanho = int(fila.get("tamanho", 0) or 0)
    capacidade = int(fila.get("capacidade", 0) or 0)
    if not capacidade or tamanho < capacidade * FILA_ATENCAO_FRACAO:
        return []

    cheia = tamanho >= capacidade
    return [_item(
        "fila", CRITICO if cheia else ATENCAO,
        "Fila de OS no limite" if cheia else "Fila de OS acumulando",
        f"{tamanho} de {capacidade} esperando"
        + (" — novas ordens estão sendo recusadas com 429." if cheia else "."),
        "ordens",
        "A planta está recebendo mais rápido do que processa. Veja se há trava "
        "ativa (é a causa mais comum) ou pause o gerador no console."
        if cheia else
        "Acompanhe: acima de 100% o central passa a recusar ordens com 429.",
        chave="fila",
    )]


# ── 3. Alarmes ────────────────────────────────────────────────────────────────

def _itens_alarmes(alarmes: list | None) -> list[dict]:
    """Agrupados por FONTE, mais recentes primeiro.

    Agrupar é o que impede a lista de virar um log: uma câmera com problema
    emite dezenas de alarmes iguais, e quinze linhas idênticas empurram para
    fora da tela a OS em erro que talvez as explique. A fonte é o agrupamento
    certo porque é ela que diz onde ir — `camera_dispenser_dir_7` manda o
    técnico à lente certa, que é exatamente por que a fonte carrega a câmera.
    """
    grupos: dict[str, list] = {}
    for alarme in alarmes or []:
        if alarme.get("resolvido"):
            continue
        grupos.setdefault(alarme.get("fonte") or "desconhecida", []).append(alarme)

    itens = []
    for fonte, lista in grupos.items():
        lista.sort(key=lambda a: str(a.get("ts") or ""), reverse=True)
        recente = lista[0]
        n = len(lista)
        itens.append(_item(
            "alarme", CRITICO if n > 1 else ATENCAO,
            f"{fonte}: {n} alarme(s) aberto(s)" if n > 1
            else f"{fonte}: 1 alarme aberto",
            f"[{recente.get('tipo', '?')}] {recente.get('descricao', '')}",
            "alarmes",
            "Resolva na aba de alarmes depois de tratar a causa — resolver sem "
            "tratar só apaga o registro.",
            chave=f"alarme:{fonte}",
        ))

    # Mais recente primeiro, pelo alarme mais novo de cada fonte.
    itens.sort(key=lambda i: i["chave"])
    ordem = {f"alarme:{fonte}": max(str(a.get("ts") or "") for a in lista)
             for fonte, lista in grupos.items()}
    itens.sort(key=lambda i: ordem.get(i["chave"], ""), reverse=True)
    return itens


# ── 4. Componentes ────────────────────────────────────────────────────────────

def _itens_componentes(leituras: list | None) -> list[dict]:
    """Temperatura e desgaste acima do limiar, do pior para o melhor.

    Só a ÚLTIMA leitura de cada componente entra (é o que
    `get_ultimas_leituras` devolve): a tela responde "como está agora", e a
    série histórica é o assunto da aba de temperaturas.
    """
    itens = []
    for leitura in leituras or []:
        componente = leitura.get("componente") or "?"
        tipo = (leitura.get("tipo") or "").lower()
        unidade = leitura.get("unidade") or ""
        try:
            valor = float(leitura.get("valor", 0) or 0)
        except (TypeError, ValueError):
            continue

        if tipo == "temperatura":
            if valor >= TEMP_CRITICA_C:
                sev, faixa = CRITICO, f"acima de {TEMP_CRITICA_C:.0f}°C"
            elif valor >= TEMP_ATENCAO_C:
                sev, faixa = ATENCAO, f"acima de {TEMP_ATENCAO_C:.0f}°C"
            else:
                continue
            itens.append(_item(
                "componente", sev, f"{componente} a {valor:.1f}°C",
                f"Temperatura {faixa}.", "temp",
                "Veja a série na aba de temperaturas. Se subir de forma "
                "sustentada, registre uma manutenção preventiva.",
                chave=f"componente:{componente}:temperatura",
            ))
        elif "%" in unidade:
            if valor >= DESGASTE_CRITICO:
                sev, faixa = CRITICO, f"acima de {DESGASTE_CRITICO:.0f}%"
            elif valor >= DESGASTE_ATENCAO:
                sev, faixa = ATENCAO, f"acima de {DESGASTE_ATENCAO:.0f}%"
            else:
                continue
            itens.append(_item(
                "componente", sev, f"{componente} com {valor:.0f}% de desgaste",
                f"Desgaste {faixa}.", "uso",
                "Programe a troca pela aba de nova manutenção antes que o "
                "componente pare no meio de uma OS.",
                chave=f"componente:{componente}:desgaste",
            ))

    itens.sort(key=lambda i: (i["severidade"] != CRITICO, i["titulo"]))
    return itens


# ── 5. Resíduo nos dispensers ─────────────────────────────────────────────────

def _itens_residuo(dispensers: dict | None, os_ativa: dict | None) -> list[dict]:
    """Slots com estoque parado — candidatos a limpeza.

    Slot da OS EM EXECUÇÃO fica de fora: ali o "resíduo" é a carga que está
    sendo dispensada agora, e sugerir limpeza dele seria sugerir abortar a OS.
    É a mesma distinção que o `_do_limpar` do simulador faz ao recusar limpeza
    de slot em operação.
    """
    os_id_ativa = (os_ativa or {}).get("os_id")
    itens = []
    for chave, slot in sorted((dispensers or {}).items(),
                              key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
        quantidade = slot.get("quantidade_residual") or slot.get("quantidade") or 0
        try:
            quantidade = int(quantidade)
        except (TypeError, ValueError):
            continue
        if quantidade < RESIDUO_MINIMO:
            continue
        if slot.get("status") in ("carregando", "dispensando"):
            continue
        if os_id_ativa and slot.get("os_id") == os_id_ativa:
            continue

        medicamento = slot.get("medicamento") or "medicamento não identificado"
        itens.append(_item(
            "residuo", INFO, f"D{chave} com {quantidade} un. de resíduo",
            f"{medicamento} parado no slot"
            + (f" desde a OS {slot['os_id']}." if slot.get("os_id") else "."),
            "dispensers",
            "Limpe o slot se o medicamento não for usado nas próximas ordens — "
            "slot ocupado por outro item entra na OS seguinte com descarte.",
            chave=f"residuo:{chave}",
        ))
    return itens


# ── 6. OS em erro ─────────────────────────────────────────────────────────────

def _itens_os_erro(ordens: list | None, agora: datetime | None = None,
                   janela_h: int = JANELA_OS_ERRO_H) -> list[dict]:
    momento = agora or datetime.now(timezone.utc)
    corte = momento - timedelta(hours=janela_h)

    recentes = []
    for ordem in ordens or []:
        if ordem.get("status") != "erro":
            continue
        quando = _quando(ordem.get("concluida_em") or ordem.get("criado_em"))
        if quando is None or quando < corte:
            continue
        recentes.append((quando, ordem))

    recentes.sort(key=lambda par: par[0], reverse=True)
    return [
        _item("os_erro", ATENCAO, f"OS {ordem.get('os_id', '?')} terminou em erro",
              (ordem.get("descricao") or "").strip() or "sem descrição",
              "ordens",
              "Abra o JSON da OS na aba de ordens para ver em qual etapa parou.",
              chave=f"os_erro:{ordem.get('os_id')}")
        for _, ordem in recentes
    ]


# ── Montagem ──────────────────────────────────────────────────────────────────

def _limitar(itens: list[dict]) -> list[dict]:
    """Teto por categoria, com uma linha de resumo no lugar do excedente.

    Cortar em silêncio seria mentir sobre o tamanho do problema; não cortar
    faria vinte alarmes de uma câmera empurrarem para fora da tela a OS em erro
    que talvez os explique.
    """
    vistos: dict[str, int] = {}
    saida: list[dict] = []
    excedente: dict[str, int] = {}

    for item in itens:
        categoria = item["categoria"]
        vistos[categoria] = vistos.get(categoria, 0) + 1
        if vistos[categoria] <= MAX_POR_CATEGORIA:
            saida.append(item)
        else:
            excedente[categoria] = excedente.get(categoria, 0) + 1

    for categoria, quantos in excedente.items():
        saida.append(_item(
            categoria, INFO, f"… e mais {quantos} item(ns) desta categoria",
            f"A lista mostra os {MAX_POR_CATEGORIA} primeiros para não esconder "
            f"as outras categorias.",
            _ABA_POR_CATEGORIA.get(categoria, "alarmes"),
            "Veja a lista completa na aba correspondente.",
            chave=f"{categoria}:excedente",
        ))
    return saida


# Para onde a linha de excedente leva. Deriva do primeiro item que cada
# categoria produz, então não há segunda tabela a manter em sincronia — mas
# escrevê-la explícita seria a segunda tabela, então o mapa vive aqui e o teste
# confere que toda categoria de `PRIORIDADE` tem entrada.
_ABA_POR_CATEGORIA = {
    "trava":      "trava",
    "fila":       "ordens",
    "alarme":     "alarmes",
    "componente": "temp",
    "residuo":    "dispensers",
    "os_erro":    "ordens",
}


def montar(*, trava=None, fila=None, alarmes=None, leituras=None,
           dispensers=None, os_ativa=None, ordens=None,
           agora: datetime | None = None) -> dict:
    """A lista priorizada + o resumo que a tela mostra no topo.

    Tudo por palavra-chave: são sete fontes de fato, e um chamador que trocasse
    `alarmes` por `ordens` numa chamada posicional produziria uma tela plausível
    e errada — o pior modo de falhar para a tela que existe para dizer o que
    está errado.
    """
    itens = (
        _itens_trava(trava)
        + _itens_fila(fila)
        + _itens_alarmes(alarmes)
        + _itens_componentes(leituras)
        + _itens_residuo(dispensers, os_ativa)
        + _itens_os_erro(ordens, agora)
    )
    # `PRIORIDADE.index` é a chave, e a ordem DENTRO de cada categoria já veio
    # decidida por quem a montou (alarme mais recente primeiro, componente mais
    # quente primeiro). `sorted` é estável, então ela sobrevive.
    itens.sort(key=lambda i: PRIORIDADE.index(i["categoria"]))
    itens = _limitar(itens)

    contagem = {CRITICO: 0, ATENCAO: 0, INFO: 0}
    for item in itens:
        contagem[item["severidade"]] = contagem.get(item["severidade"], 0) + 1

    return {
        "itens": itens,
        "resumo": {
            "total":      len(itens),
            "criticos":   contagem[CRITICO],
            "atencao":    contagem[ATENCAO],
            "info":       contagem[INFO],
            # `tudo_em_dia` é FALSO com qualquer item, inclusive informativo:
            # a tela promete "não há nada pendente", e resíduo parado é
            # pendência — pequena, mas pendência.
            "tudo_em_dia": not itens,
        },
    }
