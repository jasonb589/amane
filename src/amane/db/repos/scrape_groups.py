"""文件刮削分组的事务内操作; 不读取文件系统, 不改变任务载荷."""

import hashlib
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError
from sqlalchemy import delete as sqla_delete
from sqlalchemy import exists
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ...parsing import ContentType
from ..models import MediaFile, MediaFileStatus, ScrapeGroup, Task, TaskStatus, TaskType
from ..repo_types import _utcnow
from .base import chunks


class _ScrapeIdentity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: str = Field(min_length=1, strict=True)
    content_type: ContentType = ContentType.CENSORED
    media_file_id: int = Field(gt=0, strict=True)
    url: str | None = Field(default=None, strict=True)
    use_cache: set[Literal["metadata", "trans"]] = {"metadata", "trans"}

    def key(self, *, new_file: bool) -> str:
        # SQLite NOCASE 仅折叠 ASCII 字母; Unicode casefold 会合并数据库中的不同番号.
        number = self.number.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        cache: list[str] = sorted(self.use_cache)
        values = (number, str(self.content_type), self.url, cache, new_file)
        serialized = TypeAdapter(tuple[str, str, str | None, list[str], bool]).dump_json(values)
        return hashlib.sha256(serialized).hexdigest()


async def assign_scrape_group(session: AsyncSession, task_type: TaskType, payload: dict[str, object]) -> int | None:
    if task_type is not TaskType.SCRAPE:
        return None
    try:
        identity = _ScrapeIdentity.model_validate(payload)
    except ValidationError:
        return None
    media = await session.get(MediaFile, identity.media_file_id)
    if not identity.number.strip() or media is None:
        return None
    # 既有文件的显式重刮不与新文件的直接关联快路共享结果.
    key = identity.key(new_file=media.status is MediaFileStatus.PENDING and media.metadata_id is None)
    active_member = exists(
        select(Task.id).where(
            Task.scrape_group_id == ScrapeGroup.id,
            col(Task.status).in_((TaskStatus.QUEUED, TaskStatus.RUNNING)),
        )
    )
    stmt = (
        select(ScrapeGroup)
        .where(ScrapeGroup.key == key, ScrapeGroup.status != TaskStatus.FAILED, active_member)
        .order_by(col(ScrapeGroup.id).desc())
    )
    for group in (await session.exec(stmt)).all():
        members = (await session.exec(select(Task).where(Task.scrape_group_id == group.id))).all()
        # 同一个文件再次提交代表新一轮; 克隆旧载荷的显式重试也不得返回旧组结果.
        if any((member.payload or {}).get("media_file_id") == identity.media_file_id for member in members):
            continue
        return group.id
    group = ScrapeGroup(key=key)
    session.add(group)
    await session.flush()
    assert group.id is not None
    return group.id


async def finish_scrape_group(
    session: AsyncSession,
    task: Task,
    *,
    result: dict[str, object] | None,
    error: str | None,
) -> None:
    """只有生产者可结算尚在运行的组; 文件阶段失败不得覆盖已保存的影片结果."""
    if task.scrape_group_id is None:
        return
    group = await session.get(ScrapeGroup, task.scrape_group_id)
    if group is None or group.status is not TaskStatus.RUNNING or group.producer_task_id != task.id:
        return
    if error is None and result is None:
        error = "刮削生产任务未提供可复用结果"
    group.status = TaskStatus.FAILED if error is not None else TaskStatus.DONE
    group.result = TypeAdapter(dict[str, JsonValue]).validate_python(result) if result is not None else None
    group.error = error
    group.updated_at = _utcnow()
    session.add(group)


async def fail_deleted_producers(session: AsyncSession, task_ids: Sequence[int]) -> None:
    for batch in chunks(list(task_ids)):
        groups = (
            await session.exec(
                select(ScrapeGroup).where(
                    ScrapeGroup.status == TaskStatus.RUNNING,
                    col(ScrapeGroup.producer_task_id).in_(batch),
                )
            )
        ).all()
        for group in groups:
            group.status = TaskStatus.FAILED
            group.error = "刮削生产任务已删除"
            group.updated_at = _utcnow()
            session.add(group)


async def delete_orphan_scrape_groups(session: AsyncSession) -> None:
    await session.flush()
    has_member = exists(select(Task.id).where(Task.scrape_group_id == ScrapeGroup.id))
    await session.exec(sqla_delete(ScrapeGroup).where(~has_member))
