# Informe del experimento de span

> RDOC05 y RF06 (rol D, tareas D5 a D7). **Estado: método y herramientas listos; faltan los
> ciclos reales.** Las secciones de resultados y conclusiones se llenan con los datos de
> `GET /experiments/span` cuando el comprador lleve varios ciclos encendido en producción. No
> se adelantan conclusiones sin esos datos.

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

1. Encender el comprador en la EC2: `MARKET_ENABLED=true`, `WALLET_ENABLED=true` y
   `BUYER_ENABLED=true` en `/opt/energyshark/.env`, y reiniciar master.
2. Fijar el span desde la pantalla de estrategia (rol C) a cada valor durante **al menos 3 ciclos
   consecutivos** (cada ciclo dura 20 min, así que cada valor toma una hora). Valores
   propuestos: 10 s, 30 s y 90 s.
3. Solo cuentan ciclos en que TAL tuvo déficit; si genera de más, no compra y el ciclo no
   aparece en el experimento.
4. Exportar `GET /experiments/span` al final y copiar las tablas a este informe, junto con lo
   que muestra `GET /leaderboard` de la wallet en el mismo periodo, como contraste.

## Resultados

Pendiente: se llena con ciclos reales.

| Ciclo | Span | Déficit | Cubierto | Costo efectivo | Baseline | Bids rechazados |
|---|---|---|---|---|---|---|
| | | | | | | |

| Span | Ciclos | Cubierto (%) | Costo / baseline | Bids rechazados |
|---|---|---|---|---|
| | | | | |

## Qué se observó y qué midió la wallet

Pendiente.

## Conclusiones

Pendiente. Decir también qué no se puede concluir con los ciclos que hubo.
