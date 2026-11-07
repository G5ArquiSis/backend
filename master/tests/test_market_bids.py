"""Pruebas de recepción, deduplicación y reserva atómica de bids (C3)."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.cycle_repository import APPLIED, DUPLICATE, apply_central_message
from app.database import _session_factory
from app.market.bids import arbitrate_pending_bids
from app.market.models import SellerBid, SellerOffer
from app.market.seller import ACTIVE, withdraw_offer
from app.market.strategy import StrategyConfig, update_active_strategy
from app.market.swaps import (
    reconcile_pending_swap_events,
    reconcile_pending_swap_proposals,
)
from app.models import OutboxMessage, WalletEvent
from app.wallet.correlation import register_swap_cycle

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 6, 19, 0, tzinfo=UTC)
CYCLE_ID = "cycle-9431"
LAST_OFFER_AT = NOW + timedelta(minutes=8)
LAST_BID_AT = LAST_OFFER_AT + timedelta(seconds=30)
SETTINGS = get_settings().model_copy(update={"market_enabled": True, "city_code": "TAL"})


def status_message(*, cycle_id: str = CYCLE_ID, sellable: int = 600) -> dict[str, Any]:
    last_offer_at = LAST_OFFER_AT
    return {
        "idpk": f"status-{cycle_id}",
        "msgId": f"message-{cycle_id}",
        "type": "status-statement",
        "timestamp": NOW.isoformat().replace("+00:00", "Z"),
        "cityId": "central",
        "cycleId": cycle_id,
        "data": {
            "energy": {
                "generationCapacity": 1000,
                "consumption": 400,
                "generationCost": 210,
                "sellable": sellable,
            },
            "market": {
                "buybackPrice": "220.50",
                "imbalancePrice": "735.00",
                "minBid": 10,
                "lastOfferAt": last_offer_at.isoformat().replace("+00:00", "Z"),
                "lastBidAt": LAST_BID_AT.isoformat().replace("+00:00", "Z"),
                "closesAt": (LAST_BID_AT + timedelta(minutes=1)).isoformat().replace(
                    "+00:00", "Z"
                ),
                "reportOpensAt": (LAST_BID_AT + timedelta(minutes=5)).isoformat().replace(
                    "+00:00", "Z"
                ),
            },
            "validUntil": (LAST_BID_AT + timedelta(minutes=7)).isoformat().replace(
                "+00:00", "Z"
            ),
        },
    }


def bid_message(
    *,
    idpk: str = "bid-corr-001",
    msg_id: str = "msg-bid-corr-001",
    city_id: str = "COR",
    cycle_id: str = CYCLE_ID,
    offer_id: str | None = None,
    quantity: int = 200,
) -> dict[str, Any]:
    return {
        "idpk": idpk,
        "msgId": msg_id,
        "type": "bid",
        "timestamp": (NOW + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "cityId": city_id,
        "cycleId": cycle_id,
        "data": {
            "offerId": offer_id or f"TAL-{cycle_id}",
            "quantity": quantity,
        },
    }


@pytest.fixture
async def db(database: None):
    async with _session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE cycle_strategy_snapshots, strategy_state, strategy_versions, "
                "seller_bids, seller_offers, cycle_market_messages, ledger_events, "
                "cycle_ledger, wallet_events, outbox RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
        yield session


async def open_offer(db: AsyncSession) -> SellerOffer:
    assert await apply_central_message(db, status_message(), NOW, SETTINGS) == APPLIED
    return (await db.execute(select(SellerOffer))).scalar_one()


async def test_valid_bid_reserves_remaining_for_c4(db: AsyncSession) -> None:
    offer = await open_offer(db)

    result = await apply_central_message(
        db, bid_message(quantity=200), NOW + timedelta(seconds=1), SETTINGS
    )

    assert result == APPLIED
    await db.refresh(offer)
    assert offer.status == ACTIVE
    assert offer.remaining == 400

    bid = (await db.execute(select(SellerBid))).scalar_one()
    assert bid.idpk == "bid-corr-001"
    assert bid.msg_id == "msg-bid-corr-001"
    assert bid.buyer_city == "COR"
    assert bid.quantity == 200
    assert bid.price_per_energy == Decimal("477.75")
    assert bid.status == "reserved"
    assert bid.reason is None
    # C4 consumirá esta reserva y creará el swap; C3 no inventa un bid-accepted sin token.
    assert not any(
        row.payload.get("type") == "bid-accepted"
        for row in (await db.execute(select(OutboxMessage))).scalars().all()
    )


async def test_largest_quantity_first_arbitrates_pending_bids(db: AsyncSession) -> None:
    await update_active_strategy(
        db,
        StrategyConfig(bid_acceptance_policy="largest_quantity_first"),
    )
    await db.commit()
    offer = await open_offer(db)

    # El bid pequeño llega primero, pero la estrategia prioriza la cantidad mayor.
    assert await apply_central_message(
        db,
        bid_message(idpk="bid-small", msg_id="msg-small", quantity=300),
        NOW + timedelta(seconds=1),
        SETTINGS,
    ) == APPLIED
    assert await apply_central_message(
        db,
        bid_message(idpk="bid-large", msg_id="msg-large", city_id="MEL", quantity=350),
        NOW + timedelta(seconds=1, milliseconds=40),
        SETTINGS,
    ) == APPLIED

    bids_before = (await db.execute(select(SellerBid))).scalars().all()
    assert len(bids_before) == 2
    assert all(bid.status == "pending" for bid in bids_before)

    # Antes de 200 ms todavía no arbitra: permite agrupar el bid que llega 40 ms después.
    processed = await arbitrate_pending_bids(
        db,
        NOW + timedelta(seconds=1, milliseconds=100),
        SETTINGS,
    )
    assert processed == 0

    processed = await arbitrate_pending_bids(
        db,
        NOW + timedelta(seconds=2),
        SETTINGS,
    )
    assert processed == 2

    await db.refresh(offer)
    assert offer.remaining == 250
    bids = {
        bid.idpk: bid
        for bid in (await db.execute(select(SellerBid))).scalars().all()
    }
    assert bids["bid-large"].status == "reserved"
    assert bids["bid-small"].status == "rejected"
    assert bids["bid-small"].reason == "QUANTITY_UNAVAILABLE"
    assert bids["bid-small"].available == 250


async def test_same_idpk_is_deduplicated_without_reserving_twice(db: AsyncSession) -> None:
    offer = await open_offer(db)
    original = bid_message(quantity=200)
    repeated = bid_message(quantity=200, msg_id="msg-bid-retry")

    assert await apply_central_message(
        db, original, NOW + timedelta(seconds=1), SETTINGS
    ) == APPLIED
    assert await apply_central_message(
        db, repeated, NOW + timedelta(seconds=2), SETTINGS
    ) == DUPLICATE

    await db.refresh(offer)
    assert offer.remaining == 400
    assert len((await db.execute(select(SellerBid))).scalars().all()) == 1


async def test_quantity_unavailable_rejects_entire_bid_with_available(db: AsyncSession) -> None:
    await open_offer(db)
    message = bid_message(quantity=700)

    assert await apply_central_message(db, message, NOW + timedelta(seconds=1), SETTINGS) == APPLIED

    bid = (await db.execute(select(SellerBid))).scalar_one()
    assert bid.status == "rejected"
    assert bid.reason == "QUANTITY_UNAVAILABLE"
    assert bid.available == 600

    rejection = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.dedupe_key == "seller-bid:bid-corr-001:rejected"
            )
        )
    ).scalar_one()
    assert rejection.payload["type"] == "bid-rejected"
    assert rejection.payload["reason"] == "QUANTITY_UNAVAILABLE"
    assert rejection.payload["code"] == 409
    assert rejection.payload["cycleId"] == CYCLE_ID
    assert rejection.payload["cityId"] == "TAL"
    assert rejection.payload["data"] == {
        "target": "msg-bid-corr-001",
        "offerId": f"TAL-{CYCLE_ID}",
        "requested": 700,
        "available": 600,
    }


async def test_duplicate_rejected_bid_does_not_enqueue_second_response(db: AsyncSession) -> None:
    await open_offer(db)
    message = bid_message(quantity=700)

    assert await apply_central_message(db, message, NOW + timedelta(seconds=1), SETTINGS) == APPLIED
    assert await apply_central_message(
        db, {**message, "msgId": "retry-msg"}, NOW + timedelta(seconds=2), SETTINGS
    ) == DUPLICATE

    responses = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.dedupe_key == "seller-bid:bid-corr-001:rejected"
            )
        )
    ).scalars().all()
    assert len(responses) == 1


async def test_bid_below_min_bid_is_rejected(db: AsyncSession) -> None:
    await open_offer(db)

    assert await apply_central_message(
        db, bid_message(quantity=9), NOW + timedelta(seconds=1), SETTINGS
    ) == APPLIED

    bid = (await db.execute(select(SellerBid))).scalar_one()
    assert bid.status == "rejected"
    assert bid.reason == "BID_TOO_SMALL"
    response = (
        await db.execute(
            select(OutboxMessage).where(OutboxMessage.dedupe_key.like("seller-bid:%"))
        )
    ).scalar_one()
    assert response.payload["reason"] == "BID_TOO_SMALL"
    assert response.payload["code"] == 422


async def test_self_trade_is_rejected(db: AsyncSession) -> None:
    await open_offer(db)

    assert await apply_central_message(
        db, bid_message(city_id="TAL", quantity=100), NOW + timedelta(seconds=1), SETTINGS
    ) == APPLIED

    bid = (await db.execute(select(SellerBid))).scalar_one()
    assert bid.status == "rejected"
    assert bid.reason == "SELF_TRADE"
    response = (
        await db.execute(
            select(OutboxMessage).where(OutboxMessage.dedupe_key.like("seller-bid:%"))
        )
    ).scalar_one()
    assert response.payload["code"] == 422


async def test_bid_at_last_bid_at_is_rejected_as_market_closed(db: AsyncSession) -> None:
    await open_offer(db)

    assert await apply_central_message(
        db,
        bid_message(quantity=100),
        LAST_BID_AT,
        SETTINGS,
    ) == APPLIED

    bid = (await db.execute(select(SellerBid))).scalar_one()
    assert bid.status == "rejected"
    assert bid.reason == "MARKET_CLOSED"


async def test_market_closed_event_rejects_later_bids(db: AsyncSession) -> None:
    await open_offer(db)
    closed = {
        "idpk": "market-closed-c3",
        "msgId": "market-closed-msg-c3",
        "type": "market-closed",
        "timestamp": (NOW + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
        "cityId": "central",
        "cycleId": CYCLE_ID,
        "data": {},
    }
    assert await apply_central_message(db, closed, NOW + timedelta(seconds=5), SETTINGS) == APPLIED

    assert await apply_central_message(
        db,
        bid_message(idpk="bid-after-close", msg_id="msg-after-close"),
        NOW + timedelta(seconds=6),
        SETTINGS,
    ) == APPLIED
    bid = (await db.execute(select(SellerBid))).scalar_one()
    assert bid.status == "rejected"
    assert bid.reason == "MARKET_CLOSED"


async def test_withdrawn_offer_rejects_bid(db: AsyncSession) -> None:
    await open_offer(db)
    assert await withdraw_offer(db, CYCLE_ID, NOW + timedelta(seconds=2), SETTINGS) is True

    assert await apply_central_message(
        db, bid_message(), NOW + timedelta(seconds=3), SETTINGS
    ) == APPLIED
    bid = (await db.execute(select(SellerBid))).scalar_one()
    assert bid.status == "rejected"
    assert bid.reason == "OFFER_WITHDRAWN"


async def test_unknown_offer_rejects_bid(db: AsyncSession) -> None:
    await open_offer(db)

    assert await apply_central_message(
        db,
        bid_message(offer_id="TAL-not-this-offer"),
        NOW + timedelta(seconds=1),
        SETTINGS,
    ) == APPLIED
    bid = (await db.execute(select(SellerBid))).scalar_one()
    assert bid.status == "rejected"
    assert bid.reason == "OFFER_WITHDRAWN"


async def test_rejection_is_sent_to_bidding_city_through_city_destination(db: AsyncSession) -> None:
    await open_offer(db)

    assert await apply_central_message(
        db, bid_message(quantity=700), NOW + timedelta(seconds=1), SETTINGS
    ) == APPLIED
    from app.outbox import Destination

    assert Destination.city("COR").resolve() == ("energy.x", "city.COR")


async def test_two_concurrent_bids_cannot_reserve_the_same_remaining(db: AsyncSession) -> None:
    await open_offer(db)
    first = bid_message(idpk="bid-concurrent-1", msg_id="msg-concurrent-1", quantity=400)
    second = bid_message(idpk="bid-concurrent-2", msg_id="msg-concurrent-2", quantity=400)

    async def submit(message: dict[str, Any], at: datetime) -> str:
        async with _session_factory() as session:
            return await apply_central_message(session, message, at, SETTINGS)

    outcomes = await asyncio.gather(
        submit(first, NOW + timedelta(seconds=1)),
        submit(second, NOW + timedelta(seconds=1, milliseconds=40)),
    )
    assert outcomes == [APPLIED, APPLIED]

    rows = (await db.execute(select(SellerBid).order_by(SellerBid.id))).scalars().all()
    assert len(rows) == 2
    assert [row.status for row in rows].count("reserved") == 1
    assert [row.status for row in rows].count("rejected") == 1
    rejected = next(row for row in rows if row.status == "rejected")
    assert rejected.reason == "QUANTITY_UNAVAILABLE"
    assert rejected.available == 200

    offer = (await db.execute(select(SellerOffer))).scalar_one()
    await db.refresh(offer)
    assert offer.remaining == 200

async def test_swap_proposed_arrives_before_http_response_is_reconciled(
    db: AsyncSession,
) -> None:
    await open_offer(db)

    assert await apply_central_message(
        db,
        bid_message(quantity=200),
        NOW + timedelta(seconds=1),
        SETTINGS,
    ) == APPLIED

    bid = (await db.execute(select(SellerBid))).scalar_one()
    assert bid.status == "reserved"
    assert bid.swap_id is None

    swap_id = "swap-c4-race-001"
    expires_at = NOW + timedelta(seconds=60)

    # Simulamos que Wallet notifica el swap antes de que el worker
    # haya recibido y persistido la respuesta HTTP.
    wallet_event = {
        "idpk": "wallet-event-race-001",
        "msgId": "wallet-msg-race-001",
        "sender": "central",
        "type": "swap-proposed",
        "timestamp": (
            NOW + timedelta(seconds=2)
        ).isoformat().replace("+00:00", "Z"),
        "data": {
            "swapId": swap_id,
            "token": "token-race-001",
            "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
            # Intencionalmente no incluimos data.idpk.
        },
    }

    assert await apply_central_message(
        db,
        wallet_event,
        NOW + timedelta(seconds=2),
        SETTINGS,
    ) == APPLIED

    await db.refresh(bid)

    # El evento quedó registrado, pero todavía no pudo aceptar el bid.
    assert bid.status == "reserved"
    assert bid.swap_id is None

    stored_event = (
        await db.execute(
            select(WalletEvent).where(
                WalletEvent.idpk == "wallet-event-race-001"
            )
        )
    ).scalar_one()

    assert stored_event.swap_id == swap_id

    # Simulamos que posteriormente llega la respuesta HTTP con swapId.
    await register_swap_cycle(db, swap_id, bid.cycle_id)
    bid.swap_id = swap_id
    await db.commit()

    # La reconciliación recupera el evento que había quedado pendiente.
    processed = await reconcile_pending_swap_proposals(
        db,
        NOW + timedelta(seconds=3),
        SETTINGS,
    )
    await db.commit()

    await db.refresh(bid)

    assert processed == 1
    assert bid.status == "proposed"
    assert bid.swap_id == swap_id

    accepted = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.dedupe_key
                == "seller-bid:bid-corr-001:accepted"
            )
        )
    ).scalar_one()

    assert accepted.payload["type"] == "bid-accepted"
    assert accepted.payload["data"]["swapId"] == swap_id
    assert accepted.payload["data"]["swapToken"] == "token-race-001"

    # Una segunda reconciliación no vuelve a procesar el bid.
    processed_again = await reconcile_pending_swap_proposals(
        db,
        NOW + timedelta(seconds=4),
        SETTINGS,
    )
    await db.commit()

    assert processed_again == 0

async def _create_proposed_bid(
    db: AsyncSession,
    *,
    swap_id: str = "swap-c4-terminal-001",
    quantity: int = 200,
) -> SellerBid:
    await open_offer(db)
    assert await apply_central_message(
        db, bid_message(quantity=quantity), NOW + timedelta(seconds=1), SETTINGS
    ) == APPLIED
    bid = (await db.execute(select(SellerBid))).scalar_one()
    proposed = {
        "idpk": f"wallet-proposed-{swap_id}",
        "msgId": f"wallet-proposed-msg-{swap_id}",
        "sender": "central",
        "type": "swap-proposed",
        "timestamp": (NOW + timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
        "data": {
            "swapId": swap_id,
            "idpk": bid.idpk,
            "token": "signed-token",
            "expiresAt": (NOW + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
        },
    }
    assert await apply_central_message(
        db, proposed, NOW + timedelta(seconds=2), SETTINGS
    ) == APPLIED
    await db.refresh(bid)
    assert bid.status == "proposed"
    return bid


async def test_swap_settled_publishes_reserved_remaining_once(db: AsyncSession) -> None:
    bid = await _create_proposed_bid(db)
    settled = {
        "idpk": "wallet-settled-terminal-001",
        "msgId": "wallet-settled-msg-terminal-001",
        "sender": "central",
        "type": "swap-settled",
        "timestamp": (NOW + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
        "data": {"swapId": bid.swap_id, "idpk": bid.idpk},
    }

    assert await apply_central_message(
        db, settled, NOW + timedelta(seconds=3), SETTINGS
    ) == APPLIED
    await db.refresh(bid)
    offer = (await db.execute(select(SellerOffer))).scalar_one()
    await db.refresh(offer)
    assert bid.status == "settled"
    assert offer.remaining == 400

    updates = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.payload["type"].astext == "offer-updated"
            )
        )
    ).scalars().all()
    assert len(updates) == 1
    assert updates[0].payload["data"]["remaining"] == 400

    # Otro idpk para el mismo hecho no vuelve a publicar la actualización.
    duplicate_fact = {**settled, "idpk": "wallet-settled-terminal-002"}
    assert await apply_central_message(
        db, duplicate_fact, NOW + timedelta(seconds=4), SETTINGS
    ) == APPLIED
    updates = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.payload["type"].astext == "offer-updated"
            )
        )
    ).scalars().all()
    assert len(updates) == 1


@pytest.mark.parametrize(
    ("event_type", "terminal_status", "external_idpk"),
    [
        ("swap-expired", "expired", None),
        ("swap-cancelled", "cancelled", "central-disable-kwh-operation"),
    ],
)
async def test_failed_swap_releases_reserved_quantity(
    db: AsyncSession,
    event_type: str,
    terminal_status: str,
    external_idpk: str | None,
) -> None:
    bid = await _create_proposed_bid(db)
    data = {"swapId": bid.swap_id}
    if external_idpk is not None:
        data["idpk"] = external_idpk
    event = {
        "idpk": f"wallet-{terminal_status}-001",
        "msgId": f"wallet-{terminal_status}-msg-001",
        "sender": "central",
        "type": event_type,
        "timestamp": (NOW + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
        "data": data,
    }

    assert await apply_central_message(
        db, event, NOW + timedelta(seconds=3), SETTINGS
    ) == APPLIED
    await db.refresh(bid)
    offer = (await db.execute(select(SellerOffer))).scalar_one()
    await db.refresh(offer)
    assert bid.status == terminal_status
    assert offer.remaining == 600

    updates = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.payload["type"].astext == "offer-updated"
            )
        )
    ).scalars().all()
    assert len(updates) == 1
    assert updates[0].payload["data"]["remaining"] == 600


async def test_proposal_rejection_becomes_bid_rejected_and_releases_quantity(
    db: AsyncSession,
) -> None:
    await open_offer(db)
    assert await apply_central_message(
        db, bid_message(quantity=200), NOW + timedelta(seconds=1), SETTINGS
    ) == APPLIED
    bid = (await db.execute(select(SellerBid))).scalar_one()

    rejected = {
        "idpk": "wallet-proposal-rejected-001",
        "msgId": "wallet-proposal-rejected-msg-001",
        "sender": "central",
        "type": "swap-rejected",
        "timestamp": (NOW + timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
        "data": {
            "swapId": "swap-c4-proposal-rejected",
            "idpk": bid.idpk,
            "reason": "INSUFFICIENT_ASSET",
        },
    }
    assert await apply_central_message(
        db, rejected, NOW + timedelta(seconds=2), SETTINGS
    ) == APPLIED

    await db.refresh(bid)
    offer = (await db.execute(select(SellerOffer))).scalar_one()
    await db.refresh(offer)
    assert bid.status == "rejected"
    assert bid.reason == "INSUFFICIENT_ASSET"
    assert offer.remaining == 600

    response = (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.dedupe_key == f"seller-bid:{bid.idpk}:rejected"
            )
        )
    ).scalar_one()
    assert response.payload["type"] == "bid-rejected"
    assert response.payload["reason"] == "INSUFFICIENT_ASSET"
    assert response.payload["data"]["target"] == bid.msg_id


async def test_accept_rejection_releases_kwh_without_second_bid_rejected(
    db: AsyncSession,
) -> None:
    bid = await _create_proposed_bid(db)
    rejected = {
        "idpk": "wallet-accept-rejected-001",
        "msgId": "wallet-accept-rejected-msg-001",
        "sender": "central",
        "type": "swap-rejected",
        "timestamp": (NOW + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
        "data": {
            "swapId": bid.swap_id,
            "idpk": "buyer-accept-operation-001",
            "reason": "INSUFFICIENT_ASSET",
        },
    }
    assert await apply_central_message(
        db, rejected, NOW + timedelta(seconds=3), SETTINGS
    ) == APPLIED
    await db.refresh(bid)
    offer = (await db.execute(select(SellerOffer))).scalar_one()
    await db.refresh(offer)

    assert bid.status == "rejected"
    assert offer.remaining == 600
    assert (
        await db.execute(
            select(OutboxMessage).where(
                OutboxMessage.dedupe_key == f"seller-bid:{bid.idpk}:rejected"
            )
        )
    ).scalar_one_or_none() is None


async def test_terminal_event_before_http_swap_mapping_is_reconciled(
    db: AsyncSession,
) -> None:
    await open_offer(db)
    assert await apply_central_message(
        db, bid_message(quantity=200), NOW + timedelta(seconds=1), SETTINGS
    ) == APPLIED
    bid = (await db.execute(select(SellerBid))).scalar_one()
    swap_id = "swap-c4-late-terminal"
    expired = {
        "idpk": "wallet-expired-before-http-001",
        "msgId": "wallet-expired-before-http-msg-001",
        "sender": "central",
        "type": "swap-expired",
        "timestamp": (NOW + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
        "data": {"swapId": swap_id},
    }

    assert await apply_central_message(
        db, expired, NOW + timedelta(seconds=3), SETTINGS
    ) == APPLIED
    await db.refresh(bid)
    assert bid.status == "reserved"
    assert bid.swap_id is None

    await register_swap_cycle(db, swap_id, bid.cycle_id)
    bid.swap_id = swap_id
    await db.commit()

    processed = await reconcile_pending_swap_events(
        db, NOW + timedelta(seconds=4), SETTINGS
    )
    await db.commit()
    await db.refresh(bid)
    offer = (await db.execute(select(SellerOffer))).scalar_one()
    await db.refresh(offer)

    assert processed == 1
    assert bid.status == "expired"
    assert offer.remaining == 600
