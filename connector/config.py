"""Configuración de connector, leída desde variables de entorno."""

from functools import lru_cache
from pathlib import Path

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:
    import os

    from pydantic import BaseModel

    def SettingsConfigDict(**kwargs):  # noqa: N802
        return kwargs

    class BaseSettings(BaseModel):  # type: ignore
        def __init__(self, **kwargs):
            env_kwargs = {}
            for field_name in self.__class__.model_fields:
                env_val = os.environ.get(field_name.upper())
                if env_val is not None:
                    env_kwargs[field_name] = env_val
            env_kwargs.update(kwargs)
            super().__init__(**env_kwargs)


class Settings(BaseSettings):
    """Valores que el consumidor necesita del entorno."""

    broker_host: str
    broker_port: int = 5671
    # El puerto 5671 es AMQPS; en un broker local sin TLS se apaga.
    broker_use_ssl: bool = True
    broker_vhost: str = "energy"

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
    # Cola de mercado (feed público v3). Por omisión para Talca es market.TAL (sin .q).
    market_queue: str = ""
    # Exchange de mercado y routing key propia para publicar ofertas.
    market_exchange: str = "market"
    # Dónde se publica lo que va a la central: el exchange del curso y su routing key.
    central_exchange: str = "energy.x"
    central_routing_key: str = "central"
    # Cada cuánto se le pide a master lo pendiente por publicar. Es el reloj del ciclo.
    outbox_poll_seconds: float = 5
    # Flag de la E2: consumo de market.{CODE} y publicación al exchange market.
    market_enabled: bool = False

    # Límites de tasa v3 (ventana deslizante de 60 segundos)
    market_ops_per_minute: int = 60
    request_ops_per_minute: int = 10

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

    @property
    def market_queue_name(self) -> str:
        """Nombre de la cola de mercado de la ciudad (para Talca: market.TAL sin .q)."""
        return self.market_queue or f"market.{self.city_code}"

    @property
    def market_offer_routing_key(self) -> str:
        """Routing key con la que la ciudad publica sus ofertas en el exchange market."""
        return f"market.offer.{self.city_code}"


@lru_cache
def get_settings() -> Settings:
    """Devuelve la configuración del proceso, construida una sola vez."""
    return Settings()
