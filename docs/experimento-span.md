# Informe del experimento de span

> RDOC05 y RF06 (rol D, tareas D5 a D7). **Estado: corrida controlada en el stack local hecha
> (2026-11-07, 9 ciclos, abajo); falta repetirla con ciclos reales de producción**, que necesita
> el comprador encendido en la EC2. Las conclusiones de abajo valen para el mercado simulado y
> se marcan como tales.

## Pregunta

El **span** es cuánto espera el comprador la respuesta de un vendedor a un `bid` antes de
reenviarlo (o, tras `BID_MAX_ATTEMPTS` envíos, darlo por vencido y pedir esos kWh a otra
oferta). Es parte de la estrategia del ciclo (`buy_wait_span_seconds`, rol C) y se cambia desde
la pantalla de estrategia sin redeploy.

¿Cómo cambian el déficit cubierto en el mercado y el costo efectivo de cubrirlo según el span?

- Un span **corto** reenvía o abandona pronto una oferta que no responde: más bids y más
  rechazos, pero menos tiempo perdido con un vendedor lento.
- Un span **largo** le da tiempo al vendedor (que atiende bids en serie, AD4), pero si no
  responde, los kWh quedan comprometidos y puede llegar `lastBidAt` sin cubrir el déficit.

## Qué se mide por ciclo

Lo calcula `master/app/market/experiment.py` para cada ciclo en que TAL pujó:

| Dato | Definición |
|---|---|
| Span | El de los bids del ciclo (la estrategia se fija por ciclo, AD6) |
| Déficit | `ceil(max(0, consumption − generationCapacity))` del status-statement |
| Cubierto en el mercado | kWh de bids con swap liquidado (`swap-settled`), con tope en el déficit |
| Costo de mercado | Σ cantidad × (precio del swap + transporte por kWh) de los bids liquidados |
| Costo de desbalance | (déficit − cubierto) × `imbalancePrice` |
| Costo efectivo | Costo de mercado + costo de desbalance |
| Baseline | Déficit × `imbalancePrice`: lo que costaría no comprar nada |
| Bids rechazados | Bids con `bid-rejected` o `nack` del vendedor |

Por valor de span se suman ciclos, déficit, cubierto, costo efectivo, baseline y rechazos, y se
informa costo efectivo / baseline (bajo 1 es ahorro).

Límites de la medición:

- El transporte es el de la distance-table al momento del bid; el cobro real (`trade-recorded`)
  puede diferir por redondeo.
- Los demás grupos también están experimentando y el mercado cambia ciclo a ciclo: la oferta
  disponible, los precios y la competencia por las mismas ofertas no se controlan. Con pocos
  ciclos por valor de span, una diferencia puede ser ruido.

## Procedimiento (D7)

Hay dos corridas: una controlada en el stack local, ya hecha, y una con ciclos reales, pendiente.

### Corrida controlada (local)

En producción el mercado no se controla: la oferta, los precios y la latencia de los vendedores
cambian ciclo a ciclo, y con tres ciclos por span una diferencia puede ser ruido. Por eso
primero se corrió un diseño pareado en el stack local (`docker compose --profile fake`), con
`scripts/experimento_span.py`:

- TAL con déficit de 12 000 kWh, sin kWh en la cuenta, `imbalancePrice` 735 y crédito de sobra
  (el presupuesto no limita).
- HGW (la contraparte simulada de los ensayos) publica dos ofertas:
  - **A, barata y lenta**: 6 000 kWh a 300 CR/kWh. Responde cada bid después de una latencia
    fija del escenario: 20 s, 60 s o nunca.
  - **B, cara y rápida**: 12 000 kWh a 520 CR/kWh. Responde en 3 s.
- Cada span (10, 30 y 90 s) se corre contra los **mismos tres escenarios** de latencia de A: la
  única diferencia entre spans es el span. El span se fija con `PUT /strategy` antes de abrir
  el ciclo (AD6), con `BID_MAX_ATTEMPTS` = 3.
- Mercado comprimido de 300 s (`lastBidAt` a los 270 s; el comprador deja de pujar 5 s antes).
  Un ciclo real tiene unos 9 minutos hasta `lastBidAt`, así que con span 90 los tres envíos
  ocupan casi toda esta ventana y bastante menos de la real.
- Al final se lee `GET /experiments/span` y se filtran los ciclos de la corrida.

Para repetirla:

    docker compose --profile fake up -d --build
    python3 scripts/experimento_span.py --spans 10 30 90 --latencias 20 60 nunca \
        --mercado 300 --salida experimento-span.json

Si el computador se suspende a mitad de un ciclo los tiempos dejan de valer: correrlo con
`systemd-inhibit --what=sleep:idle ...`. Eso pasó en el ciclo span 90 / latencia 60
(`cycle-span90-1794073840`): se excluyó y se repitió (`cycle-span90-1794077194`).

### Corrida con ciclos reales (pendiente)

1. Encender el comprador en la EC2: `MARKET_ENABLED=true`, `WALLET_ENABLED=true` y
   `BUYER_ENABLED=true` en `/opt/energyshark/.env`, y reiniciar master.
2. Fijar el span desde la pantalla de estrategia (rol C) a cada valor durante **al menos 3 ciclos
   consecutivos** (cada ciclo dura 20 min, así que cada valor toma una hora): 10 s, 30 s y 90 s.
3. Solo cuentan ciclos en que TAL tuvo déficit; si genera de más, no compra y el ciclo no
   aparece en el experimento.
4. Exportar `GET /experiments/span` al final y agregar las tablas a este informe, junto con lo
   que muestra `GET /leaderboard` de la wallet en el mismo periodo, como contraste.

## Resultados (corrida local, 2026-11-07)

Déficit 12 000 kWh en todos los ciclos; baseline (no comprar nada) 8 820 000 CR. "Mensajes" son
los envíos de bid, contando reenvíos con el mismo idpk; "Bids" son idpk distintos, que es lo que
cuenta `bidsSent`. Ningún bid fue rechazado (`bidsRejected` 0): este diseño mide esperas, no
rechazos.

| Ciclo | Span | Latencia de A | Cubierto | Costo efectivo | Costo / baseline | Bids | Mensajes | A liquidada a los |
|---|---|---|---|---|---|---|---|---|
| `cycle-span10-1794071589` | 10 s | 20 s | 12 000 | 4 920 240 | 0,558 | 2 | 4 | 28 s |
| `cycle-span10-1794071909` | 10 s | 60 s | 6 000 | 7 530 120 | 0,854 | 9 | 25 | — |
| `cycle-span10-1794072237` | 10 s | nunca | 6 000 | 7 530 120 | 0,854 | 10 | 26 | — |
| `cycle-span30-1794072557` | 30 s | 20 s | 12 000 | 4 920 240 | 0,558 | 2 | 2 | 28 s |
| `cycle-span30-1794072878` | 30 s | 60 s | 12 000 | 4 920 240 | 0,558 | 2 | 4 | 69 s |
| `cycle-span30-1794073198` | 30 s | nunca | 6 000 | 7 530 120 | 0,854 | 4 | 10 | — |
| `cycle-span90-1794073519` | 90 s | 20 s | 12 000 | 4 920 240 | 0,558 | 2 | 2 | 32 s |
| `cycle-span90-1794077194` | 90 s | 60 s | 12 000 | 4 920 240 | 0,558 | 2 | 2 | 69 s |
| `cycle-span90-1794076812` | 90 s | nunca | 6 000 | 7 530 120 | 0,854 | 2 | 4 | — |

| Span | Ciclos | Cubierto (%) | Costo / baseline | Bids | Mensajes | Bids rechazados |
|---|---|---|---|---|---|---|
| 10 s | 3 | 66,7 | 0,755 | 21 | 55 | 0 |
| 30 s | 3 | 83,3 | 0,656 | 8 | 16 | 0 |
| 90 s | 3 | 83,3 | 0,656 | 6 | 8 | 0 |

Los 6 000 kWh de B se liquidaron en todos los ciclos en unos 10 s, con cualquier span.

## Qué se observó

- **Span más corto que la latencia del vendedor: se pierde la oferta barata.** Con span 10 y A
  respondiendo a los 60 s, cada bid a A vence tras tres envíos (unos 30 a 45 s) antes de que
  llegue la respuesta. HGW igual propuso 8 swaps que TAL ya no aceptó (el bid estaba vencido y
  la respuesta se ignora, como corresponde): esos kWh quedaron retenidos del lado del vendedor
  hasta que el swap venció. Para el resto del mercado, un span corto es más tráfico y swaps
  colgados.
- **Span igual o mayor que la latencia: mismo resultado, menos mensajes.** Con latencia 20 s
  los tres spans cubrieron todo al mismo costo. Con latencia 60 s, span 30 lo logró al tercer
  envío y span 90 al primero. Más allá de la latencia del vendedor, alargar el span no compra
  más; solo ahorra reenvíos.
- **Vendedor que nunca responde: el span no ayuda, y aparece un defecto del comprador.** Con A
  muda, ningún span cubrió esos 6 000 kWh aunque B tenía 6 000 kWh más disponibles a 520, bajo
  el desbalance. Cuando un bid a A vence, en el siguiente tick A sigue siendo la oferta más
  barata y sin bid vivo, así que el comprador le vuelve a pujar con otro idpk, y así hasta
  `lastBidAt`: span 10 lo hizo 9 veces, span 30 3 veces y span 90 una. Ese desbalance costó
  4 410 000 CR por ciclo, que habrían sido 3 120 120 comprándole a B: 1 289 880 CR por ciclo.
- `/experiments/span` cuadra con la wallet: los swaps liquidados son los de
  `GET /_fake/state` de la wallet falsa, y el costo de mercado coincide con precio + transporte.

## Conclusiones

Del mercado simulado:

1. El span tiene que ser al menos la latencia típica de respuesta de los vendedores. Más corto
   pierde ofertas buenas (span 10: 66,7 % cubierto y costo 0,755 del baseline, contra 83,3 % y
   0,656 de span 30 y 90) y además satura de bids y swaps colgados a la contraparte.
2. Entre 30 y 90 s no hubo diferencia de cobertura ni de costo en estos escenarios; 90 usó la
   mitad de mensajes. El riesgo de 90 es una ventana real con varios vendedores lentos en
   serie: tres envíos de 90 s son 4,5 min de un mercado de unos 9.
3. Recomendación para la estrategia: **span 30 s** por defecto. Cubre vendedores de hasta unos
   90 s de latencia en total (tres envíos) y deja margen para reintentar con otra oferta.
4. Mejora del comprador que sale del experimento (no implementada): una oferta cuyo bid venció
   por `NO_RESPONSE` debería quedar descartada el resto del ciclo, como ya se hace con las que
   rechazan por cantidad (`_offers_rejected_as_too_small`). Sin eso, un vendedor que no responde
   bloquea esos kWh con cualquier span.

Lo que **no** se puede concluir: cómo se comportan los vendedores reales (latencia,
rechazos, competencia por la misma oferta), ni si los 9 minutos reales cambian la comparación
entre 30 y 90 s. Para eso está la corrida con ciclos reales de arriba.
