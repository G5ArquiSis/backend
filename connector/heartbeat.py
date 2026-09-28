"""Señal de vida en archivo del connector (RNF7).

connector no expone una API HTTP, así que su healthcheck es file-based: el
consumidor refresca un archivo mientras está sano y este módulo mira qué tan
fresco está. Ejecutado como script sale 0 (healthy) o 1 (unhealthy), que es lo
que Docker interpreta.
"""

import sys
import time

from config import get_settings


def record_heartbeat() -> None:
    """Marca que el consumidor sigue sano en este instante."""
    path = get_settings().heartbeat_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def is_heartbeat_fresh() -> bool:
    """Indica si la última señal de vida es más reciente que el máximo tolerado."""
    settings = get_settings()
    if not settings.heartbeat_path.exists():
        return False

    age_seconds = time.time() - settings.heartbeat_path.stat().st_mtime
    return age_seconds < settings.heartbeat_max_age_seconds


if __name__ == "__main__":
    sys.exit(0 if is_heartbeat_fresh() else 1)
