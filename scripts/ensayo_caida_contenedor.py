#!/usr/bin/env python3
"""Ensayo de caída de contenedores (E6): reiniciar sin perder el reporte pendiente.

master no tiene temporizadores: el plazo del `negotiation-report` vive en Postgres
(`cycle_ledger.report_due_at`) y lo dispara el `POST /internal/outbox/claim` de connector.
Este ensayo comprueba que una caída justo antes del plazo no lo pierde ni lo duplica:

1. La central le manda a TAL un status-statement de un ciclo comprimido: el periodo de cierre
   abre en `--mercado` segundos y el reporte vence 15 s después (`REPORT_MARGIN_SECONDS`).
2. Unos segundos antes del plazo se matan (`docker compose kill`) los servicios elegidos:
   por omisión master-1, master-2 y connector.
3. Se los vuelve a levantar después del plazo, se espera que estén healthy y se lee
   `central.q`: debe llegar el reporte de ese ciclo, una sola vez (o reintentos con el mismo
   idpk), y `GET /cycles/{id}` debe mostrarlo enviado.

En producción el reinicio lo hace `restart: unless-stopped`; aquí se hace con `docker compose
start` para controlar cuánto dura la caída.

Con `--perder-reporte` la caída dura hasta después de `validUntil`: el reporte ya no puede
salir, y es lo que debe detectar el guardián de ciclos (alerta "Ciclo sin reporte",
`docs/monitoreo.md` § Guardián de ciclos). El script comprueba que no sale fuera de la ventana y
que `GET /cycles/{id}` lo muestra sin enviar.

    python3 scripts/ensayo_caida_contenedor.py [--servicios master-1 master-2 connector]
        [--mercado 40] [--caida 30] [--perder-reporte]
"""

from __future__ import annotations

import argparse
import time

from ensayos import local


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--servicios", nargs="+", default=["master-1", "master-2", "connector"])
    parser.add_argument(
        "--mercado", type=float, default=40, help="segundos hasta que abre el periodo de cierre"
    )
    parser.add_argument(
        "--caida", type=float, default=30, help="segundos que los servicios quedan abajo"
    )
    parser.add_argument(
        "--perder-reporte",
        action="store_true",
        help="seguir abajo hasta que cierre la ventana: prueba del guardián de ciclos",
    )
    args = parser.parse_args()
    # Con --perder-reporte la ventana cierra 20 s después de abrir el periodo de cierre.
    cierre = 20 if args.perder_reporte else args.mercado + 120

    local.exigir_local()
    r = local.Resultado("Caída de contenedores")
    local.stack_arriba(r)
    ciclo = local.ciclo_nuevo("caida")
    central = local.Broker("central")
    try:
        central.vaciar("central.q")
        r.paso(f"Ciclo {ciclo}: el reporte vence en ~{args.mercado + 15:.0f} s")
        estado = local.status_statement(ciclo, mercado=args.mercado, cierre=cierre)
        cierra = time.monotonic() + args.mercado + cierre
        central.a_ciudad("TAL", estado)
        abierto = local.esperar(
            lambda: (
                local.master(f"/cycles/{ciclo}")[0] == 200
                and local.master(f"/cycles/{ciclo}")[1].get("opened")
            ),
            20,
        )
        r.chequeo(bool(abierto), "master abrió el ciclo")

        r.paso(f"Caída de {', '.join(args.servicios)} antes del plazo")
        time.sleep(max(0.0, args.mercado + 15 - 8 - 1))
        local.compose("kill", *args.servicios)
        r.info(f"servicios muertos: {[local.estado_contenedor(s) for s in args.servicios]}")
        temprano = local.esperar_reporte(central, ciclo, 1)
        r.chequeo(not temprano, "el reporte no salió antes de la caída")
        if args.perder_reporte:
            r.info("abajo hasta 10 s después de que cierre la ventana")
            time.sleep(max(0.0, cierra + 10 - time.monotonic()))
        else:
            time.sleep(args.caida)

        r.paso("Reinicio")
        local.compose("start", *args.servicios)
        sanos = local.esperar(
            lambda: all(local.estado_contenedor(s) == "healthy" for s in args.servicios), 120, 3
        )
        r.chequeo(
            sanos is not None,
            "los servicios volvieron healthy",
            str({s: local.estado_contenedor(s) for s in args.servicios}),
        )

        if args.perder_reporte:
            reportes = local.esperar_reporte(central, ciclo, 30)
            r.paso("La ventana cerró sin reporte: el guardián debe avisar")
            r.chequeo(not reportes, "no sale un reporte fuera de la ventana")
            status, detalle = local.master(f"/cycles/{ciclo}")
            r.chequeo(
                status == 200 and detalle.get("reportSentAt") is None,
                "GET /cycles/{id} muestra el ciclo sin reporte",
                str(detalle.get("reportSentAt")) if status == 200 else str(status),
            )
            r.info("con la license key local: CycleClosed owedReport=true reportSent=false")
        else:
            r.paso("El reporte pendiente sale una vez")
            reportes = local.esperar_reporte(central, ciclo, 45)
            r.chequeo(
                bool(reportes),
                "llegó el negotiation-report a la central",
                f"{len(reportes)} envío(s)",
            )
            idpks = {m["idpk"] for m in reportes}
            r.chequeo(
                len(idpks) <= 1,
                "todos los envíos son la misma operación (mismo idpk)",
                f"{len(idpks)} idpk distintos",
            )
            if reportes:
                r.info(f"data: {reportes[0]['data']}")
            status, detalle = local.master(f"/cycles/{ciclo}")
            r.chequeo(
                status == 200 and detalle.get("reportSentAt") is not None,
                "GET /cycles/{id} muestra el reporte enviado",
                str(detalle.get("reportSentAt")) if status == 200 else str(status),
            )
    finally:
        local.compose("start", *args.servicios, check=False)
        central.cerrar()
    r.terminar()


if __name__ == "__main__":
    main()
