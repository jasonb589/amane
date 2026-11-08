"""persist FC2 multipart plans

Revision ID: 2023ee887911
Revises: b214ab0f2c78
Create Date: 2026-11-09 03:47:23.318013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2023ee887911"
down_revision: str | None = "b214ab0f2c78"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fc2_part_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_directory", sa.String(), nullable=False),
        sa.Column("directory_key", sa.String(), nullable=False),
        sa.Column("number", sa.String(), nullable=False),
        sa.Column("members", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("library_id", "directory_key", "number", name="uq_fc2_part_plans_group"),
    )
    op.create_index("ix_fc2_part_plans_library_number", "fc2_part_plans", ["library_id", "number"])


def downgrade() -> None:
    op.drop_table("fc2_part_plans")
