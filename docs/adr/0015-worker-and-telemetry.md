# 0015. The check worker, and telemetry worth alerting on

Date: 2026-08-21
Status: Accepted

## Context

[0012](0012-hand-written-sql-with-alembic.md) gave the lab a database and an API that
writes to it. Nothing observes any of it. The only alert rule is `up == 0`, which fires
when the application is gone entirely and is silent about every other way software fails.

There is also no second failure surface. One process serves HTTP; if it is alive, the
monitoring says everything is fine.

## Decision

A worker, in its own container, from the same image.

```yaml
worker:
  image: ghcr.io/hennifant/devops-lab-api:${IMAGE_TAG:-latest}
  command: ["python", "-m", "app.worker"]
```

One image, one build — [0011](0011-build-once-deploy-many.md) is unchanged. It ticks every
`CHECK_INTERVAL_SECONDS`, selects targets whose interval has elapsed, checks them with
bounded concurrency, and writes every outcome to `check_results`.

Metrics on port 9101, never published to the host; Prometheus reaches them over the Compose
network as `worker:9101`. **9101 rather than 9100**, which is node-exporter's conventional
port and arrives in Phase 3.

Six alert rules replace the one:

| Rule | What it catches |
| --- | --- |
| `WorkerDown` | the container is gone |
| `WorkerStalled` | the process is up but has stopped producing results |
| `TargetDown` | a target failing for ten minutes |
| `ChecksSlow` | p99 check duration above five seconds |
| `SchedulingQuerySlow` | the unindexed query starting to cost real time |
| `APIDown` | unchanged |

## Consequences

- The API can be healthy while the worker is dead, and that is now visible in two
  independent ways: `up{job="devops-lab-worker"}` and a stale last-run timestamp.
- `check_results` grows by roughly one row per target per interval, unbounded. Deliberate:
  retention, vacuum and backup stop being theoretical. PR 3 addresses it.
- Two more containers in production, one more in staging.
- Alerts can now be noisy in a way `up == 0` never was. `TargetDown` waits ten minutes
  because a single failed check is the internet being the internet.
- Nothing yet delivers these alerts anywhere except Gotify's web interface — see
  [0014](0014-alerting-to-gotify.md). Email is the next small step.

## Background

### Scheduling reads the truth rather than caching it

```sql
SELECT t.id, t.name, t.url, t.interval_seconds
  FROM targets t
  LEFT JOIN LATERAL (
      SELECT checked_at FROM check_results r
       WHERE r.target_id = t.id
       ORDER BY r.checked_at DESC LIMIT 1
  ) last ON true
 WHERE t.enabled
   AND (last.checked_at IS NULL
        OR last.checked_at < now() - make_interval(secs => t.interval_seconds));
```

The alternative — a `targets.last_checked_at` column written after each check — is always
fast and creates two sources of truth for one fact, which can drift. It would also delete
the experiment described next.

### The index is deliberately missing

There is no index on `(target_id, checked_at DESC)`. Migration 0002 says so, and PR 3 adds
it with an `EXPLAIN` before and after.

Shipping the index now would leave nothing to measure. More importantly, the *shape* of the
degradation is the lesson: this query runs on every tick, so as `check_results` grows the
sequential scan does not make an endpoint slow — it **starves the worker loop**, and that
surfaces as a stale `devops_lab_worker_last_run_timestamp_seconds` and a firing alert. A
performance problem arriving as an operational symptom is worth more than a benchmark.

`devops_lab_db_query_duration_seconds{query="schedule"}` is the instrument, and
`SchedulingQuerySlow` fires before the worker actually stalls.

**Escape hatch**, if it degrades faster than expected:

```sql
CREATE INDEX CONCURRENTLY idx_check_results_target_checked
    ON check_results (target_id, checked_at DESC);
```

An experiment that cannot be stopped is a hazard.

### Every outcome is a row, never an exception

A non-2xx, a timeout, a DNS failure and a TLS error are all *results*. `check_one` catches
everything and returns a dict. An exception escaping it would end the loop, and a worker
that dies because one website is broken is worse than no worker — it fails silently in the
direction of looking fine.

The tick itself is wrapped too: a failing tick logs and continues, because the stale
timestamp is what tells anyone something is wrong.

### Disabled targets lose their series

When a target is disabled or deleted, `devops_lab_target_up` for it is removed rather than
left at its last value. A frozen gauge means `target_up == 0 for 10m` fires against
something nobody is checking on purpose — which is how people learn to ignore alerts, the
same reasoning that removed the heartbeat in [0014](0014-alerting-to-gotify.md).

The set of published label values is tracked in the worker rather than read back out of
`prometheus_client`, whose label registry is private API.

### Worker metrics live in the worker, not in the shared module

*Learned in production, 2026-08-21, from a false alarm that reached Gotify.*

`WorkerStalled` fired while the worker was demonstrably healthy:

```
job=devops-lab-worker  devops_lab_worker_last_run_timestamp_seconds  age 5s
job=devops-lab-api     devops_lab_worker_last_run_timestamp_seconds  0
```

Both processes imported `app/metrics.py`, and **importing a module registers every metric
it defines**. The API therefore published the worker's gauges too. An unlabelled `Gauge`
sits at 0 until something sets it, and nothing in the API ever would, so `time() - 0`
satisfied the rule forever.

The zero was not missing data. It was a false statement, and the alert acted on it.

Two changes, because either alone leaves a trap: worker-only metrics are defined in
`app/worker.py`, and the rules select `{job="devops-lab-worker"}`. Genuinely shared
metrics — `devops_lab_db_query_duration_seconds`, `devops_lab_targets_total` — stay in
`app/metrics.py`.

### A quantile over almost no data is not a quantile

`ChecksSlow` also fired, on a worker that had just restarted and was behaving perfectly.
With a handful of observations `histogram_quantile` lands in the `+Inf` bucket and returns
infinity, which is greater than any threshold.

Both percentile rules now carry a sample-rate guard:

```promql
and sum(rate(devops_lab_check_duration_seconds_count{job="devops-lab-worker"}[10m])) > 0.05
```

Below that rate the estimate is not trustworthy and the rule stays quiet. Percentile alerts
without a volume guard are a standard way to generate noise after every restart.

### The pattern behind both

Neither alert was wrong about its own expression. Both were wrong about what the data
*meant* — a zero that stood for "never set" and an infinity that stood for "not enough
samples". An alert rule is a claim about reality, and the failure mode is not a missing
alert but one that cries wolf. The first real alerts this lab delivered end to end were
two false positives of its own making, which is worth keeping in the record rather than
tidying away.

### Cardinality

`devops_lab_checks_total{target, result}` is targets × 3. `MAX_TARGETS` defaults to 50, so
150 series. The `target` label carries the target *name*, never the URL: a URL with a query
string turns a bounded label into an unbounded one.

### Configuration is validated against configuration

`CHECK_TIMEOUT_SECONDS` must be below `MIN_INTERVAL_SECONDS`, or checks can overlap. It is
compared against that floor and never against the smallest `interval_seconds` in the
database — that would put a query in the startup path and make a startup check depend on
data that changes whenever someone creates a target.

### Verified before shipping

```
38 tests pass against a real Postgres
live stack:  worker checks a seeded target, records success, serves metrics on :9101
             /api/status → {"up":1,"down":0,"unknown":0,"disabled":0}
broken target added → target_up{broken} 0, failure counter increments,
             worker stays healthy
SIGTERM      exit 0, pool closed
```

- [Prometheus alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)
- [LATERAL joins](https://www.postgresql.org/docs/current/queries-table-expressions.html#QUERIES-LATERAL)
