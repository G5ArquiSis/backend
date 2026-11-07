"""Contrato compartido de configuración de estrategia (C0 / AD6).

C0 define el modelo validado y un stub con valores por defecto. La persistencia de
versiones y los snapshots inmutables por ciclo corresponden a C5. Los consumidores
(D y E) deben leer la estrategia a través de ``get_active_strategy`` y no mantener
copias propias de sus parámetros.
"""

from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


PriceMode = Literal["fixed", "market_fraction"]
BidAcceptancePolicy = Literal["arrival_order", "largest_quantity_first"]
OfferSelectionPolicy = Literal["lowest_effective_price", "lowest_nominal_price"]


class StrategyConfig(BaseModel):
    """Parámetros efectivos de una estrategia para un ciclo.

    En modo ``market_fraction``, el precio se calcula con la fórmula:

    ``buybackPrice + price_factor * (imbalancePrice - buybackPrice)``.

    ``offer_quantity=None`` significa ofrecer todo el ``sellable`` disponible.
    ``buy_wait_span_seconds`` es el tiempo que el comprador espera una respuesta
    antes de reintentar o pasar a otra oferta.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    price_mode: PriceMode = "market_fraction"
    price_per_energy: Decimal | None = Field(default=None, ge=0)
    price_factor: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    offer_quantity: int | None = Field(default=None, gt=0, strict=True)
    bid_acceptance_policy: BidAcceptancePolicy = "arrival_order"
    buy_wait_span_seconds: int = Field(default=60, ge=0)
    offer_selection_policy: OfferSelectionPolicy = "lowest_effective_price"

    @model_validator(mode="after")
    def validate_price_configuration(self) -> "StrategyConfig":
        if self.price_mode == "fixed" and self.price_per_energy is None:
            raise ValueError("price_per_energy es obligatorio cuando price_mode='fixed'")
        if self.price_mode == "market_fraction" and self.price_per_energy is not None:
            raise ValueError(
                "price_per_energy debe omitirse cuando price_mode='market_fraction'"
            )
        return self


DEFAULT_STRATEGY = StrategyConfig()


async def get_active_strategy(
    cycle_id: str,
    session: "AsyncSession | None" = None,
) -> StrategyConfig:
    """Devuelve la configuración efectiva para ``cycle_id``.

    En C0 devuelve la estrategia por defecto, sin leer ni escribir la base de datos.
    ``session`` es opcional para permitir que C5 incorpore la lectura/creación del
    snapshot dentro de la transacción del consumidor, sin cambiar la llamada simple
    ``get_active_strategy(cycle_id)`` que usan los demás módulos.
    """
    if not cycle_id.strip():
        raise ValueError("cycle_id no puede estar vacío")

    # C5 reemplazará este stub por la selección transaccional de la versión activa
    # y la creación del snapshot persistido para el ciclo.
    del session
    return DEFAULT_STRATEGY
