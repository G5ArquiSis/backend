# Guía de Demo y Defensa Individual: Anomalía 1 (Rol B)

> Esta guía prepara al responsable del **Rol B (Vicente Pavez)** para ejecutar la **Anomalía 1** en vivo ante el ayudante y defender su arquitectura en la evaluación individual (~5 minutos).

---

## 1. ¿Qué exige el enunciado para la Anomalía 1?

- **Escenario inducido por el ayudante:** Se provoca el reenvío de un mensaje sensible con el mismo `idpk` (duplicación de red en RabbitMQ o reintento de la central).
- **Comportamiento esperado:**
  1. El ledger **no muta dos veces** los balances contables de energía o presupuesto.
  2. El mensaje duplicado queda registrado en la base de datos y es consultable en `/message-log` y en la SPA (RF05).
  3. El consumidor de mensajes recibe respuesta exitosa (HTTP 200) para confirmar el `ack` y no bloquear la cola.

---

## 2. Cómo ejecutar la Demo en Vivo

Puedes ejecutar la prueba automatizada o correr el script CLI que imprime cada paso:

### Opción A: Script interactivo (Recomendado para la demo en vivo)
```bash
# Contra tu backend local:
python3 scripts/demo_anomalia_1.py --url http://127.0.0.1:8001

# O contra producción (EC2 / API Gateway):
python3 scripts/demo_anomalia_1.py --url https://api.melchort.me
```

### Opción B: Test de integración con Pytest
```bash
pytest master/tests/test_demo_anomalia_1.py -v
```

### Opción C: Verificación visual en la SPA
1. En la página de **Historial de ciclos** (`CycleHistory.jsx`), muestra que los balances de presupuesto y energía no se duplicaron.
2. En la página de **Registro de mensajes** (`Messages.jsx`), filtra por "Duplicados" y muestra en pantalla la fila registrada con el `idpk`, motivo `DUPLICATE_IDPK` y fecha.

---

## 3. ¿Dónde vive esto en el código? (Ruta de archivos)

Si el ayudante te pide abrir el código y mostrar la implementación:

1. **Restricción de Unicidad en la Base de Datos:**
   - [`master/app/models.py`](../master/app/models.py):
     - `LedgerEvent.idpk`: `mapped_column(String(64), unique=True, index=True)` $\rightarrow$ La base de datos es la última línea de defensa contra duplicados.
     - `DuplicateMessage`: tabla dedicada para auditar intentos colisionados (RF05).

2. **Lógica de Detección e Idempotencia:**
   - [`master/app/ledger_service.py`](../master/app/ledger_service.py):
     - Método `process_message()`:
       1. Consulta si el `idpk` ya existe en `ledger_events`.
       2. Si existe: inserta en `duplicate_messages`, hace `commit`, no altera `cycle_ledger` y devuelve `is_duplicate=True` con deltas en 0.
       3. Si no existe: aplica los deltas contables sobre `cycle_ledger` e inserta el evento en `ledger_events` de forma atómica en una sola transacción SQL.

3. **Controlador HTTP:**
   - [`master/app/routers/cycles.py`](../master/app/routers/cycles.py):
     - `POST /ledger/messages`: ante `is_duplicate=True`, devuelve HTTP 200 para que `connector` (Rol A) haga `ack` al broker sin reencolar.
     - `GET /message-log`: expone los duplicados paginados para la interfaz.

---

## 4. Preguntas típicas de Defensa Individual (~5 minutos)

### Pregunta 1: "¿Por qué existe la tabla `duplicate_messages` separada de `ledger_events`?"
> **Respuesta:** *"Porque cumple con el principio de segregación de responsabilidades: `ledger_events` es nuestro Event Log inmutable y contable; solo contiene operaciones válidas que modificaron el saldo. Si mezcláramos los reintentos duplicados en la misma tabla contable, alteraríamos la fuente de verdad. La tabla `duplicate_messages` es un log de auditoría (RF05) que permite mostrar al ayudante y al operador cuándo, quién y cuántas veces se intentó re-ejecutar un mensaje sin ensuciar la contabilidad."*

### Pregunta 2: "¿Qué se rompe si eliminamos la verificación de `idpk` en el ledger?"
> **Respuesta:** *"Se rompe la consistencia financiera de la ciudad. Ante cualquier corte de red temporal o timeout de 30 segundos donde la central o el broker reenvíen un `demand-statement` o un `transfer`, el backend descontaría o acreditaría fondos dos veces. En un escenario de alta concurrencia, una ciudad podría quebrar artificialmente o acumular créditos ficticios. La idempotencia por `idpk` garantiza que procesar un mensaje 1 vez o 100 veces produzca exactamente el mismo saldo final."*

### Pregunta 3: "¿Por qué devolvemos HTTP 200 en lugar de un error HTTP 409/422 ante un duplicado?"
> **Respuesta:** *"Porque para el protocolo de mensajería (RabbitMQ), el mensaje duplicado ya fue atendido con éxito en el pasado. Si devolviéramos un error (como 500 o 400), el consumidor `connector` rechazaría o reencolaría el mensaje, creando un bucle infinito de reintentos. Al responder 200 con `is_duplicate=True`, le indicamos al consumidor que puede hacer `ack` con seguridad sabiendo que el estado contable ya está al día."*
