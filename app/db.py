"""Database access: an async connection pool and hand-written SQL.

No ORM and no query builder — see ADR 0012. Every statement here is one you can paste
into psql behind ``EXPLAIN ANALYZE`` and watch the plan change when an index appears.

Two details are load-bearing rather than decorative:

* The pool is opened without waiting. A pool that blocks on an unreachable database at
  startup takes ``/health`` down with it, which defeats the point of separating liveness
  from readiness.
* The pool is closed on shutdown. That is what makes the SIGTERM requirement real: a pool
  that is never closed leaves connections held on the server after the container is gone,
  and Postgres keeps them until they time out.
"""

import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.metrics import DB_QUERY_DURATION

logger = logging.getLogger(__name__)

# /app/alembic.ini in the image, the repository root in a checkout — the package always
# sits one level below it.
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

# Bounded on purpose, and well below the Compose healthcheck's five-second timeout.
# psycopg_pool waits thirty seconds for a connection by default, which turns "the database
# is down" into "the container hangs": startup stalls before it can serve /health at all,
# and /ready takes half a minute to admit it is not ready.
SHORT_TIMEOUT = 3.0

_pool: AsyncConnectionPool | None = None


async def open_pool(settings: Settings) -> AsyncConnectionPool:
    global _pool
    _pool = AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    await _pool.open(wait=False)
    logger.info(
        "database pool opened",
        extra={"min_size": settings.db_pool_min, "max_size": settings.db_pool_max},
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close(timeout=SHORT_TIMEOUT)
        _pool = None
        logger.info("database pool closed")


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool is not open")
    return _pool


@asynccontextmanager
async def query(name: str, timeout: float | None = None):
    """Hand out a connection and time what happens on it.

    The label is the query's name, not its text: the histogram has to stay comparable
    across the before-and-after that PR 3 measures.
    """
    with DB_QUERY_DURATION.labels(query=name).time():
        async with get_pool().connection(timeout=timeout) as conn:
            yield conn


@lru_cache(maxsize=1)
def expected_revision() -> str:
    """The migration revision this build of the code expects to find applied.

    Read from the migration scripts rather than a constant, so it cannot fall out of sync
    with them. Alembic is in the image regardless — the ``migrate`` service runs it.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


async def check_readiness() -> tuple[bool, dict[str, str]]:
    """Return whether the database is usable, and a per-check detail map.

    Two separate questions. "Postgres answers" and "this code's schema is applied" fail
    for different reasons and need different fixes, so the body names which one broke
    instead of reporting a bare 503.
    """
    checks = {"database": "unknown", "migrations": "unknown"}
    try:
        async with query("ready", timeout=SHORT_TIMEOUT) as conn:
            await conn.execute("SELECT 1")
            checks["database"] = "ok"

            cursor = await conn.execute("SELECT version_num FROM alembic_version")
            row = await cursor.fetchone()
    except Exception as exc:
        checks["database"] = f"unreachable: {type(exc).__name__}"
        return False, checks

    applied = row["version_num"] if row else None
    expected = expected_revision()
    if applied != expected:
        checks["migrations"] = f"at {applied or 'none'}, expected {expected}"
        return False, checks

    checks["migrations"] = "ok"
    return True, checks


async def count_targets(timeout: float | None = None) -> int:
    async with query("count_targets", timeout=timeout) as conn:
        cursor = await conn.execute("SELECT count(*) AS n FROM targets")
        row = await cursor.fetchone()
    return int(row["n"])
