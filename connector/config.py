"""Configuración de connector, leída desde variables de entorno."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Valores que el consumidor necesita del entorno."""

    broker_host: str
    broker_port: int = 5671
    # El puerto 5671 es AMQPS; en un broker local sin TLS se apaga.
    broker_use_ssl: bool = True
    broker_vhost: str = "/"

    # Credenciales y cola del observer: los demand-set de la E0.
    broker_user: str
    broker_password: str
    broker_queue: str

    # Credenciales de la ciudad: el usuario es city.{CODE}, la misma cadena que se usa
    # como user_id al publicar. Sin contraseña, el consumo de la ciudad queda apagado y
    # connector sigue atendiendo solo la E0.
    city_code: str = "TAL"
    city_broker_password: str = ""
    # Cola de la ciudad. Vacía, se usa city.{CODE}.q, que es como la nombra el curso
    # (igual que observer.N.q): el usuario no tiene permisos sobre otro nombre.
    city_queue: str = ""
    # Dónde se publica lo que va a la central: el exchange del curso y su routing key.
    central_exchange: str = "energy.x"
    central_routing_key: str = "central"
    # Cada cuánto se le pide a master lo pendiente por publicar. Es el reloj del ciclo.
    outbox_poll_seconds: float = 5

    master_events_url: str
    master_http_timeout_seconds: float = 10

    reconnect_initial_delay_seconds: float = 1
    reconnect_max_delay_seconds: float = 60

    heartbeat_path: Path = Path("/tmp/connector-heartbeat")
    heartbeat_max_age_seconds: float = 120

    # El .env es compartido por los tres containers, así que las variables de
    # Postgres llegan acá aunque connector no las use.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def city_identity(self) -> str:
        """Usuario del broker y user_id de la ciudad; también es su routing key."""
        return f"city.{self.city_code}"

    @property
    def city_queue_name(self) -> str:
        """Nombre de la cola de la ciudad, que no es igual al usuario: lleva el sufijo .q."""
        return self.city_queue or f"{self.city_identity}.q"


@lru_cache
def get_settings() -> Settings:
    """Devuelve la configuración del proceso, construida una sola vez."""
    return Settings()
