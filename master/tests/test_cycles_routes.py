"""Tests de las rutas HTTP de ciclos (/cycles) y registro de mensajes (/message-log).

Verifica que las rutas están registradas, sus respuestas coinciden con el OpenAPI
y con lo que consume el frontend (CycleHistory.jsx y Messages.jsx).
"""

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CycleLedger, DuplicateMessage, LedgerEvent


def test_cycles_routes_are_registered(app: FastAPI) -> None:
    """Las rutas de ciclos y log de mensajes quedan registradas en la app."""
    paths = app.openapi()["paths"]

    assert {"/cycles", "/cycles/{cycle_id}", "/message-log"} <= set(paths)


def test_cycles_publishes_page_and_limit_parameters(app: FastAPI) -> None:
    """GET /cycles publica parámetros sueltos de paginación."""
    parameters = app.openapi()["paths"]["/cycles"]["get"]["parameters"]
    names = {p["name"] for p in parameters}

    assert {"page", "limit"} <= names


def test_message_log_publishes_category_and_pagination_parameters(app: FastAPI) -> None:
    """GET /message-log publica parámetros sueltos para Messages.jsx."""
    parameters = app.openapi()["paths"]["/message-log"]["get"]["parameters"]
    names = {p["name"] for p in parameters}

    assert {"page", "limit", "category"} <= names


@pytest.mark.integration
async def test_get_cycles_empty_list(client: httpx.AsyncClient, session: AsyncSession) -> None:
    """GET /cycles devuelve lista vacía si no hay ciclos."""
    response = await client.get("/cycles")
    assert response.status_code == 200

    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["page"] == 1


@pytest.mark.integration
async def test_get_cycles_with_data_matches_frontend_contract(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """GET /cycles devuelve los campos requeridos por CycleHistory.jsx (opened, energyBalance)."""
    cycle = CycleLedger(
        cycle_id="cycle-9431",
        budget_balance=250000.0,
        energy_balance=1500,
        is_closed=False,
    )
    session.add(cycle)
    await session.commit()

    response = await client.get("/cycles")
    assert response.status_code == 200

    data = response.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["cycleId"] == "cycle-9431"
    # Campos que lee CycleHistory.jsx:
    assert item["opened"] is True
    assert item["energyBalance"] == 1500


@pytest.mark.integration
async def test_get_cycle_detail_matches_frontend_contract(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """GET /cycles/{cycleId} devuelve balances, transfers, operations requeridos por CycleHistory.jsx."""
    cycle = CycleLedger(
        cycle_id="cycle-9431",
        budget_balance=250000.0,
        energy_balance=1500,
        generation_capacity=1000000,
        consumption=800000,
        generation_cost=210.0,
        last_operation_idpk="op-last-idpk",
        is_closed=False,
    )
    session.add(cycle)
    await session.flush()

    event = LedgerEvent(
        idpk="op-last-idpk",
        msg_id="msg-op-1",
        cycle_id="cycle-9431",
        event_type="demand-statement",
        delta_budget=-210000.0,
        delta_energy=1000,
        resulting_budget=250000.0,
        resulting_energy=1500,
        payload={"balance": {"quantity": 1000, "valuePerKwh": 210.0}},
        description="demand-statement",
        applied_at=datetime.now(UTC),
    )
    session.add(event)
    await session.commit()

    response = await client.get("/cycles/cycle-9431")
    assert response.status_code == 200

    data = response.json()
    assert data["cycleId"] == "cycle-9431"
    # Propiedades usadas por CycleHistory.jsx:
    assert data["opened"] is True
    assert data["balances"]["energy"] == 1500
    assert data["balances"]["budget"] == 250000.0
    assert data["lastOperationIdpk"] == "op-last-idpk"
    assert len(data["operations"]) == 1
    assert data["operations"][0]["idpk"] == "op-last-idpk"
    assert data["operations"][0]["isLast"] is True
    assert data["operations"][0]["kind"] == "demand-statement"


@pytest.mark.integration
async def test_get_cycle_not_found(client: httpx.AsyncClient, session: AsyncSession) -> None:
    """GET /cycles/{cycleId} devuelve 404 si el ciclo no existe."""
    response = await client.get("/cycles/cycle-nonexistent")
    assert response.status_code == 404


@pytest.mark.integration
async def test_message_log_matches_messages_jsx(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """GET /message-log devuelve campos requeridos por Messages.jsx (RF05)."""
    dup = DuplicateMessage(
        idpk="dup-idpk-123",
        msg_id="msg-dup-123",
        event_type="demand-statement",
        cycle_id="cycle-9431",
        reason="DUPLICATE_IDPK",
        details={"balance": {"quantity": 100}},
    )
    session.add(dup)
    await session.commit()

    response = await client.get("/message-log?category=duplicate")
    assert response.status_code == 200

    data = response.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["category"] == "duplicate"
    assert item["idpk"] == "dup-idpk-123"
    assert item["messageType"] == "demand-statement"
    assert item["reason"] == "DUPLICATE_IDPK"
    assert "receivedAt" in item
