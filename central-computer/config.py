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

    Só o dashboard e a IHM falam com o central pelo navegador. `*` num serviço
    autenticado deixa qualquer página aberta no mesmo browser do técnico
    disparar requisições em nome dele.
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


@dataclass
class Settings:
    # ── MySQL ─────────────────────────────────────────────────────────────────
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "mysql")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_DB:   str = os.getenv("MYSQL_DB",   "apsen_db")
    MYSQL_USER: str = os.getenv("MYSQL_USER", "apsen")
    MYSQL_PASS: str = os.getenv("MYSQL_PASS", "apsen_pass_2024")

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
    TIMEOUT_CARREGAMENTO:        float = float(os.getenv("TIMEOUT_CARREGAMENTO",        "180"))
    TIMEOUT_POSICIONAMENTO:      float = float(os.getenv("TIMEOUT_POSICIONAMENTO",      "120"))
    TIMEOUT_DISPENSA:            float = float(os.getenv("TIMEOUT_DISPENSA",            "120"))
    TIMEOUT_VISAO_DISPENSER:     float = float(os.getenv("TIMEOUT_VISAO_DISPENSER",     "30"))
    TIMEOUT_VISAO_MESA:          float = float(os.getenv("TIMEOUT_VISAO_MESA",          "30"))
    TIMEOUT_PESO:                float = float(os.getenv("TIMEOUT_PESO",                "15"))
    TIMEOUT_LIMPEZA:             float = float(os.getenv("TIMEOUT_LIMPEZA",             "60"))

    # ── Triple Check ──────────────────────────────────────────────────────────
    TRIPLE_CHECK_MIN_DIVERGENCIAS: int = _limiar_triple_check()

    # ── Backpressure da fila de OS ────────────────────────────────────────────
    MAX_FILA_OS: int = _max_fila_os()

    # ── Geometria da célula ───────────────────────────────────────────────────
    # Nº de dispensers, em duas fileiras frente a frente. Fonte única para o
    # mapa de posições (orchestrator), a validação de slot (main) e o seed de
    # `dispenser_estado` (database).
    NUM_SLOTS: int = _num_slots()

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
