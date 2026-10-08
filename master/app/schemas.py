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

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)

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


# --- Modelos del protocolo v2 y del Ledger (E1 / RF01 / RF05) ---


class MessageEnvelope(CamelCaseModel):
    """Envelope v2 de los mensajes intercambiados con el broker."""

    idpk: str
    msg_id: str
    type: str
    timestamp: datetime
    cycle_id: str | None = None
    city_id: str | None = None
    sender: str | None = None
    data: dict = Field(default_factory=dict)


class StatusStatementInfo(CamelCaseModel):
    """Información del status-statement de un ciclo."""

    generation_capacity: int = 0
    consumption: int = 0
    generation_cost: float = 0.0
    valid_until: datetime | None = None
    received_at: datetime | None = None


class TransferInfo(CamelCaseModel):
    """Información de una transferencia de fondos."""

    idpk: str
    msg_id: str
    quantity: float
    penalty: float | None = None
    because_of: str | None = None
    received_at: datetime


class DemandStatementInfo(CamelCaseModel):
    """Información de una orden demand-statement aplicada."""

    idpk: str
    msg_id: str
    quantity: int
    value_per_kwh: float
    delta_budget: float
    delta_energy: int
    applied_at: datetime


class VoluntaryNegotiationInfo(CamelCaseModel):
    """Información de una negociación voluntaria."""

    idpk: str
    direction: str
    quantity: int
    price_per_energy: float
    status: str
    updated_at: datetime


class NegotiationReportInfo(CamelCaseModel):
    """Información del reporte enviado al cierre del ciclo."""

    budget_balance: float
    energy_balance: int
    submitted_at: datetime


class BalancesInfo(CamelCaseModel):
    """Balances finales o acumulados de un ciclo."""

    budget: float
    energy: int


class LastOperationInfo(CamelCaseModel):
    """Identificación de la última operación aplicada sobre un ciclo."""

    idpk: str
    type: str
    applied_at: datetime
    description: str | None = None


class OperationItem(CamelCaseModel):
    """Operación individual aplicada sobre el ledger para CycleHistory.jsx."""

    idpk: str
    kind: str
    is_last: bool = False
    applied_at: datetime


class CycleSummary(CamelCaseModel):
    """Resumen de ciclo para la lista en GET /cycles (CycleHistory.jsx)."""

    cycle_id: str
    opened: bool
    is_closed: bool
    energy_balance: int
    budget_balance: float
    final_balances: BalancesInfo
    last_operation_idpk: str | None = None
    created_at: datetime
    updated_at: datetime


class CycleDetail(CamelCaseModel):
    """Detalle completo y explicable de un ciclo para RF01 y CycleHistory.jsx."""

    cycle_id: str
    opened: bool
    is_closed: bool = False
    balances: BalancesInfo
    final_balances: BalancesInfo
    energy_balance: int
    budget_balance: float
    status_statement: StatusStatementInfo | None = None
    transfers: list[TransferInfo] = Field(default_factory=list)
    total_transferred_budget: float = 0.0
    demand_statements: list[DemandStatementInfo] = Field(default_factory=list)
    negotiations: list[VoluntaryNegotiationInfo] = Field(default_factory=list)
    voluntary_negotiations: list[VoluntaryNegotiationInfo] = Field(default_factory=list)
    operations: list[OperationItem] = Field(default_factory=list)
    last_operation_idpk: str | None = None
    last_operation: LastOperationInfo | None = None
    report: NegotiationReportInfo | None = None
    negotiation_report: NegotiationReportInfo | None = None
    report_error: bool = False
    created_at: datetime
    updated_at: datetime


class PaginatedCycles(CamelCaseModel):
    """Listado paginado de ciclos para GET /cycles (RF01 y CycleHistory.jsx)."""

    items: list[CycleSummary]
    page: int
    limit: int
    total: int
    pages: int


class MessageLogItem(CamelCaseModel):
    """Item individual del registro de mensajes para Messages.jsx (RF05)."""

    id: int
    category: str  # 'duplicate', 'discarded', 'nack'
    message_type: str | None = None
    idpk: str | None = None
    reason: str | None = None
    received_at: datetime


class PaginatedMessageLogs(CamelCaseModel):
    """Listado paginado para GET /message-log (Messages.jsx / RF05)."""

    items: list[MessageLogItem]
    page: int
    limit: int
    total: int
    pages: int


class DuplicateMessageOut(CamelCaseModel):
    """Registro de mensaje duplicado o rechazado (RF05)."""

    id: int
    idpk: str
    msg_id: str | None = None
    event_type: str | None = None
    cycle_id: str | None = None
    reason: str
    details: dict = Field(default_factory=dict)
    detected_at: datetime


class ProcessResult(CamelCaseModel):
    """Resultado del procesamiento contable de un mensaje."""

    cycle_id: str
    is_duplicate: bool
    event_type: str
    budget_balance: float
    energy_balance: int
    delta_budget: float = 0.0
    delta_energy: int = 0


