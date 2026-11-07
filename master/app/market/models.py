"""Modelos persistentes de propiedad del mercado (Rol C)."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class SellerOffer(Base):
    """Estado persistido de la oferta propia, con una fila por ciclo.

    La fila también funciona como marcador de ``market-closed`` cuando el cierre
    llega antes que el status-statement o cuando no hay energía vendible.
    """

    __tablename__ = "seller_offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    offer_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remaining: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    price_per_energy: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    buyback_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    imbalance_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    last_offer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    market_closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="no_offer", server_default="no_offer")
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publish_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
