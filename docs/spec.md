# Spec de la entrega E1

> RDOC02. Escrita el 2026-10-07. El estado de avance de cada punto está en
> [milestones.md](milestones.md).

## Objetivo

Construir el nodo de energía de una ciudad dentro de la red del curso. El nodo consume su cola
del broker RabbitMQ, mantiene un ledger propio (presupuesto y balance de energía), negocia energía
con la central y con otras ciudades, y reporta su posición al cierre de cada ciclo, sin
intervención manual. Una SPA muestra el historial de ciclos, la conectividad y las negociaciones.

El sistema parte del código de la E0 (`master` y `connector`), que debe seguir operativo: las
entregas son acumulativas.

## Ciudad asignada

Talca, código `TAL`. El nodo consume la cola `city.TAL` y publica con la propiedad AMQP
`user_id = city.TAL`.

## Alcance

La entrega apunta a todos los requisitos del enunciado. Se agrupan por área de trabajo:

| Área | Requisitos |
|---|---|
| Mensajería y protocolo | G02 (consumir `city.{cityId}` y responder ACK/NACK); envelope v2; propiedad AMQP `user_id`; decisión AD1 |
| Ledger y persistencia | Aplicación de `status-statement`, `transfer` y `demand-statement` (parte de RF03); RF05 (duplicados y NACK); decisión AD2 |
| Ciclo de negociación | G03 (ciclo autónomo con `negotiation-report` en la ventana); flujo voluntario con timeouts de 30 s (RF03); decisión AD3 |
| Frontend y autenticación | G04, G06; vistas de RF01, RF02 y RF04 |
| Deploy e infraestructura | G01, G05, G07, G08; RNF01 a RNF05 |
| Documentación | RDOC01 a RDOC04 |

Requisitos heredados de la E0 que se mantienen: `/history` paginado y filtrable, contenedores
`master` y `connector` con HEALTHCHECK en la misma red, Nginx en el host, HTTPS con renovación
automática y balanceo de carga sobre dos réplicas de `master`.

Fuera del alcance: nada del enunciado se descarta de antemano. Lo que no se alcance a implementar
queda registrado como pendiente en [milestones.md](milestones.md).

## Arquitectura de partida

- **Backend** (`G5ArquiSis/backend`): `master` (FastAPI + Postgres) y `connector` (consumidor
  AMQP), en contenedores sobre una EC2, detrás de Nginx y de API Gateway.
- **Frontend** (`G5ArquiSis/frontend`): SPA en React + Vite, servida desde S3 + CloudFront.
- **Contratos** (`G5ArquiSis/contratos`): schemas de mensajes, OpenAPI y contexto compartido.

Las tres decisiones abiertas (topología del consumo, persistencia del ledger y manejo de
timeouts) se resuelven en los ADRs de [`adr/`](adr/), que se escriben antes de implementarlas.

## Roles

| Rol | Área | Integrante |
|---|---|---|
| A | Mensajería y protocolo | Catalina Berrios |
| B | Ledger y persistencia | Vicente Pavez |
| C | Ciclo de negociación | Serena Barraza |
| D | Frontend y autenticación | Carlos Riffo |
| E | Deploy e infraestructura | Melchor Guerrero |

## Forma de trabajo

- Todo cambio entra por PR a `main`, con 2 aprobaciones. `main` está protegido.
- Cada merge a `main` se despliega solo a producción.
- El uso de IA se registra por integrante en [`ai-logs/`](ai-logs/), en el mismo PR que el trabajo.
- El ADR de una decisión se commitea antes que su implementación.

## Preguntas abiertas al ayudante

No se plantearon preguntas al ayudante. Ante la duda de si las partes variables de la E0 (HTTPS y
balanceo de carga) siguen vigentes en la E1, el grupo decidió mantener ambas.
