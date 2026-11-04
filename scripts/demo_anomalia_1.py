#!/usr/bin/env python3
"""Ensayo de la anomalía 1 de la demo: reenvío duplicado de un mensaje sensible.

Simula a connector contra un master local y muestra que:
1. Un demand-statement reenviado con el mismo idpk no cambia el ledger dos veces.
2. El reenvío queda en el registro de duplicados (RF05, GET /message-log).

Solo corre contra el stack local (`docker compose up`): inyecta un ciclo de prueba
que mueve el presupuesto, y en producción eso alteraría el budgetBalance que se le
reporta a la central. Además, Nginx bloquea /internal/ desde afuera.

Basado en el script original de Vicente Pavez (PR #17), adaptado al contrato actual
de /internal/messages. Uso:

    python3 scripts/demo_anomalia_1.py [--url http://127.0.0.1:8001]
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def request(url: str, body: dict | None = None) -> tuple[int, dict]:
    """GET o POST con JSON, solo con la librería estándar."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, {"error": error.read().decode("utf-8", errors="replace")}


def central(message_type: str, cycle_id: str, data: dict, idpk: str | None = None) -> dict:
    """Mensaje de la central en el envelope v2, con msgId siempre nuevo."""
    return {
        "idpk": idpk or str(uuid.uuid4()),
        "msgId": str(uuid.uuid4()),
        "type": message_type,
        "timestamp": _now(),
        "sender": "central",
        "cycleId": cycle_id,
        "data": data,
    }


def step(title: str) -> None:
    print(f"\n{'=' * 70}\n[ANOMALÍA 1] {title}\n{'=' * 70}")


def balances(base: str, cycle_id: str) -> tuple[Decimal, Decimal]:
    status, detail = request(f"{base}/cycles/{cycle_id}")
    if status != 200:
        sys.exit(f"GET /cycles/{cycle_id} respondió {status}: {detail}")
    return Decimal(str(detail["balances"]["budget"])), Decimal(str(detail["balances"]["energy"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8001", help="URL de un master local")
    base = parser.parse_args().url.rstrip("/")
    if urlparse(base).hostname not in _LOCAL_HOSTS:
        sys.exit("Este ensayo solo corre contra un master local: alteraría el presupuesto real.")

    cycle_id = f"cycle-demo-{uuid.uuid4().hex[:8]}"
    messages = f"{base}/internal/messages"
    valid_until = (datetime.now(UTC) + timedelta(minutes=20)).isoformat().replace("+00:00", "Z")

    step(f"Ciclo de prueba {cycle_id}")
    energy = {"generationCapacity": 1000000, "consumption": 800000, "generationCost": 200}
    status_statement = {"energy": energy, "validUntil": valid_until}
    print(
        "status-statement:",
        request(messages, central("status-statement", cycle_id, status_statement)),
    )
    print(
        "transfer:        ", request(messages, central("transfer", cycle_id, {"quantity": 500000}))
    )

    demand = {"balance": {"quantity": 1000, "valuePerKwh": 200}}
    idpk = str(uuid.uuid4())

    step(f"Envío original del demand-statement (idpk {idpk})")
    print("respuesta:", request(messages, central("demand-statement", cycle_id, demand, idpk)))
    before = balances(base, cycle_id)
    print(f"presupuesto {before[0]} | energía {before[1]} kWh")

    step("Reenvío con el MISMO idpk y un msgId nuevo")
    status, answer = request(messages, central("demand-statement", cycle_id, demand, idpk))
    print(f"respuesta: {status} {answer}  <- connector igual confirma y responde ACK")
    after = balances(base, cycle_id)
    print(f"presupuesto {after[0]} | energía {after[1]} kWh")

    if answer.get("outcome") != "duplicate" or after != before:
        sys.exit("\n>>> FALLÓ: el reenvío cambió el ledger o no se detectó como duplicado.")
    print("\n>>> El ledger no cambió dos veces.")

    step("Registro de duplicados (GET /message-log?category=duplicate)")
    _, log = request(f"{base}/message-log?category=duplicate&limit=50")
    found = [item for item in log.get("items", []) if item.get("idpk") == idpk]
    if not found:
        sys.exit(">>> FALLÓ: el duplicado no aparece en /message-log.")
    print(json.dumps(found[0], indent=2, ensure_ascii=False))
    print("\n>>> El duplicado quedó registrado y es consultable (RF05).")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
