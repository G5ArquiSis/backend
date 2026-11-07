# Ensayos de las anomalías de la demo (E7)

Un script por anomalía de la lista pública (enunciado E2 § Demo). Cada uno provoca la anomalía
sobre el stack local y comprueba lo que el enunciado dice que debe verse: imprime ✓/✗ por
comprobación y sale con 1 si alguna no se cumplió. Son herramientas para ensayar y para que
cada integrante sepa qué explicar; **la demo es sobre producción** y la provoca el ayudante.

## Preparación

Stack local con el broker y la wallet falsos (`scripts/fake_wallet/README.md`):

```bash
cat scripts/fake_wallet/env.fake >> .env      # una vez
# Para los ensayos del mercado: MARKET_ENABLED=true y BUYER_ENABLED=true en el .env
docker compose --profile fake up --build -d
source .venv/bin/activate                     # los scripts usan pika (connector/requirements.txt)
python3 scripts/demo_anomalia_5.py
```

Los scripts se niegan a correr si master, la wallet o el broker no son locales: inyectan
mensajes y fallas, y en producción alterarían el ledger que se reporta a la central.

El broker local tiene una segunda ciudad, **HGW**, que hace de contraparte
(`ensayos/contraparte.py`): publica ofertas en `market`, puja, propone y acepta swaps en la
wallet falsa con `fake-key-hgw`, y lee lo que TAL le manda en `city.HGW.q` y el feed en
`market.HGW`. La identidad `central` publica distance-tables y reenvía eventos de la wallet. Las
credenciales son las de `scripts/fake_wallet/rabbitmq/definitions.json` y sólo existen ahí.

## Scripts

| # | Anomalía | Script | Qué hace |
|---|---|---|---|
| 1 | Reenvío duplicado (mismo idpk) | `demo_anomalia_1.py [--tipo swap-settled\|bid]` | Liquida un swap real HGW→TAL y la central reenvía el `swap-settled` con el mismo idpk; o HGW reenvía un bid a la oferta de TAL. Comprueba ACK, registro `duplicate` en `/message-log`, wallet sin cambios y una sola propuesta de swap |
| 2 | `user_id` ajeno o malformado | `demo_anomalia_2.py` | Bid con `cityId` de otra ciudad, sin `user_id`, con `quantity` no entera, sin `idpk`, y bytes que no son JSON. Espera el `nack` correcto en `city.HGW.q`, el registro en `/message-log` y el servicio arriba |
| 3 | Contraparte que no cumple | `demo_anomalia_3.py --caso comprador-no-acepta\|vendedor-no-propone` | HGW recibe el `bid-accepted` de TAL y nunca acepta (el swap vence y no queda nada retenido; TAL vuelve a ofrecer), o recibe el bid de TAL y nunca propone (TAL sin CR retenidos) |
| 4 | Corte tras aceptar | `demo_anomalia_4.py --operacion accept\|propose` | La wallet ejecuta el `accept` (o el `propose`) de TAL y responde 504. Comprueba un solo swap liquidado o propuesto y que master converge |
| 5 | Corte del broker | `demo_anomalia_5.py --modo conexiones\|broker` | Cierra las conexiones de connector desde la API de RabbitMQ, o detiene el broker unos segundos. Comprueba reconexión sola, connector `healthy` y el ACK de TAL a una distance-table |
| 6 | Ruta deshabilitada | `demo_anomalia_6.py` | Distance-table con la ruta TAL→HGW habilitada, y a mitad del mercado una nueva con `enabled: false`. Comprueba `/connectivity` y muestra si TAL sigue pujando, con el precio efectivo antes y después |
| 7 | Lectura del monitoreo | `demo_anomalia_7.py [--segundos 4 --minutos 3]` | Wallet lenta (falla `latency`) mientras master reconcilia contra ella; imprime qué mirar en New Relic y las consultas NRQL |

## Pruebas de punta a punta y guardián (E6)

| Script | Qué hace |
|---|---|
| `escenario_e2e.py [--levantar] [--tal-compra]` | Comando único: levanta el stack y recorre un ciclo comprimido: status-statement y distance-table → oferta → bid → swap → token verificado → aceptación → `swap-settled` → `offer-updated` → `negotiation-report` |
| `ensayo_caida_contenedor.py` | Mata master y connector justo antes del plazo del reporte y los levanta: el reporte pendiente sale una vez, con el mismo idpk |
| `ensayo_caida_contenedor.py --perder-reporte` | Los deja abajo hasta que cierra la ventana: el ciclo queda sin reporte. Con la license key local, prueba la alerta "Ciclo sin reporte" del guardián (`docs/monitoreo.md` § Guardián de ciclos) |

Todos aceptan `--help`. Las esperas largas (`--espera`, `--espera-oferta`) existen porque TAL
ofrece o puja según su estrategia y el momento del ciclo.

## Estado al 2026-11-07 (main con A, B, C y D, más el PR #44)

Con `MARKET_ENABLED=true`, `WALLET_ENABLED=true` y `BUYER_ENABLED=true` en el `.env` local. Cada
ensayo de mercado arma su propio ciclo: `local.mercado_comprador` (TAL con déficit, sin kWh en la
cuenta, como tras el `imbalance-settlement` del ciclo anterior) o `local.mercado_vendedor` (TAL con
excedente y esos kWh en la cuenta), con ruta habilitada a HGW. Si la wallet falsa quedó sin crédito
para TAL después de muchos ensayos, `curl -X POST http://127.0.0.1:8090/_fake/reset`.

| # | Resultado | Qué falta |
|---|---|---|
| 1 `--tipo swap-settled` | ✓ swap por el mercado, ACK al duplicado, `duplicate` en `/message-log`, wallet sin cambios y el ciclo muestra el evento una vez | — |
| 1 `--tipo bid` | ✓ una sola propuesta de swap, `duplicate` en `/message-log`; TAL no contesta el reenvío (no se re-aplica) | — |
| 2 | ✓ los cuatro rechazos reciben `nack` en `city.HGW.q` (cityId ajeno, sin `user_id`, `quantity` no entera, sin `idpk`), JSON inválido descartado, y todo en `/message-log` | — |
| 3 `comprador-no-acepta` | ✓ el swap de TAL vence, no queda nada retenido y TAL vuelve a ofrecer | — |
| 3 `vendedor-no-propone` | ✓ TAL no queda con CR retenidos | — |
| 4 `--operacion accept` | ✓ 504 en el accept, reintento con la misma Idempotency-Key, un solo swap liquidado y master converge | — |
| 4 `--operacion propose` | ✓ 504 en el propose, un solo swap propuesto y master converge | — |
| 5 | ✓ en los dos modos (2026-11-06; no se volvió a correr) | — |
| 6 | ✓ `/connectivity` y `/market/book` reflejan la ruta deshabilitada con la tarifa punitiva; TAL sigue comprando porque el precio efectivo es menor que `imbalancePrice` | — |
| 7 | ✓ las reconciliaciones tardan lo inyectado y master sigue respondiendo (2026-11-06; no se volvió a correr) | Correrlo con la license key para verlo en New Relic |
| E2E (TAL vende) | ✓ bid, `bid-accepted`, token válido, `swap-settled` a ambos, `offer-updated` y reporte con el swap | — |
| E2E `--tal-compra` | ✓ bid, `bid-accepted`, token verificado, accept, `swap-settled` a ambos y reporte con el swap | — |
| Caída | ✓ en los dos modos (2026-11-06; no se volvió a correr) | — |

Lo que mostraron estos ensayos y se corrigió: los activos del swap del vendedor (`kwh`/`credits`
en vez de `KWH`/`CR`, TAL no podía vender; PR #42), y el destino de los NACK de connector y el bid
sin `user_id` (PR #44).
