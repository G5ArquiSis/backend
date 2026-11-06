"""Ciclo de vida del consumo AMQP: conexión, reconexión, validación, ack y publicación.

Este módulo implementa el quantum connector (protocolo v3):
- Observer: consume demand-set de la E0 por su cola dedicada.
- Ciudad: consume city.{CODE}.q con validación de identidad (user_id == cityId),
  manejo de tipos retirados, entrega a master, confirmación ACK/NACK y publicación
  generalizada desde la outbox con limitador de tasa deslizante.
- Mercado: consume market.{CODE} (feed público v3) drenando rápido hacia master
  sin responder con ACK a nivel de aplicación.
"""

from __future__ import annotations

import json
import logging
import signal
import ssl
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

try:
    import pika
    from pika.adapters.blocking_connection import BlockingChannel
    from pika.exceptions import AMQPError, NackError, UnroutableError
except ImportError:
    pika = None  # type: ignore
    BlockingChannel = Any  # type: ignore

    class AMQPError(Exception):  # type: ignore
        pass

    class NackError(AMQPError):  # type: ignore
        pass

    class UnroutableError(AMQPError):  # type: ignore
        pass


from config import Settings, get_settings
from events import (
    TYPES_WITHOUT_ACK,
    MalformedEventError,
    construir_mensaje_ack,
    construir_mensaje_nack,
    parse_demand_event,
    validacion_mensaje_entrante,
)
from heartbeat import record_heartbeat
from master_client import MasterClient, MasterUnavailableError, MessageRejectedError

logger = logging.getLogger(__name__)

# Cada cuánto despierta el bucle aunque no lleguen mensajes
_IDLE_SECONDS = 1
# Pausa tras reencolar por master caído
_REQUEUE_PAUSE_SECONDS = 1
# Una conexión que duró al menos esto se considera sana
_STABLE_SECONDS = 60


class SlidingWindowRateLimiter:
    """Controla la tasa de envíos en una ventana deslizante de 60 segundos."""

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max_per_minute
        self._timestamps: list[float] = []

    def acquire(self, now: float | None = None) -> bool:
        """Retorna True y registra si se puede publicar; False si la ventana está llena."""
        if now is None:
            now = time.monotonic()
        cutoff = now - 60.0
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.pop(0)

        if len(self._timestamps) < self.max_per_minute:
            self._timestamps.append(now)
            return True
        return False

    def wait_time(self, now: float | None = None) -> float:
        """Tiempo en segundos que se debe esperar para el siguiente cupo."""
        if now is None:
            now = time.monotonic()
        cutoff = now - 60.0
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.pop(0)
        if len(self._timestamps) < self.max_per_minute:
            return 0.0
        return max(0.01, 60.0 - (now - self._timestamps[0]))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    master = MasterClient()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    # 1. Hilo para cola del observer (E0)
    observer = threading.Thread(
        target=consume_forever,
        args=("observer", settings.broker_user, settings.broker_password, settings),
        kwargs={"serve": lambda channel: serve_observer_queue(channel, master, settings)},
        daemon=True,
    )
    observer.start()

    if not settings.city_broker_password:
        logger.warning("Sin CITY_BROKER_PASSWORD: no se consume %s", settings.city_queue_name)
        observer.join()
        return

    # 2. Hilo para consumidor del feed de mercado (market.{CODE})
    market_thread = threading.Thread(
        target=consume_forever,
        args=("market", settings.city_identity, settings.city_broker_password, settings),
        kwargs={"serve": lambda channel: serve_market_queue(channel, master, settings)},
        daemon=True,
    )
    market_thread.start()

    # 3. La conexión de la ciudad corre en el hilo principal
    consume_forever(
        "ciudad",
        settings.city_identity,
        settings.city_broker_password,
        settings,
        serve=lambda channel: serve_city_queue(channel, master, settings),
    )


def consume_forever(
    name: str,
    user: str,
    password: str,
    settings: Settings,
    serve: Callable[[BlockingChannel], None],
) -> None:
    """Mantiene una conexión al broker y la vuelve a abrir cada vez que se cae."""
    delay = settings.reconnect_initial_delay_seconds
    while True:
        connection = None
        started = time.monotonic()
        try:
            connection = pika.BlockingConnection(_connection_parameters(user, password, settings))
            logger.info("[%s] conectado al broker como %s", name, user)
            serve(connection.channel())
        except AMQPError as error:
            logger.error("[%s] conexión con el broker perdida: %r", name, error)
        except OSError as error:
            logger.error("[%s] no se pudo conectar al broker: %r", name, error)
        except Exception:
            logger.exception("[%s] error inesperado en el consumo", name)
        finally:
            _close_quietly(connection)

        if time.monotonic() - started >= _STABLE_SECONDS:
            delay = settings.reconnect_initial_delay_seconds
        record_heartbeat()
        logger.info("[%s] reintentando en %.0f s", name, delay)
        time.sleep(delay)
        delay = min(delay * 2, settings.reconnect_max_delay_seconds)


# --- Cola de la ciudad (E1 / E2) -------------------------------------------------------


def serve_city_queue(channel: BlockingChannel, master: MasterClient, settings: Settings) -> None:
    """Consume la cola de la ciudad y publica lo pendiente de master con rate limiters."""
    queue = settings.city_queue_name
    channel.confirm_delivery()
    channel.basic_qos(prefetch_count=10)
    logger.info("Escuchando la cola %s", queue)

    market_limiter = SlidingWindowRateLimiter(settings.market_ops_per_minute)
    request_limiter = SlidingWindowRateLimiter(settings.request_ops_per_minute)

    last_poll = float("-inf")
    for method, properties, body in channel.consume(queue, inactivity_timeout=_IDLE_SECONDS):
        if method is not None:
            handle_city_delivery(
                channel, method.delivery_tag, body, master, settings, properties=properties
            )

        record_heartbeat()
        now = time.monotonic()
        if now - last_poll >= settings.outbox_poll_seconds:
            last_poll = now
            publish_pending(channel, master, settings, market_limiter, request_limiter)


def handle_city_delivery(
    channel: BlockingChannel,
    delivery_tag: int,
    body: bytes,
    master: MasterClient,
    settings: Settings,
    properties: pika.BasicProperties | None = None,
) -> None:
    """Procesa una entrega en city.{CODE}.q y decide si se confirma o reencola."""
    try:
        process_city_message(channel, body, master, settings, properties=properties)
    except MasterUnavailableError as error:
        logger.warning("master no disponible, se reencola el mensaje: %s", error)
        channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
        time.sleep(_REQUEUE_PAUSE_SECONDS)
        return
    except (UnroutableError, NackError) as error:
        logger.error("No se pudo entregar respuesta al emisor: %r", error)
    except AMQPError:
        raise
    except Exception as error:
        logger.exception("Falla procesando un mensaje; se descarta y se registra")
        master.guardar_log_master("discarded", _as_text(body), f"error interno: {error!r}")

    channel.basic_ack(delivery_tag=delivery_tag)


def process_city_message(
    channel: BlockingChannel,
    body: bytes,
    master: MasterClient,
    settings: Settings,
    properties: pika.BasicProperties | None = None,
) -> None:
    """Valida un mensaje entrante (identidad y envelope), lo entrega a master y responde."""
    user_id = properties.user_id if properties else None
    message, nack, discard_reason = validacion_mensaje_entrante(
        body, settings.city_code, user_id=user_id
    )

    if discard_reason is not None:
        logger.warning("Mensaje descartado: %s", discard_reason)
        master.guardar_log_master("discarded", _as_text(body), discard_reason)
        return

    if nack is not None:
        target_rk = nack.pop("_target_routing_key", None)
        _reject(channel, nack, body, master, settings, target_routing_key=target_rk)
        return

    try:
        outcome = master.enviar_evento_master(message)
    except MessageRejectedError as error:
        if message["type"] in TYPES_WITHOUT_ACK:
            logger.warning("%s inválido, se descarta sin responder: %s", message["type"], error)
            reason = f"Contenido inválido para {message['type']}: {error}"[:300]
            master.guardar_log_master("discarded", _as_text(body), reason)
            return
        cycle_id = message.get("cycleId")
        nack = construir_mensaje_nack(
            message["msgId"],
            "MALFORMED_MESSAGE",
            422,
            f"Contenido inválido para {message['type']}: {error}"[:300],
            cycle_id if isinstance(cycle_id, str) else None,
            settings.city_code,
        )
        _reject(channel, nack, body, master, settings)
        return

    logger.info("type=%s msgId=%s -> %s", message["type"], message["msgId"], outcome)

    # Respuesta de ACK de recepción solo para mensajes que no están en TYPES_WITHOUT_ACK
    if message["type"] not in TYPES_WITHOUT_ACK:
        sender_city = message.get("cityId")
        if sender_city and message["type"] in ("bid", "bid-accepted"):
            # ACK directo a la cola de la ciudad emisora
            publish_message(
                channel,
                exchange="energy.x",
                routing_key=f"city.{sender_city}",
                payload=construir_mensaje_ack(message["msgId"], settings.city_code),
                settings=settings,
            )
        else:
            # ACK a la central
            publish_message(
                channel,
                exchange=settings.central_exchange,
                routing_key=settings.central_routing_key,
                payload=construir_mensaje_ack(message["msgId"], settings.city_code),
                settings=settings,
            )


# --- Cola del Mercado / Feed público (v3) ---------------------------------------------


def serve_market_queue(channel: BlockingChannel, master: MasterClient, settings: Settings) -> None:
    """Consume el feed del mercado (market.{CODE}) drenando rápido y sin enviar ACK."""
    queue = settings.market_queue_name
    channel.basic_qos(prefetch_count=50)
    logger.info("Escuchando el feed de mercado en la cola %s", queue)

    for method, _properties, body in channel.consume(queue, inactivity_timeout=_IDLE_SECONDS):
        if method is not None:
            handle_market_delivery(channel, method.delivery_tag, body, master, settings)
        record_heartbeat()


def handle_market_delivery(
    channel: BlockingChannel,
    delivery_tag: int,
    body: bytes,
    master: MasterClient,
    settings: Settings,
) -> None:
    """Procesa un mensaje de mercado: lo persiste en master y confirma solo a AMQP."""
    try:
        message = json.loads(body.decode("utf-8"))
    except Exception as err:
        logger.error("Mensaje de mercado no parseable: %s", err)
        channel.basic_ack(delivery_tag=delivery_tag)
        return

    try:
        outcome = master.enviar_evento_master(message)
        logger.info(
            "market type=%s msgId=%s -> %s",
            message.get("type"),
            message.get("msgId"),
            outcome,
        )
    except MasterUnavailableError as error:
        logger.warning("master no disponible, se reencola mensaje de mercado: %s", error)
        channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
        time.sleep(_REQUEUE_PAUSE_SECONDS)
        return
    except MessageRejectedError as error:
        logger.warning("Mensaje de mercado rechazado por master: %s", error)
    except Exception as error:
        logger.exception("Error procesando mensaje de mercado: %s", error)

    # Confirmación obligatoria al broker para no tapar la cola. SIN ACK de aplicación.
    channel.basic_ack(delivery_tag=delivery_tag)


# --- Publicación generalizada y Outbox --------------------------------------------------


def publish_pending(
    channel: BlockingChannel,
    master: MasterClient,
    settings: Settings,
    market_limiter: SlidingWindowRateLimiter | None = None,
    request_limiter: SlidingWindowRateLimiter | None = None,
) -> None:
    """Publica mensajes pendientes de outbox respetando los destinos y rate limits."""
    if market_limiter is None:
        market_limiter = SlidingWindowRateLimiter(settings.market_ops_per_minute)
    if request_limiter is None:
        request_limiter = SlidingWindowRateLimiter(settings.request_ops_per_minute)

    try:
        pending = master.retirar_pendientes()
    except MasterUnavailableError as error:
        logger.warning("No se pudo consultar la outbox de master: %s", error)
        return

    for item in pending:
        payload = item.get("payload", {})
        msg_type = payload.get("type")
        exchange = item.get("exchange") or settings.central_exchange
        routing_key = (
            item.get("routing_key") or item.get("routingKey") or settings.central_routing_key
        )

        # 1. Límite de operaciones de mercado (máx 60/min)
        if (
            msg_type in ("offer", "offer-updated", "offer-withdrawn")
            or exchange == settings.market_exchange
        ):
            if not market_limiter.acquire():
                logger.warning(
                    "Límite de operaciones de mercado alcanzado (60/min). "
                    "Se espera al siguiente poll."
                )
                continue

        # 2. Límite de requests directos a la central (máx 10/min)
        elif msg_type == "request" and not request_limiter.acquire():
            logger.warning("Límite de requests alcanzado (10/min). Se espera al siguiente poll.")
            continue

        try:
            publish_message(channel, exchange, routing_key, payload, settings)
            master.confirmar_envio(item["id"])
        except (UnroutableError, NackError) as error:
            logger.error(
                "Destino %s / %s no recibió el mensaje %s: %r",
                exchange,
                routing_key,
                item["id"],
                error,
            )
        except MasterUnavailableError as error:
            logger.warning("Publicado, pero master no registró el envío %s: %s", item["id"], error)


def publish_message(
    channel: BlockingChannel,
    exchange: str,
    routing_key: str,
    payload: dict[str, Any],
    settings: Settings,
) -> None:
    """Publica un mensaje AMQP con user_id de la ciudad y entrega obligatoria."""
    props = (
        pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
            user_id=settings.city_identity,
        )
        if pika is not None
        else type(
            "BasicProperties",
            (),
            {
                "user_id": settings.city_identity,
                "content_type": "application/json",
                "delivery_mode": 2,
            },
        )()
    )
    channel.basic_publish(
        exchange=exchange,
        routing_key=routing_key,
        body=json.dumps(payload).encode("utf-8"),
        properties=props,
        mandatory=True,
    )
    logger.info(
        "Publicado en %s (rk=%s) type=%s msgId=%s",
        exchange,
        routing_key,
        payload.get("type"),
        payload.get("msgId"),
    )


def publish_to_central(
    channel: BlockingChannel, payload: dict[str, Any], settings: Settings
) -> None:
    """Publica a la central (mantiene retrocompatibilidad)."""
    publish_message(
        channel,
        exchange=settings.central_exchange,
        routing_key=settings.central_routing_key,
        payload=payload,
        settings=settings,
    )


def _reject(
    channel: BlockingChannel,
    nack: dict[str, Any],
    body: bytes,
    master: MasterClient,
    settings: Settings,
    target_routing_key: str | None = None,
) -> None:
    logger.warning("Se responde NACK por %s", nack["reason"])
    if target_routing_key:
        publish_message(
            channel,
            exchange="energy.x",
            routing_key=target_routing_key,
            payload=nack,
            settings=settings,
        )
    else:
        publish_to_central(channel, nack, settings)
    master.guardar_log_master("nack", _as_text(body), nack["reason"])


# --- Cola del observer (E0) ------------------------------------------------------------


def serve_observer_queue(
    channel: BlockingChannel, master: MasterClient, settings: Settings
) -> None:
    """Consume los demand-set de la E0 y los reenvía a master."""
    channel.basic_qos(prefetch_count=10)
    channel.queue_declare(queue=settings.broker_queue, passive=True)
    logger.info("Escuchando la cola %s", settings.broker_queue)

    for method, _properties, body in channel.consume(
        settings.broker_queue, inactivity_timeout=_IDLE_SECONDS
    ):
        if method is not None:
            handle_observer_delivery(channel, method.delivery_tag, body, master)
        record_heartbeat()


def handle_observer_delivery(
    channel: BlockingChannel, delivery_tag: int, body: bytes, master: MasterClient
) -> None:
    """Reenvía un demand-set a master y confirma solo si quedó guardado."""
    try:
        event = parse_demand_event(body)
    except MalformedEventError as error:
        logger.error("demand-set malformado, se descarta: %s", error)
        channel.basic_ack(delivery_tag=delivery_tag)
        return

    try:
        master.publish_event(event)
    except MasterUnavailableError as error:
        logger.warning("master no disponible, se reencola el demand-set: %s", error)
        channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
        time.sleep(_REQUEUE_PAUSE_SECONDS)
        return

    channel.basic_ack(delivery_tag=delivery_tag)


# --- Piezas internas -------------------------------------------------------------------


def _connection_parameters(
    user: str, password: str, settings: Settings
) -> pika.ConnectionParameters:
    ssl_options = None
    if settings.broker_use_ssl:
        ssl_options = pika.SSLOptions(ssl.create_default_context(), settings.broker_host)

    return pika.ConnectionParameters(
        host=settings.broker_host,
        port=settings.broker_port,
        virtual_host=settings.broker_vhost,
        credentials=pika.PlainCredentials(user, password),
        heartbeat=30,
        blocked_connection_timeout=60,
        ssl_options=ssl_options,
    )


def _close_quietly(connection: pika.BlockingConnection | None) -> None:
    if connection is None or connection.is_closed:
        return
    try:
        connection.close()
    except Exception:
        logger.debug("La conexión ya estaba rota al cerrarla")


def _as_text(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


if __name__ == "__main__":
    main()
