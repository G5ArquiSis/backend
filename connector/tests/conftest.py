"""Fixtures compartidas por los tests de connector.

connector es un paquete plano sin subcarpeta contenedora (Dockerfile lo copia
como *.py sueltos a /srv), así que sus módulos se importan por nombre directo
(`config`, `events`, `consumer`, `master_client`) y no como `connector.config`.
Este conftest agrega el directorio padre a sys.path para que los tests puedan
hacer lo mismo sin instalar el paquete.
"""

import os
import sys
from collections.abc import Callable
from pathlib import Path

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
os.environ.setdefault("HEARTBEAT_PATH", "/tmp/connector-heartbeat-tests")

from master_client import MasterClient  # noqa: E402

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def master_with() -> Callable[[Handler], MasterClient]:
    """Fábrica de un MasterClient cuyo HTTP va a un handler en memoria, sin red.

    httpx.MockTransport intercepta al nivel de transporte: master_client.py no se
    entera de que no hay socket, así que el test ejerce el mismo código que
    producción hasta el punto exacto donde termina la responsabilidad del cliente.
    """

    def build(handler: Handler) -> MasterClient:
        client = MasterClient()
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
        return client

    return build
