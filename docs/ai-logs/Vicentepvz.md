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

