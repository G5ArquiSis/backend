#!/usr/bin/env python3
"""Ensayo de la anomalía 3: contraparte que no cumple.

Qué debe verse: el nodo sigue dentro de la ventana y no queda nada retenido.

    --caso comprador-no-acepta (TAL vende)
        HGW lee la oferta de TAL, manda un bid y recibe `bid-accepted` con el swap, pero nunca
        lo acepta. Al vencer `expiresAt` el swap debe quedar `expired`, los kWh de TAL ya no
        retenidos (`held` 0) y TAL debe volver a ofrecer esa cantidad (`offer-updated`).
    --caso vendedor-no-propone (TAL compra)
        HGW publica una oferta barata, recibe el bid de TAL, le acusa recibo y no hace nada más:
        nunca propone ni responde. TAL no debe quedar con CR retenidos ni bloqueado: pasado el
        plazo debería seguir operando (por ejemplo, pujar en otra oferta).

    python3 scripts/demo_anomalia_3.py --caso comprador-no-acepta
    python3 scripts/demo_anomalia_3.py --caso vendedor-no-propone [--precio 100]
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from decimal import Decimal

from ensayos import local
from ensayos.contraparte import FEED, Contraparte


def retenido(ciudad: str, activo: str) -> Decimal:
    balance = local.wallet_balance(ciudad).get(activo, {})
    return Decimal(str(balance.get("held", 0)))


def comprador_no_acepta(r: local.Resultado, hgw: Contraparte, espera: float) -> None:
    r.paso(f"HGW espera la oferta de TAL (hasta {espera:.0f} s)")
    oferta = hgw.esperar_oferta("TAL", espera)
    if not r.chequeo(oferta is not None, "TAL publicó una oferta"):
        return
    cantidad = max(1, min(100, int(oferta["data"]["quantity"])))
    bid = hgw.pujar("TAL", oferta["data"]["offerId"], cantidad)
    r.info(f"bid de {cantidad} kWh a {oferta['data']['offerId']}")

    respuesta = hgw.esperar_respuesta(bid, 20)
    if not r.chequeo(
        respuesta is not None and respuesta["type"] == "bid-accepted",
        "TAL acepta el bid (propone el swap)",
        respuesta["type"] if respuesta else "sin respuesta",
    ):
        return
    swap_id = respuesta["data"]["swapId"]
    expira = datetime.fromisoformat(respuesta["data"]["expiresAt"].replace("Z", "+00:00"))
    r.info(f"swap {swap_id}, vence {respuesta['data']['expiresAt']}; HGW no lo acepta")
    r.chequeo(retenido("TAL", "KWH") >= cantidad, "mientras tanto, los kWh de TAL están retenidos")

    r.paso("Esperando a que venza el swap")
    time.sleep(max(0.0, (expira - datetime.now(expira.tzinfo)).total_seconds()) + 3)
    swaps = {s["swapId"]: s for s in local.wallet_admin("GET", "/state")[1]["swaps"]}
    r.chequeo(
        swaps.get(swap_id, {}).get("status") == "expired",
        "el swap quedó expired",
        swaps.get(swap_id, {}).get("status", "?"),
    )
    r.chequeo(retenido("TAL", "KWH") == 0, "TAL no tiene kWh retenidos")
    reoferta = hgw.broker.leer(
        FEED, 30, lambda m: m.get("type") == "offer-updated" and m.get("cityId") == "TAL"
    )
    r.chequeo(
        reoferta is not None,
        "TAL vuelve a ofrecer (offer-updated)",
        f"remaining {reoferta['data'].get('remaining')}" if reoferta else "no llegó",
    )


def vendedor_no_propone(
    r: local.Resultado, hgw: Contraparte, precio: Decimal, espera: float
) -> None:
    r.paso(f"HGW publica una oferta de 5 000 kWh a {precio} y espera el bid de TAL")
    oferta = hgw.ofrecer(5000, precio)
    bid = hgw.esperar_bid(espera, oferta["data"]["offerId"])
    if not r.chequeo(
        bid is not None,
        "TAL puja por la oferta de HGW",
        "¿TAL tiene déficit y el mercado está abierto?",
    ):
        return
    hgw.acusar_recibo(bid)
    r.info(f"bid de {bid['data']['quantity']} kWh; HGW acusa recibo y nunca propone")
    r.chequeo(retenido("TAL", "CR") == 0, "TAL no retiene CR por un bid sin swap")

    r.paso(f"HGW se queda callado {espera:.0f} s")
    otro = hgw.esperar_bid(espera, oferta["data"]["offerId"])
    r.info("TAL reintentó el bid" if otro else "TAL no volvió a pujar por esta oferta")
    r.chequeo(retenido("TAL", "CR") == 0, "TAL sigue sin CR retenidos")
    r.chequeo(local.master("/health")[0] == 200, "master sigue respondiendo")
    hgw.broker.publicar(
        "market",
        "market.offer.HGW",
        local.mensaje(
            "offer-withdrawn", {"offerId": oferta["data"]["offerId"]}, ciudad="HGW", ciclo=hgw.ciclo
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--caso",
        choices=["comprador-no-acepta", "vendedor-no-propone"],
        default="comprador-no-acepta",
    )
    parser.add_argument(
        "--precio",
        type=Decimal,
        default=Decimal("100"),
        help="precio de la oferta de HGW (vendedor-no-propone)",
    )
    parser.add_argument("--espera", type=float, default=90, help="segundos de cada espera")
    args = parser.parse_args()

    local.exigir_local()
    r = local.Resultado("Anomalía 3")
    local.stack_arriba(r)
    local.wallet_admin("POST", "/market", {"kwhEnabled": True})
    hgw = Contraparte(local.ciclo_actual())
    try:
        hgw.vaciar()
        if args.caso == "comprador-no-acepta":
            comprador_no_acepta(r, hgw, args.espera)
        else:
            vendedor_no_propone(r, hgw, args.precio, args.espera)
    finally:
        hgw.cerrar()
    r.terminar()


if __name__ == "__main__":
    main()
