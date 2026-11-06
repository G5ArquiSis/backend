#!/usr/bin/env python3
"""Ensayo de la anomalía 7: lectura del propio monitoreo, con la wallet lenta.

Qué debe verse: explicar qué se observa en APM e infraestructura durante la anomalía, incluida
la latencia de la wallet (`docs/monitoreo.md`, dashboard "EnergyShark E2").

El script vuelve lenta la wallet falsa (falla `latency` durante `--minutos`) y, cada 20 s, le
pide a master una reconciliación (`POST /internal/wallet/reconcile`), que lee la wallet con el
WalletClient: cada intento queda como evento `WalletCall`. Al final imprime qué mirar y las
consultas NRQL. Para verlo en New Relic hay que correr el stack local con la license key en
`.env` y un nombre de app propio, para no mezclarlo con producción:

    NEW_RELIC_LICENSE_KEY=...            # la del grupo; nunca se commitea
    NEW_RELIC_APP_NAME=EnergyShark ensayo

Sin license key el ensayo igual muestra el efecto en los logs de master (llamadas lentas,
reintentos) y en `GET /wallet/balance-status`.

    python3 scripts/demo_anomalia_7.py [--segundos 4] [--minutos 3]
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime

from ensayos import local

CONSULTAS = """\
-- Latencia de la wallet vista por TAL (cada intento HTTP del WalletClient)
SELECT percentile(durationMs, 50, 95, 99) FROM WalletCall TIMESERIES SINCE 15 minutes ago
-- Resultado de cada intento: ok, retryable, no_response...
SELECT count(*) FROM WalletCall FACET endpoint, outcome TIMESERIES SINCE 15 minutes ago
-- APM: transacciones de master más lentas mientras la wallet tarda
SELECT average(duration) FROM Transaction WHERE appName = '{app}' FACET name SINCE 15 minutes ago
-- Infraestructura: memoria y CPU de los contenedores durante el ensayo
SELECT average(memoryUsageBytes)/1e6, average(cpuPercent) FROM ContainerSample FACET name
  TIMESERIES SINCE 15 minutes ago"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--segundos", type=float, default=4, help="latencia por respuesta")
    parser.add_argument("--minutos", type=float, default=3, help="duración de la lentitud")
    parser.add_argument("--app", default="EnergyShark ensayo", help="NEW_RELIC_APP_NAME local")
    args = parser.parse_args()

    local.exigir_local()
    r = local.Resultado("Anomalía 7")
    local.stack_arriba(r)
    local.wallet_admin("POST", "/market", {"kwhEnabled": True})
    inicio = datetime.now(UTC).isoformat(timespec="seconds")
    # Una latencia por llamada: `times` alto para cubrir toda la duración.
    llamadas = int(args.minutos * 60 * 4)

    r.paso(f"Wallet lenta: {args.segundos:.1f} s por respuesta durante {args.minutos:.0f} min")
    status, _ = local.wallet_admin(
        "POST",
        "/faults",
        {
            "kind": "latency",
            "seconds": args.segundos,
            "times": llamadas,
        },
    )
    r.chequeo(status == 200, "falla de latencia activa")
    duraciones: list[float] = []
    try:
        fin = time.monotonic() + args.minutos * 60
        while time.monotonic() < fin:
            antes = time.monotonic()
            status, _ = local.http(
                "POST", f"{local.MASTER}/internal/wallet/reconcile", {}, timeout=120
            )
            duracion = time.monotonic() - antes
            duraciones.append(duracion)
            r.info(
                f"reconciliación → {status} en {duracion:.1f} s "
                f"(faltan {max(0, fin - time.monotonic()):.0f} s)"
            )
            time.sleep(max(0.0, 20 - duracion))
    finally:
        local.wallet_admin("DELETE", "/faults")
        r.info("latencia retirada")

    r.paso("Qué dejó la anomalía")
    r.chequeo(
        bool(duraciones) and max(duraciones) >= args.segundos,
        "las llamadas de master a la wallet se alargaron",
        f"máx {max(duraciones, default=0):.1f} s",
    )
    logs = local.logs_desde("master-1", inicio) + local.logs_desde("master-2", inicio)
    lentas = [linea for linea in logs.splitlines() if "wallet" in linea.lower()]
    r.info(f"{len(lentas)} líneas de log de master que mencionan la wallet desde el inicio")
    for linea in lentas[-5:]:
        r.info(f"  {linea[:160]}")
    r.chequeo(local.master("/health")[0] == 200, "master siguió respondiendo")

    print("\nQué mirar en New Relic (dashboard EnergyShark E2, páginas Wallet e Infraestructura):")
    print(
        f"  - Latencia p95 de WalletCall sube a ~{args.segundos * 1000:.0f} ms y vuelve a "
        "bajar al retirar la falla."
    )
    print("  - Si supera el timeout del cliente, aparecen intentos `no_response` y reintentos.")
    print("  - APM: las transacciones de master que llaman a la wallet se alargan; las demás no.")
    print("  - Infraestructura: memoria y CPU no deberían moverse (es espera de red, no carga).")
    print("\nConsultas NRQL:\n" + CONSULTAS.format(app=args.app))
    r.terminar()


if __name__ == "__main__":
    main()
