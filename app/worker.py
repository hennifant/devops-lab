"""The check worker: a second process, a second failure surface.

Runs from the same image as the API with a different command. Separate on purpose — the
API can serve traffic while this is dead, and that has to be *visible*. Inside the API
process it would be neither separately restartable nor separately observable, which is
the whole reason it exists.

Metrics are served on their own port and never published to the host; Prometheus reaches
them over the Compose network as ``worker:9101``.
"""

import asyncio
import logging
import signal
import time
from typing import Any

import httpx2
from prometheus_client import start_http_server

from app import db
from app.config import get_settings
from app.logging import configure_logging
from prometheus_client import Counter, Gauge, Histogram

# Defined here, not in app/metrics.py. Importing a module registers its metrics, so a
# shared definition had the API publishing devops_lab_worker_last_run_timestamp_seconds as
# 0 forever — and WorkerStalled fired against the API's copy while the worker was healthy.
# It did, on 2026-08-21, and the false alarm reached Gotify. A process should expose only
# metrics it can actually produce.
CHECKS_TOTAL = Counter(
    "devops_lab_checks_total",
    "Checks performed, by target and outcome.",
    ["target", "result"],
)

CHECK_DURATION = Histogram(
    "devops_lab_check_duration_seconds",
    "Time an individual check took, including connect and read.",
    ["target"],
)

TARGET_UP = Gauge(
    "devops_lab_target_up",
    "Whether the last check of a target succeeded.",
    ["target"],
)

WORKER_LAST_RUN = Gauge(
    "devops_lab_worker_last_run_timestamp_seconds",
    "Unix time at which the worker last completed a tick.",
)

WORKER_RUN_DURATION = Histogram(
    "devops_lab_worker_run_duration_seconds",
    "Time one worker tick took, from selecting targets to writing results.",
)


def forget_targets(names: set[str]) -> None:
    """Drop the target_up series for targets that are gone or disabled.

    Without this a deleted target keeps its last value forever, and
    ``target_up == 0 for 10m`` fires against something nobody is checking on purpose.
    """
    for name in names:
        try:
            TARGET_UP.remove(name)
        except KeyError:
            pass

logger = logging.getLogger(__name__)

# A target is due when its most recent result is older than its interval. The truth lives
# in check_results rather than a denormalised targets.last_checked_at, so there is exactly
# one answer to "when was this last checked" and it cannot drift.
#
# This is also the query PR 3 measures: without an index on
# (target_id, checked_at DESC) the LATERAL lookup degrades as the table grows, and it
# degrades into a *stalled worker* rather than a slow endpoint — which is a far better
# lesson. devops_lab_db_query_duration_seconds{query="schedule"} is the instrument.
DUE_TARGETS = """
SELECT t.id, t.name, t.url, t.interval_seconds
  FROM targets t
  LEFT JOIN LATERAL (
      SELECT checked_at
        FROM check_results r
       WHERE r.target_id = t.id
       ORDER BY r.checked_at DESC
       LIMIT 1
  ) last ON true
 WHERE t.enabled
   AND (last.checked_at IS NULL
        OR last.checked_at < now() - make_interval(secs => t.interval_seconds))
"""


async def check_one(client: httpx2.AsyncClient, target: dict[str, Any]) -> dict[str, Any]:
    """Perform one check. Never raises.

    Every outcome is a row: a non-2xx, a timeout, a DNS failure and a TLS error are all
    results, not exceptions. An exception escaping here would take down the loop, and a
    worker that dies because one website is broken is worse than useless.
    """
    name, url = target["name"], target["url"]
    started = time.monotonic()
    try:
        response = await client.get(url)
        elapsed = time.monotonic() - started
        ok = 200 <= response.status_code < 400
        outcome = {
            "result": "success" if ok else "failure",
            "status_code": response.status_code,
            "duration_ms": int(elapsed * 1000),
            "error": None if ok else f"HTTP {response.status_code}",
        }
    except (httpx2.TimeoutException, asyncio.TimeoutError):
        elapsed = time.monotonic() - started
        outcome = {
            "result": "timeout",
            "status_code": None,
            "duration_ms": int(elapsed * 1000),
            "error": "timed out",
        }
    except Exception as exc:
        elapsed = time.monotonic() - started
        outcome = {
            "result": "failure",
            "status_code": None,
            "duration_ms": int(elapsed * 1000),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }

    CHECK_DURATION.labels(target=name).observe(elapsed)
    CHECKS_TOTAL.labels(target=name, result=outcome["result"]).inc()
    TARGET_UP.labels(target=name).set(1 if outcome["result"] == "success" else 0)
    logger.info(
        "check complete",
        extra={
            "target": name,
            "url": url,
            "result": outcome["result"],
            "status_code": outcome["status_code"],
            "duration_ms": outcome["duration_ms"],
        },
    )
    return {"target_id": target["id"], **outcome}


async def tick(client: httpx2.AsyncClient, concurrency: int) -> int:
    """One pass: select what is due, check it, write the results."""
    async with db.query("schedule") as conn:
        cursor = await conn.execute(DUE_TARGETS)
        due = await cursor.fetchall()

    if not due:
        return 0

    limit = asyncio.Semaphore(concurrency)

    async def bounded(target):
        async with limit:
            return await check_one(client, target)

    results = await asyncio.gather(*(bounded(t) for t in due))

    async with db.query("insert_results") as conn:
        for row in results:
            await conn.execute(
                """
                INSERT INTO check_results (target_id, result, status_code, duration_ms, error)
                VALUES (%(target_id)s, %(result)s, %(status_code)s, %(duration_ms)s, %(error)s)
                """,
                row,
            )
    return len(results)


# Names this process has published a target_up series for. Tracked here rather than read
# back out of prometheus_client, whose label registry is private API.
_published: set[str] = set()


async def sync_series() -> None:
    """Drop target_up series for targets that are gone or disabled."""
    async with db.query("live_targets") as conn:
        cursor = await conn.execute("SELECT name FROM targets WHERE enabled")
        live = {r["name"] for r in await cursor.fetchall()}
    stale = _published - live
    forget_targets(stale)
    _published.difference_update(stale)
    _published.update(live)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    start_http_server(settings.worker_metrics_port)
    logger.info(
        "worker starting",
        extra={
            "tick_seconds": settings.check_interval_seconds,
            "timeout_seconds": settings.check_timeout_seconds,
            "concurrency": settings.check_concurrency,
            "metrics_port": settings.worker_metrics_port,
        },
    )

    await db.open_pool(settings)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)

    timeout = httpx2.Timeout(settings.check_timeout_seconds)
    try:
        async with httpx2.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            while not stopping.is_set():
                started = time.monotonic()
                try:
                    with WORKER_RUN_DURATION.time():
                        checked = await tick(client, settings.check_concurrency)
                        await sync_series()
                    WORKER_LAST_RUN.set(time.time())
                    if checked:
                        logger.info("tick complete", extra={"checked": checked})
                except Exception as exc:
                    # A failing tick must not end the loop; the stale last-run timestamp
                    # is what tells anyone that something is wrong.
                    logger.error("tick failed", extra={"error": f"{type(exc).__name__}: {exc}"})

                elapsed = time.monotonic() - started
                remaining = max(0.0, settings.check_interval_seconds - elapsed)
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass
    finally:
        await db.close_pool()
        logger.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
