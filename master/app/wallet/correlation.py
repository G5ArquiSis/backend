"""Integración C/D: registrar el ciclo al conocer swapId; trade-recorded también lo fija."""

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from app.models import WalletEvent, WalletSwapCycle


async def register_swap_cycle(session, swap_id, cycle_id):
    await session.execute(
        insert(WalletSwapCycle)
        .values(swap_id=swap_id, cycle_id=cycle_id)
        .on_conflict_do_nothing(index_elements=["swap_id"])
    )
    link = await session.get(WalletSwapCycle, swap_id, populate_existing=True)
    if link.cycle_id != cycle_id:
        raise ValueError("swapId asociado a otro ciclo")
    await session.execute(
        update(WalletEvent)
        .where(WalletEvent.swap_id == swap_id, WalletEvent.cycle_id.is_(None))
        .values(cycle_id=cycle_id)
    )
