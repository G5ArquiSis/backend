# AI log — Vicentepvz

- **Integrante:** Vicente Pavez
- **Autocompletado:** no

## 2026-10-07 — Análisis y redacción de ADR-0002 (AD2: Persistencia del ledger)

- **Herramienta y modo:** Antigravity, Gemini 3.8 Flash, agéntico.
- **Tarea:** Evaluar la estrategia de persistencia del ledger (Snapshot vs. Event Sourcing vs. Híbrido, SQL vs. NoSQL) y redactar el ADR-0002 para cumplir con AD2.
- **Prompts relevantes:**
  > "Ayudame a reescribir y validar este ADR, quiero que quede bien hecho y que cumpla con todos los requerimientos del enunciado"

- **Qué produjo la IA:** En el documento se formalizó el modelo híbrido (Event Log inmutable en `ledger_events` + proyección de estado consolidado en `cycle_ledger` sobre PostgreSQL), el esquema relacional con sus tipos de datos, la convención de signos de `demand-statement`, el manejo de traspaso de presupuestos/energía, y la garantía de idempotencia a nivel de base de datos (`UNIQUE(idpk)`) para RF05 y la Anomalía 1 de la demo.
- **Verificación:** Validación contra los requerimientos del enunciado E1 (reconstruibilidad de AD2, lecturas O(1) para RF01, idempotencia para RF05) y la plantilla `docs/adr/0000-plantilla.md`.
- **Correcciones del integrante:** Se priorizó pulir en detalle el ADR antes de generar el log, se ajustó la convención de nomenclatura del archivo para evitar colisiones con AD1 (`0002-persistencia-ledger.md`).
- **Referencia:** Rama `feature/ad2-persistencia-ledger`, commits de `docs/adr/` y PR de AD2.

## 2026-10-07 — Mejora de la especificación OpenAPI para el historial de ciclos (RF01)

- **Herramienta y modo:** Antigravity, Gemini 3.8 Flash, agéntico.
- **Tarea:** Reestructuración de la especificación OpenAPI 3.1.0 para los endpoints del historial de ciclos (`/cycles` y `/cycles/{cycleId}`) cumpliendo con todos los campos exigidos por RF01.
- **Prompts relevantes:**
  > "Ayudame a terminar y mejorar estos contratos, además, agrega las mejoras que creas necesarias"
- **Qué produjo la IA:** Edición de `openapi/openapi.yaml` en el repo `contratos`. Se redefinieron adecuadamente los endpoints `GET /cycles` (paginado) y `GET /cycles/{cycleId}`, junto con los esquemas de datos: `StatusStatementInfo`, `TransferInfo` (con soporte para penalizaciones), `DemandStatementInfo` (con deltas de energía y presupuesto), `VoluntaryNegotiationInfo`, `NegotiationReportInfo`, `BalancesInfo` y `LastOperationInfo` para identificar explícitamente la última operación aplicada.
- **Verificación:** Validación de sintaxis YAML y estructura OpenAPI 3.1.0 contra los requerimientos de RF01 y las necesidades de mocking del frontend.
- **Correcciones del integrante:** Se mejoró la definición de `TransferInfo` para incluir el tipo de operación y el rol de `giver`/`receiver` en cada transferencia, superando la simple lista de `penalties` del borrador anterior.
- **Referencia:** Rama `feature/rf01-cycles-openapi` en repo `contratos`, commit en `openapi/openapi.yaml`.

## 2026-10-07 — Modelado de entidades y tablas en SQLAlchemy

- **Herramienta y modo:** Antigravity, Gemini 3.8 Flash, agéntico.
- **Tarea:** Definir las entidades de base de datos en SQLAlchemy (`master/app/models.py`) correspondientes al modelo híbrido de AD2 (CycleLedger, LedgerEvent, DuplicateMessage, VoluntaryNegotiation) y actualizar fixtures de pruebas.
- **Prompts relevantes:**
  > "Siguiendo con el modelo de AD2, ahora quiero que me ayudes con los modelos de la db"
- **Qué produjo la IA:** Modelado de las tablas `cycle_ledger`, `ledger_events`, `duplicate_messages` y `voluntary_negotiations` en `master/app/models.py` preservando `DemandEvent` de la E0. Configuración de tipos relacionales, índices y llaves de unicidad para idempotencia (`UNIQUE(idpk)`). Actualización del fixture de truncado en `master/tests/conftest.py`.
- **Verificación:** Script de verificación de importación de modelos y registro en `Base.metadata.tables`.
- **Correcciones del integrante:** Se revisaron los modelos para asegurar que todos los campos requeridos por la especificación estuvieran presentes y con los tipos de datos correctos.
- **Referencia:** Repo backend.

## 2026-10-07 — Lógica de negocio del Ledger y tests con fixtures
- **Herramienta y modo:** Antigravity, Gemini 3.8 Flash, agéntico.
- **Tarea:** Implementar el servicio contable del ledger (`LedgerService`) con aplicación atómica de mensajes (`status-statement`, `transfer`, `demand-statement`), convención de signos, traspaso de presupuesto entre ciclos, idempotencia estricta por `idpk`, y suite de tests unitarios con fixtures JSON.
- **Prompts relevantes:**
  > "Ahora quiero que me ayudes con la lógica de negocio del ledger"
- **Qué produjo la IA:**
  - `master/app/ledger_service.py`: Implementación de `LedgerService` con métodos de procesamiento atómico (`process_message`), consulta de ciclos (`get_cycle_detail`, `list_cycles`) y reconstrucción matemática de balances desde el Event Log (`reconstruct_cycle_from_events`).
  - `master/app/schemas.py`: DTOs para el contrato de envelopes (`MessageEnvelope`), detalles de ciclo para RF01 (`CycleDetail`, `PaginatedCycles`) y resultados contables (`ProcessResult`).
  - `master/tests/fixtures/`: 5 archivos de fixtures JSON representativos de la mecánica de mensajes (`status_statement.json`, `transfer.json`, `demand_statement_positive.json`, `demand_statement_negative.json`, `negotiation_report.json`).
  - `master/tests/test_ledger_service.py`: Suite de tests unitarios que comprueba:
    1. Aplicación de `status-statement` y fijación de costos base.
    2. Acreditación de fondos vía `transfer`.
    3. Convención de signos positiva de `demand-statement` ($+quantity$ energía, $-(quantity \times value)$ presupuesto).
    4. Convención de signos negativa de `demand-statement` ($-|quantity|$ energía, $+ (|quantity| \times value)$ presupuesto).
    5. Idempotencia ante reenvío con mismo `idpk`: no muta los balances del ciclo y genera registro en `duplicate_messages` (RF05 y preparación para la Anomalía 1 de la demo).
    6. Traspaso entre ciclos: el presupuesto remanente se hereda y la energía no vendida se resetea a 0.
    7. Reconstruibilidad estricta: los balances acumulados coinciden exactamente con la suma de deltas históricos.
- **Verificación:** Compilación limpia de sintaxis (`py_compile`) y verificación de integridad de modelos y DTOs con Python 3.
- **Correcciones del integrante:** Se ajustaron los tests para que coincidieran con el modelo de datos y se aseguró que todos los casos de uso estuvieran cubiertos.
- **Referencia:** Rama `feature/ledger-implementation`.

## 2026-10-07 — Endpoints HTTP de ciclos y mensajes alineados con el frontend
- **Herramienta y modo:** Antigravity, Gemini 3.8 Flash, agéntico.
- **Tarea:** Implementar los controladores HTTP (`GET /cycles`, `GET /cycles/{cycle_id}` y `GET /message-log`) en FastAPI (`master/app/routers/cycles.py`), asegurando compatibilidad total con la SPA en React (`CycleHistory.jsx` y `Messages.jsx`) y manteniendo `/history` de la E0 100% operativo.
- **Prompts relevantes:**
  > "Ayudame con los endpoints de los ciclos y asegurate de que todo coincida con la parte del frontend"
- **Qué produjo la IA:**
  - `master/app/routers/cycles.py`: Rutas `GET /cycles`, `GET /cycles/{cycle_id}` y `GET /message-log`.
  - Inclusión en `master/app/main.py`: Registro del nuevo router sin alterar `history.router` ni `health.router` de la E0.
  - Sincronización en `master/app/schemas.py` y `contratos/openapi/openapi.yaml`: Adaptación de los DTOs para que coincidan de forma exacta con lo que leen `CycleHistory.jsx` (propiedades `opened`, `balances.energy`, `balances.budget`, `operations` con `kind` e `isLast`, `lastOperationIdpk`) y `Messages.jsx` (`category`, `messageType`, `idpk`, `reason`, `receivedAt`).
  - `master/tests/test_cycles_routes.py`: Tests unitarios y de integración de las rutas verificando el contrato exacto de respuesta consumido por el frontend.
- **Verificación:** Compilación con `py_compile` de router y suite de pruebas, verificación de rutas registradas y parámetros de paginación/filtros.
- **Correcciones del integrante:** No hubo modificaciones.
- **Referencia:** Rama `feature/ledger-implementation`.

## 2026-11-06 — Contrato interno I1: outbox con destino (Tarea A0 de E2)
- **Herramienta y modo:** Antigravity, Gemini 3.8 Flash, agéntico.
- **Tarea:** Implementar el contrato interno I1 (Tarea A0 de E2) definiendo la firma de `enqueue_publish(session, dedupe_key, payload, destination)` con `destination ∈ {CENTRAL, MARKET_OFFER, CITY(code)}`. Provee un stub funcional compatible con la base de datos actual y desacopla la lógica de outbox para destrabar el desarrollo en paralelo de los roles C (vendedor) y D (comprador).
- **Prompts relevantes:**
  > "Haz los cambios del paso 1, te dejo adjuntado lo que se referencia de la api v3:"
- **Qué produjo la IA:**
  - `master/app/outbox.py`: Módulo que introduce `DestinationType`, `Destination`, normalizador `parse_destination` y la función `enqueue_publish`. Resuelve destinos a sus pares `(exchange, routing_key)` según el protocolo v3 y las credenciales de TAL (`energy.x/central`, `market/market.offer.TAL`, `energy.x/city.{code}`).
  - `master/app/models.py`: Actualización del docstring del modelo `OutboxMessage` documentando las nuevas columnas `exchange` y `routing_key` que se migrarán a nivel de esquema en la Tarea A4.
  - `master/app/cycle_repository.py`: Delegación de la función interna `_enqueue` a `enqueue_publish(..., Destination.CENTRAL)` y reexportación de tipos para preservar retrocompatibilidad.
  - `master/tests/test_outbox.py`: Suite de tests unitarios que cubre resolución de exchanges/routing keys para cada destino, validación de formatos, rechazo de destinos inválidos y ejecución de encolado atómico con `dedupe_key`.
- **Verificación:** Validación de compilación limpia con `py_compile` en todos los archivos modificados y ejecución exitosa de los tests de `test_outbox.py`.
- **Correcciones del integrante:** Ninguna requerida; implementación probada y verificada.
- **Referencia:** Rama `feat/a0-outbox-destino`.

## 2026-11-06 — Schemas JSON del protocolo v3 y Simulador de mercado (Tareas A5 y A6)
- **Herramienta y modo:** Antigravity, Gemini 3.8 Flash, agéntico.
- **Tarea:** Definir los contratos formales en JSON Schema del protocolo v3 en el repositorio `contratos` (Tarea A5) e implementar el simulador de mercado con la ciudad fantasma MET en `backend` (Tarea A6) para habilitar las pruebas locales de los roles C y D y los tests de contrato en CI de E.
- **Prompts relevantes:**
  > "Ahora vamos con el paso 2: necesitamos dejar listos todos los schemas JSON del protocolo v3 en contratos para destrabar al equipo con el CI y armar el script del simulador de mercado local con la ciudad fantasma MET en backend"
- **Qué produjo la IA:**
  - `contratos/schemas/`:
    - 14 nuevos esquemas JSON: ofertas de mercado (`offer.schema.json`, `offer-updated.schema.json`, `offer-withdrawn.schema.json`, `market-closed.schema.json`), mensajes entre ciudades (`bid.schema.json`, `bid-accepted.schema.json`, `bid-rejected.schema.json`), liquidaciones centrales (`trade-recorded.schema.json`, `imbalance-settlement.schema.json`) y eventos asíncronos de wallet (`swap-proposed.schema.json`, `swap-settled.schema.json`, `swap-rejected.schema.json`, `swap-expired.schema.json`, `swap-cancelled.schema.json`).
    - Actualización de esquemas extendidos v3: `envelope.schema.json` con todos los tipos válidos, `status-statement.schema.json` con bloque `market` y `sellable`, `transfer.schema.json` con `balanceAfter`/`creditLine`/`penalty`, y `distance-table.schema.json` con `disabledRouteTransportCost` y estado de enlaces `enabled`.
    - Marcado de schemas retirados de E1 (`demand-statement`, `give-take`, `negotiation-proposal`) como deprecados/retirados con código 410 `TYPE_RETIRED`.
    - Documentación completa en `contratos/schemas/README.md`.
  - `contratos/AGENTS.md`: Incorporación de la sección "Protocolo v3: Mercado, Wallet y Reglas que no se rompen", especificando el intercambio de bids y ofertas, la verificación obligatoria de `user_id == city.{CODE}`, la validación de JWTs `swapToken` de la wallet y la ausencia de ACKs en el mercado público.
  - `backend/scripts/simulador_mercado.py`: Script CLI ejecutable para inyección de eventos en master (`/internal/messages`) o RabbitMQ, con modos para feed de mercado, posturas directas, anomalías de identidad (`IDENTITY_MISMATCH`), mensajes malformados y la emulación de la ciudad fantasma MET con descenso periódico de precios.
  - `backend/README.md`: Documentación de uso y ejemplos de comandos del simulador para el equipo.
- **Verificación:** Validación sintáctica de todos los esquemas JSON (`ALL JSON SCHEMAS ARE VALID`), validación con `py_compile` de `simulador_mercado.py` y ejecución de prueba del simulador con `--help`.
- **Correcciones del integrante:** Ninguna requerida; esquemas y simulador alineados con las referencias oficiales de la API v3.
- **Referencia:** Rama `feat/a5-a6-schemas-simulador`.



