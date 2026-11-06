"""Eventos propios para New Relic (RNF05 de la E2): feed del mercado, llamadas a la wallet y
ciclos con o sin reporte. Las consultas, el dashboard y las alertas que los usan están en
`docs/monitoreo.md`.

- `WalletCall`: cada intento HTTP del `WalletClient`, reintentos incluidos. Atributos:
  `method`, `endpoint`, `status` (0 si no hubo respuesta), `outcome` y `durationMs`.
- `InboundMessage`: cada mensaje que connector entrega a `POST /internal/messages`. Atributos:
  `type`, `feed` (market, wallet o cycle), `sender`, `outcome` y `lagMs`.
- `CycleClosed`: una vez por ciclo, cuando vence su ventana (`validUntil`). Atributos:
  `cycleId`, `owedReport`, `reportSent`, `reportError`, `reportDelaySeconds` y `energyBalance`.

Sin agente (desarrollo local, CI, sin `NEW_RELIC_LICENSE_KEY`) registrar no hace nada. Nunca
lanza: una falla de New Relic no puede tumbar el ciclo.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime

logger = logging.getLogger(__name__)

# Tipos que llegan por la cola market.{CODE} y eventos de la wallet que la central reenvía
# a city.{CODE}. El resto es protocolo del ciclo (status-statement, transfer, ack, ...).
MARKET_TYPES = frozenset({"offer", "offer-updated", "offer-withdrawn", "market-closed"})
WALLET_TYPES = frozenset(
    {
        "swap-proposed",
        "swap-settled",
        "swap-rejected",
        "swap-cancelled",
        "swap-expired",
    }
)

_UUID_SEGMENT = re.compile(r"/[^/]+(?=/|$)")


def _send_to_new_relic(event_type: str, attributes: dict) -> None:
    import newrelic.agent

    application = newrelic.agent.application()
    newrelic.agent.record_custom_event(event_type, attributes, application=application)


# Reemplazable en los tests.
sink: Callable[[str, dict], None] = _send_to_new_relic


def record(event_type: str, attributes: dict) -> None:
    """Registra un evento propio. Los atributos `None` se omiten (New Relic los rechaza)."""
    clean = {key: value for key, value in attributes.items() if value is not None}
    try:
        sink(event_type, clean)
    except Exception:
        logger.warning("No se pudo registrar el evento %s en New Relic", event_type, exc_info=True)


# --- Wallet ---------------------------------------------------------------------------------


def wallet_endpoint(path: str) -> str:
    """`swaps/0b6f.../accept` → `/swaps/{swapId}/accept`: agrupa sin un valor por swap."""
    path = "/" + path.strip("/")
    if path.startswith("/swaps/"):
        return "/swaps/{swapId}" + _UUID_SEGMENT.sub("", path[len("/swaps") :], count=1)
    return path


def wallet_outcome(status: int | None) -> str:
    if status is None:
        return "no_response"
    if status < 400:
        return "ok"
    if status in (429, 502, 503, 504):
        return "retryable"
    return "client_error" if status < 500 else "server_error"


def wallet_call_hook(method: str, path: str, status: int | None, elapsed_seconds: float) -> None:
    """Hook para `WalletClient(..., hook=wallet_call_hook)`."""
    record(
        "WalletCall",
        {
            "method": method,
            "endpoint": wallet_endpoint(path),
            "status": status or 0,
            "outcome": wallet_outcome(status),
            "durationMs": round(elapsed_seconds * 1000, 1),
        },
    )


# --- Mensajes entrantes ---------------------------------------------------------------------


def message_feed(message_type: str) -> str:
    if message_type in MARKET_TYPES:
        return "market"
    if message_type in WALLET_TYPES:
        return "wallet"
    return "cycle"


def _parse_instant(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def record_inbound_message(message: dict, outcome: str, now: datetime) -> None:
    """`outcome`: applied, duplicate, ignored o malformed. `lagMs` es cuánto tardó el mensaje
    desde que su emisor lo fechó hasta que master lo procesó (cola + connector + master)."""
    message_type = str(message.get("type", "unknown"))
    sent_at = _parse_instant(message.get("timestamp"))
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    record(
        "InboundMessage",
        {
            "type": message_type,
            "feed": message_feed(message_type),
            "sender": message.get("sender") or message.get("cityId") or data.get("cityId"),
            "outcome": outcome,
            "lagMs": round((now - sent_at).total_seconds() * 1000) if sent_at else None,
        },
    )


# --- Ciclos ---------------------------------------------------------------------------------


def cycle_closed_attributes(cycle) -> dict:
    """Atributos de `CycleClosed` para un `CycleLedger` cuya ventana ya venció. Se arman antes
    del commit (después, el ORM ya no tiene los valores cargados) y se registran después, para
    no informar un cierre que terminó en rollback."""
    delay = None
    if cycle.report_sent_at is not None and cycle.report_due_at is not None:
        delay = round((cycle.report_sent_at - cycle.report_due_at).total_seconds(), 1)
    return {
        "cycleId": cycle.cycle_id,
        # Sin status-statement la ciudad no debe reporte: no cuenta como ciclo sin reporte.
        "owedReport": bool(cycle.opened),
        "reportSent": cycle.report_sent_at is not None,
        "reportError": cycle.report_error,
        "reportDelaySeconds": delay,
        "energyBalance": float(cycle.energy_balance) if cycle.energy_balance is not None else None,
    }
