import time

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, Settings
from app.main import app


client = TestClient(app)


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Hello from the DevOps Lab"}


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_metrics():
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text


def test_health_answers_without_a_database(settings_env):
    """Liveness must not depend on anything. This is the whole reason it is separate."""
    settings_env(DATABASE_URL="postgresql://nobody:nobody@127.0.0.1:59999/nothing")
    with TestClient(app) as unreachable_client:
        assert unreachable_client.get("/health").status_code == 200


def test_ready_reports_503_and_names_the_failed_check(settings_env):
    settings_env(DATABASE_URL="postgresql://nobody:nobody@127.0.0.1:59999/nothing")
    started = time.perf_counter()
    with TestClient(app) as unreachable_client:
        response = unreachable_client.get("/ready")
    elapsed = time.perf_counter() - started

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not ready"
    assert body["checks"]["database"].startswith("unreachable")

    # Regression guard. psycopg_pool waits thirty seconds for a connection by default,
    # which made startup stall and /ready take half a minute to report a database that
    # was already unreachable. The Compose healthcheck allows five.
    assert elapsed < 15, f"startup and /ready took {elapsed:.1f}s with the database down"


def test_missing_database_url_fails_loudly(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(ConfigError) as excinfo:
        Settings.load()
    assert "DATABASE_URL" in str(excinfo.value)


def test_pool_bounds_are_validated(monkeypatch):
    monkeypatch.setenv("DB_POOL_MIN", "5")
    monkeypatch.setenv("DB_POOL_MAX", "2")
    with pytest.raises(ConfigError):
        Settings.load()
