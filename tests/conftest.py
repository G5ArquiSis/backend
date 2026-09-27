"""Configuración común de los tests.

`app.database` crea el engine al importarse y necesita DATABASE_URL. Crear el engine no
abre conexiones, así que basta un valor por defecto para correr los tests sin Postgres.
"""

import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://energyshark:changeme@localhost:5432/energyshark_test"
)
