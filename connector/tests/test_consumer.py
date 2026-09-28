"""Tests del ciclo de vida de consumo de connector (RNF1).

No se conecta al broker real: aio_pika queda completamente mockeado. El foco es
la política de ack/nack de handle_message y la resiliencia de run_until_shutdown,
que es lo que sostiene RNF1 (perder el broker o master no debe perder eventos ni
terminar el proceso).
"""

import asyncio
import json
import logging

import httpx
import pytest
from support import FakeIncomingMessage, demand_set_payload

from consumer import handle_message, run_until_shutdown


async def test_valid_message_is_forwarded_to_master_with_the_expected_payload(
    master_transport_factory,
) -> None:
    """Un mensaje válido produce un POST a master con el mismo evento, en camelCase."""
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(201, json={"id": 1})

    client = master_transport_factory(handler)
    message = FakeIncomingMessage(json.dumps(demand_set_payload("evento-1")).encode())

    await handle_message(message, client)

    assert len(received) == 1
    body = json.loads(received[0].content)
    assert body["idpk"] == "evento-1"
    assert body["packageBody"]["demands"][0]["city"] == "Los Santos"


async def test_valid_message_is_acked_after_master_confirms(master_transport_factory) -> None:
    """El ack solo ocurre después de que master confirma el persist (política del módulo)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 1})

    client = master_transport_factory(handler)
    message = FakeIncomingMessage(json.dumps(demand_set_payload()).encode())

    await handle_message(message, client)

    assert message.acked
    assert message.nacked_with_requeue is None


async def test_master_returning_500_requeues_the_message_instead_of_dropping_it(
    master_transport_factory,
) -> None:
    """Si master responde error, el evento no se pierde: se reencola en vez de ack."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = master_transport_factory(handler)
    message = FakeIncomingMessage(json.dumps(demand_set_payload()).encode())

    await handle_message(message, client)

    assert message.nacked_with_requeue is True
    assert not message.acked


async def test_master_returning_500_logs_the_event_id_for_diagnosis(
    master_transport_factory, caplog: pytest.LogCaptureFixture
) -> None:
    """El fallo de master queda registrado con el idpk, no en un except vacío."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = master_transport_factory(handler)
    message = FakeIncomingMessage(json.dumps(demand_set_payload("evento-diagnostico")).encode())

    with caplog.at_level(logging.ERROR):
        await handle_message(message, client)

    assert "evento-diagnostico" in caplog.text


async def test_master_connection_timeout_requeues_the_message(master_transport_factory) -> None:
    """Un timeout de red hacia master es indistinguible de un 500 para la política de ack."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = master_transport_factory(handler)
    message = FakeIncomingMessage(json.dumps(demand_set_payload()).encode())

    await handle_message(message, client)

    assert message.nacked_with_requeue is True


async def test_malformed_json_is_acked_and_not_forwarded_to_master(
    master_transport_factory,
) -> None:
    """JSON malformado no debe reencolarse para siempre: se descarta con ack."""
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(201, json={"id": 1})

    client = master_transport_factory(handler)
    message = FakeIncomingMessage(b"esto no es json")

    await handle_message(message, client)

    assert message.acked
    assert received == []


async def test_message_missing_required_fields_is_acked_and_not_forwarded(
    master_transport_factory,
) -> None:
    """Un evento sin los campos del contrato se descarta igual que el JSON malformado."""
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(201, json={"id": 1})

    client = master_transport_factory(handler)
    message = FakeIncomingMessage(json.dumps({"idpk": "sin-tipo-ni-body"}).encode())

    await handle_message(message, client)

    assert message.acked
    assert received == []


async def test_malformed_message_logs_explicitly_instead_of_silently_dropping(
    master_transport_factory, caplog: pytest.LogCaptureFixture
) -> None:
    """El descarte de un mensaje inválido queda registrado, nunca en un except vacío."""
    client = master_transport_factory(lambda request: httpx.Response(201, json={"id": 1}))
    message = FakeIncomingMessage(b"{ json invalido")

    with caplog.at_level(logging.ERROR):
        await handle_message(message, client)

    assert caplog.text.strip() != ""


async def test_broker_connection_failure_does_not_crash_and_keeps_retrying(monkeypatch) -> None:
    """RNF1: si aio_pika.connect_robust falla, el proceso reintenta en vez de terminar.

    Se cuentan los intentos de conexión y se apaga el loop apenas se observan
    varios, en vez de dejar correr run_until_shutdown indefinidamente en el test.
    """
    attempts = 0
    shutdown = asyncio.Event()

    async def failing_connect(*args: object, **kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            shutdown.set()
        raise ConnectionError("broker inalcanzable")

    monkeypatch.setattr("consumer.aio_pika.connect_robust", failing_connect)

    from config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "reconnect_initial_delay_seconds", 0)
    monkeypatch.setattr(settings, "reconnect_max_delay_seconds", 0)

    await asyncio.wait_for(run_until_shutdown(shutdown), timeout=5)

    assert attempts >= 3
