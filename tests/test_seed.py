"""The seed has to be safe to run on every deploy, which means running it twice must
change nothing. Idempotency is the whole requirement, so it is what gets tested."""

import asyncio

import psycopg
import pytest

from app.config import ConfigError
from app.seed import parse_seed_targets, seed

pytestmark = pytest.mark.db


def count_targets(url: str) -> int:
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT count(*) FROM targets").fetchone()[0]


def test_parses_name_url_pairs():
    assert parse_seed_targets("a=https://a.example, b=http://b.example") == [
        ("a", "https://a.example"),
        ("b", "http://b.example"),
    ]


@pytest.mark.parametrize("raw", ["no-equals-sign", "=https://a.example", "a=", "a=ftp://a"])
def test_rejects_malformed_entries(raw):
    with pytest.raises(ConfigError):
        parse_seed_targets(raw)


def test_empty_seed_is_a_no_op(clean_database, settings_env):
    settings_env(SEED_TARGETS="")
    assert asyncio.run(seed()) == 0
    assert count_targets(clean_database) == 0


def test_seeding_twice_changes_nothing(clean_database, settings_env):
    settings_env(SEED_TARGETS="api-self=http://api:8000/health,other=https://example.com")

    assert asyncio.run(seed()) == 2
    assert count_targets(clean_database) == 2

    assert asyncio.run(seed()) == 2
    assert count_targets(clean_database) == 2


def test_seeding_updates_a_changed_url(clean_database, settings_env):
    settings_env(SEED_TARGETS="a=https://one.example")
    asyncio.run(seed())

    settings_env(SEED_TARGETS="a=https://two.example")
    asyncio.run(seed())

    with psycopg.connect(clean_database) as conn:
        url = conn.execute("SELECT url FROM targets WHERE name = 'a'").fetchone()[0]
    assert url == "https://two.example"
    assert count_targets(clean_database) == 1
