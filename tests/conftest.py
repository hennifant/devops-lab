"""Test fixtures.

Two kinds of test live here. Most need no database and must stay fast; the ones marked
``db`` need a real Postgres and get the schema the same way production does — by running
``alembic upgrade head``, not by building tables by hand. A hand-built schema tests a
schema nobody deploys.

``DATABASE_URL`` is set before the application is imported. Without a database it points
somewhere unreachable on purpose: the pool opens without waiting, so ``/health`` still
answers and ``/ready`` correctly reports 503. That is the behaviour, not a workaround.
"""

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
UNREACHABLE_URL = "postgresql://nobody:nobody@127.0.0.1:59999/nothing"

os.environ["DATABASE_URL"] = TEST_DATABASE_URL or UNREACHABLE_URL

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "db: requires a live Postgres")


def _database_reachable() -> bool:
    if not TEST_DATABASE_URL:
        return False
    import psycopg

    try:
        with psycopg.connect(TEST_DATABASE_URL, connect_timeout=5):
            return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """A database with the schema applied, or a skip — unless CI says otherwise.

    ``REQUIRE_DB=1`` turns the skip into a failure. Without it, a CI job whose Postgres
    service failed to start would report green while silently running none of these tests.
    """
    if not _database_reachable():
        message = (
            "no database at TEST_DATABASE_URL"
            if TEST_DATABASE_URL
            else "TEST_DATABASE_URL is not set"
        )
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail(f"REQUIRE_DB=1 but {message}")
        pytest.skip(message)

    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture
def clean_database(migrated_database):
    import psycopg

    with psycopg.connect(migrated_database, autocommit=True) as conn:
        conn.execute("TRUNCATE targets RESTART IDENTITY CASCADE")
    return migrated_database


@pytest.fixture
def client(clean_database):
    """A client with the lifespan running, so the pool is open."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def settings_env(monkeypatch):
    """Override settings for one test and drop the cached Settings around it."""

    def override(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()

    get_settings.cache_clear()
    yield override
    get_settings.cache_clear()
