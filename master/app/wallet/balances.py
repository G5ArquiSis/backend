"""Actualización serializada de snapshots oficiales (incluye primeras inserciones)."""

from decimal import Decimal

from sqlalchemy import text

from app.models import WalletBalance


async def lock_balances(session):
    await session.execute(text("SELECT pg_advisory_xact_lock(21730005)"))


async def update_balances(session, assets, as_of, source, event_key=""):
    if as_of.tzinfo is None:
        raise ValueError("asOf sin zona horaria")
    await lock_balances(session)
    for asset in sorted(assets):
        if asset not in ("CR", "KWH"):
            continue
        values = assets[asset]
        available, held = Decimal(str(values["available"])), Decimal(str(values["held"]))
        if not available.is_finite() or not held.is_finite() or held < 0:
            raise ValueError("Saldo inválido")
        if asset == "KWH" and (available < 0 or available % 1 or held % 1):
            raise ValueError("KWH debe ser entero no negativo")
        row = await session.get(WalletBalance, asset, populate_existing=True)
        if row is None:
            row = WalletBalance(asset=asset, available=0, held=0)
            session.add(row)
        if (
            row.as_of is None
            or as_of > row.as_of
            or (
                as_of == row.as_of
                and (
                    source == "balance"
                    or (row.source != "balance" and event_key > (row.event_key or ""))
                )
            )
        ):
            row.available, row.held = available, held
            row.as_of, row.source = as_of, source
            row.event_key = event_key
            if "creditLine" in values:
                row.credit_line = Decimal(str(values["creditLine"]))
    await session.flush()
