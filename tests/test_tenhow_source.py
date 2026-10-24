"""Tenhow 源里的纯函数: 番号归一化 / cid→ASIN 映射 / 演员名索引."""

import asyncio

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
from amane.net.errors import RequestError

SITE = "https://tenhow.net"


class _StubClient:
    """只实现 get_html: 未登记的 URL 一律 404, 用来模拟站点上的死链."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    async def get_html(self, url: str, cookies: dict[str, str] | None = None) -> str:
        del cookies
        self.requested.append(url)
        page = self.pages.get(url)
        if page is None:
            raise RequestError(url, "HTTP 404")
        return page


def _reset_index() -> None:
    TenhowCrawler._index = {}
    TenhowCrawler._pending = []


def test_lookup_treats_page_404_as_miss() -> None:
    """演员页 404 只当没命中: 否则单页死链会让整个源被判成 not_found."""
    _reset_index()
    crawler = TenhowCrawler(_StubClient({}))
    assert asyncio.run(crawler._lookup("kuno_hinano", _keys("MIDV-581"))) is None


def test_extend_index_skips_dead_pages_and_keeps_growing() -> None:
    """补索引时遇到死链要跳过 (并记入已爬), 不能中断也没能挡住后面的页进索引."""
    _reset_index()
    pages = {
        f"{SITE}/index.html": '<a href="aoi_tsukasa.html">葵 つかさ</a><a href="dead.html">x</a>',
        f"{SITE}/aoi_tsukasa.html": '<a href="kuno_hinano.html">九野 ひなの</a>',
    }
    crawler = TenhowCrawler(_StubClient(pages))
    asyncio.run(crawler._extend_index(10))

    assert "dead.html" in TenhowCrawler._crawled
    assert TenhowCrawler._index["九野ひなの"] == "kuno_hinano"


def test_page_budget_widens_while_index_is_small() -> None:
    """索引小的时候一次补更多页, 免得几十次刮削才轮到分类页里的演员."""
    _reset_index()
    crawler = TenhowCrawler(_StubClient({}))
    assert crawler._page_budget() == _COLD_START_PAGES

    TenhowCrawler._index = {f"n{i}": f"p{i}" for i in range(_COLD_START_INDEX_SIZE)}
    assert crawler._page_budget() == _MAX_PAGES_PER_FETCH
    TenhowCrawler._crawled = set()
    TenhowCrawler._updated = 0.0
    TenhowCrawler._pages = {}


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
