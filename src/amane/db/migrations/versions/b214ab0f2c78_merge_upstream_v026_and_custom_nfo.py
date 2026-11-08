"""合并上游 0.26 与自定义 NFO 迁移

Revision ID: b214ab0f2c78
Revises: 384b2b3fdd6a, fb4fd486708c
Create Date: 2026-11-08 07:38:20.701162
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "b214ab0f2c78"
down_revision: str | Sequence[str] | None = ("384b2b3fdd6a", "fb4fd486708c")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """统一迁移末端, 保留两条分支的数据与结构."""


def downgrade() -> None:
    """只恢复分支标记, 不修改数据."""
