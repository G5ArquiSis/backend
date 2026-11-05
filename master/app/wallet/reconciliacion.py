"""
aquí se hace la reconciliación entre el ledger local y la wallet.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import WalletBalance, WalletReconciliacion, WalletStatements


def valor_decimal(value):
    return Decimal(str(value))


def copia_saldos_local(diccionario_saldos):
    return {asset: {"available": str(row.available), "held": str(row.held)}
            for asset, row in diccionario_saldos.items()}


def calcular_delta(saldos_wallet, saldos_local):

    # calculamos el delta como (valor en la wallet) - (valor en la base de datos local)

    diccionario_deltas = {}

    for activo, values in saldos_wallet.items():
        diccionario_deltas[activo] = {}

        for key in ("available", "held"):
            saldo_local = saldos_local.get(activo)

            saldo = getattr(
                saldo_local, key) if saldo_local is not None else Decimal(0)

            diferencia = valor_decimal(values[key]) - saldo
            diccionario_deltas[activo][key] = str(diferencia)

    return diccionario_deltas


async def reconcile(session, client, *, cycle_id=None, max_pages=30):
    """Consulta saldos y asientos; falla sin publicar snapshots parciales.

    Si statement tiene más de max_pages, no corrige hasta leerlo completo.
    El consumidor debe reintentar en otra ejecución con la misma información.
    """
    # primero se obtienen los saldos oficiales que aparecen en la wallet

    balance = await client.get_balance()
    dict_assets = balance["assets"]

    saldos_wallet = {activo: {"available": str(valor_decimal(valores["available"])),
                              "held": str(valor_decimal(valores["held"]))}
                     for activo, valores in dict_assets.items() if activo in ("CR", "KWH")}

    saldos_local = {activo: await session.get(WalletBalance, activo) for activo in saldos_wallet}

    local_before = copia_saldos_local(saldos_local)

    delta = calcular_delta(saldos_wallet, saldos_local)

    cursor = None
    seen_cursors = set()
    entries = []

    for _ in range(max_pages):
        page = await client.get_statement(cursor=cursor, limit=100)
        entries.extend(page.get("entries", []))
        cursor = page.get("nextCursor")
        if cursor is None:
            break
        if cursor in seen_cursors:
            raise ValueError("Wallet devolvió cursor repetido")
        seen_cursors.add(cursor)
    else:
        raise ValueError(
            "Statement supera max_pages; no se modificaron los saldos")

    count = 0
    for entry in entries:
        inserted = await session.execute(
            pg_insert(WalletStatements).values(
                entry_id=entry["entryId"], cycle_id=entry.get("cycleId"),
                asset=entry["asset"], kind=entry["kind"],
                amount=valor_decimal(entry["amount"]), payload=entry,
            ).on_conflict_do_nothing(index_elements=["entry_id"]).returning(WalletStatements.entry_id)
        )
        count += int(inserted.scalar_one_or_none() is not None)
    as_of = datetime.fromisoformat(balance["asOf"].replace("Z", "+00:00"))

    for asset, values in saldos_wallet.items():
        row = saldos_local[asset]
        if row is None:
            row = WalletBalance(
                asset=asset, available=Decimal(0), held=Decimal(0))
            session.add(row)
        if row.as_of is None or as_of >= row.as_of:
            row.available = valor_decimal(values["available"])
            row.held = valor_decimal(values["held"])
            row.as_of = as_of
            row.source = "balance"
            row.credit_line = (valor_decimal(dict_assets[asset]["creditLine"])
                               if "creditLine" in dict_assets[asset] else None)

    session.add(WalletReconciliacion(cycle_id=cycle_id, local_before=local_before,
                                     remote=saldos_wallet, delta=delta, entries_imported=count))

    await session.commit()
    return {"localBefore": local_before, "wallet": saldos_wallet,
            "delta": delta, "entriesImported": count,
            "asOf": balance["asOf"]}


# función para consultar la reconciliación más reciente, para mostrarla en el dashboard de
# la ciudad.
# se incluye cuándo se ejecutó, qué diferencias se encontraron y cuántos asientos nuevos
# se importaron.

async def latest_reconciliation(session):

    ultima_rec = await session.execute(select(WalletReconciliacion)
                                       .order_by(WalletReconciliacion.id.desc()).limit(1))

    row = ultima_rec.scalar_one_or_none()
    if row is None:
        return None

    return {"checkedAt": row.checked_at, "cycleId": row.cycle_id,
            "localBefore": row.local_before, "wallet": row.remote,
            "delta": row.delta, "entriesImported": row.entries_imported}
