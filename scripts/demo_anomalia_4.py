#!/usr/bin/env python3
"""Ensayo de la anomalía 4: corte tras aceptar (timeout de una escritura que sí se procesó).

Qué debe verse: el nodo converge al estado real sin duplicar la operación.

La wallet falsa tiene una falla `504` que ejecuta la escritura y pierde la respuesta: TAL ve un
timeout, pero en la wallet la operación ocurrió. El cliente debe reintentar con la misma
Idempotency-Key (la wallet devuelve el mismo resultado) o reconciliar con `GET /swaps/{id}`.

    --operacion accept (por omisión; TAL compra)
        HGW ofrece, TAL puja, HGW propone el swap y manda `bid-accepted`. El `accept` de TAL
        recibe 504 una vez. Debe quedar un solo swap liquidado y el ledger de TAL al día.
    --operacion propose (TAL vende)
        HGW puja por la oferta de TAL. El `propose` de TAL recibe 504 una vez. Debe haber una
        sola propuesta de TAL para ese bid (no dos swaps por el mismo kWh).

    python3 scripts/demo_anomalia_4.py [--operacion accept|propose] [--precio 100]
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal

from ensayos import local
from ensayos.contraparte import Contraparte


def fallar_una_vez(r: local.Resultado, operacion: str) -> None:
    local.wallet_admin("DELETE", "/faults")
    status, activas = local.wallet_admin(
        "POST", "/faults", {"kind": "504", "operations": [operacion], "times": 1}
    )
    r.chequeo(status == 200, f"falla 504 activa para el próximo {operacion}", json.dumps(activas))


def falla_consumida() -> bool:
    return local.wallet_admin("GET", "/faults")[1] == []


def tal_compra(r: local.Resultado, hgw: Contraparte, precio: Decimal, espera: float) -> None:
    r.paso(f"HGW ofrece 5 000 kWh a {precio} y espera el bid de TAL (hasta {espera:.0f} s)")
    oferta = hgw.ofrecer(5000, precio)
    bid = hgw.esperar_bid(espera, oferta["data"]["offerId"])
    if not r.chequeo(bid is not None, "TAL puja por la oferta de HGW"):
        return
    hgw.acusar_recibo(bid)
    swap = hgw.proponer_swap(bid, precio)
    r.info(f"HGW propone {swap['swapId']}: {swap['quantity']} kWh por {swap['amount']} CR")

    fallar_una_vez(r, "accept")
    hgw.aceptar_bid(bid, precio, swap)

    r.paso("TAL acepta; la wallet procesa y responde 504")
    r.chequeo(local.esperar(falla_consumida, 30) is not None, "TAL llamó a accept (falla usada)")
    liquidados = local.esperar(
        lambda: [
            e
            for e in local.wallet_events("TAL", "swap-settled")
            if e["data"].get("swapId") == swap["swapId"]
        ],
        30,
    )
    r.chequeo(bool(liquidados), "el swap quedó liquidado en la wallet")
    rechazos = [
        e
        for e in local.wallet_events("TAL", "swap-rejected")
        if e["data"].get("swapId") == swap["swapId"]
    ]
    r.chequeo(
        not rechazos,
        "ningún accept repetido fue rechazado (misma Idempotency-Key)",
        ", ".join(e.get("reason", "?") for e in rechazos),
    )
    convergencia(r, swap["swapId"])


def tal_vende(r: local.Resultado, hgw: Contraparte, espera: float) -> None:
    r.paso(f"HGW espera la oferta de TAL (hasta {espera:.0f} s)")
    oferta = hgw.esperar_oferta("TAL", espera)
    if not r.chequeo(oferta is not None, "TAL publicó una oferta"):
        return
    fallar_una_vez(r, "propose")
    cantidad = max(1, min(100, int(oferta["data"]["quantity"])))
    bid = hgw.pujar("TAL", oferta["data"]["offerId"], cantidad)

    r.paso("TAL propone el swap; la wallet lo registra y responde 504")
    respuesta = hgw.esperar_respuesta(bid, 45)
    r.chequeo(local.esperar(falla_consumida, 5) is not None, "TAL llamó a propose (falla usada)")
    r.chequeo(
        respuesta is not None and respuesta["type"] == "bid-accepted",
        "TAL termina respondiendo bid-accepted",
        respuesta["type"] if respuesta else "nada",
    )
    propuestas = [
        s
        for s in local.wallet_admin("GET", "/state")[1]["swaps"]
        if s["proposer"] == "TAL" and s["memo"] == oferta["data"]["offerId"]
    ]
    r.chequeo(
        len(propuestas) == 1,
        "una sola propuesta de TAL para el bid",
        f"{len(propuestas)}: {[s['status'] for s in propuestas]}",
    )
    if respuesta and respuesta["type"] == "bid-accepted":
        status, _ = hgw.aceptar_swap(respuesta["data"]["swapId"])
        r.chequeo(status == 202, "HGW acepta el swap")
        convergencia(r, respuesta["data"]["swapId"])


def convergencia(r: local.Resultado, swap_id: str) -> None:
    r.paso("El nodo converge")
    status, saldo = local.master("/wallet/balance-status")
    if status == 200:
        r.info(f"balance oficial en master: {json.dumps(saldo.get('assets'))[:200]}")
    estado_real = local.wallet_admin("GET", "/state")[1]["balances"]["TAL"]
    r.info(f"wallet real de TAL: {json.dumps(estado_real)}")
    ciclo = local.ciclo_actual()
    status, detalle = local.master(f"/cycles/{ciclo}")
    if status == 200:
        swaps = [s for s in detalle.get("swaps", []) if swap_id in json.dumps(s)]
        r.chequeo(len(swaps) == 1, "el ciclo muestra el swap una sola vez", json.dumps(swaps)[:200])
    else:
        r.info(f"GET /cycles/{ciclo} → {status}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--operacion", choices=["accept", "propose"], default="accept")
    parser.add_argument("--precio", type=Decimal, default=Decimal("100"))
    parser.add_argument("--espera", type=float, default=90)
    args = parser.parse_args()

    local.exigir_local()
    r = local.Resultado("Anomalía 4")
    local.stack_arriba(r)
    local.wallet_admin("POST", "/market", {"kwhEnabled": True})
    hgw = Contraparte(local.ciclo_actual())
    try:
        hgw.vaciar()
        if args.operacion == "accept":
            tal_compra(r, hgw, args.precio, args.espera)
        else:
            tal_vende(r, hgw, args.espera)
    finally:
        local.wallet_admin("DELETE", "/faults")
        hgw.cerrar()
    r.terminar()


if __name__ == "__main__":
    main()
