"""Test end-to-end del contrato entre connector y master, contra Postgres real.

No es una repetición de master/tests/test_history_integration.py: ese archivo
prueba invariantes de la capa de datos (idempotencia, filtros, paginación) una
por una. Este simula el camino completo que connector recorre en producción -
recibir un evento del broker, reenviarlo por HTTP, y que quede consultable en el
historial - con un único payload de punta a punta, incluidos los campos que
connector no toca (received_at, generado por el servidor) y los que sí
transforma (el array `demands` anidado en packageBody).

Usa las fixtures de master/tests/conftest.py (client, session, database) en vez
de duplicarlas: son la misma app y la misma base, solo que aquí se ejercitan
como lo haría connector y no como pruebas unitarias de cada invariante.
"""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession


def connector_payload() -> dict:
    """El JSON que connector.master_client.MasterClient.publish_event envía.

    Con las mismas claves camelCase y la misma forma anidada que
    DemandEventMessage.model_dump(mode="json", by_alias=True) produce en
    connector/events.py - el otro lado del contrato duplicado a propósito
    (CLAUDE.md seccion 2).
    """
    return {
        "idpk": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        "type": "demand-set",
        "packageBody": {
            "demands": [
                {"city": "Los Santos", "demand": 1013.123, "unit": "GW"},
                {"city": "New New York", "demand": 842.5, "unit": "GW"},
            ],
            "validUntil": "2026-12-12T00:00:00Z",
            "metaContent": "lectura horaria del observer",
            "constraints": {"maxDemand": 2000},
        },
    }


async def test_event_posted_like_connector_appears_complete_in_the_history(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """POST /events con un payload tipo connector, luego GET /history lo expone completo (RF1)."""
    posted = await client.post("/events", json=connector_payload())
    assert posted.status_code == 201
    event_id = posted.json()["id"]

    history = (await client.get("/history")).json()

    assert history["total"] == 1
    listed = history["items"][0]
    assert listed["id"] == event_id
    assert listed["idpk"] == "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    assert listed["receivedAt"] is not None
    assert listed["packageBody"]["metaContent"] == "lectura horaria del observer"
    assert listed["packageBody"]["constraints"] == {"maxDemand": 2000}
    assert [item["city"] for item in listed["packageBody"]["demands"]] == [
        "Los Santos",
        "New New York",
    ]


async def test_event_posted_like_connector_is_reachable_by_its_generated_id(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """RF2: el detalle por id devuelve el mismo evento que quedó en el historial."""
    posted = await client.post("/events", json=connector_payload())
    event_id = posted.json()["id"]

    detail = await client.get(f"/history/{event_id}")

    assert detail.status_code == 200
    assert detail.json()["idpk"] == "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    assert detail.json()["packageBody"]["demands"][1]["unit"] == "GW"
