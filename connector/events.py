"""Mensajes que connector entiende: validación del envelope v3 y armado de respuestas.

Tiene tres partes:
1. El protocolo entre ciudades y con la central: qué mensaje se descarta, cuál se responde
   con NACK, validación estricta de identidad (user_id vs cityId) y cómo se arma un ACK.
2. Manejo de tipos retirados de la E1 (negotiation-proposal, give, take, demand-statement).
3. El evento demand-set de la E0, que sigue llegando por la cola del observer.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

VALID_TYPES = {
    # Protocolo base
    "ack",
    "nack",
    "error",
    "status-statement",
    "transfer",
    "distance-table",
    "negotiation-report",
    "request",
    # Protocolo v3: mercado
    "offer",
    "offer-updated",
    "offer-withdrawn",
    "market-closed",
    # Protocolo v3: mensajes entre ciudades
    "bid",
    "bid-accepted",
    "bid-rejected",
    # Protocolo v3: liquidaciones centrales y eventos de wallet
    "trade-recorded",
    "imbalance-settlement",
    "swap-proposed",
    "swap-settled",
    "swap-rejected",
    "swap-expired",
    "swap-cancelled",
    # Tipos retirados de la E1
    "demand-statement",
    "give",
    "take",
    "negotiation-proposal",
}

# Tipos de la E1 que la central retiró en v3 (responde TYPE_RETIRED 410)
RETIRED_TYPES = {
    "demand-statement",
    "give",
    "take",
    "negotiation-proposal",
}

# Mensajes a los que NUNCA se responde con ACK a nivel de aplicación (evita loops y cumple v3)
TYPES_WITHOUT_ACK = {
    "ack",
    "nack",
    "error",
    "offer",
    "offer-updated",
    "offer-withdrawn",
    "market-closed",
    "bid-rejected",
}


def construir_mensaje_ack(msg_id_origen: str, city_code: str) -> dict[str, Any]:
    """ACK de un mensaje recibido; solo confirma la recepción, no su contenido."""
    return {
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
    """NACK de un mensaje malformado o no aceptado. `reason` y `code` van en nivel superior."""
    data: dict[str, Any] = {"target": target, "message": mensaje}
    if cycle_id is not None:
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
    body: bytes,
    city_code: str | None = None,
    user_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Clasifica un mensaje entrante de la cola de la ciudad. Devuelve (mensaje, nack, razón).

    Verifica:
    - JSON parseable y presencia de msgId.
    - Tipos retirados de la E1 (se descartan sin NACK ni reintento).
    - Reglas de identidad de la E2: si el mensaje trae cityId (ej: bid), user_id de AMQP
      debe ser 'city.{cityId}'. De lo contrario se responde NACK IDENTITY_MISMATCH (403).
    - Envelope válido (idpk != msgId, campos obligatorios).
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

    msg_type = data.get("type")

    # 1. Tratamiento de tipos retirados de la E1
    if msg_type in RETIRED_TYPES:
        return (
            None,
            None,
            f"TYPE_RETIRED: el tipo '{msg_type}' fue retirado en el protocolo v3 (410)",
        )

    # 2. Si es una notificación de error TYPE_RETIRED de la central, es válida para registro
    if msg_type == "error" and (data.get("reason") == "TYPE_RETIRED" or data.get("code") == 410):
        return (data, None, None)

    # Helper para construir NACK
    def nack(
        reason: str,
        code: int,
        mensaje: str,
        target_routing_key: str | None = None,
    ) -> tuple[None, dict[str, Any] | None, str | None]:
        if msg_type in TYPES_WITHOUT_ACK:
            return (None, None, f"{reason}: {mensaje} (no se responde a un {msg_type})")
        cycle_id = data.get("cycleId")
        nack_obj = construir_mensaje_nack(
            msg_id,
            reason,
            code,
            mensaje,
            cycle_id if isinstance(cycle_id, str) else None,
            city_code,
        )
        if target_routing_key:
            nack_obj["_target_routing_key"] = target_routing_key
        return (None, nack_obj, None)

    # 3. Validación de Identidad (user_id de AMQP) para mensajes punto a punto entre ciudades
    sender_city = data.get("cityId")
    if sender_city and msg_type in ("bid", "bid-accepted", "bid-rejected"):
        expected_user_id = f"city.{sender_city}"
        if not user_id:
            # Falta user_id en el mensaje: se descarta y registra (no se sabe a quién enviar NACK)
            return (
                None,
                None,
                f"IDENTITY_MISMATCH: falta user_id AMQP en {msg_type} de ciudad {sender_city}",
            )
        if user_id != expected_user_id:
            # user_id no corresponde a su cityId: responde NACK IDENTITY_MISMATCH a la cola
            # de quien publicó
            if user_id.startswith("city."):
                return nack(
                    "IDENTITY_MISMATCH",
                    403,
                    f"user_id '{user_id}' no corresponde al cityId '{sender_city}'",
                    target_routing_key=user_id,
                )
            return (
                None,
                None,
                f"IDENTITY_MISMATCH: user_id '{user_id}' ajeno y no corresponde a cityId "
                f"'{sender_city}'",
            )

    # 4. Validación del envelope estándar
    idpk = data.get("idpk")
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
    """Convierte el JSON crudo del broker en un evento validado."""
    try:
        return DemandEventMessage.model_validate_json(body)
    except ValueError as exc:
        raise MalformedEventError(str(exc)) from exc
