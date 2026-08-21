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
   │  build once, deploy many — both stages get the SAME image:<sha>
   ├── staging      automatic    api + db + migrate   → seed → smoke test
   │                             ~/deploy/devops-lab-stg
   └── production   REQUIRES APPROVAL   full stack    → smoke test (no seed)
                                 ~/deploy/devops-lab
```

Neither stage deploys from the runner workspace; both rsync into their own directory and
write `.env` from repository secrets. See [ADR 0011](docs/adr/0011-build-once-deploy-many.md).

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
             ▲
             │ alembic upgrade head, exits 0 before api starts
        ┌──────────┐
        │ migrate  │  one-shot, same image as api, restart: "no"
        └──────────┘
```

## Layout

| Path | Purpose |
| --- | --- |
| [app/main.py](app/main.py) | FastAPI app: `/`, `/health`, `/ready`, `/metrics`, `/api/targets`, `/api/status` |
| [app/config.py](app/config.py) | settings from environment variables; missing credentials fail loudly |
| [app/db.py](app/db.py) | async pool, hand-written SQL, readiness checks |
| [app/logging.py](app/logging.py) | structured JSON logging to stdout |
| [app/metrics.py](app/metrics.py) | `devops_lab_*` metric objects, shared with the worker |
| [app/seed.py](app/seed.py) | `python -m app.seed` — idempotent upsert from `SEED_TARGETS` |
| [alembic/](alembic/) | migrations, hand-written DDL, one per pull request |
| [scripts/smoke-test.sh](scripts/smoke-test.sh) | deployment smoke test: readiness, round trip, metric |
| [tests/](tests/) | pytest suite; `db`-marked tests need a live Postgres |
| [docs/app-requirements.md](docs/app-requirements.md) | what the application must be, and why |
| [Dockerfile](Dockerfile) | python:3.14-slim, non-root `appuser`, HEALTHCHECK |
| [compose.yaml](compose.yaml) | api, db, prometheus, alertmanager, grafana |
| [compose.dev.yaml](compose.dev.yaml) | dev overlay: builds `api` locally instead of pulling |
| [README.md](README.md) | human-facing overview with mermaid diagrams |
| [monitoring/](monitoring/) | Prometheus config + rules, Alertmanager config, Grafana provisioning |
| [docs/adr/](docs/adr/) | architecture decision records — read these before changing anything structural |
| [requirements.in](requirements.in) | direct dependencies; `requirements.txt` is compiled from it |
| [.dockerignore](.dockerignore) | allowlist — denies everything, permits what the Dockerfile copies |
| [renovate.json](renovate.json) | dependency and base-image update rules |
| [.github/workflows/ci.yml](.github/workflows/ci.yml) | test → build → push to GHCR |
| [.github/workflows/deploy.yml](.github/workflows/deploy.yml) | deploy on the self-hosted runner |
| [.github/workflows/renovate.yml](.github/workflows/renovate.yml) | self-hosted Renovate, weekly + `workflow_dispatch` |
| [.github/workflows/runner-test.yml](.github/workflows/runner-test.yml) | manual runner smoke test (`workflow_dispatch`) |

## Commands

```bash
# once per clone, or the secret-scanning hook never runs
.venv/bin/pre-commit install

# tests. Without TEST_DATABASE_URL the db-marked tests skip; CI sets REQUIRE_DB=1 so that
# a missing database fails the job instead of silently skipping half the suite.
.venv/bin/python -m pytest
docker run --rm -d --name pgtest -p 55432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=devops_test postgres:18
TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/devops_test \
  .venv/bin/python -m pytest

# migrations. The dev overlay is required, or `run` pulls the image from GHCR.
docker compose -f compose.yaml -f compose.dev.yaml run --rm migrate alembic upgrade head
docker compose -f compose.yaml -f compose.dev.yaml run --rm migrate alembic downgrade -1
docker compose -f compose.yaml -f compose.dev.yaml \
  run --rm -e SEED_TARGETS='api-self=http://api:8000/health' migrate python -m app.seed

# the deployment smoke test, against any running stack
GITHUB_OUTPUT=/dev/null ./scripts/smoke-test.sh http://127.0.0.1:18000 smoke-local

# dev stack — always use the overlay, it builds api from the working tree
docker compose -f compose.yaml -f compose.dev.yaml up -d
docker compose ps
docker compose logs -f api

# dependencies: edit the .in file, then recompile. Never edit a .txt by hand.
# --output-file, never -o: Renovate replays the command from the header and its parser
# rejects the short form with "Option -o not supported (yet)".
uv pip compile --universal requirements.in     --output-file=requirements.txt
uv pip compile --universal requirements-dev.in --output-file=requirements-dev.txt
```

Plain `docker compose up -d` pulls `api` from GHCR instead of building it and fails unless
Docker is logged in to the registry. It is also the wrong thing to test against: the pulled
image contains the last CI build, not the working tree.

## Critical operational rules

### Compose project identity

Two Compose projects run on this machine and must never merge:

| Project | Directory | Image source | Ports |
| --- | --- | --- | --- |
| `devops-lab` | `~/deploy/devops-lab` | GHCR, SHA tag | 8000, 9090, 9093, 3000 |
| `devops-lab-stg` | `~/deploy/devops-lab-stg` | GHCR, same SHA tag | 28000 |
| `devops-lab-dev` | this checkout | built from working tree | 18000, 19090, 19093, 13000 |

They were previously one project by accident: Compose derives the project name from the
directory basename when nothing else is set, and both directories are called `devops-lab`.
The two stacks shared containers, volumes and network while disagreeing about config and
env — which is how a live container ended up with `DATABASE_URL=postgresql://:@db:5432/`.

What keeps them apart, and must not be undone:

- `name: devops-lab` in [compose.yaml](compose.yaml), overridden by `COMPOSE_PROJECT_NAME`
  in the local `.env`. Precedence: `-p` > `COMPOSE_PROJECT_NAME` > `name:` > directory basename.
- **No `container_name:` anywhere.** It is a global, Docker-wide name that bypasses the
  project prefix entirely, so two projects would still collide on it. Compose-generated
  names (`devops-lab-api-1`) are correct.
- Services address each other by *service* name (`api:8000`, `db:5432`, `prometheus:9090`).
  Those are network aliases and are independent of container names.
- Host ports are variables so both stacks can bind at once.
- `restart:` is a variable too. The deployment defaults to `unless-stopped` so it survives
  a reboot; the dev checkout sets `RESTART_POLICY=no`. See [ADR 0009](docs/adr/0009-restart-policy-and-pinned-monitoring-images.md).

The deploy never runs from the runner workspace. `actions/checkout` cleans it on every run;
replacing a bind-mounted file gives it a new inode while the running container keeps the
deleted one, so a config change would silently never take effect.

### Secrets

- `.env` holds `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`. Gitignored. Never commit it.
- [.env.example](.env.example) documents the required keys and is committed.
- The Alertmanager notification secret (webhook URL / app token) belongs in its own
  gitignored env file, not in `monitoring/alertmanager/alertmanager.yml`.
- Never print secret values into logs, workflow output, or chat.

### Security constraints

- Prometheus, Alertmanager and Grafana bind to `127.0.0.1` only. The loopback address is
  hardcoded in [compose.yaml](compose.yaml), not a variable — Prometheus and Alertmanager
  have no authentication whatsoever, so exposing them would let anyone on the LAN read all
  metrics and silence alerts through the Alertmanager API. Only `api` is published on the LAN.
- Grafana still runs on the default `admin`/`admin` credentials. Loopback binding is what
  currently protects it; that is not a substitute for setting a password.
- **The repository is public.** Anything committed is world-readable from the moment it
  lands. See [ADR 0010](docs/adr/0010-public-repository.md).
- **Never move a `pull_request`-triggered job onto the self-hosted runner.** Anyone can open
  a pull request on a public repository; such a job would execute a stranger's code on the
  M2 with Docker socket access. The `test` job stays on `ubuntu-latest`. This is the single
  most important rule in this file.
- **Never use `pull_request_target`.** It runs with the base repository's secrets.
- `main` is protected: pull request required, `test` must pass, admins included, no force
  pushes. A force push needs the protection removed first.
- The runner is trusted infrastructure. Treat anything it executes as running on the host.

### Images and reproducibility

- Deploys always use the commit SHA tag, never `:latest`. `IMAGE_TAG` in Compose defaults to
  `latest` only for local convenience — do not rely on it for anything deployed.
- The goal is: *given a commit SHA, the image is reproducible*. Three things carry that:
  the pinned base image digest in [Dockerfile](Dockerfile), the compiled
  [requirements.txt](requirements.txt), and the SHA image tag. Breaking any one of them
  breaks the guarantee.
- `requirements.txt` and `requirements-dev.txt` are **generated**. Edit the `.in` files and
  recompile with `uv pip compile --universal`. Renovate replays that exact command, so the
  header it writes must not be suppressed.
- The base image digest is maintained by Renovate. Do not bump it by hand.
- Every image is pinned, including the monitoring stack. No `:latest` anywhere that gets
  deployed — `docker compose pull` re-resolves a floating tag on every run.

## Conventions

- **Commits**: short. Imperative mood, sentence case, no scope prefix, no trailing period —
  matching history (`Add Prometheus metrics`, `Separate CI and deployment workflows`).
  Subject only; add a body just when the "why" is genuinely not obvious.
  **No `Co-Authored-By`, no AI attribution, no tool footers.**
- **Branches**: `feature/<topic>`, merged into `main` via pull request. No direct pushes to `main`.
- **Language**: English for code, comments, commits, and docs.
- Prefer changes that are visible in `git` over click-ops. A Grafana dashboard built in the
  UI and not exported to `monitoring/grafana/provisioning/` does not count as done.
- **Secrets never reach a commit.** `pre-commit` runs gitleaks over the staged diff before
  a commit is written; `.env` is gitignored, so it is never staged and never scanned.
  GitHub push protection is the server-side backstop for provider-issued tokens. Neither
  replaces the actual control, which is that credentials live in GitHub Secrets and the
  deploy writes `.env` at run time — see
  [ADR 0003](docs/adr/0003-deployment-env-from-repository-secrets.md).
- **Structural decisions get an ADR** in [docs/adr/](docs/adr/), using
  [the template](docs/adr/0000-template.md). Records are immutable — supersede, never edit.
  The `Background` section is not optional: it is why this repository exists.

## Current state

Working end to end: push → test → multi-arch build → GHCR → self-hosted deploy with SHA tag,
Prometheus scraping the API, Grafana with a provisioned Prometheus datasource.

Done in PR 1 of the application work ([docs/app-requirements.md](docs/app-requirements.md)):
Alembic migrations as their own Compose service, a `targets` table, `/ready` checking the
database *and* the applied revision, configuration from the environment, structured JSON
logs, graceful shutdown, a gated seed, and a smoke test that writes and reads a record
instead of polling `/health`. See [ADR 0012](docs/adr/0012-hand-written-sql-with-alembic.md)
and [ADR 0013](docs/adr/0013-liveness-and-readiness.md).

Done in Phase 1: the two Compose projects are isolated, `monitoring/` is tracked, the deploy
runs from a stable directory with `.env` written from repository secrets, Prometheus has a
named volume, the monitoring ports are on loopback, dependencies and the base image digest
are pinned, the build context is an allowlist, both workflows have concurrency groups, and
Renovate maintains the pins.

Remaining Phase 1 backlog, in priority order:

1. Alertmanager has a receiver with no destination — alerts fire into nothing.
2. The deploy has a smoke test but no rollback: a failed production deploy leaves the
   broken version running.
3. Grafana admin credentials at default.

## Roadmap

**Phase 1 — harden the foundation (current).** Work through the backlog above. Nothing new
gets built on a base that has ten known holes; every one of them gets worse, not better,
once Kubernetes is in the picture.

**Phase 2 — make the app real (in progress).** An uptime checker, specified in
[docs/app-requirements.md](docs/app-requirements.md) and delivered in three pull requests:
PR 1 the operational contract and the database (done), PR 2 the worker and its telemetry,
PR 3 indexes with before-and-after evidence, retention, and a provisioned dashboard.

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
