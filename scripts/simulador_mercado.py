#!/usr/bin/env python3
"""Simulador de mercado local para pruebas y desarrollo (Tarea A6 - Rol A).

Permite inyectar en master local (a través de POST /internal/messages) o en un
broker RabbitMQ local/remoto los distintos tipos de mensajes del protocolo v3:
1. Ofertas del mercado (feed público): `offer`, `offer-updated`, `offer-withdrawn`.
2. Posturas directas de otras ciudades (bids):
   - Bids válidos de ciudades pares (ej: COR, HGW).
   - Bids anómalos con suplantación de identidad (`user_id != cityId`) para verificar
     la respuesta NACK IDENTITY_MISMATCH (403) de la Tarea A2 (Anomalía 2 de la demo).
   - Bids malformados para verificar el descarte y registro en /message-log (RF05).
3. Notificaciones centrales: `market-closed`.
4. Modo Ciudad Fantasma (MET): simula el comportamiento de MET vendiendo a
   imbalancePrice * 0.9 bajando de precio periódicamente y comprando la mejor oferta.

Uso:
    python3 scripts/simulador_mercado.py --mode feed
    python3 scripts/simulador_mercado.py --mode bids
    python3 scripts/simulador_mercado.py --mode ghost --iterations 3 --interval 2
    python3 scripts/simulador_mercado.py --mode anomalies
    python3 scripts/simulador_mercado.py --mode all
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("simulador_mercado")


def now_iso() -> str:
    """Devuelve la fecha actual en formato ISO 8601 UTC."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def post_to_master(base_url: str, message: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Envía un mensaje al endpoint interno de master usando solo la librería estándar."""
    url = f"{base_url.rstrip('/')}/internal/messages"
    data = json.dumps(message).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        try:
            return err.code, json.loads(body)
        except Exception:
            return err.code, {"error": body}
    except Exception as exc:
        return 0, {"error": str(exc)}


def post_to_broker(
    broker_url: str,
    exchange: str,
    routing_key: str,
    payload: dict[str, Any],
    user_id: str | None = None,
) -> bool:
    """Publica directamente al broker si se dispone de la librería pika."""
    try:
        import pika
    except ImportError:
        logger.error("pika no está instalado. Instálalo o usa el modo HTTP hacia master.")
        return False

    try:
        params = pika.URLParameters(broker_url)
        connection = pika.BlockingConnection(params)
        channel = connection.channel()

        props = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
            user_id=user_id,
        )
        channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=json.dumps(payload).encode("utf-8"),
            properties=props,
        )
        connection.close()
        logger.info(
            "Publicado a broker: exchange=%s rk=%s user_id=%s",
            exchange,
            routing_key,
            user_id,
        )
        return True
    except Exception as exc:
        logger.error("Fallo al publicar en broker: %s", exc)
        return False


# --- Creadores de mensajes v3 --------------------------------------------------------


def make_envelope(
    msg_type: str,
    cycle_id: str,
    city_id: str | None = None,
    sender: str | None = None,
    data: dict[str, Any] | None = None,
    idpk: str | None = None,
    msg_id: str | None = None,
) -> dict[str, Any]:
    """Construye un sobre compatible con la v3."""
    env: dict[str, Any] = {
        "idpk": idpk or str(uuid.uuid4()),
        "msgId": msg_id or str(uuid.uuid4()),
        "type": msg_type,
        "cycleId": cycle_id,
        "timestamp": now_iso(),
        "data": data or {},
    }
    if city_id:
        env["cityId"] = city_id
    if sender:
        env["sender"] = sender
    return env


def make_offer(
    cycle_id: str, city_id: str, offer_id: str, quantity: float, price: float
) -> dict[str, Any]:
    """Crea un mensaje offer del mercado público."""
    return {
        "type": "offer",
        "cycleId": cycle_id,
        "cityId": city_id,
        "data": {
            "offerId": offer_id,
            "quantity": quantity,
            "pricePerEnergy": price,
        },
    }


def make_offer_updated(
    cycle_id: str, city_id: str, offer_id: str, remaining: float, price: float
) -> dict[str, Any]:
    """Crea un mensaje offer-updated del mercado público."""
    return {
        "type": "offer-updated",
        "cycleId": cycle_id,
        "cityId": city_id,
        "data": {
            "offerId": offer_id,
            "remaining": remaining,
            "pricePerEnergy": price,
        },
    }


def make_offer_withdrawn(cycle_id: str, city_id: str, offer_id: str) -> dict[str, Any]:
    """Crea un mensaje offer-withdrawn."""
    return {
        "type": "offer-withdrawn",
        "cycleId": cycle_id,
        "cityId": city_id,
        "data": {
            "offerId": offer_id,
        },
    }


def make_market_closed(cycle_id: str) -> dict[str, Any]:
    """Crea un mensaje market-closed emitido por la central."""
    return {
        "type": "market-closed",
        "cycleId": cycle_id,
        "sender": "central",
    }


def make_bid(cycle_id: str, buyer_city: str, offer_id: str, quantity: float) -> dict[str, Any]:
    """Crea un bid directo entre ciudades con sobre estándar."""
    return make_envelope(
        msg_type="bid",
        cycle_id=cycle_id,
        city_id=buyer_city,
        data={
            "offerId": offer_id,
            "quantity": quantity,
        },
    )


# --- Escenarios de Simulación --------------------------------------------------------


def run_feed_scenario(base_url: str, cycle_id: str) -> None:
    """Inyecta un flujo típico de ofertas del feed público."""
    logger.info("=== Inyectando ofertas de mercado (feed) para ciclo %s ===", cycle_id)

    offers = [
        make_offer(cycle_id, "HGW", f"HGW-{cycle_id}", 15000, 210.0),
        make_offer(cycle_id, "COR", f"COR-{cycle_id}", 8000, 225.5),
        make_offer(cycle_id, "PUQ", f"PUQ-{cycle_id}", 12000, 205.0),
    ]

    for offer in offers:
        status, resp = post_to_master(base_url, offer)
        logger.info(
            "Oferta enviada: %s (%s kWh @ %s CR) -> HTTP %s outcome=%s",
            offer["data"]["offerId"],
            offer["data"]["quantity"],
            offer["data"]["pricePerEnergy"],
            status,
            resp.get("outcome"),
        )
        time.sleep(0.5)

    # Actualización de oferta
    update_msg = make_offer_updated(cycle_id, "HGW", f"HGW-{cycle_id}", 9000, 208.0)
    status, resp = post_to_master(base_url, update_msg)
    logger.info(
        "Actualización enviada: %s (queda %s kWh @ %s CR) -> HTTP %s outcome=%s",
        update_msg["data"]["offerId"],
        update_msg["data"]["remaining"],
        update_msg["data"]["pricePerEnergy"],
        status,
        resp.get("outcome"),
    )

    # Retiro de oferta
    withdraw_msg = make_offer_withdrawn(cycle_id, "COR", f"COR-{cycle_id}")
    status, resp = post_to_master(base_url, withdraw_msg)
    logger.info(
        "Retiro de oferta enviado: %s -> HTTP %s outcome=%s",
        withdraw_msg["data"]["offerId"],
        status,
        resp.get("outcome"),
    )


def run_bids_scenario(base_url: str, cycle_id: str, our_city: str) -> None:
    """Inyecta posturas de compra válidas hacia nuestra ciudad."""
    logger.info("=== Inyectando bids de compra hacia %s para ciclo %s ===", our_city, cycle_id)

    # 1. Bid válido de COR
    bid1 = make_bid(cycle_id, "COR", f"{our_city}-{cycle_id}", 3500)
    status, resp = post_to_master(base_url, bid1)
    logger.info(
        "Bid válido enviado desde COR (3500 kWh) -> HTTP %s outcome=%s", status, resp.get("outcome")
    )

    # 2. Reintento con mismo idpk (debe devolver duplicate sin romper nada)
    status, resp = post_to_master(base_url, bid1)
    logger.info(
        "Reintento con mismo idpk -> HTTP %s outcome=%s (esperado duplicate)",
        status,
        resp.get("outcome"),
    )


def run_anomalies_scenario(
    base_url: str, cycle_id: str, our_city: str, broker_url: str | None = None
) -> None:
    """Inyecta mensajes de prueba para las anomalías evaluadas en la demo."""
    logger.info("=== Inyectando anomalías de prueba (Anomalía 1 y 2) ===")

    # Anomalía 2: Bid con identidad suplantada (user_id de AMQP no coincide con cityId)
    # Si hay broker disponible, se publica con BasicProperties.user_id = 'city.ATTACKER'
    malicious_bid = make_bid(cycle_id, "HGW", f"{our_city}-{cycle_id}", 2000)
    if broker_url:
        logger.info("Publicando bid con user_id usurpado al broker (city.ATTACKER vs HGW)...")
        post_to_broker(
            broker_url,
            exchange="energy.x",
            routing_key=f"city.{our_city}",
            payload=malicious_bid,
            user_id="city.ATTACKER",
        )
        logger.info("Verifica en /message-log si connector respondió NACK IDENTITY_MISMATCH (403).")
    else:
        logger.info(
            "Nota: Para verificar el NACK de IDENTITY_MISMATCH por AMQP ejecuta con --broker-url."
        )

    # Mensaje malformado (sin datos obligatorios)
    malformed = {
        "type": "bid",
        "cycleId": cycle_id,
        "cityId": "COR",
        "data": {},  # falta offerId y quantity
    }
    status, resp = post_to_master(base_url, malformed)
    logger.info("Mensaje malformado enviado -> HTTP %s detalle=%s", status, resp)


def run_ghost_scenario(
    base_url: str,
    cycle_id: str,
    imbalance_price: float,
    iterations: int,
    interval: float,
) -> None:
    """Simula la ciudad fantasma MET:

    - Vende a imbalancePrice * 0.9 bajando en cada tick.
    - Publica offer y luego offer-updated periódicamente.
    """
    logger.info(
        "=== Iniciando simulación de Ciudad Fantasma MET "
        "(imbalance=%s, iteraciones=%s, intervalo=%ss) ===",
        imbalance_price,
        iterations,
        interval,
    )

    current_price = round(imbalance_price * 0.9, 2)
    offer_id = f"MET-{cycle_id}"
    quantity = 20000.0

    # 1. Oferta inicial
    initial_offer = make_offer(cycle_id, "MET", offer_id, quantity, current_price)
    status, resp = post_to_master(base_url, initial_offer)
    logger.info(
        "[MET] Publica oferta inicial: %s kWh @ %s CR -> HTTP %s outcome=%s",
        quantity,
        current_price,
        status,
        resp.get("outcome"),
    )

    # 2. Ticks de bajada de precio
    for i in range(1, iterations + 1):
        time.sleep(interval)
        current_price = round(max(current_price * 0.95, imbalance_price * 0.3), 2)
        remaining = max(quantity - (i * 2000), 5000.0)

        update = make_offer_updated(cycle_id, "MET", offer_id, remaining, current_price)
        status, resp = post_to_master(base_url, update)
        logger.info(
            "[MET Tick %s/%s] Baja precio: %s kWh remanentes @ %s CR -> HTTP %s outcome=%s",
            i,
            iterations,
            remaining,
            current_price,
            status,
            resp.get("outcome"),
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulador de mercado local y ciudad fantasma MET (Tarea A6)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
        help="URL base del servicio master local",
    )
    parser.add_argument(
        "--broker-url",
        default="",
        help="URL AMQP para publicar directo al broker (ej: amqps://...)",
    )
    parser.add_argument(
        "--cycle",
        default=f"cycle-sim-{datetime.now(UTC).strftime('%H%M%S')}",
        help="Identificador del ciclo a simular",
    )
    parser.add_argument(
        "--city",
        default="TAL",
        help="Código de nuestra ciudad receptora de bids",
    )
    parser.add_argument(
        "--mode",
        choices=["feed", "bids", "ghost", "met", "anomalies", "close", "all"],
        default="all",
        help="Modo de simulación a ejecutar",
    )
    parser.add_argument(
        "--imbalance-price",
        type=float,
        default=700.0,
        help="Precio de desbalance base para la simulación de MET",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Cantidad de ticks para la simulación de MET",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Segundos entre ticks de la ciudad fantasma",
    )

    args = parser.parse_args()

    logger.info("Arrancando simulador en modo '%s' contra %s", args.mode, args.url)

    if args.mode in ("feed", "all"):
        run_feed_scenario(args.url, args.cycle)

    if args.mode in ("bids", "all"):
        run_bids_scenario(args.url, args.cycle, args.city)

    if args.mode in ("anomalies", "all"):
        run_anomalies_scenario(args.url, args.cycle, args.city, args.broker_url or None)

    if args.mode in ("ghost", "met", "all"):
        run_ghost_scenario(
            args.url,
            args.cycle,
            args.imbalance_price,
            args.iterations,
            args.interval,
        )

    if args.mode in ("close", "all"):
        closed = make_market_closed(args.cycle)
        status, resp = post_to_master(args.url, closed)
        logger.info("Market-closed enviado -> HTTP %s outcome=%s", status, resp.get("outcome"))

    logger.info("=== Simulación completada exitosamente ===")


if __name__ == "__main__":
    main()
