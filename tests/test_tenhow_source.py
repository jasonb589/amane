"""Tenhow 源里的纯函数: 番号归一化 / cid→ASIN 映射 / 演员名索引."""

import asyncio
from collections.abc import Mapping
from typing import Any, cast

import pytest

from amane.aggregate.models import AggregatedMetadata
from amane.crawlers.http import HttpClient
from amane.crawlers.models import FilmActor, SearchQuery
from amane.crawlers.sites import tenhow
from amane.crawlers.sites.tenhow import (
    _COLD_START_INDEX_SIZE,
    _COLD_START_PAGES,
    _MAX_PAGES_PER_FETCH,
    TenhowCrawler,
    _cid_map,
    _keys,
    _names_on_page,
    _norm_name,
)
from amane.net.errors import FailureKind, FailureReason, RequestError, RequestFailure

SITE = "https://tenhow.net"


class _StubClient:
    """只实现 get_html: 未登记的 URL 一律 404, 用来模拟站点上的死链."""

    def __init__(self, pages: Mapping[str, str | RequestError]) -> None:
        self.pages = dict(pages)
        self.requested: list[str] = []

    async def get_html(self, url: str, cookies: dict[str, str] | None = None) -> str:
        del cookies
        self.requested.append(url)
        page = self.pages.get(url)
        if page is None:
            raise RequestError(url, RequestFailure(FailureKind.HTTP_STATUS, "HTTP 404", status=404))
        if isinstance(page, RequestError):
            raise page
        return page

    def crawler(self) -> TenhowCrawler:
        """Tenhow 在测试中只消费 get_html, 其余网络操作均不参与."""
        return TenhowCrawler(cast(HttpClient, self))


def test_lookup_treats_page_404_as_miss() -> None:
    """演员页 404 只当没命中: 否则单页死链会让整个源被判成 not_found."""
    crawler = _StubClient({}).crawler()
    assert asyncio.run(crawler._lookup("kuno_hinano", _keys("MIDV-581"))) is None


def test_extend_index_skips_dead_pages_and_keeps_growing() -> None:
    """补索引时遇到死链要跳过 (并记入已爬), 不能中断也没能挡住后面的页进索引."""
    pages = {
        f"{SITE}/index.html": '<a href="aoi_tsukasa.html">葵 つかさ</a><a href="dead.html">x</a>',
        f"{SITE}/aoi_tsukasa.html": '<a href="kuno_hinano.html">九野 ひなの</a>',
    }
    crawler = _StubClient(pages).crawler()
    asyncio.run(crawler._extend_index(10))

    assert "dead" in crawler._crawled
    assert crawler._index["九野ひなの"] == "kuno_hinano"


def test_page_budget_widens_while_index_is_small() -> None:
    """索引小的时候一次补更多页, 免得几十次刮削才轮到分类页里的演员."""
    crawler = _StubClient({}).crawler()
    assert crawler._page_budget() == _COLD_START_PAGES

    crawler._index = {f"n{i}": f"p{i}" for i in range(_COLD_START_INDEX_SIZE)}
    assert crawler._page_budget() == _MAX_PAGES_PER_FETCH


def test_keys_accepts_cid_variants() -> None:
    # JUR-517 / jur517 / jur00517 三种写法必须能互相命中
    assert _keys("JUR-517") & _keys("jur00517")
    assert _keys("jur517") & _keys("jur00517")


def test_norm_name_strips_half_and_full_width_spaces() -> None:
    assert _norm_name("Satsuki Mei") == "SatsukiMei"
    assert _norm_name("　葵　つかさ　") == "葵つかさ"


def test_cid_map_binds_cid_to_nearest_cover() -> None:
    html = (
        '<a href="images/B0ABCDEFGH.jpg"><img></a>'
        '<a href="https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=jur517/">x</a>'
        '<a href="images/B0ZZZZZZZZ.jpg"><img></a>'
        '<a href="https://video.dmm.co.jp/av/content/?id=jur00518">y</a>'
    )
    mapping = _cid_map(html)
    assert mapping["jur517"] == "B0ABCDEFGH"
    assert mapping["jur00518"] == "B0ZZZZZZZZ"


def test_names_on_page_reads_actress_title() -> None:
    html = "<html><head><title>葵つかさのアダルトDVDパケ写一覧</title></head></html>"
    assert _names_on_page(html, "aoi_tsukasa") == {"葵つかさ": "aoi_tsukasa"}


def test_names_on_page_collects_links() -> None:
    html = '<a href="aoi_tsukasa.html">葵 つかさ</a><a href="body.html">BODY</a>'
    found = _names_on_page(html, "index")
    assert found["葵つかさ"] == "aoi_tsukasa"
    assert found["BODY"] == "body"


@pytest.mark.asyncio
async def test_index_budget_and_total_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tenhow, "_MAX_INDEX_PAGES", 3)
    client = _StubClient({f"{SITE}/index.html": "".join(f'<a href="p{i}.html">n{i}</a>' for i in range(8))})
    crawler = client.crawler()
    await crawler._extend_index(1)
    assert client.requested == [f"{SITE}/index.html"]
    await crawler._extend_index(20)
    assert len(client.requested) == 3
    assert not crawler._pending
    assert not _StubClient({}).crawler()._index


@pytest.mark.asyncio
async def test_index_retry_preserves_failed_page() -> None:
    url = f"{SITE}/index.html"
    failure = RequestError(url, RequestFailure(FailureKind.TIMEOUT, "timeout"))
    client = _StubClient({url: failure})
    crawler = client.crawler()
    with pytest.raises(RequestError) as error:
        await crawler._extend_index(1)
    assert error.value.reason is FailureReason.TIMEOUT
    assert crawler._pending == ["index"]
    assert not crawler._crawled
    client.pages[url] = '<a href="actor.html">Actor</a>'
    await crawler._extend_index(1)
    assert crawler._index == {"Actor": "actor"}


@pytest.mark.asyncio
async def test_lookup_retries_transient_failure_without_negative_cache() -> None:
    url = f"{SITE}/actor.html"
    client = _StubClient({url: RequestError(url, RequestFailure(FailureKind.HTTP_STATUS, "HTTP 503", status=503))})
    crawler = client.crawler()
    with pytest.raises(RequestError) as error:
        await crawler._lookup("actor", _keys("JUR-517"))
    assert error.value.reason is FailureReason.SERVER_ERROR
    client.pages[url] = '<a href="images/B0ABCDEFGH.jpg"></a><a href="/?id=jur00517">x</a>'
    assert await crawler._lookup("actor", _keys("JUR-517")) == "B0ABCDEFGH"
    assert len(client.requested) == 2


@pytest.mark.asyncio
async def test_fetch_uses_new_index_and_expires_it(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _StubClient(
        {
            f"{SITE}/index.html": '<a href="actor.html">Actor</a>',
            f"{SITE}/actor.html": '<a href="images/B0ABCDEFGH.jpg"></a><a href="/?id=jur00517">x</a>',
        }
    )
    crawler = client.crawler()
    query = SearchQuery("JUR-517", partial_result=AggregatedMetadata("JUR-517", actors=[FilmActor(name="Actor")]))
    result = await crawler.fetch(query)
    assert result is not None and result.external_id == "B0ABCDEFGH"
    initial = crawler._updated
    monkeypatch.setattr(tenhow.time, "monotonic", lambda: initial + tenhow._INDEX_TTL + 1)
    client.pages[f"{SITE}/actor.html"] = '<a href="images/B0ZZZZZZZZ.jpg"></a><a href="/?id=jur00517">x</a>'
    result = await crawler.fetch(query)
    assert result is not None and result.external_id == "B0ZZZZZZZZ"
    assert client.requested.count(f"{SITE}/index.html") == 2


@pytest.mark.asyncio
async def test_concurrent_index_extensions_do_not_duplicate_seed() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class PausedClient(_StubClient):
        async def get_html(self, url: str, **kwargs: Any) -> str:
            if url.endswith("/index.html"):
                entered.set()
                await release.wait()
            return await super().get_html(url, **kwargs)

    client = PausedClient({f"{SITE}/index.html": '<a href="actor.html">Actor</a>'})
    crawler = client.crawler()
    first = asyncio.create_task(crawler._extend_index(1))
    await entered.wait()
    second = asyncio.create_task(crawler._extend_index(1))
    release.set()
    await asyncio.gather(first, second)
    assert client.requested == [f"{SITE}/index.html", f"{SITE}/actor.html"]
