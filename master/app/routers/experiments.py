"""API del experimento de span (RF06): datos por ciclo y agregados por valor de span.

Solo traduce entre HTTP y la capa de datos; las queries viven en `app/market/experiment.py`.
"""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.market.experiment import span_experiment
from app.market.schemas import SpanAggregate, SpanCycle, SpanExperiment

router = APIRouter(tags=["experiments"])

SessionDependency = Annotated[AsyncSession, Depends(get_session)]


def _ratio(part: Decimal, whole: Decimal) -> Decimal | None:
    return (part / whole).quantize(Decimal("0.0001")) if whole else None


@router.get("/experiments/span")
async def get_span_experiment(session: SessionDependency) -> SpanExperiment:
    """Por ciclo: span usado, déficit cubierto en el mercado, costo efectivo frente al
    baseline (déficit × imbalancePrice) y bids rechazados; y lo mismo agregado por span."""
    cycles, summaries = await span_experiment(session)
    return SpanExperiment(
        cycles=[
            SpanCycle(
                cycle_id=c.cycle_id,
                span_seconds=c.span_seconds,
                deficit=c.deficit,
                covered=c.covered,
                coverage=_ratio(c.covered, c.deficit),
                market_cost=c.market_cost,
                imbalance_cost=c.imbalance_cost,
                effective_cost=c.effective_cost,
                baseline_cost=c.baseline_cost,
                savings=c.savings,
                bids_sent=c.bids_sent,
                bids_rejected=c.bids_rejected,
            )
            for c in cycles
        ],
        by_span=[
            SpanAggregate(
                span_seconds=s.span_seconds,
                cycles=s.cycles,
                cycle_ids=s.cycle_ids,
                deficit=s.deficit,
                covered=s.covered,
                coverage=_ratio(s.covered, s.deficit),
                effective_cost=s.effective_cost,
                baseline_cost=s.baseline_cost,
                cost_vs_baseline=_ratio(s.effective_cost, s.baseline_cost),
                bids_sent=s.bids_sent,
                bids_rejected=s.bids_rejected,
            )
            for s in summaries
        ],
    )
