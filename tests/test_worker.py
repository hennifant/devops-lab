"""Worker tests.

The HTTP side goes through a MockTransport: what needs proving is that every outcome
becomes a row and that nothing escapes as an exception, not that the client can fetch a
URL. Metrics are read through ``REGISTRY.get_sample_value`` rather than private
attributes, so these tests do not break when prometheus_client rearranges its internals.
"""

import asyncio

import httpx2
import pytest
from prometheus_client import REGISTRY

from app import db, worker


def _client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _target(name="t", url="http://example.invalid/", tid=1):
    return {"id": tid, "name": name, "url": url, "interval_seconds": 60}


def _gauge(name: str) -> float | None:
    return REGISTRY.get_sample_value("devops_lab_target_up", {"target": name})


def _counter(name: str, result: str) -> float:
    value = REGISTRY.get_sample_value(
        "devops_lab_checks_total", {"target": name, "result": result}
    )
    return value or 0.0


def _check(handler, target):
    async def scenario():
        async with _client(handler) as client:
            return await worker.check_one(client, target)

    return asyncio.run(scenario())


def test_success_is_recorded():
    out = _check(lambda r: httpx2.Response(200), _target("ok"))
    assert out["result"] == "success"
    assert out["status_code"] == 200
    assert _gauge("ok") == 1


def test_non_2xx_is_a_failure_not_an_exception():
    out = _check(lambda r: httpx2.Response(503), _target("bad"))
    assert out["result"] == "failure"
    assert out["status_code"] == 503
    assert out["error"] == "HTTP 503"
    assert _gauge("bad") == 0


def test_timeout_is_its_own_outcome():
    def handler(request):
        raise httpx2.ConnectTimeout("too slow", request=request)

    out = _check(handler, _target("slow"))
    assert out["result"] == "timeout"
    assert out["status_code"] is None


def test_transport_error_does_not_escape():
    """A broken target must never be able to end the worker loop."""

    def handler(request):
        raise httpx2.ConnectError("no route to host", request=request)

    out = _check(handler, _target("gone"))
    assert out["result"] == "failure"
    assert "ConnectError" in out["error"]


def test_every_outcome_increments_its_own_counter():
    before = _counter("counted", "success")
    _check(lambda r: httpx2.Response(200), _target("counted"))
    assert _counter("counted", "success") == before + 1


@pytest.mark.db
def test_tick_writes_a_row_per_due_target(with_pool):
    async def scenario():
        async with db.get_pool().connection() as conn:
            await conn.execute(
                "INSERT INTO targets (name, url, interval_seconds)"
                " VALUES ('due', 'http://x/', 60)"
            )
        async with _client(lambda r: httpx2.Response(200)) as client:
            checked = await worker.tick(client, concurrency=2)
        async with db.get_pool().connection() as conn:
            cursor = await conn.execute(
                "SELECT r.result FROM check_results r"
                " JOIN targets t ON t.id = r.target_id WHERE t.name = 'due'"
            )
            rows = await cursor.fetchall()
        return checked, [r["result"] for r in rows]

    checked, results = with_pool(scenario)
    assert checked == 1
    assert results == ["success"]


@pytest.mark.db
def test_a_target_checked_recently_is_not_due_again(with_pool):
    async def scenario():
        async with db.get_pool().connection() as conn:
            cursor = await conn.execute(
                "INSERT INTO targets (name, url, interval_seconds)"
                " VALUES ('recent', 'http://x/', 3600) RETURNING id"
            )
            tid = (await cursor.fetchone())["id"]
            await conn.execute(
                "INSERT INTO check_results (target_id, result) VALUES (%s, 'success')",
                (tid,),
            )
        async with _client(lambda r: httpx2.Response(200)) as client:
            return await worker.tick(client, concurrency=2)

    assert with_pool(scenario) == 0


@pytest.mark.db
def test_disabled_targets_are_never_due(with_pool):
    async def scenario():
        async with db.get_pool().connection() as conn:
            await conn.execute(
                "INSERT INTO targets (name, url, enabled) VALUES ('off', 'http://x/', false)"
            )
        async with _client(lambda r: httpx2.Response(200)) as client:
            return await worker.tick(client, concurrency=2)

    assert with_pool(scenario) == 0


@pytest.mark.db
def test_disabling_a_target_removes_its_series(with_pool):
    """A frozen gauge would make TargetDown fire against something nobody checks."""

    async def scenario():
        async with db.get_pool().connection() as conn:
            await conn.execute("INSERT INTO targets (name, url) VALUES ('vanishing', 'http://x/')")
        async with _client(lambda r: httpx2.Response(500)) as client:
            await worker.tick(client, concurrency=1)
        await worker.sync_series()
        during = _gauge("vanishing")

        async with db.get_pool().connection() as conn:
            await conn.execute("UPDATE targets SET enabled = false WHERE name = 'vanishing'")
        await worker.sync_series()
        return during, _gauge("vanishing")

    during, after = with_pool(scenario)
    assert during == 0
    assert after is None


def test_timeout_at_or_above_the_interval_floor_is_rejected(settings_env):
    from app.config import ConfigError, Settings

    settings_env(MIN_INTERVAL_SECONDS="30", CHECK_TIMEOUT_SECONDS="30")
    with pytest.raises(ConfigError, match="CHECK_TIMEOUT_SECONDS"):
        Settings.load()
