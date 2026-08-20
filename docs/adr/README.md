# Architecture decision records

One file per decision, numbered, in chronological order. A record is immutable once
accepted: if a decision is revisited, write a new record and mark the old one
`Superseded by NNNN`. The point is to preserve why something was done, including the
reasoning that later turned out to be wrong.

Each record carries a `Background` section beyond the usual format. This repository is a
learning lab, so how the mechanism works is recorded alongside what was decided.

Start from [0000-template.md](0000-template.md).

| # | Decision | Date |
| --- | --- | --- |
| [0001](0001-two-isolated-compose-projects.md) | Two isolated Compose projects | 2026-08-14 |
| [0002](0002-deploy-from-a-stable-directory.md) | Deploy from a stable directory, not the runner workspace | 2026-08-14 |
| [0003](0003-deployment-env-from-repository-secrets.md) | Write the deployment `.env` from repository secrets | 2026-08-14 |
| [0004](0004-dev-builds-the-api-image-locally.md) | The dev stack builds the API image locally | 2026-08-17 |
| [0005](0005-dockerignore-as-an-allowlist.md) | `.dockerignore` as an allowlist | 2026-08-17 |
| [0006](0006-compiled-requirements-with-uv.md) | Compiled requirements with uv | 2026-08-17 |
| [0007](0007-concurrency-groups.md) | Concurrency groups for CI and deploy | 2026-08-17 |
| [0008](0008-renovate-as-an-action.md) | Renovate as a self-hosted Action, and a pinned base image digest | 2026-08-17 |
| [0009](0009-restart-policy-and-pinned-monitoring-images.md) | Restart policy, and pinned monitoring images | 2026-08-19 |
| [0010](0010-public-repository.md) | Public repository, and the exposure that comes with it | 2026-08-20 |
| [0011](0011-build-once-deploy-many.md) | Build once, deploy many | 2026-08-20 |
