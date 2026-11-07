"""Ciclo de vida de la oferta propia (Rol C, tarea C2).

Este módulo es dueño de la proyección de la oferta propia y de sus publicaciones.
No procesa bids ni swaps: C3/C4 llaman a ``set_offer_remaining`` cuando el estado
operacional de una venta cambia.

La outbox y la fila ``seller_offers`` se actualizan en la misma transacción. La
clave única por ciclo y el bloqueo ``FOR UPDATE`` hacen que ambas réplicas de
master no creen ni publiquen dos ofertas para el mismo ciclo.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.market.models import SellerOffer
from app.market.strategy import StrategyConfig, get_active_strategy
from app.outbox import Destination, enqueue_publish

APPLIED = "applied"
DUPLICATE = "duplicate"
ACTIVE = "active"
NO_OFFER = "no_offer"
WITHDRAWN = "withdrawn"
CLOSED = "closed"

_CENT = Decimal("0.01")


def calculate_offer_price(
    buyback_price: Decimal,
    imbalance_price: Decimal,
    strategy: StrategyConfig,
) -> Decimal:
    """Calcula el precio configurado y lo mantiene dentro del intervalo del ciclo.

    ``market_fraction`` interpola entre el piso de recompra y el techo de
    desbalance. En modo ``fixed`` se acota al mismo intervalo para que una
    configuración administrativa no publique un precio fuera de las reglas C2.
    """
    floor_price = _money(buyback_price)
    ceiling_price = _money(imbalance_price)
    if floor_price < 0 or ceiling_price < floor_price:
        raise ValueError("intervalo de precios del mercado inválido")

    if strategy.price_mode == "fixed":
        if strategy.price_per_energy is None:
            raise ValueError("la estrategia fixed necesita price_per_energy")
        candidate = _money(strategy.price_per_energy)
    else:
        candidate = _money(
            floor_price + strategy.price_factor * (ceiling_price - floor_price)
        )

    return min(max(candidate, floor_price), ceiling_price)


def calculate_offer_quantity(sellable: object, strategy: StrategyConfig) -> int:
    """Devuelve una cantidad entera positiva que nunca supera ``sellable``."""
    available = _whole_kwh(sellable)
    if available <= 0:
        return 0
    configured = strategy.offer_quantity
    if configured is None:
        return available
    if isinstance(configured, bool) or int(configured) != configured or configured <= 0:
        raise ValueError("offer_quantity debe ser un entero positivo")
    return min(available, int(configured))


def _money(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise TypeError(f"se esperaba un precio numérico y llegó {value!r}")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("el precio debe ser finito")
    return number.quantize(_CENT, rounding=ROUND_HALF_UP)


def _whole_kwh(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise TypeError(f"se esperaba una cantidad de KWH y llegó {value!r}")
    quantity = Decimal(str(value))
    if not quantity.is_finite():
        raise ValueError("la cantidad debe ser finita")
    if quantity < 0:
        raise ValueError("sellable no puede ser negativo")
    return int(quantity.to_integral_value(rounding=ROUND_FLOOR))


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"se esperaba una fecha ISO-8601 y llegó {value!r}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("lastOfferAt debe incluir zona horaria")
    return parsed


def _timestamp(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now debe incluir zona horaria")
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _new_envelope(
    message_type: str,
    cycle_id: str,
    data: dict[str, Any],
    now: datetime,
    settings: Settings,
) -> dict[str, Any]:
    return {
        "idpk": str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": message_type,
        "timestamp": _timestamp(now),
        "cityId": settings.city_code,
        "cycleId": cycle_id,
        "data": data,
    }


async def _get_or_create_cycle_offer(
    session: AsyncSession,
    cycle_id: str,
) -> SellerOffer:
    """Crea el marcador de ciclo si aún no existe y bloquea su fila."""
    await session.execute(
        postgres_insert(SellerOffer)
        .values(
            cycle_id=cycle_id,
            remaining=0,
            status=NO_OFFER,
            publish_version=0,
        )
        .on_conflict_do_nothing(index_elements=[SellerOffer.cycle_id])
    )
    result = await session.execute(
        select(SellerOffer)
        .where(SellerOffer.cycle_id == cycle_id)
        .with_for_update()
    )
    return result.scalar_one()


async def _publish(
    session: AsyncSession,
    offer: SellerOffer,
    message_type: str,
    data: dict[str, Any],
    now: datetime,
    settings: Settings,
) -> None:
    """Encola una versión nueva; debe llamarse con la fila de oferta bloqueada."""
    offer.publish_version += 1
    payload = _new_envelope(message_type, offer.cycle_id, data, now, settings)
    dedupe_key = f"seller-offer:{offer.cycle_id}:v{offer.publish_version}"
    await enqueue_publish(session, dedupe_key, payload, Destination.MARKET_OFFER)
    offer.updated_at = now


async def ensure_offer_for_cycle(
    session: AsyncSession,
    message: dict[str, Any],
    now: datetime,
    settings: Settings,
) -> SellerOffer | None:
    """Publica como máximo una oferta al procesar el status-statement del ciclo.

    El `sellable` puede venir en ``data.energy`` (forma descrita en el enunciado)
    o en ``data.market`` (forma expuesta en el detalle B4 de la distribución).
    ``market-closed`` se persiste incluso si llega antes que el status-statement.
    """
    if not settings.market_enabled:
        return None

    cycle_id = message.get("cycleId")
    data = message.get("data")
    if not isinstance(cycle_id, str) or not cycle_id.strip() or not isinstance(data, dict):
        raise ValueError("status-statement requiere cycleId y data")

    market = data.get("market")
    energy = data.get("energy")
    if not isinstance(market, dict) or not isinstance(energy, dict):
        # Mensajes E1 / status-statement sin extensión de mercado: no se oferta.
        return None

    last_offer_raw = market.get("lastOfferAt")
    buyback_raw = market.get("buybackPrice")
    imbalance_raw = market.get("imbalancePrice")
    sellable_raw = energy.get("sellable")
    if sellable_raw is None:
        sellable_raw = market.get("sellable")
    if None in (last_offer_raw, buyback_raw, imbalance_raw, sellable_raw):
        # Si B4 todavía no ha entregado el bloque extendido, no inventamos plazos
        # ni precios. El evento queda persistido por el handler de B.
        return None

    last_offer_at = _parse_datetime(last_offer_raw)
    buyback_price = _money(buyback_raw)
    imbalance_price = _money(imbalance_raw)
    sellable = _whole_kwh(sellable_raw)

    offer = await _get_or_create_cycle_offer(session, cycle_id)
    if offer.status == CLOSED or offer.market_closed_at is not None:
        return offer
    if offer.status != NO_OFFER:
        # Idempotencia adicional a la del status-statement y al índice único.
        return offer

    offer.last_offer_at = last_offer_at
    offer.buyback_price = buyback_price
    offer.imbalance_price = imbalance_price

    # Congela la estrategia al recibir el status-statement del ciclo, incluso si
    # no hay energía vendible. Un cambio posterior de configuración solo afecta
    # al siguiente ciclo.
    strategy = await get_active_strategy(cycle_id, session=session)

    if now >= last_offer_at:
        offer.reason = "LAST_OFFER_AT_PASSED"
        return offer

    if sellable <= 0:
        offer.reason = "NO_SELLABLE_ENERGY"
        return offer

    quantity = calculate_offer_quantity(sellable, strategy)
    if quantity <= 0:
        offer.reason = "NO_SELLABLE_ENERGY"
        return offer

    price = calculate_offer_price(buyback_price, imbalance_price, strategy)
    offer_id = f"{settings.city_code.upper()}-{cycle_id}"
    offer.offer_id = offer_id
    offer.quantity = quantity
    offer.remaining = quantity
    offer.price_per_energy = price
    offer.status = ACTIVE
    offer.reason = None

    await _publish(
        session,
        offer,
        "offer",
        {
            "offerId": offer_id,
            "quantity": quantity,
            "pricePerEnergy": _json_number(price),
        },
        now,
        settings,
    )
    return offer


async def set_offer_remaining(
    session: AsyncSession,
    cycle_id: str,
    remaining: int,
    now: datetime,
    settings: Settings,
) -> bool:
    """Publica ``offer-updated`` cuando C4 informa una venta o liberación.

    ``remaining`` es el saldo actualizado absoluto de la oferta (no la cantidad
    vendida). La operación es idempotente: si no cambió, no genera otro mensaje.
    """
    if not settings.market_enabled:
        return False
    value = _whole_kwh(remaining)
    offer = await _find_offer_for_update(session, cycle_id)
    if offer is None or offer.status != ACTIVE or offer.market_closed_at is not None:
        return False
    if not _before_last_offer(offer, now):
        return False
    if value > (offer.quantity or 0):
        raise ValueError("remaining no puede superar la cantidad publicada")
    if value == offer.remaining:
        return False

    offer.remaining = value
    await _publish(
        session,
        offer,
        "offer-updated",
        {
            "offerId": offer.offer_id,
            "remaining": value,
            "pricePerEnergy": _json_number(offer.price_per_energy),
        },
        now,
        settings,
    )
    return True


async def restore_offer_remaining(
    session: AsyncSession,
    cycle_id: str,
    remaining: int,
    now: datetime,
    settings: Settings,
) -> bool:
    """Devuelve al libro local una reserva de kWh que no llegó a liquidarse.

    El estado local se corrige incluso si la ventana de publicación ya terminó.
    Si la oferta todavía admite actualizaciones públicas, el cambio se publica
    en la misma transacción.
    """
    value = _whole_kwh(remaining)
    offer = await _find_offer_for_update(session, cycle_id)
    if offer is None:
        return False
    if value > (offer.quantity or 0):
        raise ValueError("remaining no puede superar la cantidad publicada")
    if value == offer.remaining:
        return False

    offer.remaining = value
    if (
        settings.market_enabled
        and offer.status == ACTIVE
        and offer.market_closed_at is None
    ):
        await _publish(
            session,
            offer,
            "offer-updated",
            {
                "offerId": offer.offer_id,
                "remaining": value,
                "pricePerEnergy": _json_number(offer.price_per_energy),
            },
            now,
            settings,
        )
    else:
        offer.updated_at = now
    return True


async def publish_current_offer_remaining(
    session: AsyncSession,
    cycle_id: str,
    now: datetime,
    settings: Settings,
) -> bool:
    """Publica el remaining ya reservado cuando Wallet confirma la liquidación.

    La cantidad se descontó al aceptar el bid; por eso, en el evento settled,
    el valor almacenado no cambia, pero sí debe publicarse una nueva versión.
    """
    if not settings.market_enabled:
        return False
    offer = await _find_offer_for_update(session, cycle_id)
    if (
        offer is None
        or offer.status != ACTIVE
        or offer.market_closed_at is not None
    ):
        return False

    await _publish(
        session,
        offer,
        "offer-updated",
        {
            "offerId": offer.offer_id,
            "remaining": offer.remaining,
            "pricePerEnergy": _json_number(offer.price_per_energy),
        },
        now,
        settings,
    )
    return True


async def change_offer_price(
    session: AsyncSession,
    cycle_id: str,
    price_per_energy: Decimal,
    now: datetime,
    settings: Settings,
) -> bool:
    """Actualiza el precio de la oferta vigente, sin salir de los límites del ciclo."""
    if not settings.market_enabled:
        return False
    price = _money(price_per_energy)
    offer = await _find_offer_for_update(session, cycle_id)
    if offer is None or offer.status != ACTIVE or offer.market_closed_at is not None:
        return False
    if not _before_last_offer(offer, now):
        return False
    if offer.buyback_price is None or offer.imbalance_price is None:
        raise ValueError("la oferta no conserva los límites de precio del ciclo")
    if not offer.buyback_price <= price <= offer.imbalance_price:
        raise ValueError("price_per_energy debe estar entre buybackPrice e imbalancePrice")
    if price == offer.price_per_energy:
        return False

    offer.price_per_energy = price
    await _publish(
        session,
        offer,
        "offer-updated",
        {
            "offerId": offer.offer_id,
            "remaining": offer.remaining,
            "pricePerEnergy": _json_number(price),
        },
        now,
        settings,
    )
    return True


async def withdraw_offer(
    session: AsyncSession,
    cycle_id: str,
    now: datetime,
    settings: Settings,
) -> bool:
    """Retira voluntariamente la oferta mientras el mercado admite publicaciones."""
    if not settings.market_enabled:
        return False
    offer = await _find_offer_for_update(session, cycle_id)
    if offer is None or offer.status != ACTIVE or offer.market_closed_at is not None:
        return False
    if not _before_last_offer(offer, now):
        return False

    offer.status = WITHDRAWN
    await _publish(
        session,
        offer,
        "offer-withdrawn",
        {"offerId": offer.offer_id},
        now,
        settings,
    )
    return True


async def _find_offer_for_update(
    session: AsyncSession,
    cycle_id: str,
) -> SellerOffer | None:
    result = await session.execute(
        select(SellerOffer)
        .where(SellerOffer.cycle_id == cycle_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


def _before_last_offer(offer: SellerOffer, now: datetime) -> bool:
    return (
        offer.last_offer_at is not None
        and now.tzinfo is not None
        and now.utcoffset() is not None
        and now < offer.last_offer_at
    )


async def close_market(
    session: AsyncSession,
    cycle_id: str,
    now: datetime,
) -> bool:
    """Marca el mercado del ciclo como cerrado, incluso si no había oferta.

    B2 invoca esta función desde el handler existente de ``market-closed``; C no
    registra un segundo handler para ese tipo porque ya pertenece al flujo wallet.
    """
    if not isinstance(cycle_id, str) or not cycle_id.strip():
        raise ValueError("market-closed requiere cycleId")

    offer = await _get_or_create_cycle_offer(session, cycle_id)
    if offer.status == CLOSED or offer.market_closed_at is not None:
        return False

    offer.status = CLOSED
    offer.market_closed_at = now
    offer.reason = "MARKET_CLOSED"
    offer.updated_at = now
    return True
