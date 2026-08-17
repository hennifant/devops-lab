# 0003. Write the deployment `.env` from repository secrets

Date: 2026-08-14
Status: Accepted

*Recorded retroactively on 2026-08-17.*

## Context

`compose.yaml` reads `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` from `.env`.
That file is gitignored, so it is never in a checkout, so the deployment never had it.
Compose substituted empty strings without failing, and the deployed container ran with

```
DATABASE_URL=postgresql://:@db:5432/
```

Placing the file on the runner by hand does not work: `actions/checkout` deletes untracked
files on every run (see [0002](0002-deploy-from-a-stable-directory.md)).

The database survived only because its volume had been initialised earlier with real
credentials, and Postgres ignores `POSTGRES_*` on an already-initialised data directory.
A single `docker compose down -v` would have left the stack unable to start: an empty
`POSTGRES_PASSWORD` aborts initialisation.

## Decision

The deploy workflow generates the deployment `.env` on every run, from GitHub repository
secrets:

```bash
umask 077
hash=$(find monitoring -type f -exec sha256sum {} + | sort | sha256sum | cut -c1-12)
cat > "$DEPLOY_DIR/.env" <<EOF
IMAGE_TAG=${{ github.event.workflow_run.head_sha }}
MONITORING_CONFIG_HASH=$hash
POSTGRES_USER=${{ secrets.POSTGRES_USER }}
POSTGRES_PASSWORD=${{ secrets.POSTGRES_PASSWORD }}
POSTGRES_DB=${{ secrets.POSTGRES_DB }}
EOF
```

Required repository secrets: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`.

## Consequences

- The deployed configuration is reproducible from the repository plus its secrets. No
  manual step on the host.
- `umask 077` gives the file mode `0600`. It is still plaintext on disk, readable by the
  runner user — which is also the user the runner executes arbitrary workflow code as.
  This is adequate for a lab, not for anything holding real data. A secret manager is the
  eventual answer.
- Rotating the database password now means changing the secret *and* the existing volume;
  `POSTGRES_PASSWORD` only takes effect on first initialisation, never afterwards.
- Anyone who can push to `main` can print the secrets. That is inherent to putting
  credentials in repository secrets and is why the runner counts as trusted infrastructure.

## Background

Compose substitutes `${VAR}` from, in order: the shell environment, then the `.env` file
in the project directory. An unset variable becomes an **empty string** with only a
warning — it is not an error. That is why the broken `DATABASE_URL` was well-formed
enough to go unnoticed. `${VAR:?error}` would make Compose fail instead, which is worth
considering for values that must never be empty.

`MONITORING_CONFIG_HASH` is not read by any program. It exists because Compose decides
whether to recreate a container by comparing the *service definition* — image, environment,
mounts, labels — and never hashes the contents behind a bind mount. A change to
`monitoring/prometheus/prometheus.yml` alone would therefore leave the container running
with the old configuration. Feeding a checksum of `monitoring/` in as an environment
variable makes the service definition change whenever the configuration does, so Compose
recreates exactly those containers and only then.

Password characters matter here: the value is interpolated into
`postgresql://user:pass@db:5432/db`. A `/`, `@`, `:` or `#` in the password breaks URL
parsing. Generate alphanumerics only, or URL-encode.

- [Compose environment variable interpolation](https://docs.docker.com/reference/compose-file/interpolation/)
- [GitHub encrypted secrets](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions)
