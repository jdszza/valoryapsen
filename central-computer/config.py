import logging
import os
from dataclasses import dataclass, field

_cfg_logger = logging.getLogger(__name__)

_DEFAULT_SECRET_KEY = "apsen-mude-esta-chave-em-producao-2024"
_TAMANHO_MINIMO_SECRET = 32


class ConfiguracaoInsegura(RuntimeError):
    """Configuração que não pode ir para produção — o central recusa subir."""


def validar_secret_key(chave: str, ambiente: str) -> None:
    """Recusa subir com segredo default, vazio ou curto demais.

    O valor default está VERSIONADO neste repositório: quem o tem consegue
    forjar um JWT com `role=admin`, liberar a trava do Triple Check e mexer em
    usuários. Um warning no startup não resolve isso — em produção ninguém lê
    log de boot, e o sistema segue funcionando como se estivesse protegido.

    `APSEN_ENV=dev` mantém o boot permissivo (com warning) para quem só quer
    subir a stack de simulação. Qualquer outro valor — inclusive o default,
    `prod` — trata segredo fraco como erro de configuração.
    """
    problema = None
    if not chave or chave == _DEFAULT_SECRET_KEY:
        problema = "SECRET_KEY está com o valor default, que é público no repositório"
    elif len(chave) < _TAMANHO_MINIMO_SECRET:
        problema = (f"SECRET_KEY tem {len(chave)} caracteres — "
                    f"mínimo {_TAMANHO_MINIMO_SECRET}")

    if not problema:
        return

    receita = 'python -c "import secrets; print(secrets.token_hex(32))"'
    if ambiente == "dev":
        _cfg_logger.warning(
            "⚠️  %s. Tolerado porque APSEN_ENV=dev — NÃO suba assim em produção. "
            "Gere uma chave com: %s", problema, receita,
        )
        return
    raise ConfiguracaoInsegura(
        f"{problema}. Defina SECRET_KEY no .env (gere com: {receita}) "
        f"ou rode com APSEN_ENV=dev se isto for um ambiente de simulação."
    )


def _origens_cors() -> list[str]:
    """Origens de browser autorizadas a chamar o central.

    Só o dashboard e o app de manutenção falam com o central pelo navegador.
    `*` num serviço autenticado deixa qualquer página aberta no mesmo browser
    do técnico disparar requisições em nome dele.
    """
    bruto = os.getenv("CORS_ORIGINS", "http://localhost:8050,http://localhost:8051")
    return [origem.strip() for origem in bruto.split(",") if origem.strip()]


def _limiar_triple_check() -> int:
    """Nº de fontes divergentes que ativa a trava. Faixa válida: 1..3.

    O default é 1 — a regra conservadora. Este é um sistema farmacêutico de
    contagem: um falso negativo é medicamento errado, ou na quantidade errada,
    chegando ao paciente. Só suba o limiar com evidência de que uma fonte
    específica gera trava sem causa real.

    Valor fora da faixa cai no default em vez de virar comportamento silencioso:
    0 travaria toda dispensa e >3 nunca travaria — os dois esvaziam a trava de
    sentido, um por excesso e o outro por ausência.
    """
    bruto = os.getenv("TRIPLE_CHECK_MIN_DIVERGENCIAS", "1")
    try:
        valor = int(bruto)
    except ValueError:
        valor = 0
    if not 1 <= valor <= 3:
        _cfg_logger.warning(
            "TRIPLE_CHECK_MIN_DIVERGENCIAS=%r fora da faixa 1..3 — usando 1.", bruto
        )
        return 1
    return valor


def _max_fila_os() -> int:
    """Quantas OS podem ESPERAR na fila do orquestrador. Faixa válida: 1..1000.

    O gerador posta uma OS a cada `INTERVALO_OS` (90s por padrão) e uma OS de 6
    slots leva de 90 a 140s — o sistema recebe mais rápido do que processa. Com
    a trava do Triple Check ativa, o loop único para por tempo indeterminado
    esperando o supervisor, e aí a fila cresce sem teto: memória do processo e
    linhas "aguardando" no banco. O limite transforma isso numa recusa
    explícita (429), que o gerador sabe tratar.

    O default de 5 é ~7 minutos de trabalho enfileirado: absorve rajada sem
    esconder que a planta está mais lenta que a demanda.
    """
    bruto = os.getenv("MAX_FILA_OS", "5")
    try:
        valor = int(bruto)
    except ValueError:
        valor = 0
    if not 1 <= valor <= 1000:
        _cfg_logger.warning("MAX_FILA_OS=%r fora da faixa 1..1000 — usando 5.", bruto)
        return 5
    return valor


def _num_slots() -> int:
    """Quantos dispensers a célula tem. Faixa válida: 2..64, e PAR.

    O arranjo físico é de DUAS FILEIRAS frente a frente, com a mesa CNC
    percorrendo o corredor entre elas (ver `orchestrator.POSICOES`). Um número
    ímpar deixaria uma fileira mais longa que a outra — geometria que o resto
    do código não modela — então ímpar cai no default em vez de virar uma
    fileira torta silenciosa.

    O valor precisa ser o MESMO em todos os serviços: o central deriva dele o
    mapa de posições e a validação de slot, e cada simulador valida a faixa que
    aceita. É por isso que ele mora numa env var comum (`NUM_SLOTS`), declarada
    uma vez no compose, e não numa constante por arquivo.
    """
    bruto = os.getenv("NUM_SLOTS", "8")
    try:
        valor = int(bruto)
    except ValueError:
        valor = 0
    if not 2 <= valor <= 64 or valor % 2 != 0:
        _cfg_logger.warning("NUM_SLOTS=%r inválido (par, 2..64) — usando 8.", bruto)
        return 8
    return valor


def _mysql_pool_max() -> int:
    """Quantas conexões MySQL o central guarda abertas. Faixa válida: 1..64.

    Antes não havia pool: cada operação abria TCP + handshake + autenticação e
    fechava. Um evento de adapter faz de 1 a 3 escritas, e a telemetria dos
    slots sozinha são `NUM_SLOTS` gravações a cada 15s — a maior latência
    evitável do central estava aí.

    O teto vale por dois lados. Pequeno demais, as threads do `to_thread` se
    atropelam e a conexão extra é aberta do jeito antigo; grande demais,
    o central sozinho encosta no `max_connections` do MySQL (151 por padrão) e
    o erro que aparece — 1040, "too many connections" — é justamente o que o
    `init_db` classifica como transitório, ou seja, 30 retries no boot
    seguinte. 8 cobre o paralelismo real (`asyncio.to_thread` + as rotas
    síncronas do FastAPI) com folga.
    """
    bruto = os.getenv("MYSQL_POOL_MAX", "8")
    try:
        valor = int(bruto)
    except ValueError:
        valor = 0
    if not 1 <= valor <= 64:
        _cfg_logger.warning("MYSQL_POOL_MAX=%r fora da faixa 1..64 — usando 8.", bruto)
        return 8
    return valor


# ── Controles globais de demonstração ─────────────────────────────────────────
# As duas funções abaixo existem IDÊNTICAS aqui e nos quatro simuladores. A
# duplicação é deliberada: simulador não importa módulo do central (imagens
# separadas, mesmo motivo de `os_templates.novo_os_id` × `simulator._novo_os_id`
# — ver CLAUDE.md). O que a torna segura é `tests/test_modo_apresentacao.py`,
# que compara as cinco cópias entre si tabela de entradas por tabela de
# entradas: "liguei o modo apresentação" valendo em quatro serviços e não no
# quinto produz exatamente a surpresa que o modo existe para eliminar.

def _modo_apresentacao() -> bool:
    """Modo demonstração: o acaso é desligado, o sistema roda determinístico.

    Aceita as grafias que alguém digita no `.env` sem pensar ("1", "true",
    "sim", "on"); qualquer outra coisa é falso. Nunca levanta: um valor
    estranho aqui não pode impedir o central de subir.

    O central não sorteia falha nenhuma — quem sorteia são os simuladores. Ele
    lê a variável para publicá-la (`/api/v1/estado`, log de boot, tela de
    pré-voo): operar a planta sem saber qual modo está em vigor é o que faz
    alguém confundir demo sem imprevisto com hardware perfeito.
    """
    return os.getenv("MODO_APRESENTACAO", "0").strip().lower() in (
        "1", "true", "yes", "sim", "on",
    )


def _fator_velocidade() -> float:
    """Multiplicador de TODOS os tempos simulados. Faixa válida: 0.1..10.

    0.5 roda a demo no dobro da velocidade; 2.0, na metade, para explicar cada
    etapa com calma. Fora da faixa cai em 1.0 com warning, em vez de virar
    comportamento silencioso: 0 faria toda etapa terminar instantaneamente
    (sem nada para mostrar) e um valor enorme travaria a apresentação inteira
    em cima do primeiro slot.
    """
    bruto = os.getenv("FATOR_VELOCIDADE", "1.0")
    try:
        valor = float(bruto)
    except ValueError:
        valor = 0.0
    if not 0.1 <= valor <= 10.0:
        _cfg_logger.warning(
            "FATOR_VELOCIDADE=%r fora da faixa 0.1..10 — usando 1.0.", bruto)
        return 1.0
    return valor


def _cronograma_s(nome: str, default: float, minimo: float, maximo: float) -> float:
    """Um dos quatro números do relógio do ciclo, já com a folga aplicada.

    Eles eram os ÚNICOS deste arquivo lidos com `float(os.getenv(...))` cru —
    todos os outros passam por um leitor com faixa, warning e queda no default.
    E o comentário ao lado deles diz, com todas as letras, que um cronograma
    errado é a única forma de esta feature derrubar comprimido no chão.

    Os dois modos de falhar eram silenciosos do jeito errado:

    - **texto não numérico** — `2,5` com vírgula, que é como se digita em
      pt-BR, ou a variável vazia. O `float()` levantava `ValueError` na
      avaliação do `dataclass`, ou seja, no import: o central não subia e o
      traceback apontava para `config.py`, não para o `.env` de quem digitou;
    - **zero ou negativo** — `cronograma_do_ciclo` devolvia prazo ≤ 0 e
      `_dormir_ou_cancelar` retornava NA HORA dizendo que o prazo foi cumprido
      (`asyncio.wait(timeout=-1)` acorda imediatamente com `feitos` vazio, que
      é exatamente o sinal de "venceu"). O `dispensar` saía com a mesa ainda
      andando.

    O teto existe pelo motivo oposto e é generoso: um número absurdamente alto
    não derruba nada, só faz cada ciclo esperar minutos — mas aí a demonstração
    parece travada e ninguém sabe por quê.
    """
    bruto = os.getenv(nome, str(default))
    try:
        valor = float(bruto)
    except (TypeError, ValueError):
        _cfg_logger.warning(
            "%s=%r não é número — usando %s. (Decimal com PONTO: 2.5, não 2,5.)",
            nome, bruto, default,
        )
        valor = default
    if not minimo <= valor <= maximo:
        _cfg_logger.warning(
            "%s=%r fora da faixa %s..%s — usando %s.", nome, bruto, minimo, maximo, default
        )
        valor = default
    return valor * _folga_timeout()


def _folga_timeout() -> float:
    """Fator aplicado aos TIMEOUT_* do orquestrador. Nunca menor que 1.0.

    Este é o ponto em que a task de modo apresentação mais tinha como dar
    errado: desacelerar a célula sem desacelerar o teto de espera faz a OS
    abortar por `timeout_carregamento` no meio da demonstração — e o log
    culparia o dispenser, que estava fazendo exatamente o que se pediu.

    O `max(1.0, ...)` não é conservadorismo distraído, é a assimetria real dos
    dois lados:

    - **Acelerar** (fator < 1) não ganha nada com timeout menor. O timeout é
      teto de espera por um adapter travado, não parte do ciclo: encolhê-lo não
      torna a demo mais rápida em um segundo sequer. O que ele encolhe junto é a
      folga para o custo FIXO, que não escala com fator nenhum — `_post`
      retentando 3× com `sleep(1)` chega a ~32 s por comando, e o MySQL e a rede
      levam o que levam. Com `TIMEOUT_PESO` (15 s) a 0.1, o teto viraria 1,5 s e
      a OS abortaria com a planta inteira saudável.
    - **Desacelerar** (fator > 1) precisa do aumento, e é aí que ele vem.
    """
    return max(1.0, _fator_velocidade())


def _console_senha() -> str:
    """Senha do console de operação. Vazia = console DESABILITADO.

    Independente do JWT de propósito: o console é a mesa de operação de quem
    apresenta a planta, não mais um técnico do app de manutenção. Dar-lhe um
    usuário no banco criaria uma conta com poder de disparar OS que ninguém
    lembraria de desativar; exigir um JWT de admin o acorrentaria ao app que
    ele existe para não precisar abrir.

    Não existe default embutido, e a ausência não vira senha fraca: sem a
    variável o console inteiro some (503 em toda rota `/console*`). Uma senha
    padrão versionada aqui seria pior que não ter console nenhum — ela abriria
    o disparo de OS para quem lesse o repositório.
    """
    return os.getenv("CONSOLE_SENHA", "").strip()


def _console_sessao_horas() -> float:
    """Validade do cookie de sessão do console. Faixa 0.25..24 h.

    Oito horas, como o JWT: é um turno. Fora da faixa cai no default — 0
    deslogaria a cada clique e 720 faria um notebook esquecido aberto virar
    acesso permanente ao disparo de OS.
    """
    bruto = os.getenv("CONSOLE_SESSAO_HORAS", "8")
    try:
        valor = float(bruto)
    except ValueError:
        valor = 0.0
    if not 0.25 <= valor <= 24:
        _cfg_logger.warning(
            "CONSOLE_SESSAO_HORAS=%r fora da faixa 0.25..24 — usando 8.", bruto)
        return 8.0
    return valor


@dataclass
class Settings:
    # ── MySQL ─────────────────────────────────────────────────────────────────
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "mysql")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_DB:   str = os.getenv("MYSQL_DB",   "apsen_db")
    MYSQL_USER: str = os.getenv("MYSQL_USER", "apsen")
    MYSQL_PASS: str = os.getenv("MYSQL_PASS", "apsen_pass_2024")
    # Conexões reaproveitadas em vez de uma nova por operação. Ver `_conn` em
    # database.py.
    MYSQL_POOL_MAX: int = field(default_factory=_mysql_pool_max)

    # ── Ambiente ──────────────────────────────────────────────────────────────
    # "dev" afrouxa a validação de segredo (ver `validar_secret_key`). Qualquer
    # outro valor é tratado como produção.
    APSEN_ENV: str = os.getenv("APSEN_ENV", "prod").strip().lower()

    # ── JWT ───────────────────────────────────────────────────────────────────
    # OBRIGATÓRIO em produção: defina SECRET_KEY no ambiente com ≥32 caracteres aleatórios.
    # Gere com: python -c "import secrets; print(secrets.token_hex(32))"
    # O central RECUSA subir com o valor default fora de APSEN_ENV=dev.
    SECRET_KEY: str = os.getenv("SECRET_KEY", _DEFAULT_SECRET_KEY)
    # Janela em que a revalidação de usuário fica em cache. Curta de propósito:
    # é o atraso máximo entre desativar um técnico e ele perder o acesso.
    AUTH_CACHE_TTL_S: float = float(os.getenv("AUTH_CACHE_TTL_S", "30"))

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: list = field(default_factory=_origens_cors)

    # ── Seed de usuários (lidos apenas na primeira inicialização do DB) ────────
    # Altere via env vars antes do primeiro `docker compose up`.
    SEED_ADMIN_SENHA: str  = os.getenv("SEED_ADMIN_SENHA",  "Apsen@Admin#2024!")
    SEED_MANUT_SENHA: str  = os.getenv("SEED_MANUT_SENHA",  "Apsen@Manut#2024!")

    # ── Adapter URLs ──────────────────────────────────────────────────────────
    DISPENSER_ADAPTER_URL: str = os.getenv("DISPENSER_ADAPTER_URL", "http://dispenser-adapter:8100")
    CNC_ADAPTER_URL:       str = os.getenv("CNC_ADAPTER_URL",       "http://cnc-adapter:8101")
    VISION_ADAPTER_URL:    str = os.getenv("VISION_ADAPTER_URL",    "http://vision-adapter:8102")
    WEIGHT_ADAPTER_URL:    str = os.getenv("WEIGHT_ADAPTER_URL",    "http://weight-adapter:8103")

    # ── Timeouts de orquestração (segundos) ───────────────────────────────────
    # Todos escalados por `_folga_timeout()`: com a célula desacelerada para
    # narrar a demo, um teto fixo abortaria a OS por timeout de um passo que
    # está apenas demorando o que se pediu. Ver a docstring de `_folga_timeout`
    # para por que o fator só aumenta, nunca reduz.
    TIMEOUT_CARREGAMENTO:        float = float(os.getenv("TIMEOUT_CARREGAMENTO",        "180")) * _folga_timeout()
    # Estes dois NÃO governam mais o ciclo da mesa — o ciclo agora é por
    # RELÓGIO (ver `cronograma_do_ciclo` no orquestrador), e o evento da placa
    # registra ou cancela, nunca autoriza o passo seguinte.
    #
    # Eles continuam existindo, e continuam configuráveis, pelo que ainda
    # governam: `TIMEOUT_POSICIONAMENTO` é o teto do HOMING (passos 4-inicial e
    # 5), que segue sendo handshake — o homing não tem duração previsível,
    # porque depende de quão longe do fim de curso a mesa estava, e pode levar
    # `HOMING_TIMEOUT_MS` por eixo. `TIMEOUT_DISPENSA` é o teto de espera do
    # `limpeza_ok` e dos comandos que ainda confirmam.
    #
    # Deixá-los aqui com o comentário trocado é deliberado: um timeout que
    # ninguém usa mas continua no `.env` é uma alavanca que o operador vai
    # girar esperando efeito, e depois vai procurar o problema em outro lugar.
    TIMEOUT_POSICIONAMENTO:      float = float(os.getenv("TIMEOUT_POSICIONAMENTO",      "120")) * _folga_timeout()
    TIMEOUT_DISPENSA:            float = float(os.getenv("TIMEOUT_DISPENSA",            "120")) * _folga_timeout()
    TIMEOUT_VISAO_DISPENSER:     float = float(os.getenv("TIMEOUT_VISAO_DISPENSER",     "30"))  * _folga_timeout()
    TIMEOUT_VISAO_MESA:          float = float(os.getenv("TIMEOUT_VISAO_MESA",          "30"))  * _folga_timeout()
    TIMEOUT_PESO:                float = float(os.getenv("TIMEOUT_PESO",                "15"))  * _folga_timeout()
    TIMEOUT_LIMPEZA:             float = float(os.getenv("TIMEOUT_LIMPEZA",             "60"))  * _folga_timeout()

    # ── O cronograma do ciclo da mesa (segundos) ──────────────────────────────
    #
    # O ciclo `mover` → `dispensar` deixou de esperar confirmação: o central
    # calcula QUANDO cada peça acontece e dispara no relógio. Estes quatro
    # números são esse relógio, e escalam por `_folga_timeout()` como os
    # TIMEOUT_* — desacelerar a demo sem desacelerar o cronograma faria o
    # `dispensar` sair com a mesa ainda em trânsito, que é a única forma de esta
    # feature derrubar comprimido no chão.
    #
    # `CNC_TETO_TRAJETO_S` é um TETO de QUALQUER deslocamento, e a conta que o
    # justifica é a da mesa montada:
    #
    #     FEED 750 mm/min = 12,5 mm/s = 1000 passos/s (STEPS_PER_MM = 80)
    #     CoreXY: max_p = max(|dx+dy|, |dx−dy|) × 80
    #     pior trajeto da célula = HOME(0,0) → D8(7,18)
    #         max_p = max(|7+18|, |7−18|) × 80 = 25 × 80 = 2000 passos
    #         ≈ 2016 ms somando o meio-período real de cada pulso da rampa
    #
    # 2,5 s é isso mais folga. **Ele precisa ser MAIOR que o trajeto real, não
    # IGUAL a ele** — e é essa assimetria que o mantém fora da regra do mapa
    # duplicado (CLAUDE.md, "A cópia do mapa no cnc_simulator não existe mais").
    # Uma cópia da geometria teria de CONCORDAR com a placa, e passaria a mentir
    # no dia em que alguém regravasse um waypoint na bancada; um limite superior
    # continua verdadeiro enquanto a mesa couber embaixo dele.
    #
    # O que o quebra é subir o FEED sem subir o teto — por isso a conta está
    # escrita aqui, e por isso o `cnc/README.md` a repete na lista de bancada.
    CNC_TETO_TRAJETO_S:     float = field(
        default_factory=lambda: _cronograma_s("CNC_TETO_TRAJETO_S", 2.5, 0.1, 60.0))
    # Serial + ACK + salto de thread do adapter + latência do `loop()` da placa.
    # É o que separa "a mesa chegou" de "o central soube que ela chegou", e é
    # estimativa — a medida de bancada está pendente no `cnc/README.md`.
    CNC_MARGEM_CHEGADA_S:   float = field(
        default_factory=lambda: _cronograma_s("CNC_MARGEM_CHEGADA_S", 0.75, 0.1, 60.0))
    # O mecanismo solta uma unidade por ciclo de servo. MESMO número que o
    # `WP()` do firmware usa para gravar o dwell das receitas — ver
    # `cronograma_do_ciclo`.
    DISPENSA_S_POR_UNIDADE: float = field(
        default_factory=lambda: _cronograma_s("DISPENSA_S_POR_UNIDADE", 1.0, 0.05, 30.0))
    DISPENSA_FOLGA_S:       float = field(
        default_factory=lambda: _cronograma_s("DISPENSA_FOLGA_S", 1.0, 0.05, 30.0))

    # ── Modo de demonstração ──────────────────────────────────────────────────
    # O central não sorteia falha; estes dois campos existem para PUBLICAR o
    # que está em vigor (log de boot, `/api/v1/estado`, tela de pré-voo) e,
    # no caso do fator, para escalar os timeouts acima.
    MODO_APRESENTACAO: bool  = field(default_factory=_modo_apresentacao)
    FATOR_VELOCIDADE:  float = field(default_factory=_fator_velocidade)

    # ── Triple Check ──────────────────────────────────────────────────────────
    TRIPLE_CHECK_MIN_DIVERGENCIAS: int = _limiar_triple_check()

    # ── Backpressure da fila de OS ────────────────────────────────────────────
    MAX_FILA_OS: int = _max_fila_os()

    # ── Geometria da célula ───────────────────────────────────────────────────
    # Nº de dispensers, em duas fileiras frente a frente. Fonte única para o
    # mapa de posições (orchestrator), a validação de slot (main) e o seed de
    # `dispenser_estado` (database).
    NUM_SLOTS: int = _num_slots()

    # ── Console de operação ───────────────────────────────────────────────────
    # Senha própria, independente do JWT. VAZIA = console desabilitado (503 em
    # toda rota `/console*`) — nunca uma senha default embutida.
    CONSOLE_SENHA: str = field(default_factory=_console_senha)
    CONSOLE_SESSAO_HORAS: float = field(default_factory=_console_sessao_horas)

    # ── Volume de escrita e de broadcast ──────────────────────────────────────
    # Intervalo mínimo entre broadcasts de eventos de ALTA FREQUÊNCIA (posição
    # da CNC a cada 0.5s, telemetria de todos os slots a cada 15s). Transição de
    # verdade — trava, fim de OS, alarme — ignora o throttle e sai na hora.
    BROADCAST_MIN_INTERVALO_MS: int = int(os.getenv("BROADCAST_MIN_INTERVALO_MS", "500"))
    # 1 em N eventos "movendo" vira linha em `cnc_eventos`. 0 = nenhum (default):
    # a trajetória já está no estado em memória e no dashboard; o banco só
    # precisa das transições. Suba para amostrar rastro (20 ≈ 1 linha/10s).
    CNC_AMOSTRAGEM_MOVENDO: int = int(os.getenv("CNC_AMOSTRAGEM_MOVENDO", "0"))
    # Retenção das tabelas de histórico de alta cardinalidade.
    RETENCAO_DIAS: int = int(os.getenv("RETENCAO_DIAS", "30"))
    EXPURGO_INTERVALO_HORAS: float = float(os.getenv("EXPURGO_INTERVALO_HORAS", "24"))


settings = Settings()
