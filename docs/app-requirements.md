# Application requirements

Specification for the application this lab operates. Written for whoever implements it —
including another agent — so the platform work already done still fits.

**Read [engineering.md](engineering.md) and [adr/](adr/) first.** They describe constraints
that are not negotiable here.

## Why an application at all

The lab currently runs a FastAPI service with three endpoints and an unused Postgres. That
is enough to prove a pipeline works and not enough to prove anything can be *operated*.
There is nothing for monitoring to show, nothing for alerting to catch, nothing whose loss
would justify a backup.

So the application is not chosen for its features. **It is chosen for the failure modes it
produces.** A todo list has none, which is why nobody learns operations from one.

## What it does

An uptime checker. A background worker periodically fetches a list of URLs and records the
outcome; an HTTP API manages the list and exposes the results.

```
API  ──manage targets──▶  Postgres  ◀──write results──  Worker  ──HTTP──▶  the internet
```

Chosen because the failure modes arrive on their own. The internet breaks constantly —
timeouts, TLS errors, 500s, DNS failures — so realistic alerting scenarios occur without
generating synthetic load. It also grows a table over time, which makes indexes, retention,
vacuum and backup real rather than theoretical.

### Explicitly out of scope

No authentication, no user accounts, no frontend, no notifications. Anything that does not
produce an operational consequence is scope that competes with the point of the project.
The application must stay small enough that the lifecycle around it remains the subject.

## Operational contract

These are the non-negotiables. They matter more than the feature set.

### `GET /health` — liveness

Returns 200 if the process is alive. **Touches nothing else** — no database, no network.
Answers only "should this container be restarted".

### `GET /ready` — readiness

Returns 200 only if the database is reachable **and** migrations are at head. Returns 503
otherwise, with a body naming which check failed.

This is the endpoint the Compose healthcheck and the deployment smoke test use. The
distinction matters: today's healthcheck hits `/health`, so a container can report healthy
while answering every real request with a database error.

### `GET /metrics`

Prometheus exposition. Application metrics, not only HTTP counters — see below.

### Migrations

Versioned, with Alembic. Runnable as a standalone command, idempotent, and executed
**before** the application starts — as a separate Compose service, not from the application
entrypoint:

```yaml
migrate:
  image: <same image>
  command: ["alembic", "upgrade", "head"]
  depends_on:
    db:
      condition: service_healthy

api:
  depends_on:
    migrate:
      condition: service_completed_successfully
```

Running migrations from the entrypoint appears simpler and breaks the moment there is more
than one replica.

### Configuration

From environment variables only. No configuration file in the image. Every setting needs a
sane default except credentials, which must fail loudly when absent rather than silently
becoming an empty string — the lab has already shipped `postgresql://:@db:5432/` once
([ADR 0003](adr/0003-deployment-env-from-repository-secrets.md)).

### Shutdown

Handle `SIGTERM`: stop accepting new work, finish in-flight requests, close the pool, exit.
Without it every deploy drops requests, and the worker can leave a half-written result.

### Logs

Structured, one JSON object per line, to stdout. No log files, no rotation — the container
runtime owns that.

### Seed

A documented way to insert deterministic sample targets, so staging and smoke tests have
data without manual clicking.

## HTTP API

```
GET    /health
GET    /ready
GET    /metrics

GET    /api/targets                    list
POST   /api/targets                    {name, url, interval_seconds}
GET    /api/targets/{id}
DELETE /api/targets/{id}
GET    /api/targets/{id}/results?limit=100

GET    /api/status                     aggregate: counts of up / down / unknown
```

Validation is part of the point: a rejected malformed URL is a 4xx that should show up in
metrics distinctly from a 5xx.

## Data model

```
targets
  id                serial primary key
  name              text not null
  url               text not null
  interval_seconds  int not null default 60
  enabled           bool not null default true
  created_at        timestamptz not null default now()

check_results
  id           bigserial primary key
  target_id    int not null references targets(id) on delete cascade
  checked_at   timestamptz not null default now()
  result       text not null          -- success | failure | timeout
  status_code  int null
  duration_ms  int null
  error        text null

  index on (target_id, checked_at desc)
```

`check_results` grows without bound by design. Deciding what to do about that — retention,
partitioning, aggregation — is a later exercise, and it should be reachable through metrics
before it is fixed.

## Worker

- Runs on a fixed tick. Selects targets whose `interval_seconds` has elapsed.
- Bounded concurrency; a configurable limit, not one task per target.
- Per-request timeout strictly below the shortest interval.
- A failing target must never take down the worker loop. Every outcome — success, non-2xx,
  timeout, DNS failure, TLS error — is a recorded row, not an exception that escapes.
- Records its last completed run in a metric so a stalled worker is detectable from outside.

## Metrics

Concrete names, because Prometheus rules and Grafana dashboards will be written against
them:

```
devops_lab_checks_total{target, result}              counter    result: success|failure|timeout
devops_lab_check_duration_seconds{target}            histogram
devops_lab_target_up{target}                         gauge      1 or 0
devops_lab_worker_last_run_timestamp_seconds         gauge
devops_lab_worker_run_duration_seconds               histogram
devops_lab_targets_total                             gauge
devops_lab_db_query_duration_seconds{query}          histogram
```

Keep the `target` label to the target *name*, not the URL. URLs as label values invite
unbounded cardinality the moment someone adds a query string.

These make alerts possible that say something:

```
time() - devops_lab_worker_last_run_timestamp_seconds > 300     worker stalled
devops_lab_target_up == 0 for 10m                               a target is down
histogram_quantile(0.99, ...) > 2                               checks got slow
```

Compare with the only alert that exists today, `up == 0`, which fires when the application
is gone entirely and is silent about everything else.

## Failure modes it must be able to produce

This list is the acceptance criterion. If a scenario cannot be triggered, the application
is not doing its job for this project.

| Scenario | How to trigger | What should be observable |
| --- | --- | --- |
| Target down | add an unreachable URL | `target_up` drops to 0, failure counter rises |
| Target slow | add a deliberately slow URL | duration histogram shifts |
| Database gone | stop the `db` container | `/ready` returns 503, container unhealthy |
| Worker stalled | pause the worker | `worker_last_run` goes stale |
| Table growth | let it run | query duration climbs without the right index |

## Fitting the existing platform

Constraints from work already done — breaking any of these breaks a documented decision.

- **Python 3.14, FastAPI, psycopg 3.** `psycopg[binary]` is already declared and unused.
- **Dependencies:** add to `requirements.in`, then recompile. Never edit a `.txt` by hand.
  Use `--output-file`, never `-o` — Renovate replays the header command and its parser
  rejects the short form ([ADR 0006](adr/0006-compiled-requirements-with-uv.md)).
- **`.dockerignore` is an allowlist.** It denies everything and permits `requirements.txt`
  and `app/`. A new top-level directory — `alembic/`, `migrations/` — **will not reach the
  build** until it is added there. Expect the first build to fail on this
  ([ADR 0005](adr/0005-dockerignore-as-an-allowlist.md)).
- **The image builds for `linux/amd64` and `linux/arm64`.** No dependency that lacks a wheel
  for both, unless a build stage is added for it.
- **Non-root `appuser`, pinned base image digest.** Do not unpin it; Renovate maintains it.
- **No `:latest`** on anything that gets deployed.
- **No `container_name:`** anywhere in Compose — it is a Docker-wide name that breaks
  project isolation ([ADR 0001](adr/0001-two-isolated-compose-projects.md)).
- **Tests run on `ubuntu-latest`** and must not require a live database, or they must bring
  their own. CI has no Postgres service today.

## What the smoke test will assert

Once this exists, the deployment smoke test stops being a liveness poll and becomes:

```
1. GET /ready              → 200, so the database is reachable and migrations applied
2. POST /api/targets       → 201
   GET  /api/targets/{id}  → 200, the record round-tripped through Postgres
3. GET /metrics            → contains devops_lab_targets_total, and it increased
```

That is evidence worth promoting a build to production on. `curl /health` is not.
