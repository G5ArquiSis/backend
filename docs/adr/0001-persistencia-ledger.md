# ADR-0002: Modelo y motor de persistencia del ledger

- **Estado:** propuesto
- **Fecha:** 2026-10-07
- **Responsable:** Vicente Pavez
- **Área del enunciado:** AD2


## Contexto

El sistema EnergyShark gestiona balances de energía (kWh) y presupuesto (créditos) organizados en ciclos de 2 horas. 
Para la decisión AD2, el enunciado exige la siguiente propiedad arquitectónica:
> *El estado del ledger de cualquier ciclo pasado es reconstruible y explicable desde lo persistido.*

Adicionalmente, se deben cumplir los siguientes requerimientos:
1. **Reglas de negocio del ledger:** El presupuesto se transfiere de forma acumulativa entre ciclos, mientras que el excedente de energía se descarta al cierre del ciclo.
2. **Historial explicable (RF01):** La API y la SPA deben exponer para cada ciclo su capacidad inicial, consumos, transferencias recibidas, demand-statements aplicados, operaciones voluntarias, report final y explícitamente cuál fue la **última operación aplicada**.
3. **Idempotencia y auditoría de duplicados (RF05 / Anomalía 1):** Mensajes sensibles con el mismo `idpk` reenviados no deben alterar dos veces los balances, pero deben quedar registrados en el registro de duplicados.
4. **Infraestructura:** El backend corre en contenedores Docker sobre una instancia EC2 (Free Tier) y hereda la base de datos PostgreSQL de la E0.

## Alternativas consideradas

### Dimensión 1: Motor de base de datos (SQL vs. NoSQL)

1. **NoSQL (MongoDB / DynamoDB):**
   - *Ventajas:* Esquema flexible para payloads JSON variables de distintos tipos de mensajes (`status-statement`, `transfer`, `demand-statement`).
   - *Desventajas:* Requiere agregar un servicio nuevo a Docker Compose y a la EC2, consumiendo recursos adicionales en Free Tier. Menor soporte nativo para transacciones atómicas complejas multi-tabla que garanticen consistencia estricta en balances financieros.
2. **SQL (PostgreSQL) [ELEGIDA]:**
   - *Ventajas:* Ya está configurado y operando en la infraestructura heredada de la E0. Provee garantías ACID estrictas indispensables para transacciones contables, soporte para columnas `JSONB` indexables para guardar payloads variables, y soporte de índices únicos para garantizar idempotencia por `idpk`.
   - *Desventajas:* Requiere esquemas definidos y migraciones.

### Dimensión 2: Modelo de persistencia (Snapshot vs. Event Log vs. Híbrido)

1. **Snapshot Puro (Solo estado final del ciclo):**
   - *Ventajas:* Consultas inmediatas $O(1)$.
   - *Desventajas:* **Viola la propiedad exigida.** No permite explicar cómo varió el balance con cada mensaje recibido, no permite auditar qué demand-statement alteró qué monto, ni permite identificar cuál fue la última operación aplicada en caso de auditoría.
2. **Event Log Puro (Event Sourcing estricto sin snapshots):**
   - *Ventajas:* Trazabilidad total; cada mensaje recibido se almacena como un evento inmutable. Reconstrucción completa garantizada.
   - *Desventajas:* Las lecturas del estado actual (`GET /history`, cálculo de `negotiation-report` por Rol C) requerirían recalcular y sumarizar todos los eventos desde el ciclo 0 en cada petición HTTP, lo que degrada fuertemente el rendimiento y CPU en una EC2 con recursos acotados.
3. **Modelo Híbrido (Event Log + Proyección/Snapshot por Ciclo) [ELEGIDA]:**
   - *Ventajas:* 
     - **Escritura y auditoría:** Cada transacción u operación aplicada se almacena como un evento inmutable en una tabla `ledger_events` (con `idpk`, tipo, deltas y payload).
     - **Lectura operacional:** Una tabla `cycle_ledger` mantiene la proyección consolidada del ciclo actual (budget acumulado, energía actual, balances y el `last_operation_idpk`).
     - **Reconstruibilidad:** Si la tabla `cycle_ledger` se corrompe o requiere auditoría, se puede recalcular desde cero a partir de `ledger_events`.
     - **Lecturas O(1):** Consultas de la SPA (RF01) y lecturas del módulo de negociación (Rol C) leen directamente el snapshot.

## Decisión

Se adopta **PostgreSQL** con un **Modelo Híbrido de Persistencia (Event Log + Proyección de Estado por Ciclo)**:
- Se utilizará la tabla `ledger_events` como *fuente de verdad inmutable*, con un índice único sobre `idpk` para garantizar idempotencia a nivel de base de datos.
- Se mantendrá la tabla `cycle_ledger` como *proyección materializada* del estado del ciclo, actualizada de forma atómica en la misma transacción SQL en la que se registra el evento.
- Para RF05, cualquier reintento con un `idpk` ya existente será detectado antes de aplicar los deltas y se registrará en `duplicate_messages` sin modificar `cycle_ledger`.

## Consecuencias y tradeoffs

- **Lo que se gana:**
  - Cumplimiento total de la reconstruibilidad y explicabilidad exigida en AD2.
  - Tiempos de respuesta óptimos ($O(1)$) en las lecturas de historial (RF01) y reportes de negociación (Rol C).
  - Integridad transaccional ACID: el evento y la actualización del balance ocurren juntos o fallan juntos.

- **Lo que se pierde / riesgos:**
  - Pequeña redundancia de datos (almacenar el delta en el evento y el total acumulado en el ciclo).
  - Complejidad adicional al tener que mantener sincronizados el log y el snapshot. Esto se mitiga ejecutando ambas mutaciones dentro de un mismo bloque transaccional (`session.commit()`).

## Post-mortem (después de la demo)

