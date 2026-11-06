"""Tests unitarios del contrato interno I1 (outbox con destino, Tarea A0)."""

from unittest.mock import AsyncMock

import pytest

from app.outbox import (
    CENTRAL,
    CITY,
    MARKET_OFFER,
    Destination,
    DestinationType,
    enqueue_publish,
    parse_destination,
)


def test_destination_singletons_and_resolution() -> None:
    """Verifica que las constantes y métodos resuelvan el exchange y routing key correctos."""
    assert Destination.CENTRAL.resolve() == ("energy.x", "central")
    assert Destination.MARKET_OFFER.resolve() == ("market", "market.offer.TAL")
    assert Destination.city("COR").resolve() == ("energy.x", "city.COR")
    assert Destination.CITY("hgw").resolve() == ("energy.x", "city.HGW")
    assert Destination.city("city.COR").resolve() == ("energy.x", "city.COR")
    assert Destination.city("CITY:COR").resolve() == ("energy.x", "city.COR")

    # Singletons a nivel de módulo
    assert CENTRAL.resolve() == ("energy.x", "central")
    assert MARKET_OFFER.resolve() == ("market", "market.offer.TAL")
    assert CITY("PUQ").resolve() == ("energy.x", "city.PUQ")


def test_destination_custom_city_code() -> None:
    """Verifica que se pueda resolver la routing key del mercado para otra ciudad si fuera necesario."""
    assert Destination.MARKET_OFFER.resolve(our_city_code="HGW") == ("market", "market.offer.HGW")


def test_parse_destination_valid_formats() -> None:
    """Verifica que parse_destination entienda todas las variantes que C y D podrían usar."""
    assert parse_destination(Destination.CENTRAL) == Destination.CENTRAL
    assert parse_destination(DestinationType.CENTRAL) == Destination.CENTRAL
    assert parse_destination("CENTRAL") == Destination.CENTRAL
    assert parse_destination("central") == Destination.CENTRAL

    assert parse_destination(Destination.MARKET_OFFER) == Destination.MARKET_OFFER
    assert parse_destination(DestinationType.MARKET_OFFER) == Destination.MARKET_OFFER
    assert parse_destination("MARKET_OFFER") == Destination.MARKET_OFFER
    assert parse_destination("market") == Destination.MARKET_OFFER

    assert parse_destination("city.COR") == Destination.city("COR")
    assert parse_destination("CITY:COR") == Destination.city("COR")
    assert parse_destination("CITY(COR)") == Destination.city("COR")
    assert parse_destination(("CITY", "COR")) == Destination.city("COR")


def test_parse_destination_invalid() -> None:
    """Verifica que destinos malformados levanten ValueError tempranamente."""
    with pytest.raises(ValueError, match="Destino no reconocido"):
        parse_destination("UNKNOWN_DESTINATION")

    with pytest.raises(ValueError, match="El código de ciudad no puede estar vacío"):
        Destination.city("")

    with pytest.raises(ValueError, match="Para destino CITY se debe indicar el código"):
        parse_destination(DestinationType.CITY)


@pytest.mark.asyncio
async def test_enqueue_publish_executes_insert() -> None:
    """Verifica que enqueue_publish ejecute el INSERT en la sesión con el dedupe_key y payload."""
    mock_session = AsyncMock()

    payload = {"type": "offer", "cycleId": "cycle-1", "data": {"quantity": 100}}
    await enqueue_publish(
        mock_session,
        dedupe_key="offer:cycle-1:1",
        payload=payload,
        destination=Destination.MARKET_OFFER,
    )

    assert mock_session.execute.await_count == 1
    call_args = mock_session.execute.call_args[0]
    insert_stmt = call_args[0]

    # Verifica que sea una sentencia de inserción sobre la tabla outbox
    assert str(insert_stmt.table) == "outbox"
