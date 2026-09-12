"""
APSEN — Pré-voo: "está tudo de pé para apresentar?", respondido em segundos.

A pergunta é feita cinco minutos antes de a banca entrar na sala, e hoje ela
custa abrir o dashboard, o app de manutenção, o `docker compose ps` e o log de
três containers. Esta tela responde de uma vez, item a item, em verde ou
vermelho.

Duas decisões governam o arquivo inteiro:

**Item vermelho diz o que FAZER.** "mysql: falha" manda quem lê procurar o
problema sozinho, justamente quando não há tempo. Todo item carrega um campo
`acao`, e ele é a frase que resolve — `docker compose up -d mysql`, "libere a
trava no painel acima", "rode o seed de histórico". Item verde não precisa de
ação e não tem.

**A tela não pode travar por causa de um serviço morto**, que é exatamente o
caso em que ela é mais necessária. Toda sonda tem timeout curto e todas correm
em `gather`: o tempo total é o da mais lenta, não a soma. Um serviço fora
produz UM item vermelho, e os outros doze continuam respondendo.

Este módulo não importa FastAPI nem `database`
──────────────────────────────────────────────
Ele recebe o cliente HTTP e os fatos do banco já lidos; quem os busca é o
`main.py`. É a mesma separação de `console.py` (a parte testável fora do
framework) e de `seed_demo.py` (decide × persiste), e é o que permite testar
"com um serviço fora, só aquele item fica vermelho" sem subir treze containers.
"""
import asyncio
import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)

# Estados de um item. `alerta` existe porque nem tudo que não está OK está
# QUEBRADO: catálogo vazio impede a demonstração, mas modo apresentação ligado é
# só uma informação que o operador precisa ter na cabeça — pintar os dois de
# vermelho ensinaria a ignorar o vermelho.
OK, ALERTA, FALHA, INFO = "ok", "alerta", "falha", "info"

# Timeout por sonda. Curto de propósito: um serviço que leva mais de 2s para
# responder `/ping` está com problema de qualquer jeito, e a tela existe para
# ser lida em segundos.
TIMEOUT_SONDA_S = 2.0
# Teto do conjunto. As sondas correm em paralelo, então isto é folga sobre o
# TIMEOUT_SONDA_S, não a soma — serve para o caso em que o próprio `gather`
# emperra (event loop saturado).
TIMEOUT_TOTAL_S = 6.0


class Servico(NamedTuple):
    """Um serviço da stack e como perguntar a ele se está de pé."""
    nome:    str
    host:    str      # nome DNS na rede `apsen-net`
    porta:   int
    caminho: str      # `/ping` nos serviços de API; `/` nos dois Dash
    papel:   str


# Os treze serviços do compose. O caminho é o MESMO que o healthcheck do compose
# usa, e `tests/test_prevoo.py` compara esta tabela contra o `docker-compose.yml`
# — nome, porta e caminho. Sem essa comparação, acrescentar um serviço deixaria
# o pré-voo dizendo "tudo verde" sobre uma stack que ele não conhece inteira,
# que é o pior modo possível de falhar para uma tela de conferência.
SERVICOS: tuple[Servico, ...] = (
    Servico("central-computer",    "central-computer",    8000, "/ping",
            "Orquestra a planta e serve este console."),
    Servico("dispenser-adapter",   "dispenser-adapter",   8100, "/ping",
            "Ponte para os dispensers."),
    Servico("cnc-adapter",         "cnc-adapter",         8101, "/ping",
            "Ponte para a mesa CNC."),
    Servico("vision-adapter",      "vision-adapter",      8102, "/ping",
            "Ponte para as três câmeras."),
    Servico("weight-adapter",      "weight-adapter",      8103, "/ping",
            "Ponte para a balança HX711."),
    Servico("cnc-simulator",       "cnc-simulator",       8200, "/ping",
            "Mesa CNC simulada."),
    Servico("dispenser-simulator", "dispenser-simulator", 8201, "/ping",
            "Os oito dispensers simulados."),
    Servico("vision-simulator",    "vision-simulator",    8202, "/ping",
            "As três câmeras simuladas."),
    Servico("weight-simulator",    "weight-simulator",    8203, "/ping",
            "Célula de carga simulada."),
    Servico("dashboard",           "dashboard",           8050, "/",
            "Painel de acompanhamento (Dash)."),
    Servico("manut_web",           "manut_web",           8051, "/",
            "App de manutenção (Dash)."),
)

# O central responde por dentro (ver `_item_central`) e o MySQL não fala HTTP —
# os dois entram no relatório por caminhos próprios. O erp-simulator não tem
# porta NENHUMA: worker de laço, sem API, e é a exceção que o
# `tests/test_compose.py` já registra por não ter healthcheck. Aqui ele aparece
# como item informativo, e não some: sumir seria a tela dizer "12 de 12" sobre
# uma stack de 13.
NOME_CENTRAL   = "central-computer"
NOME_MYSQL     = "mysql"
NOME_GERADOR   = "erp-simulator"

# Serviços que a sonda HTTP visita: todos menos o central, que se responde por
# dentro.
SONDAVEIS = tuple(s for s in SERVICOS if s.nome != NOME_CENTRAL)


def _item(id_: str, grupo: str, titulo: str, estado: str,
          detalhe: str = "", acao: str = "") -> dict:
    return {"id": id_, "grupo": grupo, "titulo": titulo, "estado": estado,
            "detalhe": detalhe, "acao": acao}


# ── Serviços ──────────────────────────────────────────────────────────────────

async def _sondar(cliente, servico: Servico, timeout: float) -> dict:
    """Uma sonda. NUNCA levanta — exceção aqui derrubaria o `gather` inteiro."""
    url = f"http://{servico.host}:{servico.porta}{servico.caminho}"
    subir = f"docker compose up -d {servico.nome}"
    try:
        resposta = await cliente.get(url, timeout=timeout)
    except Exception as exc:
        # Timeout e recusa de conexão pedem AÇÕES diferentes, e distingui-los é
        # metade do valor desta tela: "não subiu" resolve-se com `up -d`;
        # "subiu e não responde" só se resolve olhando o log. O httpx levanta
        # `TimeoutException`, e este módulo não o importa de propósito (ele não
        # depende de framework nenhum), então a separação é pelo NOME da classe.
        if "timeout" in type(exc).__name__.lower():
            return _item(servico.nome, "Serviços", servico.nome, FALHA,
                         f"Não respondeu em {timeout:.0f}s ({servico.papel})",
                         f"O processo está de pé mas travado, ou o container "
                         f"reinicia em laço. Veja "
                         f"`docker compose logs --tail=50 {servico.nome}`.")
        return _item(servico.nome, "Serviços", servico.nome, FALHA,
                     f"{type(exc).__name__}: {exc} ({servico.papel})",
                     f"Suba o serviço: `{subir}`")

    codigo = getattr(resposta, "status_code", 0)
    if codigo >= 300:
        return _item(servico.nome, "Serviços", servico.nome, FALHA,
                     f"HTTP {codigo} em {servico.caminho} ({servico.papel})",
                     f"Veja `docker compose logs --tail=50 {servico.nome}`.")
    return _item(servico.nome, "Serviços", servico.nome, OK,
                 f"porta {servico.porta} respondendo ({servico.papel})")


async def sondar_servicos(cliente, timeout: float = TIMEOUT_SONDA_S) -> list[dict]:
    """Todas as sondas em paralelo. O tempo total é o da mais lenta.

    `return_exceptions=True` é o cinto sobre o suspensório: `_sondar` já não
    levanta, mas uma exceção que escapasse levaria junto o resultado de todas as
    outras — e a tela apareceria em branco justamente no dia em que um serviço
    quebrou de um jeito novo.
    """
    resultados = await asyncio.gather(
        *[_sondar(cliente, s, timeout) for s in SONDAVEIS],
        return_exceptions=True,
    )
    itens: list[dict] = []
    for servico, resultado in zip(SONDAVEIS, resultados):
        if isinstance(resultado, BaseException):
            logger.warning("[PREVOO] sonda de %s falhou: %s", servico.nome, resultado)
            itens.append(_item(servico.nome, "Serviços", servico.nome, FALHA,
                               f"Sonda falhou: {resultado}",
                               f"Veja `docker compose logs --tail=50 {servico.nome}`."))
        else:
            itens.append(resultado)
    return itens


def item_central() -> dict:
    """O central responde por dentro, e não por uma sonda HTTP a si mesmo.

    Um auto-GET testaria sobretudo o event loop que acabou de servir ESTA
    requisição, e um timeout ali produziria o relatório absurdo "o central está
    fora" numa página que o central acabou de entregar.
    """
    return _item(NOME_CENTRAL, "Serviços", NOME_CENTRAL, OK,
                 "respondendo — é ele quem está servindo esta página")


def item_gerador(pausado: bool) -> dict:
    """O erp-simulator não tem porta: não há como sondá-lo.

    Ele aparece assim mesmo, como informação, porque sumir faria a tela dizer
    "12 de 12" sobre uma stack de 13 — e o gerador pausado é justamente uma das
    causas de "a planta não está fazendo nada" numa apresentação.
    """
    if pausado:
        return _item(NOME_GERADOR, "Serviços", NOME_GERADOR, ALERTA,
                     "PAUSADO pelo console — nenhuma OS entra sozinha",
                     "Retome no botão do topo, ou dispare as ordens à mão.")
    return _item(NOME_GERADOR, "Serviços", NOME_GERADOR, INFO,
                 "ativo (sem porta: worker de laço, não dá para sondar por HTTP)")


# ── Banco ─────────────────────────────────────────────────────────────────────

def itens_banco(diagnostico: dict | None) -> list[dict]:
    """Itens de MySQL, schema, catálogo e slots a partir dos fatos já lidos.

    `diagnostico` é `None` quando o banco não respondeu — e aí é UM item
    vermelho, não quatro: com o MySQL fora, dizer também "schema incompleto" e
    "catálogo vazio" seria três diagnósticos derivados de uma causa só, e quem
    lê acabaria caçando três problemas que não existem.
    """
    if diagnostico is None:
        return [_item("mysql", "Banco", "MySQL", FALHA,
                      "Não respondeu — schema, catálogo e slots não puderam ser "
                      "verificados",
                      "Suba o banco: `docker compose up -d mysql` e espere o "
                      "healthcheck. Depois reinicie o central "
                      "(`docker compose restart central-computer`), que é quem "
                      "cria o schema.")]

    itens = [_item("mysql", "Banco", "MySQL", OK,
                   f"conectado ({diagnostico.get('versao', 'versão desconhecida')})")]

    faltando = diagnostico.get("schema_faltando") or []
    if faltando:
        itens.append(_item("schema", "Banco", "Schema", FALHA,
                           "Faltando: " + ", ".join(faltando),
                           "Reinicie o central (`docker compose restart "
                           "central-computer`): `init_db` cria o que falta em "
                           "todo startup."))
    else:
        itens.append(_item("schema", "Banco", "Schema", OK,
                           f"{diagnostico.get('tabelas', 0)} tabelas conferidas"))

    meds = diagnostico.get("medicamentos", 0)
    if meds:
        itens.append(_item("catalogo", "Banco", "Catálogo de medicamentos", OK,
                           f"{meds} medicamentos"))
    else:
        itens.append(_item("catalogo", "Banco", "Catálogo de medicamentos", FALHA,
                           "vazio — as OS sairiam sem SKU para a câmera comparar",
                           "Reinicie o central: `_seed_medicamentos` popula o "
                           "catálogo no startup."))

    esperados = diagnostico.get("slots_esperados", 0)
    presentes = diagnostico.get("slots_presentes", 0)
    if presentes == esperados and esperados:
        itens.append(_item("slots_banco", "Banco", "Slots em `dispenser_estado`",
                           OK, f"{presentes} de {esperados}"))
    else:
        itens.append(_item("slots_banco", "Banco", "Slots em `dispenser_estado`",
                           FALHA, f"{presentes} de {esperados}",
                           "Reinicie o central: `_seed_dispenser_estado` insere "
                           "só os slots que faltam, sem tocar nos existentes."))
    return itens


# ── Ordens padrão ─────────────────────────────────────────────────────────────

def item_templates(problemas: list | None, total: int,
                   catalogo_carregado: bool) -> dict:
    """As dez ordens contra o catálogo real.

    `problemas` vazio com o catálogo FORA não significa "válido" — só as
    checagens estruturais rodaram. É a mesma ressalva que
    `GET /api/v1/ordens/templates` já faz com os dois campos separados, e
    apagá-la aqui faria a tela dar um verde que não foi verificado.
    """
    if not catalogo_carregado:
        return _item("templates", "Ordens", "10 ordens padrão", ALERTA,
                     "Só a estrutura foi verificada — sem catálogo não dá para "
                     "conferir se os medicamentos existem",
                     "Resolva o item do banco acima e recarregue esta tela.")
    if problemas:
        return _item("templates", "Ordens", "10 ordens padrão", FALHA,
                     f"{len(problemas)} problema(s): " + "; ".join(problemas[:3]),
                     "Corrija `central-computer/os_templates.py` — o gerador "
                     "DESCARTA template inválido, então a ordem quebrada não "
                     "seria disparada.")
    return _item("templates", "Ordens", "10 ordens padrão", OK,
                 f"{total} ordens válidas contra o catálogo")


def item_fila(tamanho: int, capacidade: int) -> dict:
    if capacidade and tamanho >= capacidade:
        return _item("fila", "Ordens", "Fila de OS", ALERTA,
                     f"{tamanho}/{capacidade} — CHEIA, o próximo disparo é "
                     f"recusado com 429",
                     "Espere a fila drenar, ou use o reset da planta para "
                     "esvaziá-la (ele cancela as OS que esperam).")
    return _item("fila", "Ordens", "Fila de OS", OK,
                 f"{tamanho}/{capacidade} esperando")


# ── Célula ────────────────────────────────────────────────────────────────────

def itens_celula(snapshot: dict, home: tuple, tolerancia_mm: float = 1.0) -> list[dict]:
    """Trava e posição da CNC — os dois estados que impedem começar do zero."""
    trava = snapshot.get("trava") or {}
    if trava.get("ativa"):
        itens = [_item("trava", "Célula", "Trava do Triple Check", FALHA,
                       f"ATIVA na OS {trava.get('os_id', '?')}, slot "
                       f"D{trava.get('slot_id', '?')}: {trava.get('motivo', '')}",
                       "Confira o slot na bancada e libere a trava no botão do "
                       "topo — enquanto ela estiver ativa, NENHUMA OS anda.")]
    else:
        itens = [_item("trava", "Célula", "Trava do Triple Check", OK,
                       "sem trava — a fila pode andar")]

    cnc = snapshot.get("cnc") or {}
    x, y = cnc.get("posicao_x"), cnc.get("posicao_y")
    if x is None or y is None:
        itens.append(_item("cnc", "Célula", "Posição da CNC", ALERTA,
                           "posição desconhecida — nenhum evento da CNC chegou "
                           "ainda",
                           "Dispare uma OS ou reinicie o cnc-simulator; a "
                           "posição aparece no primeiro evento."))
    elif abs(x - home[0]) <= tolerancia_mm and abs(y - home[1]) <= tolerancia_mm:
        itens.append(_item("cnc", "Célula", "Posição da CNC", OK,
                           f"em HOME ({x:.0f}, {y:.0f})"))
    else:
        itens.append(_item("cnc", "Célula", "Posição da CNC", ALERTA,
                           f"em ({x:.0f}, {y:.0f}), fora de HOME "
                           f"({home[0]:.0f}, {home[1]:.0f})",
                           "Normal se uma OS acabou de rodar. Para começar do "
                           "zero, use o reset da planta — ele manda a CNC para "
                           "HOME."))

    ativa = snapshot.get("os_ativa")
    if ativa:
        itens.append(_item("os_ativa", "Célula", "OS em execução", ALERTA,
                           f"{ativa.get('os_id', '?')} rodando agora",
                           "Espere ela fechar antes de resetar — o reset recusa "
                           "enquanto houver OS em execução."))
    else:
        itens.append(_item("os_ativa", "Célula", "OS em execução", OK,
                           "célula ociosa"))

    alarmes = snapshot.get("alarmes_ativos") or 0
    if alarmes:
        itens.append(_item("alarmes", "Célula", "Alarmes abertos", ALERTA,
                           f"{alarmes} em aberto",
                           "Resolva no app de manutenção (:8051) ou deixe como "
                           "está: alarme aberto não bloqueia a planta."))
    else:
        itens.append(_item("alarmes", "Célula", "Alarmes abertos", OK, "nenhum"))
    return itens


# ── Modo de demonstração ──────────────────────────────────────────────────────

def itens_modo(modo_apresentacao: bool, fator_velocidade: float,
               falha_armada: dict | None, clientes_ws: int) -> list[dict]:
    """O que está VALENDO agora. Informação, não conferência.

    Nenhum destes é erro — são as três coisas que, esquecidas, fazem alguém
    explicar a planta errada: modo apresentação ligado passa por "o hardware
    nunca falha", fator de velocidade diferente de 1 confunde quem cronometra, e
    uma falha armada esquecida dispara no meio de outra explicação.
    """
    itens = [
        _item("modo", "Modo", "Modo de operação",
              ALERTA if modo_apresentacao else INFO,
              "APRESENTAÇÃO — nenhuma falha aleatória será emitida"
              if modo_apresentacao else
              "realista — as probabilidades de falha estão valendo",
              "Se a ideia era mostrar o tratamento de erro, arme uma falha no "
              "painel de injeção: ela funciona com o modo ligado."
              if modo_apresentacao else ""),
        _item("velocidade", "Modo", "Fator de velocidade",
              INFO if fator_velocidade == 1.0 else ALERTA,
              f"{fator_velocidade:.2f}×"
              + ("" if fator_velocidade == 1.0 else
                 " — os tempos simulados e os timeouts estão escalados"),
              "" if fator_velocidade == 1.0 else
              "Defina FATOR_VELOCIDADE=1.0 no `.env` e recrie a stack para "
              "voltar ao tempo real."),
    ]

    if falha_armada:
        itens.append(_item("injecao", "Modo", "Falha armada", ALERTA,
                           f"{falha_armada.get('tipo')} no slot "
                           f"D{falha_armada.get('slot_id')} — dispara na próxima "
                           f"ocorrência aplicável",
                           "Se não for isso que você quer mostrar agora, "
                           "desarme no painel de injeção."))
    else:
        itens.append(_item("injecao", "Modo", "Falha armada", OK,
                           "nenhuma — a planta roda pelo comportamento normal"))

    itens.append(_item("websocket", "Modo", "Clientes WebSocket", INFO,
                       f"{clientes_ws} conectado(s) — dashboard, console e app "
                       f"de manutenção contam um cada"))
    return itens


# ── Resumo ────────────────────────────────────────────────────────────────────

def resumo(itens: list[dict]) -> dict:
    """Contagem por estado + o veredito que a página mostra em cima.

    `pronto` é FALSO com qualquer falha, e só com falha: alerta é informação que
    o operador precisa ter, não impedimento. Misturar os dois faria a tela
    responder "não" a quem só ligou o modo apresentação de propósito — e uma
    tela que responde "não" sempre deixa de ser lida.
    """
    contagem = {OK: 0, ALERTA: 0, FALHA: 0, INFO: 0}
    for item in itens:
        contagem[item["estado"]] = contagem.get(item["estado"], 0) + 1
    return {
        "pronto":   contagem[FALHA] == 0,
        "contagem": contagem,
        "total":    len(itens),
    }
