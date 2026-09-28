# EnergyShark E1 — Backend

Nodo de ciudad para la E1 de IIC2173. Parte del código de la E0 (que la E1 exige mantener
operativo): `master` expone la API HTTP y persiste en Postgres; `connector` consume del broker
del curso y le envía los eventos a `master` por HTTP POST.

| Servicio | Carpeta | Qué hace |
|---|---|---|
| `master` | [`master/`](master/) | FastAPI: `/history` (paginado y filtrable), `/history/{id}`, `POST /events`, `/health` |
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
docker compose ps           # los tres deben terminar (healthy)
curl -i http://127.0.0.1:8000/health
curl -i 'http://127.0.0.1:8000/history?page=1&limit=25'
```

## Tests y lint

Usar Python 3.12 (asyncpg no compila en 3.14).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r master/requirements.txt -r connector/requirements.txt pytest pytest-asyncio httpx ruff
pytest                      # los tests de integración se saltan si no hay Postgres
TEST_DATABASE_URL=postgresql+asyncpg://energyshark:changeme@localhost:5432/energyshark_test pytest
ruff check .
```

## Documentación

Todo en [`docs/`](docs/): ADRs, spec de la entrega, milestones y declaración de uso de IA.

## Reglas del repo

- Nunca subir `.env` ni `.pem` (ya están en `.gitignore`).
- Toda variable de entorno nueva se documenta en `.env.example`.
- Todo cambio entra por PR revisado por alguien de otra área.
- El ADR de una decisión se commitea **antes** que su implementación.
