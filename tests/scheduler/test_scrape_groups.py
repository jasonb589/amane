"""同番号分集共享一次影片获取, 逐文件任务与整理仍各自完成."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from xml.etree import ElementTree

import pytest

from amane.config import HotSettings
from amane.crawlers.base import Crawler
from amane.crawlers.factory import CrawlerFactory
from amane.crawlers.models import MediaMetadata
from amane.db.models import MediaFileStatus, Task, TaskStatus, TaskType
from amane.db.repository import Repository
from amane.enums import Language, MetadataField
from amane.handlers import OrganizeHandler, ScrapeHandler, ScrapePayload
from amane.handlers.models import CacheKind, ScrapeResult
from amane.llm import Translator
from amane.media.pipeline import MaterializedImages
from amane.net.http import WebClient
from amane.observability.models import SiteOutcomeKind
from amane.parsing import ContentType
from amane.scheduler.worker import CANCEL_ERROR, AsyncWorker
from tests.helpers import LOAD_TOLERANT_TIMEOUT

if TYPE_CHECKING:
    from amane.crawlers.models import FetchOptions, SearchQuery
    from amane.media import ResourceStore


@dataclass(frozen=True)
class _Files:
    number: str
    content_type: ContentType
    paths: tuple[Path, ...]
    media_ids: tuple[int, ...]
    output: Path

    def payloads(self) -> list[ScrapePayload]:
        return [
            ScrapePayload(
                number=self.number,
                content_type=self.content_type,
                media_file_id=media_id,
                path=str(path),
                use_cache=set(),
            )
            for path, media_id in zip(self.paths, self.media_ids, strict=True)
        ]


async def _files(
    repo: Repository,
    root: Path,
    *,
    number: str = "MIDV-123",
    content_type: ContentType = ContentType.CENSORED,
    count: int = 5,
    organize: bool = False,
) -> _Files:
    incoming = root / "incoming"
    incoming.mkdir(parents=True)
    output = root / "organized"
    library = await repo.create_library(
        name=number,
        path=str(incoming),
        auto_organize=organize,
        video_template=f"{output.as_posix()}/{{number}}[-CD{{cd?}}].{{ext}}",
        copy_resources=[],
    )
    assert library.id is not None
    paths: list[Path] = []
    media_ids: list[int] = []
    for part in range(1, count + 1):
        path = incoming / f"{number}-CD{part}.mp4"
        path.write_bytes(f"{number}-video-{part}".encode())
        media = await repo.create_media_file(library.id, path=str(path), number=number)
        assert media.id is not None
        paths.append(path)
        media_ids.append(media.id)
    return _Files(number, content_type, tuple(paths), tuple(media_ids), output)


def _config(content_type: ContentType, *, failed_source: bool = False) -> HotSettings:
    config = HotSettings()
    config.actor_scraping.auto_scrape = False
    config.scraping.content_routes[content_type] = ["javdb", "jav321"] if failed_source else ["javdb"]
    return config


def _factory(number: str, *, failed_source: bool = False) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    crawler = AsyncMock(spec=Crawler)
    crawler.fetch.return_value = MediaMetadata(number=number, title="Shared title", studio="Studio")
    missing = AsyncMock(spec=Crawler)
    missing.fetch.return_value = None
    factory = AsyncMock(spec=CrawlerFactory)
    factory.get_crawlers.return_value = {"javdb": crawler, "jav321": missing} if failed_source else {"javdb": crawler}
    return factory, crawler, missing


async def _receive(worker: AsyncWorker, count: int) -> list[int]:
    async with asyncio.timeout(LOAD_TOLERANT_TIMEOUT):
        return [await worker._done_queue.get() for _ in range(count)]


async def _stop(worker: AsyncWorker) -> None:
    worker.retire()
    await worker.wait_stopped()
    await worker.shutdown_active()


async def _stored_tasks(repo: Repository, tasks: list[Task]) -> list[Task]:
    found = await repo.find_tasks(task_ids=[task.id for task in tasks if task.id is not None])
    assert len(found) == len(tasks)
    return found


async def _assert_bound(repo: Repository, files: _Files) -> int:
    metadata = await repo.get_metadata_by_number(files.number)
    assert metadata is not None and metadata.id is not None
    for media_id in files.media_ids:
        media = await repo.get_media_file(media_id)
        assert media is not None and media.metadata_id == metadata.id and media.status is MediaFileStatus.SCRAPED
    return metadata.id


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 3])
@pytest.mark.parametrize(
    ("number", "content_type"),
    [
        ("MIDV-123", ContentType.CENSORED),
        ("FC2-4800334", ContentType.FC2),
        ("Brazzers.24.01.01", ContentType.WESTERN),
        ("010123-001", ContentType.UNCENSORED),
    ],
)
async def test_all_content_types_share_sources_beyond_worker_capacity(
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
    number: str,
    content_type: ContentType,
    concurrency: int,
) -> None:
    files = await _files(repo, tmp_path, number=number, content_type=content_type)
    tasks = await repo.create_tasks(TaskType.SCRAPE, files.payloads())
    factory, crawler, missing = _factory(number, failed_source=True)
    handler = ScrapeHandler(repo, factory, resource_store, _config(content_type, failed_source=True))
    worker = AsyncWorker(repo, {TaskType.SCRAPE: handler}, concurrency=concurrency, poll_interval=0.01)
    worker.start()
    try:
        await _receive(worker, len(tasks))
    finally:
        await _stop(worker)

    stored = await _stored_tasks(repo, tasks)
    assert all(task.status is TaskStatus.DONE for task in stored)
    metadata_id = await _assert_bound(repo, files)
    results = [ScrapeResult.model_validate(task.result) for task in stored]
    assert {result.metadata_id for result in results} == {metadata_id}
    assert all("jav321" in result.failed_sites for result in results)
    assert (
        sum(any(outcome.outcome is SiteOutcomeKind.CACHE_HIT for outcome in result.outcomes) for result in results) == 4
    )
    assert crawler.fetch.await_count == missing.fetch.await_count == 1
    assert factory.get_crawlers.await_count == 1


@pytest.mark.asyncio
async def test_shared_translation_and_images_keep_each_external_video_and_nfo(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = await _files(repo, tmp_path, count=4, organize=True)
    tasks = await repo.create_tasks(TaskType.SCRAPE, files.payloads())
    config = _config(files.content_type)
    config.llm.enabled = True
    config.llm.translate_fields = [MetadataField.TITLE]
    config.scraping.field_language[MetadataField.TITLE] = Language.ZH_CN
    factory, crawler, _ = _factory(files.number)
    translator = AsyncMock(spec=Translator)
    translator.translate.return_value = "共用标题"
    materialize = AsyncMock(return_value=MaterializedImages(["/api/resources/shared-poster"], [], []))
    monkeypatch.setattr("amane.handlers.scrape.materialize_images", materialize)
    scrape = ScrapeHandler(repo, factory, resource_store, config, AsyncMock(spec=WebClient), translator)
    organize = OrganizeHandler(repo, config, resource_store, safe_dirs=[tmp_path])
    worker = AsyncWorker(
        repo, {TaskType.SCRAPE: scrape, TaskType.ORGANIZE: organize}, concurrency=3, poll_interval=0.01
    )
    worker.start()
    try:
        await _receive(worker, len(tasks) * 2)
    finally:
        await _stop(worker)

    metadata = await repo.get_metadata_by_number(files.number)
    assert metadata is not None and metadata.title == "共用标题"
    assert metadata.poster_urls == ["/api/resources/shared-poster"]
    assert crawler.fetch.await_count == translator.translate.await_count == materialize.await_count == 1
    for part, (path, media_id, task) in enumerate(zip(files.paths, files.media_ids, tasks, strict=True), start=1):
        target = files.output / f"{files.number}-CD{part}.mp4"
        assert target.read_bytes() == f"{files.number}-video-{part}".encode()
        assert ElementTree.parse(target.with_suffix(".nfo")).getroot().findtext("title") == f"{files.number} 共用标题"
        assert not path.exists() and await repo.get_media_file(media_id) is None
        assert task.id is not None
        children = await repo.list_children(task.id)
        assert len(children) == 1
        child, key = children[0]
        assert child.status is TaskStatus.DONE and child.type is TaskType.ORGANIZE
        assert child.payload["media_file_ids"] == [media_id] and key == f"organize:{media_id}"


@pytest.mark.asyncio
async def test_failed_source_is_requested_once_and_explicit_retry_starts_one_new_round(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    files = await _files(repo, tmp_path, count=4)
    tasks = await repo.create_tasks(TaskType.SCRAPE, files.payloads())
    factory, crawler, _ = _factory(files.number)
    crawler.fetch.return_value = None
    handler = ScrapeHandler(repo, factory, resource_store, _config(files.content_type))
    worker = AsyncWorker(repo, {TaskType.SCRAPE: handler}, concurrency=3, poll_interval=0.01)
    worker.start()
    try:
        await _receive(worker, len(tasks))
        failed = await _stored_tasks(repo, tasks)
        assert all(task.status is TaskStatus.FAILED for task in failed)
        assert crawler.fetch.await_count == 1
        assert await repo.get_metadata_by_number(files.number) is None
        for task in failed:
            assert await repo.list_task_links(parent_task_id=task.id) == []
        crawler.fetch.return_value = MediaMetadata(number=files.number, title="Retry result")
        retried = await repo.retry_tasks(failed)
        await _receive(worker, len(retried))
    finally:
        await _stop(worker)
    assert all(task.status is TaskStatus.DONE for task in await _stored_tasks(repo, retried))
    assert crawler.fetch.await_count == 2
    await _assert_bound(repo, files)
    original_group = {task.scrape_group_id for task in failed}
    retry_group = {task.scrape_group_id for task in retried}
    assert len(original_group) == len(retry_group) == 1 and original_group.isdisjoint(retry_group)


@pytest.mark.asyncio
async def test_high_priority_member_produces_while_waiting_members_leave_capacity_for_other_numbers(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    files = await _files(repo, tmp_path / "first")
    payloads = files.payloads()
    tasks = await repo.create_tasks(TaskType.SCRAPE, payloads[:-1])
    producer = await repo.create_task(TaskType.SCRAPE, payloads[-1], priority=10)
    assert producer.id is not None
    other = await _files(repo, tmp_path / "other", number="MIDV-456", count=1)
    other_task = await repo.create_task(TaskType.SCRAPE, other.payloads()[0], priority=-1)
    assert other_task.id is not None
    started = asyncio.Event()
    release = asyncio.Event()
    factory, crawler, _ = _factory(files.number)

    async def fetch(query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata:
        if query.number == files.number:
            assert query.file_path == str(files.paths[-1])
            started.set()
            await release.wait()
        return MediaMetadata(number=query.number, title="Independent title")

    crawler.fetch.side_effect = fetch
    handler = ScrapeHandler(repo, factory, resource_store, _config(files.content_type))
    worker = AsyncWorker(repo, {TaskType.SCRAPE: handler}, concurrency=2, poll_interval=0.01)
    worker.start()
    try:
        await asyncio.wait_for(started.wait(), LOAD_TOLERANT_TIMEOUT)
        assert await _receive(worker, 1) == [other_task.id]
        waiting = await _stored_tasks(repo, tasks)
        assert all(task.status is TaskStatus.QUEUED for task in waiting)
        group = await repo.get_scrape_group_for_task(producer.id)
        assert group is not None and group.producer_task_id == producer.id and group.status is TaskStatus.RUNNING
        release.set()
        await _receive(worker, len(tasks) + 1)
    finally:
        release.set()
        await _stop(worker)
    assert crawler.fetch.await_count == 2
    await _assert_bound(repo, files)
    await _assert_bound(repo, other)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_producer", [False, True])
async def test_group_cancellation_does_not_repeat_requests_or_leave_waiters_running(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, cancel_producer: bool
) -> None:
    files = await _files(repo, tmp_path, count=4)
    tasks = await repo.create_tasks(TaskType.SCRAPE, files.payloads())
    started = asyncio.Event()
    release = asyncio.Event()
    factory, crawler, _ = _factory(files.number)

    async def fetch(query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata:
        started.set()
        await release.wait()
        return MediaMetadata(number=query.number, title="Released result")

    crawler.fetch.side_effect = fetch
    handler = ScrapeHandler(repo, factory, resource_store, _config(files.content_type))
    worker = AsyncWorker(repo, {TaskType.SCRAPE: handler}, concurrency=3, poll_interval=0.01)
    worker.start()
    try:
        await asyncio.wait_for(started.wait(), LOAD_TOLERANT_TIMEOUT)
        assert tasks[0].id is not None
        group = await repo.get_scrape_group_for_task(tasks[0].id)
        assert group is not None and group.producer_task_id is not None
        if cancel_producer:
            assert await worker.cancel_task(group.producer_task_id)
            await _receive(worker, len(tasks))
            stored = await _stored_tasks(repo, tasks)
            assert all(task.status is TaskStatus.FAILED for task in stored)
            assert crawler.fetch.await_count == 1
            release.set()
            retried = await repo.retry_tasks(stored)
            await _receive(worker, len(retried))
            assert all(task.status is TaskStatus.DONE for task in await _stored_tasks(repo, retried))
        else:
            follower = next(task for task in tasks if task.id != group.producer_task_id)
            assert follower.id is not None
            assert await repo.fail_queued_tasks(error=CANCEL_ERROR, task_ids=[follower.id]) == 1
            release.set()
            await _receive(worker, len(tasks) - 1)
            stored = await _stored_tasks(repo, tasks)
            assert sum(task.status is TaskStatus.DONE for task in stored) == len(tasks) - 1
            assert next(task for task in stored if task.id == follower.id).status is TaskStatus.FAILED
    finally:
        release.set()
        await _stop(worker)
    assert crawler.fetch.await_count == (2 if cancel_producer else 1)
    if cancel_producer:
        await _assert_bound(repo, files)


@pytest.mark.asyncio
async def test_new_repository_worker_and_handler_reuse_completed_group_without_recording_context(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    files = await _files(repo, tmp_path)
    tasks = await repo.create_tasks(TaskType.SCRAPE, files.payloads())
    factory, crawler, _ = _factory(files.number)
    config = _config(files.content_type)
    first_worker = AsyncWorker(repo, {TaskType.SCRAPE: ScrapeHandler(repo, factory, resource_store, config)})
    first = await repo.claim_next_task()
    assert first is not None and first.id is not None
    await first_worker._execute(first)
    group = await repo.get_scrape_group_for_task(first.id)
    assert group is not None and group.status is TaskStatus.DONE

    rebuilt = Repository(repo._engine, embeddings=repo.embeddings)
    second_worker = AsyncWorker(
        rebuilt,
        {TaskType.SCRAPE: ScrapeHandler(rebuilt, factory, resource_store, config)},
        concurrency=1,
        poll_interval=0.01,
    )
    second_worker.start()
    try:
        await _receive(second_worker, len(tasks) - 1)
    finally:
        await _stop(second_worker)
    assert crawler.fetch.await_count == 1
    assert all(task.status is TaskStatus.DONE for task in await _stored_tasks(rebuilt, tasks))
    await _assert_bound(rebuilt, files)


@pytest.mark.asyncio
async def test_late_default_cache_file_reuses_metadata_but_explicit_rescrape_and_force_still_fetch(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    files = await _files(repo, tmp_path, count=3)
    first_payload, late_payload, force_payload = files.payloads()
    cached = {CacheKind.metadata, CacheKind.trans}
    first_payload = first_payload.model_copy(update={"use_cache": cached})
    late_payload = late_payload.model_copy(update={"use_cache": cached})
    factory, crawler, missing = _factory(files.number, failed_source=True)
    config = _config(files.content_type, failed_source=True)
    worker = AsyncWorker(
        repo, {TaskType.SCRAPE: ScrapeHandler(repo, factory, resource_store, config)}, concurrency=1, poll_interval=0.01
    )
    worker.start()
    try:
        first = await repo.create_task(TaskType.SCRAPE, first_payload)
        await _receive(worker, 1)
        late = await repo.create_task(TaskType.SCRAPE, late_payload)
        await _receive(worker, 1)
        assert first.scrape_group_id != late.scrape_group_id
        assert crawler.fetch.await_count == missing.fetch.await_count == 1
        late_file = await repo.get_media_file(files.media_ids[1])
        assert late_file is not None and late_file.status is MediaFileStatus.SCRAPED
        assert late_file.metadata_id is not None

        repeated = await repo.create_task(TaskType.SCRAPE, late_payload)
        await _receive(worker, 1)
        assert crawler.fetch.await_count == 1
        assert missing.fetch.await_count == 2
        forced = await repo.create_task(TaskType.SCRAPE, force_payload)
        await _receive(worker, 1)
        assert crawler.fetch.await_count == 2
        assert missing.fetch.await_count == 3
    finally:
        await _stop(worker)
    assert all(task.status is TaskStatus.DONE for task in await _stored_tasks(repo, [first, late, repeated, forced]))
    await _assert_bound(repo, files)
