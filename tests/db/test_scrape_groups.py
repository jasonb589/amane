"""同番号任务共享影片结果, 排队成员不占执行容量且文件任务独立保留."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlmodel import select

from amane.db.models import MediaFileStatus, ScrapeGroup, Task, TaskStatus, TaskType
from amane.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import Sequence


async def _payloads(repo: Repository, root: Path, count: int = 3) -> list[dict[str, object]]:
    library = await repo.create_library(name="Shared", path=str(root))
    assert library.id is not None
    payloads: list[dict[str, object]] = []
    for part in range(1, count + 1):
        media = await repo.create_media_file(library.id, path=str(root / f"ABC-123-CD{part}.mp4"))
        assert media.id is not None
        payloads.append({"number": "ABC-123", "media_file_id": media.id})
    return payloads


def _id(task: Task | None) -> int:
    assert task is not None and task.id is not None
    return task.id


async def _groups(repo: Repository) -> Sequence[ScrapeGroup]:
    async with repo._session() as session:
        return (await session.exec(select(ScrapeGroup))).all()


@pytest.mark.asyncio
async def test_concurrent_enqueue_and_claim_select_one_producer_and_skip_waiters(
    repo: Repository, tmp_path: Path
) -> None:
    payloads = await _payloads(repo, tmp_path, 4)
    # 两个 Repository 共享数据库, 不能仅靠单实例进程锁阻止重复建组或认领.
    other = Repository(repo._engine)
    tasks = await asyncio.gather(
        *(
            owner.create_task(TaskType.SCRAPE, payload)
            for owner, payload in zip((repo, other, repo, other), payloads, strict=True)
        )
    )
    assert len({_id(task) for task in tasks}) == 4
    assert len({task.scrape_group_id for task in tasks}) == 1 and tasks[0].scrape_group_id is not None
    unrelated = await repo.create_task(TaskType.CLEANUP, {})

    claimed = await asyncio.gather(repo.claim_next_task(), other.claim_next_task())
    ids = {_id(task) for task in claimed}
    assert _id(unrelated) in ids
    producer = next(task for task in claimed if task is not None and task.type is TaskType.SCRAPE)
    group = await repo.get_scrape_group_for_task(_id(producer))
    assert group is not None and group.status is TaskStatus.RUNNING and group.producer_task_id == producer.id
    assert await repo.claim_next_task() is None
    for task in tasks:
        row = await repo.get_task(_id(task))
        assert row is not None and row.status is (TaskStatus.RUNNING if row.id == producer.id else TaskStatus.QUEUED)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"number": "abc-123"},
        {"number": "ABC-124"},
        {"number": "ÀBC-123"},
        {"content_type": "western"},
        {"url": "https://example.com/item"},
        {"use_cache": []},
        {"use_cache": ["trans", "metadata"]},
    ],
)
async def test_group_identity_respects_request_semantics(
    repo: Repository, tmp_path: Path, change: dict[str, object]
) -> None:
    first, second, _ = await _payloads(repo, tmp_path)
    second.update(change)
    tasks = await repo.create_tasks(TaskType.SCRAPE, [first, second])
    same = change in ({"number": "abc-123"}, {"use_cache": ["trans", "metadata"]})
    assert (tasks[0].scrape_group_id == tasks[1].scrape_group_id) is same
    assert all(task.scrape_group_id is not None for task in tasks)


@pytest.mark.asyncio
async def test_invalid_and_by_number_payloads_do_not_join_file_groups(repo: Repository, tmp_path: Path) -> None:
    first, _, _ = await _payloads(repo, tmp_path)
    created = await repo.create_task(TaskType.SCRAPE, first)
    for fields in (
        {"media_file_id": None},
        {"media_file_id": True},
        {"media_file_id": 999999},
        {"media_file_id": "1"},
        {"content_type": "invalid"},
        {"use_cache": ["invalid"]},
        {"number": " "},
    ):
        task = await repo.create_task(TaskType.SCRAPE, {**first, **fields, "scrape_group_id": created.scrape_group_id})
        assert task.scrape_group_id is None
    repeated = await repo.create_task(TaskType.SCRAPE, first)
    assert repeated.scrape_group_id is not None and repeated.scrape_group_id != created.scrape_group_id
    assert len(await _groups(repo)) == 2


@pytest.mark.asyncio
async def test_unicode_numbers_do_not_use_casefold_equivalence(repo: Repository, tmp_path: Path) -> None:
    first, second, _ = await _payloads(repo, tmp_path)
    first["number"] = "ÄBC-123"
    second["number"] = "äbc-123"
    tasks = await repo.create_tasks(TaskType.SCRAPE, [first, second])
    assert tasks[0].scrape_group_id != tasks[1].scrape_group_id


@pytest.mark.asyncio
async def test_new_file_binding_does_not_join_existing_file_rescrape(repo: Repository, tmp_path: Path) -> None:
    first, second, third = await _payloads(repo, tmp_path)
    metadata = await repo.upsert_metadata("ABC-123")
    assert metadata.id is not None
    second_id = second["media_file_id"]
    third_id = third["media_file_id"]
    assert isinstance(second_id, int) and isinstance(third_id, int)
    await repo.update_media_file(second_id, metadata_id=metadata.id, status=MediaFileStatus.SCRAPED)
    await repo.update_media_file(third_id, metadata_id=metadata.id)
    tasks = await repo.create_tasks(TaskType.SCRAPE, [first, second, third])
    assert tasks[0].scrape_group_id != tasks[1].scrape_group_id
    assert tasks[1].scrape_group_id == tasks[2].scrape_group_id


@pytest.mark.asyncio
async def test_result_is_available_before_producer_file_completion_and_survives_failure(
    repo: Repository, tmp_path: Path
) -> None:
    tasks = await repo.create_tasks(TaskType.SCRAPE, await _payloads(repo, tmp_path))
    producer = await repo.claim_next_task()
    assert _id(producer) == _id(tasks[0])
    result: dict[str, object] = {"metadata_id": 123, "field_sources": {"title": "source"}, "failed_sites": []}
    await repo.complete_scrape_group(_id(producer), result)
    await repo.complete_scrape_group(_id(producer), result)
    with pytest.raises(ValueError, match="不允许修改"):
        await repo.complete_scrape_group(_id(producer), {"metadata_id": 999})
    with pytest.raises(ValueError, match="只有刮削生产任务"):
        await repo.complete_scrape_group(_id(tasks[1]), result)

    follower = await repo.claim_next_task()
    assert _id(follower) == _id(tasks[1])
    await repo.fail_task(_id(producer), "file finalization failed")
    group = await repo.get_scrape_group_for_task(_id(follower))
    assert group is not None and group.status is TaskStatus.DONE and group.result == result
    assert group.error is None
    rebuilt = Repository(repo._engine)
    restored = await rebuilt.get_scrape_group_for_task(_id(tasks[2]))
    assert restored is not None and restored.result == result


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["complete", "followups", "fail", "cancel", "restart", "delete", "batch-delete"])
async def test_producer_lifecycle_releases_queued_members(repo: Repository, tmp_path: Path, finish: str) -> None:
    tasks = await repo.create_tasks(TaskType.SCRAPE, await _payloads(repo, tmp_path))
    producer_id = _id(await repo.claim_next_task())
    result: dict[str, object] = {"metadata_id": 42}
    if finish == "complete":
        await repo.complete_task(producer_id, result)
    elif finish == "followups":
        await repo.complete_task_with_followups(producer_id, result, [])
    elif finish == "fail":
        await repo.fail_task(producer_id, "source failed", {"failed_sites": ["source"]})
    elif finish == "cancel":
        assert await repo.fail_running_task(producer_id, "cancelled")
    elif finish == "restart":
        assert await repo.fail_all_running_tasks() == 1
    elif finish == "delete":
        assert await repo.delete_task(producer_id)
    else:
        assert await repo.delete_tasks([producer_id]) == 1
    follower = await repo.claim_next_task()
    assert _id(follower) == _id(tasks[1])
    group = await repo.get_scrape_group_for_task(_id(follower))
    assert group is not None
    if finish in {"complete", "followups"}:
        assert group.status is TaskStatus.DONE and group.result == result
    else:
        assert group.status is TaskStatus.FAILED and group.error


@pytest.mark.asyncio
async def test_follower_cancellation_does_not_fail_producer_and_retry_uses_new_group(
    repo: Repository, tmp_path: Path
) -> None:
    payloads = await _payloads(repo, tmp_path, 4)
    tasks = await repo.create_tasks(TaskType.SCRAPE, payloads[:3])
    producer_id = _id(await repo.claim_next_task())
    await repo.fail_task(_id(tasks[1]), "cancel follower")
    group = await repo.get_scrape_group_for_task(producer_id)
    assert group is not None and group.status is TaskStatus.RUNNING
    retry = (await repo.retry_tasks([tasks[1]]))[0]
    assert retry.scrape_group_id != tasks[1].scrape_group_id
    assert _id(await repo.claim_next_task()) == _id(retry)
    await repo.fail_running_task(_id(retry), "retry failed")
    await repo.fail_running_task(producer_id, "producer failed")
    later = await repo.create_task(TaskType.SCRAPE, payloads[3])
    assert later.scrape_group_id not in {task.scrape_group_id for task in [*tasks, retry]}


@pytest.mark.asyncio
async def test_cancelled_producer_cannot_publish_but_restart_keeps_committed_results(
    repo: Repository, tmp_path: Path
) -> None:
    payloads = await _payloads(repo, tmp_path)
    tasks = await repo.create_tasks(TaskType.SCRAPE, payloads)
    producer = _id(await repo.claim_next_task())
    await repo.fail_running_task(producer, "cancelled")
    with pytest.raises(ValueError, match="生产任务已停止"):
        await repo.complete_scrape_group(producer, {"metadata_id": 1})
    retries = await repo.retry_tasks(tasks)
    assert len({task.scrape_group_id for task in retries}) == 1
    # 原组剩余成员只接收失败, 不得阻断新组的实际生产者.
    for _ in range(2):
        old = _id(await repo.claim_next_task())
        await repo.fail_task(old, "cancelled")
    producer = _id(await repo.claim_next_task())
    await repo.complete_scrape_group(producer, {"metadata_id": 2})
    await repo.fail_all_running_tasks()
    group = await repo.get_scrape_group_for_task(producer)
    assert group is not None and group.status is TaskStatus.DONE and group.result == {"metadata_id": 2}


@pytest.mark.asyncio
async def test_missing_shared_result_does_not_leave_followers_blocked(repo: Repository, tmp_path: Path) -> None:
    tasks = await repo.create_tasks(TaskType.SCRAPE, await _payloads(repo, tmp_path, 2))
    producer = _id(await repo.claim_next_task())
    await repo.complete_task(producer)
    follower = _id(await repo.claim_next_task())
    assert follower == _id(tasks[1])
    group = await repo.get_scrape_group_for_task(follower)
    assert group is not None and group.status is TaskStatus.FAILED and group.error == "刮削生产任务未提供可复用结果"
    standalone = await repo.create_task(TaskType.SCRAPE, {"number": "BY-NUMBER"})
    await repo.complete_scrape_group(_id(standalone), {"metadata_id": 2})
    assert await repo.get_scrape_group_for_task(_id(standalone)) is None


@pytest.mark.asyncio
async def test_completed_group_admits_new_files_only_while_members_are_active(repo: Repository, tmp_path: Path) -> None:
    payloads = await _payloads(repo, tmp_path, 4)
    tasks = await repo.create_tasks(TaskType.SCRAPE, payloads[:2])
    producer = await repo.claim_next_task()
    await repo.complete_task(_id(producer), {"metadata_id": 1})
    late = await repo.create_task(TaskType.SCRAPE, payloads[2])
    assert late.scrape_group_id == tasks[0].scrape_group_id
    for _ in range(2):
        follower = await repo.claim_next_task()
        await repo.complete_task(_id(follower), {"metadata_id": 1})
    fresh = await repo.create_task(TaskType.SCRAPE, payloads[3])
    assert fresh.scrape_group_id != late.scrape_group_id


@pytest.mark.asyncio
async def test_followup_groups_keep_distinct_task_links_and_cleanup_only_after_last_member(
    repo: Repository, tmp_path: Path
) -> None:
    payloads = await _payloads(repo, tmp_path, 2)
    parent = await repo.create_task(TaskType.REFRESH, {})
    assert _id(await repo.claim_next_task()) == _id(parent)
    children = await repo.complete_task_with_followups(
        _id(parent), {}, [(f"scrape:{index}", TaskType.SCRAPE, payload, 0) for index, payload in enumerate(payloads)]
    )
    assert len(children) == 2 and children[0].scrape_group_id == children[1].scrape_group_id
    assert len(await repo.list_task_links(parent_task_id=_id(parent))) == 2
    assert all(child.root_task_id == parent.id for child in children)
    producer_id = _id(await repo.claim_next_task())
    await repo.complete_task(producer_id, {"metadata_id": 1})
    assert await repo.delete_task(producer_id)
    assert len(await _groups(repo)) == 1
    remaining = next(child for child in children if child.id != producer_id)
    assert await repo.delete_tasks([_id(remaining)]) == 1
    assert await _groups(repo) == []
    assert await repo.get_task(_id(parent)) is not None
