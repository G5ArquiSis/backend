"""Tests unitarios del registro de mensajes no aplicados (RF05, Tarea A7)."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cycle_repository import (
    _build_message_log_filters,
    count_message_log,
    get_message_log_counts,
    list_message_log,
    record_unapplied_message,
)
from app.cycle_schemas import (
    MessageLogCounts,
    MessageLogEntry,
    MessageLogPage,
    UnappliedMessageIn,
)
from app.models import MessageLog


def test_message_log_schema_with_sender_and_counts() -> None:
    """Verifica que MessageLogEntry y MessageLogPage incluyan los campos de ciudad, usuario y contadores."""
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
    entry = MessageLogEntry(
        id=1,
        category="nack",
        idpk="idpk-100",
        msg_id="msg-100",
        message_type="bid",
        cycle_id="cycle-1",
        reason="IDENTITY_MISMATCH",
        sender_city="COR",
        sender_user_id="city.ATTACKER",
        raw_content='{"type": "bid"}',
        received_at=now,
    )

    data = entry.model_dump(by_alias=True)
    assert data["senderCity"] == "COR"
    assert data["senderUserId"] == "city.ATTACKER"
    assert data["reason"] == "IDENTITY_MISMATCH"

    counts = MessageLogCounts(total=10, duplicate=3, discarded=5, nack=2)
    page = MessageLogPage(items=[entry], page=1, limit=25, total=1, pages=1, counts=counts)
    page_data = page.model_dump(by_alias=True)
    assert page_data["counts"]["nack"] == 2
    assert page_data["counts"]["total"] == 10


def test_build_message_log_filters_generates_expected_conditions() -> None:
    """Verifica la generación de filtros dinámicos para categoría, ciudad, tipo y fechas."""
    now = datetime.now(UTC)
    filters = _build_message_log_filters(
        category="discarded",
        city_id="cor",
        message_type="bid",
        from_date=now,
        to_date=now,
    )

    # Debe contener 5 condiciones
    assert len(filters) == 5


@pytest.mark.asyncio
async def test_record_unapplied_message_extracts_sender_and_user_id() -> None:
    """Verifica que record_unapplied_message extraiga la ciudad y user_id de envelope o argumentos."""
    mock_session = AsyncMock()

    body = {
        "idpk": "bid-999",
        "msgId": "msg-999",
        "type": "bid",
        "cityId": "COR",
        "cycleId": "c-10",
        "data": {"offerId": "TAL-1"},
    }
    raw_text = json.dumps(body)

    entry = await record_unapplied_message(
        mock_session,
        category="nack",
        raw_content=raw_text,
        reason="IDENTITY_MISMATCH",
        sender_user_id="city.ATTACKER",
    )

    assert entry.category == "nack"
    assert entry.idpk == "bid-999"
    assert entry.sender_city == "COR"
    assert entry.sender_user_id == "city.ATTACKER"
    assert entry.reason == "IDENTITY_MISMATCH"
    assert mock_session.add.called
    assert mock_session.commit.called


@pytest.mark.asyncio
async def test_get_message_log_counts_aggregates_categories() -> None:
    """Verifica que get_message_log_counts arme el diccionario con total y por categoría."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = [("duplicate", 4), ("nack", 2), ("discarded", 1)]
    mock_session.execute.return_value = mock_result

    counts = await get_message_log_counts(mock_session)
    assert counts["total"] == 7
    assert counts["duplicate"] == 4
    assert counts["nack"] == 2
    assert counts["discarded"] == 1
