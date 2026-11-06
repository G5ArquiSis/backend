"""Los eventos v3 deben estar registrados al importar cycle_repository."""
from app import cycle_repository  # noqa: F401
from app.handler_registry import HANDLERS


def test_all_wallet_event_handlers_registered():
    expected = {
        "swap-proposed", "swap-settled", "swap-rejected", "swap-expired",
        "swap-cancelled", "trade-recorded", "imbalance-settlement", "market-closed",
    }
    assert expected.issubset(HANDLERS)


def test_legacy_messages_not_registered():
    for retired in ("give", "take", "demand-statement", "negotiation-proposal"):
        assert retired not in HANDLERS
    assert "transfer" in HANDLERS  # financiamiento sigue vigente
