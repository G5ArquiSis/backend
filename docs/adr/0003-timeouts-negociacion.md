# ADR-0003: Manejo de timeouts de negociación

- **Estado:** propuesto
- **Fecha:** 2026-10-07
- **Responsable:** Melchor Guerrero
- **Área del enunciado:** AD3

> Este ADR debe commitearse **antes** que los commits que implementan la decisión (RDOC01).

## Contexto

En una negociación voluntaria el nodo envía una `negotiation-proposal` y la central responde en dos
pasos, cada uno con un plazo de 30 segundos:

1. La confirmación (`give` o `take`), que referencia la propuesta en `data.target`.
2. El pago (`transfer`). En un `take` lo emitimos nosotros; en un `give` lo emite la central.

Si un paso no llega a tiempo, el enunciado pide reintentar la operación con el **mismo `idpk`** y
un `msgId` nuevo. La propiedad exigida para AD3 es:

> Una operación reintentada nunca se aplica dos veces.

Hay cuatro condiciones del sistema que acotan la decisión:

- **Los contenedores se reinician.** Cada deploy reinicia `master` y `connector`, y la demo incluye
  botar un contenedor en medio de un ciclo. Un plazo que solo vive en memoria se pierde.
- **`master` corre en dos réplicas** sobre la misma base de datos. Lo que una réplica decide, la
  otra no debe decidirlo de nuevo.
- **Las respuestas pueden llegar tarde o repetidas.** Una confirmación puede llegar después de que
  ya reintentamos, y el broker puede entregar el mismo mensaje dos veces.
- **El `negotiation-report` tiene el mismo problema.** Debe salir dentro del periodo de cierre y,
  si la central responde `REPORT_TOO_EARLY`, reintentarse en el instante `data.opensAt`.

Este ADR asume que el estado y las decisiones viven en `master` y que `connector` transporta los
mensajes hacia y desde el broker. Esa topología le corresponde a AD1; si AD1 la cambia, el
mecanismo de este ADR se mantiene y solo cambia el proceso que lo ejecuta.

## Alternativas consideradas

1. **Temporizadores en memoria** (`asyncio.sleep` o `call_later` en el proceso que envió la
   propuesta).
   - Ventajas: es lo más simple y el plazo se cumple con precisión de milisegundos.
   - Desventajas: se pierden en cada reinicio, de modo que una propuesta en vuelo quedaría para
     siempre en "pendiente". Con dos réplicas, el temporizador vive en una sola, y la respuesta
     puede llegar a la otra. No hay forma de explicar después por qué se reintentó.

2. **Plazos persistidos, evaluados en una revisión periódica.** Cada negociación guarda en la base
   de datos su estado, su número de intento y el instante en que vence. Una revisión que corre
   cada pocos segundos busca las vencidas y decide.
   - Ventajas: sobrevive a reinicios, porque al volver basta leer la tabla. Funciona con dos
     réplicas si la transición de estado es condicional. Deja el historial de intentos consultable
     (RF04). Sirve igual para programar el `negotiation-report`.
   - Desventajas: el plazo se cumple con la granularidad de la revisión, no exacto. Agrega tablas
     y consultas.

3. **Temporizadores en el broker** (mensajes con TTL y una cola de mensajes vencidos).
   - Ventajas: el plazo también sobrevive a nuestros reinicios y no hay que consultar nada.
   - Desventajas: exige declarar colas y políticas en el broker del curso, que no administramos y
     donde solo tenemos permisos sobre `city.TAL`. Si el broker se cae, se cae también el reloj.

4. **Un planificador externo** (cron, o una cola de tareas como Celery con su propio almacén).
   - Ventajas: es una herramienta hecha para esto.
   - Desventajas: es al menos un servicio más en una instancia de 1 GB de RAM, con su propio
     healthcheck y despliegue, para resolver un problema que cabe en una tabla.

## Decisión

Se adopta la **alternativa 2**: plazos persistidos en Postgres y evaluados en una revisión
periódica, con tres barreras independientes contra la doble aplicación.

**Máquina de estados.** Cada negociación es una fila con un `idpk` propio, que identifica la
operación y no cambia entre intentos:

```
pendiente ──confirmación──► confirmada ──pago──► pagada
    │                           │
    │ vence el plazo            │ (give) el pago no llega en 30 s
    ▼                           ▼
 reintento con el mismo idpk y msgId nuevo (máximo 3 envíos en total)
    │
    ▼ se agotan los envíos, o la ventana del ciclo ya cerró
 expirada

pendiente ──error de la central──► rechazada
```

**Plazos.** Al enviar la propuesta se guarda el instante de vencimiento (30 segundos después). La
revisión periódica toma las negociaciones vencidas y, para cada una, reintenta o la marca como
expirada. Corre cada 5 segundos, así que un timeout se detecta entre 30 y 35 segundos después del
envío.

**Reintentos acotados.** Hasta 3 envíos por operación. No se reintenta si la ventana de
negociación del ciclo ya cerró: la central respondería `CYCLE_EXPIRED`.

**Errores de la central.** `PRICE_ABOVE_CAP`, `OVER_CAPACITY`, `CYCLE_EXPIRED` y `CYCLE_UNKNOWN`
dejan la negociación como rechazada y no se reintentan solos: repetir el mismo mensaje daría el
mismo error. Corregir es una operación nueva, con un `idpk` nuevo.

**Barreras contra la doble aplicación:**

1. **`idpk` único en el ledger.** El efecto de una negociación sobre el ledger se registra en
   `ledger_events` con el `idpk` de la operación, y esa columna tiene un índice único (ADR de AD2).
   Da igual cuántas confirmaciones lleguen, o de cuál intento: la segunda inserción no hace nada.
2. **Transiciones condicionales.** Cada cambio de estado es un `UPDATE ... WHERE estado =
   <el esperado>`. Si dos réplicas, o dos entregas del mismo mensaje, intentan la misma
   transición, solo una modifica la fila y solo esa continúa.
3. **Correlación por todos los intentos.** Se guarda el `msgId` de cada envío. Una respuesta se
   asocia a su negociación si su `data.target` coincide con cualquiera de ellos, de modo que la
   confirmación tardía del primer intento no se trata como un mensaje desconocido.

**Cuándo se aplica al ledger:**

- En un `take` (compramos), al recibir la confirmación: sube la energía, baja el presupuesto y se
  emite nuestro `transfer`, con `becauseOf` igual al `msgId` de la confirmación.
- En un `give` (vendemos), al recibir el `transfer` de la central y no antes: el enunciado indica
  que, sin pago, no hubo operación real.

**Respuestas tardías.** Si la confirmación llega cuando la negociación ya está expirada, se aplica
igual, una sola vez, y la negociación pasa a confirmada. La central ya ejecutó la operación, y un
ledger que la ignore reportaría un balance distinto del que la central espera.

**El `negotiation-report` usa el mismo mecanismo.** El instante de envío se calcula desde el
`validUntil` del `status-statement` persistido, menos la duración del periodo de cierre (5
minutos, configurable). Un `REPORT_TOO_EARLY` reprograma el envío para `data.opensAt`, con el
mismo `idpk`, porque es la misma operación. Una corrección del reporte lleva un `idpk` nuevo.

## Consecuencias y tradeoffs

**Lo que se gana**

- Un reinicio de cualquier contenedor no pierde negociaciones ni el reporte del ciclo: al volver,
  la revisión encuentra lo vencido en la tabla.
- La propiedad exigida no depende de que el código tenga cuidado: la garantiza un índice único de
  la base de datos.
- Cada reintento queda registrado, con su `msgId` y su hora, y se puede mostrar en la interfaz de
  administración.

**Lo que se pierde**

- Precisión: el timeout se detecta con hasta 5 segundos de atraso. Para un plazo de 30 segundos y
  un periodo de cierre de 5 minutos es aceptable.
- Simplicidad: hay más tablas y más consultas que con un `sleep`.
- La revisión periódica consulta la base de datos aunque no haya nada pendiente.

**Riesgos que quedan**

- Si la revisión deja de correr, nada vence ni se reporta. Depende de que el proceso que la
  dispara esté vivo, y por eso debe verse en el monitoreo.
- Aplicar una confirmación tardía sobre una negociación expirada es una interpretación nuestra del
  enunciado. Si la central no la considera ejecutada, el ledger quedaría con una operación de más.
- El tope de 3 envíos es arbitrario; no hay datos todavía de cuánto tarda la central en responder.

## Post-mortem (después de la demo)

Qué pasó en la realidad: qué funcionó, qué no, qué cambiaríamos.
