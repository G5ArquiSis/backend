#!/usr/bin/env python3
"""Ensayo de la anomalía 6: ruta deshabilitada a mitad del mercado.

Qué debe verse: la selección de ofertas lo refleja, o el grupo explica por qué pagó la tarifa
punitiva. Rige la última distance-table publicada antes del `settledAt` del swap.

1. La central (identidad `central` del broker local) manda a TAL una distance-table con la
   ruta TAL→HGW habilitada y HGW publica una oferta.
2. A mitad del mercado llega una distance-table nueva (idpk nuevo) con esa ruta
   `enabled: false` y la tarifa punitiva `disabledRouteTransportCost`.
3. Se comprueba que `GET /connectivity` refleja el cambio y se observa si TAL sigue pujando
   por la oferta de HGW, con el precio efectivo antes y después:
   `precio + round2(distance_km × transportCost)` vs `precio + round2(distance_km × punitiva)`
   (`distance` viene en metros), y lo que calcula master en `GET /market/book`.

TAL compra mientras el precio efectivo sea menor que `imbalancePrice`: con la tarifa punitiva
puede seguir conviniendo, sobre todo si no hay otra oferta. Un bid de TAL sin respuesta no se
repite hasta que pasa el span de la estrategia, así que "no volvió a pujar" en esta ventana no
dice nada de la selección; la evidencia es el precio efectivo del libro.

La wallet no cobra transporte (lo hace la central en `trade-recorded`), así que la decisión es
toda de la estrategia de TAL; este script muestra la evidencia para explicarla.

    python3 scripts/demo_anomalia_6.py [--precio 100] [--distancia 4500000] [--espera 60]
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from ensayos import local
from ensayos.contraparte import Contraparte, round2

COSTO = Decimal("0.0034")
PUNITIVA = Decimal("0.034")


def tabla(habilitada: bool, distancia: int, ciclo: str) -> dict:
    return local.mensaje(
        "distance-table",
        {
            "distances": {
                "HGW": {
                    "distance": distancia,
                    "transportCost": float(COSTO),
                    "enabled": habilitada,
                },
            },
            "disabledRouteTransportCost": float(PUNITIVA),
        },
        ciclo=ciclo,
    )


def conexion_hgw() -> dict | None:
    status, cuerpo = local.master("/connectivity")
    if status != 200:
        return None
    return next((c for c in cuerpo.get("connections", []) if c["destination"] == "HGW"), None)


def oferta_en_libro(offer_id: str) -> dict | None:
    status, libro = local.master("/market/book")
    if status != 200:
        return None
    return next((o for o in libro.get("offers", []) if o["offerId"] == offer_id), None)


def mostrar_libro(r: local.Resultado, offer_id: str) -> dict | None:
    oferta = oferta_en_libro(offer_id)
    if oferta:
        r.info(
            f"master: precio efectivo {oferta['effectivePrice']} "
            f"(transporte {oferta['transportPerEnergy']}, ruta "
            f"{'habilitada' if oferta['routeEnabled'] else 'deshabilitada'}); "
            f"conviene frente al desbalance: {oferta['worthBuying']}"
        )
    return oferta


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--precio", type=Decimal, default=Decimal("100"))
    parser.add_argument(
        "--distancia",
        type=int,
        default=4_500_000,
        help="`distance` de HGW en la tabla, en metros como la publica la central",
    )
    parser.add_argument("--espera", type=float, default=60, help="segundos observando bids")
    args = parser.parse_args()

    local.exigir_local()
    r = local.Resultado("Anomalía 6")
    local.stack_arriba(r)
    local.wallet_admin("POST", "/market", {"kwhEnabled": True})
    central = local.Broker("central")
    # Ciclo con déficit para TAL y el mercado abierto durante las dos fases.
    ciclo = local.mercado_comprador(central, "anomalia6", mercado=2 * args.espera + 60)
    hgw = Contraparte(ciclo)
    km = Decimal(args.distancia) / 1000
    efectivo = args.precio + round2(km * COSTO)
    punitivo = args.precio + round2(km * PUNITIVA)
    try:
        hgw.vaciar()
        r.paso("Ruta TAL→HGW habilitada; HGW ofrece")
        central.a_ciudad("TAL", tabla(True, args.distancia, ciclo))
        habilitada = local.esperar(lambda: (c := conexion_hgw()) and c.get("enabled") is True, 15)
        r.chequeo(habilitada is not None, "GET /connectivity muestra HGW habilitada")
        oferta = hgw.ofrecer(5000, args.precio)
        r.info(f"precio efectivo con la ruta habilitada: {efectivo} por kWh")
        # Espera a que master aplique esta tabla: la del ciclo también tenía HGW habilitada.
        libro = local.esperar(
            lambda: (
                (o := oferta_en_libro(oferta["data"]["offerId"]))
                and Decimal(o["effectivePrice"]) == efectivo
                and o
            ),
            15,
        )
        r.chequeo(
            libro is not None,
            "GET /market/book muestra el mismo precio efectivo",
            str(oferta_en_libro(oferta["data"]["offerId"]))[:120] if libro is None else "",
        )
        antes = hgw.esperar_bid(args.espera, oferta["data"]["offerId"])
        r.info(
            f"TAL pujó {antes['data']['quantity']} kWh"
            if antes
            else f"TAL no pujó en {args.espera:.0f} s"
        )
        if antes:
            hgw.acusar_recibo(antes)

        r.paso("A mitad del mercado: distance-table nueva con la ruta deshabilitada")
        central.a_ciudad("TAL", tabla(False, args.distancia, ciclo))
        deshabilitada = local.esperar(
            lambda: (c := conexion_hgw()) and c.get("enabled") is False, 15
        )
        r.chequeo(deshabilitada is not None, "GET /connectivity muestra HGW deshabilitada")
        r.info(f"precio efectivo con la tarifa punitiva: {punitivo} por kWh")
        libro = local.esperar(
            lambda: (
                (o := oferta_en_libro(oferta["data"]["offerId"]))
                and o["routeEnabled"] is False
                and o
            ),
            15,
        )
        if r.chequeo(libro is not None, "GET /market/book marca la ruta deshabilitada"):
            r.chequeo(
                Decimal(libro["effectivePrice"]) == punitivo,
                "master usa la tarifa punitiva en el precio efectivo",
                libro["effectivePrice"],
            )
            mostrar_libro(r, oferta["data"]["offerId"])
        hgw.broker.publicar(
            "market",
            "market.offer.HGW",
            local.mensaje(
                "offer-updated",
                {
                    "offerId": oferta["data"]["offerId"],
                    "remaining": 5000,
                    "pricePerEnergy": float(args.precio),
                },
                ciudad="HGW",
                ciclo=ciclo,
            ),
        )
        despues = hgw.esperar_bid(args.espera, oferta["data"]["offerId"])
        if despues:
            r.info(
                f"TAL pujó {despues['data']['quantity']} kWh con la ruta deshabilitada: "
                f"la estrategia debe justificar pagar {punitivo} (¿no había otra oferta "
                "más barata contando transporte?)"
            )
        elif antes:
            r.info(
                "TAL no repitió el bid en esta ventana: el anterior sigue esperando respuesta "
                "hasta que pase el span. La selección se ve en el precio efectivo de arriba"
            )
        else:
            r.info(
                "TAL no pujó en ninguna de las dos fases: no hay evidencia sobre la selección "
                "(¿la estrategia de compra está activa y TAL tiene déficit?)"
            )
    finally:
        hgw.broker.publicar(
            "market",
            "market.offer.HGW",
            local.mensaje(
                "offer-withdrawn", {"offerId": f"HGW-{ciclo}"}, ciudad="HGW", ciclo=ciclo
            ),
        )
        central.a_ciudad("TAL", tabla(True, args.distancia, ciclo))
        central.cerrar()
        hgw.cerrar()
    r.terminar()


if __name__ == "__main__":
    main()
