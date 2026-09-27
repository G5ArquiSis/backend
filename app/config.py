"""Configuración del backend, leída desde variables de entorno."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Valores que el servicio necesita del entorno. Ver `.env.example`."""

    database_url: str

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Devuelve la configuración del proceso, construida una sola vez."""
    return Settings()
