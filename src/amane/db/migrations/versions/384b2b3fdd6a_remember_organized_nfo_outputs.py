"""remember organized nfo outputs

Revision ID: 384b2b3fdd6a
Revises: b7c8d9e0f1a2
Create Date: 2026-11-07 16:39:36.178191
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "384b2b3fdd6a"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nfo_outputs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("metadata_id", sa.Integer(), sa.ForeignKey("metadata.id"), nullable=False),
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("libraries.id"), nullable=False),
        sa.Column("video_path", sa.String(), nullable=False),
        sa.Column("nfo_path", sa.String(), nullable=False),
        sa.Column("path_key", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_nfo_outputs_metadata_id", "nfo_outputs", ["metadata_id"])
    op.create_index("ix_nfo_outputs_library_id", "nfo_outputs", ["library_id"])
    op.create_index("ix_nfo_outputs_path_key", "nfo_outputs", ["path_key"], unique=True)


def downgrade() -> None:
    op.drop_table("nfo_outputs")
