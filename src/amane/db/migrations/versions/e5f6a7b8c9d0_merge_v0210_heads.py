"""merge upstream v0.21 head and custom head

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9, 1fdf4d41aac1
Create Date: 2026-10-16 04:00:00.000000

上游 v0.21.0 在 ``8dbd0fa0905e`` 之后又接了三条迁移 (``ea2ffb776110`` 无码前缀重投影 →
``136fd7914700`` DRGBD / SMDV 重投影 → ``1fdf4d41aac1`` 删 ``tasks.log_file``), 而我们的
自定义链尾 ``d4e5f6a7b8c9`` 经 ``c3f1a2b4d5e6`` 挂在同一父 ``8dbd0fa0905e`` 上, 于是合完
出现两个 head. 这里用 merge 迁移把两条支线并起来, 保持恰好一个 head.

**不做数据改写**: 三条上游迁移分别重投影无码标记与删列, 与我们的自定义改写
(``automation='scrape_organize'`` → ``SCRAPE`` + ``auto_organize=1``, 已幂等) 互不相干, 没有先后依赖;
``1fdf4d41aac1`` 删的 ``tasks.log_file`` 也不在本 fork 的任何代码里出现.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = ("d4e5f6a7b8c9", "1fdf4d41aac1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """只合并分支, 无数据改写."""


def downgrade() -> None:
    """只合并分支, 无数据改写."""
