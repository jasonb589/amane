"""合并上游 v0271 与定制刮削组迁移

Revision ID: cc1508d71201
Revises: 0bc5a04e2fb7, e06def65f72a
Create Date: 2026-11-12 19:36:17.862078
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "cc1508d71201"
down_revision: str | Sequence[str] | None = ("0bc5a04e2fb7", "e06def65f72a")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
