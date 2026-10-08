from typing import Any, Dict
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.repository import save_audit_log, upsert_distance_table
from app.schemas import AuditLogCreate

router = APIRouter(prefix="/interno", tags=["interno"])


@router.post("/logs")
async def create_audit_log(
    payload: AuditLogCreate,
    session: AsyncSession = Depends(get_session),
):
    """RF05: Registrar los mensaje descartados, duplicados o respondidos con NACK en el log"""
    log_entry = await save_audit_log(session, payload)
    return {"status": "ok", "id": log_entry.id}


@router.post("/eventos")
async def receive_event(
    event: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
):

    event_type = event.get("type")

    if event_type == "distance-table":
        data = event.get("data", {})
        distances = data.get("distances", {})
        await upsert_distance_table(session, distances)
        return {"status": "processed", "type": "distance-table"}

    return {"status": "accepted"}
