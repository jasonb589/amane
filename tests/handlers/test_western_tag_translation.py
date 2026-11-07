"""欧美标签独立翻译的真实刮削、聚合与持久化边界."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from amane.aggregate import CrawlerLike
from amane.config import HotSettings
from amane.crawlers.models import FetchOptions, FilmActor, MediaMetadata, SearchQuery
from amane.enums import Language, MetadataField, SiteName
from amane.handlers import CacheKind, ScrapeHandler, ScrapePayload
from amane.parsing import ContentType

if TYPE_CHECKING:
    from amane.db.repository import Repository
    from amane.media import ResourceStore


class _Source:
    def __init__(self) -> None:
        self.data = MediaMetadata(
            number="TEST-001",
            title="Original title",
            plot="Original plot",
            actors=[FilmActor(name="Original Actor")],
            studio="Original Studio",
            tags=["Drama", "Action"],
        )
        self.queries: list[SearchQuery] = []

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata:
        self.queries.append(query)
        return self.data.model_copy(update={"number": query.number}, deep=True)

    async def fetch_from_url(self, url: str, options: FetchOptions | None = None) -> MediaMetadata:
        return self.data.model_copy(deep=True)

    def serves_url(self, url: str) -> bool:
        return False


class _Factory:
    def __init__(self, source: _Source) -> None:
        self.source = source

    async def get_crawlers(self, names: Iterable[str]) -> dict[str, CrawlerLike]:
        return {str(name): self.source for name in names if name == SiteName.JAVDB}


class _Translator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Language, MetadataField, bool]] = []
        self.failures: set[str] = set()
        self.empty: set[str] = set()

    async def translate(
        self, text: str, target: Language, field: MetadataField, *, use_cache: bool = True
    ) -> str | None:
        self.calls.append((text, target, field, use_cache))
        if text in self.failures:
            raise RuntimeError("translation unavailable")
        if text in self.empty:
            return None
        return f"{target}:{text}"


@dataclass
class _Case:
    config: HotSettings
    source: _Source
    translator: _Translator
    handler: ScrapeHandler


@pytest.fixture
def translation_case(repo: Repository, resource_store: ResourceStore) -> _Case:
    config = HotSettings()
    config.scraping.content_routes = {content_type: [SiteName.JAVDB] for content_type in ContentType}
    config.scraping.field_language = {
        MetadataField.TITLE: Language.ZH_TW,
        MetadataField.PLOT: Language.ZH_TW,
        MetadataField.TAGS: Language.JP,
    }
    config.llm.translate_western_tags = True
    source = _Source()
    translator = _Translator()
    handler = ScrapeHandler(
        repo=repo,
        factory=_Factory(source),
        resource_store=resource_store,
        pipeline_config=config,
        translator=translator,
    )
    return _Case(config=config, source=source, translator=translator, handler=handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", list(ContentType))
async def test_independent_switch_only_translates_western_tags(
    repo: Repository, translation_case: _Case, content_type: ContentType
) -> None:
    case = translation_case
    assert case.config.llm.enabled is False
    assert MetadataField.TAGS not in case.config.llm.translate_fields
    result = await case.handler.handle(ScrapePayload(number="TEST-001", content_type=content_type))
    assert result.success
    metadata = await repo.get_metadata_by_number("TEST-001")
    assert metadata is not None
    expected_tags = ["zh_cn:Drama", "zh_cn:Action"] if content_type is ContentType.WESTERN else ["Drama", "Action"]
    assert metadata.tags == expected_tags
    assert metadata.title == "Original title"
    assert metadata.plot == "Original plot"
    assert metadata.actors == ["Original Actor"]
    assert metadata.studio == "Original Studio"
    assert metadata.raw["javdb"]["tags"] == ["Drama", "Action"]
    assert metadata.field_sources["tags"] == "javdb"
    assert metadata.content_type is content_type
    assert [call[2] for call in case.translator.calls] == (
        [MetadataField.TAGS, MetadataField.TAGS] if content_type is ContentType.WESTERN else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("global_enabled", "western_enabled", "content_type", "target"),
    [
        (False, False, ContentType.WESTERN, None),
        (False, True, ContentType.WESTERN, Language.ZH_TW),
        (True, True, ContentType.WESTERN, Language.ZH_TW),
        (True, False, ContentType.WESTERN, Language.JP),
        (True, True, ContentType.CENSORED, Language.JP),
    ],
)
async def test_western_target_has_priority_without_double_translation(
    repo: Repository,
    translation_case: _Case,
    global_enabled: bool,
    western_enabled: bool,
    content_type: ContentType,
    target: Language | None,
) -> None:
    case = translation_case
    case.config.llm.enabled = global_enabled
    case.config.llm.translate_western_tags = western_enabled
    case.config.llm.western_tags_language = Language.ZH_TW
    case.config.llm.translate_fields = [MetadataField.TITLE, MetadataField.PLOT, MetadataField.TAGS]
    result = await case.handler.handle(ScrapePayload(number="TEST-001", content_type=content_type))
    assert result.success
    metadata = await repo.get_metadata_by_number("TEST-001")
    assert metadata is not None
    expected_tags = [f"{target}:{tag}" for tag in case.source.data.tags] if target else case.source.data.tags
    assert metadata.tags == expected_tags
    tag_calls = [call for call in case.translator.calls if call[2] is MetadataField.TAGS]
    expected_calls = [(tag, target, MetadataField.TAGS, True) for tag in case.source.data.tags] if target else []
    assert tag_calls == expected_calls
    assert metadata.title == ("zh_tw:Original title" if global_enabled else "Original title")
    assert metadata.plot == ("zh_tw:Original plot" if global_enabled else "Original plot")
    assert metadata.actors == ["Original Actor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("use_translation_cache", [False, True])
async def test_tag_failures_preserve_originals_and_raw_and_forward_cache_policy(
    repo: Repository, translation_case: _Case, use_translation_cache: bool
) -> None:
    case = translation_case
    case.source.data.tags = ["Drama", "Unavailable", "Empty", "Action"]
    case.translator.failures = {"Unavailable"}
    case.translator.empty = {"Empty"}
    cache = {CacheKind.trans} if use_translation_cache else set()
    result = await case.handler.handle(
        ScrapePayload(number="TEST-001", content_type=ContentType.WESTERN, use_cache=cache)
    )
    assert result.success
    metadata = await repo.get_metadata_by_number("TEST-001")
    assert metadata is not None
    assert metadata.tags == ["zh_cn:Drama", "Unavailable", "Empty", "zh_cn:Action"]
    assert metadata.raw["javdb"]["tags"] == ["Drama", "Unavailable", "Empty", "Action"]
    assert len(case.translator.calls) == 4
    assert all(call[3] is use_translation_cache for call in case.translator.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("global_enabled", [False, True])
async def test_locked_tags_skip_llm_and_keep_manual_values_and_source(
    repo: Repository, translation_case: _Case, global_enabled: bool
) -> None:
    case = translation_case
    metadata = await repo.upsert_metadata(
        "TEST-001", content_type=ContentType.WESTERN, tags=["Manual"], field_sources={"tags": "manual-source"}
    )
    assert metadata.id is not None
    await repo.set_metadata_locks(metadata.id, [MetadataField.TAGS])
    case.config.llm.enabled = global_enabled
    case.config.llm.translate_fields = [MetadataField.TAGS]
    result = await case.handler.handle(
        ScrapePayload(number="TEST-001", content_type=ContentType.WESTERN, use_cache=set())
    )
    assert result.success
    updated = await repo.get_metadata(metadata.id)
    assert updated is not None
    assert updated.tags == ["Manual"]
    assert updated.field_sources["tags"] == "manual-source"
    assert updated.locked_fields == ["tags"]
    assert updated.raw["javdb"]["tags"] == ["Drama", "Action"]
    assert case.translator.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_type", "payload_type", "translated"),
    [
        (ContentType.WESTERN, ContentType.CENSORED, True),
        (ContentType.CENSORED, ContentType.WESTERN, False),
    ],
)
async def test_existing_type_governs_translation_even_without_metadata_cache(
    repo: Repository,
    translation_case: _Case,
    stored_type: ContentType,
    payload_type: ContentType,
    translated: bool,
) -> None:
    case = translation_case
    metadata = await repo.upsert_metadata(
        "TEST-001",
        content_type=stored_type,
        tags=["Stale"],
        raw={"javdb": {"number": "TEST-001", "title": "Cached title", "tags": ["Cached tag"]}},
    )
    assert metadata.id is not None
    result = await case.handler.handle(
        ScrapePayload(number="test-001", content_type=payload_type, use_cache={CacheKind.trans})
    )
    assert result.success
    updated = await repo.get_metadata(metadata.id)
    assert updated is not None
    assert updated.content_type is stored_type
    assert updated.tags == (["zh_cn:Drama", "zh_cn:Action"] if translated else ["Drama", "Action"])
    assert updated.title == "Original title"
    assert updated.raw["javdb"]["tags"] == ["Drama", "Action"]
    assert len(case.source.queries) == 1
    assert case.source.queries[0].content_type is payload_type
    assert [call[0] for call in case.translator.calls] == (["Drama", "Action"] if translated else [])


@pytest.mark.asyncio
async def test_western_tags_keep_vr_marker_and_sql_filter(repo: Repository, translation_case: _Case) -> None:
    case = translation_case
    case.source.data.tags = ["VR", "8kVr", "vr専用", "Drama"]
    result = await case.handler.handle(ScrapePayload(number="TEST-001", content_type=ContentType.WESTERN))
    assert result.success
    metadata = await repo.get_metadata_by_number("TEST-001")
    assert metadata is not None and metadata.vr
    assert metadata.tags == ["VR", "8kVr", "vr専用", "zh_cn:Drama"]
    assert case.translator.calls == [("Drama", Language.ZH_CN, MetadataField.TAGS, True)]
    filtered, total = await repo.list_metadata(vr=True)
    assert total == 1 and [item.id for item in filtered] == [metadata.id]


@pytest.mark.asyncio
async def test_missing_translator_keeps_western_tags(repo: Repository, resource_store: ResourceStore) -> None:
    config = HotSettings()
    config.llm.translate_western_tags = True
    config.scraping.content_routes = {ContentType.WESTERN: [SiteName.JAVDB]}
    source = _Source()
    handler = ScrapeHandler(repo, _Factory(source), resource_store, config)
    result = await handler.handle(ScrapePayload(number="TEST-001", content_type=ContentType.WESTERN))
    assert result.success
    metadata = await repo.get_metadata_by_number("TEST-001")
    assert metadata is not None and metadata.tags == ["Drama", "Action"]
