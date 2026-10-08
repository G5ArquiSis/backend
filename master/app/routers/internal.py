"""Endpoints que solo usa connector: entrada de mensajes de la central y outbox.

No son parte de la API pública. Nginx responde 404 a `/internal/` desde afuera;
connector llega directo por la red de Docker.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.cycle_repository import (
    MalformedMessageError,
    apply_central_message,
    claim_outbox,
    mark_outbox_sent,
    record_unapplied_message,
    run_due_work,
)
from app.cycle_schemas import ApplyResult, MessageLogEntry, OutboxItem, UnappliedMessageIn
from app.database import get_session

router = APIRouter(prefix="/internal", tags=["internal"])

SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]


@router.post("/messages", response_model=ApplyResult)
async def receive_message(
    message: dict, session: SessionDependency, settings: SettingsDependency
) -> ApplyResult:
    """Aplica al ledger un mensaje de la central ya validado por connector.

    Es idempotente: reenviar el mismo mensaje responde `duplicate` y no cambia
    nada. Los tipos que no afectan al ciclo responden `ignored`. Los tres son
    éxito para connector. Responde 422 si al mensaje le falta un campo de su tipo.
    """
    try:
        outcome = await apply_central_message(session, message, datetime.now(UTC), settings)
    except MalformedMessageError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return ApplyResult(outcome=outcome)


@router.post("/outbox/claim", response_model=list[OutboxItem])
async def claim_pending_messages(
    session: SessionDependency, settings: SettingsDependency
) -> list[OutboxItem]:
    """Entrega los mensajes por publicar a la central.

    connector lo llama cada pocos segundos. Antes de responder, revisa los plazos
    persistidos y encola lo que venció: el negotiation-report del ciclo y los
    reintentos de propuestas. Esa revisión es el reloj del sistema (ADR de AD3).
    """
    now = datetime.now(UTC)
    await run_due_work(session, now, settings)
    messages = await claim_outbox(session, now, settings)

    return [OutboxItem(id=message.id, payload=message.payload) for message in messages]


@router.post("/outbox/{message_id}/sent", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_message_sent(message_id: int, session: SessionDependency) -> None:
    """connector confirma que publicó el mensaje; deja de ofrecerse."""
    if not await mark_outbox_sent(session, message_id, datetime.now(UTC)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mensaje no encontrado")


@router.post("/message-log", response_model=MessageLogEntry, status_code=status.HTTP_201_CREATED)
async def log_unapplied_message(
    entry: UnappliedMessageIn, session: SessionDependency
) -> MessageLogEntry:
    """connector deja constancia de un mensaje descartado o respondido con NACK (RF05).

    `rawContent` es el cuerpo tal como llegó del broker, aunque no sea JSON válido.
    Los duplicados no se informan por acá: los registra master al detectarlos.
    """
    stored = await record_unapplied_message(
        session, entry.category, entry.raw_content, entry.reason
    )

    return MessageLogEntry.model_validate(stored)
