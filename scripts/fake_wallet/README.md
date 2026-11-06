# Wallet falsa

Imitación local de la wallet de la central (API v3, `wallet.openapi.yaml` del curso, tag
v3.0.2) para desarrollar y probar sin tocar la wallet real ni gastar su cupo. Viene con un
RabbitMQ local con la misma topología y los mismos permisos que el broker del curso, así que
el flujo completo es el de producción:

```
master ──HTTP──► fake-wallet ──evento──► rabbitmq (energy.x, city.TAL) ──► connector ──► master
```

**No es la wallet real.** Todo vive en memoria y se pierde al reiniciar. El puntaje
(`/leaderboard`) es fijo y no hay cierre de ciclo.

## Levantarla

```bash
cp .env.example .env                              # si no existe; completar POSTGRES_*
cat scripts/fake_wallet/env.fake >> .env          # apunta master y connector a lo local
docker compose --profile fake up --build
```

Sin `--profile fake` no se levantan ni `rabbitmq` ni `fake-wallet`. Las líneas de `env.fake`
quedan al final de `.env` y pisan a las anteriores. Para volver a la central real, se borran.
Esas credenciales sólo existen en el broker local, así que no son secretos.

| Servicio | Desde el host | Desde la red de compose |
|---|---|---|
| Wallet | `http://127.0.0.1:8090/v1` | `http://fake-wallet:8080/v1` |
| JWKS | `http://127.0.0.1:8090/.well-known/jwks.json` | `http://fake-wallet:8080/.well-known/jwks.json` |
| RabbitMQ (AMQP, sin TLS) | `127.0.0.1:5672`, vhost `energy` | `rabbitmq:5672` |
| Consola de RabbitMQ | `http://127.0.0.1:15672` (admin/admin) | — |

Colas del broker local: `city.TAL.q` (eventos para TAL), `market.TAL` (feed del mercado),
`central.q` (todo lo que la ciudad publica a la central, para inspeccionarlo) y
`observer.local.q`. Para los ensayos (`scripts/ensayos/`) hay una segunda ciudad, HGW
(usuario `city.HGW`, contraseña `local-hgw`), con sus colas `city.HGW.q` y `market.HGW`.

## Ciudades y saldos

Cada API key es una ciudad. Por defecto: `fake-key-tal` (TAL), `fake-key-hgw` (HGW) y
`fake-key-cor` (COR), cada una con 600 000 CR, 20 000 kWh y 1 200 000 CR de línea de
crédito. Con la key de otra ciudad se hace de contraparte. Por ejemplo, HGW acepta un swap
que propuso TAL:

```bash
W=http://127.0.0.1:8090
EXP=$(date -u -d '+40 sec' +%Y-%m-%dT%H:%M:%SZ)
curl -s -X POST $W/v1/swaps -H 'X-Api-Key: fake-key-tal' -H "Idempotency-Key: $(uuidgen)" \
  -H 'Content-Type: application/json' \
  -d "{\"give\":{\"asset\":\"KWH\",\"amount\":100},\"take\":{\"asset\":\"CR\",\"amount\":3300},\"expiresAt\":\"$EXP\",\"memo\":\"TAL-1\"}"
curl -s -X POST $W/v1/swaps/<swapId>/accept -H 'X-Api-Key: fake-key-hgw' -H "Idempotency-Key: $(uuidgen)"
```

Los eventos para HGW llegan a `city.HGW.q`; los de COR no tienen cola en el broker local. Todos se ven en `GET /_fake/events`.

## Qué imita

- Escrituras asíncronas: `202 {swapId, status: "accepted"}` y el resultado llega después
  (0,3 s por defecto) como evento a `city.{CODE}`, con el envelope de la E1,
  `sender: "central"` y `user_id` "central".
- `Idempotency-Key`: la misma llave con la misma petición devuelve `200` con el estado actual
  y no ejecuta nada. Con otra petición es `422 IDEMPOTENCY_MISMATCH`.
- Retenciones, línea de crédito en CR y liquidación atómica de las dos patas.
- Todos los rechazos de `swap-rejected`: `INSUFFICIENT_ASSET` (con `available` sólo en la
  copia de quien intentó la operación), `SAME_ASSET`, `SELF_SWAP`, `NOT_PROPOSER`,
  `SWAP_NOT_PROPOSED`, `SWAP_EXPIRED` y `MARKET_CLOSED`.
- Vencimiento (`swap-expired`) y cancelación (`swap-cancelled`).
- Token ES256 con los claims de `token/swap-token-claims.schema.json`, verificable con el
  JWKS. La llave se genera al arrancar: los tokens de antes de un reinicio dejan de verificar.
- `GET /swaps/{id}`, `/balance`, `/statement` (paginado con `cursor`) y `/leaderboard`.

Decisiones donde la referencia no es explícita:

- Un `accept` rechazado por `SELF_SWAP`, `SWAP_NOT_PROPOSED`, `SWAP_EXPIRED` o `MARKET_CLOSED`
  sólo le llega a quien lo intentó y no cambia el swap. `INSUFFICIENT_ASSET` le llega también
  al proponente y libera su retención, como dice la referencia.
- `expiresAt` en el pasado o a más de 60 s (con 2 s de tolerancia) es `422 MALFORMED_REQUEST`,
  y un monto en CR con más de dos decimales también.

## Controles (`/_fake`, no existen en la wallet real)

| Petición | Qué hace |
|---|---|
| `GET /_fake/state` | Saldos de todas las ciudades, swaps y fallas activas |
| `GET /_fake/events?city=TAL&type=swap-settled` | Eventos emitidos (los últimos 1000), incluidos los descartados |
| `POST /_fake/accounts/TAL` `{"cr": 1000, "kwh": 50, "creditLine": 0}` | Fija saldos y deja un asiento `adjustment` |
| `POST /_fake/market` `{"kwhEnabled": false}` | Cierra el mercado: cancela los swaps con KWH pendientes y rechaza los nuevos con `MARKET_CLOSED` |
| `POST /_fake/cycle` `{"cycleId": "cycle-2"}` | Ciclo con que se anotan los asientos nuevos |
| `POST /_fake/faults` | Inyecta una falla (abajo) |
| `DELETE /_fake/faults` | Quita las fallas |
| `POST /_fake/reset` | Vuelve todo al estado inicial |

### Fallas

```bash
curl -X POST http://127.0.0.1:8090/_fake/faults -H 'Content-Type: application/json' \
  -d '{"kind": "502", "times": 1, "operations": ["propose"]}'
```

| `kind` | Qué simula |
|---|---|
| `502`, `504` | Respuesta perdida: la escritura **sí** se ejecuta, pero la ciudad recibe 502/504 (504 espera `seconds`). Hay que reintentar con la misma `Idempotency-Key`. |
| `503` | `WALLET_BUSY` con `Retry-After`: nada se aceptó. |
| `429` | `RATE_LIMITED` con `Retry-After`: nada se aceptó. |
| `latency` | La respuesta tarda `seconds`. |
| `drop` | El evento no se publica: la ciudad tiene que reconciliar con `GET /swaps/{id}`. |
| `duplicate` | El evento se publica dos veces con el mismo `msgId`. |
| `delay` | El evento se publica `seconds` después. |

`times` (por defecto 1) dice a cuántas peticiones o eventos aplica. `operations` limita las
fallas HTTP a `propose`, `accept`, `cancel` o `read`, y por defecto aplican a todas.
`retryAfter` fija el header de 503 y 429.

## Configuración

Por variables de entorno del contenedor `fake-wallet` (en `docker-compose.yml`):

| Variable | Por defecto | Qué es |
|---|---|---|
| `FAKE_WALLET_KEYS` | `TAL=fake-key-tal,HGW=fake-key-hgw,COR=fake-key-cor` | Ciudades y sus API keys |
| `FAKE_WALLET_INITIAL_CR` / `_INITIAL_KWH` / `_CREDIT_LINE` | 600000 / 20000 / 1200000 | Saldos iniciales |
| `FAKE_WALLET_PROCESSING_SECONDS` | 0.3 | Demora entre el 202 y el evento |
| `FAKE_WALLET_AMQP_URL` | (vacío: no publica) | Broker al que publica los eventos |
| `FAKE_WALLET_SIGNING_KEY` | (vacío: se genera) | Ruta a un PEM EC P-256 para firmar los tokens |

## Tests

```bash
pytest scripts/fake_wallet
# Además valida respuestas y eventos contra los schemas del curso:
ENERGYSHARK_CONTRACTS_DIR=/ruta/a/energyshark-contracts pytest scripts/fake_wallet
```
