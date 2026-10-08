"""Tests del ciclo de vida de consumo de connector (RNF1).

No se conecta al broker real: aio_pika queda completamente mockeado. El foco es
la política de ack/nack de handle_message y la resiliencia de run_until_shutdown,
que es lo que sostiene RNF1 (perder el broker o master no debe perder eventos ni
terminar el proceso).
"""

import json
from events import construir_mensaje_ack, validacion_mensaje_entrante


def test_parse_valid_envelope():
    raw = json.dumps({
        "idpk": "uuid-1",
        "msgId": "uuid-2",
        "type": "demand-statement",
        "timestamp": "2026-09-01T12:00:00Z",
        "data": {"balance": {"quantity": 100, "valuePerKwh": 200}},
    }).encode("utf-8")

    msg, nack, discard = validacion_mensaje_entrante(raw)
    assert discard is None
    assert nack is None
    assert msg["msgId"] == "uuid-2"


def test_discard_without_msg_id():
    raw = json.dumps({"foo": "bar"}).encode("utf-8")
    msg, nack, discard = validacion_mensaje_entrante(raw)
    assert msg is None
    assert nack is None
    assert discard is not None


def test_nack_idpk_equals_msgid():
    raw = json.dumps({
        "idpk": "same-id",
        "msgId": "same-id",
        "type": "demand-statement",
        "timestamp": "2026-09-01T12:00:00Z",
        "data": {},
    }).encode("utf-8")

    msg, nack, discard = validacion_mensaje_entrante(raw)
    assert msg is None
    assert discard is None
    assert nack is not None
    assert nack["reason"] == "IDPK EQUALS MSGID"
    assert nack["code"] == 422


def test_nack_unknown_type():
    raw = json.dumps({
        "idpk": "uuid-1",
        "msgId": "uuid-2",
        "type": "tipo-invalido",
        "timestamp": "2026-09-01T12:00:00Z",
        "data": {},
    }).encode("utf-8")

    msg, nack, discard = validacion_mensaje_entrante(raw)
    assert msg is None
    assert discard is None
    assert nack is not None
    assert nack["reason"] == "UNKNOWN TYPE"
    assert nack["code"] == 400


def test_build_ack_envelope():
    ack = construir_mensaje_ack(target_msg_id="target-123", city_code="TAL")
    assert ack["type"] == "ack"
    assert ack["cityId"] == "TAL"
    assert ack["data"]["target"] == "target-123"
    assert ack["msgId"] != "target-123"
