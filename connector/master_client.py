"""Cliente HTTP hacia master, único punto de contacto entre los dos quanta.

httpx queda aislado tras esta clase (CLAUDE.md 8): sus fallos de red y sus
respuestas de error se traducen a MasterUnavailableError, así consumer.py decide
qué hacer con el mensaje sin conocer las excepciones de la librería.
"""

import httpx

from config import get_settings
from events import DemandEventMessage


class MasterUnavailableError(Exception):
    """master no pudo confirmar la persistencia del evento."""


class MasterClient:
    """Envía eventos a POST /events y traduce los fallos al dominio."""

    def __init__(self) -> None:
        settings = get_settings()
        self._events_url = settings.master_events_url
        self._client = httpx.AsyncClient(timeout=settings.master_http_timeout_seconds)

    async def publish_event(self, event: DemandEventMessage) -> None:
        """POSTea el evento y retorna solo si master confirmó haberlo guardado.

        El JSON sale con las claves camelCase del contrato, no con los nombres
        internos de los atributos.
        """
        payload = event.model_dump(mode="json", by_alias=True)
        try:
            response = await self._client.post(self._events_url, json=payload)
        except httpx.HTTPError as exc:
            raise MasterUnavailableError(str(exc)) from exc

        if response.status_code not in (200, 201):
            raise MasterUnavailableError(
                f"master respondió {response.status_code}: {response.text}"
            )

    async def aclose(self) -> None:
        """Cierra el pool de conexiones HTTP."""
        await self._client.aclose()
