"""map custom automation=scrape_organize onto upstream auto_organize

Revision ID: c3f1a2b4d5e6
Revises: b1c7f3a9d5e2
Create Date: 2026-10-13 02:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f1a2b4d5e6"
down_revision: str | None = "b1c7f3a9d5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """把自定义档位 ``automation='scrape_organize'`` 折进上游的 ``libraries.auto_organize``.

    本 fork 在 v0.19 上自定义了第四个自动化级别 (监控+刮削+整理); 上游 v0.20 用
    ``libraries.auto_organize`` 布尔列 + 原有三级 automation 实现了同一件事. 旧取值留在库里会让
    ``LibraryAutomation`` 读该行时抛 ``ValueError`` (媒体库列表 / 监控启动都会失败), 所以这里改写:
    级别降回 ``scrape``, 自动整理置 1 —— 行为与旧档位完全一致.

    上游的 ``4f36abe6b903`` 已在本迁移之前加好 ``auto_organize`` 列 (默认 0), 因此这里可以安全写入.
    """
    op.execute(
        sa.text("UPDATE libraries SET automation = 'scrape', auto_organize = 1 WHERE automation = 'scrape_organize'")
    )


def downgrade() -> None:
    """不还原: 升级后无法区分哪些行原本是自定义档位 (用户可能自行调整过 automation / 自动整理)."""
