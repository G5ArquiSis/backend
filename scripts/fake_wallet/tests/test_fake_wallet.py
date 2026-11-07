"""Tests de la wallet falsa.

Si `ENERGYSHARK_CONTRACTS_DIR` apunta a un clon de `iic2173/energyshark-contracts` (v3.0.2),
además se valida cada respuesta y cada evento contra los schemas del curso.
"""

import json
import os
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import jwt
import pytest

from fake_wallet.app import Config, create_app
from fake_wallet.core import iso, now_utc

TAL, HGW = "fake-key-tal", "fake-key-hgw"


# --- Validación opcional contra los schemas del curso -------------------------------------


def _load_validators():
    root = os.environ.get("ENERGYSHARK_CONTRACTS_DIR")
    if not root:
        return None
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    schemas = {}
    for path in Path(root, "schemas").rglob("*.schema.json"):
        schema = json.loads(path.read_text())
        schemas[str(path.relative_to(Path(root, "schemas")))] = schema
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )

    def validate(name: str, instance) -> None:
        Draft202012Validator(schemas[name], registry=registry).validate(instance)

    return validate


VALIDATE = _load_validators()


def conforms(name: str, instance) -> None:
    if VALIDATE is not None:
        VALIDATE(name, instance)


# --- Fixtures -----------------------------------------------------------------------------


@pytest.fixture
async def wallet_app():
    app = create_app(Config(processing_seconds=0))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://wallet") as client:
        client.app = app
        yield client


def propose_body(give=("KWH", 7300), take=("CR", 2409000), seconds=30, memo="TAL-1"):
    return {
        "give": {"asset": give[0], "amount": give[1]},
        "take": {"asset": take[0], "amount": take[1]},
        "expiresAt": iso(now_utc() + timedelta(seconds=seconds)),
        "memo": memo,
    }


async def write(client, path, key, idpk=None, body=None):
    headers = {"X-Api-Key": key, "Idempotency-Key": idpk or str(uuid.uuid4())}
    return await client.post(f"/v1{path}", headers=headers, json=body)


def events(client, city=None, type=None):
    selected = [
        e
        for e in client.app.state.events
        if (city is None or e["city"] == city) and (type is None or e["message"]["type"] == type)
    ]
    for event in selected:
        conforms(f"amqp/{event['message']['type']}.schema.json", event["message"])
    return selected


async def propose(client, body=None, key=TAL, idpk=None):
    response = await write(client, "/swaps", key, idpk, body or propose_body())
    assert response.status_code == 202, response.text
    conforms("wallet/write-accepted.schema.json", response.json())
    await client.app.state.settle()
    return response.json()["swapId"]


async def balance(client, key):
    response = await client.get("/v1/balance", headers={"X-Api-Key": key})
    assert response.status_code == 200
    conforms("wallet/balance.schema.json", response.json())
    return response.json()["assets"]


# --- Autenticación y forma ------------------------------------------------------------------


async def test_sin_api_key_es_401(wallet_app):
    response = await wallet_app.get("/v1/balance")
    assert response.status_code == 401
    assert response.json()["reason"] == "UNAUTHORIZED"
    conforms("wallet/http-error.schema.json", response.json())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(counterparty="HGW"),
        lambda b: b.pop("memo"),
        lambda b: b["give"].update(amount=7300.5),
        lambda b: b["take"].update(amount=10.123),
        lambda b: b.update(expiresAt=iso(now_utc() + timedelta(seconds=120))),
        lambda b: b.update(expiresAt=iso(now_utc() - timedelta(seconds=1))),
        lambda b: b.update(memo="x" * 129),
    ],
)
async def test_cuerpo_invalido_es_422_y_no_crea_nada(wallet_app, mutate):
    body = propose_body()
    mutate(body)
    response = await write(wallet_app, "/swaps", TAL, body=body)
    assert response.status_code == 422
    assert response.json()["reason"] == "MALFORMED_REQUEST"
    conforms("wallet/http-error.schema.json", response.json())
    assert wallet_app.app.state.wallet.swaps == {}


async def test_idempotency_key_que_no_es_uuid_es_422(wallet_app):
    response = await write(wallet_app, "/swaps", TAL, idpk="no-es-uuid", body=propose_body())
    assert response.status_code == 422
    assert response.json()["reason"] == "MALFORMED_REQUEST"


async def test_swap_desconocido_es_404(wallet_app):
    response = await write(wallet_app, "/swaps/no-existe/accept", HGW)
    assert response.status_code == 404
    assert response.json()["reason"] == "SWAP_UNKNOWN"
    conforms("wallet/http-error.schema.json", response.json())


# --- Proponer -------------------------------------------------------------------------------


async def test_proponer_retiene_give_y_emite_token_verificable(wallet_app):
    swap_id = await propose(wallet_app)

    [event] = events(wallet_app, "TAL", "swap-proposed")
    data = event["message"]["data"]
    assert data["swapId"] == swap_id
    assert data["balancesAfter"]["KWH"] == {"available": 20000 - 7300, "held": 7300}

    jwks = (await wallet_app.get("/.well-known/jwks.json")).json()
    conforms("wallet/jwks.schema.json", jwks)
    key = jwt.PyJWK(jwks["keys"][0]).key
    claims = jwt.decode(data["token"], key, algorithms=["ES256"])
    conforms("token/swap-token-claims.schema.json", claims)
    assert claims["sub"] == swap_id
    assert claims["proposer"] == "TAL"
    assert claims["give"] == {"asset": "KWH", "amount": 7300}
    assert claims["memo"] == "TAL-1"

    state = (await wallet_app.get(f"/v1/swaps/{swap_id}", headers={"X-Api-Key": TAL})).json()
    conforms("wallet/swap-state.schema.json", state)
    assert state["status"] == "proposed"


async def test_misma_llave_devuelve_200_y_otro_cuerpo_es_mismatch(wallet_app):
    idpk = str(uuid.uuid4())
    body = propose_body()
    swap_id = await propose(wallet_app, body, idpk=idpk)

    replay = await write(wallet_app, "/swaps", TAL, idpk, body)
    assert replay.status_code == 200
    conforms("wallet/swap-state.schema.json", replay.json())
    assert replay.json()["swapId"] == swap_id
    assert len(wallet_app.app.state.wallet.swaps) == 1

    other = await write(wallet_app, "/swaps", TAL, idpk, propose_body(memo="otro"))
    assert other.status_code == 422
    assert other.json()["reason"] == "IDEMPOTENCY_MISMATCH"


async def test_mismo_activo_se_acepta_y_se_rechaza_despues(wallet_app):
    swap_id = await propose(wallet_app, propose_body(give=("CR", 1000), take=("CR", 1200.5)))
    [event] = events(wallet_app, "TAL", "swap-rejected")
    assert event["message"]["reason"] == "SAME_ASSET"
    assert event["message"]["code"] == 422
    state = (await wallet_app.get(f"/v1/swaps/{swap_id}", headers={"X-Api-Key": TAL})).json()
    assert state["status"] == "rejected"


async def test_proponer_sin_saldo_es_insufficient_asset(wallet_app):
    await propose(wallet_app, propose_body(give=("KWH", 999999)))
    [event] = events(wallet_app, "TAL", "swap-rejected")
    assert event["message"]["reason"] == "INSUFFICIENT_ASSET"
    data = event["message"]["data"]
    assert (data["asset"], data["available"], data["required"]) == ("KWH", 20000, 999999)


# --- Aceptar y cancelar ---------------------------------------------------------------------


async def test_aceptar_liquida_las_dos_patas(wallet_app):
    swap_id = await propose(wallet_app, propose_body(take=("CR", 1500000.25)))
    accept_idpk = str(uuid.uuid4())
    response = await write(wallet_app, f"/swaps/{swap_id}/accept", HGW, accept_idpk)
    assert response.status_code == 202
    await wallet_app.app.state.settle()

    [to_tal] = events(wallet_app, "TAL", "swap-settled")
    [to_hgw] = events(wallet_app, "HGW", "swap-settled")
    assert to_tal["message"]["data"]["idpk"] == accept_idpk
    assert to_tal["message"]["data"]["balancesAfter"] == {
        "CR": {"available": 600000 + 1500000.25, "held": 0},
        "KWH": {"available": 20000 - 7300, "held": 0},
    }
    assert to_hgw["message"]["data"]["balancesAfter"] == {
        "CR": {"available": 600000 - 1500000.25, "held": 0},
        "KWH": {"available": 20000 + 7300, "held": 0},
    }
    # El comprador usó su línea de crédito: CR negativo, pero sobre -creditLine.
    assert (await balance(wallet_app, HGW))["CR"]["available"] == -900000.25

    statement = (await wallet_app.get("/v1/statement", headers={"X-Api-Key": TAL})).json()
    conforms("wallet/statement-page.schema.json", statement)
    assert {(e["asset"], e["amount"]) for e in statement["entries"]} == {
        ("KWH", -7300),
        ("CR", 1500000.25),
    }


async def test_aceptar_sin_saldo_rechaza_a_ambos_y_libera_la_retencion(wallet_app):
    swap_id = await propose(wallet_app, propose_body(take=("CR", 5_000_000)))
    await write(wallet_app, f"/swaps/{swap_id}/accept", HGW)
    await wallet_app.app.state.settle()

    [to_hgw] = events(wallet_app, "HGW", "swap-rejected")
    [to_tal] = events(wallet_app, "TAL", "swap-rejected")
    assert to_hgw["message"]["data"]["available"] == 600000
    assert "available" not in to_tal["message"]["data"]
    assert to_tal["message"]["data"]["operation"] == "accept"
    assert (await balance(wallet_app, TAL))["KWH"] == {"available": 20000, "held": 0}


async def test_rechazos_de_accept_y_cancel(wallet_app):
    swap_id = await propose(wallet_app)

    await write(wallet_app, f"/swaps/{swap_id}/accept", TAL)
    await write(wallet_app, f"/swaps/{swap_id}/cancel", HGW)
    await wallet_app.app.state.settle()
    reasons = [e["message"]["reason"] for e in events(wallet_app, type="swap-rejected")]
    assert reasons == ["SELF_SWAP", "NOT_PROPOSER"]

    await write(wallet_app, f"/swaps/{swap_id}/cancel", TAL)
    await wallet_app.app.state.settle()
    [cancelled] = events(wallet_app, "TAL", "swap-cancelled")
    assert cancelled["message"]["data"]["balancesAfter"]["KWH"] == {"available": 20000, "held": 0}

    await write(wallet_app, f"/swaps/{swap_id}/accept", HGW)
    await wallet_app.app.state.settle()
    last = events(wallet_app, "HGW", "swap-rejected")[-1]["message"]
    assert (last["reason"], last["code"]) == ("SWAP_NOT_PROPOSED", 409)


async def test_vencimiento_libera_la_retencion(wallet_app):
    swap_id = await propose(wallet_app, propose_body(seconds=5))
    wallet = wallet_app.app.state.wallet
    later = now_utc() + timedelta(seconds=10)
    [expired] = wallet.expire_due(later)
    conforms("amqp/swap-expired.schema.json", expired.message)
    assert expired.message["data"]["swapId"] == swap_id
    assert wallet.swaps[swap_id].status == "expired"


async def test_cerrar_el_mercado_cancela_los_swaps_con_kwh(wallet_app):
    swap_id = await propose(wallet_app)
    response = await wallet_app.post("/_fake/market", json={"kwhEnabled": False})
    assert response.json() == {"kwhEnabled": False}
    [cancelled] = events(wallet_app, "TAL", "swap-cancelled")
    assert cancelled["message"]["data"]["swapId"] == swap_id

    await propose(wallet_app)
    rejected = events(wallet_app, "TAL", "swap-rejected")[-1]["message"]
    assert (rejected["reason"], rejected["code"]) == ("MARKET_CLOSED", 410)


# --- Fallas inyectadas ----------------------------------------------------------------------


async def test_502_pierde_la_respuesta_pero_ejecuta_la_operacion(wallet_app):
    await wallet_app.post("/_fake/faults", json={"kind": "502", "operations": ["propose"]})
    idpk, body = str(uuid.uuid4()), propose_body()

    lost = await write(wallet_app, "/swaps", TAL, idpk, body)
    assert lost.status_code == 502
    await wallet_app.app.state.settle()
    assert len(wallet_app.app.state.wallet.swaps) == 1

    retry = await write(wallet_app, "/swaps", TAL, idpk, body)
    assert retry.status_code == 200
    assert retry.json()["status"] == "proposed"


async def test_503_no_acepta_nada(wallet_app):
    await wallet_app.post("/_fake/faults", json={"kind": "503", "retryAfter": 3})
    response = await write(wallet_app, "/swaps", TAL, body=propose_body())
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "3"
    assert response.json()["reason"] == "WALLET_BUSY"
    assert wallet_app.app.state.wallet.swaps == {}


async def test_evento_perdido_se_recupera_con_get(wallet_app):
    await wallet_app.post("/_fake/faults", json={"kind": "drop"})
    swap_id = await propose(wallet_app)
    assert [e["fault"] for e in events(wallet_app)] == ["drop"]
    state = (await wallet_app.get(f"/v1/swaps/{swap_id}", headers={"X-Api-Key": TAL})).json()
    assert state["status"] == "proposed"
    assert state["lastEvent"]["type"] == "swap-proposed"


async def test_evento_duplicado_repite_el_msg_id(wallet_app):
    await wallet_app.post("/_fake/faults", json={"kind": "duplicate"})
    await propose(wallet_app)
    first, second = events(wallet_app, "TAL")
    assert first["message"]["msgId"] == second["message"]["msgId"]


async def test_leaderboard(wallet_app):
    response = await wallet_app.get("/v1/leaderboard", headers={"X-Api-Key": TAL})
    conforms("wallet/leaderboard.schema.json", response.json())
    assert response.json()["cityId"] == "TAL"
