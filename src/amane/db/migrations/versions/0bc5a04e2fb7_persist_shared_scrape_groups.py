"""persist shared scrape groups

Revision ID: 0bc5a04e2fb7
Revises: 2023ee887911
Create Date: 2026-11-09 18:41:49.104993
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0bc5a04e2fb7"
down_revision: str | None = "2023ee887911"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scrape_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("status", sa.Enum("QUEUED", "RUNNING", "DONE", "FAILED", name="taskstatus"), nullable=False),
        sa.Column("producer_task_id", sa.Integer(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_scrape_groups_key", "scrape_groups", ["key"])
    op.create_index("ix_scrape_groups_status", "scrape_groups", ["status"])
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("scrape_group_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_tasks_scrape_group_id", "scrape_groups", ["scrape_group_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_tasks_scrape_group_id", ["scrape_group_id"])


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_scrape_group_id")
        batch.drop_constraint("fk_tasks_scrape_group_id", type_="foreignkey")
        batch.drop_column("scrape_group_id")
    op.drop_table("scrape_groups")
