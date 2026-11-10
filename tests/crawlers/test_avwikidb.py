"""AVWikiDB 完整实页回放、番号一致性、单次请求与失败恢复."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, call

import pytest
from curl_cffi.requests import Response

from amane.aggregate import aggregate, compile_priority
from amane.config import SiteConfig
from amane.crawlers.factory import CrawlerFactory
from amane.crawlers.http import HttpClient
from amane.crawlers.models import SearchQuery
from amane.crawlers.sites.avwikidb import AvWikiDBCrawler
from amane.enums import BrowserMode
from amane.net.connectivity import ConnectivityStatus
from amane.net.errors import FailureReason, SourceError

from .driven import assert_expected, build_mock, call_step, load_toml

pytestmark = pytest.mark.asyncio
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "avwikidb"


@pytest.mark.parametrize("case", sorted(_FIXTURES.glob("*.toml")), ids=lambda path: path.stem)
async def test_captured_pages(case: Path, http_client: HttpClient, mock_web_client: AsyncMock) -> None:
    config = load_toml(case)["fetch"]
    build_mock(mock_web_client, case.parent, config["responses"])
    result = await call_step(AvWikiDBCrawler(http_client).fetch(SearchQuery(config["number"])), config)
    if config.get("expected_none"):
        assert result is None
    elif not config.get("expected_error"):
        assert result is not None
        assert_expected(result, config["expected"])
    assert mock_web_client.get_text.await_count == config["requests"]


@pytest.mark.parametrize(
    "number", ["", "123456", "FC2-1234567", "fc1234567", "ABC", "../START-654", "START-654?x=1", "START_654"]
)
async def test_invalid_or_unsupported_number_does_not_request(
    number: str, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    assert await AvWikiDBCrawler(http_client).fetch(SearchQuery(number)) is None
    mock_web_client.get_text.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "https://avwikidb.com.evil.example/work/START-654/",
        "https://avwikidb.com@evil.example/work/START-654/",
        "https://avwikidb.com/work/START-654/other",
        "https://avwikidb.com/work/%2FSTART-654/",
        "https://avwikidb.com/actor/START-654/",
        "file:///work/START-654/",
        "https://[invalid/work/START-654/",
    ],
)
async def test_unrelated_urls_do_not_request(url: str, http_client: HttpClient, mock_web_client: AsyncMock) -> None:
    assert await AvWikiDBCrawler(http_client).fetch_from_url(url) is None
    mock_web_client.get_text.assert_not_awaited()


async def test_search_redirect_reuses_detail_and_wrong_redirect_does_not_stop_fallback(
    http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    mock_web_client.get_text.side_effect = [
        SourceError(FailureReason.NOT_FOUND),
        (_FIXTURES / "start641.html").read_text(encoding="utf-8"),
        (_FIXTURES / "start654.html").read_text(encoding="utf-8"),
    ]
    result = await AvWikiDBCrawler(http_client).fetch(SearchQuery("START654"))
    assert result is not None and result.number == "START-654"
    assert [args.args[0] for args in mock_web_client.get_text.await_args_list] == [
        "https://avwikidb.com/work/START-654/",
        "https://avwikidb.com/search/?q=START-654",
        "https://avwikidb.com/search/?q=START654",
    ]


async def test_search_list_requires_matching_detail(http_client: HttpClient, mock_web_client: AsyncMock) -> None:
    mock_web_client.get_text.side_effect = [
        SourceError(FailureReason.NOT_FOUND),
        (_FIXTURES / "search-start654.html").read_text(encoding="utf-8"),
        (_FIXTURES / "start641.html").read_text(encoding="utf-8"),
    ]
    assert await AvWikiDBCrawler(http_client).fetch(SearchQuery("START-654")) is None
    assert mock_web_client.get_text.await_count == 3


async def test_missing_work_and_empty_search_remain_miss(http_client: HttpClient, mock_web_client: AsyncMock) -> None:
    empty = (_FIXTURES / "empty.html").read_text(encoding="utf-8")
    mock_web_client.get_text.side_effect = [SourceError(FailureReason.NOT_FOUND), empty, empty]
    assert await AvWikiDBCrawler(http_client).fetch(SearchQuery("ZZZZNOTREAL-999999")) is None
    assert mock_web_client.get_text.await_count == 3


@pytest.mark.parametrize("reason", [FailureReason.CLOUDFLARE_CHALLENGE, FailureReason.RATE_LIMITED])
async def test_source_failures_do_not_become_search_misses(
    reason: FailureReason, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    mock_web_client.get_text.side_effect = SourceError(reason)
    with pytest.raises(SourceError) as exc:
        await AvWikiDBCrawler(http_client).fetch(SearchQuery("START-654"))
    assert exc.value.reason is reason
    assert mock_web_client.get_text.await_count == 1


async def test_configured_mirror_uses_prefix_and_session_for_details_and_probe(
    http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    mock_web_client.get_text.return_value = (_FIXTURES / "mgs1434.html").read_text(encoding="utf-8")
    crawler = AvWikiDBCrawler(
        http_client, SiteConfig(base_url="https://mirror.example/avwiki/", cookie={"clearance": "user-cookie"})
    )
    crawler.headers["Referer"] = "https://mirror.example/avwiki/"
    assert crawler.serves_url("https://avwikidb.com/work/300MIUM-1434/")
    assert crawler.serves_url("https://mirror.example/avwiki/work/300MIUM-1434/")
    assert not crawler.serves_url("https://avwikidb.com/actor/300MIUM-1434/")
    result = await crawler.fetch_from_url("https://avwikidb.com/work/300MIUM-1434/?from=bookmark")
    assert result is not None and result.source_url == "https://mirror.example/avwiki/work/300MIUM-1434/"
    assert result.thumb_urls[1].startswith("https://mirror.example/avwiki/mgsimg/")
    response = Response()
    response.url = crawler.base_url
    response.status_code = 200
    response.content = (_FIXTURES / "mgs1434.html").read_bytes()
    mock_web_client.request.return_value = response
    outcome = await crawler.check_connectivity()
    assert outcome.status is ConnectivityStatus.OK
    assert mock_web_client.get_text.await_args_list == [
        call(
            "https://mirror.example/avwiki/work/300MIUM-1434/",
            cookies={"clearance": "user-cookie"},
            headers={"Referer": "https://mirror.example/avwiki/"},
            encoding="utf-8",
        ),
    ]
    mock_web_client.request.assert_awaited_once_with(
        "GET",
        "https://mirror.example/avwiki",
        cookies={"clearance": "user-cookie"},
        headers={"Referer": "https://mirror.example/avwiki/"},
        max_attempts=1,
    )
    mock_web_client.get_text.reset_mock()
    aggregated = await aggregate(
        SearchQuery("300MIUM-1434", url="https://avwikidb.com/work/300MIUM-1434/"),
        {"avwikidb": crawler},
        compile_priority(["avwikidb"], {}),
    )
    assert aggregated.metadata.title == result.title
    assert aggregated.metadata.source_urls == {"avwikidb": result.source_url}
    mock_web_client.get_text.assert_awaited_once()


def _page(movie: dict[str, object], *, dmm: dict[str, object] | None = None) -> str:
    props = {"movie": movie, "dmmData": dmm, "similarSection": {"movies": [{"adultVideoId": "START-654"}]}}
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": props}})
        + "</script>"
    )


@pytest.mark.parametrize(
    "movie",
    [
        {"adultVideoId": "START-641"},
        {"adultVideoId": "START-654", "_needsDisambiguation": True},
        {"adultVideoId": "START-654-extra"},
    ],
)
async def test_direct_detail_cannot_use_neighbors_or_disambiguation(
    movie: dict[str, object], http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    mock_web_client.get_text.return_value = _page(movie)
    assert await AvWikiDBCrawler(http_client).fetch_from_url("https://avwikidb.com/work/START-654/") is None


async def test_alias_keeps_complete_mgs_identity(http_client: HttpClient, mock_web_client: AsyncMock) -> None:
    mock_web_client.get_text.return_value = (_FIXTURES / "mgs1434.html").read_text(encoding="utf-8")
    crawler = AvWikiDBCrawler(http_client)
    assert await crawler.fetch_from_url("https://avwikidb.com/work/200MIUM-1434/") is None
    result = await crawler.fetch_from_url("https://avwikidb.com/work/MIUM-1434/")
    assert result is not None and result.number == "300MIUM-1434"


@pytest.mark.parametrize(
    ("floor", "number", "mgs_code", "requested", "expected"),
    [
        ("mgs", "MIUM-1434", "300MIUM-1434", "200MIUM-1434", None),
        ("mgs", "MIUM-1434", "300MIUM-1434", "MIUM-1434", "300MIUM-1434"),
        ("mgs", "MIUM-1434", "300MIUM-1434", "300MIUM-1434", "300MIUM-1434"),
        ("videoa", "START-659", "107START-659", "START-659", "START-659"),
        ("videoc", "ORECZ-734", "230ORECZ-734", "ORECZ-734", "ORECZ-734"),
    ],
)
async def test_missing_alias_uses_the_own_catalog_identity(
    floor: str,
    number: str,
    mgs_code: str,
    requested: str,
    expected: str | None,
    http_client: HttpClient,
    mock_web_client: AsyncMock,
) -> None:
    mock_web_client.get_text.return_value = _page(
        {"floor": floor, "adultVideoId": number, "adultVideoAlias": None, "mgsProductCode": mgs_code}
    )
    result = await AvWikiDBCrawler(http_client).fetch_from_url(f"https://avwikidb.com/work/{requested}/")
    assert (result.number if result is not None else None) == expected


@pytest.mark.parametrize(
    "text", ["<html>layout changed</html>", '<script id="__NEXT_DATA__">{</script>', _page({"adultVideoId": 654})]
)
async def test_bad_structured_data_reports_parse_error(
    text: str, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    mock_web_client.get_text.return_value = text
    with pytest.raises(SourceError) as exc:
        await AvWikiDBCrawler(http_client).fetch(SearchQuery("START-654"))
    assert exc.value.reason is FailureReason.PARSE_ERROR


async def test_nullable_relations_gender_and_sample_identity(
    http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    stem = "https://pics.dmm.co.jp/digital/video/1start00654/1start00654"
    mock_web_client.get_text.return_value = _page(
        {
            "adultVideoId": "START-654",
            "floor": "videoa",
            "fanzaContentId": "1start00654",
            "actor": [{"actor": {"name": "Actor A"}}, {"actor": None}],
            "histrion": [{"histrion": {"name": "Actor B"}}],
            "durationMin": -1,
            "genre": None,
            "summary": None,
            "sampleVideoBestUrl": "javascript:alert(1)",
        },
        dmm={
            "content_id": "1start00654",
            "sampleImageURL": {
                "sample_l": {"image": []},
                "sample_s": {"image": [f"{stem}-1.jpg", f"{stem}0-1.jpg", f"{stem}pl.jpg", "file:///x.jpg"]},
            },
        },
    )
    result = await AvWikiDBCrawler(http_client).fetch(SearchQuery("START-654"))
    assert result is not None
    assert [(actor.name, actor.gender.value) for actor in result.actors] == [("Actor A", "female"), ("Actor B", "male")]
    assert result.runtime is result.plot is None and result.tags == result.trailer_urls == []
    assert result.extrafanart == [
        stem.replace("https://pics.dmm.co.jp", "https://awsimgsrc.dmm.co.jp/pics_dig") + "-1.jpg"
    ]


async def test_factory_returns_native_source_without_plugins(http_client: HttpClient) -> None:
    factory = CrawlerFactory(http_client, {"avwikidb": SiteConfig(use_browser=BrowserMode.OFF)})
    crawler = await factory.get("avwikidb")
    assert isinstance(crawler, AvWikiDBCrawler)
    assert await factory.get("avwikidb") is crawler
