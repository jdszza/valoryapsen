import os
from dataclasses import dataclass


@dataclass
class Settings:
    # ── MySQL ─────────────────────────────────────────────────────────────────
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "mysql")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_DB:   str = os.getenv("MYSQL_DB",   "apsen_db")
    MYSQL_USER: str = os.getenv("MYSQL_USER", "apsen")
    MYSQL_PASS: str = os.getenv("MYSQL_PASS", "apsen_pass_2024")

    # ── JWT ───────────────────────────────────────────────────────────────────
    SECRET_KEY: str = os.getenv("SECRET_KEY", "apsen-mude-esta-chave-em-producao-2024")

    # ── Adapter URLs ──────────────────────────────────────────────────────────
    DISPENSER_ADAPTER_URL: str = os.getenv("DISPENSER_ADAPTER_URL", "http://dispenser-adapter:8100")
    CNC_ADAPTER_URL:       str = os.getenv("CNC_ADAPTER_URL",       "http://cnc-adapter:8101")
    VISION_ADAPTER_URL:    str = os.getenv("VISION_ADAPTER_URL",    "http://vision-adapter:8102")

    # ── Timeouts de orquestração (segundos) ───────────────────────────────────
    TIMEOUT_CARREGAMENTO:        float = float(os.getenv("TIMEOUT_CARREGAMENTO",        "180"))
    TIMEOUT_POSICIONAMENTO:      float = float(os.getenv("TIMEOUT_POSICIONAMENTO",      "120"))
    TIMEOUT_DISPENSA:            float = float(os.getenv("TIMEOUT_DISPENSA",            "120"))
    TIMEOUT_VISAO_DISPENSER:     float = float(os.getenv("TIMEOUT_VISAO_DISPENSER",     "30"))
    TIMEOUT_VISAO_MESA:          float = float(os.getenv("TIMEOUT_VISAO_MESA",          "30"))


settings = Settings()
