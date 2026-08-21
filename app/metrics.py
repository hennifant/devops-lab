"""Application metrics, shared by the API and (from PR 2) the worker.

Names are fixed by docs/app-requirements.md, because Prometheus rules and Grafana
dashboards are written against them. Changing one silently breaks an alert.

The ``target`` label carries the target *name*, never the URL: a URL with a query string
turns a bounded label into an unbounded one, which is how Prometheus dies. MAX_TARGETS
caps the other side of the same problem.
"""

from prometheus_client import Counter, Gauge, Histogram

TARGETS_TOTAL = Gauge(
    "devops_lab_targets_total",
    "Number of targets currently configured.",
)

DB_QUERY_DURATION = Histogram(
    "devops_lab_db_query_duration_seconds",
    "Time spent executing a named database query.",
    ["query"],
)


CHECKS_TOTAL = Counter(
    "devops_lab_checks_total",
    "Checks performed, by target and outcome.",
    ["target", "result"],
)

CHECK_DURATION = Histogram(
    "devops_lab_check_duration_seconds",
    "Time an individual check took, including connect and read.",
    ["target"],
)

TARGET_UP = Gauge(
    "devops_lab_target_up",
    "Whether the last check of a target succeeded.",
    ["target"],
)

WORKER_LAST_RUN = Gauge(
    "devops_lab_worker_last_run_timestamp_seconds",
    "Unix time at which the worker last completed a tick.",
)

WORKER_RUN_DURATION = Histogram(
    "devops_lab_worker_run_duration_seconds",
    "Time one worker tick took, from selecting targets to writing results.",
)


def forget_targets(names: set[str]) -> None:
    """Drop the target_up series for targets that no longer exist or are disabled.

    Without this a deleted target keeps its last value forever, and a rule such as
    ``target_up == 0 for 10m`` fires against something nobody is checking on purpose.
    That is how people learn to ignore alerts.
    """
    for name in names:
        try:
            TARGET_UP.remove(name)
        except KeyError:
            pass
