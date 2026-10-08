"""Configuración de master, leída desde variables de entorno."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Valores que el servicio web necesita del entorno."""

    database_url: str

    # Código de la ciudad: va como cityId en todo mensaje que emitimos.
    city_code: str = "TAL"
    # Duración del periodo de cierre: el negotiation-report solo se acepta en ese
    # tramo final de la ventana. Es un valor del despliegue de la central.
    report_closing_seconds: int = 300
    # Margen tras la apertura del periodo de cierre, por la diferencia entre relojes.
    report_margin_seconds: int = 15
    negotiation_timeout_seconds: int = 30
    # Envíos totales de una misma operación antes de darla por expirada (ADR de AD3).
    negotiation_max_attempts: int = 3
    # Tras este tiempo sin confirmación de envío, un mensaje retirado vuelve a ofrecerse.
    outbox_redelivery_seconds: int = 60

    # El .env es compartido por los tres containers, así que las variables del
    # broker llegan acá aunque master no las use.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Devuelve la configuración del proceso, construida una sola vez."""
    return Settings()
