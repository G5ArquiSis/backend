# ADR-0001: Topología del Consumo de Mensajería

- **Estado:** aceptado
- **Fecha:** 2026-10-07
- **Responsable:** Catalina Berríos 
- **Área del enunciado:** AD1 


> **Nota (2026-11-03):** este ADR formaliza una topología que ya existía. La separación en
> `connector` y `master` viene de la E0, portada en el commit `dab4622`, y el ADR se escribió
> después de portarla. La alternativa 2 no se eligió en la E1: se ratificó.

## Contexto

El sistema EnergyShark interactúa con un broker RabbitMQ centralizado. En la Entrega 0 se definió una arquitectura de dos contenedores: `connector` (consumidor AMQP) y `master` (API HTTP y persistencia). 

Para la Entrega 1 se debe garantizar que la caída o inestabilidad del broker no afecte la disponibilidad de la API web para servir consultas sobre datos ya persistidos, y que la reconexión con el broker sea automática sin pérdida de mensajes ni intervención manual.

## Alternativas consideradas

1. **Alternativa 1:** **Implementar un Monolito en un único contenedor**:

Consumir RabbitMQ y levantar FastAPI/PostgreSQL en el mismo proceso o contenedor.

- *Ventaja*: Menor sobrecarga HTTP entre servicios.

- *Desventaja*: Un bloqueo o fallo de dependencias del broker puede degradar el runtime de la API web o complicar el ciclo de vida del contenedor master.


2. **Alternativa 2:** **Implementar una topología desacoplada de dos contenedores**

Un contenedor dedicado exclusivamente a mensajería AMQP (`connector`) y un contenedor para la lógica de dominio, persistencia y API REST (`master`), comunicados mediante llamadas HTTP internas.

- *Ventaja*: Aísla caídas o reconexiones del broker para no degradar la disponibilidad de la API REST.

 *Desventaja*: Introduce latencia por llamadas HTTP internas y exige coordinar la orquestación y fallos de red entre ambos contenedores en Docker.

## Decisión

Se decide mantener la separación en dos servicios/contenedores dentro de la misma red Docker:

1. **connector**: Proceso dedicado en Python ejecutando una conexión AMQP resiliente (usando `pika` con lógica de reconexión y exponential backoff). 

Consume desde la cola `city.{CODE}` y publica respuestas (ACK/NACK) en el exchange central. Traslada los eventos válidos a `master` mediante llamadas HTTP POST internas (`http://master:8000/internal/...`).

2. **master**: Servicio FastAPI que atiende la API pública, administra la base de datos PostgreSQL y expone endpoints internos para que `connector` reporte eventos procesados, duplicados y descartes.

## Consecuencias y tradeoffs

- **Positivas**:

  - Aislamiento de fallas: si RabbitMQ se cae, solo `connector` entra en bucle de reintento. `master` sigue respondiendo a la SPA y a los healthchecks.

  - Satisface directamente RNF1 heredado de E0 y RNF04 de E1.

- **Negativas**:

  - Se añade la latencia del salto HTTP interno entre `connector` y `master`. Se mitiga manteniendo timeouts cortos y conexiones HTTP persistentes (`httpx.Client`).

## Post-mortem (después de la demo)

### 1. Qué decidimos y por qué
En la E0 y E1 se decidió aislar estrictamente la mensajería en un contenedor dedicado (`connector`, con `pika`) separado del servicio de persistencia y API (`master`, con FastAPI y Postgres), comunicados mediante llamadas HTTP internas (`/internal/*`). El objetivo fundamental fue cumplir con RNF1: que la inestabilidad, latencia o caída completa de RabbitMQ no degradara la disponibilidad de la API web para las consultas de los usuarios ni corrompiera el estado persistido.

### 2. Qué pasó en la realidad durante la Entrega 2 (E2)
La arquitectura de consumo debió evolucionar para soportar el protocolo v3, caracterizado por tres tipos de tráfico con requerimientos muy dispares:
1. **Feed público del mercado (`market.TAL`):** Alto volumen de mensajes (`offer`, `offer-updated`, `offer-withdrawn`, `market-closed`) con TTL de un ciclo (20 min) y cola acotada a 5.000 ítems. **Regla estricta:** NUNCA se responde con ACK de aplicación en esta cola; solo se confirma con `basic_ack` a RabbitMQ tras asegurar su persistencia en `master`.
2. **Mensajería transaccional entre ciudades (`city.TAL.q`):** Tráfico directo entre pares (`bid`, `bid-accepted`, `bid-rejected`, eventos de wallet y central). Exige validación obligatoria de identidad (`properties.user_id == city.{CODE}`), emisión de NACK `IDENTITY_MISMATCH` (código 403) dirigido a la routing key de quien realmente publicó el mensaje, descarte silencioso registrado de tipos retirados (410 `TYPE_RETIRED`), y ACK de aplicación solo tras persistir en el ledger.
3. **Eventos heredados de la E0 (`observer.q`):** Recepción de `demand-set` para compatibilidad de ciclo base.

Adicionalmente, se integró en `connector` la publicación generalizada desde la tabla `outbox` hacia los exchanges `energy.x` y `market`, gobernada por un limitador de tasa con ventana deslizante de 60 segundos (máximo 60 ops/min de mercado y 10 requests/min).

### 3. Qué funcionó bien (Aciertos)
- **Resiliencia demostrada ante fallas de red y caídas del broker:** Durante los ensayos de la demo (Anomalía 5: corte breve de conexión) y reinicios del contenedor del broker, `connector` ejecutó su lógica de reconexión con exponential backoff de forma autónoma. `master` jamás experimentó degradación y continuó respondiendo a la SPA y a los healthchecks sin interrupción.
- **Aislamiento de la carga del mercado:** El consumo de `market.TAL` permitió drenar ráfagas intensas de ofertas de otras ciudades sin competir directamente por los recursos de conexión de la API web.
- **Protección contra abusos y penalizaciones:** El rate limiter en memoria dentro de `connector` aseguró que nunca se superara el tope de 60 publicaciones por minuto al exchange `market`, reteniendo los mensajes en la base de datos sin descartarlos y evitando suspensiones (`ACCESS_REFUSED`) o baneos por parte de los operadores del broker.
- **Monitoreo desacoplado:** La actividad del consumidor se supervisó limpiamente mediante el archivo de latido (`/tmp/connector-heartbeat`), permitiendo a New Relic y al proceso monitor verificar el estado de salud sin abrir puertos HTTP en el contenedor de mensajería.

### 4. Qué generó fricción y qué no funcionó tan bien (Limitaciones)
- **Sobrecarga y latencia del salto HTTP interno (`connector` ↔ `master`):** Para mensajes de mercado de alta frecuencia, convertir cada evento AMQP en un request HTTP POST JSON individual hacia `/internal/messages` introdujo una sobrecarga de CPU por serialización/deserialización repetitiva y una latencia añadida de red local.
- **Complejidad multihilo con `pika.BlockingConnection`:** `pika` es síncrono y no thread-safe. Para escuchar simultáneamente la cola del observer, la cola transaccional de la ciudad y el feed de mercado, `connector` debió recurrir a hilos separados (`threading.Thread`) con conexiones AMQP independientes. Esto complejizó el manejo limpio de señales del sistema operativo (`SIGTERM`/`SIGINT`) y la propagación de excepciones fatales.
- **Polling de la outbox:** Que `connector` consulte la base de datos mediante `POST /internal/outbox/claim` cada 5 segundos fija una cota inferior de latencia en la salida de ofertas y respuestas a bids, lo que en situaciones de alta competencia por compras puede restarle velocidad al nodo frente a contrapartes que publiquen de forma reactiva inmediata.

### 5. Qué cambiaríamos en una próxima iteración (Lecciones de arquitectura)
- **Migración a un framework asíncrono unificado (`asyncio` + `aio-pika` / `faststream`):** Unificar el consumidor en una arquitectura basada en `asyncio` permitiría multiplexar todas las colas y publicaciones en un único hilo con event loop cooperativo, eliminando por completo los bloqueos entre hilos y simplificando el ciclo de vida de los canales AMQP.
- **Batching o canal de alto rendimiento para el feed de mercado:** Implementar procesamiento por lotes (micro-batching) para agrupar múltiples ofertas recibidas en una sola llamada de persistencia hacia `master`, o utilizar comunicación por sockets Unix/gRPC con Protobuf en lugar de HTTP/1.1 con JSON.
- **Notificación reactiva de la outbox:** Sustituir el sondeo periódico (polling) por un mecanismo de notificación reactiva (como `LISTEN`/`NOTIFY` de PostgreSQL o eventos en memoria) para que `master` avise a `connector` en el milisegundo exacto en que se encola un bid o una oferta.
