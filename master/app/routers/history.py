"""Capa de presentación del historial de demanda y de la ingesta de eventos.

Solo traduce entre HTTP y la capa de datos: validación con Pydantic, códigos de
estado y forma de la respuesta. Ninguna query vive acá (CLAUDE.md 4.3).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import DemandEvent
from app.repository import (
    count_demand_events,
    get_demand_event,
    list_demand_events,
    save_demand_event,
)
from app.schemas import (
    DemandEventIn,
    DemandEventOut,
    HistoryFilters,
    PackageBody,
    PageParams,
    PaginatedHistory,
)

router = APIRouter(tags=["history"])

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.get("/history", response_model=PaginatedHistory)
async def list_history(
    pagination: Annotated[PageParams, Depends()],
    filters: Annotated[HistoryFilters, Depends()],
    session: SessionDependency,
) -> PaginatedHistory:
    """Historial paginado y filtrable (RF1, RF3, RF4).

    Ambos modelos se declaran con Depends() y no con Query(): FastAPI expande un
    solo modelo Query() por endpoint, y con dos dejaría de publicar `page`,
    `limit` y los filtros como parámetros sueltos.
    """
    total = await count_demand_events(session, filters)
    events = await list_demand_events(session, filters, pagination)

    return PaginatedHistory(
        items=[_to_response(event) for event in events],
        page=pagination.page,
        limit=pagination.limit,
        total=total,
        # División entera hacia arriba: evita el redondeo de un float intermedio.
        pages=(total + pagination.limit - 1) // pagination.limit,
    )


@router.get("/history/{event_id}", response_model=DemandEventOut)
async def read_history_entry(event_id: int, session: SessionDependency) -> DemandEventOut:
    """Detalle de un evento por el id generado por el sistema (RF2).

    Responde 404 si el id no existe.
    """
    event = await get_demand_event(session, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evento no encontrado")

    return _to_response(event)


@router.post(
    "/events",
    response_model=DemandEventOut,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "El idpk ya estaba guardado; no se creó una fila nueva"}},
)
async def create_event(
    event: DemandEventIn, response: Response, session: SessionDependency
) -> DemandEventOut:
    """Ingesta de un evento enviado por connector.

    Es idempotente: reenviar un idpk ya guardado devuelve el registro existente
    con 200 en vez de crear una fila nueva. Los dos códigos son éxito para
    connector, que puede hacer ack en ambos casos.
    """
    stored, created = await save_demand_event(session, event)
    if not created:
        response.status_code = status.HTTP_200_OK

    return _to_response(stored)


def _to_response(event: DemandEvent) -> DemandEventOut:
    """Arma la respuesta anidada a partir de la fila plana de la tabla.

    DemandEventOut.model_validate(event) no alcanza: la fila es plana y usa otro
    nombre para el tipo (`event_type`), mientras que el DTO anida el contenido
    del evento dentro de `packageBody`.
    """
    return DemandEventOut(
        id=event.id,
        idpk=event.idpk,
        type=event.event_type,
        received_at=event.received_at,
        package_body=PackageBody(
            demands=event.demands,
            valid_until=event.valid_until,
            meta_content=event.meta_content,
            constraints=event.constraints or {},
        ),
    )
