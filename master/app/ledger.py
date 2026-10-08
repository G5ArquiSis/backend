"""Reglas de cálculo del ledger y de la negociación, sin base de datos ni HTTP.

Son funciones puras: reciben números y devuelven números. Las fórmulas vienen del
enunciado de la E1 (§ Sobre la negociación y § Negociando bolsas de energía) y se
mantienen acá, juntas, para poder probarlas sin levantar nada.

Todo se calcula con Decimal: los montos son dinero y con float un total como
2024 * 220.5 puede diferir en el último decimal del que calcula la central.
"""

from decimal import ROUND_HALF_UP, Decimal

GIVE = "give"
TAKE = "take"

# La central paga un 5 % sobre el costo de generación por la energía que le vendemos.
_SELL_PREMIUM = Decimal("1.05")
_CENT = Decimal("0.01")


def round2(value: Decimal) -> Decimal:
    """Redondea a dos decimales, con el medio hacia arriba."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def price_cap(generation_cost: Decimal) -> Decimal:
    """Tope de `pricePerEnergy` de una propuesta: round2(1.05 × generationCost).

    Es el mismo número en ambas direcciones, y además el precio al que la central
    liquida un `give`.
    """
    return round2(_SELL_PREMIUM * generation_cost)


def settlement_price(direction: str, generation_cost: Decimal) -> Decimal:
    """Precio al que la central liquida la operación, que no es el que ofertamos."""
    if direction == GIVE:
        return price_cap(generation_cost)
    return generation_cost


def total_amount(energy: Decimal, price_per_energy: Decimal) -> Decimal:
    """Monto de una operación: se redondea el total, con el precio ya redondeado."""
    return round2(energy * price_per_energy)


def sellable_energy(generation_capacity: Decimal, consumption: Decimal) -> Decimal:
    """Máximo vendible en un ciclo: lo generado que la ciudad no consume."""
    return max(Decimal(0), generation_capacity - consumption)


def status_statement_energy(generation_capacity: Decimal, consumption: Decimal) -> Decimal:
    """Balance energético con que parte el ciclo; negativo si falta energía."""
    return generation_capacity - consumption


def demand_statement_deltas(quantity: Decimal, value_per_kwh: Decimal) -> tuple[Decimal, Decimal]:
    """Efecto de un demand-statement como (delta de energía, delta de presupuesto).

    Con `quantity` positivo la central entrega energía y la cobramos del
    presupuesto; con negativo la retira y nos la paga. Una sola fórmula cubre los
    dos signos.
    """
    return quantity, -(quantity * value_per_kwh)


def negotiation_deltas(direction: str, energy: Decimal, amount: Decimal) -> tuple[Decimal, Decimal]:
    """Efecto de una negociación liquidada como (delta de energía, delta de presupuesto)."""
    if direction == GIVE:
        return -energy, amount
    return energy, -amount
