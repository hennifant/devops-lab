# 0012. Hand-written SQL, with Alembic for migrations only

Date: 2026-08-20
Status: Accepted

## Context

Until now the database was decorative. Postgres ran, `psycopg[binary]` was declared in
[requirements.in](../../requirements.in), and no line of application code imported it. The
smoke test could prove the process served HTTP and nothing more.

[docs/app-requirements.md](../app-requirements.md) fixes that by specifying an uptime
checker, and states the learning goal that decides this record: *a query that can get
slow*. PR 2 ships a scheduling query without an index on purpose and PR 3 adds the index,
so the improvement is a measurement rather than an assertion.

That goal only pays off if `EXPLAIN ANALYZE` can be put in front of the exact statement
that ran. Every layer between the code and the SQL makes that indirect.

## Decision

Data access is hand-written SQL executed through psycopg 3, with `psycopg_pool` for
pooling. No ORM and no query builder. Alembic handles migrations only, with hand-written
`op.execute` DDL rather than `autogenerate`, because there are no models to generate from.

Migrations run as their own one-shot Compose service, not from the application entrypoint:

```yaml
migrate:
  image: <the same image as api>
  restart: "no"
  command: ["alembic", "upgrade", "head"]
  depends_on:
    db: { condition: service_healthy }
```

**One migration per pull request** rather than one large initial migration. PR 1 creates
`targets`, PR 2 creates `check_results`, PR 3 adds the index.

## Consequences

- Every statement that reaches production is a statement that was in the review, and can
  be pasted into psql unchanged.
- Serialisation is manual. Rows come back as dicts through `dict_row` and are validated by
  Pydantic response models on the way out; nothing does that automatically.
- Migrations are written by hand. `autogenerate` is unavailable, which is exactly the
  safety net that would otherwise catch a forgotten column.
- **Alembic pulls SQLAlchemy, Mako and greenlet into the runtime image**, even though no
  application code imports them. The `migrate` service is the same image as `api`, so
  splitting them would mean a second artefact and a second build path, which
  [0011](0011-build-once-deploy-many.md) exists to avoid. This is a bill, not an oversight.
  `yoyo-migrations` would have avoided it; Alembic wins because it is what people use, and
  the transferable skill is worth four packages.
- `psycopg-pool` is a **new dependency**. An earlier draft of the specification claimed the
  pool was already declared. It was not: `psycopg[binary]` resolves to `psycopg` and
  `psycopg-binary` only, and the pool is reached through `psycopg[binary,pool]`.
- `greenlet` is compiled, but publishes wheels for `aarch64` and `x86_64`, so the
  multi-architecture build is unaffected.
- The `migrate` service must override `restart:`. Everything else in
  [compose.yaml](../../compose.yaml) inherits `${RESTART_POLICY:-unless-stopped}`
  ([0009](0009-restart-policy-and-pinned-monitoring-images.md)); a one-shot service that
  inherits it exits zero, is restarted, exits zero, and loops forever.
- `alembic/` and `alembic.ini` had to be added to the [.dockerignore](../../.dockerignore)
  allowlist. Anyone adding a top-level directory later will hit the same wall
  ([0005](0005-dockerignore-as-an-allowlist.md)) — which is the allowlist working.
- Three migrations across three deploys demonstrate that the migration path survives being
  used more than once. A single upfront migration would only demonstrate that Alembic can
  bootstrap an empty database.

## Background

### What the pool is actually for

`AsyncConnectionPool` keeps a small number of connections open and hands them out.
Postgres forks a backend process per connection, so connecting per request means paying a
process fork and a TLS handshake on every request.

Two settings here are load-bearing rather than incidental:

- **`open=False` followed by `await pool.open(wait=False)`.** Opening a pool that waits
  for the database makes application startup depend on the database being up, which would
  take `/health` down with it — see [0013](0013-liveness-and-readiness.md).
- **A bounded acquisition timeout.** psycopg_pool waits thirty seconds for a connection by
  default. With the database down that turned startup into a thirty-second stall and made
  `/ready` take half a minute to report something it already knew, while the Compose
  healthcheck allows five seconds. `app/db.py` uses three seconds for the startup and
  readiness paths. This was found by a test taking ninety seconds, which is the only
  reason anyone noticed.

### Why migrations are not run from the entrypoint

Running `alembic upgrade head` before `uvicorn` in an entrypoint script is one line and
works — until there are two replicas. Both start, both run the migration, and the second
one either fails or applies DDL concurrently with the first. Alembic takes a lock on
`alembic_version`, so the usual outcome is a container that dies at startup.

A separate service makes the ordering explicit and gives Compose something to gate on:

```yaml
api:
  depends_on:
    migrate:
      condition: service_completed_successfully
```

`service_completed_successfully` waits for a zero exit, so a failed migration stops the
deploy instead of starting an application against a schema that is not there.

### `targets.name` is UNIQUE, which the specification did not say

Two reasons it has to be. The seed upserts on the name, which needs a unique constraint
for `ON CONFLICT (name)` to have anything to conflict on. And the name becomes the
`target` label on every check metric in PR 2 — two targets sharing a name would silently
collide into one time series.

### The `postgresql://` scheme has to be rewritten for Alembic

`DATABASE_URL` is a libpq connection string. SQLAlchemy reads the same URL as a dialect
specifier and resolves a bare `postgresql://` to psycopg2, which this image does not
install, so migrations die with `ModuleNotFoundError: No module named 'psycopg2'`.
[alembic/env.py](../../alembic/env.py) rewrites the scheme to `postgresql+psycopg://` and
reads the URL from the environment rather than `alembic.ini` — that file is committed and
the credentials are not.

- [psycopg 3 connection pools](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)
- [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- [Compose `depends_on` conditions](https://docs.docker.com/reference/compose-file/services/#depends_on)
