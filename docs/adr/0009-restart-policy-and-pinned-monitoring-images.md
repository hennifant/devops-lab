# 0009. Restart policy, and pinned monitoring images

Date: 2026-08-19
Status: Accepted

## Context

The M2 rebooted on 2026-08-19 at 09:27. Two hours later the deployment was still down:

```
boot:              2026-08-19 09:27
docker daemon:     2026-08-19 09:29
all 10 containers: Exited (0)
```

No service declared a restart policy, so every container had `RestartPolicy=no`. Both
stacks — production and development — stayed down until someone noticed by accident and
ran `docker compose up -d` by hand.

Nothing reported the outage. The alert for it (`up == 0`) lives in Prometheus, and
Prometheus was one of the ten containers that did not come back. A monitoring stack that
shares the failure mode of the thing it monitors cannot report on it.

Separately, three services ran on floating tags:

```yaml
image: prom/prometheus:latest
image: prom/alertmanager:latest
image: grafana/grafana:latest
```

This is the same defect that [0008](0008-renovate-as-an-action.md) fixed for the base
image, left in place for the monitoring stack. A deploy could silently move Prometheus to
a new major version, because `docker compose pull` re-resolves `:latest` every time.

## Decision

Every service gets a restart policy, expressed as a variable so the two stacks can differ:

```yaml
restart: ${RESTART_POLICY:-unless-stopped}
```

The deployment takes the default. The development checkout sets `RESTART_POLICY=no` in its
`.env`, because a dev stack should not silently reclaim its ports on every boot.

The monitoring images are pinned to the versions already running, so nothing moves as a
side effect of this change:

```yaml
prom/prometheus:v3.13.2
prom/alertmanager:v0.34.0
grafana/grafana:13.1.3
```

## Consequences

- The deployment survives a reboot without human intervention.
- Prometheus can no longer change major version behind your back. Renovate proposes bumps
  as reviewable pull requests instead.
- Three more versions to keep current. If Renovate is ever removed, they rot.
- A container stuck in a crash loop now restarts forever instead of staying dead. That is
  usually right, but it can hide a defect: a service that fails, restarts and half-works
  looks healthier in `docker ps` than one that stopped. Alerting has to catch that, not
  the container list.
- `RESTART_POLICY` is one more variable in `.env`. Forgetting it in a new checkout yields
  the production default, which is the safe direction to fail.

## Background

Docker offers four restart policies: `no`, `on-failure[:max-retries]`, `always` and
`unless-stopped`. The relevant distinction is the last two, and it is not about crashes —
both restart a crashed container, and both start it again when the daemon starts.

The difference is what happens to a container **you** stopped:

| | after crash | after daemon restart | after `docker stop` + daemon restart |
| --- | --- | --- | --- |
| `always` | restarts | starts | **starts again** |
| `unless-stopped` | restarts | starts | stays down |

`unless-stopped` records that the stop was deliberate. In a lab where containers get
stopped to inspect something, `always` would resurrect them at the next reboot and quietly
undo the intent. That is why the default here is `unless-stopped`.

A restart policy is enforced by the Docker daemon, which means it is worthless if the
daemon itself does not start at boot. On this host:

```
systemctl is-enabled docker  →  enabled
```

Worth re-checking after any reinstall; the policy fails silently otherwise.

One thing a restart policy does **not** do: it does not wait for dependencies.
`depends_on: condition: service_healthy` only applies to `docker compose up`, not to the
daemon's own restart sequence at boot. After a reboot the API may start before Postgres is
accepting connections, crash, and be restarted until it succeeds. The end state is
correct; the log will show failed attempts. Making that orderly is a separate concern from
this decision.

- [Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/)
