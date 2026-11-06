"""Casos v3 de transporte, desorden, correlación y reconciliación."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.wallet.balances import update_balances
from app.wallet.client import WalletClient, WalletError, retry_after
from app.wallet.history import swap_states
from app.wallet.reconciliacion import reconcile


@pytest.mark.parametrize("failure", [429, 502, 503, 504, "timeout"])
async def test_retries_preserve_operation_and_measure_every_attempt(monkeypatch, failure):
    sleep = AsyncMock()
    monkeypatch.setattr("app.wallet.client.asyncio.sleep", sleep)
    requests, metrics = [], []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("respuesta perdida", request=request)
            return httpx.Response(failure, headers={"Retry-After": "2"})
        return httpx.Response(202, json={"swapId": "s-1", "status": "accepted"})

    async with WalletClient(
        "https://wallet.example/v1",
        "retry-test",
        transport=httpx.MockTransport(handler),
        hook=lambda *values: metrics.append(values),
    ) as client:
        result = await client.accept_swap("s-1", "same-key")
    assert result["status"] == "accepted"  # nunca inferir settled de 202
    assert [r.headers["Idempotency-Key"] for r in requests] == ["same-key"] * 2
    assert all(r.url.path == "/v1/swaps/s-1/accept" for r in requests)
    assert len(metrics) == 2
    sleep.assert_awaited_once()


async def test_permanent_error_does_not_retry():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(422, json={"reason": "IDEMPOTENCY_MISMATCH"})

    async with WalletClient(
        "https://wallet.example", "permanent", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(WalletError) as caught:
            await client.cancel_swap("s", "key")
    assert caught.value.status_code == 422
    assert len(requests) == 1


async def test_client_instances_share_budget():
    async with (
        WalletClient("https://wallet.example", "shared") as one,
        WalletClient("https://wallet.example", "shared") as two,
    ):
        assert one._reads is two._reads
        assert one._writes is two._writes
        assert one._reads.maximum * 2 <= 300
        assert one._writes.maximum * 2 <= 120


def test_retry_after_invalid_and_numeric():
    assert retry_after("2.5", 1) == 2.5
    assert retry_after("not-a-date", 1) == 1
    assert retry_after("-1", 1) == 0


def test_swap_settlement_wins_over_out_of_order_proposal():
    from itertools import permutations

    events = [
        {"type": kind, "data": {"swapId": "s"}}
        for kind in ["swap-proposed", "swap-settled", "swap-rejected"]
    ]
    for order in permutations(events):
        [swap] = swap_states([*order, *order])
        assert swap["status"] == "settled"


async def test_older_snapshot_cannot_overwrite_newer_balance():
    row = SimpleNamespace(
        available=Decimal("100"),
        held=Decimal("5"),
        as_of=datetime(2026, 11, 6, 12, tzinfo=UTC),
        source="balance",
    )
    session = SimpleNamespace(
        execute=AsyncMock(), get=AsyncMock(return_value=row), flush=AsyncMock()
    )
    await update_balances(
        session, {"CR": {"available": 1, "held": 0}}, datetime(2026, 11, 6, 11, tzinfo=UTC), "event"
    )
    assert row.available == 100 and row.held == 5


async def test_pagination_failure_does_not_publish_snapshot():
    session = SimpleNamespace(execute=AsyncMock(), get=AsyncMock(), commit=AsyncMock())
    client = SimpleNamespace(
        get_balance=AsyncMock(return_value={"assets": {"CR": {"available": 1, "held": 0}}}),
        get_statement=AsyncMock(return_value={"entries": [], "nextCursor": "repeat"}),
    )
    with pytest.raises(ValueError, match="cursor repetido"):
        await reconcile(session, client)
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
