# 0014. Alerting reaches a phone, through Gotify

Date: 2026-08-21
Status: Accepted

## Context

Alertmanager has been running since the monitoring stack was added, with a receiver that
had no destination:

```yaml
receivers:
  - name: devops-lab
```

Every alert since then has fired into nothing. The one rule that exists, `up == 0`, has
never told anyone anything.

The cost of that became concrete on 2026-08-19: the M2 rebooted at 09:27 and the entire
deployment stayed down for two hours. Nobody noticed. The alert for exactly that case
lives in Prometheus, and Prometheus was one of the containers that did not come back —
but even had it survived, there was nowhere for the alert to go.

This also blocks the next piece of work. The worker in PR 2 brings rules worth acting on —
a stalled worker, a target down, checks getting slow — and writing them against a receiver
that discards them would be theatre.

## Decision

Self-hosted Gotify, reached through a bridge, delivering to an Android device.

```
Prometheus → Alertmanager → gotify-bridge → Gotify → phone
```

Alertmanager has no Gotify receiver and its webhook payload does not match Gotify's message
API, so `druggeri/alertmanager_gotify_bridge` translates between them. Two containers,
both pinned, both in production only.

```yaml
receivers:
  - name: devops-lab
    webhook_configs:
      - url: http://gotify-bridge:8080/gotify_webhook
        send_resolved: true
```

### Gotify is bound to the LAN, everything else stays on loopback

This is the first exception to the rule in `engineering.md`, and it is narrow.

Prometheus and Alertmanager have **no authentication whatsoever** — exposing them lets
anyone on the network read every metric and silence alerts through the API. Gotify is a
different kind of service: user login, per-application tokens, built to be reached. The
rule was never "bind everything to loopback", it was "do not expose services that cannot
defend themselves".

It is still a web interface on a home network, so `GOTIFY_ADMIN_PASSWORD` comes from
repository secrets like every other credential.

This exception is **temporary**. A Cloudflare Tunnel is planned, which would reach Gotify
and Grafana without any LAN binding. When it exists, this port moves back behind
`127.0.0.1`, and that is a removal rather than a new decision.

### Staging is left out

Staging has no monitoring stack, deliberately ([0011](0011-build-once-deploy-many.md)).
Running the chain there would mean a second Prometheus and Alertmanager on the same host.

The thing that would buy — testing the delivery path before production — is available for
the cost of one command against the production Alertmanager:

```bash
curl -s -XPOST localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[{
  "labels": {"alertname":"TestFromShell","severity":"none"},
  "annotations": {"summary":"testing the delivery chain"}}]'
```

Prometheus *rules* remain untestable in staging, but they are a file that `promtool` can
check and that the deploy rolls out through `MONITORING_CONFIG_HASH`.

## Consequences

- Alerts reach a phone. The failure mode of 2026-08-19 becomes visible — with the
  exception noted below.
- Two more containers and a named `gotify-data` volume, which holds the applications and
  their tokens. Losing it means re-issuing the token and redeploying.
- One service is now reachable from the LAN. It has authentication; it is still surface.
- Two secrets to manage: `GOTIFY_ADMIN_PASSWORD` and `GOTIFY_TOKEN`.
- **The first deploy cannot be complete.** The application token only exists once Gotify
  runs, so `GOTIFY_TOKEN` is empty on the first pass. The bridge does not start at all —
  it exits with `The token for Gotify API must be set` and, under
  `restart: unless-stopped`, crash-loops until the token arrives. Louder than running
  idle, and visible in `docker compose ps`. Create the application in Gotify, store the token as a secret, deploy again.
  The alternative — placing the token on the host by hand — is precisely what
  [0003](0003-deployment-env-from-repository-secrets.md) removed.
- **This still cannot report a total outage of the M2.** Everything in the chain runs on
  the machine being monitored. If it is down, so is the thing that would tell you. See
  below.

## Background

### Verified before shipping

The chain was assembled in a throwaway Compose project and exercised end to end:

```
alert POSTed to Alertmanager  →  message in Gotify
  title:  proving the delivery chain      (from annotations.summary)
  body:   if this reaches Gotify the wiring works
  priority: 5
```

The bridge logs `a user-defined template discovery has an error … Falling back to default
alerting` on startup. That is expected — no custom templates are mounted — and not a fault.

Confirmed again on the real deployment, 2026-08-21: after the second deploy supplied the
token, the bridge left its restart loop and `Alerting pipeline is wired up` arrived in
Gotify.

**Not verified:** delivery of the *resolved* notification. `send_resolved: true` is
configured, but no resolved message arrived within a 45-second window after the alert was
ended. The likely cause is timing — `group_wait: 10s` plus `group_interval: 30s` plus
resolution processing — rather than configuration, but this is stated as unproven rather
than assumed. Confirm it against the first real alert that recovers.

### Compose interpolates the whole file, not just the named services

*Learned on the first deploy, 2026-08-21.*

The staging job runs `docker compose up -d api db migrate`. It still failed:

```
error while interpolating services.gotify.environment.GOTIFY_DEFAULTUSER_PASS:
required variable GOTIFY_ADMIN_PASSWORD is missing a value
```

Naming services selects what to *start*. Compose parses and interpolates the entire file
first, so `${GOTIFY_ADMIN_PASSWORD:?}` in a service that will never run is still resolved —
and still fails when unset.

The tempting fix is to drop the `:?` guard. That would be wrong: the variable would
silently become an empty string, which is the exact failure this repository already shipped
once as `DATABASE_URL=postgresql://:@db:5432/`
([0003](0003-deployment-env-from-repository-secrets.md)).

Instead staging writes a literal placeholder. It never starts Gotify and has no business
holding production's password, so the value is `unused-in-staging` rather than the secret.

### The heartbeat rule is temporary

```yaml
- alert: AlertingPipelineHeartbeat
  expr: vector(1)
```

Always true, so it fires permanently and warns about nothing. Its only job was to prove the
chain delivers once. It did, and it has been removed — a permanently firing alert is noise,
and noise is how people learn to ignore alerts.

### What this does not solve: the watchdog

An alerting stack that runs on the host it monitors cannot report that host being gone.
The grown-up form of the heartbeat inverts the logic:

```
Prometheus fires a Watchdog alert continuously
        ↓ every few minutes
an external service outside this machine
        ↓ signal stops arriving
that service alerts you
```

A dead man's switch. It is the only construction that reports a total outage, and it needs
a component that is not on the M2 — healthchecks.io on its free tier, or a cron on any
other machine. Prometheus Operator ships exactly this pattern as `Watchdog`.

Deliberately not built here: it is a separate decision with an external dependency, and it
belongs next to the Cloudflare Tunnel work rather than inside this change.

- [Alertmanager webhook receiver](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config)
- [alertmanager_gotify_bridge](https://github.com/DRuggeri/alertmanager_gotify_bridge)
