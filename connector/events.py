"""Schema del evento demand-set y su parseo, en la copia propia de connector.

master define su propia versión de este mismo contrato a propósito: son quanta
independientes y no comparten código (CLAUDE.md 2). Lo único que ambos acuerdan
es el nombre y el tipo de cada campo del JSON — nunca su orden ni un algoritmo
compartido.
"""

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

import json
import uuid

from typing import Any, Dict, Optional, Tuple


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


def construir_mensaje_ack(msg_id_origen: str, city_code: str):

    mensaje_ack = {
        # se genera un nuevo y aleatoria idpk para el mensaje de ack, ya que es un mensaje independiente
        "idpk": str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": "ack",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cityId": city_code,
        "data": {
            "target": msg_id_origen
        }
    }

    return mensaje_ack


def construir_mensaje_nack(target, reason, code, mensaje, cycle_id: Optional[str] = None):

    diccionario_data = {
        "target": target,
        "message": mensaje
    }

    if cycle_id != None:
        diccionario_data["cycleId"] = cycle_id

    mensaje_nack = {
        # se genera un nuevo y aleatoria idpk para el mensaje de nack, ya que es un mensaje independiente
        "idpk": str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": "nack",
        "reason": reason,
        "code": code,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": diccionario_data
    }

    return mensaje_nack


def construir_mensaje_request(msg_type: str, city_code: str, data: Dict[str, Any], cycle_id: Optional[str] = None):

    # Construye un mensaje de type request (ej: distance-table o status-statement)

    mensaje = {
        "idpk": str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": msg_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cityId": city_code,
        "data": data,
    }

    if cycle_id:
        mensaje["cycleId"] = cycle_id

    return mensaje


def validacion_mensaje_entrante(body):

    # Retorna: (msg_dict, nack_dict, discard_reason)
    # Si el mensaje se descarta (invalido) se retorna (None, None, "razón de descarte")
    # Si el mensaje no tiene los 4 atributos validos es un nack, se retorna (None, nack, None)
    # Si el mensaje es valido se retorna el json del evento (mensaje, None, None)

    try:
        data = json.loads(body.decode("utf-8"))

    except Exception as e:
        # caso en que el mensaje no puede parsearse como JSON, se descarta sin NACK porque no hay msgId al que responder
        # hay que registrarlo en el log
        return (None, None, f"JSON no parseable: {e}")

    if not isinstance(data, dict):
        return (None, None, "Envelope no es un objeto JSON")

    # para continuar con lo realizado en la E0, se siguen recibiendo eventos para guardar en nuestra bdd
    if data.get("type") == "demand-set" and "packageBody" in data:
        return (data, None, None)

    msg_id = data.get("msgId")

    if not msg_id or not isinstance(msg_id, str):
        # caso en el que el mensaje no incluye msgId. Tambien se descarta sin NACK y se registra en el log
        mensaje = "Mensaje no incluye msgId"
        return (None, None, mensaje)

    # casos en que el mensaje si puede parsearse como json y tiene un msgid
    idpk = data.get("idpk")
    msg_type = data.get("type")
    timestamp = data.get("timestamp")

    # 1. Chequeo de campos obligatorios
    if not idpk or not msg_type or not timestamp or "data" not in data:

        # se envía un nack al msgId del mensaje recibido, indicando que el mensaje es inválido
        target = msg_id
        reason = "MALFORMED_MESSAGE"
        code = 422
        mensaje = "Faltan campos obligatorios en el envelope (idpk, msgId, type, timestamp)"
        cycle_id = data.get("cycleId")

        return (None, construir_mensaje_nack(target, reason, code, mensaje, cycle_id), None)

    # 2. idpk equals msgId
    if idpk == msg_id:

        target = msg_id
        reason = "IDPK_EQUALS_MSGID"
        code = 422
        mensaje = "idpk no puede ser idéntico a msgId"
        cycle_id = data.get("cycleId")

        return (None, construir_mensaje_nack(target, reason, code, mensaje, cycle_id), None)

    # 3. Validar tipo del mensaje
    if msg_type not in VALID_TYPES:
        target = msg_id
        reason = "UNKNOWN_TYPE"
        code = 400
        mensaje = f"Este mensaje es de type desconocido: {msg_type}"
        cycle_id = data.get("cycleId")

        return (None, construir_mensaje_nack(target, reason, code, mensaje, cycle_id), None)

    return (data, None, None)


def construir_solicitud_directa(tipo_pedido: str, codigo_ciudad: str) -> Dict[str, Any]:

    # Construye un mensaje de type request (ej: distance-table o status-statement)

    return construir_mensaje_request(
        msg_type="request",
        city_code=codigo_ciudad,
        data={"ask": tipo_pedido},
    )


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
