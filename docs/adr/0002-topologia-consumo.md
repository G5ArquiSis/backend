# ADR-0001: Topología del Consumo de Mensajería

- **Estado:** propuesto
- **Fecha:** 2026-10-07
- **Responsable:** Catalina Berríos 
- **Área del enunciado:** AD1 


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

Consume desde la cola `city.{CODE}` y publica respuestas (ACK/NACK) en el exchange central. Traslada los eventos válidos a `master` mediante llamadas HTTP POST internas (`http://master:8000/interno/...`).

2. **master**: Servicio FastAPI que atiende la API pública, administra la base de datos PostgreSQL y expone endpoints internos para que `connector` reporte eventos procesados, duplicados y descartes.

## Consecuencias y tradeoffs

- **Positivas**:

  - Aislamiento de fallas: si RabbitMQ se cae, solo `connector` entra en bucle de reintento. `master` sigue respondiendo a la SPA y a los healthchecks.

  - Satisface directamente RNF1 heredado de E0 y RNF04 de E1.

- **Negativas**:

  - Se añade la latencia del salto HTTP interno entre `connector` y `master`. Se mitiga manteniendo timeouts cortos y conexiones HTTP persistentes (`httpx.Client`).

## Post-mortem (después de la demo)

Qué pasó en la realidad: qué funcionó, qué no, qué cambiaríamos.
