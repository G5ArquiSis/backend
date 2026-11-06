#!/usr/bin/env python3
"""Ensayo de la anomalía 1: reenvío duplicado de un `swap-settled` o de un bid (mismo idpk).

Qué debe verse (enunciado E2 § Demo): el ledger no cambia dos veces, no se propone un segundo
swap y el duplicado queda registrado (RF05, `GET /message-log`).

    --tipo swap-settled (por omisión)
        Se liquida un swap real en la wallet falsa: HGW entrega kWh y TAL los paga. TAL acepta
        directo en la wallet, sin pasar por su nodo, así el ensayo no depende de la estrategia.
        El `swap-settled` le llega a TAL por `city.TAL.q`, y el script lo reenvía como la
        central: mismo idpk, msgId nuevo.
    --tipo bid
        TAL vende: HGW lee la oferta de TAL en el feed, manda un bid y lo reenvía con el mismo
        idpk. Debe haber una sola propuesta de swap en la wallet y la misma respuesta.

Reemplaza la versión de la E1 (demand-statement por HTTP, retirado en v3), que partió del
script de Vicente Pavez (PR #17).

Sólo contra el stack local con el perfil `fake` (scripts/ensayos/README.md):

    python3 scripts/demo_anomalia_1.py [--tipo swap-settled|bid]
"""

from __future__ import annotations

import argparse
import json
import time
import uuid

from ensayos import local
from ensayos.contraparte import Contraparte


def duplicar_swap_settled(r: local.Resultado, ciclo: str) -> None:
    r.paso("Swap real HGW → TAL en la wallet falsa (10 kWh por 3 300 CR)")
    local.wallet_admin("POST", "/market", {"kwhEnabled": True})
    status, propuesta = local.wallet_v1(
        "POST",
        "/swaps",
        "HGW",
        {
            "give": {"asset": "KWH", "amount": 10},
            "take": {"asset": "CR", "amount": 3300},
            "expiresAt": local.en(40),
            "memo": f"ensayo-anomalia-1-{ciclo}",
        },
    )
    if not r.chequeo(status == 202, "HGW propone el swap", f"{status}"):
        return
    swap_id = propuesta["swapId"]
    local.esperar(lambda: local.wallet_events("HGW", "swap-proposed"), 5)
    status, _ = local.wallet_v1("POST", f"/swaps/{swap_id}/accept", "TAL")
    r.chequeo(status == 202, "TAL acepta en la wallet", f"swapId {swap_id}")

    liquidado = local.esperar(
        lambda: next(
            (
                e
                for e in local.wallet_events("TAL", "swap-settled")
                if e["data"].get("swapId") == swap_id
            ),
            None,
        ),
        10,
    )
    if not r.chequeo(liquidado is not None, "la wallet emite swap-settled a TAL"):
        return
    r.info(f"idpk del swap-settled: {liquidado['idpk']}")
    time.sleep(3)  # que connector y master procesen el original
    antes = local.wallet_admin("GET", "/state")[1]["balances"]["TAL"]

    r.paso("La central reenvía el mismo swap-settled (mismo idpk, msgId nuevo)")
    duplicado = {**liquidado, "msgId": str(uuid.uuid4()), "timestamp": local.ahora()}
    central = local.Broker("central")
    try:
        central.a_ciudad("TAL", duplicado)
        ack = central.leer(
            "central.q",
            10,
            lambda m: (
                m.get("type") in {"ack", "nack"}
                and m.get("data", {}).get("target") == duplicado["msgId"]
            ),
        )
    finally:
        central.cerrar()
    r.chequeo(
        ack is not None and ack["type"] == "ack",
        "TAL responde ACK al duplicado",
        json.dumps(ack)[:160] if ack else "sin respuesta en central.q",
    )
    verificar_registro(r, liquidado["idpk"])
    despues = local.wallet_admin("GET", "/state")[1]["balances"]["TAL"]
    r.chequeo(antes == despues, "la wallet no cambió (no hubo un segundo movimiento)")
    detalle_ciclo(r, ciclo, liquidado["idpk"])


def duplicar_bid(r: local.Resultado, ciclo: str, espera: float) -> None:
    hgw = Contraparte(ciclo)
    try:
        hgw.vaciar()
        r.paso(f"HGW espera la oferta de TAL en el feed (hasta {espera:.0f} s)")
        oferta = hgw.esperar_oferta("TAL", espera)
        if not r.chequeo(
            oferta is not None, "TAL publicó una oferta", "¿hay excedente y mercado abierto?"
        ):
            return
        offer_id = oferta["data"]["offerId"]
        cantidad = max(1, min(100, int(oferta["data"]["quantity"])))
        r.info(
            f"oferta {offer_id}: {oferta['data']['quantity']} kWh a "
            f"{oferta['data']['pricePerEnergy']}"
        )

        r.paso(f"HGW manda un bid de {cantidad} kWh y lo reenvía con el mismo idpk")
        primero = hgw.pujar("TAL", offer_id, cantidad)
        respuesta_1 = hgw.esperar_respuesta(primero, 15)
        segundo = hgw.pujar("TAL", offer_id, cantidad, idpk=primero["idpk"])
        respuesta_2 = hgw.esperar_respuesta(segundo, 15)
        tipo_1 = respuesta_1 and respuesta_1["type"]
        tipo_2 = respuesta_2 and respuesta_2["type"]
        r.chequeo(tipo_1 is not None, "TAL responde el primer bid", str(tipo_1))
        r.chequeo(tipo_1 == tipo_2, "el reenvío recibe la misma respuesta", f"{tipo_1} / {tipo_2}")
        if tipo_1 == "bid-accepted" and tipo_2 == "bid-accepted":
            r.chequeo(
                respuesta_1["data"]["swapId"] == respuesta_2["data"]["swapId"],
                "las dos respuestas apuntan al mismo swap",
            )
        propuestas = [
            s
            for s in local.wallet_admin("GET", "/state")[1]["swaps"]
            if s["proposer"] == "TAL" and s["memo"] == offer_id
        ]
        r.chequeo(
            len(propuestas) <= 1,
            "TAL propuso un solo swap para ese bid",
            f"{len(propuestas)} propuestas con memo {offer_id}",
        )
        verificar_registro(r, primero["idpk"])
    finally:
        hgw.cerrar()


def verificar_registro(r: local.Resultado, idpk: str) -> None:
    entrada = local.esperar(
        lambda: next(
            (
                e
                for e in local.message_log()
                if e.get("idpk") == idpk and e.get("category") == "duplicate"
            ),
            None,
        ),
        10,
    )
    r.chequeo(
        entrada is not None, "el duplicado quedó en /message-log como duplicate", f"idpk {idpk}"
    )


def detalle_ciclo(r: local.Resultado, ciclo: str, idpk: str) -> None:
    status, detalle = local.master(f"/cycles/{ciclo}")
    if status != 200:
        r.info(f"GET /cycles/{ciclo} → {status}: no se puede contar los eventos del ciclo")
        return
    eventos = [e for e in detalle.get("walletEvents", []) if idpk in json.dumps(e)]
    r.chequeo(
        len(eventos) == 1,
        "el ciclo muestra el swap-settled una sola vez",
        f"{len(eventos)} eventos con ese idpk",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tipo", choices=["swap-settled", "bid"], default="swap-settled")
    parser.add_argument(
        "--espera-oferta",
        type=float,
        default=120,
        help="segundos esperando la oferta de TAL (--tipo bid)",
    )
    args = parser.parse_args()

    local.exigir_local()
    r = local.Resultado("Anomalía 1")
    local.stack_arriba(r)
    ciclo = local.ciclo_actual()
    r.info(f"ciclo: {ciclo}")
    if args.tipo == "swap-settled":
        duplicar_swap_settled(r, ciclo)
    else:
        duplicar_bid(r, ciclo, args.espera_oferta)
    r.terminar()


if __name__ == "__main__":
    main()
