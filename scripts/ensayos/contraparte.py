"""HGW simulada: la otra ciudad de los ensayos, que hace de vendedor o de comprador frente a TAL.

Sigue el flujo del protocolo v3 (enunciado E2 § 2): el vendedor publica `offer` en `market`, el
comprador manda `bid` a `city.{vendedor}`, el vendedor propone el swap en la wallet y responde
`bid-accepted` con `swapId` y `swapToken`, y el comprador acepta en la wallet. Cada paso es un
método, para que un ensayo pueda saltarse justo el que la anomalía pide no cumplir.

Usa la identidad `city.HGW` del broker local y la API key `fake-key-hgw` de la wallet falsa.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from ensayos import local

CIUDAD = "HGW"
COLA = "city.HGW.q"
FEED = "market.HGW"


def round2(valor: Decimal) -> Decimal:
    return valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def numero(valor: Decimal) -> int | float:
    return int(valor) if valor == valor.to_integral_value() else float(valor)


class Contraparte:
    def __init__(self, ciclo: str):
        self.ciclo = ciclo
        self.broker = local.Broker("city.HGW")

    def cerrar(self) -> None:
        self.broker.cerrar()

    def vaciar(self) -> None:
        """Descarta lo viejo de las colas de HGW, para no confundirlo con lo del ensayo."""
        self.broker.vaciar(COLA)
        self.broker.vaciar(FEED)

    # --- HGW vendedor --------------------------------------------------------------------

    def ofrecer(self, cantidad: int, precio: Decimal) -> dict:
        oferta = local.mensaje(
            "offer",
            {
                "offerId": f"HGW-{self.ciclo}",
                "quantity": cantidad,
                "pricePerEnergy": numero(precio),
            },
            ciudad=CIUDAD,
            ciclo=self.ciclo,
        )
        self.broker.publicar("market", f"market.offer.{CIUDAD}", oferta)
        return oferta

    def esperar_bid(self, segundos: float, offer_id: str | None = None) -> dict | None:
        def es_bid(m: dict) -> bool:
            return m.get("type") == "bid" and (
                offer_id is None or m.get("data", {}).get("offerId") == offer_id
            )

        return self.broker.leer(COLA, segundos, es_bid)

    def acusar_recibo(self, recibido: dict) -> None:
        self.broker.a_ciudad(
            recibido["cityId"], local.mensaje("ack", {"target": recibido["msgId"]}, ciudad=CIUDAD)
        )

    def proponer_swap(self, bid: dict, precio: Decimal, segundos: int = 40) -> dict:
        """Propone en la wallet el swap del bid. Devuelve `{swapId, token, expiresAt, amount}`."""
        cantidad = int(bid["data"]["quantity"])
        monto = round2(Decimal(cantidad) * precio)
        expira = local.en(segundos)
        status, cuerpo = local.wallet_v1(
            "POST",
            "/swaps",
            CIUDAD,
            {
                "give": {"asset": "KWH", "amount": cantidad},
                "take": {"asset": "CR", "amount": numero(monto)},
                "expiresAt": expira,
                "memo": bid["data"]["offerId"],
            },
        )
        if status != 202:
            raise RuntimeError(f"la wallet falsa rechazó la propuesta de HGW: {status} {cuerpo}")
        swap_id = cuerpo["swapId"]
        propuesto = local.esperar(
            lambda: next(
                (
                    e
                    for e in local.wallet_events(CIUDAD, "swap-proposed")
                    if e["data"].get("swapId") == swap_id
                ),
                None,
            ),
            10,
        )
        if propuesto is None:
            raise RuntimeError(f"no llegó swap-proposed de {swap_id}")
        return {
            "swapId": swap_id,
            "token": propuesto["data"]["token"],
            "expiresAt": expira,
            "amount": monto,
            "quantity": cantidad,
        }

    def aceptar_bid(self, bid: dict, precio: Decimal, swap: dict) -> dict:
        aceptado = local.mensaje(
            "bid-accepted",
            {
                "target": bid["msgId"],
                "offerId": bid["data"]["offerId"],
                "quantity": swap["quantity"],
                "pricePerEnergy": numero(precio),
                "amount": numero(swap["amount"]),
                "swapId": swap["swapId"],
                "swapToken": swap["token"],
                "expiresAt": swap["expiresAt"],
            },
            ciudad=CIUDAD,
            ciclo=self.ciclo,
        )
        self.broker.a_ciudad(bid["cityId"], aceptado)
        return aceptado

    # --- HGW comprador -------------------------------------------------------------------

    def esperar_oferta(self, de: str, segundos: float) -> dict | None:
        return self.broker.leer(
            FEED, segundos, lambda m: m.get("type") == "offer" and m.get("cityId") == de
        )

    def pujar(self, a: str, offer_id: str, cantidad: int, idpk: str | None = None) -> dict:
        bid = local.mensaje(
            "bid",
            {"offerId": offer_id, "quantity": cantidad},
            ciudad=CIUDAD,
            ciclo=self.ciclo,
            idpk=idpk,
        )
        self.broker.a_ciudad(a, bid)
        return bid

    def esperar_respuesta(self, bid: dict, segundos: float) -> dict | None:
        """`bid-accepted` o `bid-rejected` a ese bid (por `data.target`), o un nack."""
        return self.broker.leer(
            COLA,
            segundos,
            lambda m: (
                m.get("type") in {"bid-accepted", "bid-rejected", "nack", "error"}
                and m.get("data", {}).get("target") == bid["msgId"]
            ),
        )

    def aceptar_swap(self, swap_id: str) -> tuple[int, object]:
        return local.wallet_v1("POST", f"/swaps/{swap_id}/accept", CIUDAD)
