"""Libro de ofertas (rol D, tareas D0 y D1).

Aplica el feed `market.{CODE}` (`offer`, `offer-updated`, `offer-withdrawn`, `market-closed`)
a la tabla `market_offers` y calcula el precio efectivo de cada oferta para TAL.

Contrato interno I4, para C (ver la competencia) y E (monitoreo):

- `list_open_offers(session, cycle_id)`: las ofertas abiertas de otras ciudades en ese ciclo,
  con transporte y precio efectivo, de la más barata a la más cara.
- `effective_price(session, offer)`: precio + transporte hasta ese vendedor según la
  distance-table vigente.

Reglas del feed (enunciado E2 § 2):

- Puede llegar desordenado o repetido: un mensaje con `timestamp` anterior al último aplicado
  no pisa nada, y el mismo `idpk` dos veces es un duplicado.
- `remaining` ajeno siempre puede estar desactualizado: un `bid-rejected` con `available` lo
  corrige (ver `buyer.py`).
- Nuestras propias ofertas no entran al libro.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.handler_registry import APPLIED, extend_handler, register_handler
from app.market.models import (
    OFFER_CLOSED,
    OFFER_OPEN,
    OFFER_WITHDRAWN,
    MarketOffer,
)
from app.models import CycleLedger, DistanceTable, LedgerEvent

DUPLICATE = "duplicate"
IGNORED = "ignored"


def round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise TypeError(f"número inválido: {value!r}")
    return Decimal(str(value))


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"fecha sin zona horaria: {value!r}")
    return parsed


# --- Datos del ciclo que necesita el comprador -------------------------------------------


@dataclass(frozen=True)
class Market:
    """El bloque `market` del status-statement de un ciclo, más la energía del ciclo."""

    cycle_id: str
    opens_at: datetime
    last_bid_at: datetime
    closes_at: datetime
    imbalance_price: Decimal
    buyback_price: Decimal
    min_bid: Decimal
    deficit: Decimal  # ceil(max(0, consumption - generationCapacity)), en kWh
    sellable: Decimal


async def market_of(session: AsyncSession, cycle_id: str) -> Market | None:
    """El mercado de un ciclo, leído de su status-statement; None si no llegó o es de la E1."""
    found = await session.execute(
        select(LedgerEvent.payload)
        .where(LedgerEvent.cycle_id == cycle_id, LedgerEvent.kind == "status-statement")
        .order_by(LedgerEvent.id)
        .limit(1)
    )
    payload = found.scalar_one_or_none()
    return _market_from_statement(payload) if payload else None


async def current_market(session: AsyncSession, now: datetime) -> Market | None:
    """El ciclo cuyo mercado está abierto ahora (`opensAt` ≤ now < `closesAt`).

    El mercado del ciclo N+1 ocurre durante el consumo del ciclo N, así que hay a lo más uno
    abierto a la vez. Se buscan los status-statement más recientes.
    """
    found = await session.execute(
        select(LedgerEvent.payload)
        .join(CycleLedger, CycleLedger.cycle_id == LedgerEvent.cycle_id)
        .where(LedgerEvent.kind == "status-statement", CycleLedger.valid_until > now)
        .order_by(LedgerEvent.id.desc())
        .limit(5)
    )
    for payload in found.scalars():
        market = _market_from_statement(payload)
        if market and market.opens_at <= now < market.closes_at:
            return market
    return None


def _market_from_statement(message: dict) -> Market | None:
    data = message.get("data") or {}
    market = data.get("market")
    if not isinstance(market, dict) or "closesAt" not in market:
        return None
    energy = data["energy"]
    shortfall = _decimal(energy["consumption"]) - _decimal(energy["generationCapacity"])
    return Market(
        cycle_id=message["cycleId"],
        opens_at=_instant(market["opensAt"]),
        last_bid_at=_instant(market["lastBidAt"]),
        closes_at=_instant(market["closesAt"]),
        imbalance_price=_decimal(market["imbalancePrice"]),
        buyback_price=_decimal(market["buybackPrice"]),
        min_bid=_decimal(market.get("minBid", 1)),
        # El déficit se redondea hacia arriba (README del repo del curso, "Montos").
        deficit=Decimal(math.ceil(max(Decimal(0), shortfall))),
        sellable=_decimal(energy.get("sellable", 0)),
    )


# --- Transporte y precio efectivo --------------------------------------------------------


@dataclass(frozen=True)
class Route:
    distance_km: Decimal | None
    transport_per_energy: Decimal | None
    enabled: bool | None


def route_to(table: DistanceTable | None, city: str) -> Route:
    """Transporte por kWh hasta `city`: `round2(distance_km × tarifa)`.

    `distance` viene en metros. Una ruta con `enabled: false` paga
    `disabledRouteTransportCost` (la tarifa punitiva de la tabla). Sin entrada para esa
    ciudad, el transporte es desconocido y la oferta no se compra.
    """
    if table is None:
        return Route(None, None, None)
    entry = (table.distances or {}).get(city)
    if not isinstance(entry, dict):
        return Route(None, None, None)
    distance_km = _decimal(entry["distance"]) / Decimal(1000)
    enabled = bool(entry.get("enabled", True))
    if enabled:
        rate = _decimal(entry["transportCost"])
    else:
        data = (table.payload or {}).get("data") or {}
        rate = _decimal(data.get("disabledRouteTransportCost", entry["transportCost"]))
    return Route(distance_km, round2(distance_km * rate), enabled)


@dataclass(frozen=True)
class BookEntry:
    """Una oferta del libro con lo que le cuesta a TAL comprarla."""

    offer: MarketOffer
    route: Route

    @property
    def effective_price(self) -> Decimal | None:
        if self.route.transport_per_energy is None:
            return None
        return self.offer.price_per_energy + self.route.transport_per_energy


async def latest_distance_table(session: AsyncSession) -> DistanceTable | None:
    """La distance-table vigente: rige la última recibida."""
    found = await session.execute(
        select(DistanceTable).order_by(DistanceTable.received_at.desc(), DistanceTable.id.desc())
    )
    return found.scalars().first()


async def effective_price(session: AsyncSession, offer: MarketOffer) -> Decimal | None:
    """Contrato I4: precio de la oferta + transporte hasta su vendedor (None si no hay ruta)."""
    table = await latest_distance_table(session)
    return BookEntry(offer, route_to(table, offer.city_id)).effective_price


async def list_open_offers(session: AsyncSession, cycle_id: str) -> list[BookEntry]:
    """Contrato I4: ofertas abiertas del ciclo, de la más barata (efectiva) a la más cara.

    Las que no tienen ruta conocida van al final.
    """
    found = await session.execute(
        select(MarketOffer).where(
            MarketOffer.cycle_id == cycle_id,
            MarketOffer.status == OFFER_OPEN,
            MarketOffer.remaining > 0,
        )
    )
    table = await latest_distance_table(session)
    entries = [BookEntry(offer, route_to(table, offer.city_id)) for offer in found.scalars()]
    return sorted(
        entries,
        key=lambda e: (
            e.effective_price is None,
            e.effective_price or Decimal(0),
            e.offer.offer_id,
        ),
    )


async def book_cycle(session: AsyncSession, now: datetime) -> str | None:
    """El ciclo que muestra el libro: el del mercado abierto o, si no hay, el último con
    ofertas."""
    market = await current_market(session, now)
    if market is not None:
        return market.cycle_id
    found = await session.execute(
        select(MarketOffer.cycle_id).order_by(MarketOffer.last_message_at.desc()).limit(1)
    )
    return found.scalar_one_or_none()


# --- Handlers del feed -------------------------------------------------------------------


async def _apply_offer_message(
    session: AsyncSession, message: dict, now: datetime, settings: Settings
) -> str:
    seller = message["cityId"]
    data = message["data"]
    offer_id = data["offerId"]
    if not isinstance(offer_id, str) or not offer_id:
        raise ValueError("offerId inválido")
    if seller == settings.city_code:
        return IGNORED  # nuestra propia oferta no es parte del libro de compra

    kind = message["type"]
    at = _instant(message["timestamp"])
    offer = await session.get(MarketOffer, offer_id, with_for_update=True)

    if offer is None:
        offer = MarketOffer(
            offer_id=offer_id,
            city_id=seller,
            cycle_id=message["cycleId"],
            remaining=Decimal(0),
            price_per_energy=Decimal(0),
            status=OFFER_OPEN,
            last_message_at=at,
            last_idpk=message["idpk"],
        )
        session.add(offer)
        _write(offer, kind, data)
        return APPLIED

    if offer.last_idpk == message["idpk"]:
        return DUPLICATE
    if offer.city_id != seller:
        return IGNORED  # otra ciudad no puede tocar una oferta ajena
    if at < offer.last_message_at:
        # Llegó tarde. Lo único que aporta un `offer` viejo es la cantidad original.
        if kind == "offer" and offer.quantity is None:
            offer.quantity = _decimal(data["quantity"])
            return APPLIED
        return IGNORED
    if offer.status != OFFER_OPEN:
        return IGNORED  # retirada o cerrada: no se reabre

    _write(offer, kind, data)
    offer.last_message_at = at
    offer.last_idpk = message["idpk"]
    return APPLIED


def _write(offer: MarketOffer, kind: str, data: dict) -> None:
    if kind == "offer":
        offer.quantity = _decimal(data["quantity"])
        offer.remaining = _decimal(data["quantity"])
        offer.price_per_energy = _decimal(data["pricePerEnergy"])
    elif kind == "offer-updated":
        offer.remaining = _decimal(data["remaining"])
        offer.price_per_energy = _decimal(data["pricePerEnergy"])
    elif kind == "offer-withdrawn":
        offer.status = OFFER_WITHDRAWN


async def _close_market(
    session: AsyncSession, message: dict, now: datetime, settings: Settings, outcome: str | None
) -> str:
    """`market-closed`: el libro de ese ciclo (y de los anteriores) queda cerrado."""
    cycle_id = message.get("cycleId")
    if not cycle_id:
        return IGNORED
    closed = await session.execute(
        update(MarketOffer)
        .where(MarketOffer.cycle_id == cycle_id, MarketOffer.status == OFFER_OPEN)
        .values(status=OFFER_CLOSED)
    )
    from app.market.buyer import close_cycle_bids

    expired = await close_cycle_bids(session, cycle_id)
    return APPLIED if closed.rowcount or expired else IGNORED


def register() -> None:
    for kind in ("offer", "offer-updated", "offer-withdrawn"):
        register_handler(kind, _apply_offer_message)
    # market-closed ya lo registra la wallet (rol B); el libro solo agrega su paso.
    extend_handler("market-closed", _close_market)
