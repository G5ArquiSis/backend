# Spec de la entrega E2

> RDOC02. Escrita el 2026-11-06 a partir del enunciado E2 y del reparto de roles del grupo. El
> avance de cada punto está en [milestones.md](milestones.md). La spec de la E1 está al final.

## Objetivo

Llevar el nodo de Talca al **mercado entre ciudades** del protocolo v3. Desde el 23 de octubre
la central dejó de negociar con las ciudades: opera la wallet (créditos `CR` y energía `KWH`,
fuente de verdad de los saldos) y liquida al cierre de cada ciclo de 20 minutos. Las ciudades
comercian entre sí: publican ofertas en el exchange `market`, otras ciudades mandan bids a la
cola de la vendedora, y el trato se cierra con un swap atómico en la wallet, firmado con un token
ES256 que el comprador verifica antes de aceptar.

El nodo tiene que hacerlo solo, ciclo tras ciclo: consumir sus dos colas (`city.TAL` y
`market.TAL`), aplicar los eventos de la wallet una sola vez y en cualquier orden, conciliar su
ledger con el saldo oficial, vender y comprar según una estrategia configurable sin redeploy, y
seguir enviando el `negotiation-report` en todos los ciclos. Las entregas son acumulativas: lo de
la E0 y la E1 sigue operativo.

## Ciudad asignada

Talca, código `TAL`. Consume `city.TAL` (mensajes de la central y de otras ciudades) y
`market.TAL` (feed del mercado), y publica con la propiedad AMQP `user_id = city.TAL`.

## Alcance

| Área | Requisitos | Rol |
|---|---|---|
| Mensajería y protocolo | G02; consumo de `market.TAL`; mensajes de otras ciudades con verificación de `user_id` (`IDENTITY_MISMATCH`); publicación al exchange `market` y a `city.{CODE}` con límite de tasa; RF05; schemas v3 | A |
| Wallet, ledger y ciclo | G03; cliente de la wallet idempotente; eventos `swap-*`, `trade-recorded` e `imbalance-settlement`; reconciliación con `GET /balance` (AD5); RF01 | B |
| Vendedor y estrategia | G04 como vendedor; oferta por ciclo; bids atendidos en serie (AD4); estrategia como configuración (AD6); RF04 | C |
| Comprador y experimento | G04 como comprador; libro de ofertas con precio efectivo; verificación del token; RF02; experimento de span (RF06, RDOC05) | D |
| Infraestructura y calidad | G01, G05 a G09; RNF01 a RNF05; migraciones, flags y rutas protegidas; wallet falsa; métricas y alertas; contract tests en CI; ensayos de anomalías; RDOC02 y RDOC03 | E |

Se retira el flujo de la E1 con la central (`negotiation-proposal`, `give`/`take`,
`demand-statement`): la central responde `TYPE_RETIRED`. `POST /negotiations` responde 410.

Sin cualquiera de los gates G01 a G09 la nota máxima es 4,0; su estado y evidencia están en
[gates-e2.md](gates-e2.md). Si falta tiempo, el orden de prioridad acordado es: gates (sobre todo
G04, un swap liquidado), RF03, ADRs y después las pantallas secundarias.

## Arquitectura de partida

La de la E1 ([arquitectura.md](arquitectura.md)): `master` (FastAPI, dos réplicas, Postgres) y
`connector` (pika) en una EC2 detrás de Nginx y API Gateway; SPA en S3 + CloudFront con Auth0;
New Relic. Cambia en la E2:

- `connector` atiende una tercera cola (`market.TAL`) y publica a destinos distintos de la
  central. La outbox guarda el destino de cada mensaje (contrato interno I1).
- `master` llama a la wallet por HTTP (`master/app/wallet/`) y concilia su ledger contra ella.
- Las columnas nuevas entran por migraciones SQL numeradas, porque `create_all` no altera tablas
  existentes.
- El mercado y la wallet se encienden por flags de entorno (`MARKET_ENABLED`, `WALLET_ENABLED`),
  porque cada merge a `main` despliega solo a producción.

Las decisiones nuevas (AD4 atención de bids, AD5 consistencia wallet-ledger, AD6 estrategia) se
escriben como ADR en [`adr/`](adr/) antes que su código, y AD1 a AD3 reciben su post-mortem.

## Roles

| Rol | Área | Integrante |
|---|---|---|
| A | Mensajería, protocolo y contratos de mensajes | Vicente Pavez |
| B | Wallet, ledger y ciclo autónomo | Catalina Berrios |
| C | Lado vendedor, estrategia y administración | Por confirmar |
| D | Lado comprador, libro de mercado y experimento de span | Por confirmar |
| E | Infraestructura, monitoreo, calidad y documentación | Melchor Guerrero |

Cada rol es dueño de un conjunto de archivos (`connector/*` es de A, `master/app/wallet/*` de B,
`master/app/market/*` de C y D, `deploy/*`, `.github/workflows/*`, `scripts/*` y estos documentos
de E). Si una tarea obliga a tocar un archivo de otro rol, el dueño hace el cambio o lo revisa.

## Forma de trabajo

- Todo cambio entra por PR a `main`, protegido, con 2 aprobaciones. Parejas de revisión: A↔B,
  C↔D; E revisa a todos en infraestructura.
- Cada merge a `main` se despliega solo a producción. El código nuevo entra apagado detrás de un
  flag y se enciende en el `.env` de la EC2 cuando está probado.
- Los contratos internos de la Hora 0 (I1 outbox con destino, I2 cliente de wallet y registro de
  handlers, I3 estrategia, I4 libro de ofertas, I5 infraestructura) se mergean primero para que
  cada rol programe contra un stub. Para probar sin la red real están la wallet falsa y el broker
  local (`scripts/fake_wallet/`).
- El ADR de una decisión se commitea antes que su implementación.
- Un cambio de contrato entra por PR en `contratos` antes que su código. El CI compara el OpenAPI
  generado desde la app con el de `contratos` y valida los mensajes contra los schemas del curso.
- Una ruta HTTP nueva se avisa a E antes de mergearla, para protegerla en Nginx.
- Toda variable de entorno nueva se documenta en `.env.example`. Nunca se suben `.env`, `.pem`
  ni `WALLET_API_KEY`.
- Reintentos con tope y backoff: un reintento conserva el `idpk` y una corrección usa uno nuevo.
  No se hace ACK de un ACK, NACK o error.
- El uso de IA se registra por integrante en [`ai-logs/`](ai-logs/), en el mismo PR que el
  trabajo.

## Spec de la E1 (histórico)

Escrita el 2026-10-07. El nodo consumía `city.TAL`, llevaba un ledger append-only con índice
único por `idpk`, negociaba energía con la central y otras ciudades por mensajes (timeouts de
30 s) y reportaba su posición al cierre de cada ciclo. Las decisiones abiertas se resolvieron en
[ADR-0001](adr/0001-topologia-consumo.md) (topología del consumo),
[ADR-0002](adr/0002-persistencia-ledger.md) (persistencia del ledger) y
[ADR-0003](adr/0003-timeouts-negociacion.md) (timeouts).

| Rol E1 | Área | Integrante |
|---|---|---|
| A | Mensajería y protocolo | Catalina Berrios |
| B | Ledger y persistencia | Vicente Pavez |
| C | Ciclo de negociación | Serena Barraza |
| D | Frontend y autenticación | Carlos Riffo |
| E | Deploy e infraestructura | Melchor Guerrero |

No se plantearon preguntas al ayudante. Ante la duda de si HTTPS y el balanceo de carga de la E0
seguían vigentes, el grupo decidió mantener ambos.
