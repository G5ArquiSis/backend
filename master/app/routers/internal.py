"""Endpoints que solo usa connector: entrada de mensajes de la central y outbox.

No son parte de la API pública. Nginx responde 404 a `/internal/` desde afuera;
connector llega directo por la red de Docker.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
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
from app.wallet.client import WalletClient
from app.wallet.reconciliacion import reconcile

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
    now = datetime.now(UTC)
    try:
        outcome = await apply_central_message(session, message, now, settings)
    except MalformedMessageError as error:
        metrics.record_inbound_message(message, "malformed", now)
        raise HTTPException(status_code=422, detail=str(error)) from error

    metrics.record_inbound_message(message, outcome, now)
    return ApplyResult(outcome=outcome)


@router.post("/outbox/claim", response_model=list[OutboxItem])
async def claim_pending_messages(
    session: SessionDependency, settings: SettingsDependency, background_tasks: BackgroundTasks
) -> list[OutboxItem]:
    """Entrega los mensajes por publicar a la central.

    connector lo llama cada pocos segundos. Antes de responder, revisa los plazos
    persistidos y encola lo que venció: el negotiation-report del ciclo y los
    reintentos de propuestas. Esa revisión es el reloj del sistema (ADR de AD3).
    """
    from app.market.bids import process_pending_bids
    from app.market.buyer import buyer_tick
    from app.market.swaps import process_reserved_bids
    from app.wallet.scheduler import reconcile_due

    # Primero se arbitran los bids agrupados; luego C4 propone los swaps que
    # hayan quedado reservados en la misma consulta de outbox.
    background_tasks.add_task(process_pending_bids, settings)
    background_tasks.add_task(process_reserved_bids, settings)
    background_tasks.add_task(reconcile_due, settings)
    background_tasks.add_task(buyer_tick, settings)
    now = datetime.now(UTC)
    await run_due_work(session, now, settings)
    messages = await claim_outbox(session, now, settings)

    return [
        OutboxItem(
            id=message.id,
            payload=message.payload,
            exchange=getattr(message, "exchange", "energy.x") or "energy.x",
            routing_key=getattr(message, "routing_key", "central") or "central",
        )
        for message in messages
    ]


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


@router.post("/wallet/reconcile")
async def reconcile_wallet(session: SessionDependency, settings: SettingsDependency):
    """
    este enpoint se debe invocar cada vez que se quiera reconciliar el estado de la
    wallet con el ledger local de la ciudad. No se invoca automaticamente desde conector.
    Es decision del rol E en su scheduler
    """

    if not settings.wallet_base_url or not settings.wallet_api_key:
        raise HTTPException(status_code=503, detail="Wallet sin configurar")

    async with WalletClient(settings.wallet_base_url, settings.wallet_api_key) as wallet:
        return await reconcile(session, wallet)
