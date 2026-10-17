"""merge upstream v1.1.0 head and custom head

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0, 9054ebacea29
Create Date: 2026-10-17 20:30:00.000000

上游在 ``1fdf4d41aac1`` 之后接了 ``9054ebacea29`` (番号类型从文件移到条目:
``metadata`` 新增 ``content_type`` 并回填, ``media_files.content_type`` 随后删除),
而我们的自定义链在 ``e5f6a7b8c9d0`` 收尾 (v0.21.0 的合并点), 于是合完出现两个 head.
这里用 merge 迁移把两条支线并起来, 保持恰好一个 head.

**不做数据改写**: 两条支线自 ``1fdf4d41aac1`` 起改的是不同的表与列
(上游动 ``metadata`` / ``media_files.content_type``, 我们这条只加了 ``automation``
与 ``auto_organize``), 互不相干, 没有先后依赖.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = ("e5f6a7b8c9d0", "9054ebacea29")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """只合并分支, 无数据改写."""


def downgrade() -> None:
    """只合并分支, 无数据改写."""
