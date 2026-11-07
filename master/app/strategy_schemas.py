"""DTOs HTTP para la administración de la estrategia (C5 / RF04)."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.market.strategy import StrategyConfig


class StrategyApiModel(BaseModel):
    """DTO HTTP en camelCase, compatible con los nombres Python internos."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class StrategyAdminOut(StrategyApiModel):
    version: int
    config: StrategyConfig


class StrategyOfferOut(StrategyApiModel):
    version: int
    price_mode: Literal["fixed", "market_fraction"]
    price_per_energy: Decimal | None
    price_factor: Decimal
    offer_quantity: int | None


class StrategyOfferPatch(StrategyApiModel):
    """Campos editables de la oferta; solo actualiza los campos enviados."""

    price_mode: Literal["fixed", "market_fraction"] | None = None
    price_per_energy: Decimal | None = Field(default=None, ge=0)
    price_factor: Decimal | None = Field(default=None, ge=0, le=1)
    offer_quantity: int | None = Field(default=None, gt=0, strict=True)


class SellerSwapOut(StrategyApiModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    id: int
    cycle_id: str
    offer_id: str
    buyer_city: str
    quantity: int
    price_per_energy: Decimal | None
    status: str
    swap_id: str | None
    swap_expires_at: datetime | None
    created_at: datetime
