#!/usr/bin/env python3
"""Ensayo de la anomalía 2: bid con `user_id` ajeno o mensaje malformado de otra ciudad.

Qué debe verse: NACK o descarte según la regla, sin caída del servicio. HGW (conexión
`city.HGW` del broker local) le manda a TAL:

1. Un bid que dice venir de MET (`cityId` MET, `user_id` city.HGW) → `nack` IDENTITY_MISMATCH 403.
2. Un bid sin `user_id` en las propiedades AMQP → `nack` IDENTITY_MISMATCH 403.
3. Un bid con `quantity` no entera → `nack` MALFORMED_MESSAGE 422.
4. Un bid sin `idpk` → `nack` MALFORMED_MESSAGE 422.
5. Bytes que no son JSON → se descarta sin responder (no hay `msgId` al cual responder).

Al final, master sigue respondiendo y connector sigue `healthy`. Cada rechazo queda en
`/message-log` (RF05). El broker no deja publicar con un `user_id` distinto del usuario
autenticado, por eso el "ajeno" se prueba con el `cityId` (1) y con su ausencia (2).

    python3 scripts/demo_anomalia_2.py
"""

from __future__ import annotations

import argparse
import time

from ensayos import local
from ensayos.contraparte import COLA, Contraparte

CASOS = [
    ("bid de MET publicado por HGW", "IDENTITY_MISMATCH", 403),
    ("bid sin user_id", "IDENTITY_MISMATCH", 403),
    ("bid con quantity no entera", "MALFORMED_MESSAGE", 422),
    ("bid sin idpk", "MALFORMED_MESSAGE", 422),
]


def main() -> None:
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args()
    local.exigir_local()
    r = local.Resultado("Anomalía 2")
    local.stack_arriba(r)
    ciclo = local.ciclo_actual()
    hgw = Contraparte(ciclo)
    log_antes = {e["id"] for e in local.message_log()}
    try:
        hgw.vaciar()
        enviados = []

        suplantado = local.mensaje(
            "bid", {"offerId": f"TAL-{ciclo}", "quantity": 10}, ciudad="MET", ciclo=ciclo
        )
        hgw.broker.a_ciudad("TAL", suplantado)
        enviados.append(suplantado)

        sin_user_id = local.mensaje(
            "bid", {"offerId": f"TAL-{ciclo}", "quantity": 10}, ciudad="HGW", ciclo=ciclo
        )
        hgw.broker.a_ciudad("TAL", sin_user_id, user_id=None)
        enviados.append(sin_user_id)

        fraccion = local.mensaje(
            "bid", {"offerId": f"TAL-{ciclo}", "quantity": 10.5}, ciudad="HGW", ciclo=ciclo
        )
        hgw.broker.a_ciudad("TAL", fraccion)
        enviados.append(fraccion)

        sin_idpk = local.mensaje(
            "bid", {"offerId": f"TAL-{ciclo}", "quantity": 10}, ciudad="HGW", ciclo=ciclo
        )
        del sin_idpk["idpk"]
        hgw.broker.a_ciudad("TAL", sin_idpk)
        enviados.append(sin_idpk)

        hgw.broker.a_ciudad("TAL", b"{esto no es json")

        for (titulo, razon, codigo), enviado in zip(CASOS, enviados, strict=True):
            r.paso(titulo)
            nack = hgw.broker.leer(
                COLA,
                15,
                lambda m, target=enviado["msgId"]: (
                    m.get("type") == "nack" and m.get("data", {}).get("target") == target
                ),
            )
            visto = f"{nack.get('reason')} {nack.get('code')}" if nack else "sin nack en city.HGW.q"
            r.chequeo(
                nack is not None and (nack.get("reason"), nack.get("code")) == (razon, codigo),
                f"nack {razon} {codigo}",
                visto,
            )

        r.paso("Bytes que no son JSON")
        time.sleep(2)
        nuevos = [e for e in local.message_log() if e["id"] not in log_antes]
        r.chequeo(
            any(
                e["category"] == "discarded" and "esto no es json" in e["rawContent"]
                for e in nuevos
            ),
            "se descartó y quedó en /message-log como discarded",
        )
        r.chequeo(
            sum(e["category"] == "nack" for e in nuevos) >= len(CASOS),
            "los rechazos quedaron en /message-log como nack",
            f"{sum(e['category'] == 'nack' for e in nuevos)} nuevos",
        )
    finally:
        hgw.cerrar()

    r.paso("El servicio sigue arriba")
    r.chequeo(local.master("/health")[0] == 200, "master responde /health")
    estado = local.estado_contenedor("connector")
    r.chequeo(estado in {"healthy", "running"}, "connector sigue corriendo", estado)
    r.terminar()


if __name__ == "__main__":
    main()
