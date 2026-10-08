"""Dobles y datos de prueba compartidos por los tests de connector.

Separado de conftest.py (que solo debe declarar fixtures) para que los tests
puedan importarlo por nombre de módulo sin ambigüedad: un `from conftest import
...` explícito choca con el conftest.py de la raíz del repo, porque ninguna de
las dos carpetas de tests es un paquete instalado.
"""

import json

import httpx


class FakeChannel:
    """Doble del canal de pika: registra publicaciones y confirmaciones en vez de tocar AMQP."""

    def __init__(self, publish_error: Exception | None = None) -> None:
        self.published: list[dict] = []
        self.user_ids: list[str] = []
        self.routing_keys: list[str] = []
        self.exchanges: list[str] = []
        self.acked: list[int] = []
        self.requeued: list[int] = []
        self._publish_error = publish_error

    def basic_publish(self, exchange, routing_key, body, properties, mandatory=False) -> None:
        if self._publish_error is not None:
            raise self._publish_error
        self.published.append(json.loads(body))
        self.user_ids.append(properties.user_id)
        self.routing_keys.append(routing_key)
        self.exchanges.append(exchange)

    def basic_ack(self, delivery_tag: int) -> None:
        self.acked.append(delivery_tag)

    def basic_nack(self, delivery_tag: int, requeue: bool = False) -> None:
        assert requeue is True, "un nack sin requeue pierde el mensaje"
        self.requeued.append(delivery_tag)


class RecordingMaster:
    """Handler de httpx que anota cada request y responde lo configurado por ruta."""

    def __init__(self, responses: dict[str, httpx.Response] | None = None) -> None:
        self.requests: list[tuple[str, object]] = []
        self._responses = responses or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.url.path, body))
        return self._responses.get(
            request.url.path, httpx.Response(200, json={"outcome": "applied"})
        )

    def bodies(self, path: str) -> list[object]:
        return [body for requested, body in self.requests if requested == path]


def central_message(message_type: str = "transfer", **overrides: object) -> dict:
    """Mensaje válido de la central en el envelope v2."""
    message = {
        "idpk": "idpk-1",
        "msgId": "msg-1",
        "type": message_type,
        "timestamp": "2026-09-01T12:00:00Z",
        "sender": "central",
        "cycleId": "cycle-9431",
        "data": {"quantity": 1000},
    }
    message.update(overrides)
    return message


def as_body(message: dict) -> bytes:
    return json.dumps(message).encode("utf-8")


def demand_set_payload(idpk: str = "3f2504e0-4f89-11d3-9a0c-0305e82c3301") -> dict:
    """Cuerpo JSON de un evento demand-set válido, tal como lo publica el broker."""
    return {
        "idpk": idpk,
        "type": "demand-set",
        "packageBody": {
            "demands": [{"city": "Los Santos", "demand": 1013.123, "unit": "GW"}],
            "validUntil": "2026-12-12T00:00:00Z",
            "metaContent": "...",
            "constraints": {},
        },
    }
