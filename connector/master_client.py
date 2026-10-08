"""Cliente HTTP hacia master, único punto de contacto entre los dos quanta.

httpx queda aislado tras esta clase (CLAUDE.md 8): sus fallos de red y sus
respuestas de error se traducen a excepciones del dominio, así consumer.py decide
qué hacer con el mensaje sin conocer las excepciones de la librería.

Es síncrono, igual que el consumidor: pika bloquea el hilo mientras espera
mensajes, y un cliente asíncrono no aportaría nada.
"""

import logging
from typing import Any

import httpx

from config import get_settings
from events import DemandEventMessage

logger = logging.getLogger(__name__)


class MasterUnavailableError(Exception):
    """master no respondió o falló: el mensaje no quedó guardado y hay que reintentar."""


class MessageRejectedError(Exception):
    """master entendió el mensaje pero no lo puede aplicar: le falta un campo de su tipo."""


class MasterClient:
    """Habla con master: eventos de la E0, mensajes del ciclo, outbox y registro."""

    def __init__(self) -> None:
        settings = get_settings()
        self._events_url = settings.master_events_url
        # MASTER_EVENTS_URL apunta a /events; el resto de los endpoints cuelga de la misma base.
        self._base_url = self._events_url.rstrip("/").removesuffix("/events")
        self._client = httpx.Client(timeout=settings.master_http_timeout_seconds)

    def publish_event(self, event: DemandEventMessage) -> None:
        """POSTea un demand-set de la E0 y retorna solo si master confirmó haberlo guardado.

        El JSON sale con las claves camelCase del contrato, no con los nombres
        internos de los atributos.
        """
        self._post(self._events_url, event.model_dump(mode="json", by_alias=True))

    def enviar_evento_master(self, mensaje: dict[str, Any]) -> str:
        """Entrega a master un mensaje de la central y devuelve applied, duplicate o ignored.

        Levanta MessageRejectedError si master lo considera malformado, y
        MasterUnavailableError si no hay cómo saber si quedó guardado.
        """
        response = self._post(f"{self._base_url}/internal/messages", mensaje, rejects=True)
        return response.json().get("outcome", "applied")

    def guardar_log_master(self, categoria: str, mensaje_evento: str, reason: str | None) -> bool:
        """Deja constancia de un mensaje descartado o respondido con NACK (RF05).

        No levanta: perder una línea del registro no justifica reencolar ni botar
        el consumo. Devuelve si quedó guardado.
        """
        try:
            self._post(
                f"{self._base_url}/internal/message-log",
                {"category": categoria, "rawContent": mensaje_evento, "reason": reason},
            )
        except MasterUnavailableError as error:
            logger.error("No se pudo registrar el mensaje %s: %s", categoria, error)
            return False
        return True

    def retirar_pendientes(self) -> list[dict[str, Any]]:
        """Mensajes que master quiere publicar a la central, como {id, payload}.

        Esta llamada es además el reloj de master: al recibirla revisa los plazos
        vencidos (reporte del ciclo, reintentos de propuestas).
        """
        return self._post(f"{self._base_url}/internal/outbox/claim", None).json()

    def confirmar_envio(self, message_id: int) -> None:
        """Avisa a master que el mensaje ya se publicó, para que deje de ofrecerlo."""
        self._post(f"{self._base_url}/internal/outbox/{message_id}/sent", None)

    def close(self) -> None:
        """Cierra el pool de conexiones HTTP."""
        self._client.close()

    def _post(self, url: str, body: Any, rejects: bool = False) -> httpx.Response:
        try:
            response = self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise MasterUnavailableError(str(exc)) from exc

        if rejects and response.status_code == 422:
            raise MessageRejectedError(response.text)
        if not response.is_success:
            raise MasterUnavailableError(
                f"master respondió {response.status_code}: {response.text}"
            )
        return response
