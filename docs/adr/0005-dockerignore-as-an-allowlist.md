# 0005. `.dockerignore` as an allowlist

Date: 2026-08-17
Status: Accepted

## Context

There was no `.dockerignore`. The build context was the whole directory:

```
75M  .          of which:
75M  .venv
212K .git
20K  .pytest_cache
```

Effectively the entire context was the virtual environment. It is sent to the builder on
every build — over the wire in CI, through QEMU for the foreign architecture — despite the
Dockerfile copying only `requirements.txt` and `app/`.

## Decision

```
*
!requirements.txt
!app

app/**/__pycache__
```

Deny everything, then permit exactly what the Dockerfile copies. Not a denylist of known
offenders.

## Consequences

- Build context drops from 75 MB to roughly 50 KB.
- `.env` cannot end up in an image layer, even if someone later writes `COPY . .`. This
  is the main reason for the allowlist form and it is a security property, not a
  performance one.
- Adding a file the build needs now requires editing `.dockerignore` too. The build fails
  loudly when this is forgotten, which is the desired trade: a clear error beats silent
  bloat or a silent leak.
- `!app` re-includes the directory wholesale, so build artefacts inside it have to be
  excluded again explicitly — hence the `__pycache__` line.

## Background

The build context is the directory sent to the daemon *before* any instruction runs.
Files excluded by `.dockerignore` never reach the builder at all, so they cannot be
copied, cannot appear in a layer, and cannot be recovered from the image later. This is
different from deleting a file in a later `RUN`: that leaves it in the earlier layer,
where `docker history` and any registry client can still read it.

The syntax is Go's `filepath.Match` plus `!` for re-inclusion. Order matters: later
patterns override earlier ones, so `*` first and exceptions after. A subtlety worth
knowing is that re-including a path inside an excluded *directory* does not work —
Docker never descends into a directory it has excluded. Excluding `app` and then writing
`!app/main.py` would yield nothing. That is why the pattern here excludes at the top
level and re-includes whole directories.

The denylist alternative fails open. Every new secret file — a token, a kubeconfig, a
backup dump — is included by default until someone remembers to add it. The allowlist
fails closed.

- [.dockerignore reference](https://docs.docker.com/build/concepts/context/#dockerignore-files)
