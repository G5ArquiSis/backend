"""Punto de entrada de master: ensambla la aplicación FastAPI."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import create_schema, engine
from app.migrations import run_migrations
from app.routers import experiments, health, history, internal, market, negotiations


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prepara el esquema de la base antes de que el servicio acepte tráfico.

    Primero `create_all` crea las tablas que falten; después las migraciones alteran
    las que ya existían (ver app/migrations).
    """
    await create_schema()
    await run_migrations(engine)
    yield


app = FastAPI(title="EnergyShark", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(history.router)
app.include_router(internal.router)
app.include_router(negotiations.router)
app.include_router(market.router)
app.include_router(experiments.router)
