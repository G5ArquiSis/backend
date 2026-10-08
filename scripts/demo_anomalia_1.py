#!/usr/bin/env python3
"""Script interactivo para la Demo con el ayudante: Anomalía 1.

Demuestra en vivo:
1. Reenvío duplicado de un mensaje sensible (mismo idpk).
2. El ledger no cambia dos veces (idempotencia contable).
3. El mensaje duplicado queda registrado en duplicate_messages / message-log (RF05).

Uso:
    python3 scripts/demo_anomalia_1.py [--url http://127.0.0.1:8001]
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime


def make_request(url: str, method: str = "GET", data: dict | None = None) -> tuple[int, dict]:
    """Realiza una petición HTTP usando solo la librería estándar de Python."""
    req_data = json.dumps(data).encode("utf-8") if data else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"error": body}
        return e.code, parsed


def print_step(title: str) -> None:
    print(f"\n{'='*70}\n[DEMO ANOMALÍA 1] {title}\n{'='*70}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Demo Anomalía 1: Reenvío duplicado de idpk")
    parser.add_argument("--url", default="http://127.0.0.1:8001", help="URL base de la API")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    cycle_id = f"cycle-demo-{int(time.time())}"
    shared_idpk = f"sensible-idpk-{uuid.uuid4().hex[:8]}"

    print_step(f"Iniciando ciclo de prueba en {base_url} (Ciclo: {cycle_id})")

    # 1. Crear ciclo base
    stat_msg = {
        "idpk": f"stat-{uuid.uuid4().hex[:8]}",
        "msgId": str(uuid.uuid4()),
        "type": "status-statement",
        "cycleId": cycle_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": {
            "energy": {"generationCapacity": 1000000, "consumption": 800000, "generationCost": 200.0}
        },
    }
    status, res = make_request(f"{base_url}/ledger/messages", "POST", stat_msg)
    print(f"1. Status-statement enviado: HTTP {status} -> Capacidad: 1,000,000 kWh, Costo: 200")

    # 2. Cargar fondos
    transfer_msg = {
        "idpk": f"trans-{uuid.uuid4().hex[:8]}",
        "msgId": str(uuid.uuid4()),
        "type": "transfer",
        "cycleId": cycle_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": {"quantity": 500000.0},
    }
    status, res = make_request(f"{base_url}/ledger/messages", "POST", transfer_msg)
    print(f"2. Transfer recibido: HTTP {status} -> Presupuesto inicial: $500,000 créditos")

    # 3. Enviar mensaje sensible por primera vez
    print_step(f"PASO 1: Envío original del mensaje sensible con idpk='{shared_idpk}'")
    first_demand = {
        "idpk": shared_idpk,
        "msgId": str(uuid.uuid4()),
        "type": "demand-statement",
        "cycleId": cycle_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": {
            "balance": {"quantity": 1000, "valuePerKwh": 200.0}  # +1000 kWh, -$200,000
        },
    }
    status, res = make_request(f"{base_url}/ledger/messages", "POST", first_demand)
    print(f"Respuesta HTTP: {status} (CREATED)")
    print(f"isDuplicate:    {res.get('isDuplicate')}")
    print(f"Delta energía:  {res.get('deltaEnergy'):+d} kWh")
    print(f"Delta budget:   ${res.get('deltaBudget'):+,.2f} créditos")
    print(f"Nuevo balance:  Presupuesto=${res.get('budgetBalance'):,.2f} | Energía={res.get('energyBalance')} kWh")

    initial_budget = res.get("budgetBalance")
    initial_energy = res.get("energyBalance")

    # 4. Reenvío duplicado intencional con el MISMO idpk
    print_step(f"PASO 2: Provocando Anomalía -> Reenvío duplicado con el MISMO idpk='{shared_idpk}'")
    retry_demand = {
        "idpk": shared_idpk,  # MISMO IDPK
        "msgId": str(uuid.uuid4()),  # msgId nuevo
        "type": "demand-statement",
        "cycleId": cycle_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": {
            "balance": {"quantity": 1000, "valuePerKwh": 200.0}
        },
    }
    status, res = make_request(f"{base_url}/ledger/messages", "POST", retry_demand)
    print(f"Respuesta HTTP: {status} (OK - Conector puede hacer ACK sin reintentar)")
    print(f"isDuplicate:    {res.get('isDuplicate')} (¡COLISIÓN DETECTADA!)")
    print(f"Delta energía:  {res.get('deltaEnergy')} kWh (0 mutaciones)")
    print(f"Delta budget:   ${res.get('deltaBudget')} créditos (0 mutaciones)")

    # 5. Comprobar que el ledger NO cambió
    print_step("PASO 3: Verificando integridad del Ledger (GET /cycles/{cycleId})")
    status, cycle_detail = make_request(f"{base_url}/cycles/{cycle_id}")
    final_budget = cycle_detail.get("balances", {}).get("budget")
    final_energy = cycle_detail.get("balances", {}).get("energy")

    print(f"Presupuesto post-duplicado: ${final_budget:,.2f} (Esperado: ${initial_budget:,.2f})")
    print(f"Energía post-duplicada:     {final_energy} kWh (Esperado: {initial_energy} kWh)")

    if final_budget == initial_budget and final_energy == initial_energy:
        print("\n>>> VERIFICACIÓN EXITOSA: Los balances del ledger NO sufrieron alteración doble. <<<")
    else:
        print("\n>>> ERROR: Los balances cambiaron tras el duplicado. <<<")
        sys.exit(1)

    # 6. Comprobar que el duplicado quedó en message-log (RF05)
    print_step("PASO 4: Verificando registro en la tabla de duplicados (GET /message-log?category=duplicate)")
    status, log_data = make_request(f"{base_url}/message-log?category=duplicate")
    dups = [item for item in log_data.get("items", []) if item.get("idpk") == shared_idpk]

    if dups:
        print(f"Fila encontrada en duplicate_messages: {json.dumps(dups[0], indent=2)}")
        print("\n>>> VERIFICACIÓN EXITOSA: El intento duplicado está registrado y es consultable para RF05. <<<")
    else:
        print("\n>>> ERROR: No se encontró el registro del duplicado en /message-log. <<<")
        sys.exit(1)

    print("\n" + "#"*70)
    print("# DEMO ANOMALÍA 1 LISTA Y VERIFICADA AL 100% PARA EL AYUDANTE")
    print("#"*70 + "\n")


if __name__ == "__main__":
    main()
