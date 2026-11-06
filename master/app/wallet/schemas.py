"""Contratos de respuesta wallet v3; las lecturas no llevan Idempotency-Key."""

from typing import Literal, NotRequired, TypedDict

SwapStatus = Literal["accepted", "proposed", "settled", "cancelled", "expired", "rejected"]


class SwapResponse(TypedDict):
    swapId: str
    status: SwapStatus
    lastEvent: NotRequired[dict]


class AssetBalance(TypedDict):
    available: float
    held: float
    creditLine: NotRequired[float]


class BalanceResponse(TypedDict):
    cityId: str
    assets: dict[str, AssetBalance]
    asOf: str
    lastSettledCycle: str | None
    balanceAtLastClose: dict[str, float] | None


class StatementPage(TypedDict):
    entries: list[dict]
    nextCursor: str | None


class LeaderboardResponse(TypedDict):
    cityId: str
    score: float
    cyclesComputed: int
    position: int
