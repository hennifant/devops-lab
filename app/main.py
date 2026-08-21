"""The API: manage uptime targets, and answer the two questions an operator asks.

``/health`` and ``/ready`` are deliberately different endpoints. Liveness answers "should
this container be restarted"; readiness answers "can this container serve a real request".
Conflating them is how a stack ends up reporting healthy while every request fails on the
database — which is exactly what this lab did before PR 1.
"""

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlparse

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field, field_validator

from app import db
from app.config import get_settings
from app.logging import configure_logging
from app.metrics import TARGETS_TOTAL

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("starting", extra={"max_targets": settings.max_targets})

    await db.open_pool(settings)
    # Best effort, and time-boxed: the database may legitimately be a few seconds behind
    # us, and startup must not block on it — /health has to answer either way. Readiness
    # refreshes the gauge as soon as the database appears, so a failure here self-heals.
    try:
        TARGETS_TOTAL.set(await db.count_targets(timeout=db.SHORT_TIMEOUT))
    except Exception as exc:
        logger.warning("could not prime targets gauge", extra={"error": str(exc)})

    try:
        yield
    finally:
        await db.close_pool()
        logger.info("stopped")


app = FastAPI(title="DevOps Lab API", lifespan=lifespan)

Instrumentator().instrument(app).expose(app)


@app.middleware("http")
async def access_log(request: Request, call_next):
    """Replaces uvicorn's access log with real fields instead of a formatted string."""
    started = time.perf_counter()
    response = await call_next(request)
    logger.info(
        "request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return response


class TargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=2048)
    interval_seconds: int = Field(default=60, ge=1)

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("url must use http or https")
        if not parsed.netloc:
            raise ValueError("url must include a host")
        return value


class TargetUpdate(BaseModel):
    enabled: bool


class CheckResultOut(BaseModel):
    id: int
    target_id: int
    checked_at: datetime
    result: str
    status_code: int | None
    duration_ms: int | None
    error: str | None


class TargetOut(BaseModel):
    id: int
    name: str
    url: str
    interval_seconds: int
    enabled: bool
    created_at: datetime


@app.get("/")
def root():
    return {"message": "Hello from the DevOps Lab"}


@app.get("/health")
def health():
    """Liveness. Touches nothing — no database, no network. The process is running."""
    return {"status": "healthy"}


@app.get("/ready")
async def ready(response: Response):
    """Readiness. The database answers and this build's migrations are applied."""
    ok, checks = await db.check_readiness()
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not ready", "checks": checks}

    TARGETS_TOTAL.set(await db.count_targets())
    return {"status": "ready", "checks": checks}


@app.get("/api/targets", response_model=list[TargetOut])
async def list_targets():
    async with db.query("list_targets") as conn:
        cursor = await conn.execute(
            """
            SELECT id, name, url, interval_seconds, enabled, created_at
              FROM targets
             ORDER BY id
            """
        )
        return await cursor.fetchall()


@app.post("/api/targets", response_model=TargetOut, status_code=status.HTTP_201_CREATED)
async def create_target(payload: TargetCreate):
    settings = get_settings()
    if payload.interval_seconds < settings.min_interval_seconds:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"interval_seconds must be at least {settings.min_interval_seconds}",
        )

    async with db.query("create_target") as conn:
        cursor = await conn.execute("SELECT count(*) AS n FROM targets")
        row = await cursor.fetchone()
        # The ceiling is a cardinality limit, not a product decision: every target is a
        # label value on every check metric.
        if int(row["n"]) >= settings.max_targets:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"target limit of {settings.max_targets} reached",
            )

        try:
            cursor = await conn.execute(
                """
                INSERT INTO targets (name, url, interval_seconds)
                     VALUES (%(name)s, %(url)s, %(interval_seconds)s)
                  RETURNING id, name, url, interval_seconds, enabled, created_at
                """,
                payload.model_dump(),
            )
        except psycopg.errors.UniqueViolation:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"a target named {payload.name!r} already exists"
            ) from None
        created = await cursor.fetchone()

    TARGETS_TOTAL.set(await db.count_targets())
    logger.info("target created", extra={"target_id": created["id"], "target": created["name"]})
    return created


@app.get("/api/targets/{target_id}", response_model=TargetOut)
async def get_target(target_id: int):
    async with db.query("get_target") as conn:
        cursor = await conn.execute(
            """
            SELECT id, name, url, interval_seconds, enabled, created_at
              FROM targets
             WHERE id = %s
            """,
            (target_id,),
        )
        target = await cursor.fetchone()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such target")
    return target


@app.patch("/api/targets/{target_id}", response_model=TargetOut)
async def update_target(target_id: int, payload: TargetUpdate):
    """Enable or disable a target.

    Silencing a known-broken target without destroying its history is the move an
    operator makes against a permanently firing alert; DELETE cascades the history away
    and is the wrong tool for it.
    """
    async with db.query("update_target") as conn:
        cursor = await conn.execute(
            """
            UPDATE targets
               SET enabled = %s
             WHERE id = %s
         RETURNING id, name, url, interval_seconds, enabled, created_at
            """,
            (payload.enabled, target_id),
        )
        updated = await cursor.fetchone()
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such target")
    logger.info(
        "target updated",
        extra={"target_id": updated["id"], "target": updated["name"], "enabled": updated["enabled"]},
    )
    return updated


@app.delete("/api/targets/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(target_id: int):
    async with db.query("delete_target") as conn:
        cursor = await conn.execute(
            "DELETE FROM targets WHERE id = %s RETURNING name", (target_id,)
        )
        deleted = await cursor.fetchone()
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such target")

    TARGETS_TOTAL.set(await db.count_targets())
    logger.info("target deleted", extra={"target_id": target_id, "target": deleted["name"]})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/targets/{target_id}/results", response_model=list[CheckResultOut])
async def list_results(target_id: int, limit: int = Query(100, ge=1, le=1000)):
    """Most recent check results for one target, newest first."""
    async with db.query("list_results") as conn:
        cursor = await conn.execute("SELECT 1 FROM targets WHERE id = %s", (target_id,))
        if await cursor.fetchone() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such target")
        cursor = await conn.execute(
            """
            SELECT id, target_id, checked_at, result, status_code, duration_ms, error
              FROM check_results
             WHERE target_id = %s
             ORDER BY checked_at DESC
             LIMIT %s
            """,
            (target_id, limit),
        )
        return await cursor.fetchall()


@app.get("/api/status")
async def aggregate_status():
    """Counts of up / down / unknown, from each target's most recent result.

    A target with no result yet is ``unknown`` rather than down — the worker may simply
    not have reached it. Conflating the two would make a fresh deployment look broken.
    """
    async with db.query("aggregate_status") as conn:
        cursor = await conn.execute(
            """
            SELECT
                count(*) FILTER (WHERE t.enabled AND last.result = 'success')  AS up,
                count(*) FILTER (WHERE t.enabled AND last.result IS NOT NULL
                                   AND last.result <> 'success')               AS down,
                count(*) FILTER (WHERE t.enabled AND last.result IS NULL)      AS unknown,
                count(*) FILTER (WHERE NOT t.enabled)                          AS disabled
              FROM targets t
              LEFT JOIN LATERAL (
                  SELECT result
                    FROM check_results r
                   WHERE r.target_id = t.id
                   ORDER BY r.checked_at DESC
                   LIMIT 1
              ) last ON true
            """
        )
        row = await cursor.fetchone()
    return {k: int(row[k]) for k in ("up", "down", "unknown", "disabled")}
