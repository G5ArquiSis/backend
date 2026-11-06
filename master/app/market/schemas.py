"""DTOs del libro de ofertas y de los bids (RF02 y motor comprador), sin lógica."""

from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas import CamelCaseModel


class BookOffer(CamelCaseModel):
    """Una oferta de otra ciudad con lo que le cuesta a TAL comprarla."""

    offer_id: str
    city_id: str
    quantity: Decimal | None
    remaining: Decimal
    price_per_energy: Decimal
    distance_km: Decimal | None
    transport_per_energy: Decimal | None
    # Precio + transporte; None si no hay ruta conocida hasta el vendedor.
    effective_price: Decimal | None
    # False si la ruta está deshabilitada (paga la tarifa punitiva); None si no hay ruta.
    route_enabled: bool | None
    # True si sale más barato que pagar el desbalance.
    worth_buying: bool | None
    updated_at: datetime


class MarketWindow(CamelCaseModel):
    opens_at: datetime
    last_bid_at: datetime
    closes_at: datetime
    imbalance_price: Decimal
    buyback_price: Decimal
    min_bid: Decimal
    deficit: Decimal
    is_open: bool


class MarketBook(CamelCaseModel):
    """El libro del ciclo con mercado abierto (o del último con ofertas)."""

    cycle_id: str | None
    market: MarketWindow | None
    offers: list[BookOffer]


class BidOut(CamelCaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)

    id: int
    idpk: str
    cycle_id: str
    offer_id: str
    seller: str
    quantity: Decimal
    price_per_energy: Decimal
    transport_per_energy: Decimal
    state: str
    reason: str | None
    attempts: int
    span_seconds: int | None
    swap_id: str | None
    swap_amount: Decimal | None
    created_at: datetime
    updated_at: datetime


class BidList(CamelCaseModel):
    items: list[BidOut]
