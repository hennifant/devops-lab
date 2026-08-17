# 0002. Deploy from a stable directory, not the runner workspace

Date: 2026-08-14
Status: Accepted

*Recorded retroactively on 2026-08-17.*

## Context

The deploy workflow ran `docker compose` directly in the self-hosted runner's workspace,
`~/Development/actions-runner/_work/devops-lab/devops-lab`.

Two problems with that:

**The workspace belongs to CI, not to the deployment.** `actions/checkout` runs
`git clean -ffdx` and `git reset --hard` on every job. Anything untracked is deleted —
which is why `.env` was never present there. Reconfiguring or reinstalling the runner
would take the deployment with it.

**Bind mounts do not survive file replacement.** Compose mounts
`./monitoring/prometheus/prometheus.yml` into the Prometheus container. Git replaces a
changed file by unlinking it and creating a new one, which produces a **new inode**. The
running container's mount still points at the old, deleted inode. Prometheus would keep
serving configuration that no longer exists on disk, and Compose would not notice,
because nothing in the Compose specification changed.

## Decision

The deployment lives in `~/deploy/devops-lab`. The workflow checks out into the runner
workspace as before, then synchronises into the deployment directory and runs Compose there:

```bash
rsync -a --delete --exclude '.git' --exclude '.env' ./ "$DEPLOY_DIR/"
cd "$DEPLOY_DIR"
docker compose pull
docker compose up -d --remove-orphans
```

`--exclude .env` protects the generated environment file from being deleted by
`--delete`; see [0003](0003-deployment-env-from-repository-secrets.md).

## Consequences

- The deployment survives runner reinstallation, and its path does not depend on runner
  internals.
- It can be inspected and debugged by hand without navigating into `_work/`.
- One more place where state lives. `~/deploy/devops-lab` is not in git and is not backed
  up; it is reconstructed from the next deploy.
- The directory can drift if someone edits files there directly. `rsync --delete` corrects
  that on the next deploy, which is the intended behaviour but will silently discard
  manual edits.
- A rejected alternative was making the deployment directory its own git clone
  (`git fetch && git checkout <sha>`), which would make `git log` there answer "what is
  deployed". More self-documenting, but more moving parts on the runner. rsync won on
  simplicity; the SHA is recorded in `IMAGE_TAG` instead.

## Background

`actions/checkout` defaults to `clean: true`, which means `git clean -ffdx` — remove
untracked files and directories, including ignored ones — followed by a hard reset. This
is what keeps a persistent self-hosted runner's workspace from accumulating debris between
jobs, and it is exactly what makes the workspace unsuitable as a deployment target.

The inode problem is a general Docker property, not a git one. A bind mount of a *file*
resolves to an inode when the container starts. Replace the file — any editor that writes
atomically does this, as does git — and the container keeps the old inode alive; from
inside, the content never changes. Mounting the *directory* instead avoids it, because
lookups then happen through the directory entry each time. This repository now mounts
`./monitoring/prometheus` rather than the individual file, which is a second layer of
protection on top of moving out of the workspace.

- [actions/checkout inputs](https://github.com/actions/checkout#usage)
- [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)
