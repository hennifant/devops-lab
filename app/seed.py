"""Deterministic sample targets, so staging and smoke tests have data without clicking.

    python -m app.seed

Reads ``SEED_TARGETS`` as ``name=url,name=url`` and upserts on the name, so running it
twice changes nothing. It is a no-op and exits 0 when the variable is empty.

That gate matters in production. An unconditional seed would make the deployment an owner
of application data: a target deleted through the API would silently reappear on the next
deploy. So ``SEED_TARGETS`` is set for staging and left empty for production, where
targets are created through the API and are expected to persist.
"""

import asyncio
import logging
import sys
from urllib.parse import urlparse

from app import db
from app.config import ConfigError, get_settings
from app.logging import configure_logging

# Named explicitly: run as `python -m app.seed` the module's __name__ is "__main__", and
# a log line whose logger field says "__main__" cannot be filtered on.
logger = logging.getLogger("app.seed")


def parse_seed_targets(raw: str) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, separator, url = entry.partition("=")
        name, url = name.strip(), url.strip()
        if not separator or not name or not url:
            raise ConfigError(f"SEED_TARGETS entry {entry!r} is not name=url")
        if urlparse(url).scheme not in {"http", "https"}:
            raise ConfigError(f"SEED_TARGETS entry {entry!r} has a non-http(s) url")
        targets.append((name, url))
    return targets


async def seed() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    targets = parse_seed_targets(settings.seed_targets)
    if not targets:
        logger.info("SEED_TARGETS is empty, nothing to seed")
        return 0

    await db.open_pool(settings)
    try:
        async with db.query("seed_targets") as conn:
            for name, url in targets:
                await conn.execute(
                    """
                    INSERT INTO targets (name, url)
                         VALUES (%s, %s)
                    ON CONFLICT (name) DO UPDATE
                            SET url = EXCLUDED.url
                    """,
                    (name, url),
                )
        logger.info("seeded targets", extra={"count": len(targets)})
    finally:
        await db.close_pool()
    return len(targets)


def main() -> None:
    try:
        asyncio.run(seed())
    except ConfigError as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
