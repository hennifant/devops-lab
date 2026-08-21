"""Create the targets table

Revision ID: 0001
Revises:
Create Date: 2026-08-20

Hand-written DDL rather than autogenerate: there are no SQLAlchemy models to compare
against, and the SQL that runs against production should be the SQL in the review.

``name`` is UNIQUE, which the specification's schema sketch did not state. Two reasons it
has to be: the seed upserts on it, and it becomes the ``target`` label on every check
metric — duplicate names would collide two targets into one time series.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE targets (
            id                serial       PRIMARY KEY,
            name              text         NOT NULL UNIQUE,
            url               text         NOT NULL,
            interval_seconds  int          NOT NULL DEFAULT 60,
            enabled           bool         NOT NULL DEFAULT true,
            created_at        timestamptz  NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE targets")
