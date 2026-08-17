# 0001. Two isolated Compose projects

Date: 2026-08-14
Status: Accepted

*Recorded retroactively on 2026-08-17.*

## Context

Two checkouts of this repository on the same machine both ran `docker compose`:

- `~/Development/devops-lab` — the development checkout
- `~/Development/actions-runner/_work/devops-lab/devops-lab` — the runner workspace

`docker compose ls` reported a single project with two config files:

```
devops-lab  running(5)  /home/hennifant/Development/devops-lab/compose.yaml,
                        /home/hennifant/Development/actions-runner/_work/devops-lab/devops-lab/compose.yaml
```

Container labels confirmed the split: `api` and `db` had been created from the runner
workspace, `prometheus`, `grafana` and `alertmanager` from the development checkout.
They shared one network and one set of volumes while disagreeing about configuration,
environment and bind-mount paths.

The visible symptom was a deployed container running with

```
DATABASE_URL=postgresql://:@db:5432/
```

because the runner workspace has no `.env` — it is gitignored and therefore never in a
checkout. The failure was silent only because the application does not use the database
yet. Further consequences: a plain `docker compose up -d` in the development checkout
would replace the SHA-pinned deployed API container with `:latest`, and a
`docker compose down` in either directory would tear down both halves.

## Decision

Run two separate Compose projects that can never merge.

| Project | Directory | Image source | Host ports |
| --- | --- | --- | --- |
| `devops-lab` | `~/deploy/devops-lab` | GHCR, commit SHA tag | 8000, 9090, 9093, 3000 |
| `devops-lab-dev` | the development checkout | built from the working tree | 18000, 19090, 19093, 13000 |

Mechanically:

- `name: devops-lab` at the top of `compose.yaml`, overridden by `COMPOSE_PROJECT_NAME`
  in the development checkout's `.env`.
- All `container_name:` entries removed.
- Host ports expressed as variables with defaults.

## Consequences

- Both stacks can run simultaneously. Local experiments cannot damage the deployment.
- Containers are now named `devops-lab-api-1` rather than `devops-lab-api`. Any script
  or habit that addressed a container by its old fixed name breaks. `docker compose logs api`
  is the replacement and is better anyway, because it is project-scoped.
- `container_name:` is now effectively forbidden in this repository. Reintroducing it
  anywhere would recreate the collision.
- Two Postgres instances run on the machine. The development one holds throwaway data.
- Slightly more to keep in your head: which project a command is aimed at. `-p` or the
  right working directory now matters.

## Background

When no project name is given, Compose derives one. The precedence, highest first:

1. `-p` / `--project-name` on the command line
2. the `COMPOSE_PROJECT_NAME` environment variable — including from `.env`
3. the top-level `name:` attribute in the Compose file
4. **the basename of the project directory**

Both directories here are called `devops-lab`, so both landed on rule 4 with the same
answer. Nothing was misconfigured; the collision was the documented default behaviour.

The project name prefixes networks (`<project>_default`), volumes
(`<project>_<volume>`) and generated container names (`<project>-<service>-<n>`).

`container_name:` is the exception that made the problem unfixable by renaming alone.
It sets a **global, Docker-wide** container name and bypasses the project prefix
completely. Two projects would still have collided on it. Docker container names are
unique per daemon, not per Compose project.

Services keep reaching each other because Prometheus scrapes `api:8000`, Grafana points
at `http://prometheus:9090` and the API connects to `db:5432` — those are *service*
names, which Compose publishes as network aliases. They are unrelated to container names
and survived the change untouched.

- [Compose project name precedence](https://docs.docker.com/compose/how-tos/project-name/)
- [Compose file `name` top-level element](https://docs.docker.com/reference/compose-file/version-and-name/)
