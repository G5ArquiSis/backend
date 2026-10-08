"""Configuración de connector, leída desde variables de entorno."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Valores que el consumidor necesita del entorno."""

    broker_host: str
    broker_port: int = 5671
    broker_user: str = "city.TAL"
    broker_password: str
    broker_vhost: str = "/"
    broker_queue: str

    master_events_url: str
    master_http_timeout_seconds: float = 10

    reconnect_initial_delay_seconds: float = 1
    reconnect_max_delay_seconds: float = 60

    heartbeat_path: Path = Path("/tmp/connector-heartbeat")
    heartbeat_max_age_seconds: float = 120

    city_code: str = "TAL"

    # El .env es compartido por los tres containers, así que las variables de
    # Postgres llegan acá aunque connector no las use.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def broker_url(self) -> str:
        """URL de conexión al broker.

        El puerto 5671 es AMQPS: el esquema debe ser amqps:// para que el cliente
        haga el handshake TLS. Usuario y contraseña se escapan porque pueden
        traer caracteres que rompen la URL.
        """
        credentials = f"{quote(self.broker_user, safe='')}:{quote(self.broker_password, safe='')}"
        vhost = self.broker_vhost.lstrip("/")
        return f"amqps://{credentials}@{self.broker_host}:{self.broker_port}/{vhost}"


@lru_cache
def get_settings() -> Settings:
    """Devuelve la configuración del proceso, construida una sola vez."""
    return Settings()
