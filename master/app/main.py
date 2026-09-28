"""Punto de entrada de master: ensambla la aplicación FastAPI."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import create_schema
from app.routers import health, history


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prepara el esquema de la base antes de que el servicio acepte tráfico."""
    await create_schema()
    yield


app = FastAPI(title="EnergyShark", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(history.router)
