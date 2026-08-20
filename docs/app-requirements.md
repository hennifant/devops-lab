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

- **Python 3.14, FastAPI, psycopg 3.** `psycopg[binary]` is declared and unused. Add the
  `pool` extra.
- **`httpx2` is currently a development dependency only.** It sits in `requirements-dev.in`
  for the FastAPI `TestClient`. The worker needs an HTTP client at runtime, so it moves to
  `requirements.in`; the line in `requirements-dev.in` is then redundant because that file
  already pulls in `-r requirements.in`. Recompile both `.txt` files.
- **Alembic drags SQLAlchemy into the deployed image.** `alembic` depends on `sqlalchemy`,
  `mako`, `markupsafe` and `greenlet`. Because the `migrate` service runs the same image,
  SQLAlchemy ships to production even though no application code imports it. Accepted
  deliberately — see the decision below.
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

---

# Decisions on the implementation questions

## Data access: hand-written SQL on psycopg 3

No ORM, no query builder. `psycopg` with `psycopg_pool`, and Alembic for migrations only,
with hand-written `op.execute` DDL rather than `autogenerate`.

**Correction to an earlier draft of this document:** `requirements.in` declares
`psycopg[binary]`, which resolves to `psycopg` and `psycopg-binary` and *not* the pool.
The pool is a separate distribution, `psycopg-pool`, reached through the extra
`psycopg[binary,pool]`. It is a new dependency — pure Python with wheels for both target
architectures, so harmless, but the claim "no new dependency" was wrong. Say so in the ADR.

The reason is the stated learning goal. "A query that can get slow" is only instructive if
you can put `EXPLAIN ANALYZE` in front of the exact statement that ran and watch the plan
change when an index appears. An abstraction layer makes that indirect. The ORM failure
class — N+1, lazy loading, session lifecycle — is a genuine skill and a distraction from
this project's subject.

The cost is real: serialisation by hand, more boilerplate, and migrations written as SQL
instead of generated. Accepted, and the application is small enough to bound it.

**Connection pool lifecycle.** Open an `AsyncConnectionPool` in a FastAPI lifespan handler
and close it on shutdown. This is not decoration — it is what makes the SIGTERM requirement
real. A pool that is never closed leaves connections held on the server after the container
is gone, and Postgres will keep them until timeout.

## Worker: its own container, from the same image

A `worker` service running `ghcr.io/hennifant/devops-lab-api:<sha>` with a different
`command`. One image, one build, no second artefact — [ADR 0011](adr/0011-build-once-deploy-many.md)
continues to apply unchanged.

An asyncio task inside the API process would give one container fewer and destroy the
reason the worker exists. The point is a *second failure surface*: the API serving traffic
while the worker is dead, and that being visible. Inside the same process it is neither
separately restartable nor separately observable.

A separate image and repository would be cleaner in principle and doubles the build path
for no benefit at this size.

Three details that follow:

- **Metrics port: 9101, not 9100.** `prometheus_client.start_http_server(9101)` in the
  worker. 9100 is node-exporter's conventional port, and node-exporter arrives in Phase 3.
  Colliding now guarantees confusion later.
- **No `ports:` entry for the worker.** Prometheus reaches it over the Compose network as
  `worker:9101`. Publishing it on the host would put an unauthenticated endpoint on the LAN.
- **`restart: ${RESTART_POLICY:-unless-stopped}`** like every other service
  ([ADR 0009](adr/0009-restart-policy-and-pinned-monitoring-images.md)).

### The `migrate` service must set `restart: "no"`

`compose.yaml` applies `restart: ${RESTART_POLICY:-unless-stopped}` to every service. A
one-shot service inheriting that will run `alembic upgrade head`, exit 0, be restarted,
exit 0, and loop forever — while `depends_on: service_completed_successfully` may never
settle. Override it explicitly on that service.

### Compose healthcheck moves to `/ready`

The image `HEALTHCHECK` stays on `/health` — it answers "should this container be
restarted", and restarting an API because the database is briefly gone is wrong.

The Compose healthcheck for `api` moves to `/ready`, because that is what `depends_on`
and the smoke test should gate on. A transient database outage marking `api` unhealthy is
correct behaviour, not a bug.

The worker has no HTTP API; healthcheck it against its metrics port.

## Staging runs the worker

Staging becomes `api db migrate worker`.

This does not contradict [ADR 0011](adr/0011-build-once-deploy-many.md). That record
excluded Prometheus, Alertmanager and Grafana from staging because they are *infrastructure*
already exercised by production. The worker is *application code under test*. Different
category — promoting it untested would leave the second failure surface unverified, which
is the one thing staging is well placed to catch here.

Two mitigations against a single host doing double outbound traffic:

- A larger `CHECK_INTERVAL_SECONDS` in staging than in production.
- **Staging seeds point at the staging stack's own `/health`.** Self-referential, zero
  external traffic, and it still exercises the whole path: worker → HTTP → database →
  metrics. Production seeds may point outward.

## Delivery: three pull requests

```
PR 1  Contract and database    Alembic, schema, /ready, config from env,
                               structured logs, SIGTERM, seed, targets CRUD,
                               smoke test upgrade, Postgres service in CI   → ADR 0012
PR 2  Worker and telemetry     checks, results, devops_lab_* metrics,
                               scrape job, alert rules                      → ADR 0013
PR 3  Hardening                indexes with EXPLAIN evidence, retention,
                               Grafana dashboard as code                    → ADR 0014
```

Each merges green, deploys to staging automatically, and waits for approval before
production. Every ADR number above is provisional — check `docs/adr/README.md` for the
next free one at the time of writing.

One consequence to make deliberate rather than accidental: **PR 2 ships a query that is
knowingly slow, and PR 3 fixes it.** That inversion is the point — it produces a
before-and-after measurement instead of an assertion that an index helps. Record it in
ADR 0013 so nobody later reads it as an oversight.

A single pull request would be the fastest route to a running system and the worst to
understanding: a broken deploy would have ten suspects instead of two.

## Additional constraints not raised in the questions

- **Cardinality has a hard ceiling.** `devops_lab_checks_total{target, result}` is
  targets × 3. Cap targets at 50 and reject creation beyond it with a 4xx. Document the
  number. An uncapped label on user-supplied data is how Prometheus dies.
- **CI keeps fast tests fast.** A `postgres:18` service container is right for tests that
  need a database; tests that do not should not wait for one. The connection URL comes from
  an environment variable with a default pointing at the service container.
- **Alert rules land in `monitoring/prometheus/rules/`** and are picked up by the deploy
  through `MONITORING_CONFIG_HASH`, which forces Prometheus to be recreated when the
  configuration changes ([ADR 0003](adr/0003-deployment-env-from-repository-secrets.md)).
  They will fire into nothing until Phase 3 gives Alertmanager a destination. That is
  expected, and a rule that fires into nothing is still worth writing — you can see it in
  the Alertmanager UI.

---

# Round 2: corrections and remaining decisions

## Alembic and SQLAlchemy in the runtime image

Accepted. Name it in ADR 0012 so nobody later reads it as an oversight.

The tension is real: the data-access decision is "no ORM", and Alembic is SQLAlchemy's
migration tool. Using it without SQLAlchemy means paying for half a tool. `yoyo-migrations`
would avoid the dependency entirely and is a closer fit to hand-written SQL.

Alembic wins anyway, for a reason outside the code: it is what people use, so it is the
transferable skill. The cost is a handful of megabytes and four more packages for Renovate
to track and Trivy to scan — not a design flaw, just a bill. `greenlet` is compiled but
publishes wheels for `aarch64` and `x86_64`, so multi-architecture builds are unaffected.

## Scheduling: the LATERAL join, unindexed in PR 2

Option (a). One source of truth; no denormalised `last_checked_at` that can drift.

**Ship it without the index in PR 2, on purpose.** The alternative — index immediately and
measure on `/api/targets/{id}/results` instead — produces a weaker lesson. A slow results
endpoint is a slow endpoint. A slow *scheduling* query starves the worker loop, which shows
up as a stale `devops_lab_worker_last_run_timestamp_seconds` and fires an alert. The
degradation surfaces as an operational symptom rather than a benchmark, which is the whole
point of doing it in this order.

Two conditions on that, so it stays an experiment rather than an accident:

- Instrument it from the start: `devops_lab_db_query_duration_seconds{query="schedule"}`.
  That histogram is the measuring instrument for the before-and-after in PR 3, and it makes
  the degradation visible before it hurts.
- Document the escape hatch in ADR 0013 — the one-line `CREATE INDEX CONCURRENTLY` to run
  by hand if it degrades faster than expected. An experiment you cannot stop is a hazard.

## Seed: automatic, from `SEED_TARGETS`, gated on it being set

Mechanism (b) + (e), with one change: **the seed step runs only when `SEED_TARGETS` is
non-empty.**

Idempotent upsert on `name`, immediately after `migrate`, list parsed from the variable,
which `deploy.yml` already writes per environment. No code path knows an environment name.

The change matters for production. With an unconditional seed, deleting a target through
the API would see it silently reappear on the next deploy — the deployment would own
application data that the API claims to own. So `SEED_TARGETS` is set in staging and left
empty in production, where targets are created through the API by a human and persist.

Staging's value points at the staging stack itself:

```
SEED_TARGETS=api-self=http://api:8000/health
```

Zero external traffic, and the whole path still runs: worker → HTTP → database → metrics.

## Smoke test: create, verify, delete

Option (a), with `DELETE` in an `always()` step so an aborted run leaves nothing behind.
Use a name unique per run — include the run id — so a previously failed cleanup cannot
collide.

The metric assertion is reworded rather than dropped. Read `/metrics` *between* the POST
and the DELETE:

```
1. GET /ready                        → 200
2. GET /metrics                      → record devops_lab_targets_total as baseline
3. POST /api/targets                 → 201
4. GET  /api/targets/{id}            → 200, the record round-tripped through Postgres
5. GET /metrics                      → devops_lab_targets_total == baseline + 1
6. DELETE /api/targets/{id}          → in always()
```

That is precise, survives cleanup, and needs no metric invented for the test's benefit.

## Logging: stdlib `logging` with a JSON formatter

Option (a). The problem is not formatting, it is that uvicorn owns `uvicorn`,
`uvicorn.access` and `uvicorn.error`, and that cost is identical in all three options. No
new dependency for something a `dictConfig` and thirty lines cover.

One refinement: **disable uvicorn's access log** and emit access records from a middleware
instead. Routing uvicorn's access logger through a JSON formatter yields JSON wrapping a
preformatted string — structurally valid, semantically useless, because method, path,
status and duration stay trapped inside one text field. A middleware emits them as real
fields, and it already has the duration that the metrics need.

## `enabled`: yes, `PATCH`, and remove the series

Option (a). Silencing a known-broken target without destroying its history is exactly the
move an operator makes against a permanently firing alert; `DELETE` cascades the history
away and is the wrong tool.

And yes — **a disabled target's `devops_lab_target_up` series must be removed**, not frozen.
A stuck gauge means `target_up == 0 for 10m` fires against something deliberately
unmonitored. That is how people learn to ignore alerts.

## On the self-decided list

All sound. One correction:

**Startup configuration validation cannot compare against the smallest
`interval_seconds`.** That value lives in the database, so checking it at startup either
requires a query — which defeats fail-fast, and fails when the database is briefly gone —
or reads a value that changes the moment someone creates a target.

Keep it purely at configuration level: introduce `MIN_INTERVAL_SECONDS`, enforce it when a
target is created, and assert `CHECK_TIMEOUT_SECONDS < MIN_INTERVAL_SECONDS` at startup.
Same protection against overlapping checks, no database dependency in the startup path.
