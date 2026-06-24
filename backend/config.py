import os
from dataclasses import dataclass


@dataclass
class Settings:
    # ── MQTT ──────────────────────────────────────────────────────────────────
    MQTT_HOST: str   = os.getenv("MQTT_HOST", "mosquitto")
    MQTT_PORT: int   = int(os.getenv("MQTT_PORT", "1883"))

    # ── MySQL ─────────────────────────────────────────────────────────────────
    MYSQL_HOST: str  = os.getenv("MYSQL_HOST", "mysql")
    MYSQL_PORT: int  = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_DB:   str  = os.getenv("MYSQL_DB",   "apsen_db")
    MYSQL_USER: str  = os.getenv("MYSQL_USER", "apsen")
    MYSQL_PASS: str  = os.getenv("MYSQL_PASS", "apsen_pass_2024")

    # ── JWT ───────────────────────────────────────────────────────────────────
    SECRET_KEY: str  = os.getenv("SECRET_KEY", "apsen-mude-esta-chave-em-producao-2024")

    # ── Integração ERP / SAP ──────────────────────────────────────────────────
    ERP_ENABLED: bool  = os.getenv("ERP_ENABLED", "false").lower() == "true"
    ERP_URL:     str   = os.getenv("ERP_URL", "")
    ERP_CENTRO:  str   = os.getenv("ERP_CENTRO", "APSEN-SP-01")
    ERP_TIMEOUT: float = float(os.getenv("ERP_TIMEOUT", "3"))


settings = Settings()
