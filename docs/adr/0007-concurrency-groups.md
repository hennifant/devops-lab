# 0007. Concurrency groups for CI and deploy

Date: 2026-08-17
Status: Accepted

## Context

Neither workflow declared a `concurrency:` group. Two pushes in quick succession
therefore produced two CI runs and, on completion, two deploy runs racing each other on
the same self-hosted runner — both calling `docker compose up -d` against the same
project, in an order nobody controls. The stack could end up on the older commit.

Superseded pull request runs also kept executing to completion, spending minutes on
commits that had already been replaced.

## Decision

`ci.yml`:

```yaml
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

`deploy.yml`:

```yaml
concurrency:
  group: deploy
  cancel-in-progress: false
```

## Consequences

- Deploys serialise. Only one can touch the host at a time.
- A running deploy is never interrupted part-way through `docker compose up -d`.
- Pushing twice quickly to a pull request cancels the first run. If you wanted results
  for the intermediate commit, they are gone — rerun it manually.
- Deploys can now *queue*, so the delay between a merge and a live change is no longer
  bounded by the deploy's own duration.
- Intermediate commits are not deployed at all when several land while a deploy is
  running. Correct for a deployment, but it means the deployed SHA can skip commits.

## Background

**Why the deploy group has no `github.ref` in it.** The group name defines the lane. For
CI, one lane per branch is right: runs on different branches are independent. For
deployment there is exactly one target host, so there must be exactly one lane, globally.
Adding `github.ref` would create a lane per branch and reintroduce the race the moment
anything deployed from a second ref.

**What `cancel-in-progress: false` actually does.** It does not queue everything. GitHub
keeps at most **one** pending run per concurrency group. When a run is in progress and a
second arrives, the second waits. When a third arrives, the *pending* one is cancelled and
the third takes its place. The effect is "newest wins", which is what you want for a
deployment: the goal is convergence on the latest commit, not replaying every intermediate
state onto the host.

For CI the opposite is right, hence `cancel-in-progress` keyed to the event. On a pull
request the intermediate results are worthless once a newer commit exists. On `main` the
build job may be halfway through pushing layers and a manifest to GHCR, and interrupting
that is worse than letting it finish.

Note that a cancelled workflow run does not cancel a job already running on a self-hosted
runner instantly — the runner is asked to stop, and a shell step finishes its current
command first. Another reason not to cancel deploys.

- [Using concurrency](https://docs.github.com/en/actions/using-jobs/using-concurrency)
