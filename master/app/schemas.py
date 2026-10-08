"""DTOs del contrato HTTP de master.

Son estructuras de datos puras: exponen campos y no llevan lógica de negocio
(CLAUDE.md 7.4). Las claves viajan en camelCase porque ese es el formato que
emite el broker y que reenvía connector; el código Python usa snake_case. El
acoplamiento entre ambos servicios queda así limitado al nombre y tipo de cada
campo — connascence débil, nunca posicional (seccion 4.1).
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from typing import List, Optional

DEFAULT_PAGE_SIZE = 25  # RF3: el historial se pagina de a 25 por defecto.
MAX_PAGE_SIZE = 100


class CamelCaseModel(BaseModel):
    """Base de los DTOs: serializa en camelCase y acepta ambas formas al leer."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DemandItem(CamelCaseModel):
    """Demanda de una ciudad dentro de un evento demand-set."""

    city: str
    demand: float
    unit: str


class PackageBody(CamelCaseModel):
    """Cuerpo del evento tal como lo publica la central."""

    demands: list[DemandItem]
    valid_until: datetime | None = None
    meta_content: str | None = None
    constraints: dict = Field(default_factory=dict)


class DemandEventIn(CamelCaseModel):
    """Evento tal como connector lo envía a POST /events."""

    idpk: str
    type: str
    package_body: PackageBody


class DemandEventOut(CamelCaseModel):
    """Evento tal como lo devuelve la API, con todos los campos guardados (RF1)."""

    model_config = ConfigDict(alias_generator=to_camel,
                              populate_by_name=True, from_attributes=True)

    id: int
    idpk: str
    type: str
    received_at: datetime
    package_body: PackageBody


class PageParams(CamelCaseModel):
    """Paginación de GET /history (RF3)."""

    page: int = Field(default=1, ge=1)
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)


class HistoryFilters(CamelCaseModel):
    """Filtros de GET /history (RF4).

    Van agrupados en un objeto en vez de sueltos como seis parámetros del
    endpoint (seccion 7.1). Los filtros temporales son por día completo, que es
    la forma que muestra el enunciado: ?receivedAt=2026-08-18
    """

    idpk: str | None = None
    type: str | None = None
    received_at: date | None = None
    valid_until: date | None = None
    city: str | None = None
    unit: str | None = None


class PaginatedHistory(CamelCaseModel):
    """Una página del historial más los datos para navegar el resto."""

    items: list[DemandEventOut]
    page: int
    limit: int
    total: int
    pages: int


class AuditLogCreate(BaseModel):
    category: str
    reason: Optional[str] = None
    nack_code: Optional[int] = None
    mensaje_evento: str


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    reason: Optional[str] = None
    nack_code: Optional[int] = None
    mensaje_evento: str
    created_at: datetime


class AuditLogListResponse(BaseModel):
    items: List[AuditLogResponse]
    total: int
    page: int
    limit: int


class RouteConnectivity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    destination_code: str
    distance: float
    transport_cost: float
    enabled: bool
    updated_at: Optional[datetime] = None


class ConnectivityResponse(BaseModel):
    routes: List[RouteConnectivity]
