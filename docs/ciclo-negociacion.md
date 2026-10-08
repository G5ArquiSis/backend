# Ciclo de negociación: ledger, reporte y negociaciones

Cómo `master` lleva el ledger, envía el `negotiation-report` y maneja las negociaciones
voluntarias, y qué necesita de `connector` para que todo corra. Las decisiones están en los ADRs
de [AD2](adr/0001-persistencia-ledger.md) y [AD3](adr/0003-timeouts-negociacion.md).

## Reparto de responsabilidades

```
central ──► city.TAL ──► connector ──POST /internal/messages──► master ──► Postgres
                             ▲                                     │
                             └────── POST /internal/outbox/claim ──┘
central ◄── publica ─────────┘
```

- **`connector` transporta.** Recibe de la central, valida el envelope, responde ACK o NACK,
  reenvía el mensaje a `master` y publica lo que `master` le entrega.
- **`master` decide y persiste.** Aplica los mensajes al ledger, lleva el estado de cada
  negociación y resuelve qué hay que enviar y cuándo.

`master` no tiene temporizadores. Cada vez que `connector` consulta la outbox, `master` revisa en
la base de datos los plazos vencidos y encola lo que corresponda. Por eso un reinicio no pierde
el reporte de un ciclo ni una negociación en curso.

## Contrato con `connector`

Son tres llamadas HTTP a `master`, por la red de Docker (`http://master:8000`).

### 1. Reenviar cada mensaje válido de la central

```
POST /internal/messages
Content-Type: application/json

<el mensaje completo, tal como llegó del broker>
```

| Respuesta | Significado | Qué hace `connector` |
|---|---|---|
| 200 `{"outcome": "applied"}` | Se aplicó | Confirmar el mensaje al broker |
| 200 `{"outcome": "duplicate"}` | Ese `idpk` ya estaba aplicado; no cambió nada | Confirmar el mensaje al broker |
| 200 `{"outcome": "ignored"}` | El tipo no afecta al ciclo (`ack`, `distance-table`, etc.) | Confirmar el mensaje al broker |
| 422 | Le falta un campo que su tipo exige | Responder NACK a la central |
| 5xx o sin respuesta | `master` no está disponible | Reencolar y reintentar; no confirmar |

Se reenvían todos los tipos: `master` descarta los que no usa. El ACK del protocolo a la central
lo sigue enviando `connector`; este endpoint no lo reemplaza.

### 2. Retirar los mensajes por publicar

```
POST /internal/outbox/claim
```

Responde una lista, vacía si no hay nada:

```json
[{ "id": 12, "payload": { "idpk": "...", "msgId": "...", "type": "negotiation-report", "...": "..." } }]
```

`payload` es el mensaje completo, listo para publicar a la central con `user_id = city.TAL`. Hay
que llamarlo **cada 5 segundos**, haya o no mensajes entrantes: es el reloj del sistema. Si deja
de llamarse, no sale el reporte ni vencen los timeouts.

### 3. Confirmar cada publicación

```
POST /internal/outbox/{id}/sent
```

Un mensaje retirado y no confirmado se vuelve a ofrecer a los 60 segundos. Así, si `connector`
se cae entre retirar y publicar, el mensaje no se pierde. Publicar dos veces el mismo mensaje no
hace daño: lleva el mismo `idpk`.

## Qué hace `master` con cada mensaje

| Tipo | Efecto |
|---|---|
| `status-statement` | Abre el ciclo, fija el balance de energía (`generationCapacity - consumption`) y programa el reporte |
| `transfer` sin `becauseOf` | Suma `quantity` al presupuesto |
| `demand-statement` | Suma `quantity` a la energía y resta `quantity × valuePerKwh` del presupuesto |
| `take` | Confirma nuestra compra, la aplica al ledger y encola nuestro `transfer` de pago |
| `give` | Confirma nuestra venta y abre el plazo de 30 segundos para recibir el pago |
| `transfer` con `becauseOf` | Pago de una venta: recién acá se aplica al ledger |
| `error`, `nack` | Rechaza la propuesta, o reprograma el reporte si es `REPORT_TOO_EARLY` |

El presupuesto se traspasa entre ciclos: el `budgetBalance` que se reporta es la suma de todos
los eventos. La energía es por ciclo.

## El `negotiation-report`

- Se programa al recibir el `status-statement`, para 15 segundos después de que abre el periodo
  de cierre (`validUntil` menos 5 minutos).
- Solo se envía por ciclos que la central abrió, y nunca después de `validUntil`.
- Ante `REPORT_TOO_EARLY` se reenvía en `data.opensAt`, con el mismo `idpk`.
- Si el ledger cambia después de reportar y la ventana sigue abierta, se envía una corrección
  con un `idpk` nuevo.

## API de negociaciones (RF04)

Entran por `https://api.melchort.me`, con el token de Auth0.

| Endpoint | Qué hace |
|---|---|
| `POST /negotiations` | Crea una propuesta: `{"direction": "give" \| "take", "quantity": 300, "pricePerEnergy": 220.5}`. Sin precio, oferta el tope del ciclo. Responde 409 con el motivo si no hay ventana abierta, si el precio supera el tope o si un `give` excede la energía vendible |
| `GET /negotiations` | Historial con estado: `pending`, `confirmed`, `paid`, `expired` o `rejected` |
| `GET /negotiations/{id}` | Detalle, con el número de intentos y el motivo de rechazo |
| `GET /cycles` | Últimos ciclos con su balance, el estado de su reporte y el presupuesto actual |

## Configuración

Variables de `master`, documentadas en [`.env.example`](../.env.example): `CITY_CODE`,
`REPORT_CLOSING_SECONDS`, `REPORT_MARGIN_SECONDS`, `NEGOTIATION_TIMEOUT_SECONDS`,
`NEGOTIATION_MAX_ATTEMPTS` y `OUTBOX_REDELIVERY_SECONDS`. Todas tienen un valor por defecto.

## Probarlo sin la central

Con el stack local arriba, simulando a `connector`:

```bash
# 1. Abrir un ciclo cuya ventana cierra en 2 minutos
curl -s -X POST http://127.0.0.1:8001/internal/messages -H 'content-type: application/json' -d '{
  "idpk": "prueba-1", "msgId": "m-1", "type": "status-statement", "cycleId": "cycle-1",
  "timestamp": "2026-01-01T00:00:00Z", "sender": "central",
  "data": {"energy": {"generationCapacity": 1000, "consumption": 400, "generationCost": 210},
           "validUntil": "'"$(date -u -d '+2 minutes' +%Y-%m-%dT%H:%M:%SZ)"'"}}'

# 2. Retirar la outbox: trae el negotiation-report, porque el periodo de cierre ya abrió
curl -s -X POST http://127.0.0.1:8001/internal/outbox/claim

# 3. Ver el estado
curl -s http://127.0.0.1:8001/cycles
```

## Lo que no cubre

Registro consultable de duplicados y de mensajes descartados (RF05), `distance-table` (RF02) e
historial de ciclos para la interfaz (RF01).
