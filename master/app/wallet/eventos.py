from datetime import datetime
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.cycle_repository import APPLIED, DUPLICATE, _ensure_cycle
from app.handler_registry import register_handler
from app.models import CycleLedger, WalletBalance, WalletEvent

"""
aqui se procesan los eventos que llegan de la wallet, se registran en la base de datos local y
se actualizan los saldos de las cuentas.

recordar que cuando se realiza un swap, la wallet no confirma inmediatamente que se completó. 
Primero responde HTTP 202, y posteriormente envía un evento a la cola de la ciudad indicando 
el estado del swap.

"""

EVENTOS = {"swap-proposed", "swap-settled", "swap-rejected", "swap-expired",
           "swap-cancelled", "trade-recorded", "imbalance-settlement", "market-closed"}


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

    if cycle_id:
        await _ensure_cycle(session, cycle_id, now)

    # se guarda el evento en la base de datos local.
    # si el evento ya existe (idpk duplicado), no se hace nada y se retorna DUPLICATE.

    inserted = await session.execute(
        pg_insert(WalletEvent).values(
            idpk=message["idpk"], cycle_id=cycle_id,
            swap_id=data.get("swapId"), event_type=message["type"],
            operation_idpk=data.get("idpk"), payload=message,
        ).on_conflict_do_nothing(index_elements=["idpk"]).returning(WalletEvent.id)
    )
    if inserted.scalar_one_or_none() is None:
        return DUPLICATE

    if cycle_id:
        cycle = await session.get(CycleLedger, cycle_id)
        if cycle is not None:
            cycle.last_operation_idpk = message["idpk"]

    # se deben manejar los eventos que llegan desordenados a la cola de la ciudad
    # por lo que los saldos de las cuentas se actualizan solo si el evento que llega es más
    # reciente que el último registrado.

    balances = data.get("balancesAfter")

    if isinstance(balances, dict):
        as_of = parse_timestamp(message["timestamp"])
        for activo, values in balances.items():

            if activo not in ("CR", "KWH") or not isinstance(values, dict):
                continue

            row = await session.get(WalletBalance, activo, with_for_update=True)

            if row is None:
                row = WalletBalance(
                    asset=activo, available=Decimal(0), held=Decimal(0))
                session.add(row)

            if row.as_of is None or as_of > row.as_of:
                row.available = Decimal(str(values["available"]))
                row.held = Decimal(str(values["held"]))
                row.as_of = as_of
                row.source = "event"

    # No registrar movimientos financieros especulativos: trade-recorded es
    # notificación, los asientos oficiales vienen en /statement.
    return APPLIED

for name in EVENTOS:
    register_handler(name, apply_wallet_event)
