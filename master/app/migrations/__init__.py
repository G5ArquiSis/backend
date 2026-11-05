"""Migraciones del esquema: scripts SQL numerados que se aplican al arrancar master.

`create_all` crea las tablas que faltan, pero nunca altera una que ya existe. Producción
tiene datos de la E1, así que una columna nueva en una tabla existente (por ejemplo, en
la outbox) necesita su propio `ALTER TABLE`. Para eso están los archivos de `sql/`.

Reglas para escribir una migración (detalle en `sql/README.md`):

- El nombre es `NNNN_descripcion.sql` y se aplica en orden numérico, una sola vez.
- Debe ser idempotente (`ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`...):
  en una base nueva `create_all` ya creó la tabla con su forma final antes de que corra.
- Nunca se edita una migración ya mergeada: se agrega una nueva.

Las dos réplicas de master arrancan a la vez sobre la misma base. Un advisory lock de
Postgres serializa la ejecución: la segunda réplica espera, ve las migraciones ya
registradas y no aplica nada.
"""

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

SQL_DIR = Path(__file__).parent / "sql"

# Llave del advisory lock: cualquier entero fijo sirve mientras nadie más lo use.
_LOCK_KEY = 2173_0002


def pending_files(directory: Path = SQL_DIR) -> list[Path]:
    """Archivos de migración en orden de aplicación."""
    return sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))


async def run_migrations(engine: AsyncEngine, directory: Path = SQL_DIR) -> list[str]:
    """Aplica las migraciones que falten y devuelve las versiones aplicadas en esta llamada.

    Todo corre en una transacción: si una migración falla, ninguna queda a medias y
    master no arranca, que es mejor que servir con un esquema distinto al del código.
    """
    applied_now: list[str] = []
    async with engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
        await connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version TEXT PRIMARY KEY,"
                " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        applied = set(
            (await connection.execute(text("SELECT version FROM schema_migrations"))).scalars()
        )

        # asyncpg ejecuta varias sentencias seguidas solo con su execute() sin
        # parámetros; la conexión cruda es la misma, así que sigue en la transacción.
        raw = await connection.get_raw_connection()
        for path in pending_files(directory):
            version = path.stem
            if version in applied:
                continue
            await raw.driver_connection.execute(path.read_text(encoding="utf-8"))
            await connection.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": version},
            )
            applied_now.append(version)
            logger.info("Migración aplicada: %s", version)

    return applied_now
