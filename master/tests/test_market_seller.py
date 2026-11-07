"""Pruebas de la publicación y mantenimiento de la oferta propia (C2)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.cycle_repository import APPLIED, apply_central_message
from app.database import _session_factory
from app.market.models import SellerOffer
from app.market.seller import (
    ACTIVE,
    CLOSED,
    NO_OFFER,
    calculate_offer_price,
    calculate_offer_quantity,
    change_offer_price,
    ensure_offer_for_cycle,
    set_offer_remaining,
    withdraw_offer,
)
from app.market.strategy import StrategyConfig
from app.models import OutboxMessage

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 6, 19, 0, tzinfo=UTC)
LAST_OFFER_AT = NOW + timedelta(minutes=8)
CYCLE_ID = "cycle-9431"
SETTINGS = get_settings().model_copy(update={"market_enabled": True, "city_code": "TAL"})


def status_message(
    *,
    cycle_id: str = CYCLE_ID,
    sellable: int = 600,
    buyback_price: str = "220.50",
    imbalance_price: str = "735.00",
    last_offer_at: datetime = LAST_OFFER_AT,
    sellable_in_market: bool = False,
) -> dict[str, Any]:
    energy = {"generationCapacity": 1000, "consumption": 400, "generationCost": 210}
    market = {
        "buybackPrice": buyback_price,
        "imbalancePrice": imbalance_price,
        "minBid": 10,
        "lastOfferAt": last_offer_at.isoformat().replace("+00:00", "Z"),
        "lastBidAt": (last_offer_at + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
        "closesAt": (last_offer_at + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        "reportOpensAt": (last_offer_at + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    }
    if sellable_in_market:
        market["sellable"] = sellable
    else:
        energy["sellable"] = sellable
    return {
        "idpk": f"status-{cycle_id}",
        "msgId": f"message-{cycle_id}",
        "type": "status-statement",
        "timestamp": NOW.isoformat().replace("+00:00", "Z"),
        "cityId": "central",
        "cycleId": cycle_id,
        "data": {
            "energy": energy,
            "market": market,
            "validUntil": (last_offer_at + timedelta(minutes=7)).isoformat().replace("+00:00", "Z"),
        },
    }


@pytest.fixture
async def db(database: None):
    async with _session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE seller_offers, cycle_market_messages, ledger_events, "
                "cycle_ledger, wallet_events, outbox RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
        yield session


def test_default_strategy_uses_middle_of_market_price_interval() -> None:
    strategy = StrategyConfig()
    assert calculate_offer_price(Decimal("220.50"), Decimal("735"), strategy) == Decimal(
        "477.75"
    )


def test_fixed_strategy_is_clamped_to_cycle_price_limits() -> None:
    strategy = StrategyConfig(price_mode="fixed", price_per_energy=Decimal("900"))
    assert calculate_offer_price(Decimal("220.50"), Decimal("735"), strategy) == Decimal(
        "735.00"
    )


def test_configured_quantity_never_exceeds_sellable() -> None:
    assert calculate_offer_quantity(600, StrategyConfig(offer_quantity=400)) == 400
    assert calculate_offer_quantity(350, StrategyConfig(offer_quantity=400)) == 350
    assert calculate_offer_quantity(0, StrategyConfig()) == 0
    assert calculate_offer_quantity("12.9", StrategyConfig()) == 12


async def test_status_statement_data_publishes_one_offer_to_market_outbox(db: AsyncSession) -> None:
    message = status_message()

    assert await apply_central_message(db, message, NOW, SETTINGS) == APPLIED
    offer = (await db.execute(select(SellerOffer))).scalar_one()
    queued = (await db.execute(select(OutboxMessage))).scalars().all()
    assert offer.cycle_id == CYCLE_ID
    assert offer.offer_id == f"TAL-{CYCLE_ID}"
    assert offer.quantity == offer.remaining == 600
    assert offer.price_per_energy == Decimal("477.75")
    assert offer.status == ACTIVE
    assert offer.publish_version == 1
    [outbox] = queued
    from app.outbox import Destination

    assert Destination.MARKET_OFFER.resolve() == ("market", "market.offer.TAL")

    if hasattr(OutboxMessage, "exchange"):
        assert outbox.exchange == "market"

    if hasattr(OutboxMessage, "routing_key"):
        assert outbox.routing_key == "market.offer.TAL"
    assert outbox.payload["type"] == "offer"
    assert outbox.payload["cycleId"] == CYCLE_ID
    assert outbox.payload["data"] == {
        "offerId": f"TAL-{CYCLE_ID}",
        "quantity": 600,
        "pricePerEnergy": 477.75,
    }
    assert outbox.payload["idpk"] != outbox.payload["msgId"]
    assert outbox.payload["cityId"] == "TAL"


async def test_status_statement_accepts_sellable_from_market_extension(db: AsyncSession) -> None:
    message = status_message(sellable=250, sellable_in_market=True)
    offer = await ensure_offer_for_cycle(db, message, NOW, SETTINGS)

    assert offer is not None
    assert offer.quantity == offer.remaining == 250


async def test_offer_is_created_only_once_per_cycle(db: AsyncSession) -> None:
    message = status_message()
    first = await ensure_offer_for_cycle(db, message, NOW, SETTINGS)
    second = await ensure_offer_for_cycle(db, message, NOW, SETTINGS)

    count = len((await db.execute(select(OutboxMessage))).scalars().all())
    assert first is not None and second is not None
    assert first.offer_id == second.offer_id
    assert count == 1


async def test_zero_sellable_does_not_publish_an_offer(db: AsyncSession) -> None:
    offer = await ensure_offer_for_cycle(db, status_message(sellable=0), NOW, SETTINGS)

    assert offer is not None
    assert offer.status == NO_OFFER
    assert offer.offer_id is None
    assert (await db.execute(select(OutboxMessage))).scalars().all() == []


async def test_status_received_after_last_offer_at_does_not_publish(db: AsyncSession) -> None:
    late = LAST_OFFER_AT + timedelta(seconds=1)
    offer = await ensure_offer_for_cycle(
        db,
        status_message(last_offer_at=LAST_OFFER_AT),
        late,
        SETTINGS,
    )

    assert offer is not None
    assert offer.status == NO_OFFER
    assert offer.reason == "LAST_OFFER_AT_PASSED"
    assert (await db.execute(select(OutboxMessage))).scalars().all() == []


async def test_offer_updates_after_sale_and_skips_unchanged_remaining(db: AsyncSession) -> None:
    await ensure_offer_for_cycle(db, status_message(), NOW, SETTINGS)

    changed = await set_offer_remaining(db, CYCLE_ID, 400, NOW + timedelta(minutes=1), SETTINGS)
    unchanged = await set_offer_remaining(
        db, CYCLE_ID, 400, NOW + timedelta(minutes=2), SETTINGS
    )

    assert changed is True
    assert unchanged is False
    messages = (await db.execute(select(OutboxMessage).order_by(OutboxMessage.id))).scalars().all()
    assert [message.payload["type"] for message in messages] == ["offer", "offer-updated"]
    assert messages[1].payload["data"] == {
        "offerId": f"TAL-{CYCLE_ID}",
        "remaining": 400,
        "pricePerEnergy": 477.75,
    }
    offer = (await db.execute(select(SellerOffer))).scalar_one()
    assert offer.remaining == 400


async def test_price_change_is_published_with_current_remaining(db: AsyncSession) -> None:
    await ensure_offer_for_cycle(db, status_message(), NOW, SETTINGS)
    changed = await change_offer_price(
        db, CYCLE_ID, Decimal("500"), NOW + timedelta(minutes=1), SETTINGS
    )
    assert changed is True
    messages = (await db.execute(select(OutboxMessage).order_by(OutboxMessage.id))).scalars().all()
    assert messages[-1].payload["type"] == "offer-updated"
    assert messages[-1].payload["data"]["remaining"] == 600
    assert messages[-1].payload["data"]["pricePerEnergy"] == 500

    with pytest.raises(ValueError, match="entre buybackPrice"):
        await change_offer_price(
            db, CYCLE_ID, Decimal("800"), NOW + timedelta(minutes=2), SETTINGS
        )


async def test_withdraw_publishes_once_and_closes_own_offer(db: AsyncSession) -> None:
    await ensure_offer_for_cycle(db, status_message(), NOW, SETTINGS)
    first = await withdraw_offer(db, CYCLE_ID, NOW + timedelta(minutes=1), SETTINGS)
    second = await withdraw_offer(db, CYCLE_ID, NOW + timedelta(minutes=2), SETTINGS)

    assert first is True
    assert second is False
    offer = (await db.execute(select(SellerOffer))).scalar_one()
    assert offer.status == "withdrawn"
    messages = (await db.execute(select(OutboxMessage).order_by(OutboxMessage.id))).scalars().all()
    assert [message.payload["type"] for message in messages] == ["offer", "offer-withdrawn"]


async def test_market_closed_blocks_all_later_offer_updates(db: AsyncSession) -> None:
    await ensure_offer_for_cycle(db, status_message(), NOW, SETTINGS)
    closed = {
        "idpk": "market-closed-1",
        "msgId": "market-closed-message-1",
        "type": "market-closed",
        "timestamp": (NOW + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        "cityId": "central",
        "cycleId": CYCLE_ID,
        "data": {},
    }
    assert await apply_central_message(
        db, closed, NOW + timedelta(minutes=2), SETTINGS
    ) == APPLIED
    changed = await set_offer_remaining(db, CYCLE_ID, 300, NOW + timedelta(minutes=3), SETTINGS)

    assert changed is False
    offer = (await db.execute(select(SellerOffer))).scalar_one()
    assert offer.status == CLOSED
    messages = (await db.execute(select(OutboxMessage))).scalars().all()
    assert len(messages) == 1


async def test_market_closed_before_status_prevents_late_creation(db: AsyncSession) -> None:
    closed = {
        "idpk": "market-closed-1",
        "msgId": "market-closed-message-1",
        "type": "market-closed",
        "timestamp": NOW.isoformat().replace("+00:00", "Z"),
        "cityId": "central",
        "cycleId": CYCLE_ID,
        "data": {},
    }
    assert await apply_central_message(db, closed, NOW, SETTINGS) == APPLIED
    offer = await ensure_offer_for_cycle(db, status_message(), NOW + timedelta(seconds=1), SETTINGS)

    assert offer is not None
    assert offer.status == CLOSED
    assert (await db.execute(select(OutboxMessage))).scalars().all() == []


async def test_no_market_operations_are_enqueued_after_last_offer_at(db: AsyncSession) -> None:
    await ensure_offer_for_cycle(db, status_message(), NOW, SETTINGS)
    late = LAST_OFFER_AT + timedelta(seconds=1)

    assert await set_offer_remaining(db, CYCLE_ID, 300, late, SETTINGS) is False
    assert await change_offer_price(db, CYCLE_ID, Decimal("500"), late, SETTINGS) is False
    assert await withdraw_offer(db, CYCLE_ID, late, SETTINGS) is False
    assert len((await db.execute(select(OutboxMessage))).scalars().all()) == 1


async def test_feature_flag_keeps_market_publication_disabled(db: AsyncSession) -> None:
    disabled = SETTINGS.model_copy(update={"market_enabled": False})
    offer = await ensure_offer_for_cycle(db, status_message(), NOW, disabled)

    assert offer is None
    assert (await db.execute(select(OutboxMessage))).scalars().all() == []
