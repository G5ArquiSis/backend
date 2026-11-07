"""Pruebas de contrato del cliente wallet sin PostgreSQL."""

import httpx
import pytest

from app.wallet.client import WalletClient


@pytest.mark.asyncio
async def test_get_swap_contract():
    calls = []

    def responder(request):
        calls.append(request)
        return httpx.Response(200, json={"swapId": "s-1", "state": "settled"})

    async with WalletClient("https://wallet.example", "secret",
                            transport=httpx.MockTransport(responder)) as client:
        result = await client.get_swap("s-1")
        assert result["state"] == "settled"
        assert (await client.get_swap_id("s-1"))["swapId"] == "s-1"

    assert len(calls) == 2
    assert all(c.url.path == "/swaps/s-1" for c in calls)
    assert all(c.headers["X-Api-Key"] == "secret" for c in calls)


@pytest.mark.asyncio
async def test_write_requires_idempotency_key():
    async with WalletClient("https://wallet.example", "secret",
                            transport=httpx.MockTransport
                            (lambda _: httpx.Response(202, json={}))) as client:
        with pytest.raises(ValueError, match="Idempotency-Key"):
            await client.accept_swap("s-1", "")

@pytest.mark.asyncio
async def test_propose_swap_sends_correct_payload_and_idempotency_key():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(202, json={"swapId": "s-1"})

    async with WalletClient(
        "https://wallet.example/v1",
        "secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.propose_swap(
            give={"asset": "kwh", "amount": 10},
            take={"asset": "credits", "amount": 100},
            expires_at="2026-11-06T23:00:00Z",
            memo="offer-1",
            idempotency_key="bid-1",
        )

    assert result["swapId"] == "s-1"
    assert len(requests) == 1

    request = requests[0]

    assert request.method == "POST"
    assert request.url.path == "/v1/swaps"
    assert request.headers["Idempotency-Key"] == "bid-1"
    assert request.headers["X-Api-Key"] == "secret"

    import json

    assert json.loads(request.content) == {
        "give": {"asset": "kwh", "amount": 10},
        "take": {"asset": "credits", "amount": 100},
        "expiresAt": "2026-11-06T23:00:00Z",
        "memo": "offer-1",
    }