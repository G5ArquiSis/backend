from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.market.strategy import DEFAULT_STRATEGY, StrategyConfig, get_active_strategy


def test_default_strategy_has_valid_values() -> None:
    assert DEFAULT_STRATEGY.price_mode == "market_fraction"
    assert DEFAULT_STRATEGY.price_per_energy is None
    assert DEFAULT_STRATEGY.price_factor == Decimal("0.5")
    assert DEFAULT_STRATEGY.offer_quantity is None
    assert DEFAULT_STRATEGY.bid_acceptance_policy == "arrival_order"
    assert DEFAULT_STRATEGY.buy_wait_span_seconds == 60
    assert DEFAULT_STRATEGY.offer_selection_policy == "lowest_effective_price"


def test_fixed_price_requires_a_price_and_accepts_zero_floor() -> None:
    with pytest.raises(ValidationError):
        StrategyConfig(price_mode="fixed")

    config = StrategyConfig(price_mode="fixed", price_per_energy=Decimal("0"))
    assert config.price_per_energy == Decimal("0")


def test_market_fraction_rejects_fixed_price() -> None:
    with pytest.raises(ValidationError):
        StrategyConfig(
            price_mode="market_fraction",
            price_per_energy=Decimal("330.00"),
        )


def test_price_factor_must_be_between_zero_and_one() -> None:
    assert StrategyConfig(price_factor=Decimal("0")).price_factor == Decimal("0")
    assert StrategyConfig(price_factor=Decimal("1")).price_factor == Decimal("1")

    with pytest.raises(ValidationError):
        StrategyConfig(price_factor=Decimal("1.01"))
    with pytest.raises(ValidationError):
        StrategyConfig(price_factor=Decimal("-0.01"))


def test_offer_quantity_must_be_positive_integer() -> None:
    config = StrategyConfig(offer_quantity=12)
    assert config.offer_quantity == 12

    with pytest.raises(ValidationError):
        StrategyConfig(offer_quantity=0)

    with pytest.raises(ValidationError):
        StrategyConfig(offer_quantity=Decimal("12.375"))


def test_strategy_policies_are_restricted_to_supported_options() -> None:
    assert (
        StrategyConfig(bid_acceptance_policy="largest_quantity_first").bid_acceptance_policy
        == "largest_quantity_first"
    )
    assert (
        StrategyConfig(offer_selection_policy="lowest_nominal_price").offer_selection_policy
        == "lowest_nominal_price"
    )

    with pytest.raises(ValidationError):
        StrategyConfig(bid_acceptance_policy="random")
    with pytest.raises(ValidationError):
        StrategyConfig(offer_selection_policy="highest_price")


def test_span_cannot_be_negative() -> None:
    assert StrategyConfig(buy_wait_span_seconds=0).buy_wait_span_seconds == 0
    with pytest.raises(ValidationError):
        StrategyConfig(buy_wait_span_seconds=-1)


def test_strategy_is_immutable_and_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        StrategyConfig().price_factor = Decimal("0.8")  # type: ignore[misc]

    with pytest.raises(ValidationError):
        StrategyConfig(unexpected_parameter=True)


async def test_get_active_strategy_returns_default_stub() -> None:
    strategy = await get_active_strategy("cycle-001")
    assert strategy == DEFAULT_STRATEGY


async def test_get_active_strategy_rejects_empty_cycle_id() -> None:
    with pytest.raises(ValueError, match="cycle_id"):
        await get_active_strategy(" ")
