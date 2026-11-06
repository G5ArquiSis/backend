"""Eventos propios de New Relic: forma de los atributos, sin agente ni base."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app import metrics


@pytest.fixture
def recorded(monkeypatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(metrics, "sink", lambda kind, attributes: events.append((kind, attributes)))
    return events


@pytest.mark.parametrize(
    ("path", "endpoint"),
    [
        ("swaps", "/swaps"),
        ("swaps/0b6f2d9e-4c1a-4e87-9f3b-5a2d8c7e1f40/accept", "/swaps/{swapId}/accept"),
        ("/swaps/swp-77/cancel", "/swaps/{swapId}/cancel"),
        ("swaps/swp-77", "/swaps/{swapId}"),
        ("balance", "/balance"),
        ("statement", "/statement"),
    ],
)
def test_endpoint_agrupa_los_swap_ids(path, endpoint) -> None:
    assert metrics.wallet_endpoint(path) == endpoint


def test_hook_de_la_wallet_registra_cada_intento(recorded) -> None:
    metrics.wallet_call_hook("POST", "swaps/abc/accept", 502, 0.2345)
    metrics.wallet_call_hook("GET", "balance", None, 20.0)

    assert recorded == [
        (
            "WalletCall",
            {
                "method": "POST",
                "endpoint": "/swaps/{swapId}/accept",
                "status": 502,
                "outcome": "retryable",
                "durationMs": 234.5,
            },
        ),
        (
            "WalletCall",
            {
                "method": "GET",
                "endpoint": "/balance",
                "status": 0,
                "outcome": "no_response",
                "durationMs": 20000.0,
            },
        ),
    ]


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(200, "ok"), (202, "ok"), (409, "client_error"), (429, "retryable"), (500, "server_error")],
)
def test_resultado_de_la_wallet(status, outcome) -> None:
    assert metrics.wallet_outcome(status) == outcome


def test_mensaje_entrante_clasifica_el_feed_y_mide_el_atraso(recorded) -> None:
    now = datetime(2026, 11, 5, 14, 0, 2, tzinfo=UTC)
    offer = {"type": "offer", "cityId": "HGW", "timestamp": "2026-11-05T14:00:00.500Z"}
    settled = {"type": "swap-settled", "sender": "central", "timestamp": "no es fecha"}

    metrics.record_inbound_message(offer, "applied", now)
    metrics.record_inbound_message(settled, "duplicate", now)

    assert recorded[0] == (
        "InboundMessage",
        {"type": "offer", "feed": "market", "sender": "HGW", "outcome": "applied", "lagMs": 1500},
    )
    # Sin timestamp válido no se inventa un atraso: el atributo se omite.
    assert recorded[1] == (
        "InboundMessage",
        {"type": "swap-settled", "feed": "wallet", "sender": "central", "outcome": "duplicate"},
    )


def test_ciclo_cerrado_con_y_sin_reporte() -> None:
    due = datetime(2026, 11, 5, 14, 15, tzinfo=UTC)
    reported = SimpleNamespace(
        cycle_id="c-1",
        opened=True,
        report_sent_at=due + timedelta(seconds=3),
        report_due_at=due,
        report_error=None,
        energy_balance=600,
    )
    missing = SimpleNamespace(
        cycle_id="c-2",
        opened=True,
        report_sent_at=None,
        report_due_at=due,
        report_error="CYCLE_EXPIRED",
        energy_balance=0,
    )

    assert metrics.cycle_closed_attributes(reported) == {
        "cycleId": "c-1",
        "owedReport": True,
        "reportSent": True,
        "reportError": None,
        "reportDelaySeconds": 3.0,
        "energyBalance": 600.0,
    }
    assert metrics.cycle_closed_attributes(missing)["reportSent"] is False
    assert metrics.cycle_closed_attributes(missing)["reportError"] == "CYCLE_EXPIRED"


def test_una_falla_de_new_relic_no_se_propaga(monkeypatch) -> None:
    def broken(_kind, _attributes):
        raise RuntimeError("agente caído")

    monkeypatch.setattr(metrics, "sink", broken)
    metrics.record("WalletCall", {"method": "GET"})


def test_sin_agente_registrar_no_hace_nada() -> None:
    """En tests y en local no hay license key: el agente real no debe fallar."""
    metrics._send_to_new_relic("WalletCall", {"method": "GET"})
