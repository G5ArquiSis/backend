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
