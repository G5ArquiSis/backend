"""Modelo persistente de master (capa de datos).

Define los modelos de persistencia para el ledger de ciclos, eventos contables,
duplicados y negociaciones voluntarias (ADR-0002 / E1), preservando además
DemandEvent para compatibilidad con la E0.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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


class CycleLedger(Base):
    """Estado y snapshot proyectado de un ciclo de negociación (ADR-0002 / RF01 / RF03).

    Almacena los parámetros informados por status-statement (capacidad, consumo, costo),
    los balances acumulados actuales de presupuesto (traspasado entre ciclos) y energía
    (que expira al cierre), y la referencia a la última operación aplicada para lecturas O(1).
    """

    __tablename__ = "cycle_ledger"

    cycle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    generation_capacity: Mapped[int] = mapped_column(Integer, default=0)
    consumption: Mapped[int] = mapped_column(Integer, default=0)
    generation_cost: Mapped[float] = mapped_column(Float, default=0.0)
    budget_balance: Mapped[float] = mapped_column(Float, default=0.0)
    energy_balance: Mapped[int] = mapped_column(Integer, default=0)
    total_transferred_budget: Mapped[float] = mapped_column(Float, default=0.0)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_operation_idpk: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Datos reportados a la central en negotiation-report
    report_budget_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    report_energy_balance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relaciones con eventos y propuestas del ciclo
    events: Mapped[list["LedgerEvent"]] = relationship(
        "LedgerEvent", back_populates="cycle", cascade="all, delete-orphan", order_by="LedgerEvent.applied_at"
    )
    negotiations: Mapped[list["VoluntaryNegotiation"]] = relationship(
        "VoluntaryNegotiation", back_populates="cycle", cascade="all, delete-orphan", order_by="VoluntaryNegotiation.created_at"
    )


class LedgerEvent(Base):
    """Event Log inmutable de operaciones aplicadas sobre el ledger (ADR-0002 / RF01 / RF05).

    Es la fuente única de verdad auditable. Garantiza que cualquier ciclo histórico
    sea 100% reconstruible y explicable paso a paso.
    La restricción de unicidad en `idpk` garantiza idempotencia a nivel de base de datos.
    """

    __tablename__ = "ledger_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idpk: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    msg_id: Mapped[str] = mapped_column(String(64), index=True)
    cycle_id: Mapped[str] = mapped_column(String(64), ForeignKey("cycle_ledger.cycle_id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)  # status-statement, transfer, demand-statement, give, take, report
    delta_budget: Mapped[float] = mapped_column(Float, default=0.0)
    delta_energy: Mapped[int] = mapped_column(Integer, default=0)
    resulting_budget: Mapped[float] = mapped_column(Float, default=0.0)
    resulting_energy: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    cycle: Mapped["CycleLedger"] = relationship("CycleLedger", back_populates="events")

    __table_args__ = (
        Index("ix_ledger_events_cycle_applied", "cycle_id", "applied_at"),
    )


class DuplicateMessage(Base):
    """Registro consultable de mensajes duplicados o rechazados (RF05 / Anomalía 1 de la demo).

    Registra cualquier mensaje entrante cuyo `idpk` ya haya sido procesado previamente,
    así como mensajes rechazados con NACK, permitiendo auditar y demostrar al ayudante
    que el ledger no se altera dos veces.
    """

    __tablename__ = "duplicate_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idpk: Mapped[str] = mapped_column(String(64), index=True)
    msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(64))  # DUPLICATE_IDPK, MALFORMED_MESSAGE, etc.
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class VoluntaryNegotiation(Base):
    """Registro del ciclo de vida de negociaciones voluntarias (RF04 / RF01 / AD3).

    Rastrea propuestas enviadas/recibidas, su confirmación (give/take) y pago (transfer).
    """

    __tablename__ = "voluntary_negotiations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_idpk: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    cycle_id: Mapped[str] = mapped_column(String(64), ForeignKey("cycle_ledger.cycle_id"), index=True)
    direction: Mapped[str] = mapped_column(String(16))  # 'give' o 'take'
    quantity: Mapped[int] = mapped_column(Integer)
    price_per_energy: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)  # pending, confirmed, paid, expired
    confirmation_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transfer_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    cycle: Mapped["CycleLedger"] = relationship("CycleLedger", back_populates="negotiations")
