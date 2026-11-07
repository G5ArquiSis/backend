#!/usr/bin/env python3
"""Ensayo de la anomalía 5: corte breve de la conexión al broker durante el mercado.

Qué debe verse: reconexión sin intervención manual, y la oferta publicada y los swaps en curso
terminan en un estado consistente.

    --modo conexiones (por omisión)
        Cierra desde la API de administración de RabbitMQ las conexiones de connector
        (`city.TAL` y `observer`): para connector es un corte de red. El broker sigue arriba.
    --modo broker --segundos 20
        Detiene el contenedor de RabbitMQ esos segundos y lo vuelve a levantar.

Después comprueba que connector se reconectó solo, que sigue `healthy` y que el camino completo
funciona: la central le manda una distance-table a TAL y TAL responde ACK. Si TAL tenía una
oferta publicada, se espera verla de nuevo (o un `offer-updated`) en el feed.

    python3 scripts/demo_anomalia_5.py [--modo conexiones|broker] [--segundos 20]
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime

from ensayos import local


def cortar_conexiones(r: local.Resultado) -> None:
    for usuario in ("city.TAL", "observer"):
        nombres = local.conexiones_de(usuario)
        r.chequeo(bool(nombres), f"connector tiene conexión como {usuario}", f"{len(nombres)}")
        for nombre in nombres:
            r.info(f"cerrando {nombre} → {local.cerrar_conexion(nombre)}")


def parar_broker(r: local.Resultado, segundos: float) -> None:
    local.compose("stop", "rabbitmq")
    r.info(f"rabbitmq detenido; se levanta en {segundos:.0f} s")
    time.sleep(segundos)
    local.compose("start", "rabbitmq")
    r.chequeo(
        local.esperar(lambda: local.estado_contenedor("rabbitmq") == "healthy", 90) is not None,
        "rabbitmq volvió a estar healthy",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--modo", choices=["conexiones", "broker"], default="conexiones")
    parser.add_argument("--segundos", type=float, default=20)
    parser.add_argument(
        "--espera", type=float, default=90, help="segundos máximos esperando la reconexión"
    )
    args = parser.parse_args()

    local.exigir_local()
    r = local.Resultado("Anomalía 5")
    local.stack_arriba(r)
    inicio = datetime.now(UTC).isoformat(timespec="seconds")

    r.paso("Corte")
    if args.modo == "conexiones":
        cortar_conexiones(r)
    else:
        parar_broker(r, args.segundos)

    r.paso(f"Reconexión (hasta {args.espera:.0f} s)")
    volvio = local.esperar(lambda: local.conexiones_de("city.TAL"), args.espera, cada=2)
    r.chequeo(volvio is not None, "connector se reconectó como city.TAL sin intervención")
    r.chequeo(
        local.esperar(lambda: local.conexiones_de("observer"), 30, cada=2) is not None,
        "y como observer",
    )
    sano = local.esperar(lambda: local.estado_contenedor("connector") == "healthy", 60, cada=3)
    r.chequeo(
        sano is not None,
        "connector sigue healthy (laten sus consumidores)",
        local.estado_contenedor("connector"),
    )
    reinicios = local.logs_desde("connector", inicio).count("Escuchando la cola")
    r.info(f"connector volvió a escuchar {reinicios} cola(s) desde el corte")

    r.paso("El camino completo funciona: distance-table de la central → ACK de TAL")
    central = local.Broker("central")
    try:
        central.vaciar("central.q")
        tabla = local.mensaje(
            "distance-table",
            {
                "distances": {
                    "HGW": {"distance": 62763183, "transportCost": 0.0034, "enabled": True}
                },
                "disabledRouteTransportCost": 0.034,
            },
            ciclo=local.ciclo_actual(),
        )
        central.a_ciudad("TAL", tabla)
        ack = central.leer(
            "central.q",
            20,
            lambda m: m.get("type") == "ack" and m.get("data", {}).get("target") == tabla["msgId"],
        )
        r.chequeo(ack is not None, "TAL responde ACK después del corte")
    finally:
        central.cerrar()

    hgw = local.Broker("city.HGW")
    try:
        oferta = hgw.leer(
            "market.HGW",
            30,
            lambda m: m.get("cityId") == "TAL" and m.get("type") in {"offer", "offer-updated"},
        )
        r.info(
            "TAL sigue publicando su oferta"
            if oferta
            else "sin oferta de TAL en 30 s (¿mercado abierto y con excedente?)"
        )
    finally:
        hgw.cerrar()
    r.terminar()


if __name__ == "__main__":
    main()
