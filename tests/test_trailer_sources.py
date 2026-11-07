"""预告片抽取: mgstage 回放器 / works 主题站 (WP media) / 厂牌官网 (<video src>).

三家站点的预告片都不在详情页的字段表里, 拿法各不相同, 因此各留一个用例:
mgstage 要多问一次回放器, works 站要多查一次附件, 官网直接内嵌.
"""

import asyncio
from typing import Any, cast

from amane.crawlers.sites.dahlia import DahliaCrawler
from amane.crawlers.sites.mgstage import MGStageCrawler
from amane.crawlers.sites.official import OfficialCrawler

TRAILER = "https://example.test/sample.mp4"


class _StubClient:
    """按 URL 查表: 未登记的地址一律返回空串 / None, 模拟站点上没有该资源."""

    def __init__(self, pages: dict[str, str] | None = None, payloads: dict[str, Any] | None = None) -> None:
        self.pages = pages or {}
        self.payloads = payloads or {}
        self.requested: list[str] = []

    async def get_html(self, url: str, **_: Any) -> str:
        self.requested.append(url)
        return self.pages.get(url, "")

    async def get_text(self, url: str, **_: Any) -> str:
        return await self.get_html(url)

    async def get_json(self, url: str, **_: Any) -> Any:
        self.requested.append(url)
        return self.payloads.get(url)


# --- mgstage ---------------------------------------------------------------

_MG_PAGE_URL = "https://www.mgstage.com/product/product_detail/300MIUM-800/"
_MG_PLAYER_URL = "https://www.mgstage.com/sampleplayer/sampleplayer.php?pid=69a477c5-adb4-4f90-8780-903a5b17c5cb"
_MG_PAGE = (
    '<div id="center_column"><div><h1>タイトル</h1></div>'
    '<p class="sample_movie_btn">'
    '<a href="/sampleplayer/sampleplayer.html/69a477c5-adb4-4f90-8780-903a5b17c5cb">サンプル</a>'
    "</p></div>"
)
_MG_PLAYER = '{"html": "<script>var movie = \'' + TRAILER + "';</script>\"}"


def test_mgstage_trailer_from_sample_player() -> None:
    """详情页只给 pid, 得再问一次回放器才拿得到 mp4."""
    client = _StubClient(pages={_MG_PAGE_URL: _MG_PAGE, _MG_PLAYER_URL: _MG_PLAYER})
    crawler = MGStageCrawler(cast(Any, client))

    meta = asyncio.run(crawler._scrape(_MG_PAGE_URL))

    assert meta is not None
    assert meta.trailer_urls == [TRAILER]
    assert _MG_PLAYER_URL in client.requested


def test_mgstage_without_player_pid_skips_trailer() -> None:
    """没有回放器入口的页面 (老模板) 不该产出预告片, 也不该多打一次请求."""
    page = '<div id="center_column"><div><h1>タイトル</h1></div></div>'
    client = _StubClient(pages={_MG_PAGE_URL: page})
    crawler = MGStageCrawler(cast(Any, client))

    meta = asyncio.run(crawler._scrape(_MG_PAGE_URL))

    assert meta is not None
    assert meta.trailer_urls == []
    assert len(client.requested) == 1


# --- works 主题站 (dahlia / faleno) ----------------------------------------

_DAHLIA_WORKS_URL = "https://dahlia-av.jp/wp-json/wp/v2/works?slug=dldss573"
_DAHLIA_MEDIA_URL = "https://dahlia-av.jp/wp-json/wp/v2/media/11832"
_DAHLIA_WORKS = [
    {
        "slug": "dldss573",
        "title": {"rendered": "タイトル 宮上唯依花"},
        "acf": {"作品名": "タイトル", "出演女優": "宮上唯依花", "サンプル動画": 11832},
        "acf_custom_images": {},
    }
]


def test_works_site_trailer_from_media_attachment() -> None:
    """ACF 的 サンプル動画 是附件 ID, 再查 media 才拿到 mp4."""
    client = _StubClient(
        payloads={
            _DAHLIA_WORKS_URL: _DAHLIA_WORKS,
            _DAHLIA_MEDIA_URL: {"source_url": TRAILER, "guid": {"rendered": "https://example.test/other.mp4"}},
        }
    )
    crawler = DahliaCrawler(cast(Any, client))

    meta = asyncio.run(crawler._scrape(_DAHLIA_WORKS_URL))

    assert meta is not None
    assert meta.trailer_urls == [TRAILER]
    assert _DAHLIA_MEDIA_URL in client.requested


def test_works_site_trailer_falls_back_to_guid() -> None:
    """media 没给 source_url 时退到 guid.rendered."""
    client = _StubClient(
        payloads={
            _DAHLIA_WORKS_URL: _DAHLIA_WORKS,
            _DAHLIA_MEDIA_URL: {"guid": {"rendered": TRAILER}},
        }
    )
    crawler = DahliaCrawler(cast(Any, client))

    meta = asyncio.run(crawler._scrape(_DAHLIA_WORKS_URL))

    assert meta is not None
    assert meta.trailer_urls == [TRAILER]


def test_works_site_without_sample_field_skips_media_request() -> None:
    """没有预告片的作品不该产生 media 请求."""
    works = [{**_DAHLIA_WORKS[0], "acf": {"作品名": "タイトル"}}]
    client = _StubClient(payloads={_DAHLIA_WORKS_URL: works})
    crawler = DahliaCrawler(cast(Any, client))

    meta = asyncio.run(crawler._scrape(_DAHLIA_WORKS_URL))

    assert meta is not None
    assert meta.trailer_urls == []
    assert client.requested == [_DAHLIA_WORKS_URL]


def test_works_site_survives_media_fetch_failure() -> None:
    """附件取不到只丢预告片, 其余字段照常返回."""
    client = _StubClient(payloads={_DAHLIA_WORKS_URL: _DAHLIA_WORKS})
    crawler = DahliaCrawler(cast(Any, client))

    meta = asyncio.run(crawler._scrape(_DAHLIA_WORKS_URL))

    assert meta is not None
    assert meta.title == "タイトル"
    assert meta.trailer_urls == []


# --- 厂牌官网 --------------------------------------------------------------

_OFFICIAL_URL = "https://www.moodyz.com/works/detail/midv001"
_OFFICIAL_PAGE = """<html><head><title>テスト作品 | 人気メーカー【MOODYZ】公式サイト</title></head><body>
<div class="th">品番</div><div class="td"><p><span>DVD</span>MIDV001</p></div>
<div class="p-workPage__side"><div class="video">
<video controls src="https://example.test/sample.mp4"></video>
</div></div>
</body></html>"""


def test_official_trailer_from_inline_video() -> None:
    """官网作品页自带 <video src>, 不额外请求."""
    client = _StubClient(pages={_OFFICIAL_URL: _OFFICIAL_PAGE})
    crawler = OfficialCrawler(cast(Any, client))

    meta = asyncio.run(crawler._scrape(_OFFICIAL_URL))

    assert meta is not None
    assert meta.trailer_urls == [TRAILER]
    assert client.requested == [_OFFICIAL_URL]
