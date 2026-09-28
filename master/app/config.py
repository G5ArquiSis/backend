"""Configuración de master, leída desde variables de entorno."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Valores que el servicio web necesita del entorno."""

    database_url: str

    # El .env es compartido por los tres containers, así que las variables del
    # broker llegan acá aunque master no las use.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Devuelve la configuración del proceso, construida una sola vez."""
    return Settings()
