"""Build negotiation proposals from status-statement data."""

# import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from config import get_settings

config = get_settings()

CITY_ID = "TAL"
AMQP_USER_ID = config.broker_user


def build_negotiation_proposal(
    status_statement: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build a proposal, or return None when energy_delta is zero."""
    energy_delta = float(status_statement["energy_delta"])
    generation_cost = float(status_statement["generationCost"])

    if energy_delta == 0:
        return None

    direction = "take" if energy_delta < 0 else "give"
    quantity = round(abs(energy_delta), 1)
    price_per_energy = round(1.05 * generation_cost, 1)

    msg_id = str(uuid4())
    idpk = str(uuid4())
    while idpk == msg_id:
        idpk = str(uuid4())

    return {
        "idpk": idpk,
        "msgId": msg_id,
        "type": "negotiation-proposal",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cityId": CITY_ID,
        "cycleId": status_statement["cycleId"],
        "data": {
            "direction": direction,
            "quantity": quantity,
            "pricePerEnergy": price_per_energy,
        },
    }


# def _number(source: Mapping[str, Any], field: str) -> float:
#     """Read a finite JSON number, rejecting booleans and invalid values."""
#     value = source.get(field)
#     if isinstance(value, bool) or not isinstance(value, (int, float)):
#         raise ValueError(f"{field} must be a number")
#     if not math.isfinite(value):
#         raise ValueError(f"{field} must be finite")
#     return float(value)