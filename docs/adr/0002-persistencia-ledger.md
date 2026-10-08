# ADR-0002: Modelo y motor de persistencia del ledger

- **Estado:** propuesto
- **Fecha:** 2026-10-07
- **Responsable:** Vicente Pavez
- **Área del enunciado:** AD2

> Este ADR debe commitearse **antes** que los commits que implementan la decisión (RDOC01).

## Contexto

El sistema EnergyShark gestiona balances de energía (kWh) y presupuesto (créditos) organizados en ciclos de 2 horas. 
Para la decisión **AD2**, el enunciado exige la siguiente propiedad arquitectónica:
> *El estado del ledger de cualquier ciclo pasado es reconstruible y explicable desde lo persistido.*

Adicionalmente, se deben satisfacer las siguientes reglas y requerimientos:
1. **Reglas contables del ledger:**
   - `status-statement`: fija capacidad de generación, consumo proyectado y costo de generación del ciclo.
   - `transfer`: transfiere fondos de la central al presupuesto (descontando multas si viene el campo `penalty`).
   - `demand-statement` (convención de signos):
     - `quantity > 0` (central entrega energía): balance energético $+quantity$; presupuesto $-(quantity \times valuePerKwh)$.
     - `quantity < 0` (central retira energía): balance energético $-|quantity|$; presupuesto $+ (|quantity| \times valuePerKwh)$.
   - **Traspaso entre ciclos:** El presupuesto remanente se acumula y traspasa al siguiente ciclo. En cambio, el excedente de energía no vendida no se almacena y se pierde al cierre del ciclo.
2. **Historial explicable (RF01):** La API y la SPA deben exponer para cada ciclo su capacidad inicial, consumos, transferencias recibidas, demand-statements aplicados, operaciones voluntarias de compra/venta (`give`/`take`), negotiation-report enviado, balances finales y explícitamente **cuál fue la última operación aplicada**.
3. **Idempotencia y registro de duplicados (RF05 / Anomalía 1):** Mensajes sensibles con un `idpk` ya procesado no deben alterar dos veces los balances contables, pero deben registrarse en una tabla/registro de duplicados para ser inspeccionados durante la demo.
4. **Infraestructura del proyecto:** El backend corre en contenedores Docker sobre una instancia EC2 (Free Tier) y hereda la base de datos PostgreSQL de la E0.

## Alternativas consideradas

### Dimensión 1: Motor de base de datos (SQL vs. NoSQL)

1. **NoSQL (MongoDB / DynamoDB):**
   - *Ventajas:* Esquema altamente flexible para los payloads heterogéneos de cada tipo de mensaje (`status-statement`, `transfer`, `demand-statement`).
   - *Desventajas:* Requiere agregar y desplegar un servicio adicional a Docker Compose y a la EC2, consumiendo memoria y CPU escasas en el Free Tier. No ofrece transacciones ACID relacionales nativas con la misma simplicidad para asegurar que la inserción de un evento y la actualización del saldo acumulado ocurran de forma atómica.
2. **SQL (PostgreSQL) [ELEGIDA]:**
   - *Ventajas:* Ya forma parte de la arquitectura heredada de la E0 (`master/app/database.py`, SQLAlchemy). Soporta transacciones ACID estrictas (fundamentales para consistencia contable), restricciones de unicidad sobre `idpk` para idempotencia confiable a nivel de motor, y tipos `JSONB` indexables con GIN para almacenar payloads arbitrarios sin perder estructuración relacional.
   - *Desventajas:* Requiere definición previa de esquemas y migraciones si cambian las estructuras.

### Dimensión 2: Modelo de persistencia (Snapshot vs. Event Log vs. Híbrido)

1. **Snapshot Puro (Solo estado final o acumulado del ciclo):**
   - *Ventajas:* Consultas inmediatas $O(1)$ de los balances finales.
   - *Desventajas:* **Viola la propiedad exigida.** No permite reconstruir ni explicar la evolución paso a paso del ledger ante cada transacción, imposibilita saber qué orden causó un descuadre o saldo negativo, y no permite identificar con certeza la última operación aplicada.
2. **Event Log Puro (Event Sourcing estricto sin snapshots):**
   - *Ventajas:* Trazabilidad total; cada mensaje recibido se almacena como un evento inmutable. Reconstrucción completa garantizada sumando deltas.
   - *Desventajas:* Rendimiento deficiente en lecturas. Cada consulta de la API (`GET /history`, RF01) o consulta de balances por parte del módulo de negociación (Rol C, para el `negotiation-report`) obligaría a recalcular y sumarizar todos los eventos desde el ciclo inicial. En una máquina EC2 t2/t3.micro, esto introduce latencia innecesaria y riesgo de degradación de CPU.
3. **Modelo Híbrido (Event Log + Proyección/Snapshot por Ciclo) [ELEGIDA]:**
   - *Ventajas:* 
     - **Escritura y auditoría:** Cada transacción u operación contable se almacena de forma inmutable en la tabla `ledger_events` (con `idpk`, tipo de evento, montos deltas, balances resultantes y payload).
     - **Lectura operacional:** La tabla `cycle_ledger` mantiene la proyección consolidada del ciclo actual (`budget_balance`, `energy_balance`, `last_operation_idpk`, etc.).
     - **Reconstruibilidad:** Si la tabla `cycle_ledger` sufre inconsistencias o se requiere auditar un ciclo histórico, el estado es 100% reproducible re-ejecutando en orden los deltas de `ledger_events`.
     - **Lecturas O(1):** Las consultas de la SPA (RF01) y las verificaciones rápidas de Rol C leen directamente el registro del ciclo sin recálculo.

## Decisión

Se adopta **PostgreSQL** con un **Modelo Híbrido de Persistencia (Event Log + Proyección de Estado por Ciclo)**:

### Esquema de Datos Propuesto

1. **`cycle_ledger` (Snapshot y estado proyectado por ciclo):**
   - `cycle_id` (PK, `VARCHAR(64)`): Identificador ordinal del ciclo (ej. `cycle-9431`).
   - `generation_capacity` (`INTEGER`): Capacidad informada en `status-statement`.
   - `consumption` (`INTEGER`): Consumo proyectado informado.
   - `generation_cost` (`NUMERIC`): Costo de generación base del ciclo.
   - `budget_balance` (`NUMERIC`): Saldo acumulado actual de créditos.
   - `energy_balance` (`INTEGER`): Saldo de energía del ciclo actual.
   - `last_operation_idpk` (`VARCHAR(64)`, nullable): Puntero al `idpk` de la última operación aplicada sobre este ciclo.
   - `is_closed` (`BOOLEAN`): Indicador de si el ciclo cerró y emitió su `negotiation-report`.
   - `created_at`, `updated_at` (`TIMESTAMPTZ`).

2. **`ledger_events` (Event Log inmutable - Fuente de Verdad):**
   - `id` (PK, autoincremental).
   - `idpk` (`VARCHAR(64)`, `UNIQUE`, indexado): Llave de idempotencia del mensaje.
   - `msg_id` (`VARCHAR(64)`): Identificador del mensaje recibido.
   - `cycle_id` (`VARCHAR(64)`, FK / indexado): Ciclo al que pertenece la operación.
   - `event_type` (`VARCHAR(32)`): Tipo (`status-statement`, `transfer`, `demand-statement`, `give`, `take`).
   - `delta_budget` (`NUMERIC`): Variación neta en créditos.
   - `delta_energy` (`INTEGER`): Variación neta en kWh.
   - `resulting_budget` (`NUMERIC`): Balance acumulado de presupuesto tras aplicar la operación.
   - `resulting_energy` (`INTEGER`): Balance de energía tras aplicar la operación.
   - `payload` (`JSONB`): Copia íntegra de los datos del mensaje para auditoría.
   - `applied_at` (`TIMESTAMPTZ`): Timestamp de aplicación.

3. **`duplicate_messages` (Auditoría de Duplicados - RF05 / Anomalía 1):**
   - `id` (PK, autoincremental).
   - `idpk` (`VARCHAR(64)`, indexado): Llave que causó la colisión.
   - `msg_id` (`VARCHAR(64)`): Mensaje recibido duplicado.
   - `event_type` (`VARCHAR(32)`): Tipo de mensaje.
   - `cycle_id` (`VARCHAR(64)`, nullable).
   - `detected_at` (`TIMESTAMPTZ`).

### Mecánica de Procesamiento
Al recibir un mensaje:
1. Se abre una transacción SQL en PostgreSQL.
2. Se verifica si el `idpk` ya existe en `ledger_events`:
   - Si **ya existe**, se registra en `duplicate_messages`, no se altera `cycle_ledger` y se devuelve confirmación sin error (idempotente).
   - Si **no existe**, se inserta el nuevo evento en `ledger_events` y se actualiza `cycle_ledger` (aplicando las reglas de signos y traspasos correspondientes, y actualizando `last_operation_idpk`).
3. Ambas escrituras se confirman atómicamente con un solo `session.commit()`.

## Consecuencias y tradeoffs

- **Lo que se gana:**
  - Cumplimiento estricto de la propiedad de AD2: cualquier ciclo pasado es reconstruible y explicable paso a paso.
  - Lecturas $O(1)$ sin latencia para los endpoints de historial (RF01) y para el módulo de negociación de Rol C.
  - Garantías ACID nativas que evitan inconsistencias financieras entre eventos y saldos.
  - Idempotencia garantizada por base de datos (`UNIQUE(idpk)`), permitiendo demostrar con éxito la **Anomalía 1** en la demo.
- **Lo que se pierde / riesgos:**
  - Redundancia controlada de datos entre `ledger_events` y `cycle_ledger`.
  - Riesgo de desincronización si las escrituras no fuesen atómicas, mitigado al estar encapsuladas en la misma transacción de PostgreSQL.

## Post-mortem (después de la demo)

*(A completar tras la evaluación de la demo con el ayudante, documentando el comportamiento real del ledger ante mensajes duplicados y concurrencia).*
