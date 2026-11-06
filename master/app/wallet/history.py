"""Contrato RF01 para C/D: auditar mensajes sin depender de sus modelos internos."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models import CycleMarketMessage, OutboxMessage

MARKET_TYPES = {"offer", "offer-updated", "offer-withdrawn", "bid", "bid-accepted", "bid-rejected"}


async def record_market_message(session, message):
    """C/D pueden invocarla dentro de SU transacción al persistir un mensaje."""
    if message.get("type") in MARKET_TYPES and message.get("cycleId"):
        if message.get("data", {}).get("swapId"):
            from app.wallet.correlation import register_swap_cycle

            await register_swap_cycle(session, message["data"]["swapId"], message["cycleId"])
        await session.execute(
            insert(CycleMarketMessage)
            .values(idpk=message["idpk"], cycle_id=message["cycleId"], payload=message)
            .on_conflict_do_nothing(index_elements=["idpk"])
        )


async def market_history(session, cycle_id):
    incoming = (
        (
            await session.execute(
                select(CycleMarketMessage.payload)
                .where(CycleMarketMessage.cycle_id == cycle_id)
                .order_by(CycleMarketMessage.id)
            )
        )
        .scalars()
        .all()
    )
    outgoing = (
        (
            await session.execute(
                select(OutboxMessage.payload)
                .where(OutboxMessage.payload["cycleId"].astext == cycle_id)
                .order_by(OutboxMessage.id)
            )
        )
        .scalars()
        .all()
    )
    unique = {}
    for message in [*incoming, *outgoing]:
        if message.get("type") in MARKET_TYPES:
            unique[message["idpk"]] = message
    return list(unique.values())


def swap_states(events):
    """Estados de hechos del swap; swap-rejected describe una operación fallida."""
    groups = {}
    priority = {
        "swap-proposed": 1,
        "swap-expired": 2,
        "swap-cancelled": 2,
        "swap-rejected": 2,
        "swap-settled": 3,
    }
    for event in events:
        data = event.get("data", {})
        swap_id = data.get("swapId")
        if not swap_id:
            continue
        row = groups.setdefault(swap_id, {"swapId": swap_id, "status": "unknown", "events": []})
        row["events"].append(event)
        definitive_rejection = (
            event["type"] == "swap-rejected"
            and event.get("reason") == "INSUFFICIENT_ASSET"
            and data.get("operation") in ("propose", "accept")
        )
        if event["type"] in priority and (event["type"] != "swap-rejected" or definitive_rejection):
            previous = row.get("lastEvent", {}).get("type")
            if priority[event["type"]] >= priority.get(previous, 0):
                row["status"] = event["type"].removeprefix("swap-")
                row["lastEvent"] = event
    return list(groups.values())


async def cycle_energy(session, cycle_id, fallback):
    from decimal import Decimal

    from app.models import WalletEvent

    events = (
        (await session.execute(select(WalletEvent).where(WalletEvent.cycle_id == cycle_id)))
        .scalars()
        .all()
    )
    for event in sorted(
        events,
        key=lambda e: (datetime.fromisoformat(e.payload["timestamp"].replace("Z", "+00:00")), e.id),
        reverse=True,
    ):
        if event.event_type == "imbalance-settlement":
            return Decimal(0)
        values = event.payload.get("data", {}).get("balancesAfter", {}).get("KWH")
        if values is not None:
            return Decimal(str(values["available"])) + Decimal(str(values["held"]))
    return fallback


async def report_published_at(session, cycle):
    """La publicación confirmada por connector se distingue del encolado local."""
    if cycle.report_msg_id is None:
        return None
    return (
        await session.execute(
            select(OutboxMessage.sent_at).where(
                OutboxMessage.dedupe_key == f"report:{cycle.cycle_id}:{cycle.report_msg_id}"
            )
        )
    ).scalar_one_or_none()
