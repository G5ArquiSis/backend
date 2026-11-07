"""Pruebas de los endpoints de administración de estrategia (C5)."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.market.models import SellerBid


async def test_strategy_endpoints_create_versions_and_update_offer(
    app, client, session: AsyncSession
):
    await session.execute(
        text(
            "TRUNCATE cycle_strategy_snapshots, strategy_state, strategy_versions, "
            "seller_bids RESTART IDENTITY CASCADE"
        )
    )
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        response = await client.get("/strategy")
        assert response.status_code == 200
        assert response.json()["version"] == 1
        assert response.json()["config"]["priceMode"] == "market_fraction"

        response = await client.put(
            "/strategy",
            json={
                "priceMode": "fixed",
                "pricePerEnergy": "510.25",
                "priceFactor": "0.5",
                "offerQuantity": 800,
                "bidAcceptancePolicy": "arrival_order",
                "buyWaitSpanSeconds": 45,
                "offerSelectionPolicy": "lowest_effective_price",
            },
        )
        assert response.status_code == 200
        assert response.json()["version"] == 2
        assert response.json()["config"]["pricePerEnergy"] == "510.25"

        response = await client.patch(
            "/strategy/offer",
            json={
                "priceMode": "market_fraction",
                "pricePerEnergy": None,
                "priceFactor": "0.75",
                "offerQuantity": 400,
            },
        )
        assert response.status_code == 200
        assert response.json()["version"] == 3
        assert response.json()["priceMode"] == "market_fraction"
        assert response.json()["priceFactor"] == "0.75"
        assert response.json()["offerQuantity"] == 400

        response = await client.get("/strategy/offer")
        assert response.status_code == 200
        assert response.json()["version"] == 3
        assert response.json()["offerQuantity"] == 400
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_strategy_swaps_lists_non_terminal_seller_bids(app, client, session: AsyncSession):
    await session.execute(
        text(
            "TRUNCATE cycle_strategy_snapshots, strategy_state, strategy_versions, "
            "seller_bids RESTART IDENTITY CASCADE"
        )
    )
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        session.add(
            SellerBid(
                idpk="c5-bid-001",
                msg_id="c5-msg-001",
                cycle_id="cycle-c5-001",
                offer_id="TAL-001",
                buyer_city="MET",
                quantity=125,
                price_per_energy="333.25",
                status="proposed",
                payload={},
                swap_id="swap-c5-001",
            )
        )
        session.add(
            SellerBid(
                idpk="c5-bid-002",
                msg_id="c5-msg-002",
                cycle_id="cycle-c5-001",
                offer_id="TAL-001",
                buyer_city="ABC",
                quantity=50,
                price_per_energy="333.25",
                status="settled",
                payload={},
                swap_id="swap-c5-002",
            )
        )
        await session.commit()

        response = await client.get("/strategy/swaps")
        assert response.status_code == 200
        items = response.json()
        assert len(items) == 1
        assert items[0]["swapId"] == "swap-c5-001"
        assert items[0]["status"] == "proposed"
    finally:
        app.dependency_overrides.pop(get_session, None)
