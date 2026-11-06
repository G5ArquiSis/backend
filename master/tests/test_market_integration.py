"""Rol D con PostgreSQL real: libro de ofertas, motor comprador y aceptación del swap.

La contraparte es HGW simulada (sus mensajes se aplican como los entregaría connector) y la
wallet es un `httpx.MockTransport` que firma tokens con una clave ES256 de prueba.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.cycle_repository import apply_central_message
from app.database import _session_factory
from app.market import buyer
from app.market.book import list_open_offers
from app.market.models import (
    BID_ACCEPTED,
    BID_EXPIRED,
    BID_REJECTED,
    BID_SENT,
    BID_SETTLED,
    BID_TOKEN_INVALID,
    OFFER_EXCLUDED,
    OFFER_WITHDRAWN,
    MarketBid,
    MarketOffer,
)
from app.market.token import JwksCache, jwks_url
from app.models import OutboxMessage, WalletBalance
from app.wallet.client import WalletClient

pytestmark = pytest.mark.integration

CYCLE = "cycle-9431"
OPENS = datetime(2026, 10, 20, 14, 5, tzinfo=UTC)
NOW = OPENS + timedelta(seconds=30)
WALLET = "https://wallet.test/v1"
SETTINGS = get_settings().model_copy(
    update={
        "market_enabled": True,
        "wallet_enabled": True,
        "buyer_enabled": True,
        "wallet_base_url": WALLET,
        "wallet_api_key": "clave-de-prueba",
        "buy_span_seconds": 0,
        "bid_response_timeout_seconds": 30,
        "bid_max_attempts": 3,
    }
)

PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())
KID = "k1"


@pytest.fixture
async def db(database: None) -> AsyncIterator[AsyncSession]:
    async with _session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE market_offers, market_bids, ledger_events, cycle_ledger, outbox, "
                "message_log, distance_tables, wallet_events, wallet_balance, "
                "wallet_swap_cycles, cycle_market_messages RESTART IDENTITY"
            )
        )
        await session.commit()
        yield session


def message(kind: str, idpk: str, data: dict, *, city: str | None, at: datetime = NOW, **extra):
    envelope = {
        "idpk": idpk,
        "msgId": f"msg-{idpk}",
        "type": kind,
        "timestamp": at.isoformat().replace("+00:00", "Z"),
        "cycleId": CYCLE,
        "data": data,
        **extra,
    }
    if city:
        envelope["cityId"] = city
    else:
        envelope["sender"] = "central"
    return envelope


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def statement(consumption: float = 8000.4, generation: float = 1000) -> dict:
    return message(
        "status-statement",
        "statement-1",
        {
            "energy": {
                "generationCapacity": generation,
                "consumption": consumption,
                "generationCost": 43.2,
                "sellable": 0,
            },
            "market": {
                "buybackPrice": 45.36,
                "imbalancePrice": 735,
                "minBid": 500,
                "opensAt": iso(OPENS),
                "lastOfferAt": iso(OPENS + timedelta(seconds=510)),
                "lastBidAt": iso(OPENS + timedelta(seconds=540)),
                "closesAt": iso(OPENS + timedelta(seconds=600)),
                "reportOpensAt": iso(OPENS + timedelta(seconds=600)),
            },
            "validUntil": iso(OPENS + timedelta(seconds=900)),
        },
        city=None,
    )


def distances() -> dict:
    table = message(
        "distance-table",
        "distances-1",
        {
            "distances": {
                # 100 km a 0,5 por km: 50 por kWh.
                "HGW": {"distance": 100000, "transportCost": 0.5, "enabled": True},
                # 10 km, pero deshabilitada: paga la tarifa punitiva de 40 por km.
                "COR": {"distance": 10000, "transportCost": 0.1, "enabled": False},
            },
            "disabledRouteTransportCost": 40,
        },
        city=None,
    )
    del table["cycleId"]
    return table


def offer(offer_id: str, city: str, quantity: int, price: float, *, idpk=None, at=NOW) -> dict:
    data = {"offerId": offer_id, "quantity": quantity, "pricePerEnergy": price}
    return message("offer", idpk or f"offer-{offer_id}", data, city=city, at=at)


async def apply(db: AsyncSession, msg: dict, now: datetime = NOW) -> str:
    return await apply_central_message(db, msg, now, SETTINGS)


async def setup_market(db: AsyncSession, *offers: dict) -> None:
    await apply(db, statement())
    await apply(db, distances())
    for item in offers:
        await apply(db, item)


async def bids(db: AsyncSession) -> list[MarketBid]:
    db.expire_all()
    return list((await db.execute(select(MarketBid).order_by(MarketBid.id))).scalars())


async def published(db: AsyncSession) -> list[OutboxMessage]:
    return list((await db.execute(select(OutboxMessage).order_by(OutboxMessage.id))).scalars())


def token(bid: MarketBid, swap_id: str, *, key=PRIVATE_KEY, **override) -> str:
    claims = {
        "iss": "wallet.energyshark",
        "sub": swap_id,
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(hours=1)).timestamp()),
        "proposer": bid.seller,
        "give": {"asset": "KWH", "amount": int(bid.quantity)},
        "take": {"asset": "CR", "amount": float(bid.quantity * bid.price_per_energy)},
        "memo": bid.offer_id,
        **override,
    }
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": KID})


def accepted(bid: MarketBid, swap_id: str = "swp-77", **token_override) -> dict:
    return message(
        "bid-accepted",
        f"accepted-{swap_id}",
        {
            "target": bid.msg_ids[-1],
            "offerId": bid.offer_id,
            "quantity": int(bid.quantity),
            "pricePerEnergy": float(bid.price_per_energy),
            "amount": float(bid.quantity * bid.price_per_energy),
            "swapId": swap_id,
            "swapToken": token(bid, swap_id, **token_override),
            "expiresAt": iso(NOW + timedelta(seconds=30)),
        },
        city=bid.seller,
    )


def jwks() -> JwksCache:
    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
    body = {"keys": [{**public, "kid": KID, "alg": "ES256", "use": "sig"}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    return JwksCache(jwks_url(WALLET), transport=transport)


class FakeWallet:
    """Responde los accept y anota cada llamada; `fail_first` simula un timeout."""

    def __init__(self, *, fail_first: int = 0, status: int = 202):
        self.calls: list[httpx.Request] = []
        self.fail_first = fail_first
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if len(self.calls) <= self.fail_first:
            raise httpx.ReadTimeout("sin respuesta", request=request)
        swap_id = request.url.path.split("/")[-2]
        return httpx.Response(self.status, json={"swapId": swap_id, "status": "accepted"})

    def client(self) -> WalletClient:
        return WalletClient(
            WALLET,
            "clave-de-prueba",
            transport=httpx.MockTransport(self.handler),
            max_attempts=1,
            hook=lambda *_: None,
        )


# --- Libro de ofertas (D0, D1) -----------------------------------------------------------


async def test_book_orders_by_effective_price_and_skips_own_offers(db: AsyncSession) -> None:
    await setup_market(
        db,
        offer("HGW-1", "HGW", 3000, 300),  # 300 + 50 = 350
        offer("COR-1", "COR", 3000, 100),  # 100 + 10 km × 40 = 500
        offer("TAL-1", "TAL", 3000, 1),  # propia: no entra
        offer("XXX-1", "XXX", 3000, 1),  # sin ruta: al final
    )

    entries = await list_open_offers(db, CYCLE)

    assert [e.offer.offer_id for e in entries] == ["HGW-1", "COR-1", "XXX-1"]
    assert [e.effective_price for e in entries] == [Decimal(350), Decimal(500), None]
    assert entries[1].route.enabled is False


async def test_feed_out_of_order_and_duplicates(db: AsyncSession) -> None:
    later = NOW + timedelta(seconds=10)
    update = message(
        "offer-updated",
        "upd-1",
        {"offerId": "HGW-1", "remaining": 1200, "pricePerEnergy": 280},
        city="HGW",
        at=later,
    )
    # El offer-updated llega antes que el offer original.
    assert await apply(db, update) == "applied"
    assert await apply(db, update) == "duplicate"
    assert await apply(db, offer("HGW-1", "HGW", 3000, 300)) == "applied"

    row = await db.get(MarketOffer, "HGW-1", populate_existing=True)
    assert (row.quantity, row.remaining, row.price_per_energy) == (3000, 1200, 280)

    withdrawn = message("offer-withdrawn", "wd-1", {"offerId": "HGW-1"}, city="HGW", at=later)
    assert await apply(db, withdrawn) == "applied"
    newer = message(
        "offer-updated",
        "upd-2",
        {"offerId": "HGW-1", "remaining": 900, "pricePerEnergy": 250},
        city="HGW",
        at=later + timedelta(seconds=5),
    )
    assert await apply(db, newer) == "ignored"  # retirada: no se reabre
    row = await db.get(MarketOffer, "HGW-1", populate_existing=True)
    assert row.status == OFFER_WITHDRAWN


async def test_market_book_endpoint(client: httpx.AsyncClient, db: AsyncSession) -> None:
    await setup_market(db, offer("HGW-1", "HGW", 3000, 300))

    response = await client.get("/market/book")

    assert response.status_code == 200
    body = response.json()
    assert body["cycleId"] == CYCLE
    assert body["offers"][0]["offerId"] == "HGW-1"
    assert body["offers"][0]["transportPerEnergy"] == "50.00"
    assert body["offers"][0]["worthBuying"] is True


# --- Motor comprador (D3) ----------------------------------------------------------------


async def test_bids_cover_the_deficit_with_the_cheapest_offers(db: AsyncSession) -> None:
    # Déficit: ceil(8000,4 - 1000) = 7001 kWh.
    await setup_market(
        db,
        offer("HGW-1", "HGW", 5000, 300),  # efectivo 350
        offer("COR-1", "COR", 5000, 100),  # efectivo 500
        offer("EXP-1", "HGW", 5000, 700),  # efectivo 750 > imbalance 735: no
    )
    db.add(WalletBalance(asset="KWH", available=Decimal(1), held=Decimal(0)))
    await db.commit()

    placed = await buyer.place_bids(db, NOW, SETTINGS)
    await db.commit()

    assert [(b.offer_id, b.quantity) for b in placed] == [("HGW-1", 5000), ("COR-1", 2000)]
    sent = await published(db)
    assert [m.routing_key for m in sent] == ["city.HGW", "city.COR"]
    assert sent[0].payload["data"] == {"offerId": "HGW-1", "quantity": 5000}
    assert sent[0].payload["idpk"] == placed[0].idpk
    assert placed[0].msg_ids == [sent[0].payload["msgId"]]

    # Lo que está en bids vivos ya no se vuelve a pedir.
    assert await buyer.place_bids(db, NOW + timedelta(seconds=5), SETTINGS) == []


async def test_span_delays_the_first_bid(db: AsyncSession) -> None:
    await setup_market(db, offer("HGW-1", "HGW", 9000, 300))
    waiting = SETTINGS.model_copy(update={"buy_span_seconds": 120})

    assert await buyer.place_bids(db, OPENS + timedelta(seconds=60), waiting) == []
    placed = await buyer.place_bids(db, OPENS + timedelta(seconds=120), waiting)
    assert [b.span_seconds for b in placed] == [120]


async def test_small_shortfall_buys_min_bid_only_if_cheaper_than_imbalance(
    db: AsyncSession,
) -> None:
    # Déficit 7001 cubierto salvo 1 kWh: comprar minBid (500) a 350 sale más caro que pagar
    # 1 kWh de desbalance (735), aunque se recupere el exceso a buybackPrice.
    await setup_market(db, offer("HGW-1", "HGW", 9000, 300))
    db.add(WalletBalance(asset="KWH", available=Decimal(7000), held=Decimal(0)))
    await db.commit()

    assert await buyer.place_bids(db, NOW, SETTINGS) == []


async def test_credit_limits_the_bid(db: AsyncSession) -> None:
    await setup_market(db, offer("HGW-1", "HGW", 9000, 300))
    db.add(WalletBalance(asset="CR", available=Decimal(100000), held=0, credit_line=Decimal(75000)))
    await db.commit()

    placed = await buyer.place_bids(db, NOW, SETTINGS)

    # 175000 / 350 = 500 kWh: justo minBid.
    assert [b.quantity for b in placed] == [500]


async def test_unanswered_bid_is_resent_with_same_idpk_then_expires(db: AsyncSession) -> None:
    await setup_market(db, offer("HGW-1", "HGW", 9000, 300))
    [bid] = await buyer.place_bids(db, NOW, SETTINGS)
    await db.commit()

    for attempt in (2, 3):
        later = NOW + timedelta(seconds=31 * (attempt - 1))
        await buyer.retry_or_expire_bids(db, later, SETTINGS)
        await db.commit()
        [bid] = await bids(db)
        assert (bid.state, bid.attempts, len(bid.msg_ids)) == (BID_SENT, attempt, attempt)

    await buyer.retry_or_expire_bids(db, NOW + timedelta(seconds=100), SETTINGS)
    await db.commit()
    [bid] = await bids(db)
    assert bid.state == BID_EXPIRED
    sent = await published(db)
    assert len(sent) == 3
    assert {m.payload["idpk"] for m in sent} == {bid.idpk}
    assert len({m.payload["msgId"] for m in sent}) == 3


async def test_quantity_unavailable_caps_remaining_and_rebids(db: AsyncSession) -> None:
    await setup_market(db, offer("HGW-1", "HGW", 9000, 300))
    [bid] = await buyer.place_bids(db, NOW, SETTINGS)
    await db.commit()

    rejected = message(
        "bid-rejected",
        "rej-1",
        {"target": bid.msg_ids[0], "offerId": "HGW-1", "requested": 7001, "available": 2500},
        city="HGW",
        reason="QUANTITY_UNAVAILABLE",
        code=409,
    )
    assert await apply(db, rejected) == "applied"
    assert await apply(db, rejected) == "duplicate"

    [bid] = await bids(db)
    assert (bid.state, bid.reason) == (BID_REJECTED, "QUANTITY_UNAVAILABLE")
    again = await buyer.place_bids(db, NOW + timedelta(seconds=5), SETTINGS)
    assert [b.quantity for b in again] == [2500]


async def test_self_trade_excludes_the_offer(db: AsyncSession) -> None:
    await setup_market(db, offer("HGW-1", "HGW", 9000, 300))
    [bid] = await buyer.place_bids(db, NOW, SETTINGS)
    await db.commit()
    rejected = message(
        "bid-rejected",
        "rej-1",
        {"target": bid.msg_ids[0], "offerId": "HGW-1", "requested": 7001},
        city="HGW",
        reason="SELF_TRADE",
        code=422,
    )
    await apply(db, rejected)

    row = await db.get(MarketOffer, "HGW-1", populate_existing=True)
    assert row.status == OFFER_EXCLUDED
    assert await buyer.place_bids(db, NOW + timedelta(seconds=5), SETTINGS) == []


async def test_market_closed_expires_waiting_bids(db: AsyncSession) -> None:
    await setup_market(db, offer("HGW-1", "HGW", 9000, 300))
    await buyer.place_bids(db, NOW, SETTINGS)
    await db.commit()

    closed = message("market-closed", "closed-1", {}, city=None)
    await apply(db, closed)

    [bid] = await bids(db)
    assert (bid.state, bid.reason) == (BID_EXPIRED, "MARKET_CLOSED")


async def test_reply_from_another_city_is_ignored(db: AsyncSession) -> None:
    await setup_market(db, offer("HGW-1", "HGW", 9000, 300))
    [bid] = await buyer.place_bids(db, NOW, SETTINGS)
    await db.commit()

    forged = accepted(bid)
    forged["cityId"] = "COR"
    assert await apply(db, forged) == "ignored"


# --- Token y aceptación (D4) -------------------------------------------------------------


async def placed_and_accepted(db: AsyncSession, **token_override) -> MarketBid:
    await setup_market(db, offer("HGW-1", "HGW", 9000, 300))
    [bid] = await buyer.place_bids(db, NOW, SETTINGS)
    await db.commit()
    assert await apply(db, accepted(bid, **token_override)) == "applied"
    [bid] = await bids(db)
    return bid


async def test_valid_token_is_accepted_and_settles(db: AsyncSession) -> None:
    bid = await placed_and_accepted(db)
    wallet = FakeWallet()

    await buyer.accept_proposed_swaps(db, NOW + timedelta(seconds=2), wallet.client(), jwks())
    await db.commit()

    [bid] = await bids(db)
    assert bid.state == BID_ACCEPTED
    assert [r.url.path for r in wallet.calls] == ["/v1/swaps/swp-77/accept"]
    assert wallet.calls[0].headers["Idempotency-Key"] == bid.accept_key

    settled = message("swap-settled", "settled-1", {"swapId": "swp-77"}, city=None)
    del settled["cycleId"]
    await apply(db, settled)
    [bid] = await bids(db)
    assert bid.state == BID_SETTLED


@pytest.mark.parametrize(
    "tampered",
    [
        {"give": {"asset": "KWH", "amount": 1}},
        {"take": {"asset": "CR", "amount": 1}},
        {"proposer": "COR"},
        {"memo": "otra-oferta"},
        {"sub": "swp-otro"},
        {"key": OTHER_KEY},
        {"exp": int(NOW.timestamp())},
    ],
)
async def test_tampered_token_is_never_accepted(db: AsyncSession, tampered: dict) -> None:
    await placed_and_accepted(db, **tampered)
    wallet = FakeWallet()

    await buyer.accept_proposed_swaps(db, NOW + timedelta(seconds=2), wallet.client(), jwks())
    await db.commit()

    [bid] = await bids(db)
    assert bid.state == BID_TOKEN_INVALID
    assert wallet.calls == []


async def test_timeout_after_accept_retries_with_the_same_key(db: AsyncSession) -> None:
    await placed_and_accepted(db)
    wallet = FakeWallet(fail_first=1)

    await buyer.accept_proposed_swaps(db, NOW + timedelta(seconds=2), wallet.client(), jwks())
    await db.commit()
    [bid] = await bids(db)
    assert bid.state == "proposed"

    await buyer.accept_proposed_swaps(db, NOW + timedelta(seconds=7), wallet.client(), jwks())
    await db.commit()
    [bid] = await bids(db)
    assert bid.state == BID_ACCEPTED
    keys = {request.headers["Idempotency-Key"] for request in wallet.calls}
    assert len(wallet.calls) == 2 and keys == {bid.accept_key}

    # Otra pasada no vuelve a aceptar.
    await buyer.accept_proposed_swaps(db, NOW + timedelta(seconds=12), wallet.client(), jwks())
    assert len(wallet.calls) == 2


async def test_expired_swap_is_not_accepted(db: AsyncSession) -> None:
    await placed_and_accepted(db)
    wallet = FakeWallet()

    await buyer.accept_proposed_swaps(db, NOW + timedelta(seconds=31), wallet.client(), jwks())
    await db.commit()

    [bid] = await bids(db)
    assert (bid.state, bid.reason) == ("failed", "SWAP_EXPIRED")
    assert wallet.calls == []


async def test_buyer_tick_end_to_end(db: AsyncSession) -> None:
    """Una pasada puja; HGW acepta; la pasada siguiente verifica y acepta en la wallet."""
    await setup_market(db, offer("HGW-1", "HGW", 9000, 300))
    wallet = FakeWallet()
    keys = jwks()

    await buyer.buyer_tick(SETTINGS, now=NOW, wallet_factory=wallet.client, jwks=keys)
    [bid] = await bids(db)
    assert bid.state == BID_SENT
    await apply(db, accepted(bid))

    await buyer.buyer_tick(
        SETTINGS, now=NOW + timedelta(seconds=5), wallet_factory=wallet.client, jwks=keys
    )
    [bid] = await bids(db)
    assert bid.state == BID_ACCEPTED
    assert len(wallet.calls) == 1


async def test_buyer_off_does_nothing(db: AsyncSession) -> None:
    await setup_market(db, offer("HGW-1", "HGW", 9000, 300))
    off = SETTINGS.model_copy(update={"buyer_enabled": False})

    await buyer.buyer_tick(off, now=NOW)

    assert await bids(db) == []
