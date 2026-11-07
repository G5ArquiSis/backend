from datetime import datetime
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.cycle_repository import APPLIED, DUPLICATE, _ensure_cycle
from app.handler_registry import register_handler
from app.market import seller as market_seller  # registra el modelo de ofertas antes de create_all
from app.models import CycleLedger, WalletEvent, WalletSwapCycle

"""
aqui se procesan los eventos que llegan de la wallet, se registran en la base de datos local y
se actualizan los saldos de las cuentas.

recordar que cuando se realiza un swap, la wallet no confirma inmediatamente que se completó. 
Primero responde HTTP 202, y posteriormente envía un evento a la cola de la ciudad indicando 
el estado del swap.

"""

EVENTOS = {
    "swap-proposed",
    "swap-settled",
    "swap-rejected",
    "swap-expired",
    "swap-cancelled",
    "trade-recorded",
    "imbalance-settlement",
    "market-closed",
}


def parse_timestamp(value):

    if not isinstance(value, str):
        raise ValueError("timestamp requerido")

    result = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if result.tzinfo is None:
        raise ValueError("timestamp sin zona horaria")

    return result


# funcion que recibe el evento de la wallet y lo registra en la base de datos local.
# actualiza los saldos de las cuentas en el ledger local de la ciudad


async def apply_wallet_event(session, message, now, settings):

    data = message.get("data") or {}
    cycle_id = message.get("cycleId")

    swap_id = data.get("swapId")
    if swap_id:
        from app.wallet.correlation import register_swap_cycle

        if cycle_id:
            await register_swap_cycle(session, swap_id, cycle_id)
        else:
            link = await session.get(WalletSwapCycle, swap_id)
            cycle_id = link.cycle_id if link else None

    if cycle_id:
        await _ensure_cycle(session, cycle_id, now)

    # se guarda el evento en la base de datos local.
    # si el evento ya existe (idpk duplicado), no se hace nada y se retorna DUPLICATE.

    inserted = await session.execute(
        pg_insert(WalletEvent)
        .values(
            idpk=message["idpk"],
            cycle_id=cycle_id,
            swap_id=data.get("swapId"),
            event_type=message["type"],
            operation_idpk=data.get("idpk"),
            payload=message,
        )
        .on_conflict_do_nothing(index_elements=["idpk"])
        .returning(WalletEvent.id)
    )
    if inserted.scalar_one_or_none() is None:
        return DUPLICATE

    if message["type"] == "swap-proposed":
        from app.market.swaps import apply_swap_proposed

        await apply_swap_proposed(
            session,
            message,
            now,
            settings,
        )

    if message["type"] in {"swap-settled", "swap-rejected", "swap-expired", "swap-cancelled"}:
        from app.market.swaps import apply_swap_terminal_event

        await apply_swap_terminal_event(session, message, now, settings)

    if message["type"] == "market-closed":
        if not cycle_id:
            raise ValueError("market-closed requiere cycleId")
        await market_seller.close_market(session, cycle_id, now)

    # El handler puede haber descubierto el ciclo al correlacionar el swap
    # con un bid, aunque el evento original no trajera cycleId.
    if not cycle_id and swap_id:
        link = await session.get(
            WalletSwapCycle,
            swap_id,
            populate_existing=True,
        )
        cycle_id = link.cycle_id if link else None

        if cycle_id:
            await _ensure_cycle(session, cycle_id, now)
    if cycle_id:
        cycle = await session.get(CycleLedger, cycle_id)
        if cycle is not None:
            cycle.last_operation_idpk = message["idpk"]

    # se deben manejar los eventos que llegan desordenados a la cola de la ciudad
    # por lo que los saldos de las cuentas se actualizan solo si el evento que llega es más
    # reciente que el último registrado.

    balances = data.get("balancesAfter")

    if message["type"] == "imbalance-settlement":
        from app.models import WalletBalance
        from app.wallet.balances import lock_balances

        await lock_balances(session)
        row = await session.get(WalletBalance, "CR", populate_existing=True)
        balances = {
            "CR": {"available": data["balanceAfter"], "held": row.held if row is not None else 0},
            "KWH": {"available": 0, "held": 0},
        }

    if isinstance(balances, dict):
        from app.wallet.balances import update_balances

        await update_balances(
            session, balances, parse_timestamp(message["timestamp"]), "event", message["idpk"]
        )

    if cycle_id and message["type"] == "imbalance-settlement":
        cycle.energy_balance = Decimal(0)

    # No registrar movimientos financieros especulativos: trade-recorded es
    # notificación, los asientos oficiales vienen en /statement.
    return APPLIED


for name in EVENTOS:
    register_handler(name, apply_wallet_event)
