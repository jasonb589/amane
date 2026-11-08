"""JavArchive 的番号一致性、推荐隔离、镜像会话及失败上报."""

from html import escape
from unittest.mock import AsyncMock, MagicMock

import pytest

from amane.config import SiteConfig
from amane.crawlers.http import HttpClient
from amane.crawlers.models import SearchQuery
from amane.crawlers.sites.javarchive import JavArchiveCrawler
from amane.enums import BrowserBackendName, BrowserMode
from amane.net.browser import BrowserClient
from amane.net.errors import FailureKind, FailureReason, RequestError, RequestFailure, SourceError
from amane.net.http import WebClient

_PATH = "/1174735-FC2-PPV-1234567-example-pn.html"
_URL = f"https://javarchive.com{_PATH}"


def _entry(href: str = _PATH, title: str = "FC2-PPV-1234567 作品") -> str:
    return f'<li><h3><span><a href="{escape(href, quote=True)}">{escape(title)}</a></span></h3></li>'


def _search_page(entry: str) -> str:
    return (
        '<div class="news_section_left"><ul>'
        + _entry()
        + '</ul></div><div class="box_right_all"><div class="news_1n"><form></form></div>'
        + '<div class="news_1n"><ul>'
        + entry
        + '</ul></div><div class="category_news_phai_chinh"><ul>'
        + _entry()
        + "</ul></div></div>"
    )


def _detail_page(heading: str = "<h1>FC2-PPV-1234567 <span>作品</span></h1>", *, images: bool = True) -> str:
    media = """
        <div class="fisrst_sc"><img src="http://img2.javstore.net/images/1234567pl.jpg"></div>
        <div class="Recipepod" itemtype="http://schema.org/Recipe">
          <img itemprop="image" src="//img2.javstore.net/images/1234567ps.jpg">
          <div class="tag">Keywords: FC2-PPV-1234567 作品</div>
          <div itemprop="aggregateRating"><meta itemprop="ratingValue" content="3"></div>
        </div>
        <ul id="lightgallery" class="highslide-gallery">
          <li data-src="https://contents-thumbnail2.fc2.com/w1280/sample.jpg"><a href="javascript:;"><img src="/small.jpg"></a></li>
          <li data-src="https://contents-thumbnail2.fc2.com/w1280/sample.jpg"></li>
          <li data-src="/sample2.webp?size=1280"></li>
          <li data-src="javascript:alert(1)"></li>
          <li data-src="data:image/png;base64,AA"></li>
          <li data-src="https://user:secret@images.example/sample.jpg"></li>
          <li data-src="https://images.example/movie.mp4"></li>
        </ul>
        <a href="magnet:example">下载</a><video src="https://video.example/movie.mp4"></video>
        <aside><div class="Recipepod"><img itemprop="image" src="https://images.example/embedded.jpg"></div></aside>
    """
    return (
        '<div class="news_section_left"><h1>FC2-PPV-1234567 无关作品</h1></div>'
        '<div class="box_right_all"><div class="category_news_phai_chinh">'
        f'<div class="menudd">{heading}</div><div class="news">'
        '<p class="first_des">FC2-PPV-1234567 作品</p><time>2026/11/04</time>'
        + (media if images else "")
        + '</div><ul class="news_newer"><li><img src="https://images.example/recent.jpg"></li></ul></div>'
        '<div class="news_1n"><div class="Recipepod"><img itemprop="image" src="https://images.example/related.jpg"></div>'
        '<div class="fisrst_sc"><img src="https://images.example/related-cover.jpg"></div>'
        '<ul id="lightgallery"><li data-src="https://images.example/related-sample.jpg"></li></ul></div></div>'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("number", ["FC2-1234567", "fc2ppv1234567", "FC2_PPV_1234567", "FC2 PPV 1234567"])
async def test_fetch_requires_matching_main_result_and_detail(number: str) -> None:
    web = AsyncMock(spec=WebClient)
    web.get_text.side_effect = [_search_page(_entry()), _detail_page()]

    metadata = await JavArchiveCrawler(HttpClient(web)).fetch(SearchQuery(number))

    assert metadata is not None
    assert (metadata.number, metadata.title, metadata.source_url) == ("FC2-1234567", "作品", _URL)
    assert metadata.poster_urls == ["https://img2.javstore.net/images/1234567ps.jpg"]
    assert metadata.thumb_urls == ["http://img2.javstore.net/images/1234567pl.jpg"]
    assert metadata.extrafanart == [
        "https://contents-thumbnail2.fc2.com/w1280/sample.jpg",
        "https://javarchive.com/sample2.webp?size=1280",
    ]
    assert metadata.release is None and metadata.score is None and metadata.plot is None
    assert metadata.tags == metadata.trailer_urls == []
    assert [call.args[0] for call in web.get_text.await_args_list] == [
        "https://javarchive.com/search?q=FC2-PPV-1234567",
        _URL,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("number", ["", "1234567", "ABP-123", "FC2-1234567-extra", "FC2-PPV-", "FC2-1234567/2"])
async def test_non_fc2_queries_do_not_request(number: str) -> None:
    web = AsyncMock(spec=WebClient)

    assert await JavArchiveCrawler(HttpClient(web)).fetch(SearchQuery(number)) is None

    web.get_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        "",
        _entry(title="FC2-PPV-12345670 作品"),
        _entry(title="FC2-PPV-1234567a 作品"),
        _entry(title="推荐 FC2-PPV-1234567 作品"),
        _entry(_PATH.replace("1234567", "12345670")),
        _entry("https://unrelated.example" + _PATH),
        _entry("https://javarchive.com.unrelated.example" + _PATH),
        _entry("https://secret@javarchive.com" + _PATH),
        _entry("javascript:" + _PATH),
        _entry("https://javarchive.com:8443" + _PATH),
        _entry("/../" + _PATH.lstrip("/")),
        _entry(_PATH.replace("example", "example/%2e%2e/other")),
    ],
)
async def test_search_rejects_partial_number_and_unrelated_links(entry: str) -> None:
    web = AsyncMock(spec=WebClient)
    web.get_text.return_value = _search_page(entry)

    assert await JavArchiveCrawler(HttpClient(web)).fetch(SearchQuery("FC2-1234567")) is None

    assert web.get_text.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "heading",
    [
        "",
        "<h1>FC2-PPV-12345670 作品</h1>",
        "<h1>FC2-PPV-1234567a 作品</h1>",
        "<h1>其他作品 FC2-PPV-1234567</h1>",
        "<h1>FC2-PPV-1234567</h1>",
        "<h1>FC2-PPV-1234567 作品</h1><h1>FC2-PPV-7654321 其他作品</h1>",
    ],
)
async def test_detail_requires_unique_main_heading_with_url_identity(heading: str) -> None:
    web = AsyncMock(spec=WebClient)
    web.get_text.return_value = _detail_page(heading)

    assert await JavArchiveCrawler(HttpClient(web)).fetch_from_url(_URL) is None

    assert web.get_text.await_count == 1


@pytest.mark.asyncio
async def test_missing_main_images_does_not_import_recommendations() -> None:
    web = AsyncMock(spec=WebClient)
    web.get_text.return_value = _detail_page(images=False)

    metadata = await JavArchiveCrawler(HttpClient(web)).fetch_from_url(_URL)

    assert metadata is not None
    assert metadata.title == "作品"
    assert metadata.poster_urls == metadata.thumb_urls == metadata.extrafanart == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://unrelated.example" + _PATH,
        "https://javarchive.com/search?q=FC2-PPV-1234567",
        "https://javarchive.com/1234567.html",
        "https://user:secret@javarchive.com" + _PATH,
        "https://javarchive.com" + _PATH.replace("example", "example/../other"),
        "file:///1174735-FC2-PPV-1234567-example-pn.html",
    ],
)
async def test_invalid_detail_urls_do_not_request(url: str) -> None:
    web = AsyncMock(spec=WebClient)

    assert await JavArchiveCrawler(HttpClient(web)).fetch_from_url(url) is None

    web.get_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "href",
    [_PATH, _PATH.lstrip("/"), _URL, "//www.javarchive.com" + _PATH, "https://mirror.example/prefix" + _PATH],
)
async def test_mirror_path_prefix_and_session_apply_to_search_and_detail(href: str) -> None:
    web = AsyncMock(spec=WebClient)
    web.get_text.side_effect = [_search_page(_entry(href)), _detail_page()]
    config = SiteConfig(base_url="https://mirror.example/prefix/", cookie={"session": "provided"})
    crawler = JavArchiveCrawler(HttpClient(web).for_source("javarchive", config), config)
    crawler.headers = {"X-Provided-Header": "provided"}
    assert crawler.serves_url(_URL)
    assert crawler.serves_url("https://mirror.example/prefix" + _PATH)
    assert not crawler.serves_url("https://unrelated.example" + _PATH)

    metadata = await crawler.fetch(SearchQuery("FC2-1234567"))

    assert metadata is not None and metadata.source_url == "https://mirror.example/prefix" + _PATH
    assert [call.args[0] for call in web.get_text.await_args_list] == [
        "https://mirror.example/prefix/search?q=FC2-PPV-1234567",
        "https://mirror.example/prefix" + _PATH,
    ]
    for call in web.get_text.await_args_list:
        assert call.kwargs["cookies"] == {"session": "provided"}
        assert call.kwargs["headers"] == {"X-Provided-Header": "provided"}
    assert metadata.thumb_urls == ["http://img2.javstore.net/images/1234567pl.jpg"]
    assert metadata.extrafanart == [
        "https://contents-thumbnail2.fc2.com/w1280/sample.jpg",
        "https://mirror.example/prefix/sample2.webp?size=1280",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["FC2PPV", "fc2-ppv", "FC2"])
async def test_site_number_forms_and_slashes_in_title_slug(form: str) -> None:
    web = AsyncMock(spec=WebClient)
    path = _PATH.replace("FC2-PPV", form).replace("example", "example/part")
    web.get_text.side_effect = [_search_page(_entry(path, f"{form}-1234567 作品")), _detail_page()]

    metadata = await JavArchiveCrawler(HttpClient(web)).fetch(SearchQuery("FC2-1234567"))

    assert metadata is not None
    assert metadata.source_url == "https://javarchive.com" + path


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [BrowserMode.ALWAYS, BrowserMode.AUTO])
async def test_configured_browser_renders_search_and_detail(mode: BrowserMode) -> None:
    web = AsyncMock(spec=WebClient)
    web.get_text.return_value = "<title>Just a moment...</title><script>_cf_chl_opt={}</script>"
    browser = AsyncMock(spec=BrowserClient)
    browser.resolve = MagicMock(return_value=BrowserBackendName.SOLVER)
    browser.get_page.side_effect = [(_search_page(_entry()), None), (_detail_page(), None)]
    config = SiteConfig(use_browser=mode, cookie={"session": "provided"})
    crawler = JavArchiveCrawler(HttpClient(web, browser).for_source("javarchive", config), config)

    metadata = await crawler.fetch(SearchQuery("FC2-1234567"))

    assert metadata is not None and metadata.number == "FC2-1234567"
    assert browser.get_page.await_count == 2
    assert web.get_text.await_count == (1 if mode is BrowserMode.AUTO else 0)
    for call in browser.get_page.await_args_list:
        assert call.kwargs["scope"] == "javarchive"
        assert call.kwargs["cookies"] == {"session": "provided"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["search", "detail"])
@pytest.mark.parametrize(
    ("body", "status", "reason"),
    [
        ("", 200, FailureReason.EMPTY_RESPONSE),
        ("<script>_cf_chl_opt={}</script>", 200, FailureReason.CLOUDFLARE_CHALLENGE),
        ("access denied", 403, FailureReason.HTTP_ERROR),
        ("service unavailable", 503, FailureReason.SERVER_ERROR),
    ],
)
async def test_transport_and_challenge_failures_propagate(
    stage: str, body: str, status: int, reason: FailureReason
) -> None:
    web = AsyncMock(spec=WebClient)
    if status >= 400:
        web.get_text.side_effect = RequestError(
            _URL,
            RequestFailure(kind=FailureKind.HTTP_STATUS, status=status, message=f"HTTP {status}", body=body.encode()),
        )
    else:
        web.get_text.return_value = body
    crawler = JavArchiveCrawler(HttpClient(web, browser_mode=BrowserMode.OFF))

    with pytest.raises(SourceError) as error:
        if stage == "search":
            await crawler.fetch(SearchQuery("FC2-1234567"))
        else:
            await crawler.fetch_from_url(_URL)

    assert error.value.reason is reason
    assert web.get_text.await_count == 1
