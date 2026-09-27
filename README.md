# EnergyShark E1 — Backend

API y nodo de ciudad para la E1 de IIC2173. Consume la cola `city.{CODE}` del broker del curso,
mantiene el ledger de la ciudad, negocia con la central y expone una API JSON para el frontend.

Repos relacionados (organización `G5ArquiSis`):

- [`frontend`](https://github.com/G5ArquiSis/frontend): SPA en React.
- [`contratos`](https://github.com/G5ArquiSis/contratos): schemas de mensajes, OpenAPI y contexto compartido.

## Correr en local

Requiere Docker Compose v2 (`docker compose`, no `docker-compose`).

```bash
cp .env.example .env        # completar valores
docker compose up --build
curl -i http://127.0.0.1:8000/health
```

## Tests y lint

Usar Python 3.12 (igual que la imagen).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest httpx ruff
pytest
ruff check .
```

## Documentación

Todo en [`docs/`](docs/): ADRs, spec de la entrega, milestones y declaración de uso de IA.

## Reglas del repo

- Nunca subir `.env` ni `.pem` (ya están en `.gitignore`).
- Toda variable de entorno nueva se documenta en `.env.example`.
- Todo cambio entra por PR revisado por alguien de otra área.
- El ADR de una decisión se commitea **antes** que su implementación.
