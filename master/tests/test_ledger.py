"""Tests de las reglas de cálculo del ledger: no necesitan base ni broker."""

from decimal import Decimal

from app.ledger import (
    GIVE,
    TAKE,
    demand_statement_deltas,
    negotiation_deltas,
    price_cap,
    round2,
    sellable_energy,
    settlement_price,
    status_statement_energy,
    total_amount,
)


def test_price_cap_matches_the_example_of_the_assignment() -> None:
    """El enunciado da el caso: con generationCost 210, el tope es 220.5."""
    assert price_cap(Decimal("210")) == Decimal("220.50")


def test_round2_rounds_half_up_and_not_to_even() -> None:
    """Con redondeo bancario 2.665 quedaría en 2.66; la central redondea el medio hacia arriba."""
    assert round2(Decimal("2.665")) == Decimal("2.67")
    assert round2(Decimal("2.675")) == Decimal("2.68")


def test_total_is_computed_from_the_already_rounded_price() -> None:
    """Multiplicar los dos números publicados debe reproducir el total de la central."""
    price = price_cap(Decimal("210.333"))

    assert price == Decimal("220.85")
    assert total_amount(Decimal("2024"), price) == Decimal("447000.40")


def test_give_settles_at_the_cap_and_take_at_generation_cost() -> None:
    cost = Decimal("210")

    assert settlement_price(GIVE, cost) == Decimal("220.50")
    assert settlement_price(TAKE, cost) == Decimal("210")


def test_sellable_energy_is_never_negative() -> None:
    assert sellable_energy(Decimal("1000"), Decimal("400")) == Decimal("600")
    assert sellable_energy(Decimal("400"), Decimal("1000")) == Decimal("0")


def test_status_statement_energy_can_be_a_deficit() -> None:
    assert status_statement_energy(Decimal("1234512"), Decimal("1444121")) == Decimal("-209609")


def test_positive_demand_statement_adds_energy_and_charges_budget() -> None:
    assert demand_statement_deltas(Decimal("1500"), Decimal("215")) == (
        Decimal("1500"),
        Decimal("-322500"),
    )


def test_negative_demand_statement_removes_energy_and_pays_budget() -> None:
    assert demand_statement_deltas(Decimal("-1500"), Decimal("215")) == (
        Decimal("-1500"),
        Decimal("322500"),
    )


def test_take_adds_energy_and_give_removes_it() -> None:
    assert negotiation_deltas(TAKE, Decimal("2024"), Decimal("425040")) == (
        Decimal("2024"),
        Decimal("-425040"),
    )
    assert negotiation_deltas(GIVE, Decimal("2024"), Decimal("446292")) == (
        Decimal("-2024"),
        Decimal("446292"),
    )
