"""Application metrics, shared by the API and (from PR 2) the worker.

Names are fixed by docs/app-requirements.md, because Prometheus rules and Grafana
dashboards are written against them. Changing one silently breaks an alert.

The ``target`` label carries the target *name*, never the URL: a URL with a query string
turns a bounded label into an unbounded one, which is how Prometheus dies. MAX_TARGETS
caps the other side of the same problem.
"""

from prometheus_client import Gauge, Histogram

TARGETS_TOTAL = Gauge(
    "devops_lab_targets_total",
    "Number of targets currently configured.",
)

DB_QUERY_DURATION = Histogram(
    "devops_lab_db_query_duration_seconds",
    "Time spent executing a named database query.",
    ["query"],
)
