"""Contract tests (E4 de la E2, RDOC04): lo que el nodo publica cumple los schemas.

Cada mensaje que sale hacia el broker se valida contra el schema de su tipo en dos fuentes:
los del curso (`iic2173/energyshark-contracts` v3.0.2, lo que la central acepta) y los del
grupo (`G5ArquiSis/contratos`). Si los dos no coinciden con el código, el test falla. Las
rutas a los clones vienen de `ENERGYSHARK_CONTRACTS_DIR` y `GROUP_CONTRACTS_DIR`
(`tests/validador_contratos.py`); el CI las clona y fija `CONTRACTS_REQUIRED=1`.

Los mensajes no se arman a mano: salen del código real.

- connector: se le entrega un mensaje de la central con un canal de pika de mentira y se
  valida lo que publica (ACK, y NACK por cada motivo de rechazo).
- master: se recorre un ciclo contra Postgres y se valida todo lo que retira la outbox
  (reporte, peticiones directas, propuestas). Sin base, esa parte se salta.

Al agregar un tipo nuevo que el nodo publica (bid, offer, ...), basta con producirlo en uno de
estos escenarios y pasarlo por `assert_publishable`.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import validador_contratos as contratos
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# connector exige estas variables al construir su Settings (igual que connector/tests).
os.environ.setdefault("BROKER_HOST", "broker.test")
os.environ.setdefault("BROKER_USER", "observer")
os.environ.setdefault("BROKER_PASSWORD", "secret")
os.environ.setdefault("BROKER_QUEUE", "observer.test.q")
os.environ.setdefault("MASTER_EVENTS_URL", "http://master.test/events")
os.environ.setdefault("HEARTBEAT_PATH", "/tmp/connector-heartbeat-tests")

import consumer  # noqa: E402
from app.config import get_settings as master_settings  # noqa: E402
from app.cycle_repository import (  # noqa: E402
    apply_central_message,
    claim_outbox,
    create_negotiation,
    mark_outbox_sent,
    run_due_work,
)
from app.database import _session_factory  # noqa: E402
from config import get_settings as connector_settings  # noqa: E402
from master_client import MasterClient  # noqa: E402

# Tipos de la E1 que el protocolo v3 retira (schemas/common/retired-type.schema.json del curso):
# la central responde `error` `TYPE_RETIRED` si los recibe.
RETIRED = {"negotiation-proposal", "give", "take", "demand-statement"}


def assert_publishable(message: dict) -> None:
    """El mensaje cumple el schema de su tipo en el curso y en el grupo."""
    message_type = message["type"]
    assert message_type not in RETIRED, (
        f"{message_type} está retirado en el protocolo v{contratos.COURSE_VERSION}"
    )
    contratos.course().assert_valid(contratos.course_schema_for(message_type), message)
    contratos.group().assert_valid(contratos.group_schema_for(message_type), message)


# --- 1. El validador juzga igual que el del curso -----------------------------------------
#
# El curso sólo prueba sus ejemplos con Ajv. Si jsonschema aceptara un ejemplo inválido (por
# ejemplo, por ignorar `unevaluatedProperties` o un formato), los tests de abajo pasarían con
# mensajes que la central rechaza.


def _course_examples() -> list:
    root = os.environ.get(contratos.COURSE_ENV)
    if not root:
        return [pytest.param(None, None, None, id="sin-contratos-del-curso")]
    base = Path(root, "examples")
    cases = []
    for path in sorted(base.rglob("*.json")):
        relative = path.relative_to(base)
        *schema_parts, verdict, _name = relative.parts
        if verdict not in ("valid", "invalid"):
            continue
        schema = "/".join(schema_parts) + ".schema.json"
        cases.append(pytest.param(schema, verdict == "valid", path, id=str(relative)))
    return cases


def _pointer(pointer: str) -> list[str]:
    return [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]


def _mutate(base, mutation: dict):
    """Igual que `applyMutation` de scripts/lib/repo.mjs del curso: `set` y luego `unset`."""
    doc = copy.deepcopy(base)
    for pointer, value in mutation.get("set", {}).items():
        tokens = _pointer(pointer)
        if not tokens:
            doc = copy.deepcopy(value)
            continue
        parent = doc
        for token in tokens[:-1]:
            parent = parent[int(token)] if isinstance(parent, list) else parent[token]
        parent[tokens[-1]] = copy.deepcopy(value)
    for pointer in mutation.get("unset", []):
        *path, last = _pointer(pointer)
        parent = doc
        for token in path:
            parent = parent[int(token)] if isinstance(parent, list) else parent[token]
        if isinstance(parent, list):
            del parent[int(last)]
        else:
            del parent[last]
    return doc


def _all_errors(errors):
    """Ajv lista los errores de cada rama de anyOf/oneOf; jsonschema los anida en `context`."""
    for error in errors:
        yield error
        yield from _all_errors(error.context)


def _instance_path(error) -> str:
    return "".join(
        "/" + str(p).replace("~", "~0").replace("/", "~1") for p in error.absolute_path
    )


@pytest.mark.parametrize(("schema", "valid", "path"), _course_examples())
def test_validator_agrees_with_course_examples(schema, valid, path) -> None:
    """Válidos: sin errores. Inválidos (mutación de un válido): fallan con la palabra clave y
    en el `instancePath` que declaran, como exige test/examples.test.mjs del curso."""
    course = contratos.course()
    example = json.loads(path.read_text(encoding="utf-8"))
    if valid:
        errors = course.errors(schema, example)
        assert errors == [], [f"{_instance_path(e)}: {e.message}" for e in errors]
        return

    base = json.loads((path.parent / example["base"]).resolve().read_text(encoding="utf-8"))
    errors = course.errors(schema, _mutate(base, example))
    assert errors, f"{path.name} debía ser rechazado por {schema}"
    expected = (example["expect"]["keyword"], example["expect"]["instancePath"])
    found = {(e.validator, _instance_path(e)) for e in _all_errors(errors)}
    assert expected in found, f"se esperaba {expected}; jsonschema reporta {sorted(found)}"


# --- 2. Un mensaje inválido hace fallar el CI ---------------------------------------------


def _report(**data) -> dict:
    return {
        "idpk": str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": "negotiation-report",
        "timestamp": "2026-11-06T14:15:00Z",
        "cityId": "TAL",
        "cycleId": "cycle-1",
        "data": {"budgetBalance": 1200.5, "energyBalance": -3, **data},
    }


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param(lambda m: m["data"].update(budgetBalance="1200.50"), id="monto-como-texto"),
        pytest.param(lambda m: m.pop("cityId"), id="sin-cityId"),
        pytest.param(lambda m: m.update(msgId="msg-1"), id="msgId-no-uuid"),
        pytest.param(lambda m: m.update(timestamp="2026-11-06 14:15"), id="fecha-sin-zona"),
        pytest.param(lambda m: m["data"].pop("energyBalance"), id="sin-energyBalance"),
    ],
)
def test_invalid_message_is_rejected(broken) -> None:
    """Prueba de que el CI muerde: estos mensajes no pueden pasar `assert_publishable`."""
    message = _report()
    assert_publishable(message)

    broken(message)
    with pytest.raises(pytest.fail.Exception):
        assert_publishable(message)


def test_retired_type_is_rejected() -> None:
    message = _report()
    message["type"] = "negotiation-proposal"
    with pytest.raises(AssertionError, match="retirado"):
        assert_publishable(message)


# --- 3. connector: ACK y NACK -------------------------------------------------------------


class _Channel:
    """Canal de pika de mentira: guarda lo publicado."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    def basic_publish(self, exchange, routing_key, body, properties, mandatory=False) -> None:
        self.published.append(json.loads(body))

    def basic_ack(self, delivery_tag: int) -> None:
        pass

    def basic_nack(self, delivery_tag: int, requeue: bool = False) -> None:
        pass


def _central(message_type: str = "transfer", **overrides) -> dict:
    message = {
        "idpk": str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": message_type,
        "timestamp": "2026-11-06T14:00:00Z",
        "sender": "central",
        "cycleId": "cycle-1",
        "data": {"quantity": 1000},
    }
    message.update(overrides)
    return message


def _without(key: str) -> dict:
    message = _central()
    del message[key]
    return message


def _same_ids() -> dict:
    message = _central()
    message["idpk"] = message["msgId"]
    return message


def _published_by_connector(message: dict, master_status: int = 200) -> list[dict]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/messages" and master_status != 200:
            return httpx.Response(master_status, json={"detail": "contenido inválido"})
        return httpx.Response(200, json={"outcome": "applied"})

    master = MasterClient()
    master._client = httpx.Client(transport=httpx.MockTransport(handler))
    channel = _Channel()
    consumer.handle_city_delivery(
        channel, 1, json.dumps(message).encode(), master, connector_settings()
    )
    return channel.published


@pytest.mark.parametrize(
    ("message", "master_status", "expected"),
    [
        pytest.param(_central(), 200, ("ack", None), id="ack"),
        pytest.param(_without("idpk"), 200, ("nack", "MALFORMED_MESSAGE"), id="nack-sin-idpk"),
        pytest.param(_central(data=[]), 200, ("nack", "MALFORMED_MESSAGE"), id="nack-data"),
        pytest.param(_same_ids(), 200, ("nack", "IDPK_EQUALS_MSGID"), id="nack-idpk-msgid"),
        pytest.param(_central("swap-dance"), 200, ("nack", "UNKNOWN_TYPE"), id="nack-tipo"),
        pytest.param(_central(), 422, ("nack", "MALFORMED_MESSAGE"), id="nack-de-master"),
    ],
)
def test_connector_replies_follow_the_contract(message, master_status, expected) -> None:
    [reply] = _published_by_connector(message, master_status)
    assert (reply["type"], reply.get("reason")) == expected
    assert reply["data"]["target"] == message["msgId"]
    assert_publishable(reply)


# --- 4. master: lo que sale de la outbox --------------------------------------------------

SETTINGS = master_settings()
VALID_UNTIL = datetime(2026, 11, 6, 14, 20, tzinfo=UTC)
WINDOW_START = VALID_UNTIL - timedelta(minutes=20)
CLOSING = VALID_UNTIL - timedelta(minutes=4)
CYCLE = "cycle-e4"


@pytest.fixture
async def db(database: None) -> AsyncIterator[AsyncSession]:
    async with _session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE ledger_events, cycle_ledger, negotiations, outbox, message_log, "
                "distance_tables RESTART IDENTITY"
            )
        )
        await session.commit()
        yield session


def _status_statement() -> dict:
    return _central(
        "status-statement",
        cycleId=CYCLE,
        data={
            "energy": {"generationCapacity": 1000, "consumption": 400, "generationCost": 210},
            "validUntil": VALID_UNTIL.isoformat().replace("+00:00", "Z"),
        },
    )


async def _outbox_at(db: AsyncSession, now: datetime) -> list[dict]:
    await run_due_work(db, now, SETTINGS)
    claimed = await claim_outbox(db, now, SETTINGS)
    for message in claimed:
        await mark_outbox_sent(db, message.id, now)
    return [message.payload for message in claimed]


@pytest.mark.integration
async def test_master_cycle_messages_follow_the_contract(db: AsyncSession) -> None:
    """Un ciclo de la E1 completo: peticiones directas y negotiation-report."""
    # La central habla de un ciclo sin su status-statement: se pide, junto a la distance-table.
    await apply_central_message(
        db, _central("transfer", cycleId=CYCLE, data={"quantity": 508145}), WINDOW_START, SETTINGS
    )
    grace = timedelta(seconds=SETTINGS.statement_request_grace_seconds)
    published = await _outbox_at(db, WINDOW_START + grace)

    await apply_central_message(db, _status_statement(), WINDOW_START + grace, SETTINGS)
    published += await _outbox_at(db, CLOSING)

    assert sorted(m["type"] for m in published) == ["negotiation-report", "request", "request"]
    for message in published:
        assert_publishable(message)


@pytest.mark.integration
@pytest.mark.xfail(
    strict=True,
    reason="negotiation-proposal es de la E1 y el protocolo v3 lo retira; lo reemplazan "
    "bid/offer (tareas C y D). Al quitarlo, este test pasa y hay que borrarlo.",
)
async def test_master_does_not_publish_retired_proposals(db: AsyncSession) -> None:
    await apply_central_message(db, _status_statement(), WINDOW_START, SETTINGS)
    await apply_central_message(
        db, _central("transfer", cycleId=CYCLE, data={"quantity": 1_000_000}), WINDOW_START,
        SETTINGS,
    )
    await create_negotiation(db, "take", Decimal("100"), None, WINDOW_START, SETTINGS)
    for message in await _outbox_at(db, WINDOW_START):
        assert_publishable(message)
