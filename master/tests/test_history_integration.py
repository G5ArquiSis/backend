"""Tests del historial contra un Postgres real.

Van aparte de `test_history.py` porque necesitan la base: el filtro por ciudad usa
contención JSONB, la idempotencia depende del índice único sobre idpk y el filtro
temporal depende de TIMESTAMPTZ. Ninguna de las tres cosas existe fuera de
Postgres, así que un doble en memoria estaría probando otro programa.

Sin base alcanzable la fixture `database` los salta; ver README para levantarla.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DemandEvent
from app.schemas import DEFAULT_PAGE_SIZE

pytestmark = pytest.mark.integration

DAY = datetime(2026, 8, 18, tzinfo=UTC)


def an_event(idpk: str, city: str = "Los Santos", unit: str = "GW") -> dict:
    """Cuerpo de POST /events tal como lo manda connector, en camelCase."""
    return {
        "idpk": idpk,
        "type": "demand-set",
        "packageBody": {
            "demands": [{"city": city, "demand": 1013.123, "unit": unit}],
            "validUntil": "2026-12-12T00:00:00Z",
            "metaContent": "...",
            "constraints": {},
        },
    }


async def store_events(session: AsyncSession, total: int) -> None:
    """Deja `total` eventos en la tabla, con received_at distinto y creciente."""
    session.add_all(
        [
            DemandEvent(
                idpk=f"evento-{number:03d}",
                event_type="demand-set",
                received_at=DAY + timedelta(minutes=number),
                valid_until=None,
                meta_content=None,
                constraints={},
                demands=[{"city": "Los Santos", "demand": float(number), "unit": "GW"}],
            )
            for number in range(total)
        ]
    )
    await session.commit()


async def test_history_returns_default_page_size_without_query_params(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """RF3: sin queryParams, GET /history trae DEFAULT_PAGE_SIZE registros."""
    assert DEFAULT_PAGE_SIZE == 25
    await store_events(session, 30)

    body = (await client.get("/history")).json()

    assert len(body["items"]) == DEFAULT_PAGE_SIZE
    assert body["total"] == 30
    assert body["pages"] == 2
    assert body["page"] == 1


async def test_last_page_returns_remaining_records(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Frontera de paginación: la última página trae el resto, no una página vacía."""
    await store_events(session, 30)

    body = (await client.get("/history?page=2")).json()

    assert len(body["items"]) == 5
    assert body["total"] == 30


async def test_pages_do_not_overlap(client: httpx.AsyncClient, session: AsyncSession) -> None:
    """Ninguna fila puede aparecer en dos páginas: por eso el orden desempata por id."""
    await store_events(session, 30)

    first = (await client.get("/history?page=1")).json()["items"]
    second = (await client.get("/history?page=2")).json()["items"]

    ids = [item["id"] for item in first + second]
    assert len(ids) == len(set(ids)) == 30


async def test_custom_limit_controls_the_page_size(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """RF3: ?limit=10 devuelve bloques de 10, no el DEFAULT_PAGE_SIZE."""
    await store_events(session, 30)

    body = (await client.get("/history?limit=10")).json()

    assert len(body["items"]) == 10
    assert body["limit"] == 10
    assert body["pages"] == 3


async def test_page_beyond_the_last_returns_an_empty_list_not_an_error(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Una página fuera de rango es una lista vacía con 200, nunca un error."""
    await store_events(session, 30)

    response = await client.get("/history?page=999")
    body = response.json()

    assert response.status_code == 200
    assert body["items"] == []
    assert body["total"] == 30


async def test_received_at_filter_covers_the_whole_day(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """RF4: ?receivedAt=YYYY-MM-DD incluye 00:00:00 y excluye el día siguiente."""
    borders = {
        "vispera": DAY - timedelta(microseconds=1),
        "primer-instante": DAY,
        "ultimo-instante": DAY + timedelta(days=1) - timedelta(microseconds=1),
        "dia-siguiente": DAY + timedelta(days=1),
    }
    session.add_all(
        [
            DemandEvent(
                idpk=name,
                event_type="demand-set",
                received_at=moment,
                constraints={},
                demands=[],
            )
            for name, moment in borders.items()
        ]
    )
    await session.commit()

    body = (await client.get(f"/history?receivedAt={DAY.date().isoformat()}")).json()

    assert {item["idpk"] for item in body["items"]} == {"primer-instante", "ultimo-instante"}


async def test_date_filter_combined_with_pagination_stays_within_the_filtered_set(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """RF4 + RF3: filtrar por día y paginar da un total y un segundo bloque coherentes.

    Se mezclan eventos de dos días distintos para que el filtro realmente reduzca
    el universo antes de paginar; si el filtro se ignorara, page=2 traería filas
    del otro día y el total no coincidiría con los 15 del día filtrado.
    """
    other_day = DAY + timedelta(days=1)
    session.add_all(
        [
            DemandEvent(
                idpk=f"filtrado-{number:03d}",
                event_type="demand-set",
                received_at=DAY + timedelta(minutes=number),
                constraints={},
                demands=[],
            )
            for number in range(15)
        ]
        + [
            DemandEvent(
                idpk=f"otro-dia-{number:03d}",
                event_type="demand-set",
                received_at=other_day + timedelta(minutes=number),
                constraints={},
                demands=[],
            )
            for number in range(5)
        ]
    )
    await session.commit()

    body = (
        await client.get(f"/history?receivedAt={DAY.date().isoformat()}&page=2&limit=10")
    ).json()

    assert body["total"] == 15
    assert len(body["items"]) == 5
    assert all(item["idpk"].startswith("filtrado-") for item in body["items"])


async def test_unknown_query_param_is_ignored_and_history_responds_normally(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Un query param sin match en HistoryFilters se ignora (RF4, ver test_history.py)."""
    await store_events(session, 5)

    response = await client.get("/history?planet=Tierra")
    body = response.json()

    assert response.status_code == 200
    assert body["total"] == 5


async def test_city_filter_uses_jsonb_containment(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """RF4: el filtro por ciudad entra al JSONB, no a una columna nativa."""
    await client.post("/events", json=an_event("santos", city="Los Santos"))
    await client.post("/events", json=an_event("futurama", city="New New York"))

    body = (await client.get("/history?city=Los%20Santos")).json()

    assert body["total"] == 1
    assert body["items"][0]["idpk"] == "santos"


async def test_posting_the_same_idpk_twice_does_not_duplicate_rows(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Idempotencia: un reintento de connector no crea una segunda fila."""
    payload = an_event("3f2504e0-4f89-11d3-9a0c-0305e82c3301")

    created = await client.post("/events", json=payload)
    retried = await client.post("/events", json=payload)

    assert created.status_code == 201
    assert retried.status_code == 200
    assert created.json()["id"] == retried.json()["id"]

    stored = await session.execute(select(func.count(DemandEvent.id)))
    assert stored.scalar_one() == 1


async def test_concurrent_posts_of_one_idpk_store_a_single_row(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """La carrera que motivó el ON CONFLICT: las dos réplicas comparten la base."""
    payload = an_event("carrera")

    responses = await asyncio.gather(
        client.post("/events", json=payload), client.post("/events", json=payload)
    )

    assert sorted(response.status_code for response in responses) == [200, 201]

    stored = await session.execute(select(func.count(DemandEvent.id)))
    assert stored.scalar_one() == 1


async def test_detail_returns_the_event_and_404_when_missing(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """RF2: el detalle usa el id generado por el sistema, no el idpk del broker."""
    created = (await client.post("/events", json=an_event("detalle"))).json()

    found = await client.get(f"/history/{created['id']}")
    missing = await client.get("/history/999999")

    assert found.status_code == 200
    assert found.json()["idpk"] == "detalle"
    assert found.json()["packageBody"]["demands"][0]["unit"] == "GW"
    assert missing.status_code == 404
