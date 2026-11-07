# Lado comprador: libro de ofertas, bids y aceptación del swap

> Rol D de la E2 (tareas D0 a D6). Cubre G04 como comprador, RF02, RF03 (comprador) y RF06. El
> manejo de plazos es el de [AD3](adr/0003-timeouts-negociacion.md#anexo-e2-los-bids-del-comprador).

## Flujo

```
market.TAL ──offer/offer-updated/offer-withdrawn──► libro (market_offers)
                                                        │
status-statement (déficit, imbalancePrice, minBid,      ▼
lastBidAt) + distance-table (transporte) ──────► buyer_tick ──bid──► city.{vendedor}
                                                        ▲                │
                                       bid-accepted ────┘◄───────────────┘
                                       (swapId, swapToken)
                                                        │
                       verificar el token (JWKS) ───────┤
                                                        ▼
                       POST /swaps/{id}/accept (Idempotency-Key fija) ──► wallet
                                                        │
                                       swap-settled ◄───┘  (city.TAL)
```

Todo el código está en `master/app/market/`:

| Archivo | Qué hace |
|---|---|
| `models.py` | Tablas `market_offers` (libro) y `market_bids` (bids de TAL y su swap) |
| `book.py` | D0/D1: aplica el feed, calcula transporte y precio efectivo; contrato I4 |
| `buyer.py` | D3/D4: `buyer_tick`, respuestas del vendedor y eventos de la wallet que cierran un bid |
| `token.py` | D4: verificación del `swapToken` y caché del JWKS |
| `experiment.py` | D5: datos del experimento de span |
| `strategy.py` | C0 (rol C): `get_active_strategy(cycle_id)`, de donde salen el span y la política |

## Libro de ofertas (D0, D1)

- El feed puede llegar desordenado o repetido. Cada oferta guarda el `timestamp` y el `idpk`
  del último mensaje aplicado: un mensaje más viejo no pisa nada (salvo un `offer` viejo, que
  aporta la cantidad original) y el mismo `idpk` es un duplicado.
- `offer-withdrawn` y `market-closed` son terminales: la oferta no se reabre.
- Las ofertas de TAL no entran al libro.
- **Precio efectivo** = `pricePerEnergy` + `round2(distancia_km × transportCost)`, con la
  distance-table vigente (la última recibida; `distance` viene en metros). Una ruta con
  `enabled: false` paga `disabledRouteTransportCost`. Sin ruta hacia el vendedor, el precio
  efectivo es desconocido y la oferta no se compra.
- **Contrato I4**, para C y E: `list_open_offers(session, cycle_id)` (ofertas abiertas de la más
  barata a la más cara) y `effective_price(session, offer)`.
- El `remaining` ajeno puede estar viejo; un `bid-rejected QUANTITY_UNAVAILABLE` con
  `available` lo corrige. Además, al llegar nuestro `bid-accepted` se descuenta lo reservado
  (para no volver a pedirle esos kWh antes de su `offer-updated`), y si el swap no se liquida
  (token inválido, rechazo o vencimiento) se le devuelve.

## Motor comprador (D3)

`buyer_tick` corre en segundo plano en cada `POST /internal/outbox/claim` (cada 5 s), bajo un
advisory lock (`21730008`) para que compre una sola réplica. En cada pasada:

1. **Acepta** los swaps propuestos (ver D4).
2. **Reenvía o vence** los bids sin respuesta dentro del span: mismo `idpk`, `msgId` nuevo,
   hasta `BID_MAX_ATTEMPTS` envíos y nunca después de `lastBidAt` (con 5 s de margen).
3. **Puja** si el ciclo con mercado abierto tiene déficit:
   - déficit = `ceil(max(0, consumption − generationCapacity))` del status-statement;
   - lo que falta = déficit − KWH en la wallet (`available + held`) − kWh en bids vivos, así
     el mismo kWh nunca está en dos bids;
   - recorre el libro según la política de selección de la estrategia
     (`lowest_effective_price` o `lowest_nominal_price`) y solo compra ofertas con precio
     efectivo **menor** que `imbalancePrice`;
   - pide `min(lo que falta, remaining)`, a lo más un bid vivo por oferta y 3 bids nuevos
     por pasada, partiendo la compra entre vendedores si hace falta;
   - respeta `minBid`: si falta menos que `minBid`, compra `minBid` solo si sale más barato
     que pagar el desbalance de lo que falta (el exceso se recupera a `buybackPrice`);
   - respeta el crédito: CR `available + creditLine` menos lo comprometido en bids vivos.

Respuestas del vendedor (se reconocen por `data.target`, que debe ser uno de nuestros `msgId`, y
por el `cityId` del vendedor al que pujamos):

| Respuesta | Efecto |
|---|---|
| `bid-accepted` | Bid `proposed`, guarda el swap y fija la Idempotency-Key del accept |
| `bid-rejected QUANTITY_UNAVAILABLE` | Baja el `remaining` de la oferta a `available`; la siguiente pasada pide eso |
| `bid-rejected OFFER_WITHDRAWN` / `MARKET_CLOSED` | Oferta retirada o cerrada; se pide a otra |
| `bid-rejected SELF_TRADE` | La oferta queda excluida |
| `bid-rejected BID_TOO_SMALL` | No se vuelve a pujar a esa oferta en el ciclo |
| `nack` a nuestro bid | Bid `rejected` con la razón del nack |
| `market-closed` | Vencen los bids que esperaban respuesta |

## Verificación del token y aceptación (D4)

Antes de aceptar se verifica el `swapToken` (JWT ES256) contra el JWKS de la wallet
(`{raíz de WALLET_BASE_URL}/.well-known/jwks.json`, o `WALLET_JWKS_URL`), con caché de 10 min y
recarga si llega un `kid` desconocido. Debe cumplirse todo:

- firma válida y `alg` ES256;
- `sub` = el `swapId` recibido; `proposer` = el vendedor al que le pujamos;
- `give` = exactamente los kWh pedidos; `take` = `round2(cantidad × precio de la oferta)` en CR;
- `memo` = el `offerId`; `exp` en el futuro.

Si algo no coincide, el bid queda `token_invalid` y **no se acepta**. Si el JWKS no responde, no
se decide nada y se reintenta en la pasada siguiente. El accept usa la Idempotency-Key fijada al
llegar el `bid-accepted`: tras un 502/504/429/timeout se reintenta con la misma clave, y un
reinicio a mitad de camino tampoco puede aceptar dos veces. Los eventos de la wallet cierran el
bid: `swap-settled` → `settled`; `swap-rejected` (de nuestro accept) o `swap-expired` → `failed`,
y esos kWh se vuelven a pedir.

Estados de un bid: `sent` → `proposed` → `accepted` → `settled`, o `rejected`, `token_invalid`,
`failed` y `expired`.

## API

| Ruta | Qué entrega |
|---|---|
| `GET /market/book` | RF02: libro del ciclo con mercado abierto (o el último con ofertas), con transporte, precio efectivo, ruta deshabilitada y si conviene frente al desbalance; y la ventana del mercado |
| `GET /market/bids?cycleId=` | Bids de TAL con su estado, intentos y swap |
| `GET /experiments/span` | RF06: datos del experimento por ciclo y agregados por span ([experimento-span.md](experimento-span.md)) |

En el frontend: páginas "Mercado" (`MarketBook.jsx`) y "Experimento de span"
(`SpanExperiment.jsx`).

## Configuración

| Variable | Por defecto | Qué hace |
|---|---|---|
| `BUYER_ENABLED` | `false` | Enciende el comprador; además necesita `MARKET_ENABLED` y `WALLET_ENABLED` |
| `BID_MAX_ATTEMPTS` | `3` | Envíos de un mismo bid sin respuesta antes de darlo por vencido |
| `WALLET_JWKS_URL` | vacío | JWKS alternativo; vacío usa el de la raíz de `WALLET_BASE_URL` |

El span (`buy_wait_span_seconds`) y la política de selección (`offer_selection_policy`) no son
variables de entorno: son de la estrategia del ciclo (rol C) y se cambian sin redeploy.

## Probarlo en local

Con la wallet y el broker falsos ([`scripts/fake_wallet/README.md`](../scripts/fake_wallet/README.md))
y `MARKET_ENABLED=true` y `BUYER_ENABLED=true` en el `.env`:

```bash
# Cada uno arma su ciclo con déficit para TAL (scripts/ensayos/local.py, mercado_comprador)
python3 scripts/escenario_e2e.py --tal-compra   # HGW ofrece, TAL puja, verifica el token, acepta y liquida
python3 scripts/demo_anomalia_4.py --operacion accept   # la wallet acepta y responde 504
python3 scripts/demo_anomalia_6.py              # ruta deshabilitada a mitad del mercado
```

Los tests de integración (`master/tests/test_market_integration.py`) cubren el libro con
desorden y duplicados, la compra, los rechazos, los reintentos, el token manipulado (no se
acepta), el timeout tras aceptar (una sola aceptación) y el experimento.
