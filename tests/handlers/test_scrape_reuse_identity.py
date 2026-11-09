"""共享刮削保留显式刷新意图, 并在关联分集前验证当前影片身份."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from amane.config import HotSettings
from amane.crawlers.base import Crawler
from amane.crawlers.factory import CrawlerFactory
from amane.crawlers.models import MediaMetadata
from amane.db.models import MediaFileStatus, Task, TaskStatus, TaskType
from amane.db.repo_types import WriteMode
from amane.enums import MetadataField
from amane.handlers import ScrapeHandler, ScrapePayload
from amane.handlers.models import ScrapeResult
from amane.parsing import ContentType

if TYPE_CHECKING:
    from pathlib import Path

    from amane.db.repository import Repository
    from amane.handlers.protocol import TaskResult
    from amane.media import ResourceStore


async def _payloads(repo: Repository, root: Path) -> tuple[ScrapePayload, ScrapePayload]:
    library = await repo.create_library(name="Multipart identity", path=str(root), auto_organize=False)
    assert library.id is not None
    payloads: list[ScrapePayload] = []
    for part in (1, 2):
        path = root / f"MIDV-123-CD{part}.mp4"
        path.write_bytes(f"part-{part}".encode())
        media = await repo.create_media_file(library.id, path=str(path), number="MIDV-123")
        assert media.id is not None
        payloads.append(
            ScrapePayload(number="MIDV-123", content_type=ContentType.CENSORED, media_file_id=media.id, path=str(path))
        )
    return payloads[0], payloads[1]


def _handler(
    repo: Repository, resource_store: ResourceStore, *, second_source: bool = False
) -> tuple[ScrapeHandler, AsyncMock, AsyncMock, AsyncMock]:
    crawler = AsyncMock(spec=Crawler)
    crawler.fetch.return_value = MediaMetadata(number="MIDV-123", title="Source title", plot="Source plot")
    missing = AsyncMock(spec=Crawler)
    missing.fetch.return_value = MediaMetadata(number="MIDV-123", title="Previously missing source")
    factory = AsyncMock(spec=CrawlerFactory)
    factory.get_crawlers.return_value = {"javdb": crawler, "jav321": missing} if second_source else {"javdb": crawler}
    config = HotSettings()
    config.actor_scraping.auto_scrape = False
    config.scraping.content_routes[ContentType.CENSORED] = ["javdb", "jav321"] if second_source else ["javdb"]
    return ScrapeHandler(repo, factory, resource_store, config), factory, crawler, missing


async def _execute(repo: Repository, handler: ScrapeHandler, expected: Task) -> TaskResult[ScrapeResult]:
    task = await repo.claim_next_task()
    assert task is not None and task.id is not None and task.id == expected.id
    result = await handler.handle_task(ScrapePayload.model_validate(task.payload), task.id)
    assert result.followups == []
    if result.success:
        await repo.complete_task(task.id, result.as_dict())
    else:
        assert result.error is not None
        await repo.fail_task(task.id, result.error, result.as_dict())
    return result


@pytest.mark.asyncio
async def test_new_file_reuse_does_not_swallow_existing_file_rescrape(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """新分集先关联缓存时, 排在后面的显式重刮仍补取缓存中缺失的站点."""
    old_payload, new_payload = await _payloads(repo, tmp_path)
    cached = MediaMetadata(number=old_payload.number, title="Cached title")
    metadata = await repo.upsert_metadata(
        number=old_payload.number,
        content_type=old_payload.content_type,
        title=cached.title,
        raw={"javdb": cached.model_dump(mode="json")},
        field_sources={"title": "javdb"},
    )
    assert metadata.id is not None and old_payload.media_file_id is not None
    await repo.update_media_file(old_payload.media_file_id, metadata_id=metadata.id, status=MediaFileStatus.SCRAPED)
    new_task, old_task = await repo.create_tasks(TaskType.SCRAPE, [new_payload, old_payload])
    handler, factory, crawler, missing = _handler(repo, resource_store, second_source=True)

    assert (await _execute(repo, handler, new_task)).success
    factory.get_crawlers.assert_not_awaited()
    assert (await _execute(repo, handler, old_task)).success

    assert new_task.scrape_group_id is not None and old_task.scrape_group_id is not None
    assert new_task.scrape_group_id != old_task.scrape_group_id
    factory.get_crawlers.assert_awaited_once()
    crawler.fetch.assert_not_awaited()
    missing.fetch.assert_awaited_once()
    current = await repo.get_metadata_by_number(old_payload.number)
    assert current is not None and "jav321" in current.raw
    for payload in (old_payload, new_payload):
        assert payload.media_file_id is not None
        media = await repo.get_media_file(payload.media_file_id)
        assert media is not None and media.status is MediaFileStatus.SCRAPED and media.metadata_id == metadata.id


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_type", [None, ContentType.WESTERN])
async def test_missing_or_different_type_metadata_rejects_shared_identity_without_refetching(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    replacement_type: ContentType | None,
) -> None:
    """删除影片, 或 SQLite 重用原 id 新建异类型影片, 均不能让等待分集关联到错误对象."""
    payloads = [payload.model_copy(update={"use_cache": set()}) for payload in await _payloads(repo, tmp_path)]
    producer, follower = await repo.create_tasks(TaskType.SCRAPE, payloads)
    assert producer.scrape_group_id is not None and producer.scrape_group_id == follower.scrape_group_id
    handler, factory, crawler, _ = _handler(repo, resource_store)
    produced = await _execute(repo, handler, producer)
    assert produced.success and produced.result is not None and produced.result.metadata_id is not None
    original_id = produced.result.metadata_id
    assert await repo.delete_metadata(original_id)
    if replacement_type is not None:
        replacement = await repo.upsert_metadata(
            number=payloads[0].number, content_type=replacement_type, title="Unrelated replacement", raw={}
        )
        assert replacement.id == original_id

    reused = await _execute(repo, handler, follower)

    assert not reused.success and reused.error is not None and "已删除或变更" in reused.error
    factory.get_crawlers.assert_awaited_once()
    crawler.fetch.assert_awaited_once()
    assert payloads[1].media_file_id is not None
    waiting_file = await repo.get_media_file(payloads[1].media_file_id)
    assert (
        waiting_file is not None and waiting_file.metadata_id is None and waiting_file.status is MediaFileStatus.PENDING
    )
    assert follower.id is not None
    stored = await repo.find_tasks(task_ids=[follower.id])
    assert len(stored) == 1 and stored[0].status is TaskStatus.FAILED
    if replacement_type is not None:
        current = await repo.get_metadata_by_number(payloads[0].number)
        assert (
            current is not None
            and current.content_type is replacement_type
            and current.title == "Unrelated replacement"
        )


@pytest.mark.asyncio
async def test_follower_preserves_locked_and_unlocked_edits_made_after_producer(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    """共享结果只用来关联影片, 不把生产者的旧字段覆盖到用户刚编辑的 Metadata."""
    payloads = [payload.model_copy(update={"use_cache": set()}) for payload in await _payloads(repo, tmp_path)]
    producer, follower = await repo.create_tasks(TaskType.SCRAPE, payloads)
    assert producer.scrape_group_id is not None and producer.scrape_group_id == follower.scrape_group_id
    handler, factory, crawler, _ = _handler(repo, resource_store)
    produced = await _execute(repo, handler, producer)
    assert produced.success and produced.result is not None and produced.result.metadata_id is not None
    metadata_id = produced.result.metadata_id
    edited = await repo.update_metadata(
        metadata_id,
        mode=WriteMode.MANUAL,
        title="User title",
        plot="User plot",
        field_sources={"title": "manual", "plot": "manual"},
    )
    assert edited is not None
    await repo.set_metadata_locks(metadata_id, [MetadataField.TITLE])

    reused = await _execute(repo, handler, follower)

    assert reused.success and reused.result is not None and reused.result.metadata_id == metadata_id
    factory.get_crawlers.assert_awaited_once()
    crawler.fetch.assert_awaited_once()
    current = await repo.get_metadata_by_number(payloads[0].number)
    assert current is not None
    assert current.title == "User title" and current.plot == "User plot"
    assert current.field_sources == {"title": "manual", "plot": "manual"}
    assert current.locked_fields == [MetadataField.TITLE]
    assert current.updated_at == edited.updated_at
    assert payloads[1].media_file_id is not None
    media = await repo.get_media_file(payloads[1].media_file_id)
    assert media is not None and media.metadata_id == metadata_id and media.status is MediaFileStatus.SCRAPED
