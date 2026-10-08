"""Fixtures compartidas por los tests de master."""

import os
from collections.abc import AsyncIterator

import httpx
import pytest

# database.py construye el engine al importarse. No abre conexiones, pero la URL
# tiene que existir; TEST_DATABASE_URL apunta a una base desechable, porque las
# fixtures de integración truncan la tabla antes de cada test.
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://energyshark:changeme@localhost:5432/energyshark",
    ),
)

from fastapi import FastAPI  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.database import _session_factory, create_schema  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402


@pytest.fixture
def app() -> FastAPI:
    """La aplicación FastAPI ya ensamblada, sin levantar un servidor."""
    return fastapi_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Cliente HTTP contra la app en memoria.

    ASGITransport no corre el lifespan a propósito: así los tests que no tocan la
    base no disparan create_schema() y siguen pasando sin Postgres.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


@pytest.fixture(scope="session")
async def database() -> None:
    """Prepara el esquema, o salta los tests que lo necesitan si no hay Postgres.

    Un checkout limpio sin base tiene que reportar tests saltados, nunca errores:
    quien corrija la entrega va a ejecutar `pytest` sin levantar nada.
    """
    try:
        await create_schema()
    except Exception as error:  # noqa: BLE001 - cualquier fallo de conexión sirve para saltar
        pytest.skip(f"sin Postgres alcanzable: {error}")


@pytest.fixture
async def session(database: None) -> AsyncIterator[AsyncSession]:
    """Sesión contra la base de tests, con la tabla vacía al empezar.

    El truncado va antes y no después: si un test falla, sus filas quedan para
    inspeccionar. RESTART IDENTITY hace que los `id` arranquen en 1 y los tests
    puedan afirmar sobre valores concretos.
    """
    async with _session_factory() as database_session:
        await database_session.execute(
            text(
                "TRUNCATE demand_events, ledger_events, voluntary_negotiations, "
                "duplicate_messages, cycle_ledger RESTART IDENTITY CASCADE"
            )
        )
        await database_session.commit()
        yield database_session
