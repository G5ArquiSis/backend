"""Configuración versionada de estrategia de mercado (C0 / C5 / AD6)."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from sqlalchemy import func, select, text

from app.market.models import CycleStrategySnapshot, StrategyState, StrategyVersion

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


PriceMode = Literal["fixed", "market_fraction"]
BidAcceptancePolicy = Literal["arrival_order", "largest_quantity_first"]
OfferSelectionPolicy = Literal["lowest_effective_price", "lowest_nominal_price"]

# Serializa la creación inicial de la fila singleton en las dos réplicas de master.
_STRATEGY_LOCK_KEY = 2173_0009  # 0006 ya lo usa la conciliación de la wallet
_STRATEGY_STATE_ID = 1


class StrategyConfig(BaseModel):
    """Parámetros efectivos de una estrategia para un ciclo.

    En modo ``market_fraction``, el precio se calcula con la fórmula:

    ``buybackPrice + price_factor * (imbalancePrice - buybackPrice)``.

    ``offer_quantity=None`` significa ofrecer todo el ``sellable`` disponible.
    ``buy_wait_span_seconds`` es el tiempo que el comprador espera una respuesta
    antes de reintentar o pasar a otra oferta.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        alias_generator=to_camel,
        populate_by_name=True,
    )

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


async def _get_or_create_state(session: "AsyncSession") -> StrategyState:
    """Obtiene y bloquea el puntero de estrategia; crea el default si no existe."""
    statement = (
        select(StrategyState)
        .where(StrategyState.id == _STRATEGY_STATE_ID)
        .with_for_update()
    )
    state = (await session.execute(statement)).scalar_one_or_none()
    if state is not None:
        return state

    # La fila singleton puede no existir en el primer arranque. El advisory lock
    # evita que las dos réplicas creen simultáneamente la versión inicial.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _STRATEGY_LOCK_KEY},
    )
    state = (await session.execute(statement)).scalar_one_or_none()
    if state is not None:
        return state

    current_max = await session.scalar(select(func.max(StrategyVersion.version)))
    version = StrategyVersion(
        version=(current_max or 0) + 1,
        config=DEFAULT_STRATEGY.model_dump(mode="json"),
    )
    session.add(version)
    await session.flush()

    state = StrategyState(
        id=_STRATEGY_STATE_ID,
        active_version_id=version.id,
    )
    session.add(state)
    await session.flush()
    return state


async def get_current_strategy(
    session: "AsyncSession",
) -> tuple[StrategyConfig, int]:
    """Devuelve la configuración global vigente y su número de versión."""
    state = await _get_or_create_state(session)
    version = await session.get(StrategyVersion, state.active_version_id)
    if version is None:
        raise RuntimeError("la versión activa de estrategia no existe")
    return StrategyConfig.model_validate(version.config), version.version


async def update_active_strategy(
    session: "AsyncSession",
    config: StrategyConfig,
) -> int:
    """Crea una versión nueva y la activa; nunca modifica versiones anteriores."""
    if not isinstance(config, StrategyConfig):
        config = StrategyConfig.model_validate(config)

    state = await _get_or_create_state(session)
    current_max = await session.scalar(select(func.max(StrategyVersion.version)))
    version = StrategyVersion(
        version=(current_max or 0) + 1,
        config=config.model_dump(mode="json"),
    )
    session.add(version)
    await session.flush()

    state.active_version_id = version.id
    state.updated_at = datetime.now(UTC)
    await session.flush()
    return version.version


async def _get_cycle_strategy(
    session: "AsyncSession",
    cycle_id: str,
) -> StrategyConfig:
    """Crea una sola vez el snapshot de ciclo y devuelve siempre ese snapshot."""
    snapshot = await session.get(CycleStrategySnapshot, cycle_id)
    if snapshot is not None:
        return StrategyConfig.model_validate(snapshot.config)

    # Serializamos el primer snapshot del ciclo respecto de cambios de estrategia.
    state = await _get_or_create_state(session)
    snapshot = await session.get(CycleStrategySnapshot, cycle_id)
    if snapshot is not None:
        return StrategyConfig.model_validate(snapshot.config)

    version = await session.get(StrategyVersion, state.active_version_id)
    if version is None:
        raise RuntimeError("la versión activa de estrategia no existe")

    config = StrategyConfig.model_validate(version.config)
    session.add(
        CycleStrategySnapshot(
            cycle_id=cycle_id,
            strategy_version_id=version.id,
            config=config.model_dump(mode="json"),
        )
    )
    await session.flush()
    return config


async def get_active_strategy(
    cycle_id: str,
    session: "AsyncSession | None" = None,
) -> StrategyConfig:
    """Devuelve el snapshot inmutable asociado a ``cycle_id``.

    Si el llamador entrega una sesión, la lectura y el snapshot forman parte de su
    transacción. Sin sesión, se crea una sesión propia para conservar la firma C0.
    """
    if not isinstance(cycle_id, str) or not cycle_id.strip():
        raise ValueError("cycle_id no puede estar vacío")

    if session is not None:
        return await _get_cycle_strategy(session, cycle_id.strip())

    from app.database import _session_factory

    async with _session_factory() as own_session:
        config = await _get_cycle_strategy(own_session, cycle_id.strip())
        await own_session.commit()
        return config
