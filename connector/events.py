"""Mensajes que connector entiende: validación del envelope v2 y armado de respuestas.

Tiene dos partes. La primera es el protocolo de la E1 con la central: qué mensaje
se descarta, cuál se responde con NACK y cómo se arma un ACK. La segunda es el
evento demand-set de la E0, que sigue llegando por la cola del observer.

master define su propia versión de estos contratos a propósito: son quanta
independientes y no comparten código (CLAUDE.md 2). Lo único que ambos acuerdan
es el nombre y el tipo de cada campo del JSON.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

VALID_TYPES = {
    "ack",
    "nack",
    "error",
    "status-statement",
    "transfer",
    "demand-statement",
    "distance-table",
    "give",
    "take",
    "negotiation-proposal",
    "negotiation-report",
    "request",
}

# No se hace ACK de un ACK, de un NACK ni de un error: evita el loop de cortesía.
TYPES_WITHOUT_ACK = {"ack", "nack", "error"}


def construir_mensaje_ack(msg_id_origen: str, city_code: str) -> dict[str, Any]:
    """ACK de un mensaje recibido; solo confirma la recepción, no su contenido."""
    return {
        # idpk y msgId nuevos y distintos entre sí: es un mensaje independiente.
        "idpk": str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": "ack",
        "timestamp": _ahora(),
        "cityId": city_code,
        "data": {"target": msg_id_origen},
    }


def construir_mensaje_nack(
    target: str,
    reason: str,
    code: int,
    mensaje: str,
    cycle_id: str | None = None,
    city_code: str | None = None,
) -> dict[str, Any]:
    """NACK de un mensaje malformado. `reason` y `code` van en el nivel superior."""
    data: dict[str, Any] = {"target": target, "message": mensaje}
    if cycle_id is not None:
        # Se devuelve tal cual, para que el emisor aparee el rechazo con su mensaje.
        data["cycleId"] = cycle_id

    nack: dict[str, Any] = {
        "idpk": str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": "nack",
        "reason": reason,
        "code": code,
        "timestamp": _ahora(),
        "data": data,
    }
    if city_code is not None:
        nack["cityId"] = city_code
    return nack


def construir_solicitud_directa(tipo_pedido: str, city_code: str) -> dict[str, Any]:
    """Petición directa a la central (por ejemplo, `distance-table` o `status-statement`)."""
    return {
        "idpk": str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": "request",
        "timestamp": _ahora(),
        "cityId": city_code,
        "data": {"ask": tipo_pedido},
    }


def validacion_mensaje_entrante(
    body: bytes, city_code: str | None = None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Clasifica un mensaje de la central. Devuelve (mensaje, nack, razón de descarte).

    Exactamente uno de los tres viene con valor:
      - (None, None, razón): no se puede parsear o no trae msgId. Se descarta y se
        registra; no hay mensaje válido al cual responder.
      - (None, nack, None): el envelope es parseable pero inválido. Se responde NACK.
      - (mensaje, None, None): válido.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError as error:
        return (None, None, f"JSON no parseable: {error}")

    if not isinstance(data, dict):
        return (None, None, "El envelope no es un objeto JSON")

    msg_id = data.get("msgId")
    if not msg_id or not isinstance(msg_id, str):
        return (None, None, "El mensaje no incluye msgId")

    def nack(reason: str, code: int, mensaje: str) -> tuple[None, dict[str, Any], None]:
        cycle_id = data.get("cycleId")
        return (
            None,
            construir_mensaje_nack(
                msg_id,
                reason,
                code,
                mensaje,
                cycle_id if isinstance(cycle_id, str) else None,
                city_code,
            ),
            None,
        )

    idpk = data.get("idpk")
    msg_type = data.get("type")
    if not idpk or not msg_type or not data.get("timestamp") or "data" not in data:
        return nack(
            "MALFORMED_MESSAGE",
            422,
            "Faltan campos obligatorios en el envelope (idpk, msgId, type, timestamp, data)",
        )
    if not isinstance(data["data"], dict):
        return nack("MALFORMED_MESSAGE", 422, "data debe ser un objeto")
    if idpk == msg_id:
        return nack("IDPK_EQUALS_MSGID", 422, "idpk no puede ser idéntico a msgId")
    if msg_type not in VALID_TYPES:
        return nack("UNKNOWN_TYPE", 400, f"Tipo de mensaje desconocido: {msg_type}")

    return (data, None, None)


def _ahora() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# --- Evento demand-set de la E0 --------------------------------------------------------


class MalformedEventError(Exception):
    """El mensaje del broker no corresponde a un evento demand-set válido."""


class DemandItem(BaseModel):
    """Demanda de una ciudad dentro del evento."""

    city: str
    demand: float
    unit: str


class PackageBody(BaseModel):
    """Cuerpo del evento tal como lo publica la central."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    demands: list[DemandItem]
    valid_until: datetime | None = None
    meta_content: str | None = None
    constraints: dict = Field(default_factory=dict)


class DemandEventMessage(BaseModel):
    """Evento completo, listo para reenviarse a master."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    idpk: str
    type: str
    package_body: PackageBody


def parse_demand_event(body: bytes) -> DemandEventMessage:
    """Convierte el JSON crudo del broker en un evento validado.

    Levanta MalformedEventError indicando qué campo falló: tragarse el error acá
    rompería RNF1 sin que se note en desarrollo.
    """
    try:
        return DemandEventMessage.model_validate_json(body)
    except ValueError as exc:
        raise MalformedEventError(str(exc)) from exc
