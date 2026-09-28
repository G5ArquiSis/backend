"""Tests del historial de demanda que no necesitan una base de datos.

Verifican que la app expone lo que la rúbrica va a consultar, que los filtros se
traducen al SQL que corresponde y que la presentación arma bien la respuesta. Los
que sí tocan Postgres viven en `test_history_integration.py`.
"""

from datetime import UTC, date, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models import DemandEvent
from app.repository import _apply_filters, list_demand_events
from app.routers.history import _to_response
from app.schemas import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, HistoryFilters, PageParams


def test_rubric_routes_are_registered(app: FastAPI) -> None:
    """Las rutas que evalúa la rúbrica quedan expuestas por la app ensamblada."""
    paths = app.openapi()["paths"]

    assert {"/health", "/history", "/history/{event_id}", "/events"} <= set(paths)


def test_history_publishes_pagination_and_filters_as_query_params(app: FastAPI) -> None:
    """RF3 y RF4: los modelos se expanden a queryParams sueltos, no a un objeto.

    Si `page`, `limit` o los filtros dejaran de aparecer acá, la API seguiría
    respondiendo 200 pero ignorando la paginación y los filtros por completo.
    """
    parameters = app.openapi()["paths"]["/history"]["get"]["parameters"]
    names = {parameter["name"] for parameter in parameters}

    assert {"page", "limit"} <= names
    assert {"receivedAt", "validUntil", "city", "unit", "type", "idpk"} <= names


def test_date_filter_becomes_a_half_open_day_and_city_becomes_containment() -> None:
    """RF4: el día es un rango [00:00, 00:00 del día siguiente) en UTC.

    Compilar el SELECT es la única forma de afirmar que el filtro no terminó como
    un `date(received_at) = ...`, que daría el mismo resultado en un test con
    pocas filas pero dejaría inutilizable el índice temporal.
    """
    filters = HistoryFilters(received_at=date(2026, 8, 18), city="Los Santos")

    compiled = _apply_filters(select(DemandEvent), filters).compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "@>" in sql
    assert "date(" not in sql.lower()
    assert datetime(2026, 8, 18, tzinfo=UTC) in compiled.params.values()
    assert datetime(2026, 8, 19, tzinfo=UTC) in compiled.params.values()


def test_city_and_unit_filter_the_same_demand_item() -> None:
    """?city=X&unit=Y pide una demanda que tenga ambas, no dos demandas distintas."""
    filters = HistoryFilters(city="Los Santos", unit="GW")

    compiled = _apply_filters(select(DemandEvent), filters).compile(dialect=postgresql.dialect())

    assert str(compiled).count("@>") == 1
    assert [{"city": "Los Santos", "unit": "GW"}] in compiled.params.values()


def test_response_nests_the_flat_row_into_package_body() -> None:
    """La fila es plana y el DTO es anidado: _to_response es quien traduce."""
    row = DemandEvent(
        id=7,
        idpk="3f2504e0",
        event_type="demand-set",
        received_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        valid_until=datetime(2026, 12, 12, tzinfo=UTC),
        meta_content="algo",
        constraints={},
        demands=[{"city": "Los Santos", "demand": 10223.0, "unit": "GW"}],
    )

    response = _to_response(row)

    assert response.id == 7
    assert response.type == "demand-set"
    assert response.package_body.demands[0].city == "Los Santos"
    assert response.package_body.meta_content == "algo"


async def test_history_rejects_limit_above_maximum(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un limit sobre MAX_PAGE_SIZE se rechaza con 422, no derriba la base.

    El espía comprueba lo que la prosa no puede: que el 422 ocurre antes de tocar
    Postgres, y no después de haber pedido un SELECT sin cota.
    """
    assert MAX_PAGE_SIZE == 100
    reached_database = False

    async def spy(*args: object, **kwargs: object) -> int:
        nonlocal reached_database
        reached_database = True
        return 0

    monkeypatch.setattr("app.routers.history.count_demand_events", spy)

    response = await client.get(f"/history?limit={MAX_PAGE_SIZE + 1}")

    assert response.status_code == 422
    assert not reached_database


async def test_history_rejects_page_below_one(client: httpx.AsyncClient) -> None:
    """La otra frontera de PageParams: page arranca en 1."""
    response = await client.get("/history?page=0")

    assert response.status_code == 422


async def test_history_rejects_non_numeric_page(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """page="abc" es un 422 controlado, no un 500: Pydantic lo rechaza antes de la query."""
    reached_database = False

    async def spy(*args: object, **kwargs: object) -> int:
        nonlocal reached_database
        reached_database = True
        return 0

    monkeypatch.setattr("app.routers.history.count_demand_events", spy)

    response = await client.get("/history?page=abc")

    assert response.status_code == 422
    assert not reached_database


async def test_history_rejects_non_numeric_limit(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """limit="abc" es la misma frontera que page, del lado de PageParams.limit."""
    reached_database = False

    async def spy(*args: object, **kwargs: object) -> int:
        nonlocal reached_database
        reached_database = True
        return 0

    monkeypatch.setattr("app.routers.history.count_demand_events", spy)

    response = await client.get("/history?limit=abc")

    assert response.status_code == 422
    assert not reached_database


async def test_history_rejects_negative_limit(client: httpx.AsyncClient) -> None:
    """limit=-1 es un 422 controlado por el mismo `ge=1` que rechaza limit=0."""
    response = await client.get("/history?limit=-1")

    assert response.status_code == 422


async def test_unknown_filter_is_ignored_rather_than_rejected(app: FastAPI) -> None:
    """RF4: un query param que no corresponde a ningún filtro se ignora, no es error.

    Decisión intencional y no un accidente: HistoryFilters no declara `model_config
    extra="forbid"`, así que FastAPI/Pydantic descartan cualquier query param sin
    match en el modelo en vez de responder 422. Se documenta acá con una aserción
    sobre el modelo en sí, para que cambiar ese comportamiento sea una decisión
    explícita y no una regresión silenciosa.
    """
    filters = HistoryFilters.model_validate({"city": "Los Santos", "planet": "Tierra"})

    assert filters.city == "Los Santos"
    assert not hasattr(filters, "planet")


async def test_missing_event_responds_404(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RF2: un id que no existe es 404, no un 500 ni un 200 vacío."""

    async def missing(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.routers.history.get_demand_event", missing)

    response = await client.get("/history/999999")

    assert response.status_code == 404


def test_default_page_size_is_the_one_the_rubric_asks_for() -> None:
    """RF3: el historial se pagina de a 25 salvo que pidan otra cosa."""
    assert DEFAULT_PAGE_SIZE == 25


async def test_list_demand_events_always_compiles_with_limit_and_offset() -> None:
    """RF3: ningún camino de list_demand_events puede llegar a un SELECT sin cota.

    Se captura el Select real que la función pasa a session.execute con una
    sesión doble mínima, en vez de reconstruir el statement a mano: así el test
    falla si alguien agrega una rama nueva a list_demand_events que se salte el
    LIMIT/OFFSET, no solo si cambia la firma que este test ya conoce.
    """
    captured: list[object] = []

    class ResultDouble:
        def scalars(self) -> "ResultDouble":
            return self

        def all(self) -> list:
            return []

    class SessionDouble:
        async def execute(self, statement: object) -> ResultDouble:
            captured.append(statement)
            return ResultDouble()

    await list_demand_events(SessionDouble(), HistoryFilters(), PageParams(page=2, limit=10))

    assert len(captured) == 1
    compiled = str(captured[0].compile(dialect=postgresql.dialect()))
    assert "LIMIT" in compiled.upper()
    assert "OFFSET" in compiled.upper()
