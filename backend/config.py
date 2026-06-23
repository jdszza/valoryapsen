import os
from dataclasses import dataclass

@dataclass
class Settings:
    MQTT_HOST: str = os.getenv("MQTT_HOST", "mosquitto")
    MQTT_PORT: int = int(os.getenv("MQTT_PORT", "1883"))
    DB_PATH: str = os.getenv("DB_PATH", "/data/apsen.db")

settings = Settings()
