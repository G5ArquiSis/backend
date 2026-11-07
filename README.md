# EnergyShark E1 — Backend

Nodo de ciudad para la E1 de IIC2173. Parte del código de la E0 (que la E1 exige mantener
operativo): `master` expone la API HTTP y persiste en Postgres; `connector` consume del broker
del curso y le envía los eventos a `master` por HTTP POST.

| Servicio | Carpeta | Qué hace |
|---|---|---|
| `master-1`, `master-2` | [`master/`](master/) | Dos réplicas balanceadas por Nginx. FastAPI: `/history` (paginado y filtrable), `/history/{id}`, `POST /events`, `/health` |
| `connector` | [`connector/`](connector/) | Consumidor AMQP con reconexión automática; `ack` solo tras persistir en `master` |
| `postgres` | — | Base de datos |

Repos relacionados (organización `G5ArquiSis`):

- [`frontend`](https://github.com/G5ArquiSis/frontend): SPA en React.
- [`contratos`](https://github.com/G5ArquiSis/contratos): schemas de mensajes, OpenAPI y contexto compartido.

**Contexto compartido del proyecto:** [`contratos/AGENTS.md`](https://github.com/G5ArquiSis/contratos/blob/main/AGENTS.md).
Leerlo antes de tocar el código: resume el sistema, los repositorios y las reglas que no se rompen.

## Correr en local

Requiere Docker Compose v2 (`docker compose`, no `docker-compose`).

```bash
cp .env.example .env        # completar credenciales del broker
docker compose up --build
docker compose ps           # los cuatro deben terminar (healthy)
curl -i http://127.0.0.1:8001/health      # master-1 (master-2 en 8002)
curl -i 'http://127.0.0.1:8001/history?page=1&limit=25'
```

Para trabajar sin la central ni la wallet reales (broker y wallet locales, con fallas
inyectables): `docker compose --profile fake up --build`, después de agregar
`scripts/fake_wallet/env.fake` al final de `.env`. Detalles en
[`scripts/fake_wallet/README.md`](scripts/fake_wallet/README.md).

## Tests y lint

Usar Python 3.12 (asyncpg no compila en 3.14).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r master/requirements.txt -r connector/requirements.txt \
  -r scripts/fake_wallet/requirements.txt pytest pytest-asyncio httpx ruff \
  jsonschema rfc3339-validator
pytest                      # los tests de integración se saltan si no hay Postgres
TEST_DATABASE_URL=postgresql+asyncpg://energyshark:changeme@localhost:5432/energyshark_test pytest
ruff check .
```

### Contract tests (E4)

`tests/test_contratos.py` valida cada mensaje que publican connector y master (ACK, NACK,
`request`, `negotiation-report`...) contra los schemas del curso (v3.0.2) y los del grupo. Sin
los clones se saltan; el CI los clona y falla si un mensaje no cumple:

```bash
git clone --depth 1 --branch v3.0.2 https://github.com/iic2173/energyshark-contracts.git /tmp/contratos-curso
export ENERGYSHARK_CONTRACTS_DIR=/tmp/contratos-curso GROUP_CONTRACTS_DIR=../contratos
pytest tests/test_contratos.py
```

Un tipo nuevo que el nodo publique (bid, offer...) necesita su schema en `contratos` (mergeado
antes que el código) y pasar por `assert_publishable` en ese archivo. El CI además regenera el
OpenAPI con `scripts/exportar_openapi.py` y falla si difiere de `contratos/openapi/openapi.yaml`.

### Ensayos de anomalías (E7)

`scripts/demo_anomalia_1.py` a `_7.py` provocan sobre el stack local (perfil `fake`) cada
anomalía de la demo y comprueban lo que debe verse. Detalle y estado en
[`scripts/ensayos/README.md`](scripts/ensayos/README.md).

## Herramientas de prueba y simulación (E2)

### Simulador de mercado y ciudad fantasma MET (Tarea A6)

Permite probar el flujo de mercado sin depender de la red real ni de otras ciudades conectadas. Inyecta mensajes hacia `master` (`POST /internal/messages`) o directo a un broker:

```bash
# Inyectar ofertas de mercado de prueba (feed público)
python3 scripts/simulador_mercado.py --mode feed

# Inyectar bids hacia nuestra ciudad (TAL)
python3 scripts/simulador_mercado.py --mode bids

# Simular la conducta de la ciudad fantasma MET (vende a imbalancePrice * 0.9 bajando de precio)
python3 scripts/simulador_mercado.py --mode ghost --iterations 5 --interval 5

# Inyectar anomalías de prueba (identidad usurpada y mensajes malformados)
python3 scripts/simulador_mercado.py --mode anomalies

# Correr un escenario completo representativo
python3 scripts/simulador_mercado.py --mode all
```

Parámetros útiles:
- `--url`: URL de master local (por defecto `http://127.0.0.1:8000`).
- `--broker-url`: URL de conexión AMQP si se desea publicar directo al broker.
- `--cycle`: Identificador del ciclo simulado.
- `--interval`: Segundos entre ticks de la ciudad fantasma (útil para acelerar tests).

## Documentación

Todo en [`docs/`](docs/): ADRs, spec de la entrega, milestones y declaración de uso de IA.

## Reglas del repo

- Nunca subir `.env` ni `.pem` (ya están en `.gitignore`).
- Toda variable de entorno nueva se documenta en `.env.example`.
- Todo cambio entra por PR revisado por alguien de otra área.
- El ADR de una decisión se commitea **antes** que su implementación.
