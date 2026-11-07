"""Configuración de master, leída desde variables de entorno."""

from functools import lru_cache

from pydantic import Field

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
    """Valores que el servicio web necesita del entorno."""

    database_url: str
    wallet_base_url: str | None = None
    wallet_api_key: str | None = None

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
    # Petición directa del status-statement de un ciclo del que la central habló sin
    # mandarlo: margen antes de pedirlo, espera entre pedidos y tope de pedidos.
    statement_request_grace_seconds: int = 30
    statement_request_interval_seconds: int = 120
    statement_request_max_attempts: int = 3

    # Flags de la E2. Cada merge a main despliega solo a producción: el código nuevo
    # del mercado y de la wallet entra apagado y se enciende en el .env de la EC2
    # cuando está probado, sin tocar el ciclo de la E1 que ya funciona.
    market_enabled: bool = False
    wallet_enabled: bool = False
    wallet_reconcile_interval_seconds: int = Field(default=180, gt=0)

    # Comprador (rol D). Apagado por defecto: además necesita MARKET_ENABLED y
    # WALLET_ENABLED. El span y la política de selección no van acá: son de la estrategia
    # del ciclo (app/market/strategy.py, rol C), que se cambia sin redeploy.
    buyer_enabled: bool = False
    # Envíos totales de un mismo bid (con el mismo idpk) antes de darlo por vencido.
    bid_max_attempts: int = Field(default=3, ge=1)
    # JWKS para verificar el swapToken; por defecto {raíz de la wallet}/.well-known/jwks.json.
    wallet_jwks_url: str | None = None

    # El .env es compartido por los tres containers, así que las variables del
    # broker llegan acá aunque master no las use.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Devuelve la configuración del proceso, construida una sola vez."""
    return Settings()
