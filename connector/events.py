"""Schema del evento demand-set y su parseo, en la copia propia de connector.

master define su propia versión de este mismo contrato a propósito: son quanta
independientes y no comparten código (CLAUDE.md 2). Lo único que ambos acuerdan
es el nombre y el tipo de cada campo del JSON — nunca su orden ni un algoritmo
compartido.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


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
