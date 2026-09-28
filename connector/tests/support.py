"""Dobles y datos de prueba compartidos por los tests de connector.

Separado de conftest.py (que solo debe declarar fixtures) para que
test_consumer.py pueda importarlo por nombre de módulo sin ambigüedad: un
`from conftest import ...` explícito choca con el conftest.py de la raíz del
repo, porque ninguna de las dos carpetas de tests es un paquete instalado.
"""


class FakeIncomingMessage:
    """Doble de aio_pika.abc.AbstractIncomingMessage: registra ack/nack en vez de tocar AMQP."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.acked = False
        self.nacked_with_requeue: bool | None = None

    async def ack(self) -> None:
        self.acked = True

    async def nack(self, requeue: bool = False) -> None:
        self.nacked_with_requeue = requeue


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
