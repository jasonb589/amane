"""素人海报来源例外经真实聚合、资源选择与持久化生效."""

from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal, cast
from unittest.mock import AsyncMock

import pytest

from amane.aggregate import CrawlerLike
from amane.config import HotSettings
from amane.crawlers.base import Crawler
from amane.crawlers.models import MediaMetadata
from amane.enums import DownloadableResource, MetadataField, SiteName
from amane.handlers import CacheKind, ScrapeHandler, ScrapePayload
from amane.media.provenance import resolve_image_provenance
from amane.parsing import ContentType
from tests.media.test_pipeline import FakeClient

if TYPE_CHECKING:
    from amane.db.repository import Repository
    from amane.media import ResourceStore
    from amane.net.http import WebClient


class _PosterSources:
    def __init__(self, metadata: dict[str, MediaMetadata | None]) -> None:
        self.sources: dict[str, CrawlerLike] = {}
        for site, result in metadata.items():
            crawler = AsyncMock(spec=Crawler)
            crawler.fetch.return_value = result
            self.sources[site] = crawler

    async def get_crawlers(self, names: Iterable[str]) -> dict[str, CrawlerLike]:
        return {name: self.sources[name] for name in names if name in self.sources}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "mgstage_result", "download"),
    [
        (ContentType.AMATEUR, "valid", "poster"),
        (ContentType.AMATEUR, "valid", "both"),
        (ContentType.AMATEUR, "valid", "none"),
        (ContentType.AMATEUR, "missing", "poster"),
        (ContentType.AMATEUR, "failed", "poster"),
        (ContentType.AMATEUR, "unreadable", "poster"),
        (ContentType.AMATEUR, "truncated", "poster"),
        (ContentType.AMATEUR, "empty", "poster"),
        (ContentType.CENSORED, "valid", "poster"),
    ],
)
async def test_poster_source_exception_and_quality_fallback(
    repo: Repository,
    resource_store: ResourceStore,
    content_type: ContentType,
    mgstage_result: Literal["valid", "missing", "failed", "unreadable", "truncated", "empty"],
    download: Literal["poster", "both", "none"],
) -> None:
    number = "300MIUM-1382"
    mgstage = "https://images.example/mgstage.jpg"
    alternate = "https://images.example/alternate.jpg"
    cover = "https://images.example/cover.jpg"
    mgstage_metadata = MediaMetadata(number=number, poster_urls=[] if mgstage_result == "empty" else [mgstage])
    factory = _PosterSources(
        {
            "mgstage": None if mgstage_result == "missing" else mgstage_metadata,
            "avbase": MediaMetadata(number=number, poster_urls=[alternate], thumb_urls=[cover]),
        }
    )
    sizes = {alternate: (1000, 1500), cover: (2400, 1600)}
    if mgstage_result != "unreadable":
        sizes[mgstage] = (422, 600)
    client = FakeClient(sizes, fail={mgstage} if mgstage_result == "failed" else set())
    if mgstage_result == "truncated":
        saved = await resource_store.acquire(mgstage, cast("WebClient", client))
        assert saved is not None
        # 保留文件头与尺寸, 删除图像数据尾部.
        saved.write_bytes(saved.read_bytes()[:800])
        client.downloaded.clear()
    config = HotSettings()
    config.scraping.content_routes[content_type] = [SiteName.AVBASE, SiteName.MGSTAGE]
    config.scraping.field_priority[MetadataField.POSTER_URLS] = [SiteName.AVBASE]
    config.scraping.download_resources = [] if download == "none" else [DownloadableResource.poster]
    if download == "both":
        config.scraping.download_resources.append(DownloadableResource.thumb)
    handler = ScrapeHandler(repo, factory, resource_store, config, cast("WebClient", client))

    result = await handler.handle(ScrapePayload(number=number, content_type=content_type))

    assert result.success
    metadata = await repo.get_metadata_by_number(number)
    assert metadata is not None
    preferred = content_type is ContentType.AMATEUR and mgstage_result == "valid"
    assert metadata.poster_urls[0] == (mgstage if preferred else alternate)
    if preferred:
        assert metadata.poster_urls == [mgstage, alternate]
        expected_downloads = {"poster": [mgstage], "both": [cover, mgstage], "none": []}
        assert client.downloaded == expected_downloads[download]
        provenance = await resolve_image_provenance(mgstage, resource_store, metadata.raw)
        assert provenance.source == "mgstage" and not provenance.cropped
    else:
        assert alternate in client.downloaded
    assert metadata.raw["avbase"]["poster_urls"] == [alternate]


@pytest.mark.asyncio
@pytest.mark.parametrize("exclusion", ["blacklist", "route", "unavailable", "locked"])
async def test_amateur_preference_obeys_source_eligibility_and_field_lock(
    repo: Repository, resource_store: ResourceStore, exclusion: str
) -> None:
    number = "300MIUM-1382"
    mgstage = "https://images.example/mgstage.jpg"
    alternate = "https://images.example/alternate.jpg"
    manual = "https://images.example/manual.jpg"
    mgstage_metadata = MediaMetadata(number=number, poster_urls=[mgstage])
    original = await repo.upsert_metadata(
        number=number,
        content_type=ContentType.AMATEUR,
        poster_urls=[manual],
        raw={"mgstage": mgstage_metadata.model_dump(mode="json")},
    )
    assert original.id is not None
    if exclusion == "locked":
        await repo.set_metadata_locks(original.id, [MetadataField.POSTER_URLS])
    factory = _PosterSources(
        {"avbase": MediaMetadata(number=number, poster_urls=[alternate]), "mgstage": mgstage_metadata}
    )
    if exclusion == "unavailable":
        factory.sources.pop("mgstage")
    config = HotSettings()
    config.scraping.content_routes[ContentType.AMATEUR] = (
        [SiteName.AVBASE] if exclusion == "route" else [SiteName.AVBASE, SiteName.MGSTAGE]
    )
    if exclusion == "blacklist":
        config.scraping.field_blacklist[MetadataField.POSTER_URLS] = [SiteName.MGSTAGE]
    config.scraping.download_resources = [DownloadableResource.poster]
    client = FakeClient({mgstage: (422, 600), alternate: (1000, 1500)})
    handler = ScrapeHandler(repo, factory, resource_store, config, cast("WebClient", client))

    result = await handler.handle(
        ScrapePayload(number=number, content_type=ContentType.AMATEUR, use_cache={CacheKind.metadata})
    )

    assert result.success
    metadata = await repo.get_metadata_by_number(number)
    assert metadata is not None
    assert metadata.poster_urls[0] == (manual if exclusion == "locked" else alternate)
    if exclusion != "locked":
        assert mgstage not in client.downloaded
    assert metadata.raw["mgstage"]["poster_urls"] == [mgstage]
