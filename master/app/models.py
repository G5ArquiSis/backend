"""Modelo persistente de master (capa de datos).

Define los modelos de persistencia para el ledger de ciclos, eventos contables,
duplicados y negociaciones voluntarias (ADR-0002 / E1), preservando además
DemandEvent para compatibilidad con la E0.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base declarativa de las tablas del servicio."""


class DemandEvent(Base):
    """Un evento demand-set del broker, tal como queda guardado (heredado de E0).

    El array `demands` se guarda como JSONB en vez de aplanarse en filas por
    ciudad para que `idpk` siga siendo único por fila: así un reintento del
    connector no puede duplicar el evento (idempotencia, CLAUDE.md 4.2).
    """

    __tablename__ = "demand_events"

    # `id` es el identificador generado por el sistema que expone el RF2; `idpk`
    # es el que trae el broker y no se usa como clave primaria para no depender
    # de un valor externo.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idpk: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meta_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    constraints: Mapped[dict] = mapped_column(JSONB, default=dict)
    demands: Mapped[list] = mapped_column(JSONB, default=list)

    __table_args__ = (
        # Sostiene el filtro temporal del RF4 y el orden por defecto del historial.
        Index("ix_demand_events_received_at", "received_at"),
        # Sostiene los filtros por city/unit, que viven dentro del JSONB.
        Index("ix_demand_events_demands", "demands", postgresql_using="gin"),
    )


# Los montos son dinero y la energía admite decimales: NUMERIC evita los errores de
# representación de un float al acumular deltas.
_Amount = Numeric(20, 2)
_Energy = Numeric(20, 4)


class LedgerEvent(Base):
    """Una operación aplicada al ledger: la fuente de verdad, inmutable (ADR de AD2).

    `idpk` es único: aplicar dos veces la misma operación es imposible a nivel de
    base de datos, sin depender de que el código consulte antes de insertar.
    """

    __tablename__ = "ledger_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idpk: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    energy_delta: Mapped[Decimal] = mapped_column(_Energy, default=Decimal(0))
    budget_delta: Mapped[Decimal] = mapped_column(_Amount, default=Decimal(0))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CycleLedger(Base):
    """Proyección del estado de un ciclo, actualizada junto con cada evento.

    También guarda la programación del negotiation-report: el instante de envío se
    calcula desde `valid_until`, que está persistido, y no desde un temporizador en
    memoria (ADR de AD3).
    """

    __tablename__ = "cycle_ledger"

    cycle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # True desde que llega el status-statement: solo se debe reporte por un ciclo
    # en el que la central abrió la ventana.
    opened: Mapped[bool] = mapped_column(Boolean, default=False)
    generation_capacity: Mapped[Decimal | None] = mapped_column(_Energy, nullable=True)
    consumption: Mapped[Decimal | None] = mapped_column(_Energy, nullable=True)
    generation_cost: Mapped[Decimal | None] = mapped_column(_Amount, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    energy_balance: Mapped[Decimal] = mapped_column(_Energy, default=Decimal(0))
    # Suma de los deltas de presupuesto de este ciclo. El presupuesto se traspasa
    # entre ciclos, así que el balance que se reporta es la suma de todos los eventos.
    budget_delta: Mapped[Decimal] = mapped_column(_Amount, default=Decimal(0))
    last_operation_idpk: Mapped[str | None] = mapped_column(String(64), nullable=True)

    report_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    report_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    report_idpk: Mapped[str | None] = mapped_column(String(64), nullable=True)
    report_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reported_budget: Mapped[Decimal | None] = mapped_column(_Amount, nullable=True)
    reported_energy: Mapped[Decimal | None] = mapped_column(_Energy, nullable=True)
    report_error: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Negotiation(Base):
    """Una negociación voluntaria y el estado de su máquina de estados (ADR de AD3)."""

    __tablename__ = "negotiations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Identifica la operación: se conserva en cada reintento.
    idpk: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(_Energy)
    price_per_energy: Mapped[Decimal] = mapped_column(_Amount)
    state: Mapped[str] = mapped_column(String(16), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    # msgId de cada envío: una respuesta tardía al primer intento también calza.
    msg_ids: Mapped[list] = mapped_column(JSONB, default=list)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmation_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    confirmed_energy: Mapped[Decimal | None] = mapped_column(_Energy, nullable=True)
    confirmed_price: Mapped[Decimal | None] = mapped_column(_Amount, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(_Amount, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_negotiations_msg_ids", "msg_ids", postgresql_using="gin"),)


class OutboxMessage(Base):
    """Un mensaje por publicar a la central, que connector retira y envía.

    `dedupe_key` es único: dos réplicas de master que decidan lo mismo a la vez
    (enviar el reporte de un ciclo, reintentar una propuesta) dejan una sola fila.
    """

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MessageLog(Base):
    """Mensajes que no se aplicaron: duplicados, descartados y respondidos con NACK (RF05).

    Los duplicados los registra master al detectar un idpk ya aplicado. Los
    descartados y los NACK los informa connector, que es quien valida el envelope.
    """

    __tablename__ = "message_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(16), index=True)
    # Nulos cuando el mensaje no se pudo parsear: no hay de dónde sacarlos.
    idpk: Mapped[str | None] = mapped_column(String(64), nullable=True)
    msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Texto y no JSONB: un mensaje descartado puede no ser JSON válido.
    raw_content: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("ix_message_log_received_at", "received_at"),)


class DistanceTable(Base):
    """Una distance-table recibida de la central; la vigente es la más reciente (RF02)."""

    __tablename__ = "distance_tables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idpk: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    distances: Mapped[dict] = mapped_column(JSONB, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# Alias de compatibilidad
DuplicateMessage = MessageLog
VoluntaryNegotiation = Negotiation
