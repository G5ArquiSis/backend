"""Fixtures compartidas por los tests de connector.

connector es un paquete plano sin subcarpeta contenedora (Dockerfile lo copia
como *.py sueltos a /srv), así que sus módulos se importan por nombre directo
(`config`, `events`, `consumer`, `master_client`) y no como `connector.config`.
Este conftest agrega el directorio padre a sys.path para que los tests puedan
hacer lo mismo sin instalar el paquete.
"""

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# config.py exige estas variables al construir Settings; los tests no leen un
# .env real y las fijan directamente, con valores que ningún test necesita
# examinar más de cerca.
os.environ.setdefault("BROKER_HOST", "broker.test")
os.environ.setdefault("BROKER_USER", "observer")
os.environ.setdefault("BROKER_PASSWORD", "secret")
os.environ.setdefault("BROKER_QUEUE", "observer.test.q")
os.environ.setdefault("MASTER_EVENTS_URL", "http://master.test/events")

from master_client import MasterClient  # noqa: E402


@pytest.fixture
def master_transport_factory() -> Any:
    """Fábrica de un MasterClient cuyo HTTP va a un handler en memoria, sin red.

    httpx.MockTransport intercepta al nivel de transporte: master_client.py no se
    entera de que no hay socket, así que el test ejerce el mismo código que
    producción hasta el punto exacto donde termina la responsabilidad del cliente.
    """

    def build(handler: Any) -> MasterClient:
        client = MasterClient()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://master.test"
        )
        return client

    return build


@pytest.fixture
async def master_client_ok() -> AsyncIterator[MasterClient]:
    """MasterClient cuyo POST siempre responde 201, para tests que no verifican el fallo."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 1})

    client = MasterClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    yield client
    await client.aclose()
