"""merge upstream v0.20 head and map custom automation=scrape_organize

Revision ID: c3f1a2b4d5e6
Revises: b1c7f3a9d5e2, 8dbd0fa0905e
Create Date: 2026-10-13 02:00:00.000000

本文件同时承担两件事:

1. **合并两个分支的 head** —— 自定义迁移 ``b1c7f3a9d5e2`` 与上游 v0.20 的新链尾
   ``8dbd0fa0905e`` 是同一父 ``1f050b272f7d`` 下的两条支线. 不合并就会出现两个 head
   (alembic 直接拒绝 ``upgrade head``), 而把自定义迁移改挂到上游链尾又会让 alembic 认为
   升级前的库"已经走到链尾", 从而**跳过上游那 4 条迁移** (实测: 跳过了加 ``auto_organize`` 列的
   ``4f36abe6b903``, 于是本迁移的 UPDATE 报 ``no such column``). 用 merge 迁移后, 旧库
   (停在 ``b1c7f3a9d5e2``) 会先补齐上游整条支线, 再执行下面的数据改写.
2. **数据改写** —— 把本 fork 自定义档位 ``automation='scrape_organize'`` 折进上游 v0.20 的
   ``libraries.auto_organize``: 旧取值留在库里会让 ``LibraryAutomation`` 读该行时抛
   ``ValueError`` (媒体库列表 / 监控启动都会失败), 所以级别降回 ``scrape`` 并打开自动整理,
   行为与旧档位一致.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f1a2b4d5e6"
down_revision: str | Sequence[str] | None = ("b1c7f3a9d5e2", "8dbd0fa0905e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """数据改写: 自定义档位折进上游 ``auto_organize``.

    枚举列自上游 ``05ee7dd4c06d`` 起存的是**成员名**(``SCRAPE``), 不是取值(``scrape``) ——
    这里两种写法都认, 统一改写成 ``SCRAPE`` + ``auto_organize = 1``, 行为与原档位一致.
    """
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
            # 异常顺序 (上游支线未跑到): 至少把旧枚举取值归一到上游能读的值, 不再阻断启动.
            op.execute(sa.text(f"UPDATE libraries SET automation = 'SCRAPE' WHERE automation = '{legacy}'"))


def downgrade() -> None:
    """不还原: 升级后无法区分哪些行原本是自定义档位 (用户可能自行调整过 automation / 自动整理)."""
