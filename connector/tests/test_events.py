"""Tests de la validación del envelope v2 y del armado de ACK y NACK."""

import json

from support import as_body, central_message

from events import construir_mensaje_ack, validacion_mensaje_entrante


def test_valid_envelope_is_returned_as_is() -> None:
    message, nack, discard = validacion_mensaje_entrante(as_body(central_message()))

    assert (nack, discard) == (None, None)
    assert message["msgId"] == "msg-1"


def test_unparseable_body_is_discarded_without_nack() -> None:
    """No hay mensaje válido al cual responder."""
    message, nack, discard = validacion_mensaje_entrante(b"{esto no es json")

    assert (message, nack) == (None, None)
    assert "JSON no parseable" in discard


def test_message_without_msg_id_is_discarded_without_nack() -> None:
    message, nack, discard = validacion_mensaje_entrante(json.dumps({"foo": "bar"}).encode())

    assert (message, nack) == (None, None)
    assert discard == "El mensaje no incluye msgId"


def test_missing_envelope_field_gets_a_malformed_nack() -> None:
    incomplete = central_message()
    del incomplete["timestamp"]

    message, nack, discard = validacion_mensaje_entrante(as_body(incomplete), "TAL")

    assert (message, discard) == (None, None)
    assert (nack["type"], nack["reason"], nack["code"]) == ("nack", "MALFORMED_MESSAGE", 422)
    assert nack["data"]["target"] == "msg-1"
    assert nack["data"]["cycleId"] == "cycle-9431"
    assert nack["cityId"] == "TAL"


def test_idpk_equal_to_msg_id_gets_its_own_nack() -> None:
    _, nack, _ = validacion_mensaje_entrante(as_body(central_message(idpk="msg-1")))

    assert (nack["reason"], nack["code"]) == ("IDPK_EQUALS_MSGID", 422)


def test_unknown_type_gets_a_400_nack() -> None:
    _, nack, _ = validacion_mensaje_entrante(as_body(central_message("tipo-invalido")))

    assert (nack["reason"], nack["code"]) == ("UNKNOWN_TYPE", 400)


def test_nack_has_fresh_ids_and_never_reuses_the_original_msg_id() -> None:
    _, nack, _ = validacion_mensaje_entrante(as_body(central_message(idpk="msg-1")))

    assert nack["msgId"] != "msg-1"
    assert nack["idpk"] != nack["msgId"]


def test_ack_targets_the_received_message() -> None:
    ack = construir_mensaje_ack("msg-1", "TAL")

    assert ack["type"] == "ack"
    assert ack["data"] == {"target": "msg-1"}
    assert ack["cityId"] == "TAL"
    assert ack["idpk"] != ack["msgId"]
    assert ack["msgId"] != "msg-1"
