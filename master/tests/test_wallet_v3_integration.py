"""ROL B con PostgreSQL real: correr contra una base de pruebas desechable."""

import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.cycle_repository import apply_central_message, run_due_work
from app.database import _session_factory, engine
from app.migrations import run_migrations
from app.models import MessageLog, OutboxMessage, WalletBalance, WalletEvent, WalletStatements
from app.wallet.reconciliacion import reconcile

pytestmark = pytest.mark.integration
NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)


@pytest.fixture
async def wallet_db(database) -> AsyncIterator[AsyncSession]:
    await run_migrations(engine)
    async with _session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE wallet_events, wallet_balance, wallet_statements, "
                "wallet_reconciliations, wallet_sync_state, wallet_swap_cycles, "
                "cycle_market_messages, cycle_ledger, ledger_events, outbox, "
                "message_log, negotiations, distance_tables RESTART IDENTITY"
            )
        )
        await session.commit()
        yield session


def event(kind, key, data, seconds=0, cycle=None):
    message = {
        "type": kind,
        "idpk": key,
        "msgId": "msg-" + key,
        "sender": "central",
        "timestamp": (NOW + timedelta(seconds=seconds)).isoformat(),
        "data": data,
    }
    if cycle:
        message["cycleId"] = cycle
    return message


async def apply(db, message):
    return await apply_central_message(db, message, NOW, get_settings())


@pytest.mark.parametrize("seed", range(5))
async def test_permutations_duplicates_and_cycleless_events(wallet_db, seed):
    events = [
        event(
            "swap-proposed",
            "p",
            {
                "swapId": "s",
                "idpk": "operation",
                "balancesAfter": {
                    "CR": {"available": 100, "held": 0},
                    "KWH": {"available": 0, "held": 10},
                },
            },
            1,
        ),
        event(
            "swap-settled",
            "s",
            {
                "swapId": "s",
                "idpk": "operation",
                "balancesAfter": {
                    "CR": {"available": 200, "held": 0},
                    "KWH": {"available": 0, "held": 0},
                },
            },
            2,
        ),
        event("trade-recorded", "t", {"swapId": "s", "transportTotal": 10}, 3, "cycle-1"),
    ]
    messages = events * 2
    random.Random(seed).shuffle(messages)
    for message in messages:
        await apply(wallet_db, message)
    assert (await wallet_db.get(WalletBalance, "CR")).available == Decimal(200)
    assert (await wallet_db.execute(select(func.count()).select_from(WalletEvent))).scalar() == 3
    assert (await wallet_db.execute(select(func.count()).select_from(MessageLog))).scalar() == 3
    rows = (await wallet_db.execute(select(WalletEvent))).scalars().all()
    assert all(row.cycle_id == "cycle-1" for row in rows)


async def test_reconciliation_repairs_drift_and_imports_adjustment_once(wallet_db):
    wallet_db.add(WalletBalance(asset="CR", available=1, held=0))
    await wallet_db.commit()
    balance = {
        "assets": {
            "CR": {"available": 100, "held": 2, "creditLine": 50},
            "KWH": {"available": 0, "held": 0},
        },
        "asOf": NOW.isoformat(),
    }
    entry = {
        "entryId": "adjustment-1",
        "cycleId": "cycle-1",
        "asset": "CR",
        "kind": "adjustment",
        "amount": 101,
    }
    client = SimpleNamespace(
        get_balance=AsyncMock(return_value=balance),
        get_statement=AsyncMock(return_value={"entries": [entry], "nextCursor": None}),
    )
    first = await reconcile(wallet_db, client, cycle_id="cycle-1")
    assert Decimal(first["delta"]["CR"]["available"]) == 99
    assert first["entriesImported"] == 1
    second = await reconcile(wallet_db, client, cycle_id="cycle-1")
    assert second["entriesImported"] == 0
    assert (await wallet_db.get(WalletBalance, "CR")).available == 100
    assert (
        await wallet_db.execute(select(func.count()).select_from(WalletStatements))
    ).scalar() == 1


async def test_funding_penalty_is_not_subtracted_twice(wallet_db):
    await apply(
        wallet_db,
        event(
            "transfer",
            "fund",
            {
                "quantity": 50,
                "balanceAfter": 150,
                "creditLine": 200,
                "penalty": {"amount": 50, "forCycleId": "previous"},
            },
            cycle="cycle-1",
        ),
    )
    row = await wallet_db.get(WalletBalance, "CR")
    assert row.available == 150 and row.credit_line == 200


async def test_close_and_report_use_official_balance(wallet_db, client):
    status = event(
        "status-statement",
        "status",
        {
            "energy": {
                "generationCapacity": 100,
                "consumption": 50,
                "generationCost": 10,
                "sellable": 50,
            },
            "market": {"reportOpensAt": NOW.isoformat()},
            "validUntil": (NOW + timedelta(minutes=5)).isoformat(),
        },
        cycle="cycle-1",
    )
    await apply(wallet_db, status)
    await apply(
        wallet_db,
        event(
            "imbalance-settlement",
            "close",
            {
                "balanceAfter": 500,
                "total": 100,
                "kwhHeld": 10,
                "deficit": 0,
                "coverage": 10,
                "pricePerEnergy": 10,
                "settlementId": "settlement",
            },
            seconds=1,
            cycle="cycle-1",
        ),
    )
    await run_due_work(wallet_db, NOW + timedelta(seconds=20), get_settings())
    report = (await wallet_db.execute(select(OutboxMessage))).scalar_one()
    assert report.payload["data"] == {"budgetBalance": 500, "energyBalance": 0}
    response = await client.get("/cycles/cycle-1")
    assert response.status_code == 200
    detail = response.json()
    assert detail["reportSentAt"] is None
    assert detail["reportQueuedAt"] is not None
    assert detail["lastOperationIdpk"] == "close"
    assert any(op["isLast"] and op["kind"] == "imbalance-settlement" for op in detail["operations"])
    assert Decimal(detail["balances"]["budget"]) == 500


async def test_scheduler_reconciles_once_until_interval_or_new_close(wallet_db, monkeypatch):
    from app.wallet.scheduler import reconcile_due

    fake = SimpleNamespace(
        get_balance=AsyncMock(
            return_value={
                "assets": {"CR": {"available": 10, "held": 0}, "KWH": {"available": 0, "held": 0}},
                "asOf": datetime.now(UTC).isoformat(),
            }
        ),
        get_statement=AsyncMock(return_value={"entries": [], "nextCursor": None}),
    )

    class FakeClient:
        async def __aenter__(self):
            return fake

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr("app.wallet.scheduler.WalletClient", lambda *_: FakeClient())
    settings = get_settings().model_copy(
        update={
            "wallet_enabled": True,
            "wallet_base_url": "https://fake.invalid/v1",
            "wallet_api_key": "local-test",
        }
    )
    await reconcile_due(settings)
    await reconcile_due(settings)
    assert fake.get_balance.await_count == 1


async def test_concurrent_first_snapshots_and_equal_timestamp(wallet_db):
    import asyncio

    async def write(key, amount):
        async with _session_factory() as session:
            await apply(
                session,
                event(
                    "swap-settled",
                    key,
                    {"swapId": key, "balancesAfter": {"CR": {"available": amount, "held": 0}}},
                ),
            )

    await asyncio.gather(write("a", 100), write("z", 200))
    row = await wallet_db.get(WalletBalance, "CR", populate_existing=True)
    assert row.available == 200 and row.event_key == "z"
