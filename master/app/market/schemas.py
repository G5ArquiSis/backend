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


class SpanCycle(CamelCaseModel):
    cycle_id: str
    span_seconds: int | None
    deficit: Decimal
    covered: Decimal
    # covered / deficit; None si el ciclo no tenía déficit.
    coverage: Decimal | None
    market_cost: Decimal
    imbalance_cost: Decimal
    effective_cost: Decimal
    # Costo de no comprar nada: déficit × imbalancePrice.
    baseline_cost: Decimal
    savings: Decimal
    bids_sent: int
    bids_rejected: int


class SpanAggregate(CamelCaseModel):
    span_seconds: int | None
    cycles: int
    cycle_ids: list[str]
    deficit: Decimal
    covered: Decimal
    coverage: Decimal | None
    effective_cost: Decimal
    baseline_cost: Decimal
    # effective_cost / baseline_cost: bajo 1 es ahorro frente a no comprar.
    cost_vs_baseline: Decimal | None
    bids_sent: int
    bids_rejected: int


class SpanExperiment(CamelCaseModel):
    cycles: list[SpanCycle]
    by_span: list[SpanAggregate]
