from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.repository import get_all_routes, get_audit_logs
from app.schemas import (
    AuditLogListResponse,
    AuditLogResponse,
    ConnectivityResponse,
    RouteConnectivity,
)

router = APIRouter(tags=["connectivity-and-audit"])


@router.get("/connectivity", response_model=ConnectivityResponse)
async def get_connectivity(session: AsyncSession = Depends(get_session)):
    """RF02: Vista de conectividad con la tabla de distancias y costos de transporte"""
    entries = await get_all_routes(session)
    routes = [
        RouteConnectivity(
            destination_code=e.destination_code,
            distance=e.distance,
            transport_cost=e.transport_cost,
            enabled=e.enabled,
            updated_at=e.updated_at,
        )
        for e in entries
    ]
    return ConnectivityResponse(routes=routes)


@router.get("/audit/messages", response_model=AuditLogListResponse)
async def get_audit_messages(
    category: Optional[str] = Query(
        None, description="Filtrar por: duplicate, discarded, nack]"),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    """RF05: Realizar consultas de duplicados, descartados y NACKs"""
    offset = (page - 1) * limit
    items, total = await get_audit_logs(session, category=category, limit=limit, offset=offset)

    return AuditLogListResponse(
        items=[AuditLogResponse.model_validate(i) for i in items],
        total=total,
        page=page,
        limit=limit,
    )
