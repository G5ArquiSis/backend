"""Tests de las reglas del protocolo v3 en connector (Tareas A1, A2, A3)."""

import time

import httpx
import pytest
from support import FakeChannel, RecordingMaster, as_body

import consumer
from config import Settings
from consumer import (
    SlidingWindowRateLimiter,
    handle_market_delivery,
    publish_pending,
)
from events import (
    RETIRED_TYPES,
    validacion_mensaje_entrante,
)
from heartbeat import CONSUMERS

SETTINGS = Settings(
    broker_host="localhost",
    broker_user="user",
    broker_password="pwd",
    broker_queue="observer.q",
    city_code="TAL",
    city_broker_password="pwd",
    master_events_url="http://master:8000/events",
)


class DummyProperties:
    def __init__(self, user_id: str | None = None) -> None:
        self.user_id = user_id


def test_rate_limiter_allows_up_to_capacity_and_blocks_overflow() -> None:
    """Verifica que SlidingWindowRateLimiter admita ráfagas hasta el tope y bloquee el exceso."""
    limiter = SlidingWindowRateLimiter(max_per_minute=3)
    now = time.monotonic()

    assert limiter.acquire(now) is True
    assert limiter.acquire(now + 1.0) is True
    assert limiter.acquire(now + 2.0) is True

    # Cuarto intento en la misma ventana de 60s debe ser rechazado
    assert limiter.acquire(now + 3.0) is False
    assert limiter.wait_time(now + 3.0) > 0.0

    # 61 segundos después, la ventana se limpia y vuelve a admitir
    assert limiter.acquire(now + 61.0) is True


def test_identity_mismatch_with_wrong_user_id_emits_nack() -> None:
    """A2: Un bid con user_id ajeno ('city.ATTACKER' vs cityId 'COR') recibe NACK
    IDENTITY_MISMATCH (403)."""
    bid = {
        "idpk": "bid-idpk-1",
        "msgId": "msg-bid-1",
        "type": "bid",
        "timestamp": "2026-11-06T12:00:00Z",
        "cityId": "COR",
        "cycleId": "cycle-1",
        "data": {"offerId": "TAL-1", "quantity": 1000},
    }
    body = as_body(bid)

    message, nack, discard = validacion_mensaje_entrante(
        body, city_code="TAL", user_id="city.ATTACKER"
    )

    assert message is None
    assert discard is None
    assert nack is not None
    assert nack["type"] == "nack"
    assert nack["reason"] == "IDENTITY_MISMATCH"
    assert nack["code"] == 403
    assert nack["_target_routing_key"] == "city.ATTACKER"


def test_identity_mismatch_without_user_id_is_discarded() -> None:
    """A2: Un bid sin user_id AMQP se descarta y registra (no se sabe a quién responder)."""
    bid = {
        "idpk": "bid-idpk-2",
        "msgId": "msg-bid-2",
        "type": "bid",
        "timestamp": "2026-11-06T12:00:00Z",
        "cityId": "COR",
        "data": {"offerId": "TAL-1", "quantity": 500},
    }
    body = as_body(bid)

    message, nack, discard = validacion_mensaje_entrante(body, city_code="TAL", user_id=None)

    assert message is None
    assert nack is None
    assert discard is not None
    assert "IDENTITY_MISMATCH" in discard


def test_valid_bid_with_matching_user_id_passes() -> None:
    """A2: Un bid con user_id AMQP == 'city.COR' y cityId == 'COR' es aceptado."""
    bid = {
        "idpk": "bid-idpk-3",
        "msgId": "msg-bid-3",
        "type": "bid",
        "timestamp": "2026-11-06T12:00:00Z",
        "cityId": "COR",
        "cycleId": "cycle-1",
        "data": {"offerId": "TAL-1", "quantity": 500},
    }
    body = as_body(bid)

    message, nack, discard = validacion_mensaje_entrante(body, city_code="TAL", user_id="city.COR")

    assert (nack, discard) == (None, None)
    assert message is not None
    assert message["msgId"] == "msg-bid-3"


def test_retired_type_is_discarded_without_nack() -> None:
    """A2: Mensajes con tipos retirados de E1 se descartan y registran sin responder NACK."""
    for retired in RETIRED_TYPES:
        msg = {
            "idpk": f"idpk-{retired}",
            "msgId": f"msg-{retired}",
            "type": retired,
            "timestamp": "2026-11-06T12:00:00Z",
            "cycleId": "cycle-1",
            "data": {},
        }
        body = as_body(msg)
        message, nack, discard = validacion_mensaje_entrante(body, city_code="TAL")
        assert (message, nack) == (None, None)
        assert "TYPE_RETIRED" in discard


def test_handle_market_delivery_persists_and_acks_broker_without_app_ack(master_with) -> None:
    """A1: Los mensajes del feed de mercado se persisten y se confirman a RabbitMQ sin enviar
    ACK de aplicación."""
    master = RecordingMaster(
        {"/internal/messages": httpx.Response(200, json={"outcome": "applied"})}
    )
    channel = FakeChannel()

    offer = {
        "type": "offer",
        "cycleId": "cycle-1",
        "cityId": "HGW",
        "data": {"offerId": "HGW-1", "quantity": 10000, "pricePerEnergy": 250},
    }
    body = as_body(offer)

    handle_market_delivery(
        channel, delivery_tag=42, body=body, master=master_with(master), settings=SETTINGS
    )

    # Confirmado a RabbitMQ
    assert channel.acked == [42]
    # NUNCA se emite un ACK de aplicación en el mercado
    assert channel.published == []
    # Reenviado a master
    assert len(master.requests) == 1
    assert master.requests[0][0] == "/internal/messages"


def test_publish_pending_routes_to_market_and_cities_with_rate_limiting(master_with) -> None:
    """A3: Publica según exchange y routing_key del outbox con user_id propio y rate limiting."""
    items = [
        # Oferta al mercado
        {
            "id": 101,
            "exchange": "market",
            "routing_key": "market.offer.TAL",
            "payload": {"type": "offer", "cycleId": "c1", "data": {"offerId": "TAL-1"}},
        },
        # Bid directo a otra ciudad
        {
            "id": 102,
            "exchange": "energy.x",
            "routing_key": "city.COR",
            "payload": {
                "type": "bid",
                "cycleId": "c1",
                "cityId": "TAL",
                "data": {"offerId": "COR-1"},
            },
        },
    ]

    master = RecordingMaster(
        {
            "/internal/outbox/claim": httpx.Response(200, json=items),
            "/internal/outbox/101/sent": httpx.Response(200, json={}),
            "/internal/outbox/102/sent": httpx.Response(200, json={}),
        }
    )
    channel = FakeChannel()
    market_limiter = SlidingWindowRateLimiter(max_per_minute=60)
    request_limiter = SlidingWindowRateLimiter(max_per_minute=10)

    publish_pending(channel, master_with(master), SETTINGS, market_limiter, request_limiter)

    assert len(channel.published) == 2
    assert channel.exchanges == ["market", "energy.x"]
    assert channel.routing_keys == ["market.offer.TAL", "city.COR"]
    assert channel.user_ids == ["city.TAL", "city.TAL"]


def test_publish_pending_respects_rate_limit_and_does_not_drop(master_with) -> None:
    """A3: Cuando se alcanza el tope de mercado (60/min), los mensajes remanentes no se
    descartan ni confirman."""
    market_items = [
        {
            "id": i,
            "exchange": "market",
            "routing_key": "market.offer.TAL",
            "payload": {"type": "offer", "cycleId": "c1", "data": {"offerId": f"TAL-{i}"}},
        }
        for i in range(1, 5)
    ]

    master = RecordingMaster(
        {
            "/internal/outbox/claim": httpx.Response(200, json=market_items),
            "/internal/outbox/1/sent": httpx.Response(200, json={}),
            "/internal/outbox/2/sent": httpx.Response(200, json={}),
        }
    )
    channel = FakeChannel()
    # Limiter con capacidad 2
    small_limiter = SlidingWindowRateLimiter(max_per_minute=2)

    publish_pending(
        channel, master_with(master), SETTINGS, small_limiter, SlidingWindowRateLimiter(10)
    )

    # Solo 2 deben haberse publicado y confirmado
    assert len(channel.published) == 2
    sent_calls = [r[0] for r in master.requests if "/sent" in r[0]]
    assert sent_calls == ["/internal/outbox/1/sent", "/internal/outbox/2/sent"]


class FeedChannel(FakeChannel):
    """Canal que entrega `deliveries` por consume() y después corta el bucle."""

    def __init__(self, deliveries) -> None:
        super().__init__()
        self.deliveries = deliveries

    def basic_qos(self, prefetch_count: int) -> None:
        pass

    def consume(self, queue, inactivity_timeout=None):
        yield from self.deliveries
        raise StopFeed


class StopFeed(Exception):
    pass


def test_market_consumer_beats_as_mercado(master_with, monkeypatch) -> None:
    """El consumidor del mercado late como "mercado", el nombre que exige el healthcheck.

    Antes llamaba record_heartbeat() sin nombre: el primer mensaje lanzaba TypeError, el
    reintento llamaba record_heartbeat("market") (nombre desconocido) y el hilo moría.
    """
    beats: list[str] = []
    monkeypatch.setattr(consumer, "record_heartbeat", beats.append)
    master = RecordingMaster(
        {"/internal/messages": httpx.Response(200, json={"outcome": "applied"})}
    )
    offer = {"type": "offer", "cycleId": "c", "cityId": "HGW", "data": {"offerId": "HGW-1"}}
    method = type("Method", (), {"delivery_tag": 7})()
    channel = FeedChannel([(method, None, as_body(offer)), (None, None, None)])

    with pytest.raises(StopFeed):
        consumer.serve_market_queue(channel, master_with(master), SETTINGS)

    assert beats == ["mercado", "mercado"]
    assert channel.acked == [7]


def test_market_thread_only_with_market_enabled(monkeypatch) -> None:
    started: list[str] = []

    class Thread:
        def __init__(self, target, args, kwargs, daemon):
            self.name = args[0]

        def start(self) -> None:
            started.append(self.name)

        def join(self) -> None:
            pass

    def city_loop(name, *args, **kwargs):
        started.append(name)

    monkeypatch.setattr(consumer.threading, "Thread", Thread)
    monkeypatch.setattr(consumer, "consume_forever", city_loop)
    monkeypatch.setattr(consumer, "MasterClient", lambda: None)
    monkeypatch.setattr(consumer.signal, "signal", lambda *_: None)

    for enabled, expected in (
        (False, ["observer", "ciudad"]),
        (True, ["observer", "mercado", "ciudad"]),
    ):
        started.clear()
        monkeypatch.setattr(
            consumer,
            "get_settings",
            lambda e=enabled: SETTINGS.model_copy(update={"market_enabled": e}),
        )
        consumer.main()
        assert started == expected
        for name in started:
            assert name in CONSUMERS
