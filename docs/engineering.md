# devops-lab

A self-hosted DevOps learning lab. The application is deliberately small; the point is
the lifecycle around it — containerization, CI/CD, observability, alerting, and later
Kubernetes/GitOps. Optimize for *operability and correctness*, not for app features.

Owner: hennifant. Runs on an Apple M2 (arm64) under Asahi Linux.

## Architecture

```
git push
   │
   ▼
GitHub Actions (ci.yml, ubuntu-latest)
   ├── test        pytest
   └── build       buildx → linux/amd64,linux/arm64 → GHCR
                     ghcr.io/hennifant/devops-lab-api:{latest,<sha>}
   │
   ▼  workflow_run: CI completed & success
GitHub Actions (deploy.yml, self-hosted arm64 runner on the M2)
   └── docker compose pull api && docker compose up -d api
```

Runtime stack (Docker Compose, [compose.yaml](compose.yaml)):

```
        ┌──────────┐  /metrics   ┌────────────┐  alert  ┌──────────────┐
        │  api     │────────────▶│ prometheus │────────▶│ alertmanager │
        │ :8000    │             │  :9090     │         │   :9093      │
        └────┬─────┘             └─────┬──────┘         └──────┬───────┘
             │ DATABASE_URL            │ datasource            │ (no receiver yet)
             ▼                         ▼                       ▼
        ┌──────────┐             ┌────────────┐            (planned: Gotify)
        │ postgres │             │  grafana   │
        │   18     │             │   :3000    │
        └──────────┘             └────────────┘
```

## Layout

| Path | Purpose |
| --- | --- |
| [app/main.py](app/main.py) | FastAPI app: `/`, `/health`, `/metrics` |
| [tests/](tests/) | pytest suite against the app via `TestClient` |
| [Dockerfile](Dockerfile) | python:3.14-slim, non-root `appuser`, HEALTHCHECK |
| [compose.yaml](compose.yaml) | api, db, prometheus, alertmanager, grafana |
| [monitoring/](monitoring/) | Prometheus config + rules, Alertmanager config, Grafana provisioning |
| [.github/workflows/ci.yml](.github/workflows/ci.yml) | test → build → push to GHCR |
| [.github/workflows/deploy.yml](.github/workflows/deploy.yml) | deploy on the self-hosted runner |
| [.github/workflows/runner-test.yml](.github/workflows/runner-test.yml) | manual runner smoke test (`workflow_dispatch`) |

## Commands

```bash
# tests (needs .venv active or use .venv/bin/python)
.venv/bin/python -m pytest

# local stack
docker compose up -d
docker compose ps
docker compose logs -f api

# build locally for this machine's arch
docker build -t devops-lab-api:dev .
```

## Critical operational rules

### Compose project identity

There are **two checkouts of this repo on the M2** that both run `docker compose`:

- `/home/hennifant/Development/devops-lab` — the dev checkout (manual `docker compose up`)
- `/home/hennifant/Development/actions-runner/_work/devops-lab/devops-lab` — the runner checkout (deploy workflow)

Both resolve to the Compose project name `devops-lab` and both use explicit
`container_name:` values. They therefore **share and overwrite each other's containers**
while disagreeing about config, env, and bind-mount paths. This has already produced a
live container with `DATABASE_URL=postgresql://:@db:5432/` because the runner checkout has
no `.env`.

Rules until this is properly fixed (Phase 1):

- Never assume a running container came from the checkout you are looking at. Verify:
  `docker inspect <name> --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'`
- The deploy workflow may only touch `api`. Never `docker compose up -d` without a service
  name from the runner — the runner checkout has no `monitoring/`, and Docker would create
  empty directories where the bind mounts point.
- Any change to service names, `container_name`, volumes, or project naming must account
  for both checkouts.

### Secrets

- `.env` holds `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`. Gitignored. Never commit it.
- [.env.example](.env.example) documents the required keys and is committed.
- The Alertmanager notification secret (webhook URL / app token) belongs in its own
  gitignored env file, not in `monitoring/alertmanager/alertmanager.yml`.
- Never print secret values into logs, workflow output, or chat.

### Security constraints

- **Prometheus (9090), Alertmanager (9093) and Grafana (3000) are currently bound to all
  interfaces with no authentication** on Prometheus/Alertmanager. Anyone on the LAN can read
  metrics and silence alerts. Phase 1 binds them to `127.0.0.1`.
- **Never move a `pull_request`-triggered job onto the self-hosted runner.** Fork PRs would
  execute arbitrary code on the M2 with Docker socket access. The `test` job stays on
  `ubuntu-latest`.
- The runner is trusted infrastructure. Treat anything it executes as running on the host.

### Images and reproducibility

- Deploys always use the commit SHA tag, never `:latest`. `IMAGE_TAG` in Compose defaults to
  `latest` only for local convenience — do not rely on it for anything deployed.
- The goal is: *given a commit SHA, the image is reproducible*. Until dependencies are pinned
  (Phase 1) this guarantee does not actually hold. Do not describe the setup as reproducible
  before then.

## Conventions

- **Commits**: short. Imperative mood, sentence case, no scope prefix, no trailing period —
  matching history (`Add Prometheus metrics`, `Separate CI and deployment workflows`).
  Subject only; add a body just when the "why" is genuinely not obvious.
  **No `Co-Authored-By`, no AI attribution, no tool footers.**
- **Branches**: `feature/<topic>`, merged into `main` via pull request. No direct pushes to `main`.
- **Language**: English for code, comments, commits, and docs.
- Prefer changes that are visible in `git` over click-ops. A Grafana dashboard built in the
  UI and not exported to `monitoring/grafana/provisioning/` does not count as done.

## Current state

Working end to end: push → test → multi-arch build → GHCR → self-hosted deploy with SHA tag,
Prometheus scraping the API, Grafana with a provisioned Prometheus datasource.

Known gaps, in priority order — this is the Phase 1 backlog:

1. Compose split-brain between the two checkouts (see above); `DATABASE_URL` is empty in the
   deployed container as a direct consequence.
2. `monitoring/` is untracked and therefore absent from the runner checkout.
3. Alertmanager has a receiver with no destination — alerts fire into nothing.
4. Prometheus has no *named* volume. It falls back to an anonymous volume from the image's
   `VOLUME /prometheus`, so history survives a restart but is untracked, is orphaned by
   `docker compose down`, and cannot be backed up alongside `postgres-data`/`grafana-data`.
5. Dependencies in [requirements.txt](requirements.txt) are unpinned.
6. No `.dockerignore`; the 75 MB `.venv` is sent as build context on every build.
7. No `concurrency:` group — two rapid pushes race on the same runner.
8. Deploy does not wait for health, run a smoke test, or roll back on failure.
9. Monitoring ports exposed on all interfaces; Grafana admin credentials at default.
10. The database is provisioned but unused — `psycopg` is installed and never imported.

## Roadmap

**Phase 1 — harden the foundation (current).** Work through the backlog above. Nothing new
gets built on a base that has ten known holes; every one of them gets worse, not better,
once Kubernetes is in the picture.

**Phase 2 — make the app real.** Actually use Postgres: a schema, migrations, endpoints that
read and write. Add meaningful application metrics. Without real queries and real failure
modes there is nothing for monitoring to show and nothing for alerting to catch.

**Phase 3 — close the alerting loop.** Self-hosted Gotify plus the Alertmanager→Gotify webhook
bridge, so alerts reach a phone. Add node-exporter and cAdvisor so host and container metrics
exist, enabling alerts beyond `up == 0`.

**Phase 4 — quality gates.** ruff in CI, Trivy image scanning, tests running on arm64 as well
as amd64 (the deploy target is arm64 but tests currently only run on amd64).

**Phase 5 — backup and recovery.** Postgres backup, then a *practiced* restore. Deliberately
destroy the stack and bring it back. Document a runbook.

**Phase 6 — Kubernetes.** k3s, then Deployment/Service/Ingress, then Helm.

**Phase 7 — GitOps.** Flux reconciling the cluster from git. No manual `kubectl apply`.

Deliberately out of scope for now: an internal developer platform, Vault, Velero, MinIO,
Loki, multi-repo GitOps. These are worth doing eventually, but breadth before depth is how
a lab ends up with twelve half-configured tools and nothing that can actually be operated.
