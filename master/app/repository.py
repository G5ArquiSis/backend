"""Queries del historial de demanda.

Es la misma capa de datos que `database.py`, separada solo por tamaño: allá vive
la conexión (engine, sesiones, esquema) y acá las consultas. No es una capa nueva
entre los routers y la base (CLAUDE.md seccion 4.3).

Todo el uso de SQLAlchemy sigue contenido en estos dos archivos (seccion 8).
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DemandEvent
from app.schemas import DemandEventIn, HistoryFilters, PageParams


async def save_demand_event(
    session: AsyncSession, event: DemandEventIn
) -> tuple[DemandEvent, bool]:
    """Persiste un evento de forma idempotente: un mismo idpk no duplica filas.

    Devuelve la fila y si fue creada en esta llamada; el router traduce ese bool
    a 201 o 200. Un solo INSERT ... ON CONFLICT resuelve la idempotencia de forma
    atómica: las dos réplicas de master comparten la base y pueden recibir el
    mismo idpk a la vez, así que consultar antes de insertar sería una carrera.

    `received_at` queda fuera del INSERT para que lo llene el server_default de la
    columna, y vuelve en el RETURNING de la misma ida y vuelta.
    """
    body = event.package_body
    statement = (
        postgres_insert(DemandEvent)
        .values(
            idpk=event.idpk,
            event_type=event.type,
            valid_until=body.valid_until,
            meta_content=body.meta_content,
            constraints=body.constraints,
            demands=[item.model_dump() for item in body.demands],
        )
        .on_conflict_do_nothing(index_elements=["idpk"])
        .returning(DemandEvent)
        .execution_options(populate_existing=True)
    )

    inserted = (await session.execute(statement)).scalars().first()
    await session.commit()
    if inserted is not None:
        return inserted, True

    # El INSERT no devolvió nada: el idpk ya estaba. ON CONFLICT espera a que la
    # transacción que lo insertó termine, así que este SELECT siempre lo ve.
    stored = await session.execute(select(DemandEvent).where(DemandEvent.idpk == event.idpk))
    return stored.scalar_one(), False


async def get_demand_event(session: AsyncSession, event_id: int) -> DemandEvent | None:
    """Busca un evento por el id generado por el sistema (RF2)."""
    return await session.get(DemandEvent, event_id)


async def list_demand_events(
    session: AsyncSession, filters: HistoryFilters, pagination: PageParams
) -> Sequence[DemandEvent]:
    """Devuelve una página del historial, siempre acotada con LIMIT/OFFSET (RF3).

    El curso envía miles de eventos: esta función nunca debe ejecutar un SELECT
    sin cota, ni siquiera cuando no hay filtros.
    """
    statement = _apply_filters(select(DemandEvent), filters)
    # El desempate por id no es cosmético: now() es constante dentro de una misma
    # transacción, así que dos eventos guardados juntos comparten received_at y
    # sin un orden total una fila podría salir en dos páginas o en ninguna.
    statement = statement.order_by(DemandEvent.received_at.desc(), DemandEvent.id.desc())
    statement = statement.limit(pagination.limit).offset((pagination.page - 1) * pagination.limit)

    return (await session.execute(statement)).scalars().all()


async def count_demand_events(session: AsyncSession, filters: HistoryFilters) -> int:
    """Cuenta los eventos que pasan los filtros, para el total de la paginación.

    Va como query aparte y no como `count(*) OVER ()` pegado al listado: la
    versión con ventana viaja en las filas devueltas, así que una página más allá
    del final informaría total 0, justo cuando el cliente necesita el total real.
    """
    statement = _apply_filters(select(func.count(DemandEvent.id)), filters)

    return (await session.execute(statement)).scalar_one()


def _apply_filters(statement: Select, filters: HistoryFilters) -> Select:
    """Agrega al SELECT una condición por cada filtro presente (RF4).

    `city` y `unit` viven dentro del JSONB `demands`, así que se resuelven con
    contención (`demands @> '[{"city": ...}]'`) apoyada en el índice GIN.
    """
    if filters.idpk is not None:
        statement = statement.where(DemandEvent.idpk == filters.idpk)
    if filters.type is not None:
        statement = statement.where(DemandEvent.event_type == filters.type)
    if filters.received_at is not None:
        start, end = _day_bounds(filters.received_at)
        statement = statement.where(DemandEvent.received_at >= start, DemandEvent.received_at < end)
    if filters.valid_until is not None:
        # valid_until es nullable y NULL >= start es NULL, así que los eventos sin
        # validUntil quedan fuera del filtro por sí solos.
        start, end = _day_bounds(filters.valid_until)
        statement = statement.where(DemandEvent.valid_until >= start, DemandEvent.valid_until < end)

    # city y unit se combinan en un solo elemento de contención: ?city=X&unit=Y
    # pide un evento donde una misma ciudad tenga esa unidad. Como dos condiciones
    # sueltas, bastaría que una demanda aportara la ciudad y otra distinta la
    # unidad. De paso, es una sola búsqueda contra el índice GIN.
    demand_item = {
        key: value
        for key, value in (("city", filters.city), ("unit", filters.unit))
        if value is not None
    }
    if demand_item:
        statement = statement.where(DemandEvent.demands.contains([demand_item]))

    return statement


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """Intervalo semiabierto [inicio, fin) del día, en UTC.

    Las columnas son TIMESTAMPTZ y el filtro llega como una fecha sin zona: se
    interpreta en UTC, que es lo que emite el broker y lo que devuelve la API, de
    modo que el filtro no dependa de la zona configurada en el host ni en Postgres.

    Se compara contra un rango en vez de truncar la columna con date() porque
    truncar inutilizaría el índice ix_demand_events_received_at.
    """
    start = datetime.combine(day, time.min, tzinfo=UTC)

    return start, start + timedelta(days=1)
