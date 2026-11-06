#!/usr/bin/env python3
"""Escenario de punta a punta (E6): un ciclo completo de la E2 sobre el stack local.

Comando único del entregable E6: levanta el stack (perfil `fake`: Postgres, master x2,
connector, RabbitMQ y la wallet falsa) y recorre un ciclo comprimido:

    status-statement y distance-table de la central
    → oferta de TAL en el feed → bid de HGW → TAL propone el swap → bid-accepted con el token
    → HGW verifica el token y acepta → swap-settled → offer-updated de TAL
    → negotiation-report de TAL a la central, con el swap en el ciclo

Con `--tal-compra` los papeles se invierten: HGW ofrece barato, TAL puja, HGW propone y TAL
acepta. HGW es la contraparte de `scripts/ensayos/contraparte.py`.

    python3 scripts/escenario_e2e.py --levantar          # build + up + escenario
    python3 scripts/escenario_e2e.py [--tal-compra] [--mercado 120]

Sale con 1 si algún paso no se cumplió. Estado contra main en scripts/ensayos/README.md.
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from ensayos import local
from ensayos.contraparte import FEED, Contraparte, round2, verificar_token


def levantar(r: local.Resultado) -> None:
    r.paso("docker compose --profile fake up --build --wait")
    salida = local.compose("up", "-d", "--build", "--wait", check=False)
    r.chequeo(salida.returncode == 0, "stack arriba", salida.stderr.strip()[-200:])


def tal_vende(r: local.Resultado, hgw: Contraparte, espera: float) -> None:
    r.paso("TAL ofrece y HGW compra")
    oferta = hgw.esperar_oferta("TAL", espera)
    if not r.chequeo(oferta is not None, "TAL publica su oferta en el feed"):
        return
    precio = Decimal(str(oferta["data"]["pricePerEnergy"]))
    cantidad = max(1, min(100, int(oferta["data"]["quantity"])))
    bid = hgw.pujar("TAL", oferta["data"]["offerId"], cantidad)
    respuesta = hgw.esperar_respuesta(bid, 30)
    if not r.chequeo(
        respuesta is not None and respuesta["type"] == "bid-accepted",
        "TAL acepta el bid",
        respuesta["type"] if respuesta else "sin respuesta",
    ):
        return
    datos = respuesta["data"]
    problemas = verificar_token(
        datos["swapToken"],
        swap_id=datos["swapId"],
        vendedor="TAL",
        cantidad=cantidad,
        monto=round2(cantidad * precio),
        offer_id=oferta["data"]["offerId"],
    )
    r.chequeo(not problemas, "el token del swap es válido", "; ".join(problemas))
    status, _ = hgw.aceptar_swap(datos["swapId"])
    r.chequeo(status == 202, "HGW acepta el swap en la wallet")
    liquidacion(r, datos["swapId"])
    actualizada = hgw.broker.leer(
        FEED, 30, lambda m: m.get("type") == "offer-updated" and m.get("cityId") == "TAL"
    )
    r.chequeo(actualizada is not None, "TAL publica su nuevo remaining (offer-updated)")


def tal_compra(r: local.Resultado, hgw: Contraparte, espera: float) -> None:
    r.paso("HGW ofrece y TAL compra")
    precio = Decimal("50")
    oferta = hgw.ofrecer(5000, precio)
    bid = hgw.esperar_bid(espera, oferta["data"]["offerId"])
    if not r.chequeo(bid is not None, "TAL puja por la oferta de HGW"):
        return
    hgw.acusar_recibo(bid)
    swap = hgw.proponer_swap(bid, precio)
    hgw.aceptar_bid(bid, precio, swap)
    r.info(f"HGW propone {swap['swapId']} y manda bid-accepted")
    liquidacion(r, swap["swapId"])


def liquidacion(r: local.Resultado, swap_id: str) -> None:
    for ciudad in ("TAL", "HGW"):
        evento = local.esperar(
            lambda c=ciudad: next(
                (
                    e
                    for e in local.wallet_events(c, "swap-settled")
                    if e["data"].get("swapId") == swap_id
                ),
                None,
            ),
            30,
        )
        r.chequeo(evento is not None, f"{ciudad} recibe swap-settled")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--levantar", action="store_true", help="build y up del stack antes")
    parser.add_argument("--tal-compra", action="store_true", help="TAL compra en vez de vender")
    parser.add_argument("--mercado", type=float, default=120, help="segundos de mercado")
    args = parser.parse_args()

    local.exigir_local()
    r = local.Resultado("Escenario E2E")
    if args.levantar:
        levantar(r)
    local.stack_arriba(r)
    local.wallet_admin("POST", "/market", {"kwhEnabled": True})
    ciclo = local.ciclo_nuevo("e2e")
    central = local.Broker("central")
    hgw = Contraparte(ciclo)
    try:
        hgw.vaciar()
        central.vaciar("central.q")
        r.paso(f"Ciclo {ciclo}: {args.mercado:.0f} s de mercado")
        # TAL vende si genera más de lo que consume; con --tal-compra, al revés.
        generacion, consumo = (8000, 20000) if args.tal_compra else (26000, 13000)
        central.a_ciudad(
            "TAL",
            local.status_statement(
                ciclo, mercado=args.mercado, cierre=120, generacion=generacion, consumo=consumo
            ),
        )
        central.a_ciudad(
            "TAL",
            local.mensaje(
                "distance-table",
                {
                    "distances": {
                        "HGW": {"distance": 4500, "transportCost": 0.0034, "enabled": True}
                    },
                    "disabledRouteTransportCost": 0.034,
                },
                ciclo=ciclo,
            ),
        )
        abierto = local.esperar(
            lambda: (d := local.master(f"/cycles/{ciclo}"))[0] == 200 and d[1].get("opened"), 20
        )
        r.chequeo(bool(abierto), "master abrió el ciclo")

        espera = args.mercado * 0.8
        if args.tal_compra:
            tal_compra(r, hgw, espera)
        else:
            tal_vende(r, hgw, espera)

        r.paso("Cierre: negotiation-report")
        reportes = local.esperar_reporte(central, ciclo, args.mercado + 60)
        r.chequeo(bool(reportes), "TAL envía su negotiation-report", f"{len(reportes)} envío(s)")
        if len({m["idpk"] for m in reportes}) > 1:
            # Si el ledger cambia después de reportar (por ejemplo, porque la conciliación con
            # la wallet lo corrige), master manda una corrección con idpk nuevo: es lo esperado.
            r.info("hubo corrección del reporte (idpk nuevo); rige el último")
        status, detalle = local.master(f"/cycles/{ciclo}")
        if status == 200:
            r.info(
                f"swaps en el ciclo: {len(detalle.get('swaps', []))}; reporte: "
                f"{reportes[-1]['data'] if reportes else '—'}"
            )
    finally:
        central.cerrar()
        hgw.cerrar()
    r.terminar()


if __name__ == "__main__":
    main()
