"""Servicio de lógica de negocio del Ledger (ADR-0002 / E1).

Aplica de forma atómica e idempotente las operaciones contables del ciclo:
- status-statement: fija capacidad, consumo y costo de generación del ciclo.
- transfer: acredita fondos al presupuesto (y registra penalties).
- demand-statement: aplica intercambio de energía según convención de signos:
    quantity > 0: central entrega energía -> +energía, -presupuesto (quantity * valuePerKwh)
    quantity < 0: central retira energía -> -|quantity|, +presupuesto (|quantity| * valuePerKwh)
- Traspaso de ciclo: el presupuesto remanente se acumula entre ciclos; la energía
  excedente no vendida se descarta al cierre (parte en 0 en el nuevo ciclo).
- Idempotencia (RF05 y Anomalía 1): si llega un idpk ya procesado, se registra
  en duplicate_messages y no se altera el ledger.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import CycleLedger, DuplicateMessage, LedgerEvent, VoluntaryNegotiation
from app.schemas import (
    BalancesInfo,
    CycleDetail,
    CycleSummary,
    DemandStatementInfo,
    LastOperationInfo,
    MessageLogItem,
    NegotiationReportInfo,
    OperationItem,
    PaginatedCycles,
    PaginatedMessageLogs,
    ProcessResult,
    StatusStatementInfo,
    TransferInfo,
    VoluntaryNegotiationInfo,
)


def _parse_datetime(value: Any) -> datetime | None:
    """Parsea un timestamp a datetime con timezone UTC si viene como string."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


class LedgerService:
    """Servicio que encapsula la aplicación contable sobre el ledger."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def process_message(
        self,
        idpk: str,
        msg_id: str,
        event_type: str,
        cycle_id: str,
        data: dict[str, Any],
        timestamp: datetime | str | None = None,
    ) -> ProcessResult:
        """Aplica un mensaje sobre el ledger de forma atómica e idempotente."""
        # 1. Verificación de Idempotencia: ¿idpk ya fue procesado?
        existing_event_query = await self.session.execute(
            select(LedgerEvent).where(LedgerEvent.idpk == idpk)
        )
        existing_event = existing_event_query.scalars().first()

        if existing_event is not None:
            # Idempotencia activada (RF05): registrar duplicado sin alterar el ledger
            duplicate = DuplicateMessage(
                idpk=idpk,
                msg_id=msg_id,
                event_type=event_type,
                cycle_id=cycle_id,
                reason="DUPLICATE_IDPK",
                details=data,
            )
            self.session.add(duplicate)
            await self.session.commit()

            # Obtener el estado actual del ciclo sin modificaciones
            cycle_query = await self.session.execute(
                select(CycleLedger).where(CycleLedger.cycle_id == cycle_id)
            )
            cycle = cycle_query.scalars().first()
            current_budget = cycle.budget_balance if cycle else 0.0
            current_energy = cycle.energy_balance if cycle else 0

            return ProcessResult(
                cycle_id=cycle_id,
                is_duplicate=True,
                event_type=event_type,
                budget_balance=current_budget,
                energy_balance=current_energy,
                delta_budget=0.0,
                delta_energy=0,
            )

        # 2. Obtener o inicializar CycleLedger (con traspaso de presupuesto)
        cycle_query = await self.session.execute(
            select(CycleLedger).where(CycleLedger.cycle_id == cycle_id).with_for_update()
        )
        cycle = cycle_query.scalars().first()

        if cycle is None:
            # Ciclo nuevo: hereda el presupuesto del ciclo más reciente; la energía parte en 0
            latest_cycle_query = await self.session.execute(
                select(CycleLedger).order_by(desc(CycleLedger.created_at)).limit(1)
            )
            latest_cycle = latest_cycle_query.scalars().first()

            initial_budget = latest_cycle.budget_balance if latest_cycle else 0.0
            initial_energy = 0  # La energía excedente se pierde al cierre del ciclo

            cycle = CycleLedger(
                cycle_id=cycle_id,
                budget_balance=initial_budget,
                energy_balance=initial_energy,
            )
            self.session.add(cycle)
            await self.session.flush()

        # 3. Aplicar reglas de negocio según el tipo de mensaje
        delta_budget = 0.0
        delta_energy = 0
        description: str | None = None

        if event_type == "status-statement":
            energy_data = data.get("energy", {})
            cycle.generation_capacity = int(energy_data.get("generationCapacity", cycle.generation_capacity))
            cycle.consumption = int(energy_data.get("consumption", cycle.consumption))
            cycle.generation_cost = float(energy_data.get("generationCost", cycle.generation_cost))
            if "validUntil" in data:
                cycle.valid_until = _parse_datetime(data["validUntil"])
            cycle.last_operation_idpk = idpk
            description = (
                f"status-statement: cap={cycle.generation_capacity} kWh, "
                f"cons={cycle.consumption} kWh, cost={cycle.generation_cost}"
            )

        elif event_type == "transfer":
            quantity = float(data.get("quantity", 0.0))
            penalty = float(data.get("penalty", 0.0)) if data.get("penalty") is not None else 0.0
            delta_budget = quantity
            cycle.budget_balance += delta_budget
            cycle.total_transferred_budget += delta_budget
            cycle.last_operation_idpk = idpk
            description = f"transfer: +{quantity:.2f} créditos recibidos (penalty={penalty:.2f})"

        elif event_type == "demand-statement":
            balance_data = data.get("balance", {})
            quantity = int(balance_data.get("quantity", 0))
            value_per_kwh = float(balance_data.get("valuePerKwh", 0.0))

            # Convención de signos del enunciado:
            # quantity > 0 (central entrega energía): +energía, -presupuesto (quantity * valuePerKwh)
            # quantity < 0 (central retira energía): -|quantity|, +presupuesto (|quantity| * valuePerKwh)
            delta_energy = quantity
            delta_budget = -(quantity * value_per_kwh)

            cycle.energy_balance += delta_energy
            cycle.budget_balance += delta_budget
            cycle.last_operation_idpk = idpk
            description = (
                f"demand-statement: {delta_energy:+d} kWh, {delta_budget:+.2f} créditos "
                f"(precio unitario: {value_per_kwh})"
            )

        elif event_type == "take":
            # Operación voluntaria confirmada: central entrega energía a la ciudad
            quantity = int(data.get("energy", data.get("quantity", 0)))
            price = float(data.get("pricePerEnergy", 0.0))
            delta_energy = quantity
            delta_budget = -(quantity * price)
            cycle.energy_balance += delta_energy
            cycle.budget_balance += delta_budget
            cycle.last_operation_idpk = idpk
            description = f"take confirmado: +{quantity} kWh, {delta_budget:+.2f} créditos"

        elif event_type == "give":
            # Operación voluntaria confirmada: ciudad entrega energía a la central
            quantity = int(data.get("energy", data.get("quantity", 0)))
            price = float(data.get("pricePerEnergy", 0.0))
            delta_energy = -quantity
            delta_budget = +(quantity * price)
            cycle.energy_balance += delta_energy
            cycle.budget_balance += delta_budget
            cycle.last_operation_idpk = idpk
            description = f"give confirmado: -{quantity} kWh, {delta_budget:+.2f} créditos"

        elif event_type == "negotiation-report":
            cycle.is_closed = True
            cycle.report_budget_balance = float(data.get("budgetBalance", cycle.budget_balance))
            cycle.report_energy_balance = int(data.get("energyBalance", cycle.energy_balance))
            cycle.report_submitted_at = _parse_datetime(timestamp) or datetime.now(UTC)
            cycle.last_operation_idpk = idpk
            description = (
                f"negotiation-report emitido: budget={cycle.report_budget_balance:.2f}, "
                f"energy={cycle.report_energy_balance}"
            )

        else:
            description = f"operación {event_type} aplicada"

        # 4. Registrar en el Event Log inmutable
        event = LedgerEvent(
            idpk=idpk,
            msg_id=msg_id,
            cycle_id=cycle_id,
            event_type=event_type,
            delta_budget=delta_budget,
            delta_energy=delta_energy,
            resulting_budget=cycle.budget_balance,
            resulting_energy=cycle.energy_balance,
            payload=data,
            description=description,
        )
        self.session.add(event)
        await self.session.commit()

        return ProcessResult(
            cycle_id=cycle_id,
            is_duplicate=False,
            event_type=event_type,
            budget_balance=cycle.budget_balance,
            energy_balance=cycle.energy_balance,
            delta_budget=delta_budget,
            delta_energy=delta_energy,
        )

    async def get_cycle(self, cycle_id: str) -> CycleLedger | None:
        """Obtiene un ciclo por su cycle_id."""
        result = await self.session.execute(
            select(CycleLedger).where(CycleLedger.cycle_id == cycle_id)
        )
        return result.scalars().first()

    async def get_current_balances(self, cycle_id: str) -> dict[str, Any]:
        """Permite al módulo de negociación (Rol C) consultar balances y capacidad restante en O(1)."""
        cycle = await self.get_cycle(cycle_id)
        if cycle is None:
            return {
                "cycleId": cycle_id,
                "budgetBalance": 0.0,
                "energyBalance": 0,
                "generationCapacity": 0,
                "consumption": 0,
                "generationCost": 0.0,
                "spare": 0,
                "isClosed": False,
            }
        spare = max(0, cycle.generation_capacity - cycle.consumption)
        return {
            "cycleId": cycle.cycle_id,
            "budgetBalance": cycle.budget_balance,
            "energyBalance": cycle.energy_balance,
            "generationCapacity": cycle.generation_capacity,
            "consumption": cycle.consumption,
            "generationCost": cycle.generation_cost,
            "spare": spare,
            "isClosed": cycle.is_closed,
        }

    async def record_negotiation_proposal(
        self,
        proposal_idpk: str,
        cycle_id: str,
        direction: str,
        quantity: int,
        price_per_energy: float,
    ) -> VoluntaryNegotiation:
        """Registra una propuesta de negociación iniciada por Rol C."""
        negotiation = VoluntaryNegotiation(
            proposal_idpk=proposal_idpk,
            cycle_id=cycle_id,
            direction=direction,
            quantity=quantity,
            price_per_energy=price_per_energy,
            status="pending",
        )
        self.session.add(negotiation)
        await self.session.commit()
        return negotiation

    async def update_negotiation_status(
        self,
        proposal_idpk: str,
        status: str,
        confirmation_msg_id: str | None = None,
        transfer_msg_id: str | None = None,
        total_amount: float = 0.0,
    ) -> VoluntaryNegotiation | None:
        """Actualiza el estado de una negociación (confirmed, paid, expired) tras respuesta de la central."""
        stmt = select(VoluntaryNegotiation).where(VoluntaryNegotiation.proposal_idpk == proposal_idpk)
        result = await self.session.execute(stmt)
        negotiation = result.scalars().first()
        if negotiation is None:
            return None

        negotiation.status = status
        if confirmation_msg_id:
            negotiation.confirmation_msg_id = confirmation_msg_id
        if transfer_msg_id:
            negotiation.transfer_msg_id = transfer_msg_id
        if total_amount:
            negotiation.total_amount = total_amount

        await self.session.commit()
        return negotiation

    async def get_cycle_detail(self, cycle_id: str) -> CycleDetail | None:
        """Construye la vista completa y explicable del ciclo para RF01."""
        stmt = (
            select(CycleLedger)
            .where(CycleLedger.cycle_id == cycle_id)
            .options(
                selectinload(CycleLedger.events),
                selectinload(CycleLedger.negotiations),
            )
        )
        result = await self.session.execute(stmt)
        cycle = result.scalars().first()
        if cycle is None:
            return None

        # Desglosar eventos por tipo
        status_info: StatusStatementInfo | None = None
        transfers: list[TransferInfo] = []
        demand_statements: list[DemandStatementInfo] = []
        last_op: LastOperationInfo | None = None

        if cycle.generation_capacity or cycle.generation_cost or cycle.valid_until:
            status_info = StatusStatementInfo(
                generation_capacity=cycle.generation_capacity,
                consumption=cycle.consumption,
                generation_cost=cycle.generation_cost,
                valid_until=cycle.valid_until,
                received_at=cycle.created_at,
            )

        for event in cycle.events:
            if event.event_type == "transfer":
                payload = event.payload or {}
                transfers.append(
                    TransferInfo(
                        idpk=event.idpk,
                        msg_id=event.msg_id,
                        quantity=float(payload.get("quantity", event.delta_budget)),
                        penalty=float(payload["penalty"]) if payload.get("penalty") is not None else None,
                        because_of=payload.get("becauseOf"),
                        received_at=event.applied_at,
                    )
                )
            elif event.event_type == "demand-statement":
                payload = event.payload or {}
                bal = payload.get("balance", {})
                demand_statements.append(
                    DemandStatementInfo(
                        idpk=event.idpk,
                        msg_id=event.msg_id,
                        quantity=int(bal.get("quantity", event.delta_energy)),
                        value_per_kwh=float(bal.get("valuePerKwh", 0.0)),
                        delta_budget=event.delta_budget,
                        delta_energy=event.delta_energy,
                        applied_at=event.applied_at,
                    )
                )

            # Última operación aplicada
            if cycle.last_operation_idpk and event.idpk == cycle.last_operation_idpk:
                last_op = LastOperationInfo(
                    idpk=event.idpk,
                    type=event.event_type,
                    applied_at=event.applied_at,
                    description=event.description,
                )

        # Si no encontramos el evento exacto pero hay eventos, tomamos el último
        if last_op is None and cycle.events:
            latest = cycle.events[-1]
            last_op = LastOperationInfo(
                idpk=latest.idpk,
                type=latest.event_type,
                applied_at=latest.applied_at,
                description=latest.description,
            )

        negotiations = [
            VoluntaryNegotiationInfo(
                idpk=neg.proposal_idpk,
                direction=neg.direction,
                quantity=neg.quantity,
                price_per_energy=neg.price_per_energy,
                status=neg.status,
                updated_at=neg.updated_at,
            )
            for neg in cycle.negotiations
        ]

        report_info: NegotiationReportInfo | None = None
        if cycle.is_closed and cycle.report_budget_balance is not None:
            report_info = NegotiationReportInfo(
                budget_balance=cycle.report_budget_balance,
                energy_balance=cycle.report_energy_balance or 0,
                submitted_at=cycle.report_submitted_at or cycle.updated_at,
            )

        # Construir operaciones para la vista del frontend (CycleHistory.jsx)
        operations: list[OperationItem] = []
        for event in cycle.events:
            is_last = (event.idpk == cycle.last_operation_idpk)
            operations.append(
                OperationItem(
                    idpk=event.idpk,
                    kind=event.event_type,
                    is_last=is_last,
                    applied_at=event.applied_at,
                )
            )

        if operations and not any(op.is_last for op in operations):
            operations[-1].is_last = True

        balances_obj = BalancesInfo(
            budget=cycle.budget_balance,
            energy=cycle.energy_balance,
        )

        return CycleDetail(
            cycle_id=cycle.cycle_id,
            opened=not cycle.is_closed,
            is_closed=cycle.is_closed,
            balances=balances_obj,
            final_balances=balances_obj,
            energy_balance=cycle.energy_balance,
            budget_balance=cycle.budget_balance,
            status_statement=status_info,
            transfers=transfers,
            total_transferred_budget=cycle.total_transferred_budget,
            demand_statements=demand_statements,
            negotiations=negotiations,
            voluntary_negotiations=negotiations,
            operations=operations,
            last_operation_idpk=cycle.last_operation_idpk or (operations[-1].idpk if operations else None),
            last_operation=last_op,
            report=report_info,
            negotiation_report=report_info,
            report_error=False,
            created_at=cycle.created_at,
            updated_at=cycle.updated_at,
        )

    async def list_cycles(self, page: int = 1, limit: int = 25) -> PaginatedCycles:
        """Lista ciclos paginados ordenados por created_at descendente (RF01 y CycleHistory.jsx)."""
        offset = (page - 1) * limit
        total_stmt = select(func.count()).select_from(CycleLedger)
        total = (await self.session.execute(total_stmt)).scalar_one()

        cycles_stmt = (
            select(CycleLedger)
            .order_by(desc(CycleLedger.created_at))
            .offset(offset)
            .limit(limit)
        )
        cycles = (await self.session.execute(cycles_stmt)).scalars().all()

        items: list[CycleSummary] = [
            CycleSummary(
                cycle_id=c.cycle_id,
                opened=not c.is_closed,
                is_closed=c.is_closed,
                energy_balance=c.energy_balance,
                budget_balance=c.budget_balance,
                final_balances=BalancesInfo(budget=c.budget_balance, energy=c.energy_balance),
                last_operation_idpk=c.last_operation_idpk,
                created_at=c.created_at,
                updated_at=c.updated_at,
            )
            for c in cycles
        ]

        pages = (total + limit - 1) // limit if limit > 0 else 1
        return PaginatedCycles(
            items=items,
            page=page,
            limit=limit,
            total=total,
            pages=pages,
        )

    async def list_message_logs(
        self, page: int = 1, limit: int = 25, category: str | None = None
    ) -> PaginatedMessageLogs:
        """Lista el registro de mensajes duplicados, descartados o NACK para Messages.jsx (RF05)."""
        offset = (page - 1) * limit
        stmt = select(DuplicateMessage)

        if category:
            cat = category.lower()
            if cat == "duplicate":
                stmt = stmt.where(DuplicateMessage.reason == "DUPLICATE_IDPK")
            elif cat == "nack":
                stmt = stmt.where(DuplicateMessage.reason.ilike("%NACK%"))
            elif cat == "discarded":
                stmt = stmt.where(
                    DuplicateMessage.reason != "DUPLICATE_IDPK",
                    ~DuplicateMessage.reason.ilike("%NACK%"),
                )

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        items_stmt = stmt.order_by(desc(DuplicateMessage.detected_at)).offset(offset).limit(limit)
        rows = (await self.session.execute(items_stmt)).scalars().all()

        items: list[MessageLogItem] = []
        for r in rows:
            if r.reason == "DUPLICATE_IDPK":
                cat_label = "duplicate"
            elif "NACK" in (r.reason or "").upper():
                cat_label = "nack"
            else:
                cat_label = "discarded"

            items.append(
                MessageLogItem(
                    id=r.id,
                    category=cat_label,
                    message_type=r.event_type,
                    idpk=r.idpk,
                    reason=r.reason,
                    received_at=r.detected_at,
                )
            )

        pages = (total + limit - 1) // limit if limit > 0 else 1
        return PaginatedMessageLogs(
            items=items,
            page=page,
            limit=limit,
            total=total,
            pages=pages,
        )

    async def reconstruct_cycle_from_events(self, cycle_id: str) -> dict[str, Any]:
        """Reconstruye el estado contable de un ciclo desde sus eventos (Propiedad AD2).

        Demuestra que el estado es 100% explicable y reproducible a partir del Event Log.
        """
        stmt = (
            select(LedgerEvent)
            .where(LedgerEvent.cycle_id == cycle_id)
            .order_by(LedgerEvent.applied_at.asc())
        )
        events = (await self.session.execute(stmt)).scalars().all()

        accumulated_budget_delta = sum(e.delta_budget for e in events)
        accumulated_energy_delta = sum(e.delta_energy for e in events)

        return {
            "cycle_id": cycle_id,
            "events_count": len(events),
            "reconstructed_budget_delta": accumulated_budget_delta,
            "reconstructed_energy_balance": accumulated_energy_delta,
            "events": [
                {
                    "idpk": e.idpk,
                    "type": e.event_type,
                    "delta_budget": e.delta_budget,
                    "delta_energy": e.delta_energy,
                    "resulting_budget": e.resulting_budget,
                    "resulting_energy": e.resulting_energy,
                    "applied_at": e.applied_at.isoformat(),
                }
                for e in events
            ],
        }
