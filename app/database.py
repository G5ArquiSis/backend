"""Conexión a Postgres y ciclo de vida de las sesiones."""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Entrega una sesión por request y la cierra al terminar."""
    async with _session_factory() as session:
        yield session


async def ping(session: AsyncSession) -> None:
    """Verifica que Postgres responde. Levanta si la conexión no está sana."""
    await session.execute(text("SELECT 1"))
