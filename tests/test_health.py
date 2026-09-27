"""Tests del endpoint de salud. No necesitan Postgres: la sesión se reemplaza."""

from fastapi.testclient import TestClient

from app.database import get_session
from app.main import app


class _HealthySession:
    async def execute(self, statement):
        return None


class _BrokenSession:
    async def execute(self, statement):
        raise ConnectionError("postgres no responde")


def _client_with(session) -> TestClient:
    async def override():
        yield session

    app.dependency_overrides[get_session] = override
    return TestClient(app, raise_server_exceptions=False)


def test_health_ok_when_database_responds():
    response = _client_with(_HealthySession()).get("/health")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_fails_when_database_is_down():
    response = _client_with(_BrokenSession()).get("/health")
    app.dependency_overrides.clear()

    assert response.status_code == 500
