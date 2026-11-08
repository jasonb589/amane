"""FC2PPV-DB 的原始页面回归与请求隔离契约.

完整采集响应随仓库提供, 用例继续采用共用 TOML 运行器的路由和字段断言.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, call

import pytest
from curl_cffi.requests import Response

from amane.config import SiteConfig
from amane.crawlers.http import HttpClient
from amane.crawlers.models import SearchQuery
from amane.crawlers.sites.fc2ppv_db import FC2PPVDBIndexCrawler
from amane.net.connectivity import ConnectivityStatus
from amane.net.errors import FailureReason, SourceError

from .driven import assert_expected, build_mock, call_step, load_toml

pytestmark = pytest.mark.asyncio

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fc2ppv_db"


@pytest.mark.parametrize("case", sorted(_FIXTURES.glob("*.toml")), ids=lambda path: path.stem)
async def test_captured_pages(case: Path, http_client: HttpClient, mock_web_client: AsyncMock) -> None:
    config = load_toml(case)["fetch"]
    build_mock(mock_web_client, case.parent, config["responses"])
    crawler = FC2PPVDBIndexCrawler(http_client)
    result = await call_step(crawler.fetch(SearchQuery(config["number"])), config)
    if config.get("expected_error"):
        return
    if config.get("expected_none"):
        assert result is None
    else:
        assert result is not None
        assert_expected(result, config["expected"])
    assert mock_web_client.get_text.await_count == 1


@pytest.mark.parametrize("number", ["", "4778286", "ABP-4778286", "FC2-4778286-extra", "FC2-PPV-4778286?next=1"])
async def test_unrelated_identifiers_do_not_request(
    number: str, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    crawler = FC2PPVDBIndexCrawler(http_client)
    assert await crawler.fetch(SearchQuery(number)) is None
    assert await crawler._search(SearchQuery(number)) is None
    mock_web_client.get_text.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "https://fc2ppvdb.com/articles/4778286",
        "https://fc2ppv-db.com.evil.example/ja/videos/4778286",
        "https://fc2ppv-db.com@evil.example/ja/videos/4778286",
        "https://fc2ppv-db.com/ja/videos/4778286/details",
        "https://fc2ppv-db.com/ja/videos/4778286-extra",
        "https://fc2ppv-db.com/ja/videos/not-a-number",
        "https://fc2ppv-db.com/ja/actresses/4778286",
        "https://fc2ppv-db.com/ja/search?q=4778286",
        "file:///ja/videos/4778286",
        "https://[invalid/ja/videos/4778286",
    ],
)
async def test_other_routes_do_not_request(url: str, http_client: HttpClient, mock_web_client: AsyncMock) -> None:
    crawler = FC2PPVDBIndexCrawler(http_client)
    assert await crawler.fetch_from_url(url) is None
    mock_web_client.get_text.assert_not_awaited()


@pytest.mark.parametrize(
    ("base_url", "entry"),
    [
        ("https://mirror.example/fc2-db/", "https://mirror.example/fc2-db/ja"),
        ("https://mirror.example/fc2-db/ja/", "https://mirror.example/fc2-db/ja"),
        ("https://mirror.example/fc2-db/en/", "https://mirror.example/fc2-db/en"),
        ("https://fc2ppv-db.com/en/", "https://fc2ppv-db.com/en"),
    ],
)
async def test_configured_prefix_cookie_and_headers(
    base_url: str, entry: str, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    mock_web_client.get_text.return_value = (_FIXTURES / "fc2ppv_db.html").read_text(encoding="utf-8")
    crawler = FC2PPVDBIndexCrawler(
        http_client,
        SiteConfig(base_url=base_url, cookie={"cf_clearance": "user-clearance", "age-verified": "user-age"}),
    )
    crawler.headers["User-Agent"] = "configured-agent"
    assert crawler.serves_url("https://fc2ppv-db.com/ja/videos/4778286")
    assert crawler.serves_url(f"{entry}/videos/4778286")
    assert not crawler.serves_url("https://fc2ppvdb.com/articles/4778286")
    result = await crawler.fetch(SearchQuery("FC2-4778286"))
    assert result is not None
    assert result.source_url == f"{entry}/videos/4778286"
    official_result = await crawler.fetch_from_url("https://fc2ppv-db.com/en/videos/4778286?from=bookmark")
    assert official_result == result
    expected_call = call(
        f"{entry}/videos/4778286",
        cookies={"cf_clearance": "user-clearance", "age-verified": "user-age"},
        headers={"User-Agent": "configured-agent"},
        encoding="utf-8",
    )
    assert mock_web_client.get_text.await_args_list == [expected_call, expected_call]


@pytest.mark.parametrize(
    ("base_url", "entry", "file", "status"),
    [
        (None, "https://fc2ppv-db.com/ja", "fc2ppv_db.html", ConnectivityStatus.OK),
        (
            "https://mirror.example/index",
            "https://mirror.example/index/ja",
            "cloudflare.html",
            ConnectivityStatus.FAILED,
        ),
    ],
)
async def test_connectivity_uses_the_same_entry_and_credentials(
    base_url: str | None,
    entry: str,
    file: str,
    status: ConnectivityStatus,
    http_client: HttpClient,
    mock_web_client: AsyncMock,
) -> None:
    crawler = FC2PPVDBIndexCrawler(http_client, SiteConfig(base_url=base_url, cookie={"session": "configured"}))
    crawler.headers["User-Agent"] = "configured-agent"
    response = Response()
    response.url = entry
    response.status_code = 200 if status is ConnectivityStatus.OK else 403
    response.content = (_FIXTURES / file).read_bytes()
    mock_web_client.request.return_value = response
    result = await crawler.check_connectivity()
    assert result.status is status
    assert result.reason is (None if status is ConnectivityStatus.OK else FailureReason.CLOUDFLARE_CHALLENGE)
    mock_web_client.request.assert_awaited_once_with(
        "GET",
        entry,
        cookies={"age-verified": "true", "session": "configured"},
        headers={"User-Agent": "configured-agent"},
        max_attempts=1,
    )


async def test_search_confirms_identity_without_retaining_response(
    http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    mock_web_client.get_text.side_effect = [
        (_FIXTURES / "fc2ppv_db.html").read_text(encoding="utf-8"),
        (_FIXTURES / "fc2ppv_db_sparse.html").read_text(encoding="utf-8"),
    ]
    crawler = FC2PPVDBIndexCrawler(http_client)
    query = SearchQuery("FC2-4778286")
    assert await crawler._search(query) == "https://fc2ppv-db.com/ja/videos/4778286"
    assert await crawler.fetch(query) is None
    assert mock_web_client.get_text.await_count == 2


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        (FailureReason.NOT_FOUND, 404),
        (FailureReason.CLOUDFLARE_CHALLENGE, 403),
        (FailureReason.CLOUDFLARE_CHALLENGE, 404),
        (FailureReason.RATE_LIMITED, 429),
        (FailureReason.SERVER_ERROR, 503),
    ],
)
async def test_only_missing_product_is_empty(
    reason: FailureReason, status: int, http_client: HttpClient, mock_web_client: AsyncMock
) -> None:
    failure = SourceError(reason, http_status=status, detail="source failure")
    mock_web_client.get_text.side_effect = failure
    crawler = FC2PPVDBIndexCrawler(http_client)
    if reason is FailureReason.NOT_FOUND:
        assert await crawler.fetch(SearchQuery("FC2-4778286")) is None
    else:
        with pytest.raises(SourceError) as error:
            await crawler.fetch(SearchQuery("FC2-4778286"))
        assert error.value is failure


async def test_concurrent_queries_keep_their_own_page(http_client: HttpClient, mock_web_client: AsyncMock) -> None:
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    async def response(url: str, **kwargs: object) -> str:
        if url.endswith("/4778286"):
            first_started.set()
            await asyncio.wait_for(second_started.wait(), timeout=2)
            return (_FIXTURES / "fc2ppv_db.html").read_text(encoding="utf-8")
        second_started.set()
        await asyncio.wait_for(first_started.wait(), timeout=2)
        return (_FIXTURES / "fc2ppv_db_sparse.html").read_text(encoding="utf-8")

    mock_web_client.get_text.side_effect = response
    crawler = FC2PPVDBIndexCrawler(http_client)
    first, second = await asyncio.gather(
        crawler.fetch(SearchQuery("FC2-4778286")), crawler.fetch(SearchQuery("FC2-PPV-3061625"))
    )
    assert first is not None and second is not None
    assert (first.number, first.runtime) == ("FC2-4778286", 52)
    assert (second.number, second.runtime) == ("FC2-3061625", None)
    assert first.poster_urls != second.poster_urls
    assert mock_web_client.get_text.await_count == 2
