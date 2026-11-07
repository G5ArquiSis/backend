"""CycleClosed contra un Postgres real: una vez por ciclo, con o sin reporte, aunque las dos
réplicas revisen a la vez."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.config import get_settings
from app.cycle_repository import apply_central_message, claim_outbox, run_due_work
from app.database import _session_factory

pytestmark = pytest.mark.integration

SETTINGS = get_settings()
VALID_UNTIL = datetime(2026, 9, 1, 14, 20, tzinfo=UTC)
WINDOW_START = VALID_UNTIL - timedelta(minutes=20)
CLOSING = VALID_UNTIL - timedelta(minutes=4)
AFTER = VALID_UNTIL + timedelta(seconds=1)


@pytest.fixture
async def db(database: None) -> AsyncIterator[AsyncSession]:
    async with _session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE ledger_events, cycle_ledger, negotiations, outbox, message_log, "
                "distance_tables RESTART IDENTITY"
            )
        )
        await session.commit()
        yield session


@pytest.fixture
def closed(monkeypatch) -> list[dict]:
    events: list[dict] = []

    def sink(kind: str, attributes: dict) -> None:
        if kind == "CycleClosed":
            events.append(attributes)

    monkeypatch.setattr(metrics, "sink", sink)
    return events


async def open_cycle(db: AsyncSession, cycle_id: str) -> None:
    message = {
        "idpk": f"status-{cycle_id}",
        "msgId": f"msg-status-{cycle_id}",
        "type": "status-statement",
        "timestamp": "2026-09-01T14:00:00Z",
        "sender": "central",
        "cycleId": cycle_id,
        "data": {
            "energy": {"generationCapacity": 1000, "consumption": 400, "generationCost": 210},
            "validUntil": VALID_UNTIL.isoformat().replace("+00:00", "Z"),
        },
    }
    await apply_central_message(db, message, WINDOW_START, SETTINGS)


async def test_ciclo_con_reporte_se_informa_una_vez_al_cerrar(db, closed) -> None:
    await open_cycle(db, "cycle-1")
    await run_due_work(db, CLOSING, SETTINGS)
    await claim_outbox(db, CLOSING, SETTINGS)
    assert closed == []

    await run_due_work(db, AFTER, SETTINGS)
    await run_due_work(db, AFTER + timedelta(minutes=5), SETTINGS)

    [event] = closed
    assert event["cycleId"] == "cycle-1"
    assert event["owedReport"] is True
    assert event["reportSent"] is True


async def test_ciclo_sin_reporte_se_informa_como_tal(db, closed) -> None:
    """Si el reloj no corrió en el periodo de cierre, el ciclo cierra sin reporte (G03)."""
    await open_cycle(db, "cycle-2")

    await run_due_work(db, AFTER, SETTINGS)

    [event] = closed
    assert event["reportSent"] is False


async def test_dos_replicas_informan_cada_cierre_una_sola_vez(db, closed) -> None:
    await open_cycle(db, "cycle-3")

    async def replica() -> None:
        async with _session_factory() as other:
            await run_due_work(other, AFTER, SETTINGS)

    await asyncio.gather(*(replica() for _ in range(4)))

    assert [event["cycleId"] for event in closed] == ["cycle-3"]
