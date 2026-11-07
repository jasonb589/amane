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
    _select_asin,
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


def test_cid_map_binds_cid_to_product_cover() -> None:
    html = (
        '<div class="um_image_a"><a href="images/B0ABCDEFGH.jpg"><img></a>'
        '<a href="https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=jur517/">x</a></div>'
        '<div class="um_image_a"><a href="images/B0ZZZZZZZZ.jpg"><img></a>'
        '<a href="https://video.dmm.co.jp/av/content/?id=jur00518">y</a></div>'
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
    client.pages[url] = (
        '<div class="um_image_a"><a href="images/B0ABCDEFGH.jpg"></a><a href="https://video.dmm.co.jp/av/content/?id=jur00517">x</a></div>'
    )
    assert await crawler._lookup("actor", _keys("JUR-517")) == "B0ABCDEFGH"
    assert len(client.requested) == 2


@pytest.mark.asyncio
async def test_fetch_uses_new_index_and_expires_it(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _StubClient(
        {
            f"{SITE}/index.html": '<a href="actor.html">Actor</a>',
            f"{SITE}/actor.html": '<div class="um_image_a"><a href="images/B0ABCDEFGH.jpg"></a><a href="https://video.dmm.co.jp/av/content/?id=jur00517">x</a></div>',
        }
    )
    crawler = client.crawler()
    query = SearchQuery("JUR-517", partial_result=AggregatedMetadata("JUR-517", actors=[FilmActor(name="Actor")]))
    result = await crawler.fetch(query)
    assert result is not None and result.external_id == "B0ABCDEFGH"
    initial = crawler._updated
    monkeypatch.setattr(tenhow.time, "monotonic", lambda: initial + tenhow._INDEX_TTL + 1)
    client.pages[f"{SITE}/actor.html"] = (
        '<div class="um_image_a"><a href="images/B0ZZZZZZZZ.jpg"></a><a href="https://video.dmm.co.jp/av/content/?id=jur00517">x</a></div>'
    )
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


@pytest.mark.parametrize(
    ("cid", "number", "matches"),
    [
        ("h_283dfet00001", "DFET-001", True),
        ("n_709abcd00123", "ABCD-123", True),
        ("JUR00517", "ＪＵＲ－５１７", True),
        ("300MIUM00123", "200MIUM-123", False),
        ("010115_001", "010115-001", False),
        ("", "JUR-517", False),
        ("jur005170", "JUR-517", False),
    ],
)
def test_catalog_keys_preserve_number_identity(cid: str, number: str, matches: bool) -> None:
    assert bool(_keys(cid) & _keys(number)) is matches


@pytest.mark.parametrize(
    ("mapping", "number", "expected"),
    [
        ({"84mild00881": "B0ABCDEFGH"}, "MILD-881", "B0ABCDEFGH"),
        ({"84mild881": "B0ABCDEFGH", "84mild00881": "B0ABCDEFGH"}, "MILD-881", "B0ABCDEFGH"),
        ({"84mild00881": "B0ABCDEFGH", "mild00881": "B0ZZZZZZZZ"}, "MILD-881", "B0ZZZZZZZZ"),
        ({"200mium00123": "B0ABCDEFGH", "300mium00123": "B0ZZZZZZZZ"}, "MIUM-123", None),
        ({"200mium00123": "B0ABCDEFGH", "300mium00123": "B0ZZZZZZZZ"}, "300MIUM-123", "B0ZZZZZZZZ"),
        ({"200mium00123": "B0ABCDEFGH"}, "300MIUM-123", None),
        ({"jur517": "B0ABCDEFGH", "jur00517": "B0ZZZZZZZZ"}, "JUR-517", None),
    ],
)
def test_catalog_selection_prefers_exact_and_rejects_ambiguity(
    mapping: dict[str, str], number: str, expected: str | None
) -> None:
    assert _select_asin(mapping, _keys(number)) == expected


@pytest.mark.parametrize(
    "href",
    [
        "./images/B0ABCDEFGH.jpg",
        "/images/B0ABCDEFGH.jpg",
        "https://tenhow.net/images/B0ABCDEFGH.jpg",
        "https://www.tenhow.net/images/B0ABCDEFGH.jpg",
    ],
)
def test_product_links_accept_markup_and_affiliate_variants(href: str) -> None:
    html = (
        f"<div class='um_image_a um_popup'><a href='{href}'><img></a>"
        "<div class='buythis'><a href='https://al.fanza.co.jp/?lurl="
        "https%3a%2f%2fwww.dmm.co.jp%2fmono%2fdvd%2f-%2fdetail%2f%3d%2fcid%3dh_283dfet001%2f"
        "&amp;af_id=example'><img></a></div></div>"
    )
    assert _cid_map(html) == {"h_283dfet001": "B0ABCDEFGH"}


@pytest.mark.parametrize(
    "href",
    [
        "https://unrelated.example/images/B0ABCDEFGH.jpg",
        "https://tenhow.net.evil.example/images/B0ABCDEFGH.jpg",
        "javascript:images/B0ABCDEFGH.jpg",
        "images/icon_B0ABCDEFGH.jpg",
    ],
)
def test_product_links_reject_external_and_thumbnail_addresses(href: str) -> None:
    html = (
        f'<div class="um_image_a"><a href="{href}"></a>'
        '<a href="https://video.dmm.co.jp/av/content/?id=jur00517">商品</a></div>'
    )
    assert _cid_map(html) == {}


def test_product_mapping_does_not_assign_missing_cover_to_previous_item() -> None:
    html = (
        '<div id="um_article"><div class="um_image_a"><a href="images/B0ABCDEFGH.jpg"></a>'
        '<a href="https://video.dmm.co.jp/av/content/?id=jur00517">商品</a></div>'
        '<div class="um_image_a"><a href="https://video.dmm.co.jp/av/content/?id=jur00518">欠图商品</a></div>'
        '<div class="um_image_a"><a href="images/B0ZZZZZZZZ.jpg"></a>'
        '<a href="https://advert.example/?id=jur00519">广告</a></div></div>'
    )
    assert _cid_map(html) == {"jur00517": "B0ABCDEFGH"}


def test_product_mapping_rejects_conflicting_covers() -> None:
    html = "".join(
        f'<div class="um_image_a"><a href="images/{asin}.jpg"></a>'
        '<a href="https://video.dmm.co.jp/av/content/?id=jur00517">商品</a></div>'
        for asin in ["B0ABCDEFGH", "B0ZZZZZZZZ"]
    )
    assert _cid_map(html) == {}


def test_actor_links_accept_nested_markup_and_keep_aliases() -> None:
    html = (
        "<a href='./inaba_ruka.html'><span>稲場　るか</span></a>"
        "<a href='https://tenhow.net/inaba_ruka.html'>莉々はるか</a>"
        "<a href='https://unrelated.example/actor.html'>無関係</a>"
    )
    assert _names_on_page(html, "agyo") == {"稲場るか": "inaba_ruka", "莉々はるか": "inaba_ruka"}


@pytest.mark.asyncio
async def test_fetch_prioritizes_directory_and_stops_when_actor_found() -> None:
    client = _StubClient(
        {
            f"{SITE}/index.html": '<a href="body.html">分類</a><a href="mokuji.html">AV女優リスト</a>',
            f"{SITE}/mokuji.html": '<a href="body.html">分類</a><a href="agyo.html">あ行</a><a href="kagyo.html">か行</a>',
            f"{SITE}/agyo.html": '<a href="ono_rikka.html">小野六花</a>',
            f"{SITE}/ono_rikka.html": '<div class="um_image_a"><a href="images/B0ABCDEFGH.jpg"></a><a href="https://video.dmm.co.jp/av/content/?id=mide00845">商品</a></div>',
        }
    )
    query = SearchQuery("MIDE-845", partial_result=AggregatedMetadata("MIDE-845", actors=[FilmActor(name="小野六花")]))
    crawler = client.crawler()
    result = await crawler.fetch(query)
    assert result is not None and result.poster_urls == [f"{SITE}/images/B0ABCDEFGH.jpg"]
    assert client.requested == [f"{SITE}/{page}.html" for page in ["index", "mokuji", "agyo", "ono_rikka"]]
    assert await crawler.fetch(query) == result
    assert len(client.requested) == 4


@pytest.mark.asyncio
async def test_index_reuses_product_mapping_for_lookup() -> None:
    client = _StubClient(
        {
            f"{SITE}/index.html": '<a href="actor.html">Actor</a>',
            f"{SITE}/actor.html": '<div class="um_image_a"><a href="images/B0ABCDEFGH.jpg"></a><a href="https://video.dmm.co.jp/av/content/?id=jur00517">商品</a></div>',
        }
    )
    crawler = client.crawler()
    await crawler._extend_index(2)
    result = await crawler.fetch(
        SearchQuery("JUR-517", partial_result=AggregatedMetadata("JUR-517", actors=[FilmActor(name="Actor")]))
    )
    assert result is not None and result.external_id == "B0ABCDEFGH"
    assert client.requested == [f"{SITE}/index.html", f"{SITE}/actor.html"]


@pytest.mark.asyncio
async def test_fetch_continues_directory_for_other_actor_after_first_misses() -> None:
    client = _StubClient(
        {
            f"{SITE}/index.html": '<a href="first.html">First</a><a href="mokuji.html">AV女優リスト</a>',
            f"{SITE}/first.html": "<html>没有该作品</html>",
            f"{SITE}/mokuji.html": '<a href="second.html">Second</a>',
            f"{SITE}/second.html": '<div class="um_image_a"><a href="images/B0ABCDEFGH.jpg"></a><a href="https://video.dmm.co.jp/av/content/?id=jur00517">商品</a></div>',
        }
    )
    result = await client.crawler().fetch(
        SearchQuery(
            "JUR-517",
            partial_result=AggregatedMetadata("JUR-517", actors=[FilmActor(name="First"), FilmActor(name="Second")]),
        )
    )
    assert result is not None and result.external_id == "B0ABCDEFGH"
    assert client.requested == [f"{SITE}/{page}.html" for page in ["index", "first", "mokuji", "second"]]


@pytest.mark.parametrize("actors", [[{"name": "小野六花"}], ["小野六花"]])
@pytest.mark.asyncio
async def test_fetch_uses_raw_actor_name_when_aggregated_name_is_translated(actors: list[object]) -> None:
    client = _StubClient(
        {
            f"{SITE}/index.html": '<a href="ono_rikka.html">小野六花</a>',
            f"{SITE}/ono_rikka.html": '<div class="um_image_a"><a href="images/B0ABCDEFGH.jpg"></a><a href="https://video.dmm.co.jp/av/content/?id=mide00845">商品</a></div>',
        }
    )
    query = SearchQuery(
        "MIDE-845",
        partial_result=AggregatedMetadata(
            "MIDE-845", actors=[FilmActor(name="Rikka Ono")], raw={"dmm": {"actors": actors}}
        ),
    )
    result = await client.crawler().fetch(query)
    assert result is not None and result.external_id == "B0ABCDEFGH"
    assert client.requested == [f"{SITE}/index.html", f"{SITE}/ono_rikka.html"]
