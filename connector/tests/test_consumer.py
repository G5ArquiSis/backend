"""Tests de la política de consumo de connector (RNF1, G02, AD1).

No se conecta al broker: el canal de pika es un doble que anota lo publicado y lo
confirmado, y el HTTP hacia master va a un handler en memoria. El foco es qué se
confirma, qué se reencola y qué se le responde a la central en cada caso, que es
lo que decide si un mensaje se pierde.
"""

import httpx
import pika
import pytest
from support import FakeChannel, RecordingMaster, as_body, central_message, demand_set_payload

import consumer
from config import get_settings
from consumer import (
    handle_city_delivery,
    handle_observer_delivery,
    publish_pending,
)

SETTINGS = get_settings()
MESSAGES = "/internal/messages"
LOG = "/internal/message-log"


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """La pausa tras reencolar existe para producción; en los tests solo estorba."""
    monkeypatch.setattr(consumer.time, "sleep", lambda _seconds: None)


def deliver(channel: FakeChannel, master, message: dict | bytes) -> None:
    body = message if isinstance(message, bytes) else as_body(message)
    handle_city_delivery(channel, 7, body, master, SETTINGS)


# --- Cola de la ciudad -----------------------------------------------------------------


def test_valid_message_is_stored_then_acked_to_the_central(master_with) -> None:
    """G02: master lo guarda, se confirma al broker y la central recibe su ACK."""
    master = RecordingMaster()
    channel = FakeChannel()

    deliver(channel, master_with(master), central_message())

    assert master.bodies(MESSAGES) == [central_message()]
    assert channel.acked == [7]
    [ack] = channel.published
    assert (ack["type"], ack["data"]["target"], ack["cityId"]) == ("ack", "msg-1", "TAL")
    assert channel.user_ids == ["city.TAL"]
    assert channel.routing_keys == [SETTINGS.central_routing_key]


def test_duplicate_reported_by_master_is_still_acked(master_with) -> None:
    """Un reenvío no es un error: se confirma igual, y master ya lo dejó en su registro."""
    master = RecordingMaster({MESSAGES: httpx.Response(200, json={"outcome": "duplicate"})})
    channel = FakeChannel()

    deliver(channel, master_with(master), central_message())

    assert channel.acked == [7]
    assert [message["type"] for message in channel.published] == ["ack"]


@pytest.mark.parametrize("message_type", ["ack", "nack", "error"])
def test_acks_nacks_and_errors_are_never_acknowledged(master_with, message_type: str) -> None:
    """No se hacen ACK de ACK, de NACK ni de error."""
    master = RecordingMaster()
    channel = FakeChannel()

    deliver(channel, master_with(master), central_message(message_type, data={"target": "x"}))

    assert channel.published == []
    assert channel.acked == [7]
    assert len(master.bodies(MESSAGES)) == 1


def test_message_is_requeued_and_not_acked_when_master_is_down(master_with) -> None:
    """AD1: si master no confirma, el mensaje vuelve a la cola y la central no recibe ACK."""
    master = RecordingMaster({MESSAGES: httpx.Response(503)})
    channel = FakeChannel()

    deliver(channel, master_with(master), central_message())

    assert channel.requeued == [7]
    assert channel.acked == []
    assert channel.published == []


def test_message_is_requeued_when_master_is_unreachable(master_with) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin ruta a master")

    channel = FakeChannel()

    deliver(channel, master_with(unreachable), central_message())

    assert (channel.requeued, channel.acked) == ([7], [])


def test_malformed_envelope_gets_a_nack_and_is_logged(master_with) -> None:
    """Anomalía 2: NACK a la central, registro en master, y el servicio sigue."""
    master = RecordingMaster()
    channel = FakeChannel()
    incomplete = central_message()
    del incomplete["idpk"]

    deliver(channel, master_with(master), incomplete)

    [nack] = channel.published
    assert (nack["type"], nack["reason"], nack["data"]["target"]) == (
        "nack",
        "MALFORMED_MESSAGE",
        "msg-1",
    )
    assert master.bodies(MESSAGES) == []
    [logged] = master.bodies(LOG)
    assert (logged["category"], logged["reason"]) == ("nack", "MALFORMED_MESSAGE")
    assert channel.acked == [7]


def test_content_rejected_by_master_gets_a_nack(master_with) -> None:
    """El envelope es válido, pero master no puede aplicar el contenido de ese tipo."""
    master = RecordingMaster({MESSAGES: httpx.Response(422, json={"detail": "falta quantity"})})
    channel = FakeChannel()

    deliver(channel, master_with(master), central_message(data={}))

    [nack] = channel.published
    assert (nack["type"], nack["reason"], nack["code"]) == ("nack", "MALFORMED_MESSAGE", 422)
    assert [logged["category"] for logged in master.bodies(LOG)] == ["nack"]
    assert channel.acked == [7]


def test_unparseable_message_is_discarded_and_logged_without_reply(master_with) -> None:
    """Anomalía 2: sin msgId no hay a quién responder; se registra y no se reencola."""
    master = RecordingMaster()
    channel = FakeChannel()

    deliver(channel, master_with(master), b"{esto no es json")

    assert channel.published == []
    [logged] = master.bodies(LOG)
    assert logged["category"] == "discarded"
    assert logged["rawContent"] == "{esto no es json"
    assert channel.acked == [7]


def test_message_is_confirmed_even_if_the_log_cannot_be_written(master_with) -> None:
    """Perder una línea del registro no justifica hacer circular un mensaje inválido."""
    master = RecordingMaster({LOG: httpx.Response(503)})
    channel = FakeChannel()

    deliver(channel, master_with(master), b"basura")

    assert (channel.acked, channel.requeued) == ([7], [])


def test_stored_message_is_not_requeued_when_our_ack_cannot_be_routed(master_with) -> None:
    """master ya lo guardó: reencolar solo lo repetiría como duplicado."""
    master = RecordingMaster()
    channel = FakeChannel(publish_error=pika.exceptions.UnroutableError([]))

    deliver(channel, master_with(master), central_message())

    assert (channel.acked, channel.requeued) == ([7], [])


def test_channel_failure_leaves_the_delivery_unconfirmed(master_with) -> None:
    """Se cayó la conexión: no se confirma nada y el broker reenvía al reconectar."""
    channel = FakeChannel(publish_error=pika.exceptions.ConnectionClosedByBroker(320, "bye"))

    with pytest.raises(pika.exceptions.AMQPError):
        deliver(channel, master_with(RecordingMaster()), central_message())

    assert (channel.acked, channel.requeued) == ([], [])


# --- Outbox ----------------------------------------------------------------------------


def outbox_with(*items: dict) -> RecordingMaster:
    return RecordingMaster({"/internal/outbox/claim": httpx.Response(200, json=list(items))})


def test_pending_messages_are_published_as_the_city_and_confirmed(master_with) -> None:
    """G03: el reporte que master programó sale a la central y se marca como enviado."""
    report = {"idpk": "r-1", "msgId": "m-1", "type": "negotiation-report", "cityId": "TAL"}
    master = outbox_with({"id": 12, "payload": report})
    channel = FakeChannel()

    publish_pending(channel, master_with(master), SETTINGS)

    assert channel.published == [report]
    assert channel.user_ids == ["city.TAL"]
    assert "/internal/outbox/12/sent" in [path for path, _ in master.requests]


def test_unrouted_message_is_not_confirmed_so_master_offers_it_again(master_with) -> None:
    master = outbox_with({"id": 12, "payload": {"type": "negotiation-report"}})
    channel = FakeChannel(publish_error=pika.exceptions.UnroutableError([]))

    publish_pending(channel, master_with(master), SETTINGS)

    assert "/internal/outbox/12/sent" not in [path for path, _ in master.requests]


def test_outbox_poll_survives_master_being_down(master_with) -> None:
    """Si master no responde, se intenta de nuevo en la próxima vuelta; no se cae el consumo."""
    master = RecordingMaster({"/internal/outbox/claim": httpx.Response(503)})
    channel = FakeChannel()

    publish_pending(channel, master_with(master), SETTINGS)

    assert channel.published == []


# --- Cola del observer (E0) ------------------------------------------------------------


def test_demand_set_is_forwarded_to_master_and_confirmed(master_with) -> None:
    """RNF1 de la E0: connector reenvía el evento a master por HTTP POST."""
    master = RecordingMaster({"/events": httpx.Response(201, json={"id": 1})})
    channel = FakeChannel()

    handle_observer_delivery(channel, 3, as_body(demand_set_payload()), master_with(master))

    [forwarded] = master.bodies("/events")
    assert forwarded["packageBody"]["demands"][0]["city"] == "Los Santos"
    assert channel.acked == [3]


def test_demand_set_is_requeued_when_master_is_down(master_with) -> None:
    master = RecordingMaster({"/events": httpx.Response(503)})
    channel = FakeChannel()

    handle_observer_delivery(channel, 3, as_body(demand_set_payload()), master_with(master))

    assert (channel.requeued, channel.acked) == ([3], [])


def test_malformed_demand_set_is_confirmed_and_not_forwarded(master_with) -> None:
    master = RecordingMaster()
    channel = FakeChannel()

    handle_observer_delivery(channel, 3, b'{"idpk": "x"}', master_with(master))

    assert master.requests == []
    assert channel.acked == [3]


# --- Bucle de la cola de la ciudad -----------------------------------------------------


class ScriptedChannel(FakeChannel):
    """Canal cuyo consume() entrega lo que se le programe: mensajes o silencios (None)."""

    def __init__(self, deliveries: list[bytes | None]) -> None:
        super().__init__()
        self._deliveries = deliveries
        self.declared: list[tuple[str, bool]] = []
        self.confirms_enabled = False

    def confirm_delivery(self) -> None:
        self.confirms_enabled = True

    def basic_qos(self, prefetch_count: int) -> None:
        pass

    def queue_declare(self, queue: str, passive: bool = False) -> None:
        self.declared.append((queue, passive))

    def consume(self, queue: str, inactivity_timeout: float):
        for tag, body in enumerate(self._deliveries, start=1):
            if body is None:
                yield (None, None, None)
            else:
                yield (type("Method", (), {"delivery_tag": tag}), None, body)


def test_outbox_is_polled_even_when_no_messages_arrive(master_with, monkeypatch) -> None:
    """G03: la cola pasa casi todo el ciclo en silencio y el reporte tiene que salir igual."""
    clock = iter(range(0, 1000, 3))
    monkeypatch.setattr(consumer.time, "monotonic", lambda: next(clock))
    beats: list[int] = []
    monkeypatch.setattr(consumer, "record_heartbeat", lambda: beats.append(1))
    master = RecordingMaster({"/internal/outbox/claim": httpx.Response(200, json=[])})
    channel = ScriptedChannel([None, None, None, None])

    consumer.serve_city_queue(channel, master_with(master), SETTINGS)

    polls = [path for path, _ in master.requests if path == "/internal/outbox/claim"]
    # El reloj avanza 3 s por vuelta: con el intervalo de 5 s toca consultar vuelta por medio.
    assert len(polls) == 2
    assert len(beats) == 4
    # La cola es de la central y la ciudad no tiene permiso de configuración: no se
    # declara, ni en modo pasivo (el broker respondería 403 y cerraría el canal).
    assert channel.declared == []
    assert channel.confirms_enabled is True


def test_messages_and_outbox_share_the_loop(master_with, monkeypatch) -> None:
    monkeypatch.setattr(consumer.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(consumer, "record_heartbeat", lambda: None)
    report = {"idpk": "r-1", "msgId": "m-9", "type": "negotiation-report"}
    master = RecordingMaster(
        {"/internal/outbox/claim": httpx.Response(200, json=[{"id": 1, "payload": report}])}
    )
    channel = ScriptedChannel([as_body(central_message())])

    consumer.serve_city_queue(channel, master_with(master), SETTINGS)

    assert [message["type"] for message in channel.published] == ["ack", "negotiation-report"]
    assert channel.acked == [1]


# --- Reconexión ------------------------------------------------------------------------


class Stop(BaseException):
    """Corta el bucle infinito de reconexión desde el test."""


def run_reconnections(monkeypatch, connection_seconds: float, attempts: int) -> list[float]:
    """Corre consume_forever con conexiones que duran `connection_seconds` y anota las esperas."""
    clock = {"now": 0.0}
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == attempts:
            raise Stop

    def serve(_channel) -> None:
        clock["now"] += connection_seconds
        raise pika.exceptions.ChannelClosedByBroker(403, "ACCESS_REFUSED")

    connection = type(
        "Connection",
        (),
        {"is_closed": True, "channel": lambda self: None, "close": lambda self: None},
    )
    monkeypatch.setattr(consumer.pika, "BlockingConnection", lambda _parameters: connection())
    monkeypatch.setattr(consumer.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(consumer.time, "sleep", sleep)
    monkeypatch.setattr(consumer, "record_heartbeat", lambda: None)

    with pytest.raises(Stop):
        consumer.consume_forever("ciudad", "city.TAL", "x", SETTINGS, serve=serve)
    return waits


def test_broker_rejecting_right_after_connecting_backs_off(monkeypatch) -> None:
    """Conectar y ser rechazado al instante no reinicia la espera: no se martilla al broker."""
    waits = run_reconnections(monkeypatch, connection_seconds=0.2, attempts=8)

    assert waits == [1, 2, 4, 8, 16, 32, 60, 60]


def test_connection_that_lasted_resets_the_wait(monkeypatch) -> None:
    """Tras una conexión que se sostuvo, una caída se reintenta rápido."""
    waits = run_reconnections(monkeypatch, connection_seconds=300, attempts=3)

    assert waits == [1, 1, 1]
