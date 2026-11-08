"""PURGE handler - 一键重置: 清空片库 → 清理 → 清空任务历史."""

import asyncio
from pathlib import Path

import pytest

from amane.db.models import MediaFileStatus, TaskType
from amane.db.repository import Repository
from amane.handlers import CleanupPayload, PurgeHandler, PurgePayload
from amane.media import ResourceStore
from tests.helpers import LoopProgressProbe


@pytest.fixture
def purge_env(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> tuple[Repository, ResourceStore, Path]:
    return repo, resource_store, tmp_path


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
    assert outcome.drop_when_done is False
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
    # 收尾自删: 连这条已完成记录一起消失, 用户不用再点「清除已完成」.
    assert outcome.drop_when_done is True
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


@pytest.mark.asyncio
async def test_purge_disk_io_keeps_loop_responsive_and_preserves_outside_files(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, target = await _seed_library(repo, tmp_path, "ABC-003")
    library = (await repo.list_libraries())[0]
    assert library.id is not None
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    await repo.create_media_file(library.id, path=str(outside), number="OTHER-001")
    probe = LoopProgressProbe()
    unlink = Path.unlink

    def blocked_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == target:
            probe.wait()
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", blocked_unlink)
    outcome, _ = await asyncio.gather(
        PurgeHandler(repo, resource_store).handle(
            PurgePayload(delete_files=True, prune_empty_dirs=False, run_cleanup=False, purge_task_history=False)
        ),
        probe.marker(),
    )
    assert probe.passed, "媒体删除阻塞事件循环"
    assert outcome.result is not None and outcome.result.files_removed == 1
    assert not target.exists()
    assert outside.read_bytes() == b"outside"
