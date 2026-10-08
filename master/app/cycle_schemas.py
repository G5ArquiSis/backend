"""DTOs del ciclo de negociación: lo que entra y sale por HTTP, sin lógica."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas import CamelCaseModel


class ApplyResult(CamelCaseModel):
    """Qué pasó con un mensaje de la central: applied, duplicate o ignored."""

    outcome: str


class OutboxItem(CamelCaseModel):
    """Un mensaje que connector debe publicar a la central, tal cual viene en `payload`."""

    id: int
    payload: dict


class NegotiationIn(CamelCaseModel):
    """Propuesta que crea el administrador. Sin precio, se oferta el tope del ciclo."""

    direction: Literal["give", "take"]
    quantity: Decimal = Field(gt=0)
    price_per_energy: Decimal | None = Field(default=None, gt=0)


class NegotiationOut(CamelCaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)

    id: int
    idpk: str
    cycle_id: str
    direction: str
    quantity: Decimal
    price_per_energy: Decimal
    state: str
    attempts: int
    confirmed_energy: Decimal | None
    confirmed_price: Decimal | None
    amount: Decimal | None
    reason: str | None
    created_at: datetime
    updated_at: datetime


class CycleOut(CamelCaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)

    cycle_id: str
    opened: bool
    generation_capacity: Decimal | None
    consumption: Decimal | None
    generation_cost: Decimal | None
    valid_until: datetime | None
    energy_balance: Decimal
    budget_delta: Decimal
    last_operation_idpk: str | None
    report_due_at: datetime | None
    report_sent_at: datetime | None
    reported_budget: Decimal | None
    reported_energy: Decimal | None
    report_error: str | None


class CycleList(CamelCaseModel):
    """Últimos ciclos y el presupuesto actual, que se traspasa de un ciclo a otro."""

    budget_balance: Decimal
    items: list[CycleOut]


class LedgerEventOut(CamelCaseModel):
    """Una operación aplicada al ledger dentro de un ciclo."""

    idpk: str
    kind: str
    energy_delta: Decimal
    budget_delta: Decimal
    applied_at: datetime
    # True en la última operación aplicada del ciclo (RF01).
    is_last: bool
    message: dict


class CycleBalances(CamelCaseModel):
    """Balances finales del ciclo: la energía es propia; el presupuesto viene acumulado."""

    energy: Decimal
    budget: Decimal


class CycleDetail(CamelCaseModel):
    """Todo lo que pasó en un ciclo, en un solo documento (RF01)."""

    cycle_id: str
    opened: bool
    valid_until: datetime | None
    status_statement: dict | None
    transfers: list[LedgerEventOut]
    demand_statements: list[LedgerEventOut]
    negotiations: list[NegotiationOut]
    report: dict | None
    report_sent_at: datetime | None
    report_error: str | None
    balances: CycleBalances
    last_operation_idpk: str | None
    operations: list[LedgerEventOut]


class Connection(CamelCaseModel):
    """Ruta hacia otra ciudad, según la distance-table vigente."""

    destination: str
    distance: float | None
    transport_cost: float | None
    enabled: bool | None


class Connectivity(CamelCaseModel):
    """La distance-table vigente; `updatedAt` es nulo si la central aún no envió ninguna."""

    updated_at: datetime | None
    connections: list[Connection]


class UnappliedMessageIn(CamelCaseModel):
    """Lo que connector informa al descartar un mensaje o responderle NACK."""

    category: Literal["discarded", "nack"]
    raw_content: str
    reason: str | None = None


class MessageLogEntry(CamelCaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)

    id: int
    category: str
    idpk: str | None
    msg_id: str | None
    message_type: str | None
    cycle_id: str | None
    reason: str | None
    raw_content: str
    received_at: datetime


class MessageLogPage(CamelCaseModel):
    items: list[MessageLogEntry]
    page: int
    limit: int
    total: int
    pages: int
