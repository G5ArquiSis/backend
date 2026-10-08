"""Administración de negociaciones voluntarias (RF04) y estado de los ciclos."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.cycle_repository import (
    NegotiationNotAllowedError,
    create_negotiation,
    get_negotiation,
    list_cycles,
    list_negotiations,
    total_budget,
)
from app.cycle_schemas import CycleList, CycleOut, NegotiationIn, NegotiationOut
from app.database import get_session
from app.schemas import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(tags=["negotiations"])

SessionDependency = Annotated[AsyncSession, Depends(get_session)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]


@router.post("/negotiations", response_model=NegotiationOut, status_code=status.HTTP_201_CREATED)
async def propose_negotiation(
    proposal: NegotiationIn, session: SessionDependency, settings: SettingsDependency
) -> NegotiationOut:
    """Crea una propuesta para el ciclo abierto; se publica a la central en segundos.

    Responde 409 con el motivo si no hay ventana abierta, si el precio supera el
    tope o si un `give` excede la energía vendible.
    """
    try:
        negotiation = await create_negotiation(
            session,
            proposal.direction,
            proposal.quantity,
            proposal.price_per_energy,
            datetime.now(UTC),
            settings,
        )
    except NegotiationNotAllowedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": error.reason, "message": str(error)},
        ) from error

    return NegotiationOut.model_validate(negotiation)


@router.get("/negotiations", response_model=list[NegotiationOut])
async def list_negotiation_history(
    session: SessionDependency,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> list[NegotiationOut]:
    """Historial de negociaciones con su estado: pending, confirmed, paid, expired o rejected."""
    negotiations = await list_negotiations(session, limit, (page - 1) * limit)

    return [NegotiationOut.model_validate(negotiation) for negotiation in negotiations]


@router.get("/negotiations/{negotiation_id}", response_model=NegotiationOut)
async def read_negotiation(negotiation_id: int, session: SessionDependency) -> NegotiationOut:
    negotiation = await get_negotiation(session, negotiation_id)
    if negotiation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Negociación no encontrada"
        )

    return NegotiationOut.model_validate(negotiation)


@router.get("/cycles", response_model=CycleList)
async def list_cycle_ledger(
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> CycleList:
    """Últimos ciclos con su balance, el estado de su reporte y el presupuesto actual."""
    cycles = await list_cycles(session, limit)

    return CycleList(
        budget_balance=await total_budget(session),
        items=[CycleOut.model_validate(cycle) for cycle in cycles],
    )
