"""Modelo persistente de master (capa de datos)."""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func, Column, Float, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base declarativa de las tablas del servicio."""


class DemandEvent(Base):
    """Un evento demand-set del broker, tal como queda guardado.

    El array `demands` se guarda como JSONB en vez de aplanarse en filas por
    ciudad para que `idpk` siga siendo único por fila: así un reintento del
    connector no puede duplicar el evento (idempotencia, CLAUDE.md 4.2).
    """

    __tablename__ = "demand_events"

    # `id` es el identificador generado por el sistema que expone el RF2; `idpk`
    # es el que trae el broker y no se usa como clave primaria para no depender
    # de un valor externo.
    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    idpk: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    meta_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    constraints: Mapped[dict] = mapped_column(JSONB, default=dict)
    demands: Mapped[list] = mapped_column(JSONB, default=list)

    __table_args__ = (
        # Sostiene el filtro temporal del RF4 y el orden por defecto del historial.
        Index("ix_demand_events_received_at", "received_at"),
        # Sostiene los filtros por city/unit, que viven dentro del JSONB.
        Index("ix_demand_events_demands", "demands", postgresql_using="gin"),
    )


class MessageAuditLog(Base):
    """RF05: Registro de mensajes descartados, duplicados y respondidos con NACK."""

    __tablename__ = "mensaje_logs"

    id = Column(Integer, primary_key=True, index=True)
    # 'discarded', 'nack', 'duplicate'
    category = Column(String(50), nullable=False, index=True)
    reason = Column(String(255), nullable=True)
    nack_code = Column(Integer, nullable=True)
    mensaje_evento = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True),
                        server_default=func.now(), index=True)


class DistanceTableEntry(Base):
    """RF02: Estado vigente de rutas y conectividad entre ciudades, con distancia y costo de transporte."""

    __tablename__ = "distance_table"

    destination_code = Column(String(10), primary_key=True, index=True)
    distance = Column(Float, nullable=False)
    transport_cost = Column(Float, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    updated_at = Column(DateTime(timezone=True),
                        server_default=func.now(), onupdate=func.now())
