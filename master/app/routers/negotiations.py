"""API de la E1 para la interfaz: ciclos (RF01), conectividad (RF02), negociaciones
(RF04) y registro de mensajes no aplicados (RF05).

Solo traduce entre HTTP y la capa de datos; ninguna query vive acá.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.cycle_repository import (
    NegotiationNotAllowedError,
    budget_at_close,
    count_message_log,
    create_negotiation,
    current_distance_table,
    get_cycle,
    get_cycle_report,
    get_negotiation,
    list_cycle_events,
    list_cycle_negotiations,
    list_cycles,
    list_message_log,
    list_negotiations,
    total_budget,
)
from app.cycle_schemas import (
    Connection,
    Connectivity,
    CycleBalances,
    CycleDetail,
    CycleList,
    CycleOut,
    LedgerEventOut,
    MessageLogEntry,
    MessageLogPage,
    NegotiationIn,
    NegotiationOut,
)
from app.database import get_session
from app.models import LedgerEvent
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
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> CycleList:
    """Ciclos del más reciente al más antiguo, con su balance y el presupuesto actual."""
    cycles = await list_cycles(session, limit, (page - 1) * limit)

    return CycleList(
        budget_balance=await total_budget(session),
        items=[CycleOut.model_validate(cycle) for cycle in cycles],
    )


@router.get("/cycles/{cycle_id}", response_model=CycleDetail)
async def read_cycle(cycle_id: str, session: SessionDependency) -> CycleDetail:
    """Historial de un ciclo (RF01): lo recibido, lo negociado, lo reportado y los balances.

    `operations` trae todo en el orden en que se aplicó; la última lleva
    `isLast: true`, y su idpk se repite en `lastOperationIdpk`.
    """
    cycle = await get_cycle(session, cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ciclo no encontrado")

    events = await list_cycle_events(session, cycle_id)
    operations = [_to_operation(event, cycle.last_operation_idpk) for event in events]
    negotiations = await list_cycle_negotiations(session, cycle_id)

    return CycleDetail(
        cycle_id=cycle.cycle_id,
        opened=cycle.opened,
        valid_until=cycle.valid_until,
        status_statement=next(
            (event.payload for event in events if event.kind == "status-statement"), None
        ),
        transfers=[operation for operation in operations if operation.kind == "transfer"],
        demand_statements=[
            operation for operation in operations if operation.kind == "demand-statement"
        ],
        negotiations=[NegotiationOut.model_validate(negotiation) for negotiation in negotiations],
        report=await get_cycle_report(session, cycle),
        report_sent_at=cycle.report_sent_at,
        report_error=cycle.report_error,
        balances=CycleBalances(
            energy=cycle.energy_balance, budget=await budget_at_close(session, cycle)
        ),
        last_operation_idpk=cycle.last_operation_idpk,
        operations=operations,
    )


@router.get("/connectivity", response_model=Connectivity)
async def read_connectivity(session: SessionDependency) -> Connectivity:
    """La distance-table vigente (RF02): destino, distancia, costo de transporte y estado.

    Refleja la última que publicó la central. Antes de la primera, responde una
    lista vacía en vez de 404: la interfaz muestra "sin datos", no un error.
    """
    table = await current_distance_table(session)
    if table is None:
        return Connectivity(updated_at=None, connections=[])

    return Connectivity(
        updated_at=table.received_at,
        connections=[
            _to_connection(destination, route)
            for destination, route in sorted(table.distances.items())
        ],
    )


@router.get("/message-log", response_model=MessageLogPage)
async def list_unapplied_messages(
    session: SessionDependency,
    category: Annotated[
        Literal["duplicate", "discarded", "nack"] | None,
        Query(description="Filtra por tipo de registro"),
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> MessageLogPage:
    """Mensajes duplicados, descartados y respondidos con NACK (RF05), paginados."""
    total = await count_message_log(session, category)
    entries = await list_message_log(session, category, limit, (page - 1) * limit)

    return MessageLogPage(
        items=[MessageLogEntry.model_validate(entry) for entry in entries],
        page=page,
        limit=limit,
        total=total,
        pages=(total + limit - 1) // limit,
    )


def _to_operation(event: LedgerEvent, last_operation_idpk: str | None) -> LedgerEventOut:
    return LedgerEventOut(
        idpk=event.idpk,
        kind=event.kind,
        energy_delta=event.energy_delta,
        budget_delta=event.budget_delta,
        applied_at=event.created_at,
        is_last=event.idpk == last_operation_idpk,
        message=event.payload,
    )


def _to_connection(destination: str, route: object) -> Connection:
    """Una fila de la tabla; tolera una ruta mal formada sin botar la respuesta entera."""
    fields = route if isinstance(route, dict) else {}

    return Connection(
        destination=destination,
        distance=_number_or_none(fields.get("distance")),
        transport_cost=_number_or_none(fields.get("transportCost")),
        enabled=fields.get("enabled") if isinstance(fields.get("enabled"), bool) else None,
    )


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
