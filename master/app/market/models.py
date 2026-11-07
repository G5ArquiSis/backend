"""Tablas del mercado.

- Lado vendedor (rol C): la oferta propia de cada ciclo y los bids que recibe.
- Lado comprador (rol D): el libro de ofertas ajenas y nuestros bids.

Son tablas nuevas: `create_all` las crea en el arranque y no necesitan migración.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base

# --- Lado vendedor (rol C) ------------------------------------------------------------


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


class SellerBid(Base):
    """Bid recibido por una oferta propia y su decisión persistida (C3).

    ``reserved`` significa que la cantidad ya fue descontada de
    ``SellerOffer.remaining`` y queda pendiente de que C4 proponga el swap a la
    wallet. ``rejected`` conserva el motivo de rechazo y evita decidir dos veces
    el mismo ``idpk``.
    """

    __tablename__ = "seller_bids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idpk: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    msg_id: Mapped[str] = mapped_column(String(64))
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    offer_id: Mapped[str] = mapped_column(String(128), index=True)
    buyer_city: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    price_per_energy: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True, default="processing")
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    available: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    swap_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    swap_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    proposal_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --- Lado comprador (rol D) -----------------------------------------------------------

_Price = Numeric(20, 2)
_Energy = Numeric(20, 0)

# Estados de una oferta del libro.
OFFER_OPEN = "open"
OFFER_WITHDRAWN = "withdrawn"
OFFER_CLOSED = "closed"
# El vendedor nos rechazó por SELF_TRADE: no volver a pujar a esa oferta.
OFFER_EXCLUDED = "excluded"

# Estados de un bid nuestro.
BID_SENT = "sent"  # publicado, esperando bid-accepted o bid-rejected
BID_PROPOSED = "proposed"  # llegó bid-accepted: falta verificar el token y aceptar
BID_ACCEPTED = "accepted"  # la wallet recibió nuestro accept (202/200)
BID_SETTLED = "settled"  # swap-settled
BID_REJECTED = "rejected"  # bid-rejected o nack del vendedor
BID_TOKEN_INVALID = "token_invalid"  # el token no cumple: no se acepta
BID_FAILED = "failed"  # la wallet rechazó el accept o el swap venció
BID_EXPIRED = "expired"  # sin respuesta tras los reintentos, o cerró el mercado

# Bids que ocupan cantidad: no se vuelve a pujar por esos kWh mientras sigan vivos.
LIVE_BID_STATES = (BID_SENT, BID_PROPOSED, BID_ACCEPTED)


class MarketOffer(Base):
    """Una oferta de otra ciudad, según el feed `market.{CODE}`.

    `last_message_at` es el `timestamp` del último mensaje aplicado: un mensaje más viejo no
    pisa lo que ya se sabe, porque el feed puede llegar desordenado.
    """

    __tablename__ = "market_offers"

    offer_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    city_id: Mapped[str] = mapped_column(String(8))
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    quantity: Mapped[Decimal | None] = mapped_column(_Energy, nullable=True)
    remaining: Mapped[Decimal] = mapped_column(_Energy)
    price_per_energy: Mapped[Decimal] = mapped_column(_Price)
    status: Mapped[str] = mapped_column(String(16), default=OFFER_OPEN)
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_idpk: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MarketBid(Base):
    """Un bid nuestro y su ciclo de vida hasta el swap liquidado (D3 y D4).

    `idpk` es la operación: cada reintento lo conserva y agrega un `msgId` a `msg_ids`, que es
    lo que el vendedor cita en `data.target`. `accept_key` es la Idempotency-Key del accept en
    la wallet: se fija una vez, así que un reintento tras un 502/504 nunca acepta dos veces.
    """

    __tablename__ = "market_bids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idpk: Mapped[str] = mapped_column(String(64), unique=True)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    offer_id: Mapped[str] = mapped_column(String(128), index=True)
    seller: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(_Energy)
    price_per_energy: Mapped[Decimal] = mapped_column(_Price)
    transport_per_energy: Mapped[Decimal] = mapped_column(_Price)
    state: Mapped[str] = mapped_column(String(16), default=BID_SENT)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    msg_ids: Mapped[list] = mapped_column(JSONB, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    span_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    swap_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    swap_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    swap_amount: Mapped[Decimal | None] = mapped_column(_Price, nullable=True)
    swap_price: Mapped[Decimal | None] = mapped_column(_Price, nullable=True)
    swap_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accept_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_market_bids_msg_ids", "msg_ids", postgresql_using="gin"),)
