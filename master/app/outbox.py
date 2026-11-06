"""Módulo de outbox y contrato interno I1 (Tarea A0 de la E2).

Fija la firma de `enqueue_publish` para que los roles C (vendedor) y D (comprador)
puedan programar la publicación de ofertas al mercado y mensajes directos entre
ciudades (bids, respuestas) sin esperar a la implementación definitiva de A3/A4.

Destinos soportados:
- CENTRAL: exchange 'energy.x', routing_key 'central'
- MARKET_OFFER: exchange 'market', routing_key 'market.offer.{code}' (propio: TAL)
- CITY(code): exchange 'energy.x', routing_key 'city.{code}'

En este stub funcional de la Hora 0, la función acepta cualquier destino válido y
encola el mensaje en la tabla outbox existente. En la tarea A4 se aplican las
columnas exchange y routing_key a la base de datos mediante migraciones.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OutboxMessage


class DestinationType(StrEnum):
    """Tipos de destino admitidos en el protocolo v3."""

    CENTRAL = "CENTRAL"
    MARKET_OFFER = "MARKET_OFFER"
    CITY = "CITY"


@dataclass(frozen=True)
class Destination:
    """Destino de publicación para la outbox."""

    kind: DestinationType
    city_code: str | None = None

    @classmethod
    def central(cls) -> Destination:
        """Mensajes destinados a la central (reportes, pedidos de status-statement)."""
        return cls(kind=DestinationType.CENTRAL)

    @classmethod
    def market_offer(cls) -> Destination:
        """Mensajes dirigidos al feed público del mercado.

        Incluye: offer, offer-updated y offer-withdrawn.
        """
        return cls(kind=DestinationType.MARKET_OFFER)

    @classmethod
    def city(cls, code: str) -> Destination:
        """Mensajes directos a otra ciudad (bid, bid-accepted, bid-rejected)."""
        clean_code = code.strip().upper()
        if clean_code.startswith("CITY.") or clean_code.startswith("CITY:"):
            clean_code = clean_code[5:]
        if not clean_code:
            raise ValueError("El código de ciudad no puede estar vacío.")
        return cls(kind=DestinationType.CITY, city_code=clean_code)

    @staticmethod
    def CITY(code: str) -> Destination:
        """Alias de Destination.city(code)."""
        return Destination.city(code)

    @property
    def exchange(self) -> str:
        """Exchange AMQP al que se publicará el mensaje."""
        return self.resolve()[0]

    @property
    def routing_key(self) -> str:
        """Routing key AMQP con la que se publicará el mensaje."""
        return self.resolve()[1]

    def resolve(self, our_city_code: str = "TAL") -> tuple[str, str]:
        """Calcula el par (exchange, routing_key) según el protocolo v3 y credenciales.

        - CENTRAL: exchange='energy.x', routing_key='central'
        - MARKET_OFFER: exchange='market', routing_key='market.offer.{our_city_code}'
        - CITY(code): exchange='energy.x', routing_key='city.{code}'
        """
        if self.kind == DestinationType.CENTRAL:
            return "energy.x", "central"
        if self.kind == DestinationType.MARKET_OFFER:
            return "market", f"market.offer.{our_city_code.upper()}"
        if self.kind == DestinationType.CITY:
            return "energy.x", f"city.{self.city_code}"
        raise ValueError(f"Tipo de destino no soportado: {self.kind}")


# Singletons de conveniencia
Destination.CENTRAL = Destination(kind=DestinationType.CENTRAL)
Destination.MARKET_OFFER = Destination(kind=DestinationType.MARKET_OFFER)

CENTRAL = Destination.CENTRAL
MARKET_OFFER = Destination.MARKET_OFFER
CITY = Destination.city

DestinationInput = Destination | DestinationType | str | tuple[str, str]


def parse_destination(destination: DestinationInput) -> Destination:
    """Normaliza y valida cualquier entrada al objeto Destination estándar."""
    if isinstance(destination, Destination):
        return destination

    if isinstance(destination, DestinationType):
        if destination == DestinationType.CENTRAL:
            return Destination.CENTRAL
        if destination == DestinationType.MARKET_OFFER:
            return Destination.MARKET_OFFER
        raise ValueError("Para destino CITY se debe indicar el código de ciudad.")

    if isinstance(destination, tuple) and len(destination) == 2:
        kind, code = destination
        if str(kind).strip().upper() == "CITY":
            return Destination.city(str(code))

    if isinstance(destination, str):
        text = destination.strip()
        upper = text.upper()
        if upper == "CENTRAL":
            return Destination.CENTRAL
        if upper in ("MARKET_OFFER", "MARKET"):
            return Destination.MARKET_OFFER
        if upper.startswith("CITY.") or upper.startswith("CITY:"):
            code = text.split(".", 1)[-1].split(":", 1)[-1]
            return Destination.city(code)
        if upper.startswith("CITY(") and upper.endswith(")"):
            code = text[5:-1].strip(" '\"")
            return Destination.city(code)

    raise ValueError(
        f"Destino no reconocido: {destination!r}. Debe ser CENTRAL, MARKET_OFFER o CITY(code)."
    )


async def enqueue_publish(
    session: AsyncSession,
    dedupe_key: str,
    payload: dict[str, Any],
    destination: DestinationInput = Destination.CENTRAL,
) -> None:
    """Contrato I1 (Tarea A0): Encola un mensaje para ser retirado y publicado por connector.

    Parámetros:
        session: Sesión asíncrona de base de datos SQLAlchemy.
        dedupe_key: Clave de deduplicación única (idempotencia en réplicas/reintentos).
        payload: Mensaje JSON según el protocolo v3.
        destination: Destino ∈ {CENTRAL, MARKET_OFFER, CITY(code)}.

    En la Hora 0 (stub funcional), valida el destino y encola con compatibilidad hacia
    el esquema actual de outbox. En la tarea A4 se migran formalmente las columnas
    exchange y routing_key en base de datos.
    """
    dest = parse_destination(destination)
    exchange, routing_key = dest.resolve()

    values: dict[str, Any] = {
        "dedupe_key": dedupe_key,
        "payload": payload,
    }

    # Si la tabla ya cuenta con las columnas (post-migración A4), se guardan directamente:
    if hasattr(OutboxMessage, "exchange"):
        values["exchange"] = exchange
    if hasattr(OutboxMessage, "routing_key"):
        values["routing_key"] = routing_key

    await session.execute(
        postgres_insert(OutboxMessage)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )
