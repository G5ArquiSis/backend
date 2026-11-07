# ADR-0004: Serialización de la aceptación de bids

- **Estado:** propuesto
- **Fecha:** 2026-11-06
- **Responsable:** Rol C
- **Área del enunciado:** AD4

## Contexto

El mercado permite que varias ciudades envíen bids simultáneamente a una misma oferta. El vendedor debe procesar estas solicitudes sin comprometer más energía de la que tiene disponible.

El escenario de AD4 considera bids cuya cantidad combinada supera el `remaining` de una oferta, con 40 ms de diferencia.

La respuesta esperada es que los bids se procesen secuencialmente por oferta. Cada bid se acepta o rechaza entero: se acepta únicamente si su cantidad cabe en el `remaining` disponible; de lo contrario, se rechaza con `QUANTITY_UNAVAILABLE` e informa `available`, para que el comprador pueda solicitar el resto mediante un segundo bid. Nunca deben existir dos swaps contra la misma cantidad.

El backend opera con dos réplicas de `master` sobre PostgreSQL. Por lo tanto, un mecanismo de sincronización en memoria no garantiza coordinación entre ambas.

## Alternativas consideradas

1. **Bloqueo de fila en PostgreSQL (`SELECT FOR UPDATE`).**
   - Ventajas: coordina transacciones entre réplicas mediante la base compartida.
   - Desventajas: requiere persistir el estado necesario para administrar las cantidades comprometidas y las reservas.

2. **Actualización condicional atómica en PostgreSQL.**
   - Ventajas: permite validar y modificar una cantidad mediante una operación atómica.
   - Desventajas: requiere diseñar cuidadosamente las transiciones y la recuperación de reservas.

3. **Cola de procesamiento por oferta.**
   - Ventajas: permite procesar bids secuencialmente.
   - Desventajas: requiere coordinación entre réplicas y manejo de la propiedad de la cola.

4. **Serialización en memoria.**
   - Ventajas: implementación sencilla dentro de un proceso.
   - Desventajas: no coordina las dos réplicas y no ofrece persistencia ante reinicios.

## Decisión

Se propone adoptar el bloqueo por oferta en PostgreSQL, acompañado de reservas persistidas por bid.

Cada oferta tendrá un registro persistido que permitirá coordinar las solicitudes concurrentes. Todas las aceptaciones de bids sobre una misma oferta deberán utilizar el mismo mecanismo de bloqueo.

El flujo será el siguiente:

1. Iniciar una transacción y comprobar si el bid ya fue procesado, identificándolo por su `idpk`. Si existe, devolver la misma decisión sin generar otra reserva.
2. Bloquear el registro de la oferta mediante `SELECT ... FOR UPDATE`.
3. Calcular la cantidad disponible considerando el `remaining` y las reservas activas de otros bids.
4. Si la cantidad solicitada no cabe completa, rechazar el bid con `QUANTITY_UNAVAILABLE` e informar `available`. No se aceptan bids parcialmente.
5. Si la cantidad cabe, registrar una reserva persistente asociada al bid y confirmar la transacción.
6. Fuera de la transacción, proponer el swap a la wallet mediante `POST /swaps`. Los reintentos de esta operación deben utilizar la misma `Idempotency-Key`.
7. Cuando llegue `swap-proposed`, confirmar la propuesta y enviar `bid-accepted` al comprador con el `swapId` y el `swapToken`.
8. Si la wallet rechaza la propuesta antes de confirmarla, liberar la reserva y responder `bid-rejected`.
9. Si un swap ya propuesto termina rechazado, cancelado o expirado, liberar la reserva. Si se liquida (`swap-settled`), consumirla, actualizar el `remaining` y publicar `offer-updated`.

Las reservas deben gestionarse mediante transiciones persistidas e idempotentes, para evitar liberarlas o consumirlas más de una vez ante eventos duplicados.

Las llamadas HTTP a la wallet se realizarán fuera de la transacción de PostgreSQL. Así evitamos mantener bloqueada una oferta durante una operación de red potencialmente lenta.

Ante un timeout o error ambiguo al proponer un swap, la reserva se conservará hasta conocer el resultado. No se debe liberar energía cuya situación todavía no está resuelta.

## Consecuencias y tradeoffs

- Los bids sobre una misma oferta se procesan secuencialmente.
- Las ofertas diferentes pueden procesarse en paralelo.
- El bloqueo y las reservas persistidas permiten coordinar las dos réplicas de `master`.
- Los bids duplicados no generan aceptaciones, reservas ni swaps adicionales.
- Se agrega complejidad por la persistencia de reservas y el manejo de sus estados.
- La implementación debe contemplar la recuperación de operaciones pendientes después de reinicios o fallas de comunicación con la wallet.
- El `remaining` solo se actualiza cuando el swap se liquida. Las reservas activas se consideran al calcular la cantidad disponible.

## Post-mortem (después de la demo)

Pendiente de completar después de la demo, documentando el comportamiento ante bids concurrentes, el resultado de las reservas y los rechazos o expiraciones de la wallet.