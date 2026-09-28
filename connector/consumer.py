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

import asyncio
import contextlib
import logging
import signal

import aio_pika
from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from config import get_settings
from events import DemandEventMessage, MalformedEventError, parse_demand_event
from heartbeat import record_heartbeat
from master_client import MasterClient, MasterUnavailableError

logger = logging.getLogger(__name__)


async def main() -> None:
    """Levanta el consumidor y lo mantiene vivo hasta recibir la señal de apagado."""
    logging.basicConfig(level=logging.INFO)

    shutdown = asyncio.Event()
    install_shutdown_handlers(shutdown)

    heartbeat_task = asyncio.create_task(refresh_heartbeat_while_running(shutdown))
    try:
        await run_until_shutdown(shutdown)
    finally:
        heartbeat_task.cancel()


async def run_until_shutdown(shutdown: asyncio.Event) -> None:
    """Reintenta conectarse al broker indefinidamente, con backoff exponencial.

    Perder el broker nunca debe terminar el proceso (RNF1): esta función es la
    que garantiza que el connector se recupere sin intervención manual, incluso
    si el broker está caído desde el arranque.
    """
    settings = get_settings()
    client = MasterClient()
    delay = settings.reconnect_initial_delay_seconds

    try:
        while not shutdown.is_set():
            try:
                connection = await aio_pika.connect_robust(settings.broker_url)
            except Exception:
                logger.exception("No se pudo conectar al broker, reintentando en %.1fs", delay)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shutdown.wait(), timeout=delay)
                delay = min(delay * 2, settings.reconnect_max_delay_seconds)
                continue

            delay = settings.reconnect_initial_delay_seconds
            async with connection:
                consume_task = asyncio.create_task(consume_queue(connection, client))
                shutdown_task = asyncio.create_task(shutdown.wait())
                done, pending = await asyncio.wait(
                    {consume_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if consume_task in done:
                    exc = consume_task.exception()
                    if exc is not None:
                        logger.exception("Se perdió la conexión al broker", exc_info=exc)
    finally:
        await client.aclose()


async def consume_queue(connection: AbstractRobustConnection, client: MasterClient) -> None:
    """Consume la cola del observer hasta que la conexión se caiga."""
    settings = get_settings()
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=10)
    queue = await channel.get_queue(settings.broker_queue)

    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            await handle_message(message, client)


async def handle_message(message: AbstractIncomingMessage, client: MasterClient) -> None:
    """Procesa un mensaje y decide su ack según la política del módulo."""
    try:
        event: DemandEventMessage = parse_demand_event(message.body)
    except MalformedEventError:
        logger.exception("Mensaje descartado: no corresponde a un evento demand-set válido")
        await message.ack()
        return

    try:
        await client.publish_event(event)
    except MasterUnavailableError:
        logger.exception("master no confirmó el evento %s, reencolando", event.idpk)
        await message.nack(requeue=True)
        return

    await message.ack()
    record_heartbeat()


async def refresh_heartbeat_while_running(shutdown: asyncio.Event) -> None:
    """Refresca la señal de vida mientras el proceso siga corriendo.

    No basta con tocarla al procesar mensajes: una cola sin tráfico dejaría el
    container marcado como unhealthy aunque el connector esté perfectamente vivo.

    Deliberadamente no mira el estado de la conexión al broker. El healthcheck
    responde por connector, no por RabbitMQ: si el broker se cae, connector sigue
    sano reintentando (RNF1) y marcar el container como unhealthy por una caída
    ajena solo haría que Docker lo reiniciara sin motivo.
    """
    while not shutdown.is_set():
        record_heartbeat()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=30)


def install_shutdown_handlers(shutdown: asyncio.Event) -> None:
    """Conecta SIGTERM y SIGINT al apagado ordenado.

    Va desde el principio y no como idea de último momento: sin esto, un
    `docker compose down` mataría el proceso dejando mensajes sin ack.
    """
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)


if __name__ == "__main__":
    asyncio.run(main())
