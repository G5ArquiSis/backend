# Arquitectura

> RDOC03. Diagrama UML de componentes del nodo de Talca (`TAL`) en la E2 y cómo correrlo en
> local. El detalle del flujo de mensajes está en [ciclo-negociacion.md](ciclo-negociacion.md),
> la infraestructura en [infraestructura.md](infraestructura.md) y el monitoreo en
> [monitoreo.md](monitoreo.md). Actualizado el 2026-11-06.

## Diagrama de componentes

Las líneas continuas son lo que está en `main`. Las punteadas son componentes de la E2 que
todavía están en revisión o pendientes; el estado de cada uno está en
[milestones.md](milestones.md).

```mermaid
flowchart LR
    subgraph Usuario
        SPA["«component»<br/>SPA React<br/>(app.melchort.me)"]
    end

    subgraph AWS["AWS"]
        CF["«component»<br/>S3 + CloudFront"]
        APIGW["«component»<br/>API Gateway HTTP API<br/>(api.melchort.me)<br/>JWT + CORS"]
        subgraph EC2["EC2 energyshark-ec2"]
            NGINX["«component»<br/>Nginx<br/>TLS, least_conn,<br/>rutas protegidas"]
            subgraph Compose["Docker Compose (red energyshark)"]
                subgraph MASTER["«component» master (x2, FastAPI)"]
                    API["API pública<br/>/cycles, /connectivity,<br/>/message-log, /wallet/*"]
                    LEDGER["Ledger, ciclo y outbox<br/>(cycle_repository, outbox)"]
                    WALLET["Cliente de wallet,<br/>eventos y reconciliación<br/>(app/wallet)"]
                    STRAT["Estrategia, vendedor<br/>y comprador<br/>(app/market, C y D)"]
                end
                CONN["«component»<br/>connector<br/>pika, 3 consumidores"]
                PG[("«component»<br/>PostgreSQL 16")]
            end
            NRI["«component»<br/>New Relic<br/>Infrastructure agent"]
        end
    end

    AUTH0["«component»<br/>Auth0"]
    BROKER["«component»<br/>RabbitMQ del curso"]
    CWALLET["«component»<br/>Wallet de la central<br/>(REST v1, JWKS)"]
    NR["«component»<br/>New Relic"]

    SPA -- "descarga estáticos" --> CF
    SPA -- "login (OIDC)" --> AUTH0
    SPA -- "REST + Bearer JWT" --> APIGW
    APIGW -- "valida JWT (JWKS)" --> AUTH0
    APIGW -- "HTTPS + header secreto" --> NGINX
    NGINX -- "HTTP :8001 / :8002" --> API
    API --> LEDGER
    LEDGER --> WALLET
    STRAT -.-> LEDGER
    STRAT -.-> WALLET
    LEDGER -- "SQL" --> PG
    WALLET -- "SQL" --> PG
    WALLET -- "HTTPS, X-Api-Key,<br/>Idempotency-Key" --> CWALLET
    CONN -- "POST /internal/messages<br/>POST /internal/outbox/claim<br/>POST /events" --> MASTER
    BROKER -- "city.TAL.q<br/>(central y eventos de wallet)" --> CONN
    BROKER -- "observer.*.q (E0)" --> CONN
    BROKER -. "market.TAL<br/>(feed, MARKET_ENABLED)" .-> CONN
    CONN -- "energy.x → central<br/>(user_id city.TAL)" --> BROKER
    CONN -. "exchange market y<br/>city.{CODE}" .-> BROKER
    CWALLET -- "eventos swap-*, trade-recorded,<br/>imbalance-settlement" --> BROKER
    MASTER -. "APM y eventos propios" .-> NR
    NRI -. "host y contenedores" .-> NR
```

### Componentes y sus interfaces

| Componente | Responsabilidad | Interfaz que provee | Interfaces que usa |
|---|---|---|---|
| SPA React | Vistas de RF01 (ciclos), RF02 (conectividad y mercado), RF04 (administración) y RF05 (mensajes) | — | API pública por API Gateway; Auth0 |
| API Gateway | Única puerta de la SPA. Valida el JWT de Auth0 y pone los headers CORS (RNF01, RNF02) | `https://api.melchort.me` | Nginx |
| Nginx | TLS y balanceo `least_conn` entre las dos réplicas. Bloquea `/internal/` y responde 403 a las rutas protegidas que no traen el header secreto del gateway (`deploy/nginx/*.conf`) | `https://melchort.me` | `master-1`, `master-2` |
| `master` (x2) | Decide: aplica mensajes al ledger, programa el reporte, las peticiones y la reconciliación, y sirve la API | API pública ([OpenAPI](https://github.com/G5ArquiSis/contratos/blob/main/openapi/openapi.yaml)) y `/internal/*` | PostgreSQL; wallet de la central |
| `master/app/wallet` | `WalletClient` (propose, accept, cancel, balance, statement) idempotente y con reintentos ante 429, 502, 503 y 504; aplica los eventos de la wallet una sola vez; concilia con `GET /balance` (AD5) | `GET /wallet/balance-status`, `POST /internal/wallet/reconcile` | Wallet de la central |
| `master/app/market` | Estrategia como configuración (AD6), oferta propia y bids en serie (AD4), libro de ofertas y compra | Pendiente (roles C y D) | Ledger, outbox y `WalletClient` |
| `connector` | Transporta: valida el envelope, responde ACK/NACK, reenvía a `master` y publica la outbox. Su heartbeat de 5 s es el reloj de `master` | — | Broker AMQP; `/internal/*` y `/events` de `master` |
| PostgreSQL | Todo el estado: ledger, ciclos, outbox con destino, saldos y eventos de la wallet, distance-tables, message log, historial E0 | SQL | — |
| Broker del curso | `city.TAL.q` (central, eventos de la wallet y otras ciudades), `market.TAL` (feed) y la cola del observer | AMQP | — |
| Wallet de la central | Fuente de verdad de `CR` y `KWH`; swaps atómicos con token ES256 | REST `/v1`, JWKS | — |
| New Relic | APM de `master`, eventos del mercado y la wallet, y agente de infraestructura (G08, RNF05) | — | — |

### Decisiones que se ven en el diagrama

- **`connector` y `master` son contenedores separados** ([ADR-0001](adr/0001-topologia-consumo.md)).
  Si el broker se cae, solo `connector` reintenta, y la API sigue respondiendo con lo que ya está
  en Postgres. En la E2 cada cola tiene su conexión y su heartbeat, y el contenedor está
  `healthy` sólo si laten todos los consumidores que deberían correr.
- **`master` no tiene reloj propio.** Cada 5 s, `connector` llama a `POST /internal/outbox/claim`.
  Esa llamada revisa los plazos vencidos (reporte del ciclo, peticiones de status-statement),
  dispara la reconciliación con la wallet en segundo plano y entrega lo que haya que publicar
  ([ADR-0003](adr/0003-timeouts-negociacion.md)).
- **La wallet es la fuente de verdad** ([ADR-0005](adr/0005-consistencia-wallet-ledger.md)).
  El ledger local registra los eventos de la wallet y el saldo oficial que devuelve
  `GET /balance`; si difieren, la diferencia queda a la vista en `/wallet/balance-status`.
  Las llamadas a la wallet salen sólo de `master`; `connector` nunca la llama.
- **Dos réplicas sobre la misma base.** El trabajo programado y el claim de la outbox usan
  `SELECT ... FOR UPDATE SKIP LOCKED`, y la reconciliación toma un advisory lock, así ninguna
  réplica repite lo que tomó la otra. El ledger es append-only, con índice único por `idpk`
  ([ADR-0002](adr/0002-persistencia-ledger.md)).
- **El broker es la fuente de verdad de lo pendiente.** Si `master` falla, `connector` hace
  `nack(requeue=True)` y no confirma, así que no hay cola en memoria que se pierda en un reinicio.
- **Lo nuevo entra apagado.** `MARKET_ENABLED` y `WALLET_ENABLED` se encienden en el `.env` de la
  EC2 cuando está probado, porque cada merge a `main` despliega solo a producción. Las columnas
  nuevas entran por migraciones SQL numeradas (`master/app/migrations/sql/`).

## Correr en local

Requiere Python 3.12 y Docker Compose v2. Hay dos formas: contra el broker del curso, o con el
broker y la wallet falsos del perfil `fake`, que no necesitan credenciales reales y son los que
usan los tests de punta a punta y los ensayos.

### Instalación

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r master/requirements.txt -r connector/requirements.txt \
  pytest pytest-asyncio httpx ruff jsonschema rfc3339-validator
cp .env.example .env                 # cada variable está comentada en .env.example
```

### Con el broker y la wallet falsos (recomendado)

```bash
cat scripts/fake_wallet/env.fake >> .env
docker compose --profile fake up --build -d
docker compose ps                    # postgres, master-1, master-2, connector, rabbitmq, fake-wallet
curl -s http://127.0.0.1:8001/health # master-1; master-2 en 8002
python3 scripts/escenario_e2e.py     # un ciclo comprimido de punta a punta
```

La wallet falsa responde en `http://127.0.0.1:8090/v1` y la consola de RabbitMQ en
`http://127.0.0.1:15672`. Qué imita y cómo hacer de contraparte está en
[`scripts/fake_wallet/README.md`](../scripts/fake_wallet/README.md); los ensayos de las
anomalías, en [`scripts/ensayos/README.md`](../scripts/ensayos/README.md).

### Contra el broker del curso

```bash
# en .env: BROKER_* y, para la cola de la ciudad, CITY_BROKER_PASSWORD
docker compose up --build
```

Sin `CITY_BROKER_PASSWORD`, `connector` solo consume la cola del observer. La sección "Probarlo
sin la central" de [ciclo-negociacion.md](ciclo-negociacion.md) muestra cómo simular mensajes
con `curl`. No se debe apuntar el stack local a la wallet real con la key de producción: el
ledger local divergería del de la EC2.

### Tests

```bash
ruff check .
pytest                                   # los de integración se saltan sin Postgres
TEST_DATABASE_URL=postgresql+asyncpg://energyshark:changeme@localhost:5432/energyshark_test pytest
```

Los contract tests necesitan los repos de contratos (`ENERGYSHARK_CONTRACTS_DIR`,
`GROUP_CONTRACTS_DIR`); el README del repo explica cómo.

En local no hay API Gateway ni Nginx: se llega a `master` directo y sin JWT. El frontend se corre
aparte (`npm run dev` en el repo `frontend`) y apunta a `https://api.melchort.me`, porque el
backend no pone headers CORS (eso lo hace solo API Gateway).

## Monitoreo

Los pasos para replicarlo están en [monitoreo.md](monitoreo.md): instalar el agente de
infraestructura en la EC2, poner `NEW_RELIC_LICENSE_KEY` en el `.env` (master arranca bajo el
agente de APM sólo si existe), importar el dashboard de `deploy/newrelic/` y crear las alertas,
entre ellas las del guardián de ciclos ("Ciclo sin reporte" y "Connector no late").

## Contrato

- API pública: `contratos/openapi/openapi.yaml`, generado con
  `python scripts/exportar_openapi.py ../contratos/openapi/openapi.yaml`. El CI falla si el
  generado no coincide con el de `contratos`.
- Mensajes AMQP: schemas del curso (`iic2173/energyshark-contracts`, v3.0.2) y los del grupo en
  `contratos/schemas/`. Los contract tests validan contra ambos lo que el nodo publica.
- `/internal/*`: [ciclo-negociacion.md](ciclo-negociacion.md).
