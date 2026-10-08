"""Endpoints HTTP para el historial de ciclos (RF01) y el registro de mensajes (RF05).

Expone las rutas consumidas por la SPA (CycleHistory.jsx y Messages.jsx) y definidas
en la especificación OpenAPI (ADR-0002 / RDOC04).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.ledger_service import LedgerService
from app.schemas import (
    CycleDetail,
    MessageEnvelope,
    PaginatedCycles,
    PaginatedMessageLogs,
    ProcessResult,
)

router = APIRouter(tags=["cycles"])

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    "/ledger/messages",
    response_model=ProcessResult,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "Mensaje duplicado; idpk ya aplicado, ledger no modificado (RF05)"},
        201: {"description": "Mensaje procesado y aplicado sobre el ledger exitosamente"},
    },
)
async def process_incoming_message(
    envelope: MessageEnvelope,
    response: Response,
    session: SessionDependency,
) -> ProcessResult:
    """Ingesta de mensajes consumidos del broker por Rol A (o emitidos por Rol C).

    Aplica de forma atómica e idempotente las reglas contables del ledger.
    Si el idpk ya existe, devuelve HTTP 200 (duplicate) para que connector haga ACK
    sin duplicar transacciones.
    """
    if not envelope.cycle_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El envelope debe contener un 'cycleId'",
        )

    service = LedgerService(session)
    result = await service.process_message(
        idpk=envelope.idpk,
        msg_id=envelope.msg_id,
        event_type=envelope.type,
        cycle_id=envelope.cycle_id,
        data=envelope.data,
        timestamp=envelope.timestamp,
    )

    if result.is_duplicate:
        response.status_code = status.HTTP_200_OK

    return result


@router.get("/cycles/{cycle_id}/balances")
async def get_cycle_balances(
    cycle_id: str,
    session: SessionDependency,
) -> dict[str, Any]:
    """Permite al módulo de negociación de Rol C consultar balances y spare en O(1)."""
    service = LedgerService(session)
    return await service.get_current_balances(cycle_id)


@router.get("/cycles", response_model=PaginatedCycles)
async def list_cycles(
    session: SessionDependency,
    page: Annotated[int, Query(ge=1, description="Número de página (1-indexed)")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="Ciclos por página")] = 25,
) -> PaginatedCycles:
    """Historial paginado de ciclos (RF01 y CycleHistory.jsx)."""
    service = LedgerService(session)
    return await service.list_cycles(page=page, limit=limit)


@router.get("/cycles/{cycle_id}", response_model=CycleDetail)
async def get_cycle(
    cycle_id: str,
    session: SessionDependency,
) -> CycleDetail:
    """Detalle completo y explicable de un ciclo específico (RF01 y CycleHistory.jsx)."""
    service = LedgerService(session)
    detail = await service.get_cycle_detail(cycle_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ciclo '{cycle_id}' no encontrado",
        )
    return detail


@router.get("/message-log", response_model=PaginatedMessageLogs)
async def list_message_log(
    session: SessionDependency,
    page: Annotated[int, Query(ge=1, description="Número de página (1-indexed)")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="Mensajes por página")] = 25,
    category: Annotated[str | None, Query(description="Filtrar por categoría (duplicate, discarded, nack)")] = None,
) -> PaginatedMessageLogs:
    """Registro consultable de mensajes duplicados, descartados o respondidos con NACK (RF05 y Messages.jsx)."""
    service = LedgerService(session)
    return await service.list_message_logs(page=page, limit=limit, category=category)
