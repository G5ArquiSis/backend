"""Prueba de verificación para la Anomalía 1 de la Demo evaluada con el ayudante.

Anomalía 1:
- Reenvío duplicado de un mensaje sensible (mismo idpk)
- Resultado esperado:
  1. El ledger NO se altera dos veces (idempotencia contable).
  2. El duplicado queda registrado y consultable en /message-log (RF05).
  3. El conector recibe 200 OK para confirmar el ACK sin re-ejecutar.
"""

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CycleLedger, DuplicateMessage, LedgerEvent

pytestmark = pytest.mark.integration


async def test_demo_anomalia_1_duplicate_message_flow(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Escenario completo de la Anomalía 1 ejecutado en vivo en la demo."""
    cycle_id = "cycle-demo-anomalia"
    sensitive_idpk = "demo-idpk-transaccion-sensible-001"

    # 1. Crear el ciclo base con status-statement
    stat_msg = {
        "idpk": "stat-init-idpk",
        "msgId": "stat-msg-1",
        "type": "status-statement",
        "cycleId": cycle_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": {
            "energy": {
                "generationCapacity": 1000000,
                "consumption": 800000,
                "generationCost": 200.0,
            }
        },
    }
    stat_resp = await client.post("/ledger/messages", json=stat_msg)
    assert stat_resp.status_code == 201

    # Fondos iniciales vía transfer
    transfer_msg = {
        "idpk": "trans-init-idpk",
        "msgId": "trans-msg-1",
        "type": "transfer",
        "cycleId": cycle_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": {"quantity": 500000.0},
    }
    trans_resp = await client.post("/ledger/messages", json=transfer_msg)
    assert trans_resp.status_code == 201

    # Verificamos estado previo: budget=500000, energy=0
    cycle_before = await client.get(f"/cycles/{cycle_id}")
    assert cycle_before.status_code == 200
    assert cycle_before.json()["balances"]["budget"] == 500000.0
    assert cycle_before.json()["balances"]["energy"] == 0

    # -------------------------------------------------------------
    # PASO 1 DE LA ANOMALÍA: Enviar mensaje sensible por primera vez
    # -------------------------------------------------------------
    sensitive_demand = {
        "idpk": sensitive_idpk,
        "msgId": "msg-original-demand-001",
        "type": "demand-statement",
        "cycleId": cycle_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": {
            "balance": {
                "quantity": 1000,       # +1000 kWh
                "valuePerKwh": 200.0,    # 1000 * 200 = -200,000 créditos
            }
        },
    }

    first_post = await client.post("/ledger/messages", json=sensitive_demand)
    assert first_post.status_code == 201
    first_data = first_post.json()
    assert first_data["isDuplicate"] is False
    assert first_data["budgetBalance"] == 300000.0  # 500,000 - 200,000
    assert first_data["energyBalance"] == 1000

    # -------------------------------------------------------------
    # PASO 2 DE LA ANOMALÍA: Reenvío intencional del mismo idpk
    # (Simulando reintento por timeout o duplicación de red del broker)
    # -------------------------------------------------------------
    retry_sensitive_demand = {
        "idpk": sensitive_idpk,                  # MISMO IDPK
        "msgId": "msg-retry-uuid-distinto-999",  # msgId nuevo
        "type": "demand-statement",
        "cycleId": cycle_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": {
            "balance": {
                "quantity": 1000,
                "valuePerKwh": 200.0,
            }
        },
    }

    retry_post = await client.post("/ledger/messages", json=retry_sensitive_demand)

    # ASERCIÓN 1: El backend responde HTTP 200 (éxito para hacer ACK en RabbitMQ)
    assert retry_post.status_code == 200
    retry_data = retry_post.json()
    # ASERCIÓN 2: Se marca explícitamente como duplicado
    assert retry_data["isDuplicate"] is True
    # ASERCIÓN 3: Los deltas son 0
    assert retry_data["deltaBudget"] == 0.0
    assert retry_data["deltaEnergy"] == 0

    # -------------------------------------------------------------
    # PASO 3 DE LA ANOMALÍA: Verificar que el ledger NO cambió
    # -------------------------------------------------------------
    cycle_after = await client.get(f"/cycles/{cycle_id}")
    assert cycle_after.status_code == 200
    detail = cycle_after.json()

    # El presupuesto sigue siendo 300,000 (NO se descontaron 200,000 dos veces a 100,000)
    assert detail["balances"]["budget"] == 300000.0
    # La energía sigue siendo 1,000 (NO se sumaron 1,000 dos veces a 2,000)
    assert detail["balances"]["energy"] == 1000
    # Solo debe haber 3 operaciones en la lista histórica (status, transfer, 1 demand)
    assert len(detail["demandStatements"]) == 1

    # -------------------------------------------------------------
    # PASO 4 DE LA ANOMALÍA: Verificar que el duplicado quedó en /message-log (RF05)
    # -------------------------------------------------------------
    log_resp = await client.get("/message-log?category=duplicate")
    assert log_resp.status_code == 200
    log_data = log_resp.json()
    assert log_data["total"] >= 1

    # El idpk sensible aparece registrado en la lista de duplicados
    matching_dups = [item for item in log_data["items"] if item["idpk"] == sensitive_idpk]
    assert len(matching_dups) == 1
    dup_entry = matching_dups[0]
    assert dup_entry["category"] == "duplicate"
    assert dup_entry["reason"] == "DUPLICATE_IDPK"
    assert dup_entry["messageType"] == "demand-statement"
