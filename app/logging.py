"""Structured logging: one JSON object per line, to stdout.

The container runtime owns log collection, so there are no files and no rotation.

Uvicorn's own access log is switched off at the command line and replaced by a middleware
in :mod:`app.main`. Routing it through a JSON formatter instead would produce JSON
wrapping a preformatted string — structurally valid and semantically useless, because the
method, path, status and duration stay trapped inside one text field.
"""

import json
import logging
import logging.config
import sys
from datetime import UTC, datetime

# Attributes every LogRecord carries. Anything outside this set was passed by the caller
# through `extra=` and is promoted to a top-level field.
_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "taskName", "thread", "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": JsonFormatter}},
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": sys.stdout,
                }
            },
            "root": {"handlers": ["stdout"], "level": level},
            "loggers": {
                # Uvicorn installs its own handlers on import. Claim them, or stdout ends
                # up half JSON and half plain text, which no parser can read.
                "uvicorn": {"handlers": ["stdout"], "level": level, "propagate": False},
                "uvicorn.error": {"handlers": ["stdout"], "level": level, "propagate": False},
                # Silenced rather than reformatted; the middleware emits access records.
                "uvicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False},
            },
        }
    )
