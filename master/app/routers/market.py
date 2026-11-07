"""API del mercado para la interfaz: libro de ofertas (RF02) y bids del comprador.

Solo traduce entre HTTP y la capa de datos; las queries viven en `app/market/`.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.market.book import book_cycle, list_open_offers, market_of
from app.market.buyer import list_bids
from app.market.schemas import BidList, BidOut, BookOffer, MarketBook, MarketWindow

router = APIRouter(tags=["market"])

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


@router.get("/market/book")
async def get_market_book(session: SessionDependency) -> MarketBook:
    """Ofertas abiertas de otras ciudades, de la más barata (con transporte) a la más cara."""
    now = datetime.now(UTC)
    cycle_id = await book_cycle(session, now)
    if cycle_id is None:
        return MarketBook(cycle_id=None, market=None, offers=[])
    market = await market_of(session, cycle_id)
    offers = []
    for entry in await list_open_offers(session, cycle_id):
        price = entry.effective_price
        offers.append(
            BookOffer(
                offer_id=entry.offer.offer_id,
                city_id=entry.offer.city_id,
                quantity=entry.offer.quantity,
                remaining=entry.offer.remaining,
                price_per_energy=entry.offer.price_per_energy,
                distance_km=entry.route.distance_km,
                transport_per_energy=entry.route.transport_per_energy,
                effective_price=price,
                route_enabled=entry.route.enabled,
                worth_buying=(
                    None if price is None or market is None else price < market.imbalance_price
                ),
                updated_at=entry.offer.updated_at,
            )
        )
    window = None
    if market is not None:
        window = MarketWindow(
            opens_at=market.opens_at,
            last_bid_at=market.last_bid_at,
            closes_at=market.closes_at,
            imbalance_price=market.imbalance_price,
            buyback_price=market.buyback_price,
            min_bid=market.min_bid,
            deficit=market.deficit,
            is_open=market.opens_at <= now < market.closes_at,
        )
    return MarketBook(cycle_id=cycle_id, market=window, offers=offers)


@router.get("/market/bids")
async def get_market_bids(
    session: SessionDependency,
    cycle_id: Annotated[str | None, Query(alias="cycleId")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> BidList:
    """Bids de TAL, del más reciente al más antiguo, con su estado y el swap asociado."""
    bids = await list_bids(session, cycle_id, limit)
    return BidList(items=[BidOut.model_validate(bid) for bid in bids])
