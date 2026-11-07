"""API de administración de la estrategia de mercado (C5 / RF04)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.market.models import SellerBid
from app.market.strategy import (
    StrategyConfig,
    get_current_strategy,
    update_active_strategy,
)
from app.strategy_schemas import (
    SellerSwapOut,
    StrategyAdminOut,
    StrategyOfferOut,
    StrategyOfferPatch,
)

router = APIRouter(tags=["strategy"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def _admin_response(config: StrategyConfig, version: int) -> StrategyAdminOut:
    return StrategyAdminOut(
        version=version,
        config=config.model_dump(mode="json", by_alias=True),
    )


def _offer_response(config: StrategyConfig, version: int) -> StrategyOfferOut:
    return StrategyOfferOut(
        version=version,
        price_mode=config.price_mode,
        price_per_energy=config.price_per_energy,
        price_factor=config.price_factor,
        offer_quantity=config.offer_quantity,
    )


@router.get("/strategy", response_model=StrategyAdminOut)
async def read_strategy(session: SessionDependency) -> StrategyAdminOut:
    """Devuelve la estrategia activa y su versión persistida."""
    config, version = await get_current_strategy(session)
    await session.commit()
    return _admin_response(config, version)


@router.put("/strategy", response_model=StrategyAdminOut)
async def replace_strategy(
    config: StrategyConfig,
    session: SessionDependency,
) -> StrategyAdminOut:
    """Crea una versión nueva con la configuración completa recibida."""
    version = await update_active_strategy(session, config)
    await session.commit()
    return _admin_response(config, version)


@router.get("/strategy/offer", response_model=StrategyOfferOut)
async def read_offer_strategy(session: SessionDependency) -> StrategyOfferOut:
    """Devuelve los parámetros de precio y cantidad de la oferta propia."""
    config, version = await get_current_strategy(session)
    await session.commit()
    return _offer_response(config, version)


@router.patch("/strategy/offer", response_model=StrategyOfferOut)
async def update_offer_strategy(
    changes: StrategyOfferPatch,
    session: SessionDependency,
) -> StrategyOfferOut:
    """Actualiza solo los parámetros de oferta, conservando el resto de la estrategia."""
    updates = changes.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="Debe enviar al menos un campo para actualizar")

    current, _ = await get_current_strategy(session)
    config_data = current.model_dump()
    config_data.update(updates)

    try:
        config = StrategyConfig.model_validate(config_data)
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    version = await update_active_strategy(session, config)
    await session.commit()
    return _offer_response(config, version)


@router.get("/strategy/swaps", response_model=list[SellerSwapOut])
async def list_in_progress_swaps(
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[SellerSwapOut]:
    """Lista bids aceptados cuyo swap vendedor todavía no llegó a estado terminal."""
    result = await session.execute(
        select(SellerBid)
        .where(SellerBid.status.in_(("reserved", "proposed", "wallet_error")))
        .order_by(SellerBid.created_at.desc(), SellerBid.id.desc())
        .limit(limit)
    )
    return [SellerSwapOut.model_validate(bid) for bid in result.scalars().all()]
