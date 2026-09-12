"""
APSEN - ERP Simulator v2.1
O sistema que, na planta real, EMITE as ordens de saída. Fala com o
central-computer por HTTP; sem MQTT.

Por que este nome
─────────────────
Ele não monta mais o conteúdo das ordens — escolhe uma das dez ordens padrão e a
despacha. O nome anterior descrevia uma geração que deixou de existir, e o
resíduo de nomenclatura é o mesmo que motivou o rename do app de manutenção.

"erp-simulator" e não "os-dispatcher" por duas razões. A primeira é de
vocabulário: todos os outros simuladores desta planta são nomeados pelo que
SUBSTITUEM (`cnc_simulator`, `dispenser_simulator`, `vision-simulator`,
`weight-simulator`), e "dispatcher" nomearia o mecanismo — seria o único
serviço batizado pelo COMO. A segunda é de arquitetura: este container não é uma
peça do APSEN, é o sistema do hospital que manda a ordem para o APSEN. O nome
diz de onde a OS vem, que é a informação que faltava — e o diretório já se
chamou `sap_simulator`, ou seja, é a categoria do que sempre foi (SAP é um
fornecedor; ERP é o papel).

O que ele faz
─────────────
As dez ordens padrão são definidas no central (`central-computer/os_templates.py`,
servidas por `GET /api/v1/ordens/templates`) e este serviço só decide QUAL
delas dispara e QUANDO. Cada disparo instancia um `os_id` novo — o template é
fixo, a chave primária não pode ser, porque `ordens.os_id` é UNIQUE e o central
responde 409 a um reenvio.

Para quem observa a planta, o comportamento é o de um ERP em operação: as OS
chegam em intervalos regulares e não dá para prever qual vem. O que mudou com as
ordens padrão é que o CONTEÚDO de cada uma é conhecido de antemão — que é o que
permite ensaiar uma apresentação.

Por que os templates NÃO moram aqui: o console de operação do central precisa
listar as dez e disparar a escolhida. Uma cópia local viraria duas listas
mantidas à mão, e elas divergem. Ver o topo de `os_templates.py`.

Este serviço também obedece ao interruptor do console: antes de cada envio ele
consulta `GET /api/v1/gerador` e, se a resposta for `pausado`, espera. É como o
operador assume o controle da planta sem competir com o automático — e sem que
ninguém precise parar este container. Ver `central-computer/console.py`.

A rota manteve o nome `/api/v1/gerador` de propósito: rename de serviço não é
rename de rota, e a mesma regra valeu no rename do app de manutenção, cujas
rotas `/manutencao/*` continuaram como estavam.
"""
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ERP-SIM] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

CENTRAL_URL         = os.getenv("CENTRAL_URL",         "http://central-computer:8000")
INTERVALO_OS        = int(os.getenv("INTERVALO_OS",     "90"))
# Backpressure: quanto esperar quando a fila do central está cheia, e quantas
# vezes esperar antes de desistir do ciclo (e voltar a dormir INTERVALO_OS).
ESPERA_FILA_CHEIA   = int(os.getenv("ESPERA_FILA_CHEIA", "20"))
MAX_ESPERAS_FILA    = int(os.getenv("MAX_ESPERAS_FILA",  "15"))
RELOAD_CATALOGO_MIN = int(os.getenv("RELOAD_CATALOGO_MIN", "30"))
# De quanto em quanto tempo reperguntar ao central se a pausa do console saiu.
# Curto: é o atraso entre clicar em "retomar" e a planta voltar a produzir.
ESPERA_PAUSA        = int(os.getenv("ESPERA_PAUSA",      "10"))
# Nº de dispensers da célula — o teto de itens que uma ordem padrão pode ter.
# Mesma env var do central e dos simuladores, declarada uma vez no compose.
NUM_SLOTS           = int(os.getenv("NUM_SLOTS",        "8"))


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Catálogo ───────────────────────────────────────────────────────────────────

def _carregar_catalogo(tentativas: int = 20) -> dict:
    """Catálogo real do central, indexado por nome: `{nome: {sku, categoria, ...}}`.

    Deixou de ser a fonte do sorteio e passou a ter dois papéis: validar os
    templates (nenhum medicamento inventado) e resolver `sku`/`categoria` de
    cada item na hora de instanciar — os templates só declaram nome e
    quantidade, justamente para não congelar um SKU que pode mudar no catálogo.

    A espera com retentativa continua servindo de sincronização de boot: o
    catálogo só existe depois que o `init_db` do central rodou o seed.
    """
    url = CENTRAL_URL + "/medicamentos"
    for i in range(tentativas):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            meds = resp.json()

            if not meds:
                espera = min(10, 3 + i * 2)
                logger.warning("Catálogo vazio (DB ainda inicializando?). "
                               "Tentativa %d/%d. Aguardando %ds...", i + 1, tentativas, espera)
                time.sleep(espera)
                continue

            catalogo = {m["nome"]: m for m in meds if m.get("nome")}
            logger.info("Catálogo: %d medicamentos.", len(catalogo))
            return catalogo

        except Exception as exc:
            espera = min(15, 5 + i * 2)
            logger.warning("Catálogo tentativa %d/%d falhou: %s. Aguardando %ds...",
                           i + 1, tentativas, exc, espera)
            time.sleep(espera)

    logger.error("Impossível carregar catálogo. Encerrando.")
    raise SystemExit(1)


# ── Ordens padrão ──────────────────────────────────────────────────────────────

def _carregar_templates(tentativas: int = 20) -> list:
    """As 10 ordens padrão, lidas do central.

    Mesma política de retentativa do catálogo, e pelo mesmo motivo: este
    serviço sobe junto com o central e a listagem só responde depois que o
    FastAPI está de pé. Esgotadas as tentativas, encerra — sem templates não há
    o que disparar, e `restart: unless-stopped` cuida da nova tentativa.
    """
    url = CENTRAL_URL + "/api/v1/ordens/templates"
    for i in range(tentativas):
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            corpo = resp.json() or {}
            templates = corpo.get("templates") or []

            if not templates:
                espera = min(10, 3 + i * 2)
                logger.warning("Central respondeu sem ordens padrão. "
                               "Tentativa %d/%d. Aguardando %ds...",
                               i + 1, tentativas, espera)
                time.sleep(espera)
                continue

            # O central roda a mesma validação e devolve o resultado aqui. Não
            # é motivo para desistir sozinho — quem decide é `_validar_templates`
            # logo abaixo, com o catálogo em mãos —, mas ecoar o diagnóstico
            # poupa quem for ler dois logs para achar o mesmo nome errado.
            for problema in corpo.get("problemas") or []:
                logger.error("[TEMPLATES] Central acusou: %s", problema)

            logger.info("Ordens padrão: %d carregadas do central.", len(templates))
            return templates

        except Exception as exc:
            espera = min(15, 5 + i * 2)
            logger.warning("Ordens padrão tentativa %d/%d falhou: %s. Aguardando %ds...",
                           i + 1, tentativas, exc, espera)
            time.sleep(espera)

    logger.error("Impossível carregar as ordens padrão. Encerrando.")
    raise SystemExit(1)


def _problemas_dos_templates(templates: list, catalogo: dict) -> list:
    """O que impede um template de virar OS válida. Lista vazia = pode disparar.

    Duas checagens, e as duas são do consumidor — este processo é o último
    ponto antes de a OS entrar no sistema:

      1. **medicamento que não existe no catálogo.** É o erro mais provável de
         aparecer em cima da hora: alguém corrige um nome no template e ele
         deixa de casar com a tabela `medicamentos`. A OS sairia com `sku`
         vazio, a câmera do dispenser não teria o que comparar, e o sintoma
         chegaria disfarçado de falha de leitura num slot só.
      2. **mais itens do que dispensers.** A OS entraria na fila, ocuparia a
         vaga e seria abortada com `sem_slot` só quando o orquestrador a
         pegasse.

    Os invariantes de forma (faixa de itens, quantidades, item repetido) ficam
    do lado de quem DEFINE os templates: `os_templates.validar_estrutura`, que
    roda no import do módulo e no endpoint. Repeti-los aqui seria manter a
    mesma regra em dois lugares para checar dados que já vieram checados.
    """
    problemas: list[str] = []
    for template in templates:
        tid   = template.get("template_id", "?")
        itens = template.get("itens") or []

        if len(itens) > NUM_SLOTS:
            problemas.append(
                f"{tid}: {len(itens)} itens para {NUM_SLOTS} dispensers — "
                f"a OS seria abortada com `sem_slot` depois de ocupar a fila."
            )

        for item in itens:
            nome = item.get("medicamento", "")
            if nome not in catalogo:
                problemas.append(
                    f"{tid}: '{nome}' não existe no catálogo do central "
                    f"({len(catalogo)} medicamentos)."
                )
    return problemas


def _validar_templates(templates: list, catalogo: dict) -> list:
    """Devolve os templates disparáveis; encerra o processo se nenhum sobrar.

    Template quebrado é excluído do sorteio em vez de derrubar tudo: uma ordem
    com um nome errado não é motivo para as outras nove pararem de rodar. Mas
    se NENHUMA sobrar, encerrar é melhor que ficar acordando a cada
    `INTERVALO_OS` sem ter o que enviar — o log de boot diz o motivo uma vez,
    com o nome do medicamento, e não some no meio de um laço.
    """
    problemas = _problemas_dos_templates(templates, catalogo)
    if problemas:
        logger.error("%d problema(s) nas ordens padrão:\n  - %s",
                     len(problemas), "\n  - ".join(problemas))

    quebrados = {p.split(":", 1)[0] for p in problemas}
    validos = [t for t in templates if t.get("template_id") not in quebrados]

    if not validos:
        logger.error("Nenhuma ordem padrão utilizável. Corrija `os_templates.py` "
                     "no central-computer. Encerrando.")
        raise SystemExit(1)

    if quebrados:
        logger.warning("Disparando apenas as %d ordens válidas; ignorando: %s",
                       len(validos), ", ".join(sorted(quebrados)))
    return validos


def _novo_os_id(template_id: str) -> str:
    """Chave primária de UM disparo: `{template_id}-{AAAAMMDDTHHMMSS}-{6 hex}`.

    Reimplementa `os_templates.novo_os_id`, que é a definição canônica, porque
    este container não importa módulos do central. É a ÚNICA duplicação da
    task, e ela é tolerável: divergir aqui produz ids com aparência diferente —
    visível na primeira linha do log — e nunca uma OS errada, já que a unicidade
    é garantida pelo UNIQUE de `ordens.os_id`, não pelo formato.
    """
    carimbo = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{template_id}-{carimbo}-{uuid.uuid4().hex[:6].upper()}"


def _instanciar_os(template: dict, catalogo: dict) -> dict:
    """Corpo do `POST /api/v1/ordens` para um disparo do template.

    `sku` e `categoria` saem do catálogo, não do template: o SKU é o que a
    câmera do dispenser compara (`sku_esperado`), e um valor congelado no
    template envelheceria em silêncio. O item já passou por
    `_problemas_dos_templates`, então a ausência aqui é impossível — o
    `.get` cobre a corrida com um recarregamento de catálogo no meio do ciclo.
    """
    return {
        "os_id":          _novo_os_id(template["template_id"]),
        "template_id":    template["template_id"],
        "descricao":      template["descricao"],
        "categoria":      template.get("categoria", ""),
        "categoria_desc": template.get("categoria_desc", ""),
        "medicamentos": [
            {
                "medicamento": item["medicamento"],
                "sku":         catalogo.get(item["medicamento"], {}).get("sku", ""),
                "categoria":   catalogo.get(item["medicamento"], {}).get(
                                   "categoria", template.get("categoria", "")),
                "quantidade":  item["quantidade"],
            }
            for item in template["itens"]
        ],
        "criado_em":      _ts(),
        # Marca de PROVENIÊNCIA, gravada em `ordens.payload_json`. Ninguém a
        # lê hoje; ela existe para quem for auditar de onde a OS veio.
        # Acompanhou o rename: linhas antigas no banco guardam o valor
        # anterior, que é o correto — elas foram criadas pelo serviço
        # com o nome de então.
        "origem":         "ERP_SIM_v2.1",
    }


def _sortear_template(templates: list) -> dict:
    """Escolhe QUAL das ordens padrão dispara. É o único acaso que sobrou.

    Uniforme, e não ponderado por nº de itens como era a escolha de categoria
    da v1: as dez foram desenhadas com perfis diferentes de propósito (de 2 a 8
    slots), e ponderar faria justamente as curtas — as boas de demonstrar —
    aparecerem menos.
    """
    return random.choice(templates)


# ── Backpressure ───────────────────────────────────────────────────────────────

def _detalhe_fila(resp) -> str:
    """Resumo legível do corpo de um 429, para o log não virar adivinhação."""
    try:
        fila = (resp.json() or {}).get("fila", {})
        return f"({fila.get('tamanho', '?')}/{fila.get('capacidade', '?')} na fila)"
    except Exception:
        return ""

def _consultar_fila() -> dict | None:
    """Ocupação da fila do central, ou None se não deu para saber."""
    try:
        r = requests.get(CENTRAL_URL + "/api/v1/fila", timeout=5)
        if r.status_code < 300:
            return r.json()
        logger.warning("[FILA] Central respondeu %d ao consultar a fila.", r.status_code)
    except Exception as exc:
        logger.warning("[FILA] Não foi possível consultar a fila: %s", exc)
    return None


def _esperar_vaga_na_fila() -> bool:
    """Espera a fila abrir vaga. True se há espaço, False se desistiu do ciclo.

    Uma OS leva mais que os 90s do intervalo de geração (90 a 140s medidos na
    célula de 6 slots; a de 8 leva mais): sem esta checagem, o excesso ia
    bater no 429 do central toda vez. Pior sob trava do
    Triple Check, quando o orquestrador para por tempo indeterminado.

    Duas decisões deliberadas:

      * fila indisponível (central reiniciando, endpoint fora) devolve True —
        seguir e deixar o POST decidir é melhor do que travar a geração por
        causa de uma consulta auxiliar;
      * a espera é limitada a `MAX_ESPERAS_FILA` ciclos de `ESPERA_FILA_CHEIA`
        segundos. Vencido o teto, desiste DESTE ciclo e volta ao laço normal —
        nada de `sleep` apertado e nada de sair do processo.
    """
    for tentativa in range(1, MAX_ESPERAS_FILA + 1):
        fila = _consultar_fila()
        if fila is None:
            return True
        if not fila.get("cheia"):
            return True
        logger.info(
            "[FILA] Cheia (%s/%s)%s — aguardando %ds (%d/%d).",
            fila.get("tamanho", "?"), fila.get("capacidade", "?"),
            " com TRAVA ATIVA" if fila.get("trava_ativa") else "",
            ESPERA_FILA_CHEIA, tentativa, MAX_ESPERAS_FILA,
        )
        time.sleep(ESPERA_FILA_CHEIA)
    logger.warning("[FILA] Continua cheia após %d esperas — pulando este ciclo.",
                   MAX_ESPERAS_FILA)
    return False


# ── Pausa comandada pelo console ──────────────────────────────────────────────

def _gerador_pausado() -> bool:
    """O console pausou a emissão automática de ordens?

    O nome da função e o da rota (`/api/v1/gerador`) ficaram como estavam: rename
    de serviço não é rename de contrato, e o `_estado["gerador_pausado"]` que o
    console e o dashboard leem tem o mesmo dono do outro lado.

    Central mudo devolve False — "pode gerar". Mesma decisão de
    `_esperar_vaga_na_fila`: uma consulta auxiliar que falha não pode parar a
    planta, e o pior caso aqui é uma OS entrar durante uma pausa que o operador
    ainda pode desfazer. O contrário (parar de gerar porque o central reiniciou)
    deixaria a planta em silêncio sem ninguém ter pedido.
    """
    try:
        r = requests.get(CENTRAL_URL + "/api/v1/gerador", timeout=5)
        if r.status_code < 300:
            return bool((r.json() or {}).get("pausado"))
        logger.warning("[PAUSA] Central respondeu %d ao consultar o interruptor.",
                       r.status_code)
    except Exception as exc:
        logger.warning("[PAUSA] Não foi possível consultar o interruptor: %s", exc)
    return False


def _aguardar_retomada() -> None:
    """Bloqueia enquanto o console mantiver a pausa.

    Sem teto de espera, ao contrário do backpressure de fila cheia: fila cheia
    é a planta pedindo tempo e vale a pena tentar de novo depois; pausa é uma
    pessoa dizendo "eu assumo daqui", e desistir da espera para disparar uma OS
    seria justamente competir com ela.

    Não é `sleep` apertado — `ESPERA_PAUSA` entre consultas — e o log sai só nas
    transições, senão uma pausa de meia hora enche o log com a mesma linha.
    """
    if not _gerador_pausado():
        return
    logger.warning("[PAUSA] Emissão automática PAUSADA pelo console. "
                   "Aguardando retomada (consulta a cada %ds).", ESPERA_PAUSA)
    while _gerador_pausado():
        time.sleep(ESPERA_PAUSA)
    logger.info("[PAUSA] Emissão automática retomada pelo console.")


def _enviar_os(os_payload: dict) -> bool:
    """Envia OS ao central-computer via HTTP POST. Retorna True se aceita.

    Recusa NÃO é retentada, de propósito. O central recusa em dois casos (o
    contrato está no `responses=` do `POST /api/v1/ordens` e no README, seção
    "Contrato de entrada de uma OS"): 409 quando a OS já está registrada e 503
    quando não consegue persistir. Reenviar a mesma OS daria 409 para sempre;
    no 503, encheria o log enquanto o banco está fora. Cada disparo nasce com
    `os_id` próprio, então o próximo ciclo já é outra OS — mesmo quando repete o
    template — e aqui basta logar e devolver False, que o `main()` ignora antes
    de dormir `INTERVALO_OS`.
    """
    try:
        r = requests.post(
            CENTRAL_URL + "/api/v1/ordens",
            json=os_payload,
            timeout=15,
        )
        if r.status_code < 300:
            resp = r.json()
            logger.info("[OS] %s aceita — posição na fila: %d",
                        os_payload["os_id"], resp.get("posicao_fila", "?"))
            return True
        elif r.status_code == 409:
            logger.warning("[OS] %s já registrada no central (409). Descartada.",
                           os_payload["os_id"])
            return False
        elif r.status_code == 503:
            logger.error("[OS] %s descartada: central sem persistência (503). "
                         "A OS NÃO foi processada.", os_payload["os_id"])
            return False
        elif r.status_code == 429:
            # Fila cheia: o central nem persistiu a OS. Descartar é seguro e é
            # o que evita laço — o próximo disparo nasce com os_id novo depois
            # da espera do backpressure.
            logger.warning("[OS] %s descartada: fila do central cheia (429). %s",
                           os_payload["os_id"], _detalhe_fila(r))
            return False
        else:
            logger.error("[OS] Central rejeitou OS %s: %d — %s",
                         os_payload["os_id"], r.status_code, r.text[:200])
            return False
    except Exception as exc:
        logger.error("[OS] Falha ao enviar OS %s: %s", os_payload["os_id"], exc)
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    logger.info("Aguardando central-computer (%s)...", CENTRAL_URL)
    for i in range(40):
        try:
            r = requests.get(CENTRAL_URL + "/ping", timeout=5)
            if r.status_code < 300:
                logger.info("Central disponível.")
                break
        except Exception:
            pass
        logger.info("  central não disponível ainda (%d/40)...", i + 1)
        time.sleep(5)
    else:
        logger.error("Central não disponível após 40 tentativas. Encerrando.")
        raise SystemExit(1)

    catalogo      = _carregar_catalogo()
    templates     = _validar_templates(_carregar_templates(), catalogo)
    ultimo_reload = time.time()

    logger.info("ERP Simulator v2.1 pronto — 1 das %d ordens padrão a cada %ds "
                "| catálogo com %d medicamentos | pausável pelo console",
                len(templates), INTERVALO_OS, len(catalogo))
    for t in templates:
        logger.info("  %-14s %d slot(s) — %s",
                    t["template_id"], len(t["itens"]), t["descricao"])

    while True:
        # Recarrega catálogo e templates periodicamente: editar uma ordem padrão
        # no central passa a valer sem reiniciar este container.
        if time.time() - ultimo_reload > RELOAD_CATALOGO_MIN * 60:
            try:
                novo_catalogo = _carregar_catalogo(tentativas=3)
                templates     = _validar_templates(
                    _carregar_templates(tentativas=3), novo_catalogo)
                catalogo      = novo_catalogo
                ultimo_reload = time.time()
            except SystemExit:
                # `_validar_templates` encerra quando NENHUMA ordem sobra. Aqui
                # isso não pode derrubar o processo: a definição anterior já
                # provou ser válida e continuar com ela é melhor que parar a
                # planta por causa de uma edição errada no central.
                logger.error("Recarga trouxe ordens inválidas — mantendo as %d anteriores.",
                             len(templates))
                ultimo_reload = time.time()
            except Exception as exc:
                logger.warning("Falha ao recarregar catálogo/ordens: %s. Usando anterior.", exc)

        try:
            # A pausa do console vem ANTES da fila: enquanto o operador estiver
            # no comando, nem faz sentido perguntar se cabe mais uma OS.
            _aguardar_retomada()

            # Backpressure ANTES de instanciar: OS gerada e descartada só polui
            # log e queima os_id. Se a fila não abrir, este ciclo passa em branco.
            if not _esperar_vaga_na_fila():
                time.sleep(INTERVALO_OS)
                continue

            template   = _sortear_template(templates)
            os_payload = _instanciar_os(template, catalogo)

            meds      = os_payload["medicamentos"]
            itens_str = " | ".join(f"{m['medicamento']} x{m['quantidade']}" for m in meds)
            logger.info("[OS] %s | %s | %d item(ns)",
                        os_payload["os_id"], os_payload["descricao"], len(meds))
            logger.info("     %s", itens_str)

            _enviar_os(os_payload)

        except Exception as exc:
            logger.error("Erro ao instanciar/enviar OS: %s", exc, exc_info=True)

        time.sleep(INTERVALO_OS)


if __name__ == "__main__":
    main()
