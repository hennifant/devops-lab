"""Configuration, read from the environment and nowhere else.

No configuration file ships in the image. Every setting has a usable default except the
database URL, which must fail loudly rather than default to something empty: this lab has
already run a container with ``DATABASE_URL=postgresql://:@db:5432/``, and it reported
itself healthy the entire time. See ADR 0003.

Loading is lazy on purpose. Raising at import time would make the module unimportable in
any context without a database — including the tests that deliberately have none. The
lifespan handler calls :func:`get_settings` at startup instead, so a missing variable
still stops the process before it serves a single request.
"""

import os
from dataclasses import dataclass
from functools import lru_cache


class ConfigError(RuntimeError):
    """A required setting is missing or unusable."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. It carries the database credentials and has no default; "
            f"an empty value would produce a URL that connects to nothing."
        )
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if value < 1:
        raise ConfigError(f"{name} must be at least 1, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    log_level: str
    max_targets: int
    min_interval_seconds: int
    check_interval_seconds: int
    check_timeout_seconds: int
    check_concurrency: int
    worker_metrics_port: int
    db_pool_min: int
    db_pool_max: int
    seed_targets: str

    @classmethod
    def load(cls) -> "Settings":
        settings = cls(
            database_url=_required("DATABASE_URL"),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            max_targets=_positive_int("MAX_TARGETS", 50),
            min_interval_seconds=_positive_int("MIN_INTERVAL_SECONDS", 30),
            check_interval_seconds=_positive_int("CHECK_INTERVAL_SECONDS", 10),
            check_timeout_seconds=_positive_int("CHECK_TIMEOUT_SECONDS", 10),
            check_concurrency=_positive_int("CHECK_CONCURRENCY", 5),
            worker_metrics_port=_positive_int("WORKER_METRICS_PORT", 9101),
            db_pool_min=_positive_int("DB_POOL_MIN", 1),
            db_pool_max=_positive_int("DB_POOL_MAX", 5),
            seed_targets=os.environ.get("SEED_TARGETS", "").strip(),
        )
        # Compared against the configured floor, never against the smallest interval in
        # the database: that would put a query in the startup path and make the check
        # depend on data that changes whenever someone creates a target.
        if settings.check_timeout_seconds >= settings.min_interval_seconds:
            raise ConfigError(
                f"CHECK_TIMEOUT_SECONDS ({settings.check_timeout_seconds}) must be below "
                f"MIN_INTERVAL_SECONDS ({settings.min_interval_seconds}), or checks overlap"
            )
        if settings.db_pool_max < settings.db_pool_min:
            raise ConfigError(
                f"DB_POOL_MAX ({settings.db_pool_max}) is below "
                f"DB_POOL_MIN ({settings.db_pool_min})"
            )
        return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.load()
