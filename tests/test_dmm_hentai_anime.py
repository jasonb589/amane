"""DMM 里番: アニメ目录搜索与纯链接候选解析, 以及按 URL 指定来源的域名匹配."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest

from amane.crawlers.models import MediaMetadata
from amane.crawlers.sites.dmm import DmmCrawler

ANIME_HREF = 'href="https://www.dmm.co.jp/mono/anime/-/detail/=/cid=68jvdw41/" data-i3ref="?i3_ref=search"'


class _FakeClient:
    """只实现 get_html: 未登记的 URL 抛错, 用于验证端点顺序与候选合并."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def get_html(self, url: str, **kwargs: Any) -> str:
        del kwargs
        self.calls.append(url)
        page = self.pages.get(url)
        if page is None:
            raise AssertionError(f"unexpected fetch: {url}")
        return page

    async def check(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - 未使用
        raise AssertionError("check should not be called")


def test_href_urls_reads_plain_category_links() -> None:
    """アニメ目录页只给 href (没有 detailUrl JSON), 纯链接也要能收成候选."""
    html = f"<div>{ANIME_HREF}</div>"
    assert DmmCrawler._href_urls(html) == ["https://www.dmm.co.jp/mono/anime/-/detail/=/cid=68jvdw41/"]


def test_serves_url_matches_same_site_subdomains() -> None:
    crawler = DmmCrawler(_FakeClient({}))  # type: ignore[arg-type]
    assert crawler.serves_url("https://www.dmm.co.jp/mono/anime/-/detail/=/cid=68jvdw41/")
    assert crawler.serves_url("https://video.dmm.co.jp/anime/content/?id=147jvdw41")
    assert not crawler.serves_url("https://www.amazon.co.jp/s?k=MIDV")
    assert not crawler.serves_url("")


@pytest.mark.asyncio
async def test_hentai_search_queries_anime_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """アニメ条目只出现在 mono/anime 目录里: 两个端点都要查, 命中后返回该详情页."""
    keyword = quote("義母 前編", safe="")
    anime_url = DmmCrawler._HENTAI_ANIME_SEARCH_URL.format(keyword)
    generic_url = DmmCrawler._HENTAI_SEARCH_URL.format(keyword)
    client = _FakeClient({anime_url: f"<div>{ANIME_HREF}</div>", generic_url: "<div>no hits</div>"})
    crawler = DmmCrawler(client)  # type: ignore[arg-type]

    async def fake_scrape(url: str, options: Any = None) -> MediaMetadata | None:
        del options
        return MediaMetadata(number="68jvdw41", title="義母 前編")

    monkeypatch.setattr(DmmCrawler, "_scrape", fake_scrape)

    found = await crawler._search_hentai("義母 前編")

    assert found == "https://www.dmm.co.jp/mono/anime/-/detail/=/cid=68jvdw41/"
    assert anime_url in client.calls
