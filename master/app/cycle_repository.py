"""Capa de datos del ciclo de negociación: ledger, negociaciones, reporte y outbox.

Es la misma capa de datos que `repository.py`, separada por tema: allá el
historial de la E0, acá el ciclo de la E1. Todo el uso de SQLAlchemy del ciclo
vive en este archivo; las fórmulas, en `ledger.py`.

Tres ideas sostienen el módulo (ADRs de AD2 y AD3):

- El ledger es un log de eventos con `idpk` único más una proyección por ciclo,
  actualizadas en la misma transacción.
- Nada depende de un temporizador en memoria. Qué toca enviar y cuándo se decide
  en `run_due_work`, leyendo plazos persistidos, cada vez que connector retira la
  outbox. Un reinicio no pierde nada.
- Las dos réplicas de master comparten la base: cada decisión se toma sobre filas
  bloqueadas o con un UPDATE condicional, de modo que solo una réplica la ejecuta.
"""

import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.ledger import (
    GIVE,
    TAKE,
    demand_statement_deltas,
    negotiation_deltas,
    price_cap,
    sellable_energy,
    status_statement_energy,
    total_amount,
)
from app.models import (
    CycleLedger,
    DistanceTable,
    LedgerEvent,
    MessageLog,
    Negotiation,
    OutboxMessage,
)

PENDING = "pending"
CONFIRMED = "confirmed"
PAID = "paid"
EXPIRED = "expired"
REJECTED = "rejected"

APPLIED = "applied"
DUPLICATE = "duplicate"
IGNORED = "ignored"

# Categorías del registro de mensajes no aplicados (RF05).
LOG_DUPLICATE = "duplicate"
LOG_DISCARDED = "discarded"
LOG_NACK = "nack"

# Estados desde los que todavía se acepta una confirmación. Incluye EXPIRED: si la
# central confirma tarde, ya ejecutó la operación y el ledger debe reflejarla.
_AWAITING_CONFIRMATION = (PENDING, EXPIRED)
_AWAITING_PAYMENT = (CONFIRMED, PENDING, EXPIRED)
# Negociaciones que ocupan energía vendible del ciclo.
_LIVE = (PENDING, CONFIRMED, PAID)

_OUTBOX_BATCH = 20


class MalformedMessageError(Exception):
    """Al mensaje le falta un campo que su tipo exige, o trae un valor no numérico."""


class NegotiationNotAllowedError(Exception):
    """La propuesta no se puede crear; `reason` usa los nombres de error de la central."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# --- Mensajes que llegan de la central -------------------------------------------------


async def apply_central_message(
    session: AsyncSession, message: dict, now: datetime, settings: Settings
) -> str:
    """Aplica un mensaje de la central y devuelve APPLIED, DUPLICATE o IGNORED.

    Es idempotente: reenviar el mismo mensaje devuelve DUPLICATE sin tocar el
    ledger, y el duplicado queda en el registro consultable (RF05). Todo ocurre en
    una transacción, así que un fallo a mitad de camino no deja el evento sin su
    efecto ni el efecto sin su evento.
    """
    handler = _HANDLERS.get(message.get("type"))
    if handler is None:
        return IGNORED

    try:
        outcome = await handler(session, message, now, settings)
    except (KeyError, TypeError, InvalidOperation, ValueError) as error:
        await session.rollback()
        raise MalformedMessageError(f"{message.get('type')}: {error!r}") from error

    if outcome == APPLIED:
        await session.commit()
        return outcome

    await session.rollback()
    if outcome == DUPLICATE:
        await record_unapplied_message(
            session,
            LOG_DUPLICATE,
            json.dumps(message, ensure_ascii=False),
            "La operación ya estaba aplicada; no se volvió a aplicar",
        )
    return outcome


async def _apply_status_statement(
    session: AsyncSession, message: dict, now: datetime, settings: Settings
) -> str:
    cycle_id = message["cycleId"]
    energy = message["data"]["energy"]
    generation = _decimal(energy["generationCapacity"])
    consumption = _decimal(energy["consumption"])
    valid_until = _datetime(message["data"]["validUntil"])
    report_due_at = valid_until - timedelta(
        seconds=settings.report_closing_seconds - settings.report_margin_seconds
    )

    await _ensure_cycle(session, cycle_id)
    # Abrir el ciclo es condicional: un segundo status-statement del mismo ciclo,
    # aunque traiga otro idpk, no vuelve a sumar la energía inicial.
    opened = await session.execute(
        update(CycleLedger)
        .where(CycleLedger.cycle_id == cycle_id, CycleLedger.opened.is_(False))
        .values(
            opened=True,
            generation_capacity=generation,
            consumption=consumption,
            generation_cost=_decimal(energy["generationCost"]),
            valid_until=valid_until,
            report_due_at=report_due_at,
        )
    )
    if opened.rowcount != 1:
        return DUPLICATE

    recorded = await _record_event(
        session,
        message,
        cycle_id,
        energy_delta=status_statement_energy(generation, consumption),
        budget_delta=Decimal(0),
    )
    return APPLIED if recorded else DUPLICATE


async def _apply_transfer(
    session: AsyncSession, message: dict, now: datetime, settings: Settings
) -> str:
    because_of = message["data"].get("becauseOf")
    if because_of is not None:
        return await _apply_give_payment(session, message, because_of)

    cycle_id = message["cycleId"]
    await _ensure_cycle(session, cycle_id)
    recorded = await _record_event(
        session,
        message,
        cycle_id,
        energy_delta=Decimal(0),
        budget_delta=_decimal(message["data"]["quantity"]),
    )
    return APPLIED if recorded else DUPLICATE


async def _apply_demand_statement(
    session: AsyncSession, message: dict, now: datetime, settings: Settings
) -> str:
    cycle_id = message["cycleId"]
    balance = message["data"]["balance"]
    energy_delta, budget_delta = demand_statement_deltas(
        _decimal(balance["quantity"]), _decimal(balance["valuePerKwh"])
    )

    await _ensure_cycle(session, cycle_id)
    recorded = await _record_event(session, message, cycle_id, energy_delta, budget_delta)
    return APPLIED if recorded else DUPLICATE


async def _apply_confirmation(
    session: AsyncSession, message: dict, now: datetime, settings: Settings
) -> str:
    """Confirmación `give` o `take` de una propuesta nuestra."""
    negotiation = await _negotiation_by_attempt(session, message["data"]["target"])
    if negotiation is None or negotiation.direction != message["type"]:
        return IGNORED

    energy = _decimal(message["data"]["energy"])
    price = _decimal(message["data"]["pricePerEnergy"])
    amount = total_amount(energy, price)
    confirmation = {
        "confirmation_msg_id": message["msgId"],
        "confirmed_energy": energy,
        "confirmed_price": price,
        "amount": amount,
    }

    if negotiation.direction == GIVE:
        # Vendemos: sin el pago de la central no hubo operación real, así que el
        # ledger espera al transfer. Se abre el plazo de 30 segundos para recibirlo.
        confirmed = await session.execute(
            update(Negotiation)
            .where(Negotiation.id == negotiation.id, Negotiation.state.in_(_AWAITING_CONFIRMATION))
            .values(
                state=CONFIRMED,
                deadline_at=now + timedelta(seconds=settings.negotiation_timeout_seconds),
                **confirmation,
            )
        )
        return APPLIED if confirmed.rowcount == 1 else DUPLICATE

    # Compramos: la operación queda hecha al confirmarse y nos toca pagar.
    paid = await session.execute(
        update(Negotiation)
        .where(Negotiation.id == negotiation.id, Negotiation.state.in_(_AWAITING_CONFIRMATION))
        .values(state=PAID, deadline_at=None, **confirmation)
    )
    if paid.rowcount != 1:
        return DUPLICATE

    energy_delta, budget_delta = negotiation_deltas(TAKE, energy, amount)
    recorded = await _record_event(
        session,
        message,
        negotiation.cycle_id,
        energy_delta,
        budget_delta,
        idpk=negotiation.idpk,
    )
    if not recorded:
        return DUPLICATE

    await _enqueue(
        session,
        f"transfer:{negotiation.id}",
        _envelope(
            "transfer",
            str(uuid.uuid4()),
            negotiation.cycle_id,
            {"becauseOf": message["msgId"], "quantity": _number(amount)},
            now,
            settings,
        ),
    )
    return APPLIED


async def _apply_give_payment(session: AsyncSession, message: dict, because_of: str) -> str:
    """Pago de la central por una venta nuestra: recién acá se aplica al ledger."""
    found = await session.execute(
        select(Negotiation).where(
            Negotiation.confirmation_msg_id == because_of, Negotiation.direction == GIVE
        )
    )
    negotiation = found.scalar_one_or_none()
    if negotiation is None:
        return IGNORED

    amount = _decimal(message["data"]["quantity"])
    paid = await session.execute(
        update(Negotiation)
        .where(Negotiation.id == negotiation.id, Negotiation.state.in_(_AWAITING_PAYMENT))
        .values(state=PAID, deadline_at=None, amount=amount)
    )
    if paid.rowcount != 1:
        return DUPLICATE

    energy_delta, budget_delta = negotiation_deltas(GIVE, negotiation.confirmed_energy, amount)
    recorded = await _record_event(
        session,
        message,
        negotiation.cycle_id,
        energy_delta,
        budget_delta,
        idpk=negotiation.idpk,
        kind=GIVE,
    )
    return APPLIED if recorded else DUPLICATE


async def _apply_distance_table(
    session: AsyncSession, message: dict, now: datetime, settings: Settings
) -> str:
    """Guarda la tabla de distancias; la vigente es la última recibida (RF02)."""
    distances = message["data"]["distances"]
    if not isinstance(distances, dict):
        raise TypeError("data.distances debe ser un objeto")

    inserted = await session.execute(
        postgres_insert(DistanceTable)
        .values(idpk=message["idpk"], distances=distances)
        .on_conflict_do_nothing(index_elements=["idpk"])
        .returning(DistanceTable.id)
    )
    return APPLIED if inserted.scalar_one_or_none() is not None else DUPLICATE


async def _apply_rejection(
    session: AsyncSession, message: dict, now: datetime, settings: Settings
) -> str:
    """`error` o `nack` de la central sobre una propuesta o un reporte nuestros."""
    target = message["data"]["target"]
    reason = message.get("reason") or message["type"].upper()

    negotiation = await _negotiation_by_attempt(session, target)
    if negotiation is not None:
        # No se reintenta sola: repetir el mismo mensaje daría el mismo error.
        rejected = await session.execute(
            update(Negotiation)
            .where(Negotiation.id == negotiation.id, Negotiation.state == PENDING)
            .values(state=REJECTED, reason=reason, deadline_at=None)
        )
        return APPLIED if rejected.rowcount == 1 else DUPLICATE

    opens_at = message["data"].get("opensAt")
    if reason == "REPORT_TOO_EARLY" and opens_at is not None:
        # Mismo idpk: es la misma operación, reprogramada para cuando abra el periodo.
        values = {
            "report_sent_at": None,
            "report_due_at": _datetime(opens_at)
            + timedelta(seconds=settings.report_margin_seconds),
        }
    else:
        values = {"report_error": reason}

    reported = await session.execute(
        update(CycleLedger).where(CycleLedger.report_msg_id == target).values(**values)
    )
    return APPLIED if reported.rowcount == 1 else IGNORED


_HANDLERS = {
    "status-statement": _apply_status_statement,
    "transfer": _apply_transfer,
    "demand-statement": _apply_demand_statement,
    GIVE: _apply_confirmation,
    TAKE: _apply_confirmation,
    "error": _apply_rejection,
    "nack": _apply_rejection,
    "distance-table": _apply_distance_table,
}


# --- Trabajo con plazo: reporte, correcciones y timeouts ---------------------------------


async def run_due_work(session: AsyncSession, now: datetime, settings: Settings) -> None:
    """Encola lo que ya venció: reportes, correcciones y reintentos de propuestas.

    Se llama en cada retiro de la outbox. Las filas se toman con FOR UPDATE SKIP
    LOCKED: si las dos réplicas corren esto a la vez, cada fila la procesa una sola.
    """
    await _send_due_reports(session, now, settings)
    await _correct_stale_reports(session, now, settings)
    await _retry_or_expire_negotiations(session, now, settings)
    await session.commit()


async def _send_due_reports(session: AsyncSession, now: datetime, settings: Settings) -> None:
    due = await session.execute(
        select(CycleLedger)
        .where(
            CycleLedger.opened.is_(True),
            CycleLedger.report_sent_at.is_(None),
            CycleLedger.report_error.is_(None),
            CycleLedger.report_due_at <= now,
            # Después del cierre de la ventana el reporte ya no sirve.
            CycleLedger.valid_until > now,
        )
        .with_for_update(skip_locked=True)
    )
    for cycle in due.scalars().all():
        # Conserva el idpk si es un reenvío tras REPORT_TOO_EARLY.
        await _enqueue_report(session, cycle, cycle.report_idpk or str(uuid.uuid4()), now, settings)


async def _correct_stale_reports(session: AsyncSession, now: datetime, settings: Settings) -> None:
    """Si el ledger cambió después de reportar, envía una corrección con idpk nuevo."""
    budget = await total_budget(session)
    stale = await session.execute(
        select(CycleLedger)
        .where(
            CycleLedger.report_sent_at.is_not(None),
            CycleLedger.report_error.is_(None),
            CycleLedger.valid_until > now,
            (CycleLedger.reported_energy != CycleLedger.energy_balance)
            | (CycleLedger.reported_budget != budget),
        )
        .with_for_update(skip_locked=True)
    )
    for cycle in stale.scalars().all():
        await _enqueue_report(session, cycle, str(uuid.uuid4()), now, settings)


async def _enqueue_report(
    session: AsyncSession, cycle: CycleLedger, idpk: str, now: datetime, settings: Settings
) -> None:
    budget = await total_budget(session)
    envelope = _envelope(
        "negotiation-report",
        idpk,
        cycle.cycle_id,
        {"budgetBalance": _number(budget), "energyBalance": _number(cycle.energy_balance)},
        now,
        settings,
    )
    cycle.report_idpk = idpk
    cycle.report_msg_id = envelope["msgId"]
    cycle.report_sent_at = now
    cycle.reported_budget = budget
    cycle.reported_energy = cycle.energy_balance
    await _enqueue(session, f"report:{cycle.cycle_id}:{envelope['msgId']}", envelope)


async def _retry_or_expire_negotiations(
    session: AsyncSession, now: datetime, settings: Settings
) -> None:
    overdue = await session.execute(
        select(Negotiation)
        .where(Negotiation.state.in_((PENDING, CONFIRMED)), Negotiation.deadline_at <= now)
        .with_for_update(skip_locked=True)
    )
    for negotiation in overdue.scalars().all():
        cycle = await session.get(CycleLedger, negotiation.cycle_id)
        window_open = (
            cycle is not None and cycle.valid_until is not None and cycle.valid_until > now
        )
        if negotiation.attempts >= settings.negotiation_max_attempts or not window_open:
            negotiation.state = EXPIRED
            negotiation.reason = "TIMEOUT"
            negotiation.deadline_at = None
            continue

        # Mismo idpk, msgId nuevo. Si lo que venció fue el pago de un give, el
        # enunciado pide asumir que no hubo operación y reintentarla.
        negotiation.attempts += 1
        negotiation.state = PENDING
        negotiation.deadline_at = now + timedelta(seconds=settings.negotiation_timeout_seconds)
        envelope = _proposal_envelope(negotiation, now, settings)
        negotiation.msg_ids = [*negotiation.msg_ids, envelope["msgId"]]
        await _enqueue(session, f"proposal:{negotiation.id}:{negotiation.attempts}", envelope)


# --- Outbox ----------------------------------------------------------------------------


async def claim_outbox(
    session: AsyncSession, now: datetime, settings: Settings
) -> Sequence[OutboxMessage]:
    """Retira los mensajes por publicar y los marca como entregados a connector.

    Un mensaje retirado cuyo envío no se confirma vuelve a ofrecerse pasado un
    plazo: si connector muere entre retirar y publicar, el mensaje no se pierde.
    Repetir un envío es inocuo, porque lleva el mismo idpk.
    """
    stale_before = now - timedelta(seconds=settings.outbox_redelivery_seconds)
    pending = await session.execute(
        select(OutboxMessage)
        .where(
            OutboxMessage.sent_at.is_(None),
            (OutboxMessage.claimed_at.is_(None)) | (OutboxMessage.claimed_at <= stale_before),
        )
        .order_by(OutboxMessage.id)
        .limit(_OUTBOX_BATCH)
        .with_for_update(skip_locked=True)
    )
    messages = pending.scalars().all()
    for message in messages:
        message.claimed_at = now
    await session.commit()
    return messages


async def mark_outbox_sent(session: AsyncSession, message_id: int, now: datetime) -> bool:
    """Registra que connector publicó el mensaje. Devuelve False si no existe."""
    marked = await session.execute(
        update(OutboxMessage).where(OutboxMessage.id == message_id).values(sent_at=now)
    )
    await session.commit()
    return marked.rowcount == 1


# --- Negociaciones voluntarias ---------------------------------------------------------


async def create_negotiation(
    session: AsyncSession,
    direction: str,
    quantity: Decimal,
    price_per_energy: Decimal | None,
    now: datetime,
    settings: Settings,
) -> Negotiation:
    """Crea una propuesta para el ciclo abierto y la deja lista para publicarse.

    Valida acá lo que la central rechazaría de todos modos (tope de precio y
    energía vendible): los dos números se derivan de nuestro propio
    status-statement, así que no hace falta gastar un mensaje para saberlo.
    """
    # El bloqueo sobre el ciclo serializa las propuestas: dos give simultáneos no
    # pueden pasar ambos la validación de energía vendible.
    current = await session.execute(
        select(CycleLedger)
        .where(CycleLedger.opened.is_(True), CycleLedger.valid_until > now)
        .order_by(CycleLedger.valid_until.desc())
        .limit(1)
        .with_for_update()
    )
    cycle = current.scalar_one_or_none()
    if cycle is None:
        raise NegotiationNotAllowedError(
            "CYCLE_EXPIRED", "No hay una ventana de negociación abierta"
        )

    cap = price_cap(cycle.generation_cost)
    price = cap if price_per_energy is None else price_per_energy
    if price > cap:
        raise NegotiationNotAllowedError(
            "PRICE_ABOVE_CAP", f"El precio {price} supera el tope {cap} de {cycle.cycle_id}"
        )

    if direction == GIVE:
        committed = await session.execute(
            select(func.coalesce(func.sum(Negotiation.quantity), 0)).where(
                Negotiation.cycle_id == cycle.cycle_id,
                Negotiation.direction == GIVE,
                Negotiation.state.in_(_LIVE),
            )
        )
        sellable = sellable_energy(cycle.generation_capacity, cycle.consumption)
        spare = sellable - committed.scalar_one()
        if quantity > spare:
            raise NegotiationNotAllowedError(
                "OVER_CAPACITY", f"Solo quedan {spare} kWh vendibles en {cycle.cycle_id}"
            )

    negotiation = Negotiation(
        idpk=str(uuid.uuid4()),
        cycle_id=cycle.cycle_id,
        direction=direction,
        quantity=quantity,
        price_per_energy=price,
        state=PENDING,
        attempts=1,
        msg_ids=[],
        deadline_at=now + timedelta(seconds=settings.negotiation_timeout_seconds),
    )
    session.add(negotiation)
    await session.flush()

    envelope = _proposal_envelope(negotiation, now, settings)
    negotiation.msg_ids = [envelope["msgId"]]
    await _enqueue(session, f"proposal:{negotiation.id}:1", envelope)
    await session.commit()
    # updated_at lo calcula la base en el UPDATE: se relee para devolver la fila completa.
    await session.refresh(negotiation)
    return negotiation


async def get_negotiation(session: AsyncSession, negotiation_id: int) -> Negotiation | None:
    return await session.get(Negotiation, negotiation_id)


async def list_negotiations(
    session: AsyncSession, limit: int, offset: int
) -> Sequence[Negotiation]:
    """Historial de negociaciones, de la más reciente a la más antigua, siempre acotado."""
    found = await session.execute(
        select(Negotiation).order_by(Negotiation.id.desc()).limit(limit).offset(offset)
    )
    return found.scalars().all()


async def list_cycles(session: AsyncSession, limit: int, offset: int = 0) -> Sequence[CycleLedger]:
    """Ciclos del más reciente al más antiguo, con su balance y el estado de su reporte."""
    found = await session.execute(
        select(CycleLedger)
        .order_by(CycleLedger.created_at.desc(), CycleLedger.cycle_id.desc())
        .limit(limit)
        .offset(offset)
    )
    return found.scalars().all()


async def get_cycle(session: AsyncSession, cycle_id: str) -> CycleLedger | None:
    return await session.get(CycleLedger, cycle_id)


async def list_cycle_events(session: AsyncSession, cycle_id: str) -> Sequence[LedgerEvent]:
    """Operaciones aplicadas en el ciclo, en el orden en que se aplicaron."""
    found = await session.execute(
        select(LedgerEvent).where(LedgerEvent.cycle_id == cycle_id).order_by(LedgerEvent.id)
    )
    return found.scalars().all()


async def list_cycle_negotiations(session: AsyncSession, cycle_id: str) -> Sequence[Negotiation]:
    found = await session.execute(
        select(Negotiation).where(Negotiation.cycle_id == cycle_id).order_by(Negotiation.id)
    )
    return found.scalars().all()


async def get_cycle_report(session: AsyncSession, cycle: CycleLedger) -> dict | None:
    """El negotiation-report vigente del ciclo, tal como se publicó; None si no hay."""
    if cycle.report_msg_id is None:
        return None
    found = await session.execute(
        select(OutboxMessage.payload).where(
            OutboxMessage.dedupe_key == f"report:{cycle.cycle_id}:{cycle.report_msg_id}"
        )
    )
    return found.scalar_one_or_none()


async def budget_at_close(session: AsyncSession, cycle: CycleLedger) -> Decimal:
    """Presupuesto al cierre del ciclo: lo acumulado hasta él, incluido."""
    summed = await session.execute(
        select(func.coalesce(func.sum(CycleLedger.budget_delta), 0)).where(
            CycleLedger.created_at <= cycle.created_at
        )
    )
    return Decimal(summed.scalar_one())


# --- Conectividad y registro de mensajes no aplicados ----------------------------------


async def current_distance_table(session: AsyncSession) -> DistanceTable | None:
    """La distance-table vigente: la última que publicó la central."""
    found = await session.execute(select(DistanceTable).order_by(DistanceTable.id.desc()).limit(1))
    return found.scalar_one_or_none()


async def record_unapplied_message(
    session: AsyncSession, category: str, raw_content: str, reason: str | None
) -> MessageLog:
    """Deja constancia de un mensaje duplicado, descartado o respondido con NACK.

    Los campos del envelope se extraen si el contenido se puede parsear; un
    mensaje descartado puede no ser JSON, y entonces se guarda solo el texto.
    """
    envelope = _parse_envelope(raw_content)
    entry = MessageLog(
        category=category,
        idpk=_text_or_none(envelope.get("idpk")),
        msg_id=_text_or_none(envelope.get("msgId")),
        message_type=_text_or_none(envelope.get("type")),
        cycle_id=_text_or_none(envelope.get("cycleId")),
        reason=reason[:128] if reason else None,
        raw_content=raw_content,
    )
    session.add(entry)
    await session.commit()
    return entry


async def list_message_log(
    session: AsyncSession, category: str | None, limit: int, offset: int
) -> Sequence[MessageLog]:
    """Registro de mensajes no aplicados, del más reciente al más antiguo, siempre acotado."""
    statement = select(MessageLog)
    if category is not None:
        statement = statement.where(MessageLog.category == category)
    statement = statement.order_by(MessageLog.id.desc()).limit(limit).offset(offset)

    return (await session.execute(statement)).scalars().all()


async def count_message_log(session: AsyncSession, category: str | None) -> int:
    statement = select(func.count(MessageLog.id))
    if category is not None:
        statement = statement.where(MessageLog.category == category)

    return (await session.execute(statement)).scalar_one()


async def total_budget(session: AsyncSession) -> Decimal:
    """Presupuesto actual: la suma de todos los eventos, porque se traspasa entre ciclos."""
    summed = await session.execute(select(func.coalesce(func.sum(LedgerEvent.budget_delta), 0)))
    return Decimal(summed.scalar_one())


# --- Piezas internas -------------------------------------------------------------------


async def _ensure_cycle(session: AsyncSession, cycle_id: str) -> None:
    """Crea la fila del ciclo si no existe; el primer mensaje que llega la abre."""
    await session.execute(
        postgres_insert(CycleLedger)
        .values(
            cycle_id=cycle_id,
            opened=False,
            energy_balance=Decimal(0),
            budget_delta=Decimal(0),
        )
        .on_conflict_do_nothing(index_elements=["cycle_id"])
    )


async def _record_event(
    session: AsyncSession,
    message: dict,
    cycle_id: str,
    energy_delta: Decimal,
    budget_delta: Decimal,
    idpk: str | None = None,
    kind: str | None = None,
) -> bool:
    """Registra el evento y actualiza la proyección; False si el idpk ya estaba.

    El INSERT ... ON CONFLICT es la barrera de idempotencia: resuelve en la base,
    de forma atómica, lo que un "consultar y después insertar" dejaría como carrera
    entre las dos réplicas.
    """
    inserted = await session.execute(
        postgres_insert(LedgerEvent)
        .values(
            idpk=idpk or message["idpk"],
            cycle_id=cycle_id,
            kind=kind or message["type"],
            energy_delta=energy_delta,
            budget_delta=budget_delta,
            payload=message,
        )
        .on_conflict_do_nothing(index_elements=["idpk"])
        .returning(LedgerEvent.id)
    )
    if inserted.scalar_one_or_none() is None:
        return False

    await session.execute(
        update(CycleLedger)
        .where(CycleLedger.cycle_id == cycle_id)
        .values(
            energy_balance=CycleLedger.energy_balance + energy_delta,
            budget_delta=CycleLedger.budget_delta + budget_delta,
            last_operation_idpk=idpk or message["idpk"],
        )
    )
    return True


async def _negotiation_by_attempt(session: AsyncSession, msg_id: str) -> Negotiation | None:
    """Busca la negociación a la que pertenece cualquiera de sus envíos."""
    found = await session.execute(select(Negotiation).where(Negotiation.msg_ids.contains([msg_id])))
    return found.scalar_one_or_none()


async def _enqueue(session: AsyncSession, dedupe_key: str, payload: dict) -> None:
    await session.execute(
        postgres_insert(OutboxMessage)
        .values(dedupe_key=dedupe_key, payload=payload)
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )


def _proposal_envelope(negotiation: Negotiation, now: datetime, settings: Settings) -> dict:
    return _envelope(
        "negotiation-proposal",
        negotiation.idpk,
        negotiation.cycle_id,
        {
            "direction": negotiation.direction,
            "quantity": _number(negotiation.quantity),
            "pricePerEnergy": _number(negotiation.price_per_energy),
        },
        now,
        settings,
    )


def _envelope(
    message_type: str, idpk: str, cycle_id: str, data: dict, now: datetime, settings: Settings
) -> dict:
    """Envelope v2 de un mensaje nuestro: msgId nuevo en cada envío, y cityId."""
    return {
        "idpk": idpk,
        "msgId": str(uuid.uuid4()),
        "type": message_type,
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "cityId": settings.city_code,
        "cycleId": cycle_id,
        "data": data,
    }


def _parse_envelope(raw_content: str) -> dict:
    try:
        parsed = json.loads(raw_content)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text_or_none(value: object) -> str | None:
    """Un campo del envelope solo si es texto: un mensaje malformado puede traer cualquier cosa."""
    return value[:64] if isinstance(value, str) else None


def _decimal(value: object) -> Decimal:
    # bool es subclase de int: sin este filtro, `true` se aplicaría como 1.
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise TypeError(f"se esperaba un número y llegó {value!r}")
    return Decimal(str(value))


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"fecha sin zona horaria: {value!r}")
    return parsed


def _number(value: Decimal) -> int | float:
    """Número JSON: entero si no tiene decimales, para no publicar 131212.0."""
    return int(value) if value == value.to_integral_value() else float(value)
