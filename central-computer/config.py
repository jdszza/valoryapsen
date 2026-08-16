import logging
import os
from dataclasses import dataclass

_cfg_logger = logging.getLogger(__name__)

_DEFAULT_SECRET_KEY = "apsen-mude-esta-chave-em-producao-2024"


@dataclass
class Settings:
    # ── MySQL ─────────────────────────────────────────────────────────────────
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "mysql")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_DB:   str = os.getenv("MYSQL_DB",   "apsen_db")
    MYSQL_USER: str = os.getenv("MYSQL_USER", "apsen")
    MYSQL_PASS: str = os.getenv("MYSQL_PASS", "apsen_pass_2024")

    # ── JWT ───────────────────────────────────────────────────────────────────
    # OBRIGATÓRIO em produção: defina SECRET_KEY no ambiente com ≥32 caracteres aleatórios.
    # Gere com: python -c "import secrets; print(secrets.token_hex(32))"
    SECRET_KEY: str = os.getenv("SECRET_KEY", _DEFAULT_SECRET_KEY)

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


settings = Settings()
