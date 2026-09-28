"""Capa de datos de master: conexión a Postgres y ciclo de vida de las sesiones.

Todo el uso de SQLAlchemy vive en este módulo y en `repository.py` (CLAUDE.md
seccion 8): si cambiara el ORM, el impacto queda contenido en esos dos archivos y
no esparcido por los routers.

Las queries del historial están en `repository.py`, que es la misma capa separada
por tamaño, no una capa nueva (seccion 4.3).
"""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Base

# La construcción del acceso a la base queda separada de su uso (seccion 10):
# este módulo la ensambla una vez y el resto de la app solo pide sesiones.
_engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Entrega una sesión por request y la cierra al terminar."""
    async with _session_factory() as session:
        yield session


async def create_schema() -> None:
    """Crea las tablas que falten.

    E0 no necesita migraciones versionadas: el esquema es una sola tabla y aún no
    hay datos en producción que preservar.
    """
    async with _engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def ping(session: AsyncSession) -> None:
    """Verifica que Postgres responde. Levanta si la conexión no está sana."""
    await session.execute(text("SELECT 1"))
