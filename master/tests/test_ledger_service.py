"""Tests unitarios y de integración de la lógica de negocio del Ledger (RF03 / RF05 / AD2).

Verifica la aplicación de las reglas contables con fixtures:
- status-statement fija capacidad, consumo y costo base.
- transfer acredita fondos al presupuesto.
- demand-statement aplica convención de signos (+energy/-budget vs -energy/+budget).
- Idempotencia: reenvío con mismo idpk no altera dos veces los balances y queda registrado (RF05).
- Traspaso entre ciclos: presupuesto se transfiere, energía sobrante se pierde.
- Reconstruibilidad: el estado se explica y reproduce 100% desde los eventos.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ledger_service import LedgerService
from app.models import CycleLedger, DuplicateMessage, LedgerEvent

pytestmark = pytest.mark.integration

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Carga un archivo JSON de fixture."""
    with open(FIXTURES_DIR / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


async def test_apply_status_statement(session: AsyncSession) -> None:
    """status-statement inicializa el ciclo con capacidad, consumo y costo base."""
    fixture = load_fixture("status_statement")
    service = LedgerService(session)

    result = await service.process_message(
        idpk=fixture["idpk"],
        msg_id=fixture["msgId"],
        event_type=fixture["type"],
        cycle_id=fixture["cycleId"],
        data=fixture["data"],
        timestamp=fixture["timestamp"],
    )

    assert result.is_duplicate is False
    assert result.cycle_id == "cycle-100"
    assert result.budget_balance == 0.0
    assert result.energy_balance == 0

    # Verificar en la base de datos
    cycle = await service.get_cycle("cycle-100")
    assert cycle is not None
    assert cycle.generation_capacity == 1200000
    assert cycle.consumption == 1400000
    assert cycle.generation_cost == 200.0
    assert cycle.last_operation_idpk == fixture["idpk"]


async def test_apply_transfer(session: AsyncSession) -> None:
    """transfer suma fondos directamente al presupuesto del ciclo."""
    # Primero inicializamos el ciclo
    stat_fixture = load_fixture("status_statement")
    trans_fixture = load_fixture("transfer")
    service = LedgerService(session)

    await service.process_message(
        idpk=stat_fixture["idpk"],
        msg_id=stat_fixture["msgId"],
        event_type=stat_fixture["type"],
        cycle_id=stat_fixture["cycleId"],
        data=stat_fixture["data"],
    )

    result = await service.process_message(
        idpk=trans_fixture["idpk"],
        msg_id=trans_fixture["msgId"],
        event_type=trans_fixture["type"],
        cycle_id=trans_fixture["cycleId"],
        data=trans_fixture["data"],
    )

    assert result.is_duplicate is False
    assert result.delta_budget == 500000.0
    assert result.delta_energy == 0
    assert result.budget_balance == 500000.0
    assert result.energy_balance == 0

    cycle = await service.get_cycle("cycle-100")
    assert cycle is not None
    assert cycle.budget_balance == 500000.0
    assert cycle.total_transferred_budget == 500000.0
    assert cycle.last_operation_idpk == trans_fixture["idpk"]


async def test_apply_demand_statement_positive_quantity(session: AsyncSession) -> None:
    """demand-statement con quantity > 0: central entrega energía -> +energía, -presupuesto."""
    stat_fixture = load_fixture("status_statement")
    trans_fixture = load_fixture("transfer")
    dem_pos = load_fixture("demand_statement_positive")
    service = LedgerService(session)

    await service.process_message(
        idpk=stat_fixture["idpk"],
        msg_id=stat_fixture["msgId"],
        event_type=stat_fixture["type"],
        cycle_id=stat_fixture["cycleId"],
        data=stat_fixture["data"],
    )
    await service.process_message(
        idpk=trans_fixture["idpk"],
        msg_id=trans_fixture["msgId"],
        event_type=trans_fixture["type"],
        cycle_id=trans_fixture["cycleId"],
        data=trans_fixture["data"],
    )

    # quantity = 1000 kWh, valuePerKwh = 200.0 -> +1000 kWh, -(1000*200) = -200,000 créditos
    result = await service.process_message(
        idpk=dem_pos["idpk"],
        msg_id=dem_pos["msgId"],
        event_type=dem_pos["type"],
        cycle_id=dem_pos["cycleId"],
        data=dem_pos["data"],
    )

    assert result.is_duplicate is False
    assert result.delta_energy == 1000
    assert result.delta_budget == -200000.0
    assert result.energy_balance == 1000
    assert result.budget_balance == 300000.0  # 500,000 - 200,000

    cycle = await service.get_cycle("cycle-100")
    assert cycle is not None
    assert cycle.energy_balance == 1000
    assert cycle.budget_balance == 300000.0


async def test_apply_demand_statement_negative_quantity(session: AsyncSession) -> None:
    """demand-statement con quantity < 0: central retira energía -> -energía, +presupuesto."""
    dem_neg = load_fixture("demand_statement_negative")
    service = LedgerService(session)

    # Creamos un ciclo con balance previo de energía
    cycle = CycleLedger(
        cycle_id="cycle-100",
        budget_balance=300000.0,
        energy_balance=1000,
    )
    session.add(cycle)
    await session.commit()

    # quantity = -500 kWh, valuePerKwh = 210.0 -> -500 kWh, +(-(-500)*210) = +105,000 créditos
    result = await service.process_message(
        idpk=dem_neg["idpk"],
        msg_id=dem_neg["msgId"],
        event_type=dem_neg["type"],
        cycle_id=dem_neg["cycleId"],
        data=dem_neg["data"],
    )

    assert result.is_duplicate is False
    assert result.delta_energy == -500
    assert result.delta_budget == 105000.0
    assert result.energy_balance == 500  # 1000 - 500
    assert result.budget_balance == 405000.0  # 300,000 + 105,000


async def test_idempotency_duplicate_idpk_does_not_modify_ledger(session: AsyncSession) -> None:
    """Reenvío de mensaje con el mismo idpk no altera dos veces los balances y registra duplicado (RF05 / Anomalía 1)."""
    dem_pos = load_fixture("demand_statement_positive")
    service = LedgerService(session)

    # 1. Primer envío
    first_result = await service.process_message(
        idpk=dem_pos["idpk"],
        msg_id=dem_pos["msgId"],
        event_type=dem_pos["type"],
        cycle_id=dem_pos["cycleId"],
        data=dem_pos["data"],
    )
    assert first_result.is_duplicate is False
    initial_budget = first_result.budget_balance
    initial_energy = first_result.energy_balance

    # 2. Segundo envío (mismo idpk, posible msgId diferente)
    second_result = await service.process_message(
        idpk=dem_pos["idpk"],
        msg_id="new-retry-msg-id-999",
        event_type=dem_pos["type"],
        cycle_id=dem_pos["cycleId"],
        data=dem_pos["data"],
    )

    # El servicio detecta el duplicado
    assert second_result.is_duplicate is True
    # Los balances se mantienen exactamente iguales
    assert second_result.budget_balance == initial_budget
    assert second_result.energy_balance == initial_energy
    assert second_result.delta_budget == 0.0
    assert second_result.delta_energy == 0

    # Verificar que el ledger en la base de datos no cambió
    cycle = await service.get_cycle("cycle-100")
    assert cycle is not None
    assert cycle.budget_balance == initial_budget
    assert cycle.energy_balance == initial_energy

    # Verificar que se registró en duplicate_messages (RF05)
    dups_query = await session.execute(
        select(DuplicateMessage).where(DuplicateMessage.idpk == dem_pos["idpk"])
    )
    dups = dups_query.scalars().all()
    assert len(dups) == 1
    assert dups[0].reason == "DUPLICATE_IDPK"
    assert dups[0].msg_id == "new-retry-msg-id-999"


async def test_cycle_rollover_budget_transfers_and_energy_resets(session: AsyncSession) -> None:
    """Traspaso de ciclo: el presupuesto remanente se hereda, la energía sobrante se pierde (0)."""
    service = LedgerService(session)

    # Ciclo 1 finaliza con 405,000 créditos y 500 kWh de excedente no vendido
    cycle1 = CycleLedger(
        cycle_id="cycle-100",
        budget_balance=405000.0,
        energy_balance=500,
        is_closed=True,
    )
    session.add(cycle1)
    await session.commit()

    # Llega un mensaje para el siguiente ciclo: cycle-101
    result = await service.process_message(
        idpk="new-cycle-stat-idpk",
        msg_id="msg-new-001",
        event_type="status-statement",
        cycle_id="cycle-101",
        data={
            "energy": {
                "generationCapacity": 1000000,
                "consumption": 900000,
                "generationCost": 190.0,
            }
        },
    )

    assert result.cycle_id == "cycle-101"
    # Presupuesto heredado del ciclo anterior:
    assert result.budget_balance == 405000.0
    # Energía reinicia en 0 (el excedente anterior no se almacena):
    assert result.energy_balance == 0

    cycle2 = await service.get_cycle("cycle-101")
    assert cycle2 is not None
    assert cycle2.budget_balance == 405000.0
    assert cycle2.energy_balance == 0


async def test_reconstruct_cycle_from_events(session: AsyncSession) -> None:
    """El estado del ciclo es 100% explicable y reproducible desde los eventos (Propiedad AD2)."""
    service = LedgerService(session)

    # Aplicamos status, transfer y demand-statement sobre cycle-200
    await service.process_message(
        idpk="idpk-stat-200",
        msg_id="msg-stat-200",
        event_type="status-statement",
        cycle_id="cycle-200",
        data={"energy": {"generationCapacity": 1000, "consumption": 1000, "generationCost": 200}},
    )
    await service.process_message(
        idpk="idpk-trans-200",
        msg_id="msg-trans-200",
        event_type="transfer",
        cycle_id="cycle-200",
        data={"quantity": 100000.0},
    )
    await service.process_message(
        idpk="idpk-dem-200",
        msg_id="msg-dem-200",
        event_type="demand-statement",
        cycle_id="cycle-200",
        data={"balance": {"quantity": 200, "valuePerKwh": 200.0}},
    )

    # Reconstrucción
    recon = await service.reconstruct_cycle_from_events("cycle-200")

    assert recon["cycle_id"] == "cycle-200"
    assert recon["events_count"] == 3
    # 0 (stat) + 100,000 (trans) - (200*200 = 40,000) (dem) = 60,000
    assert recon["reconstructed_budget_delta"] == 60000.0
    # 0 + 0 + 200 = 200 kWh
    assert recon["reconstructed_energy_balance"] == 200

    cycle = await service.get_cycle("cycle-200")
    assert cycle is not None
    assert cycle.budget_balance == recon["reconstructed_budget_delta"]
    assert cycle.energy_balance == recon["reconstructed_energy_balance"]
