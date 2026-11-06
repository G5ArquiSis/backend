# Ensayos de las anomalías de la demo (E7)

Un script por anomalía de la lista pública (enunciado E2 § Demo). Cada uno provoca la anomalía
sobre el stack local y comprueba lo que el enunciado dice que debe verse: imprime ✓/✗ por
comprobación y sale con 1 si alguna no se cumplió. Son herramientas para ensayar y para que
cada integrante sepa qué explicar; **la demo es sobre producción** y la provoca el ayudante.

## Preparación

Stack local con el broker y la wallet falsos (`scripts/fake_wallet/README.md`):

```bash
cat scripts/fake_wallet/env.fake >> .env      # una vez
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

Todos aceptan `--help`. Las esperas largas (`--espera`, `--espera-oferta`) existen porque TAL
ofrece o puja según su estrategia y el momento del ciclo.

## Estado al 2026-11-06 (main + PR #29)

| # | Resultado | Qué falta |
|---|---|---|
| 1 | ✗ connector responde `nack UNKNOWN_TYPE` al `swap-settled` | Tipos v3 en connector (PR #28) |
| 2 | ✗ nack `UNKNOWN_TYPE` a la central en vez de `IDENTITY_MISMATCH`/`MALFORMED_MESSAGE` a HGW | Tipos v3, identidad y destino del nack (A2, PR #28) |
| 3, 4, 1 `--tipo bid` | ✗ TAL no publica ofertas ni puja | Estrategia de venta y compra (C y D) |
| 5 | ✓ en los dos modos | — |
| 6 | ✓ `/connectivity` refleja el cambio; no hay pujas de TAL que observar | Estrategia de compra (D) para ver la selección |
| 7 | ✓ las reconciliaciones tardan lo inyectado y master sigue respondiendo | Correrlo con la license key para verlo en New Relic |

La contraparte se probó sola (HGW ofrece, recibe un bid, propone, manda `bid-accepted` y el
swap se liquida en la wallet falsa), y todos los mensajes que arman los scripts cumplen los
schemas del curso v3.0.2. Al mergear cada PR del mercado, volver a correr la tabla y
actualizarla.
