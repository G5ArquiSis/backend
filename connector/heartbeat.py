"""Señal de vida en archivo del connector (RNF7).

connector no expone una API HTTP, así que su healthcheck es file-based: cada consumidor
refresca su propio archivo mientras está sano (`{HEARTBEAT_PATH}.{consumidor}`) y este
módulo revisa que estén frescos **todos los que deberían estar corriendo**. Con un solo
archivo compartido, un consumidor colgado quedaría oculto tras otro que sigue latiendo.

Consumidores y cuándo se exigen:

- `observer`: siempre (demand-set de la E0).
- `ciudad`: si hay `CITY_BROKER_PASSWORD` (cola `city.{CODE}.q`).
- `mercado`: si además `MARKET_ENABLED=true` (cola `market.{CODE}`). El consumidor del
  mercado debe llamar a `record_heartbeat("mercado")` en cada vuelta de su bucle, igual
  que los otros dos; si no, con el mercado encendido el contenedor queda unhealthy y el
  deploy se revierte solo.

Ejecutado como script sale 0 (healthy) o 1 (unhealthy, con el consumidor atrasado en la
salida, que Docker guarda en `docker inspect`).
"""

import sys
import time
from pathlib import Path

from config import Settings, get_settings

CONSUMERS = ("observer", "ciudad", "mercado")


def heartbeat_file(consumer: str, settings: Settings | None = None) -> Path:
    if consumer not in CONSUMERS:
        raise ValueError(f"consumidor desconocido: {consumer!r}")
    path = (settings or get_settings()).heartbeat_path
    return path.with_name(f"{path.name}.{consumer}")


def record_heartbeat(consumer: str) -> None:
    """Marca que `consumer` sigue sano en este instante."""
    path = heartbeat_file(consumer)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def expected_consumers(settings: Settings) -> list[str]:
    """Los consumidores que deberían estar corriendo con esta configuración."""
    expected = ["observer"]
    if settings.city_broker_password:
        expected.append("ciudad")
        if settings.market_enabled:
            expected.append("mercado")
    return expected


def stale_consumers(settings: Settings | None = None, now: float | None = None) -> list[str]:
    """Los consumidores esperados sin señal de vida reciente (vacío si todo está sano)."""
    settings = settings or get_settings()
    now = time.time() if now is None else now
    stale = []
    for consumer in expected_consumers(settings):
        path = heartbeat_file(consumer, settings)
        if not path.exists() or now - path.stat().st_mtime >= settings.heartbeat_max_age_seconds:
            stale.append(consumer)
    return stale


def is_heartbeat_fresh() -> bool:
    return not stale_consumers()


if __name__ == "__main__":
    stale = stale_consumers()
    if stale:
        print(f"sin señal de vida reciente: {', '.join(stale)}")
    sys.exit(1 if stale else 0)
