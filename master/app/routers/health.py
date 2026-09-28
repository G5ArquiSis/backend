"""Endpoint de salud que consume el HEALTHCHECK del container (RNF7)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session, ping

router = APIRouter(tags=["health"])


@router.get("/health")
async def read_health(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
    """Responde 200 solo si el servicio puede hablar con Postgres.

    No consulta al broker a propósito: master debe seguir reportándose sano y
    atendiendo el historial aunque RabbitMQ esté caído (RNF1).
    """
    await ping(session)
    return {"status": "ok"}
