# 0011. Build once, deploy many

Date: 2026-08-20
Status: Accepted

## Context

The lab had one deployment target and no gate: a successful CI run on `main` went straight
to the only environment that exists. There was nothing between "tests passed" and "it is
live".

The obvious model, and the one the author knew from a previous employer, is a branch per
stage — `dev`, `stg`, `prd`, each merge promoting a change onward. Three stages is not the
problem; that part is still standard. Using long-lived branches as the promotion mechanism
is, for two reasons.

**It rebuilds the artefact per stage.** Merging into `stg` and then into `prd` triggers a
build in each. What was tested in staging is not what runs in production — different
commit, different build time, and without lock files potentially different dependencies.
That contradicts the guarantee assembled in [0006](0006-compiled-requirements-with-uv.md)
and [0008](0008-renovate-as-an-action.md): given a commit SHA, the image is determined.

**It multiplies merges.** The same change has to be merged three times, branches drift, and
hotfixes need cherry-picks in three directions.

In fairness, many teams running three branches also built only once and swapped
configuration. That is fine. The antipattern is precisely *rebuild per stage* — but
branches invite it, so both tend to disappear together.

## Decision

One artefact, promoted through environments. `deploy.yml` becomes two jobs:

```
CI on main → image:<sha> → staging (automatic) → smoke test → [approval] → production
```

Both jobs deploy `IMAGE_TAG: ${{ github.event.workflow_run.head_sha }}` — the same value.
Nothing is rebuilt between stages. Configuration differs; the bytes do not.

Environments rather than branches:

| Environment | Compose project | Directory | Services | Gate |
| --- | --- | --- | --- | --- |
| `staging` | `devops-lab-stg` | `~/deploy/devops-lab-stg` | `api`, `db` | none, automatic |
| `production` | `devops-lab` | `~/deploy/devops-lab` | all five | required reviewer |

Staging runs only the application under test. The monitoring stack is exercised by
production; a second copy would double the container count on a single host for no
additional signal.

## Consequences

- What is approved for production is byte-identical to what staging ran.
- A human decision now sits between `main` and production. GitHub pauses the workflow
  before the job starts; the runner is not occupied while waiting.
- Deployments are visible per environment in GitHub, including which SHA is live where.
- Both environments run on the same machine. This validates the **pipeline**, not the
  **infrastructure** — see the limitation below.
- Two more containers permanently running on the M2.
- Production deploys are no longer automatic. Nothing reaches users without someone
  clicking, which is the point and also a new way to forget.
- Staging and production currently share the repository-level `POSTGRES_PASSWORD`.
  Per-environment secrets are the obvious refinement and are not done yet.

### The limitation, stated plainly

Staging lives on the same host as production: same kernel, same disk, same power, same
network. It can find whether the image starts, whether configuration resolves, whether the
smoke test passes. It cannot find what staging environments exist to find — behaviour under
a different kernel, different network topology, or real load. Anything that takes down the
M2 takes down both.

This is not fixed by a better pipeline. It is fixed by a second machine, and it is recorded
here so nobody later mistakes this staging for the real thing.

## Background

### Environments are deployment targets, not branches

GitHub's `environment:` key attaches a job to a named target. What that buys:

- **Protection rules** — required reviewers, wait timers, and which branches may deploy.
  `production` here requires a reviewer, so the job is held before it starts.
- **Scoped secrets and variables** that override repository-level ones.
- **Deployment history** — the UI tracks which SHA is live in each environment.

None of it requires a branch. The branch is the source of truth for *code*; the environment
is the target for an *artefact*. Conflating them is what produces the rebuild problem.

Required reviewers are unavailable on a private repository on the free plan, which is part
of why the repository was made public — see [0010](0010-public-repository.md).

### Why the gate is on production and not on staging

A gate before staging would mean a human clicks to find out whether the thing works, which
inverts the purpose. Staging exists to produce the evidence that the production gate is
decided on. Automatic into staging, deliberate into production.

### The smoke test is currently thin

It polls `/health` for up to sixty seconds and dumps the last fifty log lines on failure.
That proves the container starts and serves HTTP. It does not prove the database is
reachable, because `/health` does not touch it — a container can be "healthy" and answer
every real request with a database error.

Making it meaningful requires the application to grow a `/ready` endpoint that checks the
database and that migrations have run, plus a write-and-read round trip. That is Phase 2
work; the pipeline shape does not depend on it, which is why this is being built first.

### What makes this portable to real hosts

The per-environment differences are confined to job-level `env`: `DEPLOY_DIR`,
`COMPOSE_PROJECT`, `API_PORT`, and which services to start. Moving production onto a
separate machine means changing `runs-on` to a differently labelled runner and pointing
`DEPLOY_DIR` at a path on that machine. The promotion logic does not change.

- [Using environments for deployment](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
- [Deployment protection rules](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments#deployment-protection-rules)
