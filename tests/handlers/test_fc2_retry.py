"""旧文件任务重试的 FC2 恢复必须同时满足路径证据和元数据身份约束."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from amane.aggregate import CrawlerLike
from amane.config import HotSettings
from amane.crawlers.base import Crawler
from amane.crawlers.models import FetchOptions, MediaMetadata, SearchQuery
from amane.db.models import TaskType
from amane.enums import MetadataField
from amane.handlers import CacheKind, ScrapeHandler, ScrapePayload
from amane.parsing import ContentType

if TYPE_CHECKING:
    from amane.db.repository import Repository
    from amane.media import ResourceStore

_PATH = "/media/gx/正常/4K688.com@fc4982441_1.mp4"
_CANONICAL = "FC2-4982441"
_ALIAS = "FC-4982441"


class _Sources:
    def __init__(self, *, found: bool) -> None:
        self.routes: list[list[str]] = []
        self.queries: list[SearchQuery] = []
        self.found = found
        self.archive = AsyncMock(spec=Crawler)
        self.archive.fetch.side_effect = self._fetch
        self.legacy = AsyncMock(spec=Crawler)
        self.legacy.fetch.side_effect = self._fetch
        self.sources: dict[str, CrawlerLike] = {"javarchive": self.archive, "javdb": self.legacy}

    async def _fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        self.queries.append(query)
        return (
            MediaMetadata(number=query.number, title="Fetched title", studio="Fetched studio") if self.found else None
        )

    async def get_crawlers(self, names: Iterable[str]) -> dict[str, CrawlerLike]:
        route = list(names)
        self.routes.append(route)
        return {name: self.sources[name] for name in route if name in self.sources}


def _handler(repo: Repository, resource_store: ResourceStore, sources: _Sources) -> ScrapeHandler:
    config = HotSettings()
    config.scraping.content_routes[ContentType.FC2] = ["javarchive"]
    config.scraping.content_routes[ContentType.CENSORED] = ["javdb"]
    config.scraping.content_routes[ContentType.WESTERN] = ["javdb"]
    config.actor_scraping.auto_scrape = False
    return ScrapeHandler(repo, sources, resource_store, config)


@dataclass(frozen=True)
class _Case:
    name: str
    recover: bool = False
    number: str = _ALIAS
    path: str | None = _PATH
    current_path: str = _PATH
    content_type: ContentType = ContentType.CENSORED
    missing_file: bool = False
    media_reference: bool = True
    old_metadata: bool = False
    unrelated_link: bool = False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        _Case("automatic", recover=True),
        _Case("current_path_moved", recover=True, current_path="/media/moved/FC2－4982441.mp4"),
        _Case("file_missing", recover=True, missing_file=True),
        _Case("manual_without_path", path=None),
        _Case("without_media_reference", media_reference=False),
        _Case("explicit_other_type", content_type=ContentType.WESTERN),
        _Case("canonical_forced_censored", number=_CANONICAL),
        _Case("short_fc", number="FC-123", path="/media/FC-123.mp4", current_path="/media/FC-123.mp4"),
        _Case("saved_path_conflict", path="/media/FC2-4982442.mp4"),
        _Case("current_path_conflict", current_path="/media/FC2-4982442.mp4"),
        _Case("saved_path_type_conflict", path="/media/欧美/FC2-4982441.mp4"),
        _Case("current_path_type_conflict", current_path="/media/欧美/FC2-4982441.mp4"),
        _Case("old_metadata_exists", old_metadata=True),
        _Case("linked_to_other_metadata", unrelated_link=True),
    ],
    ids=lambda case: case.name,
)
async def test_failed_file_task_retry_recovers_only_proven_fc2_alias(
    repo: Repository, resource_store: ResourceStore, case: _Case
) -> None:
    library = await repo.create_library(name="FC2", path="/media")
    assert library.id is not None
    linked = await repo.upsert_metadata("MIDV-123", title="Unrelated title") if case.unrelated_link else None
    media = (
        None
        if case.missing_file
        else await repo.create_media_file(
            library.id, path=case.current_path, metadata_id=linked.id if linked is not None else None
        )
    )
    if case.old_metadata:
        await repo.upsert_metadata(case.number, content_type=ContentType.CENSORED, title="Existing alias")
    media_id = (media.id if media is not None else 999) if case.media_reference else None
    original = ScrapePayload(
        number=case.number,
        content_type=case.content_type,
        media_file_id=media_id,
        path=case.path,
        use_cache={CacheKind.trans},
    )
    task = await repo.create_task(TaskType.SCRAPE, original, priority=7)
    assert task.id is not None
    await repo.fail_task(task.id, error="old scrape failure")
    failed = await repo.get_task(task.id)
    assert failed is not None
    (retry,) = await repo.retry_tasks([failed])
    assert retry.id is not None and retry.id != failed.id

    sources = _Sources(found=case.recover)
    handler = _handler(repo, resource_store, sources)
    payload = handler.parse_payload(retry.payload)
    before = payload.model_dump(mode="json")
    result = await handler.handle(payload)

    assert sources.routes == [["javarchive" if case.recover else "javdb"]]
    assert len(sources.queries) == 1
    query = sources.queries[0]
    assert (query.number, query.content_type) == (
        (_CANONICAL, ContentType.FC2) if case.recover else (case.number, case.content_type)
    )
    assert result.success is case.recover
    assert payload.model_dump(mode="json") == before == original.model_dump(mode="json")
    for task_id in (failed.id, retry.id):
        assert task_id is not None
        stored = await repo.get_task(task_id)
        assert stored is not None
        assert stored.payload == before and stored.priority == 7
    canonical = await repo.get_metadata_by_number(_CANONICAL)
    if case.recover:
        assert canonical is not None and canonical.content_type is ContentType.FC2
        assert canonical.title == "Fetched title"
        assert await repo.get_metadata_by_number(_ALIAS) is None
        if media is not None:
            assert media.id is not None
            current = await repo.get_media_file(media.id)
            assert current is not None and current.metadata_id == canonical.id
    else:
        assert canonical is None
        if linked is not None and media is not None:
            assert media.id is not None
            current = await repo.get_media_file(media.id)
            assert current is not None and current.metadata_id == linked.id
        if case.old_metadata:
            old = await repo.get_metadata_by_number(case.number)
            assert old is not None and old.title == "Existing alias"


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [False, True])
async def test_retry_reuses_canonical_metadata_and_preserves_its_locks(
    repo: Repository, resource_store: ResourceStore, linked: bool
) -> None:
    library = await repo.create_library(name="FC2", path="/media")
    assert library.id is not None
    canonical = await repo.upsert_metadata(
        _CANONICAL,
        content_type=ContentType.FC2,
        title="Manually locked title",
        studio="Stale studio",
    )
    assert canonical.id is not None
    await repo.set_metadata_locks(canonical.id, [MetadataField.TITLE])
    media = await repo.create_media_file(library.id, path=_PATH, metadata_id=canonical.id if linked else None)
    assert media.id is not None
    original = ScrapePayload(
        number=_ALIAS,
        content_type=ContentType.CENSORED,
        media_file_id=media.id,
        path=_PATH,
        use_cache={CacheKind.trans},
    )
    task = await repo.create_task(TaskType.SCRAPE, original)
    assert task.id is not None
    await repo.fail_task(task.id, error="old scrape failure")
    failed = await repo.get_task(task.id)
    assert failed is not None
    (retry,) = await repo.retry_tasks([failed])
    sources = _Sources(found=True)
    handler = _handler(repo, resource_store, sources)

    result = await handler.handle(handler.parse_payload(retry.payload))

    assert result.success
    assert sources.routes == [["javarchive"]]
    assert [(query.number, query.content_type) for query in sources.queries] == [(_CANONICAL, ContentType.FC2)]
    updated = await repo.get_metadata(canonical.id)
    assert updated is not None
    assert updated.title == "Manually locked title"
    assert updated.studio == "Fetched studio"
    assert updated.content_type is ContentType.FC2
    assert updated.locked_fields == [MetadataField.TITLE]
    assert await repo.get_metadata_by_number(_ALIAS) is None
    current = await repo.get_media_file(media.id)
    assert current is not None and current.metadata_id == canonical.id
