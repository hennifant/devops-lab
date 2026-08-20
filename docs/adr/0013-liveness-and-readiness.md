# 0013. Liveness and readiness are separate questions

Date: 2026-08-20
Status: Accepted

## Context

The lab had one health endpoint. `/health` returned `{"status": "healthy"}` unconditionally,
the image `HEALTHCHECK` polled it, and the deployment smoke test polled it too. Every one
of those answers the same question, and it is not the question anyone actually has.

[0011](0011-build-once-deploy-many.md) already recorded the gap:

> It polls `/health` for up to sixty seconds and dumps the last fifty log lines on failure.
> That proves the container starts and serves HTTP. It does not prove the database is
> reachable, because `/health` does not touch it — a container can be "healthy" and answer
> every real request with a database error.

This is not hypothetical here. A container in this lab has already run with
`DATABASE_URL=postgresql://:@db:5432/` ([0003](0003-deployment-env-from-repository-secrets.md))
and reported itself healthy the entire time.

With PR 1 the application has a database, so there is finally something for readiness to be
about.

## Decision

Two endpoints, answering two questions:

| | Question | Touches |
| --- | --- | --- |
| `GET /health` | Should this container be restarted? | nothing |
| `GET /ready` | Can this container serve a real request? | the database, and the applied migration revision |

`/ready` returns 503 with a body naming which check failed, so "Postgres is gone" and "the
schema is behind this build" are distinguishable without reading logs.

**The two healthchecks deliberately differ.** The image `HEALTHCHECK` stays on `/health`;
the Compose healthcheck for `api` uses `/ready`.

The deployment smoke test becomes evidence rather than a liveness poll
([scripts/smoke-test.sh](../../scripts/smoke-test.sh)):

```
1. GET /ready                → 200
2. GET /metrics              → devops_lab_targets_total, as a baseline
3. POST /api/targets         → 201
4. GET  /api/targets/{id}    → 200, the record round-tripped through Postgres
5. GET /metrics              → baseline + 1
6. DELETE /api/targets/{id}  → always, including after a failure
```

The seed runs between the deploy and the smoke test, and **only when `SEED_TARGETS` is
non-empty**. Staging sets it; production leaves it blank.

## Consequences

- A database outage now marks `api` unhealthy in `docker compose ps`. That is the correct
  signal and it will look like a regression to anyone who has not read this record.
- Restarting the container is still governed by `/health`, so a brief database outage does
  not restart an application that has nothing wrong with it.
- `/ready` costs two queries per call, every 30 seconds per healthcheck. It also refreshes
  `devops_lab_targets_total`, so the gauge self-heals if the database was absent at startup.
- Anything that gates on `depends_on: service_healthy` for `api` now waits for the database
  and the migrations, not merely for a bound socket.
- The smoke test writes to the production database on every deploy. It cleans up in a step
  that runs on failure too, and names the target after the workflow run id so a previously
  failed cleanup cannot collide. Without cleanup, production would hit `MAX_TARGETS` after
  fifty deploys.
- Gating the seed on `SEED_TARGETS` keeps the deployment from owning application data. An
  unconditional seed would resurrect any seeded target deleted through the API on the next
  deploy — the deployment and the API would disagree about who owns the row.
- Staging's seed points at the staging stack's own `/health`, so PR 2's worker exercises
  the whole path — worker, HTTP, database, metrics — without adding outbound traffic from a
  host that also runs production.
- A smoke test with six steps has six ways to fail, some of them its own. That is the
  price of it proving something.

## Background

### Why Kubernetes separates these, and why it applies here

The distinction is borrowed from Kubernetes probes, and the reason is the failure mode of
conflating them:

- **livenessProbe** failing → the container is **killed and restarted**.
- **readinessProbe** failing → the pod is **removed from the Service endpoints**, and keeps
  running.

If liveness checks the database, a database outage restarts every replica. They come back,
still cannot reach the database, and restart again — a crash loop caused by a dependency
that was going to recover on its own. Readiness is the one that should react to a
dependency being down, because taking a pod out of rotation is reversible and cheap.

Compose has no Service to be removed from, so `/ready` cannot shift traffic here. It still
earns its place: it is what `depends_on` gates on, what the healthcheck reports, and what
the smoke test asserts. And when this lab reaches Phase 6, the endpoints already exist and
map onto the probes directly.

### Why the image and Compose healthchecks differ

Compose's `healthcheck:` overrides the image's `HEALTHCHECK`, and both are used:

```
docker inspect devops-lab-dev-api-1 → /ready   (Compose)
docker inspect devops-lab-api:dev   → /health  (image)
```

The image is the general artefact: someone running it with `docker run`, without an
orchestrator, wants to know whether the process is wedged. Inside this Compose project
there is a database, other services gate on `api` being healthy, and the useful question is
whether the application can actually serve. Same image, different context, different
question — which is the argument for having both endpoints in the first place.

### Checking that migrations are applied, not just that Postgres answers

`/ready` compares the `version_num` in `alembic_version` with the head revision, read from
the migration scripts through `ScriptDirectory.get_current_head()` rather than a constant
that would have to be updated by hand. Alembic is in the image regardless, because the
`migrate` service runs it ([0012](0012-hand-written-sql-with-alembic.md)).

This is what catches the case where the image is newer than the schema — a deploy where
`migrate` was skipped, or an old container still running after a partial rollout. The
database is perfectly reachable in that state, which is why a `SELECT 1` alone is not
enough.

- [Kubernetes: configure liveness, readiness and startup probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
- [Compose healthcheck](https://docs.docker.com/reference/compose-file/services/#healthcheck)
