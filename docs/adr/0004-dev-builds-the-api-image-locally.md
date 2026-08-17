# 0004. The dev stack builds the API image locally

Date: 2026-08-17
Status: Accepted

## Context

Both stacks pulled the API image from GHCR. Locally that failed:

```
Head "https://ghcr.io/v2/hennifant/devops-lab-api/manifests/latest": unauthorized
```

The package is private, and the local Docker daemon is not logged in. Working around it
by logging in would have fixed the symptom and left the real problem: **a pulled image
does not contain your working tree.** Testing locally against GHCR means testing whatever
CI last built. To see a code change you would have to push and wait for CI first, which
defeats the point of having a local stack.

## Decision

Add a development overlay, `compose.dev.yaml`, that must be passed explicitly:

```yaml
services:
  api:
    image: devops-lab-api:dev
    build: .
    pull_policy: build
```

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d
```

`build:` deliberately does **not** go into `compose.yaml`.

## Consequences

- Local changes are testable immediately, without a registry round trip and without
  logging in to GHCR.
- The development image is tagged `devops-lab-api:dev`, so a local build can never be
  mistaken for a CI artefact in `docker images`.
- Every local `up` rebuilds. Docker's layer cache makes this cheap, but it is not free.
- Two files to type instead of one. A plain `docker compose up -d` in the development
  checkout now fails with an authentication error rather than silently doing the wrong
  thing — an acceptable, and honest, failure mode.
- The pattern generalises: further overlays (staging, a profile with extra tooling) slot
  in the same way.

## Background

The decisive argument is what `build:` in the base file would have done on the deploy
host. Compose's default `pull_policy` is `missing`: pull if the image is absent. With a
`build:` section present and a pull that fails — registry blip, expired token, network —
Compose falls back to **building the image locally instead of erroring**. The deploy
would then run something built ad hoc on the runner rather than the multi-architecture
artefact CI verified and tagged with the commit SHA. No error, no log line, and the
reproducibility guarantee quietly gone.

Keeping `build:` in a file that the deploy never loads makes that impossible rather than
merely unlikely.

`pull_policy: build` states the intent directly: always build, never consult a registry.
It also removes the need to remember `--build`.

Compose merges multiple `-f` files left to right. Later files override scalars and append
to lists, so the overlay replaces `image:` and adds `build:` while everything else in the
service — ports, healthcheck, `depends_on` — carries over unchanged.

- [Merging Compose files](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/)
- [`pull_policy`](https://docs.docker.com/reference/compose-file/services/#pull_policy)
