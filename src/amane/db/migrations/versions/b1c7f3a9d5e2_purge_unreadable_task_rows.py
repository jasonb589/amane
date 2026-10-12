"""purge task rows the current enums cannot read

Revision ID: b1c7f3a9d5e2
Revises: 1f050b272f7d
Create Date: 2026-10-12 07:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c7f3a9d5e2"
down_revision: str | None = "1f050b272f7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 amane.db.models 的枚举**成员名**一致 (自上游 05ee7dd4c06d 起, 枚举列存的是成员名, 大写).
# 枚举删值 / 改名后, 库里残留的旧取值会让 SQLAlchemy 读该行时直接抛错 (LookupError),
# 一旦被列表 / 批量操作选中就整批 500. 比较一律走 UPPER(), 兼容更早的取值写法.
_TYPES = (
    "SCRAPE",
    "ORGANIZE",
    "REFRESH",
    "CLEANUP",
    "UPSCALE",
    "ACTOR_SCRAPE",
    "RESCRAPE",
    "SCAN_INVALID",
    "DELETE",
)
_STATUSES = ("QUEUED", "RUNNING", "DONE", "FAILED")


def _in_clause(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _unreadable() -> str:
    return (
        "type IS NULL OR status IS NULL"
        f" OR UPPER(type) NOT IN ({_in_clause(_TYPES)})"
        f" OR UPPER(status) NOT IN ({_in_clause(_STATUSES)})"
    )

def _json_valid_available() -> bool:
    """``json_valid`` 需要 SQLite 编译进 JSON1; 不可用时跳过坏 JSON 判断, 不阻塞升级."""
    try:
        op.get_bind().execute(sa.text("SELECT json_valid('{}')")).one()
    except Exception:
        return False
    return True


def upgrade() -> None:
    """清掉枚举改名 / 写坏残留的历史行: 读 ``type='trash'`` (v0.18 及以前的回收任务) 会在
    枚举校验处抛错, 任务列表与批量删除整批 500. ``trash`` 的继任者是 ``delete``, 先改写保住
    历史; 其余读不出来的行 (空枚举值 / 坏 JSON) 直接删除, 反正界面上也读不出来.

    边先删, 免得留下指向已删任务的悬空行 (外键开启的库会拒绝删除被引用的行).
    """
    op.execute(sa.text("UPDATE tasks SET type = 'DELETE' WHERE UPPER(type) = 'TRASH'"))

    condition = _unreadable()
    if _json_valid_available():
        condition += (
            " OR payload IS NULL OR json_valid(payload) = 0"
            " OR (result IS NOT NULL AND json_valid(result) = 0)"
        )

    op.execute(sa.text(f"DELETE FROM task_links WHERE parent_task_id IN (SELECT id FROM tasks WHERE {condition})"))
    op.execute(sa.text(f"DELETE FROM task_links WHERE child_task_id IN (SELECT id FROM tasks WHERE {condition})"))
    op.execute(sa.text(f"DELETE FROM tasks WHERE {condition}"))


def downgrade() -> None:
    """改写过的行无法还原, 删除的行不可恢复."""
