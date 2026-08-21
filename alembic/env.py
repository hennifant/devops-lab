"""Alembic environment.

Two deviations from the generated default, both deliberate:

* The URL comes from ``DATABASE_URL``, not from ``alembic.ini``. The file is committed;
  the credentials are not.
* The scheme is rewritten to ``postgresql+psycopg://``. SQLAlchemy resolves a bare
  ``postgresql://`` to psycopg2, which this image does not install, and the migration
  dies with ``ModuleNotFoundError: No module named 'psycopg2'``.

There is no ``target_metadata``: migrations are hand-written SQL, so ``autogenerate`` has
nothing to compare against and is not used.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Migrations have no default connection string."
        )
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    elif url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
