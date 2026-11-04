"""merge upstream v0.24 head and custom head

Revision ID: b7c8d9e0f1a2
Revises: f6a7b8c9d0e1, 6940e4cf8a80
Create Date: 2026-11-04 18:00:00.000000

上游在 ``v1.1.0`` 之后又接了若干迁移, 最新到 ``6940e4cf8a80`` (重新投影
``media_files.mosaic``); 我们的自定义链在上一次合并点 ``f6a7b8c9d0e1`` 收尾,
于是合完出现两个 head. 这里用 merge 迁移把两条支线并起来, 保持恰好一个 head.

**不做数据改写**: 两条支线自上次合并点起改的是不同的表与列
(上游动 ``media_files.mosaic`` 与演员相关列, 我们这条只加了任务类型等非表结构改动),
互不相干, 没有先后依赖.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = ("f6a7b8c9d0e1", "6940e4cf8a80")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """只合并分支, 无数据改写."""


def downgrade() -> None:
    """只合并分支, 无数据改写."""
