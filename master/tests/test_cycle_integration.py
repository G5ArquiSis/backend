"""Tests del ciclo de negociación contra un Postgres real.

Necesitan la base porque lo que prueban vive en ella: el índice único sobre idpk,
los UPDATE condicionales y los bloqueos que impiden que dos réplicas decidan lo
mismo. El tiempo se pasa como argumento (`now`), así que ningún test espera 30
segundos ni depende del reloj.

Sin base alcanzable la fixture `database` los salta.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.cycle_repository import (
    APPLIED,
    CONFIRMED,
    DUPLICATE,
    EXPIRED,
    IGNORED,
    PAID,
    PENDING,
    REJECTED,
    MalformedMessageError,
    NegotiationNotAllowedError,
    apply_central_message,
    claim_outbox,
    create_negotiation,
    mark_outbox_sent,
    run_due_work,
    total_budget,
)
from app.database import _session_factory
from app.models import CycleLedger, LedgerEvent, Negotiation

pytestmark = pytest.mark.integration

SETTINGS = get_settings()
# La ventana cierra a las 14:20; el periodo de cierre abre a las 14:15.
VALID_UNTIL = datetime(2026, 9, 1, 14, 20, tzinfo=UTC)
WINDOW_START = VALID_UNTIL - timedelta(minutes=20)
CLOSING = VALID_UNTIL - timedelta(minutes=4)
CYCLE = "cycle-9431"


@pytest.fixture
async def db(database: None) -> AsyncIterator[AsyncSession]:
    """Sesión con las tablas del ciclo vacías al empezar."""
    async with _session_factory() as session:
        await session.execute(
            text("TRUNCATE ledger_events, cycle_ledger, negotiations, outbox RESTART IDENTITY")
        )
        await session.commit()
        yield session


def central(message_type: str, idpk: str, data: dict, cycle_id: str | None = CYCLE) -> dict:
    """Mensaje de la central en el envelope v2."""
    message = {
        "idpk": idpk,
        "msgId": f"msg-{idpk}",
        "type": message_type,
        "timestamp": "2026-09-01T14:00:00Z",
        "sender": "central",
        "data": data,
    }
    if cycle_id is not None:
        message["cycleId"] = cycle_id
    return message


def status_statement(
    idpk: str = "status-1",
    cycle_id: str = CYCLE,
    generation: int = 1000,
    consumption: int = 400,
    valid_until: datetime = VALID_UNTIL,
) -> dict:
    return central(
        "status-statement",
        idpk,
        {
            "energy": {
                "generationCapacity": generation,
                "consumption": consumption,
                "generationCost": 210,
            },
            "validUntil": valid_until.isoformat().replace("+00:00", "Z"),
        },
        cycle_id,
    )


async def apply(db: AsyncSession, message: dict, now: datetime = WINDOW_START) -> str:
    return await apply_central_message(db, message, now, SETTINGS)


async def outbox_at(db: AsyncSession, now: datetime) -> list[dict]:
    """Lo que connector recibiría al consultar en ese instante, y confirma que lo publicó."""
    await run_due_work(db, now, SETTINGS)
    claimed = await claim_outbox(db, now, SETTINGS)
    payloads = [message.payload for message in claimed]
    for message_id in [message.id for message in claimed]:
        await mark_outbox_sent(db, message_id, now)
    return payloads


async def cycle_row(db: AsyncSession, cycle_id: str = CYCLE) -> CycleLedger:
    db.expire_all()
    return (
        await db.execute(select(CycleLedger).where(CycleLedger.cycle_id == cycle_id))
    ).scalar_one()


async def negotiation_row(db: AsyncSession, negotiation_id: int) -> Negotiation:
    db.expire_all()
    return (
        await db.execute(select(Negotiation).where(Negotiation.id == negotiation_id))
    ).scalar_one()


# --- Ledger ----------------------------------------------------------------------------


async def test_status_statement_opens_the_cycle_with_its_energy_balance(db: AsyncSession) -> None:
    assert await apply(db, status_statement()) == APPLIED

    cycle = await cycle_row(db)
    assert cycle.opened is True
    assert cycle.energy_balance == Decimal("600")
    assert cycle.generation_cost == Decimal("210")
    assert cycle.last_operation_idpk == "status-1"


async def test_resent_message_is_not_applied_twice(db: AsyncSession) -> None:
    """Anomalía 1 de la demo: mismo idpk, el ledger no cambia dos veces."""
    await apply(db, status_statement())
    transfer = central("transfer", "transfer-1", {"quantity": 508145})

    assert await apply(db, transfer) == APPLIED
    assert await apply(db, transfer) == DUPLICATE

    assert await total_budget(db) == Decimal("508145")
    events = await db.execute(select(func.count(LedgerEvent.id)))
    assert events.scalar_one() == 2


async def test_second_status_statement_for_the_same_cycle_does_not_add_energy_again(
    db: AsyncSession,
) -> None:
    """Aunque traiga otro idpk: el ciclo se abre una sola vez."""
    await apply(db, status_statement("status-1"))

    assert await apply(db, status_statement("status-2")) == DUPLICATE
    assert (await cycle_row(db)).energy_balance == Decimal("600")


async def test_same_message_arriving_at_both_replicas_is_applied_once(db: AsyncSession) -> None:
    """Dos réplicas reciben el mismo idpk a la vez: gana una, la otra ve duplicado."""
    await apply(db, status_statement())
    transfer = central("transfer", "transfer-1", {"quantity": 1000})

    async def from_another_replica() -> str:
        async with _session_factory() as other:
            return await apply_central_message(other, transfer, WINDOW_START, SETTINGS)

    outcomes = await asyncio.gather(*(from_another_replica() for _ in range(5)))

    assert sorted(outcomes) == [APPLIED, DUPLICATE, DUPLICATE, DUPLICATE, DUPLICATE]
    assert await total_budget(db) == Decimal("1000")


async def test_demand_statement_follows_the_sign_convention(db: AsyncSession) -> None:
    await apply(db, status_statement())

    delivered = {"balance": {"quantity": 1500, "valuePerKwh": 215}}
    await apply(db, central("demand-statement", "demand-1", delivered))
    assert (await cycle_row(db)).energy_balance == Decimal("2100")
    assert await total_budget(db) == Decimal("-322500")

    withdrawn = {"balance": {"quantity": -500, "valuePerKwh": 200}}
    await apply(db, central("demand-statement", "demand-2", withdrawn))
    assert (await cycle_row(db)).energy_balance == Decimal("1600")
    assert await total_budget(db) == Decimal("-222500")


async def test_budget_carries_over_between_cycles_and_energy_does_not(db: AsyncSession) -> None:
    await apply(db, status_statement())
    await apply(db, central("transfer", "transfer-1", {"quantity": 1000}))

    next_valid_until = VALID_UNTIL + timedelta(hours=2)
    await apply(
        db,
        status_statement(
            "status-2", "cycle-9432", generation=500, consumption=450, valid_until=next_valid_until
        ),
    )
    await apply(db, central("transfer", "transfer-2", {"quantity": 250}, "cycle-9432"))

    assert await total_budget(db) == Decimal("1250")
    assert (await cycle_row(db, "cycle-9432")).energy_balance == Decimal("50")
    assert (await cycle_row(db, "cycle-9432")).budget_delta == Decimal("250")


async def test_transfer_can_arrive_before_the_status_statement(db: AsyncSession) -> None:
    """El orden de llegada no está garantizado: el primer mensaje crea la fila del ciclo."""
    assert await apply(db, central("transfer", "transfer-1", {"quantity": 1000})) == APPLIED
    assert await apply(db, status_statement()) == APPLIED

    cycle = await cycle_row(db)
    assert cycle.opened is True
    assert cycle.energy_balance == Decimal("600")
    assert await total_budget(db) == Decimal("1000")


async def test_types_that_do_not_touch_the_cycle_are_ignored(db: AsyncSession) -> None:
    assert await apply(db, central("ack", "ack-1", {"target": "x"}, None)) == IGNORED
    assert await apply(db, central("distance-table", "dist-1", {"distances": {}}, None)) == IGNORED


async def test_message_missing_a_required_field_is_rejected_without_side_effects(
    db: AsyncSession,
) -> None:
    with pytest.raises(MalformedMessageError):
        await apply(db, central("transfer", "transfer-1", {"quantity": "mucho"}))
    with pytest.raises(MalformedMessageError):
        await apply(db, central("status-statement", "status-1", {"energy": {}}))

    cycles = await db.execute(select(func.count()).select_from(CycleLedger))
    assert cycles.scalar_one() == 0


# --- negotiation-report ------------------------------------------------------------------


async def test_report_is_sent_once_when_the_closing_period_opens(db: AsyncSession) -> None:
    """G03: sale solo, con los balances del ledger, y una sola vez."""
    await apply(db, status_statement())
    await apply(db, central("transfer", "transfer-1", {"quantity": 508145}))

    assert await outbox_at(db, VALID_UNTIL - timedelta(minutes=6)) == []

    [report] = await outbox_at(db, CLOSING)
    assert report["type"] == "negotiation-report"
    assert report["cycleId"] == CYCLE
    assert report["cityId"] == SETTINGS.city_code
    assert report["data"] == {"budgetBalance": 508145, "energyBalance": 600}
    assert report["idpk"] != report["msgId"]

    assert await outbox_at(db, CLOSING + timedelta(seconds=30)) == []


async def test_report_is_not_owed_for_a_cycle_the_central_did_not_open(db: AsyncSession) -> None:
    """Solo se debe reporte si llegó el status-statement de ese ciclo."""
    await apply(db, central("transfer", "transfer-1", {"quantity": 1000}))

    assert await outbox_at(db, CLOSING) == []


async def test_report_is_not_sent_after_the_window_closed(db: AsyncSession) -> None:
    """Llegar tarde no sirve: la central respondería CYCLE_EXPIRED."""
    await apply(db, status_statement())

    assert await outbox_at(db, VALID_UNTIL + timedelta(seconds=1)) == []


async def test_report_survives_a_restart_because_the_schedule_is_persisted(
    db: AsyncSession,
) -> None:
    """Anomalía 4: nada en memoria recuerda el reporte; una sesión nueva igual lo envía."""
    await apply(db, status_statement())

    async with _session_factory() as after_restart:
        [report] = await outbox_at(after_restart, CLOSING)

    assert report["type"] == "negotiation-report"


async def test_both_replicas_checking_at_once_send_a_single_report(db: AsyncSession) -> None:
    await apply(db, status_statement())

    async def from_another_replica() -> list[dict]:
        async with _session_factory() as other:
            return await outbox_at(other, CLOSING)

    batches = await asyncio.gather(*(from_another_replica() for _ in range(4)))

    assert sum(len(batch) for batch in batches) == 1


async def test_report_too_early_is_retried_at_opens_at_with_the_same_idpk(
    db: AsyncSession,
) -> None:
    await apply(db, status_statement())
    [first] = await outbox_at(db, CLOSING)

    opens_at = CLOSING + timedelta(minutes=1)
    too_early = central(
        "error",
        "error-1",
        {"target": first["msgId"], "opensAt": opens_at.isoformat().replace("+00:00", "Z")},
    )
    too_early["reason"] = "REPORT_TOO_EARLY"
    assert await apply(db, too_early, CLOSING) == APPLIED

    assert await outbox_at(db, opens_at - timedelta(seconds=5)) == []

    [retry] = await outbox_at(db, opens_at + timedelta(seconds=30))
    assert retry["idpk"] == first["idpk"]
    assert retry["msgId"] != first["msgId"]


async def test_ledger_change_after_reporting_sends_a_correction_with_a_new_idpk(
    db: AsyncSession,
) -> None:
    await apply(db, status_statement())
    [first] = await outbox_at(db, CLOSING)

    late = {"balance": {"quantity": 100, "valuePerKwh": 200}}
    await apply(db, central("demand-statement", "demand-1", late), CLOSING)

    [correction] = await outbox_at(db, CLOSING + timedelta(seconds=10))
    assert correction["data"] == {"budgetBalance": -20000, "energyBalance": 700}
    assert correction["idpk"] != first["idpk"]


# --- Negociación voluntaria ------------------------------------------------------------


async def open_cycle(db: AsyncSession) -> None:
    await apply(db, status_statement())
    await apply(db, central("transfer", "transfer-1", {"quantity": 1_000_000}))


async def propose(db: AsyncSession, direction: str, quantity: str, now: datetime) -> dict:
    """Crea la propuesta y devuelve el mensaje que connector publicaría."""
    await create_negotiation(db, direction, Decimal(quantity), None, now, SETTINGS)
    [proposal] = await outbox_at(db, now)
    return proposal


async def test_proposal_is_published_at_the_price_cap_by_default(db: AsyncSession) -> None:
    await open_cycle(db)

    proposal = await propose(db, "take", "2024", WINDOW_START)

    assert proposal["type"] == "negotiation-proposal"
    assert proposal["data"] == {"direction": "take", "quantity": 2024, "pricePerEnergy": 220.5}


async def test_take_is_applied_on_confirmation_and_we_pay_the_central(db: AsyncSession) -> None:
    await open_cycle(db)
    proposal = await propose(db, "take", "2024", WINDOW_START)
    confirmed_at = WINDOW_START + timedelta(seconds=5)

    take = central(
        "take", "take-1", {"target": proposal["msgId"], "energy": 2024, "pricePerEnergy": 210}
    )
    assert await apply(db, take, confirmed_at) == APPLIED

    assert (await negotiation_row(db, 1)).state == PAID
    assert (await cycle_row(db)).energy_balance == Decimal("2624")
    assert await total_budget(db) == Decimal("1000000") - Decimal("425040")

    [payment] = await outbox_at(db, confirmed_at)
    assert payment["type"] == "transfer"
    assert payment["data"] == {"becauseOf": take["msgId"], "quantity": 425040}


async def test_repeated_confirmation_does_not_apply_or_pay_twice(db: AsyncSession) -> None:
    """La propiedad de AD3, ante una entrega repetida del broker."""
    await open_cycle(db)
    proposal = await propose(db, "take", "2024", WINDOW_START)
    take = central(
        "take", "take-1", {"target": proposal["msgId"], "energy": 2024, "pricePerEnergy": 210}
    )

    assert await apply(db, take) == APPLIED
    assert await apply(db, take) == DUPLICATE

    assert (await cycle_row(db)).energy_balance == Decimal("2624")
    payments = [m for m in await outbox_at(db, WINDOW_START) if m["type"] == "transfer"]
    assert len(payments) == 1


async def test_give_touches_the_ledger_only_when_the_payment_arrives(db: AsyncSession) -> None:
    await open_cycle(db)
    proposal = await propose(db, "give", "300", WINDOW_START)

    give = central(
        "give", "give-1", {"target": proposal["msgId"], "energy": 300, "pricePerEnergy": 220.5}
    )
    assert await apply(db, give) == APPLIED
    assert (await negotiation_row(db, 1)).state == CONFIRMED
    assert (await cycle_row(db)).energy_balance == Decimal("600")

    payment = central("transfer", "pay-1", {"becauseOf": give["msgId"], "quantity": 66150})
    assert await apply(db, payment) == APPLIED
    assert await apply(db, payment) == DUPLICATE

    assert (await negotiation_row(db, 1)).state == PAID
    assert (await cycle_row(db)).energy_balance == Decimal("300")
    assert await total_budget(db) == Decimal("1066150")


async def test_unanswered_proposal_is_retried_with_the_same_idpk(db: AsyncSession) -> None:
    await open_cycle(db)
    first = await propose(db, "take", "100", WINDOW_START)

    assert await outbox_at(db, WINDOW_START + timedelta(seconds=29)) == []

    [retry] = await outbox_at(db, WINDOW_START + timedelta(seconds=31))
    assert retry["idpk"] == first["idpk"]
    assert retry["msgId"] != first["msgId"]
    assert retry["data"] == first["data"]
    assert (await negotiation_row(db, 1)).attempts == 2


async def test_proposal_expires_after_the_last_attempt(db: AsyncSession) -> None:
    await open_cycle(db)
    await propose(db, "take", "100", WINDOW_START)

    sent = 1
    now = WINDOW_START
    for _ in range(SETTINGS.negotiation_max_attempts + 1):
        now += timedelta(seconds=31)
        sent += len(await outbox_at(db, now))

    assert sent == SETTINGS.negotiation_max_attempts
    negotiation = await negotiation_row(db, 1)
    assert negotiation.state == EXPIRED
    assert negotiation.reason == "TIMEOUT"


async def test_late_confirmation_of_the_first_attempt_is_applied_once(db: AsyncSession) -> None:
    """La central responde al primer envío cuando ya reintentamos: una sola aplicación."""
    await open_cycle(db)
    first = await propose(db, "take", "100", WINDOW_START)
    [retry] = await outbox_at(db, WINDOW_START + timedelta(seconds=31))

    to_first = central(
        "take", "take-1", {"target": first["msgId"], "energy": 100, "pricePerEnergy": 210}
    )
    to_retry = central(
        "take", "take-2", {"target": retry["msgId"], "energy": 100, "pricePerEnergy": 210}
    )
    assert await apply(db, to_first) == APPLIED
    assert await apply(db, to_retry) == DUPLICATE

    assert (await cycle_row(db)).energy_balance == Decimal("700")
    assert await total_budget(db) == Decimal("979000")


async def test_missing_payment_for_a_give_retries_the_operation(db: AsyncSession) -> None:
    """Sin transfer en 30 segundos no hubo operación real: se reintenta, sin tocar el ledger."""
    await open_cycle(db)
    proposal = await propose(db, "give", "300", WINDOW_START)
    give = central(
        "give", "give-1", {"target": proposal["msgId"], "energy": 300, "pricePerEnergy": 220.5}
    )
    await apply(db, give, WINDOW_START + timedelta(seconds=5))

    [retry] = await outbox_at(db, WINDOW_START + timedelta(seconds=36))

    assert retry["idpk"] == proposal["idpk"]
    assert (await negotiation_row(db, 1)).state == PENDING
    assert (await cycle_row(db)).energy_balance == Decimal("600")


async def test_error_from_the_central_rejects_the_proposal_without_retrying(
    db: AsyncSession,
) -> None:
    await open_cycle(db)
    proposal = await propose(db, "give", "300", WINDOW_START)

    error = central("error", "error-1", {"target": proposal["msgId"], "spare": 120})
    error["reason"] = "OVER_CAPACITY"
    assert await apply(db, error) == APPLIED

    negotiation = await negotiation_row(db, 1)
    assert negotiation.state == REJECTED
    assert negotiation.reason == "OVER_CAPACITY"
    assert await outbox_at(db, WINDOW_START + timedelta(seconds=31)) == []


async def test_proposal_is_refused_locally_when_the_central_would_reject_it(
    db: AsyncSession,
) -> None:
    await open_cycle(db)

    with pytest.raises(NegotiationNotAllowedError) as above_cap:
        await create_negotiation(
            db, "take", Decimal("10"), Decimal("220.51"), WINDOW_START, SETTINGS
        )
    assert above_cap.value.reason == "PRICE_ABOVE_CAP"

    await create_negotiation(db, "give", Decimal("500"), None, WINDOW_START, SETTINGS)
    with pytest.raises(NegotiationNotAllowedError) as over_capacity:
        await create_negotiation(db, "give", Decimal("101"), None, WINDOW_START, SETTINGS)
    assert over_capacity.value.reason == "OVER_CAPACITY"

    with pytest.raises(NegotiationNotAllowedError) as closed:
        await create_negotiation(
            db, "take", Decimal("10"), None, VALID_UNTIL + timedelta(seconds=1), SETTINGS
        )
    assert closed.value.reason == "CYCLE_EXPIRED"


# --- Outbox ----------------------------------------------------------------------------


async def test_unconfirmed_message_is_offered_again_and_a_confirmed_one_is_not(
    db: AsyncSession,
) -> None:
    """Si connector muere entre retirar y publicar, el mensaje no se pierde."""
    await open_cycle(db)
    await create_negotiation(db, "take", Decimal("100"), None, WINDOW_START, SETTINGS)

    [claimed] = await claim_outbox(db, WINDOW_START, SETTINGS)
    assert await claim_outbox(db, WINDOW_START + timedelta(seconds=10), SETTINGS) == []

    redelivery = WINDOW_START + timedelta(seconds=SETTINGS.outbox_redelivery_seconds + 1)
    [again] = await claim_outbox(db, redelivery, SETTINGS)
    assert again.id == claimed.id

    assert await mark_outbox_sent(db, claimed.id, redelivery) is True
    later = redelivery + timedelta(seconds=SETTINGS.outbox_redelivery_seconds + 1)
    assert await claim_outbox(db, later, SETTINGS) == []


# --- HTTP ------------------------------------------------------------------------------


async def test_internal_endpoints_apply_messages_and_serve_the_outbox(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    """El contrato que usa connector: reenviar un mensaje y retirar lo pendiente."""
    valid_until = datetime.now(UTC) + timedelta(minutes=2)
    message = status_statement(valid_until=valid_until)

    first = await client.post("/internal/messages", json=message)
    second = await client.post("/internal/messages", json=message)
    assert first.json() == {"outcome": "applied"}
    assert second.json() == {"outcome": "duplicate"}

    # Faltan menos de 5 minutos para el cierre: el reporte ya está vencido.
    claimed = (await client.post("/internal/outbox/claim")).json()
    assert [item["payload"]["type"] for item in claimed] == ["negotiation-report"]

    sent = await client.post(f"/internal/outbox/{claimed[0]['id']}/sent")
    assert sent.status_code == 204
    assert (await client.post("/internal/outbox/claim")).json() == []

    malformed = await client.post("/internal/messages", json=central("transfer", "t-1", {}))
    assert malformed.status_code == 422


async def test_negotiation_endpoints_create_and_list_proposals(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    """RF04: crear una propuesta, seguirla y ver el historial con su estado."""
    valid_until = datetime.now(UTC) + timedelta(minutes=15)
    await client.post("/internal/messages", json=status_statement(valid_until=valid_until))

    created = await client.post("/negotiations", json={"direction": "give", "quantity": 300})
    assert created.status_code == 201
    body = created.json()
    assert body["state"] == "pending"
    assert body["cycleId"] == CYCLE
    assert Decimal(body["pricePerEnergy"]) == Decimal("220.5")

    detail = await client.get(f"/negotiations/{body['id']}")
    assert detail.json()["idpk"] == body["idpk"]
    assert [item["id"] for item in (await client.get("/negotiations")).json()] == [body["id"]]
    assert (await client.get("/negotiations/999")).status_code == 404

    refused = await client.post("/negotiations", json={"direction": "give", "quantity": 301})
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "OVER_CAPACITY"

    cycles = (await client.get("/cycles")).json()
    assert Decimal(cycles["budgetBalance"]) == Decimal("0")
    assert cycles["items"][0]["cycleId"] == CYCLE
    assert Decimal(cycles["items"][0]["energyBalance"]) == Decimal("600")
