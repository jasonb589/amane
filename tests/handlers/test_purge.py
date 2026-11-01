"""PURGE handler - 一键重置: 清空片库 → 清理 → 清空任务历史."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from amane.db.models import MediaFileStatus, TaskType
from amane.db.repository import Repository
from amane.handlers import CleanupPayload, PurgeHandler, PurgePayload
from amane.media import ResourceStore

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture
async def purge_env(tmp_path: Path) -> AsyncGenerator[tuple[Repository, ResourceStore, Path]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    repo = Repository(engine)
    store = ResourceStore(engine=engine, base_dir=tmp_path / "resources")
    yield repo, store, tmp_path
    await engine.dispose()


async def _seed_library(repo: Repository, tmp_path: Path, number: str) -> tuple[int, int, Path]:
    """建一个库 + 一条影片 + 一条落在库内的媒体文件记录; 返回 (metadata_id, media_id, 文件路径)."""
    lib = await repo.create_library(name="L", path=str(tmp_path / "lib"))
    assert lib.id is not None
    meta = await repo.upsert_metadata(number=number, title="t")
    assert meta.id is not None

    target = tmp_path / "lib" / number / f"{number}.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"video")

    media = await repo.create_media_file(
        lib.id,
        path=str(target),
        number=number,
        status=MediaFileStatus.SCRAPED,
        metadata_id=meta.id,
    )
    assert media.id is not None
    return meta.id, media.id, target


@pytest.mark.asyncio
async def test_purge_clears_library_and_deletes_files(purge_env):
    repo, store, tmp_path = purge_env
    metadata_id, media_id, target = await _seed_library(repo, tmp_path, "ABC-001")

    handler = PurgeHandler(repo, store)
    outcome = await handler.handle(
        PurgePayload(delete_files=True, prune_empty_dirs=True, run_cleanup=False, purge_task_history=False)
    )

    assert outcome.success
    assert outcome.result is not None
    assert outcome.result.metadata_removed == 1
    assert outcome.result.media_removed == 1
    assert outcome.result.files_removed == 1
    assert outcome.result.files_failed == 0
    assert outcome.result.pruned_dirs == 1
    assert not target.exists()
    assert not target.parent.exists()
    assert await repo.get_media_file(media_id) is None
    assert await repo.get_metadata(metadata_id) is None


@pytest.mark.asyncio
async def test_purge_keeps_files_when_disabled(purge_env):
    """关掉删文件时只丢记录, 磁盘文件与目录原地保留."""
    repo, store, tmp_path = purge_env
    metadata_id, media_id, target = await _seed_library(repo, tmp_path, "ABC-002")

    handler = PurgeHandler(repo, store)
    outcome = await handler.handle(PurgePayload(delete_files=False, run_cleanup=False, purge_task_history=False))

    assert outcome.result is not None
    assert outcome.result.files_removed == 0
    assert outcome.result.pruned_dirs == 0
    assert target.exists()
    assert target.parent.exists()
    assert await repo.get_media_file(media_id) is None
    assert await repo.get_metadata(metadata_id) is None


@pytest.mark.asyncio
async def test_purge_clears_task_history_but_keeps_unfinished(purge_env):
    repo, store, _tmp = purge_env

    done = await repo.create_task(TaskType.CLEANUP, CleanupPayload())
    failed = await repo.create_task(TaskType.CLEANUP, CleanupPayload())
    queued = await repo.create_task(TaskType.CLEANUP, CleanupPayload())
    assert done.id is not None and failed.id is not None and queued.id is not None
    await repo.complete_task(done.id)
    await repo.fail_task(failed.id, error="boom")

    handler = PurgeHandler(repo, store)
    outcome = await handler.handle(PurgePayload(purge_media=False, run_cleanup=False, purge_task_history=True))

    assert outcome.result is not None
    assert outcome.result.tasks_removed == 2
    assert await repo.get_task(done.id) is None
    assert await repo.get_task(failed.id) is None
    assert await repo.get_task(queued.id) is not None


@pytest.mark.asyncio
async def test_purge_runs_cleanup_after_library_wipe(purge_env):
    """清空后接着回收: 演员/分类随影片消失而变成 0 作品, 一并清掉."""
    repo, store, tmp_path = purge_env
    lib = await repo.create_library(name="L", path=str(tmp_path / "lib"))
    assert lib.id is not None
    await repo.upsert_metadata(number="ACT-1", title="t", actors=["Someone"], tags=["TagA"])

    handler = PurgeHandler(repo, store)
    outcome = await handler.handle(PurgePayload(delete_files=False, purge_task_history=False))

    assert outcome.success
    assert outcome.result is not None
    assert outcome.result.metadata_removed == 1
    assert outcome.result.actors_removed == 1
    assert outcome.result.facets_removed == 1
