# Ciclo de negociación: ledger, wallet y reporte

Cómo `master` lleva el ledger, aplica los eventos de la wallet de la central, se concilia con
ella y envía el `negotiation-report`, y qué necesita de `connector` para que todo corra. Las
decisiones están en los ADRs de [AD2](adr/0002-persistencia-ledger.md),
[AD3](adr/0003-timeouts-negociacion.md) y [AD5](adr/0005-consistencia-wallet-ledger.md).
Actualizado el 2026-11-06 para el protocolo v3; describe lo que está en `main`.

## Qué cambió en la E2

- **La central ya no negocia.** Opera la wallet (`CR` y `KWH`) y liquida al cierre. El flujo de
  la E1 con ella (`negotiation-proposal`, `give`, `take`, `demand-statement`) está retirado:
  `master` ya no aplica esos tipos y `POST /negotiations` responde 410.
- **Los saldos los manda la wallet.** Sus resultados llegan como eventos a `city.TAL`
  (`swap-*`, `trade-recorded`, `imbalance-settlement`, `market-closed`) y `master` los aplica
  una sola vez por `idpk`. Además, se concilia contra `GET /balance` (AD5).
- **El mercado es entre ciudades.** Ofertas en el exchange `market`, bids a `city.{vendedor}` y
  swaps en la wallet. La outbox ya guarda el destino de cada mensaje (contrato I1, PR #24); el
  consumo de `market.TAL`, la publicación a esos destinos (PR #28) y la estrategia de compra y
  venta (roles C y D) todavía no están en `main`.

## Reparto de responsabilidades

```
central, wallet y ciudades ──► city.TAL.q ─► connector ──POST /internal/messages──► master ──► Postgres
                                                ▲                                   │  │
                                                └────── POST /internal/outbox/claim ┘  │
central ◄── publica ────────────────────────────┘                                      │
wallet de la central ◄── HTTPS (X-Api-Key) ────────────────────────────────────────────┘
```

- **`connector` transporta.** Recibe de la central, valida el envelope, responde ACK o NACK,
  reenvía el mensaje a `master` y publica lo que `master` le entrega. Atiende dos colas, cada
  una con su conexión: la de la ciudad (`city.TAL.q`, protocolo de la E1) y la del observer
  (`demand-set` de la E0, que sigue alimentando `/history`).
- **`master` decide y persiste.** Aplica los mensajes y los eventos de la wallet al ledger,
  llama a la wallet y resuelve qué hay que enviar y cuándo. `connector` nunca llama a la wallet.

`master` no tiene temporizadores. Cada vez que `connector` consulta la outbox, `master` revisa en
la base de datos los plazos vencidos, encola lo que corresponda y, si toca, se concilia con la
wallet. Por eso un reinicio no pierde el reporte de un ciclo.

## Qué confirma `connector` al broker

| Situación | Al broker | A la central |
|---|---|---|
| `master` guardó el mensaje (o era un duplicado) | Lo confirma | ACK, salvo que sea un `ack`, `nack` o `error` |
| `master` no responde o falla | Lo reencola | Nada: no se confirma lo que aún se puede perder |
| El envelope es inválido, o `master` rechaza el contenido | Lo confirma | NACK, y queda en el registro |
| No se puede parsear o no trae `msgId` | Lo confirma | Nada: no hay a quién responder; queda en el registro |
| Es un `ack`, `nack` o `error` inválido | Lo confirma | Nada: no hay NACK de un ACK, de un NACK ni de un error; queda en el registro como descartado |

Si se cae la conexión al broker, `connector` reintenta con espera creciente y sigue marcando su
señal de vida: la API sigue sirviendo lo ya persistido, y los mensajes sin confirmar vuelven
solos a la cola.

## Contrato con `connector`

Son cuatro llamadas HTTP a `master`, por la red de Docker (`http://master:8000`).

### 1. Reenviar cada mensaje válido

```
POST /internal/messages
Content-Type: application/json

<el mensaje completo, tal como llegó del broker>
```

| Respuesta | Significado | Qué hace `connector` |
|---|---|---|
| 200 `{"outcome": "applied"}` | Se aplicó | Confirmar el mensaje al broker |
| 200 `{"outcome": "duplicate"}` | Ese `idpk` ya estaba aplicado; no cambió nada y quedó en el registro de duplicados | Confirmar el mensaje al broker |
| 200 `{"outcome": "ignored"}` | El tipo no afecta al estado (`ack`, `request`, etc.) | Confirmar el mensaje al broker |
| 422 | Le falta un campo que su tipo exige | Responder NACK a la central |
| 5xx o sin respuesta | `master` no está disponible | Reencolar y reintentar; no confirmar |

Se reenvían todos los tipos, vengan de la central, de la wallet o de otra ciudad: `master`
descarta los que no usa. El ACK del protocolo a la central
lo sigue enviando `connector`; este endpoint no lo reemplaza.

### 2. Retirar los mensajes por publicar

```
POST /internal/outbox/claim
```

Responde una lista, vacía si no hay nada:

```json
[{ "id": 12, "payload": { "idpk": "...", "msgId": "...", "type": "negotiation-report", "...": "..." } }]
```

`payload` es el mensaje completo, listo para publicar con `user_id = city.TAL`. Hoy `connector`
lo publica todo a la central (`energy.x`, routing key `central`); la outbox ya guarda el
exchange y la routing key de cada mensaje, y publicarlos a su destino es parte del PR #28.

Hay que llamarlo **cada 5 segundos**, haya o no mensajes entrantes: es el reloj del sistema. Si
deja de llamarse, no sale el reporte, no se piden los status-statement que falten y no se
concilia con la wallet. Cada llamada lanza la reconciliación en segundo plano: corre cada
`WALLET_RECONCILE_INTERVAL_SECONDS` y una vez más al vencer el plazo del reporte de un ciclo.
Un advisory lock de Postgres evita que las dos réplicas llamen a la wallet a la vez.

### 3. Confirmar cada publicación

```
POST /internal/outbox/{id}/sent
```

Un mensaje retirado y no confirmado se vuelve a ofrecer a los 60 segundos. Así, si `connector`
se cae entre retirar y publicar, el mensaje no se pierde. Publicar dos veces el mismo mensaje no
hace daño: lleva el mismo `idpk`.

### 4. Informar lo que no se aplicó

```
POST /internal/message-log
Content-Type: application/json

{ "category": "discarded" | "nack", "rawContent": "<cuerpo tal como llegó>", "reason": "MALFORMED_MESSAGE",
  "userId": "city.HGW" }
```

`connector` lo llama cuando descarta un mensaje (no se pudo parsear o no trae `msgId`) y cuando
responde NACK. `rawContent` es texto, porque un mensaje descartado puede no ser JSON. `userId` es
el `user_id` AMQP con que llegó (opcional): solo `connector` lo ve, y junto al `cityId` del mensaje
muestra un `IDENTITY_MISMATCH` de otra ciudad (RF05). Los duplicados no se informan por acá: los
registra `master` al detectarlos.

El NACK se publica a quien mandó el mensaje según su `user_id` (RabbitMQ no deja publicar con
otro): `city.X` a la cola de esa ciudad, y la central a la suya. Un bid sin `user_id` recibe
`IDENTITY_MISMATCH` en la cola de la ciudad que dice ser.

## Qué hace `master` con cada mensaje

| Tipo | Origen | Efecto |
|---|---|---|
| `status-statement` | Central | Abre el ciclo, fija el balance de energía (`generationCapacity - consumption`) y programa el reporte para `market.reportOpensAt` más 15 s. Los límites de `market.limits` ajustan el cliente de la wallet |
| `transfer` | Central | Suma `quantity` al presupuesto |
| `distance-table` | Central | Guarda la tabla; la vigente es la última recibida. Cada ruta trae `enabled` |
| `swap-proposed`, `swap-settled`, `swap-rejected`, `swap-expired`, `swap-cancelled` | Wallet | Registra el evento una vez por `idpk`, liga el `swapId` a su ciclo y actualiza los saldos locales con los que trae el evento |
| `trade-recorded` | Wallet | Notificación del trato. No crea asientos: los oficiales vienen en `GET /statement` |
| `imbalance-settlement` | Wallet | Liquidación del desbalance al cierre: deja en cero la energía del ciclo |
| `market-closed` | Wallet | Se registra como evento del ciclo |
| `offer`, `bid`, `bid-accepted`, `bid-rejected`, ... | Otras ciudades | Se guardan en el historial del ciclo (`cycle_market_messages`). Decidir qué hacer con ellos es de la estrategia (roles C y D) |
| `error`, `nack` | Central | Reprograma el reporte si es `REPORT_TOO_EARLY`; si no, guarda el motivo |

Los eventos de la wallet pueden llegar en cualquier orden y repetidos: cada uno se aplica una
sola vez, y un evento viejo no pisa un saldo más nuevo.

El presupuesto se traspasa entre ciclos: el `budgetBalance` que se reporta es la suma de todos
los eventos. La energía es por ciclo.

## El `negotiation-report`

- Se programa al recibir el `status-statement`, para 15 segundos después de que abre el periodo
  de cierre: `market.reportOpensAt` en el protocolo v3, o `validUntil` menos 5 minutos si el
  mensaje no lo trae.
- Solo se envía por ciclos que la central abrió, y nunca después de `validUntil`.
- Ante `REPORT_TOO_EARLY` se reenvía en `data.opensAt`, con el mismo `idpk`.
- Si el ledger cambia después de reportar y la ventana sigue abierta, se envía una corrección
  con un `idpk` nuevo.

## Reconciliación con la wallet (AD5)

La wallet es la fuente de verdad. `master` le pide `GET /balance` y `GET /statement`, guarda los
saldos oficiales (`wallet_balances`) y los movimientos, y calcula la diferencia con su ledger
(wallet menos local) por activo. El resultado se ve en `GET /wallet/balance-status`, junto con
el estado de la última sincronización (`attemptedAt`, `completedCycle`, `error`). Si la wallet
no responde, se guarda el tipo de error y se reintenta en el siguiente intervalo, no en cada
llamada a la outbox.

`POST /internal/wallet/reconcile` fuerza una reconciliación. Es interno (Nginx bloquea
`/internal/`) y sirve para probar en local.

## Peticiones directas

El enunciado pide solicitar el estado y las tablas de transmisión si no llegan al abrir la
ventana. `master` lo hace con un `request` (sin `cycleId`, porque no está ligado a un ciclo):

- Pide el `status-statement` cuando la central habló de un ciclo (por ejemplo, con su `transfer`)
  y su estado no llegó dentro de 30 segundos. Si la central no nombró un ciclo, no se le debe
  reporte y no se pide nada.
- Lo vuelve a pedir cada 2 minutos, con un tope de 3 pedidos por ciclo: repetirlo no cambia la
  respuesta y la política de abuso del broker castiga los loops.
- Junto con cada pedido del estado, pide la `distance-table` si todavía no hay ninguna guardada.
- El conteo de pedidos vive en la outbox (`dedupe_key` por intento): no hay columnas nuevas, y
  las dos réplicas no repiten un mismo pedido.

## API para la interfaz

Entran por `https://api.melchort.me`, con el token de Auth0. Por `https://melchort.me` responden
403.

| Endpoint | Qué hace |
|---|---|
| `GET /cycles` | Ciclos del más reciente al más antiguo, con su balance y el presupuesto actual (RF01) |
| `GET /cycles/{cycleId}` | Historial de un ciclo: `statusStatement`, `transfers`, `demandStatements`, `negotiations`, `report` y `balances`. `operations` trae todo en orden, y la última lleva `isLast: true` (RF01) |
| `GET /connectivity` | La `distance-table` vigente: destino, distancia, `transportCost` y `enabled` (RF02) |
| `GET /message-log` | Duplicados, descartados y NACK, paginados, con la ciudad emisora (`cityId`, o `central`) y el `userId` AMQP. Filtra con `?category=duplicate\|discarded\|nack`, `cityId`, `since` y `until`; `counts` trae cuántos hay de cada categoría con los demás filtros (RF05) |
| `GET /wallet/balance-status` | Saldos oficiales de la wallet, su diferencia con el ledger y el estado de la última reconciliación (AD5) |
| `POST /negotiations` | Retirado en el protocolo v3: siempre responde 410 |
| `GET /negotiations`, `GET /negotiations/{id}` | Historial de las negociaciones de la E1, de solo lectura |

## Configuración

Variables de `master`, documentadas en [`.env.example`](../.env.example): `CITY_CODE`,
`REPORT_CLOSING_SECONDS`, `REPORT_MARGIN_SECONDS`, `NEGOTIATION_TIMEOUT_SECONDS`,
`NEGOTIATION_MAX_ATTEMPTS`, `OUTBOX_REDELIVERY_SECONDS`, `STATEMENT_REQUEST_GRACE_SECONDS`,
`STATEMENT_REQUEST_INTERVAL_SECONDS` y `STATEMENT_REQUEST_MAX_ATTEMPTS`. Todas tienen un valor
por defecto.

Las de la E2: `MARKET_ENABLED` y `WALLET_ENABLED` (flags, apagados por defecto),
`WALLET_BASE_URL`, `WALLET_API_KEY` (secreto, nunca al repo) y
`WALLET_RECONCILE_INTERVAL_SECONDS`.

Los ensayos de las anomalías de la demo y el escenario de punta a punta están en
[`scripts/ensayos/README.md`](../scripts/ensayos/README.md); corren contra el stack local con la
wallet y el broker falsos.

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

## Datos del broker para la ciudad

Los entrega el curso junto con la contraseña. Tres nombres se parecen y no son intercambiables:

| Dato | Valor | Dónde se usa |
|---|---|---|
| Usuario | `city.TAL` | Autenticación y propiedad `user_id` de lo que publicamos |
| Cola | `city.TAL.q` | Lo que consumimos; el usuario no tiene permisos sobre otro nombre |
| Exchange | `energy.x` | A donde publicamos a la central y a otras ciudades |
| Routing key de la central | `central` | Con la que publicamos a la central |
| Routing key de otra ciudad | `city.{CODE}` | Bids y respuestas a esa ciudad (E2) |
| Cola del mercado | `market.TAL` | Feed de ofertas (E2, con `MARKET_ENABLED`) |
| Exchange del mercado | `market`, routing key `market.offer.TAL` | Nuestras ofertas (E2) |
| Virtual host | `energy` | El mismo del observer |

La cola no se declara, ni en modo pasivo: el usuario de la ciudad no tiene permiso de
configuración sobre ella y el broker cierra el canal con 403.

## Cómo activarlo en producción

`connector` consume la cola de la ciudad solo si existe `CITY_BROKER_PASSWORD` en el `.env` de
la EC2. Sin ella sigue atendiendo únicamente la cola del observer. Para cargarla, por Session
Manager, sin que la contraseña quede en el historial:

```bash
sudo bash -c 'read -rsp "Contraseña de city.TAL: " K && echo && printf "CITY_BROKER_PASSWORD=%s\n" "$K" >> /opt/energyshark/.env'
cd /opt/energyshark && sudo docker compose --env-file .env --env-file release.env -f docker-compose.prod.yml up -d --no-deps connector
sudo docker logs -f energyshark-e1-connector-1     # debe decir "Escuchando la cola city.TAL.q"
```

`--no-deps` importa: el `.env` es compartido, y sin esa opción Compose también recrea Postgres y
`master-1` porque su configuración cambió.

## Lo que no cubre

- El consumo de `market.TAL`, la verificación de identidad de otras ciudades
  (`IDENTITY_MISMATCH`) y la publicación a destinos distintos de la central: PR #28 (rol A).
- La oferta propia, la atención de bids, la compra y la verificación del token del swap: roles C
  y D. Cuando entren, este documento se actualiza con su flujo.
- El nombre de la tabla de RF05 es `message_log`, con una columna de categoría. El ADR de AD2 la
  llamaba `duplicate_messages`; se generalizó para guardar también descartados y NACK.
