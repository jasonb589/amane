"""站点探测与刮削的会话一致性, 以及真实限制页的失败语义."""

from unittest.mock import AsyncMock

import pytest
from curl_cffi.requests import Response

from amane.config import SiteConfig
from amane.crawlers.base import Crawler
from amane.crawlers.http import HttpClient
from amane.crawlers.models import SearchQuery
from amane.crawlers.sites.caribbean import CaribbeanCrawler
from amane.crawlers.sites.kin8 import Kin8Crawler
from amane.net.connectivity import ConnectivityStatus
from amane.net.errors import FailureKind, FailureReason, RequestError, RequestFailure, SourceError
from amane.net.http import WebClient
from amane.parsing import ContentType

_MAINTENANCE = (
    "<title>メンテナンスのお知らせ</title>"
    "<p>現在、緊急メンテナンスでサービスを停止しています。</p>"
    "<h2>Service Temporarily Unavailable</h2><p>Offline for emergency maintenance. No ETA yet.</p>"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "query", "path", "page"),
    [
        (
            CaribbeanCrawler,
            SearchQuery("122524-001", content_type=ContentType.UNCENSORED),
            "/moviepages/122524-001/index.html",
            '<title>作品 | カリビアンコム</title><h1 itemprop="name">作品</h1>',
        ),
        (Kin8Crawler, SearchQuery("KIN8-0001"), "/movie/0001", '<meta property="og:title" content="作品">'),
    ],
)
async def test_fetch_forwards_configured_session_on_search_and_detail(
    source: type[Crawler], query: SearchQuery, path: str, page: str
) -> None:
    web = AsyncMock(spec=WebClient)
    web.get_text.return_value = page
    crawler = source(HttpClient(web), SiteConfig(base_url="https://mirror.example", cookie={"session": "provided"}))
    crawler.headers = {"X-Provided-Header": "provided"}

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.title == "作品"
    assert meta.source_url == f"https://mirror.example{path}"
    assert web.get_text.await_count == 2
    for call in web.get_text.await_args_list:
        assert call.args == (f"https://mirror.example{path}",)
        assert call.kwargs["cookies"] == {"session": "provided"}
        assert call.kwargs["headers"] == {"X-Provided-Header": "provided"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "body", "status", "reason"),
    [
        (CaribbeanCrawler, '<div id="driver-verify">Age Verification</div>', 200, FailureReason.AGE_VERIFICATION),
        (CaribbeanCrawler, "error code: 1010", 403, FailureReason.HTTP_ERROR),
        (Kin8Crawler, _MAINTENANCE, 403, FailureReason.SITE_MAINTENANCE),
        (Kin8Crawler, _MAINTENANCE, 200, FailureReason.SITE_MAINTENANCE),
    ],
)
async def test_connectivity_preserves_real_restriction_and_status(
    source: type[Crawler], body: str, status: int, reason: FailureReason
) -> None:
    web = AsyncMock(spec=WebClient)
    url = source.profile().base_url + ("/index2.htm" if source is CaribbeanCrawler else "")
    if status >= 400:
        web.request.side_effect = RequestError(
            url,
            RequestFailure(kind=FailureKind.HTTP_STATUS, message=f"HTTP {status}", status=status, body=body.encode()),
        )
    else:
        response = Response()
        response.status_code = status
        response.content = body.encode()
        web.request.return_value = response
    crawler = source(HttpClient(web), SiteConfig(cookie={"session": "provided"}))

    outcome = await crawler.check_connectivity()

    assert outcome.status is ConnectivityStatus.FAILED
    assert (outcome.reason, outcome.http_status, outcome.url) == (reason, status, url)
    web.request.assert_awaited_once_with("GET", url, cookies={"session": "provided"}, headers={}, max_attempts=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [None, "https://mirror.example/prefix/"])
async def test_caribbean_probe_uses_confirmed_catalog_entry(base: str | None) -> None:
    """根路径的年龄页不代表目录不可用; 镜像前缀和配置会话仍参与实际探测."""
    web = AsyncMock(spec=WebClient)
    configured = base.rstrip("/") if base else CaribbeanCrawler.profile().base_url
    catalog = f"{configured}/index2.htm"

    async def response(method: str, url: str, **kwargs: object) -> Response:
        result = Response()
        result.status_code = 200
        result.url = url
        result.content = b"<html>catalog</html>" if url == catalog else b"<title>Age Verification</title>"
        return result

    web.request.side_effect = response
    config = SiteConfig(base_url=base, cookie={"session": "provided"})
    crawler = CaribbeanCrawler(HttpClient(web), config)
    crawler.headers = {"X-Provided-Header": "provided"}

    outcome = await crawler.check_connectivity()

    assert (outcome.status, outcome.url, outcome.http_status) == (ConnectivityStatus.OK, catalog, 200)
    web.request.assert_awaited_once_with(
        "GET", catalog, cookies={"session": "provided"}, headers={"X-Provided-Header": "provided"}, max_attempts=1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["search", "detail"])
async def test_kin8_maintenance_title_cannot_become_movie_metadata(step: str) -> None:
    web = AsyncMock(spec=WebClient)
    web.get_text.return_value = _MAINTENANCE
    crawler = Kin8Crawler(HttpClient(web))

    with pytest.raises(SourceError) as error:
        if step == "search":
            await crawler._search(SearchQuery("KIN8-0001"))
        else:
            await crawler._scrape("https://www.kin8tengoku.com/movie/0001")

    assert error.value.reason is FailureReason.SITE_MAINTENANCE
    assert web.get_text.await_count == 1
