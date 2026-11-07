"""Destino de los eventos: la cola `city.{CODE}` de un RabbitMQ local, como la central real.

La wallet real entrega sus eventos a la central, que los reenvía por `energy.x` con
routing key `city.{CODE}`, `sender: "central"` y `user_id` "central". La falsa hace lo
mismo contra el broker del perfil `fake` de docker compose.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

import pika

logger = logging.getLogger("fake_wallet.sinks")


class AmqpSink:
    def __init__(self, url: str, exchange: str = "energy.x", user_id: str = "central"):
        self.parameters = pika.URLParameters(url)
        self.exchange = exchange
        self.user_id = user_id
        self._connection: pika.BlockingConnection | None = None
        self._channel = None
        # pika no es thread-safe: un solo publish a la vez sobre la misma conexión.
        self._lock = threading.Lock()

    def _publish_sync(self, city: str, message: dict) -> None:
        body = json.dumps(message).encode()
        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
            message_id=message["msgId"],
            user_id=self.user_id,
        )
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._channel is None or self._channel.is_closed:
                        self._connection = pika.BlockingConnection(self.parameters)
                        self._channel = self._connection.channel()
                    self._channel.basic_publish(self.exchange, f"city.{city}", body, properties)
                    return
                except pika.exceptions.AMQPError:
                    self._channel = None
                    if attempt == 2:
                        raise

    async def publish(self, city: str, message: dict) -> None:
        try:
            await asyncio.to_thread(self._publish_sync, city, message)
        except Exception:
            logger.exception("No se pudo publicar %s a city.%s", message.get("type"), city)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None and self._connection.is_open:
                self._connection.close()
