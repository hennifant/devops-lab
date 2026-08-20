#!/usr/bin/env bash
#
# Deployment smoke test.
#
#   scripts/smoke-test.sh <base-url> <target-name>
#
# Run from the deployment directory, on the host that published the port. Writes the id
# of the target it created to $GITHUB_OUTPUT so the caller can delete it afterwards, in a
# step that runs even when this script fails.
#
# What this proves, and why it is worth a promotion to production: the database answers,
# this build's migrations are applied, a record survives a round trip through Postgres,
# and the application's own metric moved as a result. The previous version polled /health,
# which touches nothing — a container can pass that while every real request fails.
#
# Deliberately not jq: it is not guaranteed on the self-hosted runner, and one awk and one
# sed are a smaller dependency than a package install.
set -euo pipefail

BASE_URL="${1:?usage: smoke-test.sh <base-url> <target-name>}"
TARGET_NAME="${2:?usage: smoke-test.sh <base-url> <target-name>}"

fail() {
  echo "::error::smoke test failed: $*"
  docker compose logs --tail 50 api migrate 2>/dev/null || true
  exit 1
}

metric_value() {
  curl -fsS --max-time 5 "${BASE_URL}/metrics" \
    | awk '/^devops_lab_targets_total /{printf "%.1f", $2; found=1} END{exit !found}'
}

# 1. Readiness. Sixty seconds of budget, as before — but this one waits on the database
#    and the migrations, not merely on the process binding a socket.
ready=""
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 5 "${BASE_URL}/ready" >/dev/null 2>&1; then
    echo "ready after ${attempt} attempt(s)"
    ready="yes"
    break
  fi
  sleep 2
done
if [ -z "$ready" ]; then
  echo "last /ready response:"
  curl -sS --max-time 5 "${BASE_URL}/ready" || true
  echo
  fail "did not become ready within 60s"
fi

# 2. Baseline, read before anything is written.
baseline="$(metric_value)" || fail "devops_lab_targets_total is not exposed"
echo "devops_lab_targets_total baseline: ${baseline}"

# 3. Write.
created="$(curl -fsS --max-time 10 -X POST "${BASE_URL}/api/targets" \
  -H 'content-type: application/json' \
  -d "{\"name\":\"${TARGET_NAME}\",\"url\":\"https://example.com\",\"interval_seconds\":60}")" \
  || fail "POST /api/targets did not return 2xx"

target_id="$(printf '%s' "$created" | sed -n 's/.*"id":[[:space:]]*\([0-9]\{1,\}\).*/\1/p')"
[ -n "$target_id" ] || fail "POST /api/targets returned no id: ${created}"

# Hand the id over immediately, so cleanup can run even if a later assertion fails.
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "target_id=${target_id}" >> "$GITHUB_OUTPUT"
fi
echo "created target ${target_id}"

# 4. Read it back. This is the round trip through Postgres.
fetched="$(curl -fsS --max-time 10 "${BASE_URL}/api/targets/${target_id}")" \
  || fail "GET /api/targets/${target_id} did not return 2xx"
case "$fetched" in
  *"\"${TARGET_NAME}\""*) ;;
  *) fail "the record did not round-trip: ${fetched}" ;;
esac

# 5. The application's own metric moved.
expected="$(awk -v base="$baseline" 'BEGIN{printf "%.1f", base + 1}')"
actual="$(metric_value)" || fail "devops_lab_targets_total disappeared"
[ "$actual" = "$expected" ] \
  || fail "devops_lab_targets_total is ${actual}, expected ${expected}"

echo "smoke test passed: ready, round trip, devops_lab_targets_total ${baseline} → ${actual}"
