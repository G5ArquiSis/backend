#!/usr/bin/env python3
"""Experimento de span en el stack local (rol D, tarea D7; RF06).

Corre ciclos comprimidos en que TAL tiene déficit (12 000 kWh) frente a un mercado controlado
de HGW con dos ofertas:

- **A, barata y lenta**: 6 000 kWh a 300 CR/kWh. HGW responde cada bid a esta oferta después
  de una latencia fija del escenario (o nunca).
- **B, cara y rápida**: 12 000 kWh a 520 CR/kWh. HGW responde en 3 s.

`imbalancePrice` es 735, así que ambas convienen frente al desbalance. Para cada span y cada
latencia de A se fija el span en `PUT /strategy` antes de abrir el ciclo (la estrategia se
congela por ciclo, AD6), se deja correr el mercado completo y al final se lee
`GET /experiments/span` filtrando los ciclos de esta corrida. Mismo diseño pareado para cada
span: los mismos escenarios de latencia, así la diferencia entre spans no es ruido del mercado.

    python3 scripts/experimento_span.py --spans 10 30 90 --latencias 20 60 nunca \\
        --salida /tmp/experimento-span.json

Sólo corre contra el stack local con el perfil `fake` (ver `scripts/ensayos/local.py`).
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime
from decimal import Decimal

from ensayos import local
from ensayos.contraparte import COLA, Contraparte, numero

PRECIO_A = Decimal("300")
PRECIO_B = Decimal("520")
CANTIDAD_A = 6000
CANTIDAD_B = 12000
LATENCIA_B = 3.0


def fijar_span(span: int) -> None:
    status, actual = local.master("/strategy")
    if status != 200:
        raise RuntimeError(f"GET /strategy respondió {status}")
    config = {**actual["config"], "buyWaitSpanSeconds": span}
    status, cuerpo = local.http("PUT", f"{local.MASTER}/strategy", config)
    if status != 200:
        raise RuntimeError(f"PUT /strategy respondió {status}: {cuerpo}")


def ofrecer(hgw: Contraparte, sufijo: str, cantidad: int, precio: Decimal) -> str:
    offer_id = f"HGW-{hgw.ciclo}-{sufijo}"
    hgw.broker.publicar(
        "market",
        "market.offer.HGW",
        local.mensaje(
            "offer",
            {"offerId": offer_id, "quantity": cantidad, "pricePerEnergy": numero(precio)},
            ciudad="HGW",
            ciclo=hgw.ciclo,
        ),
    )
    return offer_id


def correr_ciclo(span: int, latencia: float | None, mercado: float) -> dict:
    fijar_span(span)
    # Crédito de sobra para que el presupuesto no limite la compra, y kWh para que HGW pueda
    # proponer los swaps (incluidos los que TAL ya no acepta por llegar tarde).
    local.wallet_admin("POST", "/accounts/TAL", {"cr": 10_000_000})
    local.wallet_admin("POST", "/accounts/HGW", {"kwh": 200_000})
    hgw = Contraparte("")
    try:
        hgw.vaciar()
        # La central sólo abre el ciclo: una conexión ociosa todo el mercado la cierra el broker
        # por heartbeat, así que se abre y se cierra acá.
        central = local.Broker("central")
        try:
            central.vaciar("central.q")
            hgw.ciclo = local.mercado_comprador(central, f"span{span}", mercado)
        finally:
            central.cerrar()
        inicio = time.monotonic()
        oferta_a = ofrecer(hgw, "A", CANTIDAD_A, PRECIO_A)
        oferta_b = ofrecer(hgw, "B", CANTIDAD_B, PRECIO_B)
        precios = {oferta_a: PRECIO_A, oferta_b: PRECIO_B}
        print(f"   ciclo {hgw.ciclo}: span {span} s, latencia de A {fmt(latencia)}", flush=True)

        pendientes: dict[str, dict] = {}  # idpk -> {vence, bid}
        respondidos: dict[str, dict] = {}  # idpk -> swap propuesto
        bids_a = bids_b = respuestas_tarde = 0
        while time.monotonic() - inicio < mercado + 5:
            bid = hgw.broker.leer(COLA, 0.5, lambda m: m.get("type") == "bid")
            ahora = time.monotonic()
            if bid is not None:
                offer_id = bid["data"]["offerId"]
                idpk = bid["idpk"]
                if idpk in respondidos:
                    # Reenvío de un bid ya respondido: misma respuesta, al msgId nuevo.
                    hgw.aceptar_bid(bid, precios[offer_id], respondidos[idpk])
                elif idpk in pendientes:
                    pendientes[idpk]["bid"] = bid
                else:
                    espera = LATENCIA_B if offer_id == oferta_b else latencia
                    bids_a += offer_id == oferta_a
                    bids_b += offer_id == oferta_b
                    if espera is not None:
                        pendientes[idpk] = {"vence": ahora + espera, "bid": bid}
            for idpk, item in list(pendientes.items()):
                if item["vence"] > ahora:
                    continue
                del pendientes[idpk]
                bid = item["bid"]
                precio = precios[bid["data"]["offerId"]]
                swap = hgw.proponer_swap(bid, precio, segundos=40)
                hgw.aceptar_bid(bid, precio, swap)
                respondidos[idpk] = swap
                if not swap_aceptado_a_tiempo(swap["swapId"]):
                    respuestas_tarde += 1
        # Que alcancen a llegar los swap-settled de lo último aceptado.
        time.sleep(15)
        return {
            "cycleId": hgw.ciclo,
            "span": span,
            "latenciaA": latencia,
            "bidsDistintosA": bids_a,
            "bidsDistintosB": bids_b,
            "respuestasSinAceptar": respuestas_tarde,
        }
    finally:
        hgw.cerrar()


def swap_aceptado_a_tiempo(swap_id: str) -> bool:
    """True si TAL aceptó el swap en pocos segundos (si llegó tarde, el bid ya venció)."""
    return bool(
        local.esperar(
            lambda: next(
                (
                    s
                    for s in local.wallet_admin("GET", "/state")[1]["swaps"]
                    if s["swapId"] == swap_id and s["status"] != "proposed"
                ),
                None,
            ),
            10,
        )
    )


def fmt(latencia: float | None) -> str:
    return "nunca" if latencia is None else f"{latencia:.0f} s"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--spans", type=int, nargs="+", default=[10, 30, 90])
    parser.add_argument("--latencias", nargs="+", default=["20", "60", "nunca"])
    parser.add_argument("--mercado", type=float, default=300, help="segundos de mercado")
    parser.add_argument("--salida", default="experimento-span.json")
    args = parser.parse_args()
    latencias = [None if x == "nunca" else float(x) for x in args.latencias]

    local.exigir_local()
    r = local.Resultado("Experimento de span")
    local.stack_arriba(r)
    local.wallet_admin("POST", "/market", {"kwhEnabled": True})
    span_original = local.master("/strategy")[1]["config"]["buyWaitSpanSeconds"]
    corrida = str(uuid.uuid4())[:8]
    print(f"corrida {corrida}, {datetime.now().isoformat(timespec='seconds')}", flush=True)

    escenarios = []
    try:
        for span in args.spans:
            for latencia in latencias:
                escenarios.append(correr_ciclo(span, latencia, args.mercado))
    finally:
        fijar_span(span_original)

    _, experimento = local.master("/experiments/span")
    por_ciclo = {c["cycleId"]: c for c in experimento["cycles"]}
    for escenario in escenarios:
        escenario["medido"] = por_ciclo.get(escenario["cycleId"])
    with open(args.salida, "w") as archivo:
        json.dump(
            {"corrida": corrida, "mercado": args.mercado, "ciclos": escenarios}, archivo, indent=2
        )
    print(f"\nresultados en {args.salida}")
    for e in escenarios:
        m = e["medido"] or {}
        print(
            f"span {e['span']:>3} · A {fmt(e['latenciaA']):>6} · cubierto {m.get('covered')}"
            f" · costo {m.get('effectiveCost')} · bids {m.get('bidsSent')}"
            f" · rechazados {m.get('bidsRejected')}"
        )


if __name__ == "__main__":
    main()
