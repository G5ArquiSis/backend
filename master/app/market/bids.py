"""Recepción, validación y reserva atómica de bids (Rol C, tarea C3).

El handler se ejecuta dentro de la transacción de ``apply_central_message``.
Primero registra el idpk único y luego bloquea la oferta con ``FOR UPDATE``. Una
reserva aceptada queda en estado ``reserved`` para que C4 proponga el swap a la
wallet usando el mismo idpk como clave de idempotencia.

La identidad AMQP (user_id == cityId) se valida en connector por A2, antes de
que este handler reciba el mensaje.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.handler_registry import register_handler
from app.market.models import SellerBid, SellerOffer
from app.market.seller import ACTIVE, CLOSED
from app.market.strategy import get_active_strategy
from app.models import LedgerEvent
from app.outbox import Destination, enqueue_publish

APPLIED = "applied"
DUPLICATE = "duplicate"
RESERVED = "reserved"
REJECTED = "rejected"

_REASON_CODES = {
    "QUANTITY_UNAVAILABLE": 409,
    "OFFER_WITHDRAWN": 410,
    "BID_TOO_SMALL": 422,
    "MARKET_CLOSED": 410,
    "SELF_TRADE": 422,
}


def _required_text(value: object, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} debe ser un texto no vacío")
    value = value.strip()
    if len(value) > max_length:
        raise ValueError(f"{field} excede {max_length} caracteres")
    return value


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise TypeError(f"{field} debe ser un número entero positivo")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{field} debe ser un número entero positivo") from error
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise ValueError(f"{field} debe ser un número entero positivo")
    return int(number)


def _nonnegative_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise TypeError(f"{field} debe ser numérico")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{field} debe ser numérico") from error
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} debe ser un número finito no negativo")
    return result


def _parse_timestamp(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"{field} debe ser una fecha ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} debe incluir zona horaria")
    return parsed


def _timestamp(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now debe incluir zona horaria")
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_bid(message: dict[str, Any]) -> tuple[str, str, str, str, str, int]:
    """Valida los campos de negocio que C necesita después del envelope de A."""
    cycle_id = _required_text(message.get("cycleId"), "cycleId", 64)
    buyer_city = _required_text(message.get("cityId"), "cityId", 16).upper()
    idpk = _required_text(message.get("idpk"), "idpk", 64)
    msg_id = _required_text(message.get("msgId"), "msgId", 64)
    data = message.get("data")
    if not isinstance(data, dict):
        raise TypeError("data debe ser un objeto")
    offer_id = _required_text(data.get("offerId"), "data.offerId", 128)
    quantity = _positive_integer(data.get("quantity"), "data.quantity")
    return cycle_id, buyer_city, idpk, msg_id, offer_id, quantity


async def _market_rules(
    session: AsyncSession, cycle_id: str
) -> tuple[Decimal, datetime]:
    """Lee minBid y lastBidAt del status-statement inmutable de ese ciclo."""
    result = await session.execute(
        select(LedgerEvent.payload)
        .where(
            LedgerEvent.cycle_id == cycle_id,
            LedgerEvent.kind == "status-statement",
        )
        .order_by(LedgerEvent.id.desc())
        .limit(1)
    )
    status_message = result.scalar_one_or_none()
    if not isinstance(status_message, dict):
        raise ValueError("no existe el status-statement persistido para el ciclo")
    data = status_message.get("data")
    market = data.get("market") if isinstance(data, dict) else None
    if not isinstance(market, dict):
        raise ValueError("status-statement no contiene data.market")
    if market.get("minBid") is None or market.get("lastBidAt") is None:
        raise ValueError("status-statement requiere market.minBid y market.lastBidAt")
    min_bid = _nonnegative_decimal(market["minBid"], "market.minBid")
    last_bid_at = _parse_timestamp(market["lastBidAt"], "market.lastBidAt")
    return min_bid, last_bid_at


async def _record_rejection(
    session: AsyncSession,
    bid: SellerBid,
    *,
    reason: str,
    available: int | None,
    now: datetime,
    settings: Settings,
) -> None:
    """Persiste y encola una sola respuesta de rechazo al comprador."""
    bid.status = REJECTED
    bid.reason = reason
    bid.available = available
    data: dict[str, Any] = {
        "target": bid.msg_id,
        "offerId": bid.offer_id,
        "requested": bid.quantity,
    }
    if reason == "QUANTITY_UNAVAILABLE" and available is not None:
        data["available"] = available

    payload = {
        "idpk": str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": "bid-rejected",
        "timestamp": _timestamp(now),
        "cityId": settings.city_code.upper(),
        "cycleId": bid.cycle_id,
        "reason": reason,
        "code": _REASON_CODES[reason],
        "data": data,
    }
    await enqueue_publish(
        session,
        dedupe_key=f"seller-bid:{bid.idpk}:rejected",
        payload=payload,
        destination=Destination.city(bid.buyer_city),
    )


async def apply_bid_message(
    session: AsyncSession,
    message: dict[str, Any],
    now: datetime,
    settings: Settings,
) -> str:
    """Registra el bid y reserva toda su cantidad, o encola su rechazo.

    La transacción exterior confirma juntos el registro del idpk, el descuento de
    remaining y el mensaje de rechazo, cuando corresponde. No se llama a la wallet
    aquí: C4 consume las filas ``reserved`` y propone el swap fuera de la transacción.
    """
    cycle_id, buyer_city, idpk, msg_id, offer_id, quantity = _parse_bid(message)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now debe incluir zona horaria")

    inserted = await session.execute(
        postgres_insert(SellerBid)
        .values(
            idpk=idpk,
            msg_id=msg_id,
            cycle_id=cycle_id,
            offer_id=offer_id,
            buyer_city=buyer_city,
            quantity=quantity,
            status="processing",
            payload=message,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[SellerBid.idpk])
        .returning(SellerBid.id)
    )
    if inserted.scalar_one_or_none() is None:
        return DUPLICATE

    bid_result = await session.execute(
        select(SellerBid).where(SellerBid.idpk == idpk).with_for_update()
    )
    bid = bid_result.scalar_one()

    # El identity check real corresponde a A2 (AMQP user_id contra cityId). Esta
    # comprobación adicional evita una autocompra incluso en pruebas o llamadas internas.
    if not settings.market_enabled:
        await _record_rejection(
            session, bid, reason="MARKET_CLOSED", available=None, now=now, settings=settings
        )
        return APPLIED

    if buyer_city == settings.city_code.upper():
        await _record_rejection(
            session, bid, reason="SELF_TRADE", available=None, now=now, settings=settings
        )
        return APPLIED

    offer_result = await session.execute(
        select(SellerOffer)
        .where(SellerOffer.cycle_id == cycle_id)
        .with_for_update()
    )
    offer = offer_result.scalar_one_or_none()

    if offer is None:
        await _record_rejection(
            session, bid, reason="OFFER_WITHDRAWN", available=None, now=now, settings=settings
        )
        return APPLIED

    if offer.market_closed_at is not None or offer.status == CLOSED:
        await _record_rejection(
            session, bid, reason="MARKET_CLOSED", available=None, now=now, settings=settings
        )
        return APPLIED

    if offer.offer_id != offer_id:
        await _record_rejection(
            session, bid, reason="OFFER_WITHDRAWN", available=None, now=now, settings=settings
        )
        return APPLIED

    if offer.status != ACTIVE:
        await _record_rejection(
            session, bid, reason="OFFER_WITHDRAWN", available=None, now=now, settings=settings
        )
        return APPLIED

    min_bid, last_bid_at = await _market_rules(session, cycle_id)
    if now >= last_bid_at:
        await _record_rejection(
            session, bid, reason="MARKET_CLOSED", available=None, now=now, settings=settings
        )
        return APPLIED

    if Decimal(quantity) < min_bid:
        await _record_rejection(
            session, bid, reason="BID_TOO_SMALL", available=None, now=now, settings=settings
        )
        return APPLIED

    available = int(offer.remaining or 0)
    if quantity > available:
        await _record_rejection(
            session,
            bid,
            reason="QUANTITY_UNAVAILABLE",
            available=available,
            now=now,
            settings=settings,
        )
        return APPLIED

    if offer.price_per_energy is None:
        raise ValueError("la oferta activa no tiene price_per_energy")

    strategy = await get_active_strategy(cycle_id, session=session)
    if strategy.bid_acceptance_policy != "arrival_order":
        # largest_quantity_first necesita arbitrar un conjunto de bids antes de
        # reservar; no se simula esa política aceptando en orden de llegada. C5
        # deberá habilitarla junto con su mecanismo de agrupación/prioridad.
        raise ValueError(
            "bid_acceptance_policy largest_quantity_first requiere arbitraje por lote (C5)"
        )

    bid.status = RESERVED
    bid.price_per_energy = offer.price_per_energy
    bid.reserved_at = now
    offer.remaining = available - quantity
    offer.updated_at = now
    return APPLIED


register_handler("bid", apply_bid_message)
