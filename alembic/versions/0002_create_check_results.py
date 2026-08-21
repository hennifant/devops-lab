"""Create the check_results table.

Revision ID: 0002
Revises: 0001

Deliberately without an index on (target_id, checked_at DESC). The worker's scheduling
query does a LATERAL lookup of each target's most recent result on every tick, which
without that index is a sequential scan over a table that grows by roughly a row per
target per interval. PR 3 adds the index and measures the difference; shipping it here
would leave nothing to measure. See ADR 0015.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE check_results (
            id           bigserial    PRIMARY KEY,
            target_id    int          NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
            checked_at   timestamptz  NOT NULL DEFAULT now(),
            result       text         NOT NULL CHECK (result IN ('success', 'failure', 'timeout')),
            status_code  int          NULL,
            duration_ms  int          NULL,
            error        text         NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE check_results")
