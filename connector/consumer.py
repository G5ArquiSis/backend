"""Ciclo de vida del consumo AMQP: conexión, reconexión, ack y apagado.

Este módulo contiene solo la parte de concurrencia (CLAUDE.md 10). Qué significa
un mensaje lo resuelve events.py, y qué hacer con él, master_client.py.

Política de ack, que es lo que sostiene RNF1:
  - JSON malformado -> ack y log de error. Reencolarlo lo haría circular para
    siempre y taparía la cola con un mensaje que nunca podrá procesarse.
  - master caído    -> nack(requeue=True). El broker es la fuente de verdad de
    los eventos pendientes; no se implementa una cola propia en memoria.
  - POST confirmado -> ack y refresco de la señal de vida.
"""


import time
import json
import logging
from typing import Any, Dict
import pika
import ssl
from config import get_settings
from master_client import MasterClient
from events import validacion_mensaje_entrante, construir_mensaje_ack
from heartbeat import record_heartbeat


logger = logging.getLogger(__name__)


def publicar_mensaje_broker(
        channel: pika.adapters.blocking_connection.BlockingChannel,
        payload: Dict[str, Any],
        city_code: str,
        routing_key: str = "central"):

    body_codificado = json.dumps(payload).encode("utf-8")

    properties = pika.BasicProperties(
        content_type="application/json",
        delivery_mode=2,
        # user_id se envía como parametro del publish, no como parte del body
        user_id=f"city.{city_code}",
    )

    channel.basic_publish(
        exchange="",
        routing_key=routing_key,
        body=body_codificado,
        properties=properties,
    )

    logger.info(
        f"Publicado mensaje type={payload.get('type')} msgId={payload.get('msgId')}"
    )


def procesar_mensaje_y_responder(
        canal: pika.adapters.blocking_connection.BlockingChannel,
        body: bytes,
        master_client: MasterClient,
        city_code: str):

    mensaje, mensaje_nack, razon_descarte = validacion_mensaje_entrante(body)

    # mensaje no parseable o sin msgId
    if razon_descarte != None:
        logger.warning(f"Mensaje descartado: {razon_descarte}")

        # en este caso no hay nack, entonces no hay que mandar nada al broker
        # hay que registrar el log

        master_client.guardar_log_master(
            categoria="discarded",
            mensaje_evento=body.decode("utf-8", errors="replace"),
            reason=razon_descarte,
        )

        return

    # mensaje que si envían nack
    if mensaje_nack != None:

        logger.warning(f"Enviando NACK por {mensaje_nack.get('reason')}")

        # hay que enviar el nack al broker para que reencole el mensaje
        publicar_mensaje_broker(canal, mensaje_nack, city_code)

        master_client.guardar_log_master(
            categoria="nack",
            mensaje_evento=body.decode("utf-8", errors="replace"),
            reason=mensaje_nack.get("reason"),
            nack_code=mensaje_nack.get("code"),
        )

        return

    # mensaje valido, que tiene que enviar un ack.
    # tambien hay que reenviarlo a master para que lo guarde en su bdd de eventos
    master_client.enviar_evento_master(mensaje)

    msg_type_origen = mensaje.get("type")
    msg_id_origen = mensaje.get("msgId")

    # para no responder con un ack a un ack, nack o error
    if msg_type_origen not in ["ack", "nack", "error"] and msg_id_origen != None:

        mensaje_ack = construir_mensaje_ack(msg_id_origen, city_code)
        publicar_mensaje_broker(canal, mensaje_ack, city_code)


def conectar_broker(settings, master_client: MasterClient, nombre_cola: str):

    logger.info(
        f"Conectando a RabbitMQ en {settings.broker_host}:{settings.broker_port}..."
    )

    ssl_options = None
    if getattr(settings, "broker_use_ssl", True):
        ssl_context = ssl.create_default_context()
        ssl_options = pika.SSLOptions(ssl_context)

    credenciales = pika.PlainCredentials(
        settings.broker_user, settings.broker_password)

    parametros = pika.ConnectionParameters(
        host=settings.broker_host,
        port=settings.broker_port,
        virtual_host=settings.broker_vhost,
        credentials=credenciales,
        heartbeat=30,
        blocked_connection_timeout=60,
        ssl_options=ssl_options
    )

    conexion_broker = pika.BlockingConnection(parametros)
    canal = conexion_broker.channel()

    canal.queue_declare(queue=nombre_cola, durable=True)
    canal.basic_qos(prefetch_count=10)

    record_heartbeat()

    logger.info(f"Escuchando cola {nombre_cola}")

    for method_frame, properties, body in canal.consume(nombre_cola):
        delivery_tag = method_frame.delivery_tag

        try:
            procesar_mensaje_y_responder(
                canal, body, master_client, settings.city_code)
            record_heartbeat()
            canal.basic_ack(delivery_tag=method_frame.delivery_tag)
            # notifica al broker que el mensaje ya fue leido y retirado de la cola

        except Exception as err:
            logger.error(f"Falla procesando mensaje: {err}")
            canal.basic_ack(delivery_tag=method_frame.delivery_tag)
            # notifica al broker que el mensaje ya fue leido y retirado de la cola


def iniciar_broker():

    settings = get_settings()
    master_client = MasterClient()
    nombre_cola = f"city.{settings.city_code}"

    while True:
        try:
            conectar_broker(settings, master_client, nombre_cola)

        except pika.exceptions.AMQPConnectionError as e:
            logger.error(
                f"Conexión con RabbitMQ perdida: {e}. Reintentando en 5 segundos...")
            record_heartbeat()
            time.sleep(5)

        except Exception as e:
            logger.exception(
                f"Error inesperado en consumer: {e}. Reintentando en 5 segundos...")
            record_heartbeat()
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    iniciar_broker()
