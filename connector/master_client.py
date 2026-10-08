"""Cliente HTTP hacia master, único punto de contacto entre los dos quanta.

httpx queda aislado tras esta clase (CLAUDE.md 8): sus fallos de red y sus
respuestas de error se traducen a MasterUnavailableError, así consumer.py decide
qué hacer con el mensaje sin conocer las excepciones de la librería.
"""

import httpx
import logging
from typing import Any, Dict, Optional
from config import get_settings
from events import DemandEventMessage

logger = logging.getLogger(__name__)


class MasterUnavailableError(Exception):
    """master no pudo confirmar la persistencia del evento."""


class MasterClient:
    """Envía eventos a POST /events y traduce los fallos al dominio."""

    def __init__(self) -> None:
        settings = get_settings()
        self._events_url = settings.master_events_url

        self._base_url = self._events_url.rstrip("/").removesuffix("/events")

        timeout = getattr(settings, "master_http_timeout_seconds", 5.0)
        self._client = httpx.Client(timeout=timeout)

    def enviar_evento_master(self, event: Dict[str, Any]) -> str:

        # envio de evento a master, si falla se logea y se retorna "error" para que el mensaje se reencole

        try:
            # eventos de demanda de la e0, para que master los guarde en su bdd
            if event.get("type") == "demand-set" and "packageBody" in event:
                response = self._client.post(self._events_url, json=event)
                if response.status_code in (200, 201):
                    return "ok"
                else:
                    return "error"

            # eventos de ack/nack, para que master los guarde en su bdd de eventos
            response = self._client.post(
                f"{self._base_url}/interno/eventos", json=event)

            if response.status_code in (200, 201):
                data = response.json()
                return data.get("status", "ok")
            else:
                return "error"

        except httpx.HTTPError as exc:
            logger.error(f"Error reenviando evento a master: {exc}")
            return "error"

    def guardar_log_master(self, categoria: str,
                           mensaje_evento: str,
                           reason: Optional[str] = None,
                           nack_code: Optional[int] = None
                           ) -> bool:

        # RF05: se deben registrar los mensajes descartados, los nack y los duplicados en master,
        # Se hace un POST a /interno/logs con la información del mensaje y la razón del descarte/nack/duplicado.

        mensaje_log = {
            "category": categoria,
            "mensaje_evento": mensaje_evento,
            "reason": reason,
            "codigo_nack": nack_code,
        }

        try:
            response = self._client.post(
                f"{self._base_url}/interno/logs", json=mensaje_log)
            return response.status_code in (200, 201)

        except httpx.HTTPError as exc:
            logger.error(
                f"Error registrando log en master: {exc}")
            return False

    def publish_event(self, event: DemandEventMessage) -> None:
        """POSTea el evento y retorna solo si master confirmó haberlo guardado.

        El JSON sale con las claves camelCase del contrato, no con los nombres
        internos de los atributos.
        """
        payload = event.model_dump(mode="json", by_alias=True)
        try:
            response = self._client.post(self._events_url, json=payload)
        except httpx.HTTPError as exc:
            raise MasterUnavailableError(str(exc)) from exc

        if response.status_code not in (200, 201):
            raise MasterUnavailableError(
                f"master respondió {response.status_code}: {response.text}"
            )

    def aclose(self) -> None:
        """Cierra el pool de conexiones HTTP."""
        self._client.close()
