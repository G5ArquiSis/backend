"""Propuestas de swaps para bids reservados (Rol C, tarea C4)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import and_, or_, select, update

from app.config import Settings
from app.database import _session_factory
from app.ledger import total_amount
from app.market import seller as market_seller
from app.market.models import SellerBid
from app.models import WalletEvent
from app.outbox import Destination, enqueue_publish
from app.wallet.client import WalletClient, WalletError
from app.wallet.correlation import register_swap_cycle

logger = logging.getLogger(__name__)

_EXPIRATION_SECONDS = 60

# WalletClient realiza hasta cuatro intentos HTTP, con timeout de 20 segundos
# y esperas entre reintentos. El intervalo evita que otro worker reclame el
# mismo bid mientras la primera llamada todavía está ejecutándose.
_RETRY_SECONDS = 120

_BATCH_SIZE = 10

_RETRYABLE_WALLET_STATUSES = {429, 502, 503, 504}
_TERMINAL_SWAP_EVENTS = {"swap-settled", "swap-rejected", "swap-expired", "swap-cancelled"}


def _timestamp(value: datetime) -> str:
    """Serializa una fecha con zona horaria en formato UTC ISO-8601."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("La fecha debe incluir zona horaria")

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_number(value: Decimal) -> int | float:
    """Convierte Decimal a un número compatible con JSON."""
    return int(value) if value == value.to_integral_value() else float(value)


async def reconcile_pending_swap_proposals(
    session,
    now: datetime,
    settings: Settings,
) -> int:
    """Reprocesa eventos swap-proposed recibidos antes de guardar el swapId.

    El evento ya está persistido en WalletEvent. Cuando la respuesta HTTP permite
    asociar su swapId al SellerBid, esta función recupera el payload y completa
    la aceptación. La transacción debe ser confirmada por quien llama.
    """
    result = await session.execute(
        select(WalletEvent)
        .join(SellerBid, SellerBid.swap_id == WalletEvent.swap_id)
        .where(
            WalletEvent.event_type == "swap-proposed",
            WalletEvent.swap_id.is_not(None),
            SellerBid.status.in_(("reserved", "wallet_error")),
        )
        .order_by(WalletEvent.received_at, WalletEvent.id)
        .limit(_BATCH_SIZE)
    )

    events = result.scalars().all()
    processed_swaps: set[str] = set()

    for wallet_event in events:
        swap_id = wallet_event.swap_id
        if swap_id is None or swap_id in processed_swaps:
            continue

        processed_swaps.add(swap_id)
        await apply_swap_proposed(
            session,
            wallet_event.payload,
            now,
            settings,
        )

    return len(processed_swaps)


async def process_reserved_bids(settings: Settings) -> None:
    """Propone a Wallet los swaps de bids reservados y reconcilia eventos.

    C3 ya descontó la cantidad solicitada de SellerOffer.remaining. Este worker
    no vuelve a reservar energía ni envía bid-accepted: eso ocurre al procesar
    swap-proposed. La reconciliación se ejecuta aunque no haya nuevos bids.
    """
    now = datetime.now(UTC)

    # Un evento puede haber llegado antes que la respuesta HTTP. Reprocesamos
    # los eventos ya asociados a bids antes de buscar nuevas propuestas.
    async with _session_factory() as session:
        await reconcile_pending_swap_events(session, now, settings)
        await reconcile_pending_swap_proposals(session, now, settings)
        await session.commit()

    if (
        not settings.market_enabled
        or not settings.wallet_enabled
        or not settings.wallet_base_url
        or not settings.wallet_api_key
    ):
        return

    jobs: list[dict] = []

    # Primero reclamamos los bids y persistimos el intento. La llamada HTTP se
    # realiza después del commit, sin mantener bloqueos de base de datos.
    async with _session_factory() as session:
        due = await session.execute(
            select(SellerBid)
            .where(
                SellerBid.status == "reserved",
                SellerBid.swap_id.is_(None),
                or_(
                    SellerBid.proposal_attempted_at.is_(None),
                    SellerBid.proposal_attempted_at
                    <= now - timedelta(seconds=_RETRY_SECONDS),
                ),
            )
            .order_by(SellerBid.created_at, SellerBid.id)
            .limit(_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )

        for bid in due.scalars().all():
            if bid.price_per_energy is None:
                logger.error(
                    "Bid %s reservado sin precio; no se puede proponer el swap",
                    bid.idpk,
                )
                continue

            # Se conserva el mismo vencimiento en todos los reintentos.
            # Nunca se cambia el cuerpo de una operación que usa el mismo idpk.
            if bid.swap_expires_at is None:
                bid.swap_expires_at = now + timedelta(
                    seconds=_EXPIRATION_SECONDS
                )

            bid.proposal_attempted_at = now

            amount = total_amount(
                Decimal(bid.quantity),
                Decimal(bid.price_per_energy),
            )

            jobs.append(
                {
                    "idpk": bid.idpk,
                    "quantity": bid.quantity,
                    "offer_id": bid.offer_id,
                    "amount": amount,
                    "expires_at": _timestamp(bid.swap_expires_at),
                }
            )

        await session.commit()

    if not jobs:
        return

    # No mantenemos bloqueada la base de datos durante las llamadas HTTP.
    async with WalletClient(
        settings.wallet_base_url,
        settings.wallet_api_key,
    ) as wallet:
        for job in jobs:
            try:
                response = await wallet.propose_swap(
                    give={
                        "asset": "kwh",
                        "amount": job["quantity"],
                    },
                    take={
                        "asset": "credits",
                        "amount": _json_number(job["amount"]),
                    },
                    expires_at=job["expires_at"],
                    memo=job["offer_id"],
                    idempotency_key=job["idpk"],
                )

            except WalletError as error:
                if error.status_code in _RETRYABLE_WALLET_STATUSES:
                    # WalletClient ya agotó sus reintentos internos.
                    # Conservamos la reserva y la misma operación para
                    # volver a intentarlo posteriormente.
                    logger.warning(
                        "Wallet temporalmente no disponible para bid %s: HTTP %s",
                        job["idpk"],
                        error.status_code,
                    )
                else:
                    # Un error HTTP definitivo no equivale a un evento
                    # swap-rejected. No liberamos la reserva automáticamente.
                    logger.error(
                        "Error HTTP definitivo al proponer bid %s: "
                        "HTTP %s, payload=%r",
                        job["idpk"],
                        error.status_code,
                        error.payload,
                    )

                    async with _session_factory() as session:
                        await session.execute(
                            update(SellerBid)
                            .where(
                                SellerBid.idpk == job["idpk"],
                                SellerBid.status == "reserved",
                                SellerBid.swap_id.is_(None),
                            )
                            .values(
                                status="wallet_error",
                                reason=f"WALLET_HTTP_{error.status_code}",
                            )
                        )
                        await session.commit()

                continue

            except httpx.TransportError as error:
                # Un timeout o una desconexión no demuestra que Wallet haya
                # rechazado la operación. Se conserva la misma idempotencia.
                logger.warning(
                    "Error de transporte para bid %s: %s. "
                    "Se reintentará con la misma idempotencia.",
                    job["idpk"],
                    type(error).__name__,
                )
                continue

            # HTTP 202 significa recepción, no liquidación. El resultado
            # definitivo llegará mediante eventos de Wallet.
            swap_id = (
                response.get("swapId")
                if isinstance(response, dict)
                else None
            )

            if not isinstance(swap_id, str) or not swap_id.strip():
                logger.warning(
                    "Wallet no devolvió swapId para el bid %s. "
                    "Se espera el evento o un reintento idempotente.",
                    job["idpk"],
                )
                continue

            # Primero obtenemos el ciclo sin bloquear el bid. La asociación
            # Wallet se registra antes de bloquear SellerBid para mantener el
            # mismo orden de bloqueos que el handler de eventos.
            async with _session_factory() as session:
                result = await session.execute(
                    select(SellerBid).where(SellerBid.idpk == job["idpk"])
                )
                bid = result.scalar_one_or_none()

                if bid is None:
                    logger.error(
                        "No se encontró el bid %s al guardar el swap %s",
                        job["idpk"],
                        swap_id,
                    )
                    continue

                if bid.swap_id is not None and bid.swap_id != swap_id:
                    raise ValueError(
                        f"El bid {bid.idpk} ya tiene otro swap asociado"
                    )

                await register_swap_cycle(session, swap_id, bid.cycle_id)

                result = await session.execute(
                    select(SellerBid)
                    .where(SellerBid.idpk == job["idpk"])
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                bid = result.scalar_one_or_none()

                if bid is None:
                    logger.error(
                        "No se encontró el bid %s al guardar el swap %s",
                        job["idpk"],
                        swap_id,
                    )
                    continue

                if bid.swap_id is not None and bid.swap_id != swap_id:
                    raise ValueError(
                        f"El bid {bid.idpk} ya tiene otro swap asociado"
                    )

                bid.swap_id = swap_id
                await session.commit()

            # Si swap-proposed llegó antes de la respuesta HTTP y no incluía
            # data.idpk, ahora ya podemos encontrarlo por swapId y procesarlo.
            # Esta segunda transacción evita mantener el bloqueo del bid
            # mientras se actualizan las asociaciones/eventos Wallet.
            async with _session_factory() as session:
                reconcile_now = datetime.now(UTC)
                await reconcile_pending_swap_events(session, reconcile_now, settings)
                await reconcile_pending_swap_proposals(
                    session,
                    reconcile_now,
                    settings,
                )
                await session.commit()


async def apply_swap_proposed(
    session,
    message: dict,
    now: datetime,
    settings: Settings,
) -> None:
    """Guarda el swap propuesto y notifica su aceptación al comprador."""
    data = message.get("data")
    if not isinstance(data, dict):
        raise ValueError("swap-proposed requiere data")

    swap_id = data.get("swapId")
    operation_idpk = data.get("idpk")
    token = data.get("token")

    if not isinstance(swap_id, str) or not swap_id.strip():
        raise ValueError("swap-proposed requiere data.swapId")

    if operation_idpk is not None and not isinstance(operation_idpk, str):
        raise ValueError("data.idpk debe ser texto")

    # La operación original se identifica mediante el idpk del bid.
    conditions = [SellerBid.swap_id == swap_id]
    if operation_idpk:
        conditions.append(SellerBid.idpk == operation_idpk)

    # Primero identificamos el ciclo sin bloquear SellerBid. El handler y el
    # worker deben registrar WalletSwapCycle/WalletEvent antes de bloquear el
    # bid, evitando adquirir estos bloqueos en orden inverso.
    result = await session.execute(
        select(SellerBid).where(or_(*conditions))
    )
    candidate = result.scalar_one_or_none()

    # Puede tratarse de un swap que no pertenece a una venta nuestra.
    if candidate is None:
        return

    if candidate.swap_id is not None and candidate.swap_id != swap_id:
        raise ValueError(
            f"El bid {candidate.idpk} ya tiene otro swap asociado"
        )

    await register_swap_cycle(
        session,
        swap_id,
        candidate.cycle_id,
    )

    result = await session.execute(
        select(SellerBid)
        .where(or_(*conditions))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    bid = result.scalar_one_or_none()

    if bid is None:
        return

    if bid.swap_id is not None and bid.swap_id != swap_id:
        raise ValueError(f"El bid {bid.idpk} ya tiene otro swap asociado")

    if bid.price_per_energy is None:
        raise ValueError(f"El bid {bid.idpk} no tiene precio")

    expires_raw = data.get("expiresAt")
    if expires_raw is not None:
        if not isinstance(expires_raw, str):
            raise ValueError("data.expiresAt debe ser texto")

        expires_at = datetime.fromisoformat(
            expires_raw.replace("Z", "+00:00")
        )
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("data.expiresAt debe incluir zona horaria")

        bid.swap_expires_at = expires_at

    if bid.swap_expires_at is None:
        raise ValueError("El bid no tiene fecha de expiración del swap")

    bid.swap_id = swap_id

    # Un evento terminal puede haber llegado antes que la respuesta HTTP o que
    # swap-proposed. Al conocer el swapId lo reconciliamos antes de aceptar.
    await reconcile_pending_swap_events(session, now, settings)
    await session.refresh(bid)

    # Un evento tardío no debe reactivar un bid que ya terminó.
    if bid.status not in {"reserved", "wallet_error", "proposed"}:
        return

    # El token solo es obligatorio cuando el swap corresponde
    # a uno de nuestros bids y debemos notificar su aceptación.
    if not isinstance(token, str) or not token.strip():
        raise ValueError("swap-proposed requiere data.token")

    bid.status = "proposed"

    amount = total_amount(
        Decimal(bid.quantity),
        Decimal(bid.price_per_energy),
    )

    payload = {
        "idpk": str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": "bid-accepted",
        "timestamp": _timestamp(now),
        "cityId": settings.city_code.upper(),
        "cycleId": bid.cycle_id,
        "data": {
            "target": bid.msg_id,
            "offerId": bid.offer_id,
            "quantity": bid.quantity,
            "pricePerEnergy": _json_number(Decimal(bid.price_per_energy)),
            "amount": _json_number(amount),
            "swapId": swap_id,
            "swapToken": token,
            "expiresAt": _timestamp(bid.swap_expires_at),
        },
    }

    await enqueue_publish(
        session,
        dedupe_key=f"seller-bid:{bid.idpk}:accepted",
        payload=payload,
        destination=Destination.city(bid.buyer_city),
    )


async def reconcile_pending_swap_events(
    session,
    now: datetime,
    settings: Settings,
) -> int:
    """Aplica eventos terminales que llegaron antes de asociar el swap al bid.

    Los eventos pueden traer el idpk de una operación propia o externa. Se intenta
    correlacionar por idpk contra SellerBid; un idpk externo no tendrá coincidencia.
    El swapId sigue siendo la correlación principal cuando ya se conoce.
    """
    pending_swap_ids = select(SellerBid.swap_id).where(
        SellerBid.swap_id.is_not(None),
        SellerBid.status.in_(("reserved", "wallet_error", "proposed")),
    )
    pending_bid_ids = select(SellerBid.idpk).where(
        SellerBid.status.in_(("reserved", "wallet_error", "proposed")),
    )
    terminal_fact = or_(
        WalletEvent.event_type.in_(("swap-settled", "swap-expired", "swap-cancelled")),
        and_(
            WalletEvent.event_type == "swap-rejected",
            or_(
                WalletEvent.payload["data"]["reason"].astext == "INSUFFICIENT_ASSET",
                WalletEvent.payload["reason"].astext == "INSUFFICIENT_ASSET",
            ),
        ),
    )
    result = await session.execute(
        select(WalletEvent)
        .where(
            terminal_fact,
            or_(
                WalletEvent.swap_id.in_(pending_swap_ids),
                WalletEvent.operation_idpk.in_(pending_bid_ids),
            ),
        )
        .order_by(WalletEvent.received_at, WalletEvent.id)
        .limit(_BATCH_SIZE)
    )
    events = result.scalars().all()
    processed = 0
    for wallet_event in events:
        if await apply_swap_terminal_event(
            session, wallet_event.payload, now, settings
        ):
            processed += 1
    return processed


async def apply_swap_terminal_event(
    session,
    message: dict,
    now: datetime,
    settings: Settings,
) -> bool:
    """Aplica la liquidación, rechazo, expiración o cancelación de un swap propio."""
    event_type = message.get("type")
    if event_type not in _TERMINAL_SWAP_EVENTS:
        raise ValueError("tipo de evento swap terminal inválido")

    data = message.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"{event_type} requiere data")

    swap_id = data.get("swapId")
    if not isinstance(swap_id, str) or not swap_id.strip():
        raise ValueError(f"{event_type} requiere data.swapId")

    operation_idpk = data.get("idpk")
    if operation_idpk is not None and not isinstance(operation_idpk, str):
        raise ValueError("data.idpk debe ser texto")

    reason = data.get("reason") or message.get("reason")
    operation = data.get("operation") or message.get("operation")

    # swap-rejected solo es terminal cuando la wallet informa saldo insuficiente.
    # Otros rechazos de accept pueden ser intentos inválidos sin cancelar la propuesta.
    if event_type == "swap-rejected" and reason != "INSUFFICIENT_ASSET":
        return False

    by_swap_result = await session.execute(
        select(SellerBid).where(SellerBid.swap_id == swap_id)
    )
    by_swap = by_swap_result.scalar_one_or_none()

    # El idpk de una operación externa (por ejemplo, un cierre de la central)
    # no coincidirá con SellerBid.idpk. Aun así, intentar la búsqueda permite
    # reconciliar una cancelación iniciada por nosotros antes de conocer swapId.
    may_match_by_operation = operation != "accept" and bool(operation_idpk)
    by_operation = None
    if may_match_by_operation:
        by_operation_result = await session.execute(
            select(SellerBid).where(SellerBid.idpk == operation_idpk)
        )
        by_operation = by_operation_result.scalar_one_or_none()

    if by_swap is not None and by_operation is not None and by_swap.id != by_operation.id:
        raise ValueError("el evento wallet apunta a dos bids distintos")

    candidate = by_swap or by_operation
    if candidate is None:
        # Puede ser un swap ajeno o un evento que llegó antes de conocer el bid.
        return False

    if candidate.swap_id is not None and candidate.swap_id != swap_id:
        raise ValueError(f"el bid {candidate.idpk} ya tiene otro swap asociado")

    # Registrar la relación antes de bloquear el bid conserva el orden de locks
    # utilizado por apply_swap_proposed y el worker HTTP.
    await register_swap_cycle(session, swap_id, candidate.cycle_id)

    bid_result = await session.execute(
        select(SellerBid)
        .where(SellerBid.id == candidate.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    bid = bid_result.scalar_one_or_none()
    if bid is None:
        return False
    if bid.swap_id is not None and bid.swap_id != swap_id:
        raise ValueError(f"el bid {bid.idpk} ya tiene otro swap asociado")
    bid.swap_id = swap_id

    # El primer evento terminal válido gana. Las escrituras de la wallet son
    # atómicas: un mismo swap no puede expirar/cancelarse y luego liquidarse.
    if bid.status not in {"reserved", "wallet_error", "proposed"}:
        return False

    if event_type == "swap-settled":
        bid.status = "settled"
        bid.reason = None
        await market_seller.publish_current_offer_remaining(
            session, bid.cycle_id, now, settings
        )
        return True

    if event_type == "swap-rejected":
        bid.status = "rejected"
        bid.reason = "INSUFFICIENT_ASSET"
    elif event_type == "swap-expired":
        bid.status = "expired"
        bid.reason = "SWAP_EXPIRED"
    else:  # swap-cancelled
        bid.status = "cancelled"
        bid.reason = "SWAP_CANCELLED"

    offer_result = await session.execute(
        select(market_seller.SellerOffer)
        .where(market_seller.SellerOffer.cycle_id == bid.cycle_id)
        .with_for_update()
    )
    offer = offer_result.scalar_one_or_none()
    if offer is not None:
        restored_remaining = int(offer.remaining or 0) + bid.quantity
        await market_seller.restore_offer_remaining(
            session, bid.cycle_id, restored_remaining, now, settings
        )

    # Solo el rechazo de nuestra propuesta ocurre antes de bid-accepted y debe
    # traducirse a bid-rejected. En un accept fallido el comprador ya recibió
    # bid-accepted y también recibe el swap-rejected de la wallet.
    proposal_rejected = (
        event_type == "swap-rejected"
        and (operation == "propose" or (operation is None and operation_idpk == bid.idpk))
    )
    if proposal_rejected:
        rejection_payload = {
            "idpk": str(uuid.uuid4()),
            "msgId": str(uuid.uuid4()),
            "type": "bid-rejected",
            "timestamp": _timestamp(now),
            "cityId": settings.city_code.upper(),
            "cycleId": bid.cycle_id,
            "reason": "INSUFFICIENT_ASSET",
            "code": (
                data["code"]
                if isinstance(data.get("code"), int) and not isinstance(data.get("code"), bool)
                else 409
            ),
            "data": {
                "target": bid.msg_id,
                "offerId": bid.offer_id,
                "requested": bid.quantity,
            },
        }
        await enqueue_publish(
            session,
            dedupe_key=f"seller-bid:{bid.idpk}:rejected",
            payload=rejection_payload,
            destination=Destination.city(bid.buyer_city),
        )

    return True
