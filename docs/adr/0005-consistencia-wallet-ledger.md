# ADR-0005: Consistencia entre ledger local y wallet v3

Estado: Propuesto (documentación previa a la implementación completa).

## Contexto

En v3 la wallet es la autoridad de saldos CR y KWH. El nodo conserva un ledger
append-only para trazabilidad y decisiones, pero el stream AMQP puede entregar
eventos duplicados, atrasados o fuera de orden; un HTTP 202 no es liquidación.

## Alternativas

1. Confiar sólo en la proyección local: rápido, pero no detecta las pérdidas de eventos
   o snapshots fuera de orden.
2. Consultar la wallet en cada operación: saldo correcto al instante de la lectura, pero
   consume cuota para consultar el historial, aumenta latencia y no preserva explicación histórica.
3. Eventos idempotentes más reconciliación periódica: proyección rápida, auditoría
   completa y recuperación desde balance y statement oficiales.

## Decisión

Elegimos la tercera alternativa. 

Se decide utilizar PostgreSQL para almacenar los eventos recibidos de la Wallet, manteniendo un 
registro de cada uno y evitando duplicados mediante la restricción `UNIQUE(idpk)`. Es importante 
distinguir este identificador de `data.idpk`, ya que este último corresponde a la operación HTTP 
original y puede aparecer en distintos eventos.

Para mantener la consistencia de los saldos, los snapshots solo se actualizan cuando llega 
información más reciente. Además, se utiliza un bloqueo transaccional compartido para evitar que 
distintos procesos modifiquen los saldos simultáneamente, incluso cuando todavía no existe un 
registro previo. En el caso de los swaps, su estado se determina a partir de los eventos recibidos,
considerando que un error al aceptar una operación no significa necesariamente que el swap haya 
sido rechazado.

También se decide implementar un mecanismo de reconciliación entre el ledger local y la Wallet, 
que es la fuente oficial de los saldos. Para esto, se consultan todas las páginas de 
`GET /statement` y se almacenan sus movimientos utilizando `entryId` para evitar duplicados, 
incluyendo ajustes (`adjustment`) y penalizaciones (`penalty`). Luego, se comparan los saldos 
locales con los obtenidos mediante `GET /balance`, registrando los valores anteriores, los 
oficiales y sus diferencias. Si se detectan discrepancias, se actualizan los saldos disponibles 
(`available`) y retenidos (`held`) según la información de la Wallet. De esta forma, se evita 
contabilizar nuevamente operaciones como `transfer` o `trade-recorded`.

La reconciliación se ejecuta automáticamente cada 180 segundos y después del cierre de cada ciclo,
 aprovechando el mecanismo existente de consulta de la outbox. Para evitar que las solicitudes 
 HTTP a la Wallet retrasen el envío de mensajes, se utiliza un worker con su propia sesión de base
  de datos. Además, PostgreSQL permite coordinar las distintas réplicas del sistema para evitar 
  ejecuciones simultáneas. Si ocurre algún error, este queda registrado y la operación se vuelve
   a intentar después de un intervalo definido, evitando generar ciclos de reintentos continuos.

Finalmente, se mantiene el formato y la ventana de envío de `negotiation-report` establecidos 
por el protocolo v3, asegurando que los cambios en la gestión de saldos no alteren el 
funcionamiento del cierre de cada ciclo.

## Consecuencias

La wallet siempre prevalece para mostrar el saldo autoritativo. La proyección
local puede divergir temporalmente, pero el delta debe ser visible y explicable.
Los cupos locales son compartidos dentro del proceso, por lo que la reconciliación 
debe respetar 300 lecturas/minuto y manejar los errores 429/503.

Los timestamps no constituyen secuencias globales, por lo que empates o eventos ausentes
se resuelven consultando el balance. 
Una paginación incompleta nunca publica una corrección parcial.

## Escenario AD5

Fuente: wallet. Estímulo: swap-settled duplicado, trade-recorded anterior al
settled, y 202 perdido. Ambiente: operación normal. Artefacto: aplicación de
eventos y ledger. Respuesta: aplicar cada evento una vez, tolerar desorden y
reconciliar. Medida: saldo al cierre igual a GET /balance o delta explicado.
