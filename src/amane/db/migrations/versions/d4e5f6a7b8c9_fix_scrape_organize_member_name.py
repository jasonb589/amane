"""rewrite leftover custom automation member name (SCRAPE_ORGANIZE -> SCRAPE)

Revision ID: d4e5f6a7b8c9
Revises: c3f1a2b4d5e6
Create Date: 2026-10-13 05:30:00.000000

``c3f1a2b4d5e6`` 里第一次写的匹配值只有小写取值 (``'scrape_organize'`` / ``'scrape'``), 而枚举列
自上游 ``05ee7dd4c06d`` 起存的是**成员名** (``'SCRAPE_ORGANIZE'`` / ``'SCRAPE'``), 于是那条改写一条也没命中,
库里的 ``SCRAPE_ORGANIZE`` 一直留着, 应用读媒体库时抛
``LookupError: 'SCRAPE_ORGANIZE' is not among the defined enum values``, 启动失败.

这里补一次归一 (两种写法都认, 幂等), 已经在 ``c3f1a2b4d5e6`` 之前就改成 ``SCRAPE`` 的库不受影响.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3f1a2b4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    has_auto_organize = "auto_organize" in {c["name"] for c in sa.inspect(bind).get_columns("libraries")}
    for legacy in ("SCRAPE_ORGANIZE", "scrape_organize"):
        if has_auto_organize:
            op.execute(
                sa.text(
                    f"UPDATE libraries SET automation = 'SCRAPE', auto_organize = 1"
                    f" WHERE automation = '{legacy}'"
                )
            )
        else:
            op.execute(sa.text(f"UPDATE libraries SET automation = 'SCRAPE' WHERE automation = '{legacy}'"))


def downgrade() -> None:
    """不还原: 升级后无法区分哪些行原本是自定义档位 (用户可能自行调整过 automation / 自动整理)."""
