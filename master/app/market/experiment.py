"""Datos del experimento de span (rol D, tarea D5; RF06).

Por cada ciclo en que TAL pujó:

- el span usado (el de sus bids: la estrategia se fija por ciclo, AD6);
- el déficit y cuánto se cubrió en el mercado (kWh de bids con swap liquidado);
- el costo efectivo: lo pagado en el mercado (precio del swap + transporte) más el desbalance
  de lo que faltó, frente al baseline de no comprar nada (déficit × `imbalancePrice`);
- los bids rechazados.

El transporte es el de la distance-table al momento del bid; el cobro real llega en
`trade-recorded` y puede diferir por redondeo.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.market.book import market_of, round2
from app.market.models import BID_REJECTED, BID_SETTLED, MarketBid


@dataclass
class CycleResult:
    cycle_id: str
    span_seconds: int | None
    deficit: Decimal
    covered: Decimal
    market_cost: Decimal
    imbalance_cost: Decimal
    baseline_cost: Decimal
    bids_sent: int
    bids_rejected: int

    @property
    def effective_cost(self) -> Decimal:
        return self.market_cost + self.imbalance_cost

    @property
    def savings(self) -> Decimal:
        return self.baseline_cost - self.effective_cost


@dataclass
class SpanSummary:
    span_seconds: int | None
    cycles: int = 0
    deficit: Decimal = Decimal(0)
    covered: Decimal = Decimal(0)
    effective_cost: Decimal = Decimal(0)
    baseline_cost: Decimal = Decimal(0)
    bids_sent: int = 0
    bids_rejected: int = 0
    cycle_ids: list[str] = field(default_factory=list)


async def span_experiment(session: AsyncSession) -> tuple[list[CycleResult], list[SpanSummary]]:
    found = await session.execute(select(MarketBid).order_by(MarketBid.cycle_id, MarketBid.id))
    by_cycle: dict[str, list[MarketBid]] = defaultdict(list)
    for bid in found.scalars():
        by_cycle[bid.cycle_id].append(bid)

    cycles = []
    for cycle_id, cycle_bids in by_cycle.items():
        market = await market_of(session, cycle_id)
        if market is None:
            continue
        settled = [b for b in cycle_bids if b.state == BID_SETTLED]
        bought = sum((b.quantity for b in settled), Decimal(0))
        market_cost = sum(
            (
                b.quantity * ((b.swap_price or b.price_per_energy) + b.transport_per_energy)
                for b in settled
            ),
            Decimal(0),
        )
        covered = min(bought, market.deficit)
        cycles.append(
            CycleResult(
                cycle_id=cycle_id,
                span_seconds=cycle_bids[0].span_seconds,
                deficit=market.deficit,
                covered=covered,
                market_cost=round2(market_cost),
                imbalance_cost=round2((market.deficit - covered) * market.imbalance_price),
                baseline_cost=round2(market.deficit * market.imbalance_price),
                bids_sent=len(cycle_bids),
                bids_rejected=sum(1 for b in cycle_bids if b.state == BID_REJECTED),
            )
        )

    summaries: dict[int | None, SpanSummary] = {}
    for result in cycles:
        summary = summaries.setdefault(result.span_seconds, SpanSummary(result.span_seconds))
        summary.cycles += 1
        summary.deficit += result.deficit
        summary.covered += result.covered
        summary.effective_cost += result.effective_cost
        summary.baseline_cost += result.baseline_cost
        summary.bids_sent += result.bids_sent
        summary.bids_rejected += result.bids_rejected
        summary.cycle_ids.append(result.cycle_id)
    ordered = sorted(summaries.values(), key=lambda s: (s.span_seconds is None, s.span_seconds))
    return cycles, ordered
