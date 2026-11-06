"""Motor comprador (rol D, tareas D3 y D4): pujar, verificar el token y aceptar el swap.

Flujo (enunciado E2 § 2):

    déficit del ciclo → bid a la oferta más barata (precio + transporte) en city.{vendedor}
    → bid-accepted con swapId y swapToken → verificar el token → POST /swaps/{id}/accept
    → swap-settled

Como el resto de master, no hay temporizadores: `buyer_tick` corre en segundo plano en cada
`POST /internal/outbox/claim` (cada 5 s), lee de la base qué falta y decide. Un
advisory lock de Postgres hace que solo una réplica compre a la vez.

Reglas que sostiene:

- Solo se compra si el precio efectivo es menor que `imbalancePrice` (si no, conviene pagar el
  desbalance), entre `opensAt + span` y `lastBidAt`, y respetando `minBid`.
- Nunca el mismo kWh en dos bids: lo que está en bids vivos se descuenta del déficit, y hay a
  lo más un bid vivo por oferta.
- Un bid sin respuesta se reenvía con el mismo idpk (y un msgId nuevo) hasta
  `BID_MAX_ATTEMPTS` veces; después vence.
- Un swap se acepta solo si el token cumple (ver `token.py`), con una Idempotency-Key fija por
  bid: un reintento tras 502/504 o un reinicio a mitad de camino nunca acepta dos veces.

Todo queda apagado salvo que `BUYER_ENABLED`, `MARKET_ENABLED` y `WALLET_ENABLED` estén en true.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.handler_registry import APPLIED, extend_handler, register_handler
from app.market.book import (
    DUPLICATE,
    IGNORED,
    Market,
    _decimal,
    _instant,
    current_market,
    list_open_offers,
    market_of,
    round2,
)
from app.market.estrategia_compra import buy_strategy
from app.market.models import (
    BID_ACCEPTED,
    BID_EXPIRED,
    BID_FAILED,
    BID_PROPOSED,
    BID_REJECTED,
    BID_SENT,
    BID_SETTLED,
    BID_TOKEN_INVALID,
    LIVE_BID_STATES,
    OFFER_CLOSED,
    OFFER_EXCLUDED,
    OFFER_WITHDRAWN,
    MarketBid,
    MarketOffer,
)
from app.market.token import Expected, JwksCache, cache_for, jwks_url, verify_swap_token
from app.models import WalletBalance
from app.outbox import Destination, enqueue_publish
from app.wallet.client import WalletClient, WalletError

logger = logging.getLogger(__name__)

# Distinto del de la reconciliación (21730006).
_BUYER_LOCK = 21730008
# Nuevos bids por tick, para no inundar a un vendedor si el libro cambia mucho.
_MAX_NEW_BIDS_PER_TICK = 3
# Margen antes de lastBidAt: un bid que llega después el vendedor ya no puede aceptarlo.
_LAST_BID_MARGIN = timedelta(seconds=5)


async def _bid_by_target(session: AsyncSession, target: object) -> MarketBid | None:
    if not isinstance(target, str):
        return None
    found = await session.execute(
        select(MarketBid).where(MarketBid.msg_ids.contains([target])).with_for_update()
    )
    return found.scalar_one_or_none()


# --- Respuestas del vendedor y eventos de la wallet ----------------------------------------


async def _apply_bid_accepted(
    session: AsyncSession, message: dict, now: datetime, settings: Settings
) -> str:
    data = message["data"]
    bid = await _bid_by_target(session, data["target"])
    if bid is None or bid.seller != message["cityId"]:
        return IGNORED
    swap_id = data["swapId"]
    if bid.swap_id == swap_id:
        return DUPLICATE
    if bid.state != BID_SENT or data["offerId"] != bid.offer_id:
        return IGNORED

    bid.state = BID_PROPOSED
    bid.swap_id = swap_id
    bid.swap_token = data["swapToken"]
    bid.swap_price = _decimal(data["pricePerEnergy"])
    bid.swap_amount = _decimal(data["amount"])
    bid.swap_expires_at = _instant(data["expiresAt"])
    if _decimal(data["quantity"]) != bid.quantity:
        # El vendedor acepta o rechaza entero: otra cantidad es una respuesta inválida.
        bid.state = BID_TOKEN_INVALID
        bid.reason = "QUANTITY_MISMATCH"
    bid.accept_key = str(uuid.uuid4())
    bid.deadline_at = None

    from app.wallet.correlation import register_swap_cycle

    await register_swap_cycle(session, swap_id, bid.cycle_id)
    return APPLIED


async def _apply_bid_rejected(
    session: AsyncSession, message: dict, now: datetime, settings: Settings
) -> str:
    data = message["data"]
    bid = await _bid_by_target(session, data["target"])
    if bid is None or bid.seller != message["cityId"]:
        return IGNORED
    if bid.state != BID_SENT:
        return DUPLICATE if bid.state == BID_REJECTED else IGNORED

    reason = message.get("reason") or "REJECTED"
    bid.state = BID_REJECTED
    bid.reason = str(reason)[:64]
    bid.deadline_at = None

    offer = await session.get(MarketOffer, bid.offer_id, with_for_update=True)
    if offer is not None:
        if reason == "QUANTITY_UNAVAILABLE" and "available" in data:
            # Nuestro remaining estaba viejo: el próximo tick puja por lo que queda.
            offer.remaining = min(offer.remaining, _decimal(data["available"]))
        elif reason == "OFFER_WITHDRAWN":
            offer.status = OFFER_WITHDRAWN
        elif reason == "MARKET_CLOSED":
            offer.status = OFFER_CLOSED
        elif reason == "SELF_TRADE":
            offer.status = OFFER_EXCLUDED
    return APPLIED


async def _after_nack(
    session: AsyncSession, message: dict, now: datetime, settings: Settings, outcome: str | None
) -> str:
    """Un vendedor puede responder `nack` a nuestro bid (por ejemplo, malformado)."""
    if outcome == APPLIED:
        return IGNORED
    bid = await _bid_by_target(session, (message.get("data") or {}).get("target"))
    if bid is None or bid.state != BID_SENT:
        return IGNORED
    bid.state = BID_REJECTED
    bid.reason = str(message.get("reason") or "NACK")[:64]
    bid.deadline_at = None
    return APPLIED


async def _after_swap_settled(
    session: AsyncSession, message: dict, now: datetime, settings: Settings, outcome: str | None
) -> str:
    swap_id = (message.get("data") or {}).get("swapId")
    if not swap_id:
        return IGNORED
    settled = await session.execute(
        update(MarketBid)
        .where(MarketBid.swap_id == swap_id, MarketBid.state != BID_SETTLED)
        .values(state=BID_SETTLED, reason=None)
    )
    return APPLIED if settled.rowcount else IGNORED


async def _after_swap_rejected(
    session: AsyncSession, message: dict, now: datetime, settings: Settings, outcome: str | None
) -> str:
    data = message.get("data") or {}
    if data.get("operation") != "accept" or not data.get("swapId"):
        return IGNORED
    failed = await session.execute(
        update(MarketBid)
        .where(
            MarketBid.swap_id == data["swapId"],
            MarketBid.state.in_((BID_PROPOSED, BID_ACCEPTED)),
        )
        .values(state=BID_FAILED, reason=str(message.get("reason") or "SWAP_REJECTED")[:64])
    )
    return APPLIED if failed.rowcount else IGNORED


async def close_cycle_bids(session: AsyncSession, cycle_id: str) -> int:
    """Al cerrar el mercado vencen los bids que todavía esperaban respuesta."""
    expired = await session.execute(
        update(MarketBid)
        .where(MarketBid.cycle_id == cycle_id, MarketBid.state.in_((BID_SENT, BID_PROPOSED)))
        .values(state=BID_EXPIRED, reason="MARKET_CLOSED", deadline_at=None)
    )
    return expired.rowcount


def register() -> None:
    register_handler("bid-accepted", _apply_bid_accepted)
    register_handler("bid-rejected", _apply_bid_rejected)
    extend_handler("nack", _after_nack)
    extend_handler("swap-settled", _after_swap_settled)
    extend_handler("swap-rejected", _after_swap_rejected)


# --- El tick del comprador -----------------------------------------------------------------


def buyer_active(settings: Settings) -> bool:
    return bool(
        settings.buyer_enabled
        and settings.market_enabled
        and settings.wallet_enabled
        and settings.wallet_base_url
        and settings.wallet_api_key
    )


async def buyer_tick(
    settings: Settings,
    *,
    now: datetime | None = None,
    session_factory: Callable | None = None,
    wallet_factory: Callable[[], WalletClient] | None = None,
    jwks: JwksCache | None = None,
) -> None:
    """Una pasada del comprador. Nunca lanza: un error queda en el log y se reintenta en el
    siguiente claim."""
    if not buyer_active(settings):
        return
    if session_factory is None:
        from app.database import _session_factory as session_factory
    now = now or datetime.now(UTC)
    try:
        async with session_factory() as session:
            locked = await session.execute(text(f"SELECT pg_try_advisory_xact_lock({_BUYER_LOCK})"))
            if not locked.scalar():
                return
            wallet = (wallet_factory or _wallet_for(settings))()
            keys = jwks or cache_for(jwks_url(settings.wallet_base_url, settings.wallet_jwks_url))
            try:
                await accept_proposed_swaps(session, now, wallet, keys)
                await retry_or_expire_bids(session, now, settings)
                await place_bids(session, now, settings)
            finally:
                await wallet.aclose()
            await session.commit()
    except Exception:  # noqa: BLE001 - el comprador no puede tumbar el claim de la outbox
        logger.exception("Falló una pasada del comprador")


def _wallet_for(settings: Settings) -> Callable[[], WalletClient]:
    return lambda: WalletClient(settings.wallet_base_url, settings.wallet_api_key)


async def accept_proposed_swaps(
    session: AsyncSession, now: datetime, wallet: WalletClient, jwks: JwksCache
) -> None:
    """D4: verifica el token de cada bid aceptado por el vendedor y acepta el swap."""
    proposed = await session.execute(
        select(MarketBid)
        .where(MarketBid.state == BID_PROPOSED)
        .order_by(MarketBid.id)
        .with_for_update(skip_locked=True)
    )
    for bid in proposed.scalars().all():
        if bid.swap_expires_at is not None and bid.swap_expires_at <= now:
            bid.state, bid.reason = BID_FAILED, "SWAP_EXPIRED"
            continue
        if bid.swap_price > bid.price_per_energy:
            # Pedimos al precio de la oferta: si el vendedor cobra más, no se acepta.
            bid.state, bid.reason = BID_TOKEN_INVALID, "PRICE_ABOVE_OFFER"
            continue
        amount = round2(bid.quantity * bid.swap_price)
        if bid.swap_amount != amount:
            bid.state, bid.reason = BID_TOKEN_INVALID, "AMOUNT_MISMATCH"
            continue
        expected = Expected(bid.swap_id, bid.seller, bid.quantity, amount, bid.offer_id)
        try:
            problems = await verify_swap_token(bid.swap_token, expected, jwks, now)
        except httpx.HTTPError as error:
            logger.warning("JWKS no disponible, se reintenta: %r", error)
            return
        if problems:
            logger.warning("Token del swap %s rechazado: %s", bid.swap_id, "; ".join(problems))
            bid.state, bid.reason = BID_TOKEN_INVALID, problems[0][:64]
            continue
        try:
            await wallet.accept_swap(bid.swap_id, bid.accept_key)
        except WalletError as error:
            if error.status_code >= 500 or error.status_code == 429:
                continue  # misma llave en el próximo tick
            payload = error.payload if isinstance(error.payload, dict) else {}
            bid.state = BID_FAILED
            bid.reason = str(payload.get("reason") or f"HTTP_{error.status_code}")[:64]
            continue
        except httpx.HTTPError:
            continue  # pudo haberse aceptado: se reintenta con la misma llave
        if bid.state == BID_PROPOSED:
            bid.state = BID_ACCEPTED


async def retry_or_expire_bids(session: AsyncSession, now: datetime, settings: Settings) -> None:
    """Reenvía con el mismo idpk los bids sin respuesta, o los da por vencidos."""
    overdue = await session.execute(
        select(MarketBid)
        .where(MarketBid.state == BID_SENT, MarketBid.deadline_at <= now)
        .with_for_update(skip_locked=True)
    )
    for bid in overdue.scalars().all():
        market = await market_of(session, bid.cycle_id)
        late = market is None or now >= market.last_bid_at - _LAST_BID_MARGIN
        if late or bid.attempts >= settings.bid_max_attempts:
            bid.state, bid.reason, bid.deadline_at = BID_EXPIRED, "NO_RESPONSE", None
            continue
        bid.attempts += 1
        bid.deadline_at = now + timedelta(seconds=settings.bid_response_timeout_seconds)
        await _publish_bid(session, bid, now, settings)


async def place_bids(session: AsyncSession, now: datetime, settings: Settings) -> list[MarketBid]:
    """D3: si el ciclo con mercado abierto tiene déficit, puja por las ofertas más baratas."""
    market = await current_market(session, now)
    if market is None or market.deficit <= 0:
        return []
    strategy = buy_strategy(settings, market.cycle_id)
    starts = market.opens_at + timedelta(seconds=strategy.span_seconds)
    if now < starts or now >= market.last_bid_at - _LAST_BID_MARGIN:
        return []

    live = await session.execute(
        select(MarketBid).where(
            MarketBid.cycle_id == market.cycle_id, MarketBid.state.in_(LIVE_BID_STATES)
        )
    )
    live_bids = live.scalars().all()
    held = await _kwh_in_account(session)
    need = market.deficit - held - sum((b.quantity for b in live_bids), Decimal(0))
    if need <= 0:
        return []

    budget = await _spendable_credits(session)
    if budget is not None:
        budget -= sum(
            (b.quantity * (b.price_per_energy + b.transport_per_energy) for b in live_bids),
            Decimal(0),
        )
    busy_offers = {b.offer_id for b in live_bids}
    too_small = await _offers_rejected_as_too_small(session, market.cycle_id)

    placed = []
    for entry in await list_open_offers(session, market.cycle_id):
        if need <= 0 or len(placed) >= _MAX_NEW_BIDS_PER_TICK:
            break
        offer, price = entry.offer, entry.effective_price
        if price is None or price >= market.imbalance_price:
            continue
        if offer.city_id == settings.city_code or offer.offer_id in busy_offers | too_small:
            continue
        quantity = _bid_quantity(need, offer.remaining, price, market)
        if budget is not None and quantity > 0:
            quantity = min(quantity, Decimal(math.floor(max(budget, Decimal(0)) / price)))
            if quantity < market.min_bid:
                quantity = Decimal(0)
        if quantity <= 0:
            continue
        bid = MarketBid(
            idpk=str(uuid.uuid4()),
            cycle_id=market.cycle_id,
            offer_id=offer.offer_id,
            seller=offer.city_id,
            quantity=quantity,
            price_per_energy=offer.price_per_energy,
            transport_per_energy=entry.route.transport_per_energy,
            state=BID_SENT,
            msg_ids=[],
            attempts=1,
            deadline_at=now + timedelta(seconds=settings.bid_response_timeout_seconds),
            span_seconds=strategy.span_seconds,
        )
        session.add(bid)
        await session.flush()
        await _publish_bid(session, bid, now, settings)
        placed.append(bid)
        need -= quantity
        if budget is not None:
            budget -= quantity * price
    return placed


def _bid_quantity(need: Decimal, remaining: Decimal, price: Decimal, market: Market) -> Decimal:
    """Cuánto pedirle a una oferta: lo que falta, sin pasar de su remaining ni bajar de
    minBid.

    Si falta menos que `minBid`, se compra `minBid` solo si sale más barato que pagar el
    desbalance: el exceso se quema al cierre y la central lo paga a `buybackPrice`.
    """
    quantity = min(need, remaining)
    if quantity >= market.min_bid:
        return quantity
    if remaining < market.min_bid:
        return Decimal(0)
    buying = market.min_bid * price - (market.min_bid - need) * market.buyback_price
    return market.min_bid if buying < need * market.imbalance_price else Decimal(0)


async def _publish_bid(
    session: AsyncSession, bid: MarketBid, now: datetime, settings: Settings
) -> None:
    msg_id = str(uuid.uuid4())
    bid.msg_ids = [*bid.msg_ids, msg_id]
    envelope = {
        "idpk": bid.idpk,
        "msgId": msg_id,
        "type": "bid",
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "cityId": settings.city_code,
        "cycleId": bid.cycle_id,
        "data": {"offerId": bid.offer_id, "quantity": int(bid.quantity)},
    }
    await enqueue_publish(
        session, f"bid:{bid.idpk}:{bid.attempts}", envelope, Destination.city(bid.seller)
    )


async def _kwh_in_account(session: AsyncSession) -> Decimal:
    row = await session.get(WalletBalance, "KWH")
    if row is None:
        return Decimal(0)
    return Decimal(row.available) + Decimal(row.held)


async def _spendable_credits(session: AsyncSession) -> Decimal | None:
    """CR disponibles más la línea de crédito; None si todavía no se conoce el saldo."""
    row = await session.get(WalletBalance, "CR")
    if row is None:
        return None
    return Decimal(row.available) + Decimal(row.credit_line or 0)


async def _offers_rejected_as_too_small(session: AsyncSession, cycle_id: str) -> set[str]:
    found = await session.execute(
        select(MarketBid.offer_id).where(
            MarketBid.cycle_id == cycle_id,
            MarketBid.state == BID_REJECTED,
            MarketBid.reason == "BID_TOO_SMALL",
        )
    )
    return set(found.scalars())


async def list_bids(session: AsyncSession, cycle_id: str | None, limit: int) -> list[MarketBid]:
    query = select(MarketBid).order_by(MarketBid.id.desc()).limit(limit)
    if cycle_id:
        query = query.where(MarketBid.cycle_id == cycle_id)
    return list((await session.execute(query)).scalars())


async def count_bids_by_state(session: AsyncSession, cycle_id: str) -> dict[str, int]:
    found = await session.execute(
        select(MarketBid.state, func.count())
        .where(MarketBid.cycle_id == cycle_id)
        .group_by(MarketBid.state)
    )
    return dict(found.all())
