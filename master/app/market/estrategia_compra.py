"""Parámetros de compra mientras no exista la estrategia del rol C.

El contrato interno I3 (`StrategyConfig` y `get_active_strategy(cycle_id)`) es del rol C, que
aún no está. Mientras tanto el comprador lee estos valores del entorno. Cuando C publique I3,
`buy_strategy` pasa a leer de ahí y el resto de `buyer.py` no cambia.

- `span_seconds`: cuánto espera el comprador desde que abre el mercado antes de pujar. Es el
  parámetro del experimento de span (RF06): esperar más deja que los vendedores bajen el
  precio (MET lo baja cada 60 s), pero arriesga no alcanzar a cubrir el déficit.
- `selection_policy`: cómo se eligen las ofertas. Por ahora solo `cheapest` (la de menor
  precio efectivo primero, repartiendo entre varios vendedores si hace falta).
"""

from dataclasses import dataclass

from app.config import Settings

POLICIES = ("cheapest",)


@dataclass(frozen=True)
class BuyStrategy:
    span_seconds: int
    selection_policy: str


def buy_strategy(settings: Settings, cycle_id: str) -> BuyStrategy:
    policy = settings.buy_selection_policy
    if policy not in POLICIES:
        policy = "cheapest"
    return BuyStrategy(span_seconds=settings.buy_span_seconds, selection_policy=policy)
