"""Ciclo de vida del consumo AMQP: conexión, reconexión, ack y publicación.

Este módulo contiene solo la parte de concurrencia (CLAUDE.md 10). Qué significa
un mensaje lo resuelve events.py, y qué hacer con él, master_client.py.

connector atiende dos colas, cada una con su conexión y su hilo:

- La del observer, con los demand-set de la E0, que se reenvían a POST /events.
- La de la ciudad (city.{CODE}), con el protocolo de la E1: valida, responde ACK
  o NACK, entrega el mensaje a master y publica lo que master tiene pendiente.

Política de confirmación al broker, que es lo que sostiene RNF1 y AD1:
  - Mensaje malformado -> se confirma y se registra. Reencolarlo lo haría circular
    para siempre y taparía la cola con algo que nunca podrá procesarse.
  - master caído       -> nack(requeue=True), sin ACK a la central. El broker es la
    fuente de verdad de lo pendiente; no hay una cola propia en memoria.
  - master confirmó    -> se confirma al broker y se responde ACK a la central.
"""

import json
import logging
import signal
import ssl
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.exceptions import AMQPError, NackError, UnroutableError

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

# Cada cuánto despierta el bucle aunque no lleguen mensajes: refresca la señal de
# vida y revisa si toca pedirle a master lo pendiente por publicar.
_IDLE_SECONDS = 1
# Pausa tras reencolar por master caído, para no girar en vacío sobre el mismo mensaje.
_REQUEUE_PAUSE_SECONDS = 1
# Una conexión que duró al menos esto se considera sana; si vuelve a caerse, la espera
# de reconexión parte otra vez del mínimo.
_STABLE_SECONDS = 60


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    master = MasterClient()
    # SIGTERM es lo que envía `docker stop`: se sale ordenadamente. Los mensajes sin
    # confirmar vuelven a la cola solos cuando el broker ve cerrarse la conexión.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    observer = threading.Thread(
        target=consume_forever,
        args=("observer", settings.broker_user, settings.broker_password, settings),
        kwargs={"serve": lambda channel: serve_observer_queue(channel, master, settings)},
        daemon=True,
    )
    observer.start()

    if not settings.city_broker_password:
        logger.warning("Sin CITY_BROKER_PASSWORD: no se consume %s", settings.city_identity)
        observer.join()
        return

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
    """Mantiene una conexión al broker y la vuelve a abrir cada vez que se cae.

    La espera crece hasta un tope y vuelve al mínimo solo cuando la conexión se
    sostuvo un rato. Conectarse no basta: un rechazo del broker justo después de
    autenticar (por ejemplo, un permiso faltante) dejaría al consumidor reintentando
    cada segundo para siempre. La señal de vida se refresca también mientras se
    reintenta: un broker caído no vuelve unhealthy al contenedor, porque reiniciarlo
    no arregla nada y el resto del sistema sigue sirviendo lo ya persistido.
    """
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


# --- Cola de la ciudad (E1) ------------------------------------------------------------


def serve_city_queue(channel: BlockingChannel, master: MasterClient, settings: Settings) -> None:
    """Consume city.{CODE} y, entre mensaje y mensaje, publica lo pendiente de master."""
    queue = settings.city_identity
    # Con confirmaciones del broker, publicar a un destino que no existe levanta una
    # excepción en vez de perder el mensaje en silencio.
    channel.confirm_delivery()
    channel.basic_qos(prefetch_count=10)
    # La cola no se declara, ni siquiera en modo pasivo: es de la central, y el usuario
    # de la ciudad no tiene permiso de configuración sobre ella (el broker responde
    # 403 ACCESS_REFUSED y cierra el canal). Solo se consume.
    logger.info("Escuchando la cola %s", queue)

    # -inf: la primera vuelta consulta de inmediato, sin esperar un intervalo completo.
    last_poll = float("-inf")
    for method, _properties, body in channel.consume(queue, inactivity_timeout=_IDLE_SECONDS):
        if method is not None:
            handle_city_delivery(channel, method.delivery_tag, body, master, settings)

        record_heartbeat()
        now = time.monotonic()
        if now - last_poll >= settings.outbox_poll_seconds:
            last_poll = now
            publish_pending(channel, master, settings)


def handle_city_delivery(
    channel: BlockingChannel,
    delivery_tag: int,
    body: bytes,
    master: MasterClient,
    settings: Settings,
) -> None:
    """Procesa una entrega y decide si se confirma al broker o se reencola."""
    try:
        process_city_message(channel, body, master, settings)
    except MasterUnavailableError as error:
        logger.warning("master no disponible, se reencola el mensaje: %s", error)
        channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
        time.sleep(_REQUEUE_PAUSE_SECONDS)
        return
    except (UnroutableError, NackError) as error:
        # master ya guardó el mensaje; lo que no llegó es nuestra respuesta a la central.
        # Reencolar no lo arregla y solo repetiría el mensaje como duplicado.
        logger.error("La central no recibió nuestra respuesta: %r", error)
    except AMQPError:
        # Falló el canal: la entrega queda sin confirmar y el broker la reenvía al reconectar.
        raise
    except Exception as error:
        # Un error nuestro con un mensaje puntual: reencolarlo lo haría fallar para siempre.
        logger.exception("Falla procesando un mensaje; se descarta y se registra")
        master.guardar_log_master("discarded", _as_text(body), f"error interno: {error!r}")

    channel.basic_ack(delivery_tag=delivery_tag)


def process_city_message(
    channel: BlockingChannel, body: bytes, master: MasterClient, settings: Settings
) -> None:
    """Valida un mensaje de la central, lo entrega a master y responde ACK o NACK."""
    message, nack, discard_reason = validacion_mensaje_entrante(body, settings.city_code)

    if discard_reason is not None:
        # No hay msgId válido al cual responder: solo queda registrarlo.
        logger.warning("Mensaje descartado: %s", discard_reason)
        master.guardar_log_master("discarded", _as_text(body), discard_reason)
        return

    if nack is not None:
        _reject(channel, nack, body, master, settings)
        return

    try:
        outcome = master.enviar_evento_master(message)
    except MessageRejectedError as error:
        # El envelope estaba bien, pero al contenido le falta algo que su tipo exige.
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
    # El ACK sale después de que master guardó el mensaje: solo confirma recepción,
    # pero no tiene sentido confirmar algo que todavía podríamos perder.
    if message["type"] not in TYPES_WITHOUT_ACK:
        publish_to_central(
            channel, construir_mensaje_ack(message["msgId"], settings.city_code), settings
        )


def publish_pending(channel: BlockingChannel, master: MasterClient, settings: Settings) -> None:
    """Publica a la central lo que master tiene en su outbox y confirma cada envío.

    Si un envío falla no se confirma: master lo vuelve a ofrecer más tarde. Repetir
    un mensaje es inocuo, porque conserva su idpk.
    """
    try:
        pending = master.retirar_pendientes()
    except MasterUnavailableError as error:
        logger.warning("No se pudo consultar la outbox de master: %s", error)
        return

    for item in pending:
        try:
            publish_to_central(channel, item["payload"], settings)
            master.confirmar_envio(item["id"])
        except (UnroutableError, NackError) as error:
            logger.error("La central no recibió el mensaje %s: %r", item["id"], error)
        except MasterUnavailableError as error:
            logger.warning("Publicado, pero master no registró el envío %s: %s", item["id"], error)


def publish_to_central(
    channel: BlockingChannel, payload: dict[str, Any], settings: Settings
) -> None:
    """Publica un mensaje a la central con la identidad de la ciudad."""
    channel.basic_publish(
        exchange=settings.central_exchange,
        routing_key=settings.central_routing_key,
        body=json.dumps(payload).encode("utf-8"),
        properties=pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
            # user_id es una propiedad del publish, no un campo del cuerpo. El broker la
            # valida contra la conexión y la central la compara con cityId.
            user_id=settings.city_identity,
        ),
        mandatory=True,
    )
    logger.info("Publicado type=%s msgId=%s", payload.get("type"), payload.get("msgId"))


def _reject(
    channel: BlockingChannel,
    nack: dict[str, Any],
    body: bytes,
    master: MasterClient,
    settings: Settings,
) -> None:
    logger.warning("Se responde NACK por %s", nack["reason"])
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
    except Exception:  # noqa: BLE001 - ya se está cerrando; no hay nada que hacer con el error
        logger.debug("La conexión ya estaba rota al cerrarla")


def _as_text(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


if __name__ == "__main__":
    main()
