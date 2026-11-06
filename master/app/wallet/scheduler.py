"""Trabajo activado por el heartbeat/outbox existente, sin reloj nuevo en master."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.database import _session_factory
from app.models import CycleLedger, LedgerEvent, WalletSyncState
from app.wallet.client import WalletClient
from app.wallet.reconciliacion import reconcile

logger = logging.getLogger(__name__)


async def reconcile_due(settings):
    if not settings.wallet_enabled or not settings.wallet_base_url or not settings.wallet_api_key:
        return
    async with _session_factory() as session:
        # No espera a otra réplica ni multiplica llamadas a la wallet.
        if not (await session.execute(text("SELECT pg_try_advisory_xact_lock(21730006)"))).scalar():
            return
        await session.execute(
            insert(WalletSyncState).values(name="wallet").on_conflict_do_nothing()
        )
        state = await session.get(WalletSyncState, "wallet")
        now = datetime.now(UTC)
        cycles = (
            (
                await session.execute(
                    select(CycleLedger)
                    .where(CycleLedger.opened.is_(True))
                    .order_by(CycleLedger.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        closed = next((c for c in cycles if c.report_due_at and c.report_due_at <= now), None)
        cycle_id = closed.cycle_id if closed else None
        retry_due = state.attempted_at is None or now - state.attempted_at >= timedelta(
            seconds=settings.wallet_reconcile_interval_seconds
        )
        new_close = cycle_id is not None and state.completed_cycle != cycle_id
        # Un error respeta el intervalo incluso al cierre.
        if not retry_due and not (new_close and state.error is None):
            return
        state.attempted_at = now
        await session.flush()
        try:
            async with WalletClient(settings.wallet_base_url, settings.wallet_api_key) as client:
                status = (
                    await session.execute(
                        select(LedgerEvent)
                        .where(LedgerEvent.kind == "status-statement")
                        .order_by(LedgerEvent.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if status is not None:
                    client.configure_limits(
                        **status.payload.get("data", {}).get("market", {}).get("limits", {})
                    )
                # reconcile confirma la transacción: actualizar estado antes del commit.
                state.completed_cycle = cycle_id
                state.error = None
                await reconcile(session, client, cycle_id=cycle_id)
                from app.cycle_repository import run_due_work

                await run_due_work(session, now, settings)
        except Exception as error:
            await session.rollback()
            logger.warning("Reconciliación wallet falló: %s", type(error).__name__)
            await session.execute(
                insert(WalletSyncState).values(name="wallet").on_conflict_do_nothing()
            )
            state = await session.get(WalletSyncState, "wallet")
            state.attempted_at = now
            state.error = type(error).__name__
            await session.commit()
