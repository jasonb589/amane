"""Amazon 源里的纯函数: 结果解析 / 标题匹配 / 海报地址, 以及年龄门重试."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from amane.crawlers.models import SearchQuery
from amane.crawlers.sites.amazon import (
    _GATE_RE,
    AmazonCrawler,
    _clean,
    _keywords,
    _match_score,
    _parse_results,
    _poster_urls,
    _search_title,
    _title_matches,
)
from amane.net.errors import FailureReason, SourceError
from amane.parsing import ContentType


def test_profile_leaves_accept_language_to_fingerprint() -> None:
    """自定义 ``ja-JP`` 会让亚马逊返回 /age_check/ 年龄门, 必须交给 curl_cffi 指纹自带."""
    assert "Accept-Language" not in AmazonCrawler.profile().headers


def test_parse_results_reads_asin_image_and_alt() -> None:
    html = (
        'data-asin="B00TEST001"><img alt="Diabolus 鬼哭 下巻 DVD" '
        'src="https://m.media-amazon.com/images/I/81abc+-XYZ._AC_.jpg">'
        'data-asin="B00TEST002"><img alt="別の商品" '
        'src="https://m.media-amazon.com/images/I/71zzzQ.jpg">'
    )
    out = _parse_results(html)
    assert out[0] == ("B00TEST001", "81abc+-XYZ", "Diabolus 鬼哭 下巻 DVD")
    assert out[1][0] == "B00TEST002"
    assert len(out) == 2


def test_parse_results_skips_blocks_without_image() -> None:
    html = (
        'data-asin="B00TEST003"><span>没有图片的块</span>'
        'data-asin="B00TEST004"><img alt="有图有真相的商品" src="https://m.media-amazon.com/images/I/71zzzQ.jpg">'
    )
    out = _parse_results(html)
    assert [row[0] for row in out] == ["B00TEST004"]


def test_title_matches_accepts_containment() -> None:
    assert _title_matches("Diabolus〜鬼哭〜 下巻", "Diabolus～鬼哭～ 下巻 「追憶の辱め」 DVD")


def test_title_matches_rejects_unrelated() -> None:
    assert not _title_matches("全然ちがう作品名", "別の商品タイトルです")


def test_title_matches_folds_compatibility_numerals() -> None:
    """亚马逊把 VIII 写成兼容字符 Ⅷ; NFKC 折叠后必须判为同一标题."""
    assert _title_matches("BEAUTY VENUS VIII", "BEAUTY VENUS Ⅷ アイデアポケット [DVD]")


def test_poster_urls_prefers_original_then_scaled() -> None:
    urls = _poster_urls("81abc")
    assert urls[0] == "https://m.media-amazon.com/images/I/81abc.jpg"
    assert urls[1] == "https://m.media-amazon.com/images/I/81abc._SL1500_.jpg"


def test_clean_strips_bracket_notes_and_promo_words() -> None:
    """DMM 标题里的促销与渠道噪声原样搜亚马逊, 会把正经条目挤出结果页."""
    raw = "【視聴期限なし】義母 前編|オンラインコード版 アイデアポケット [DVD]"
    assert _clean(raw) == "義母 前編 アイデアポケット"


def test_keywords_truncate_from_long_to_short_then_number() -> None:
    terms = _keywords("禁欲の果て 3日間 相沢みなみ アイデアポケット [DVD]", "IPZZ-034")
    assert terms == [
        "禁欲の果て 3日間 相沢みなみ アイデアポケット",
        "禁欲の果て 3日間 相沢みなみ",
        "禁欲の果て 3日間",
    ]


def test_keywords_drop_trailing_maker_name() -> None:
    """实测: 多带一个厂商名亚马逊就 0 条, 砍掉才有结果."""
    assert _keywords("義母 前編 アイデアポケット", "JUFD-001") == [
        "義母 前編 アイデアポケット",
        "義母 前編",
        "JUFD-001",
    ]


def test_keywords_keep_latin_title_whole() -> None:
    """纯拉丁标题砍成前半只会搜到别的卷, 只留整标题 + 番号."""
    assert _keywords("BEAUTY VENUS VIII", "IPZZ-034") == ["BEAUTY VENUS VIII", "IPZZ-034"]


def test_match_score_prefers_title_hit_over_number_only() -> None:
    wanted = "義母 前編"
    titled = _match_score("JUFD-001", wanted, "義母 前編 アイデアポケット [DVD]")
    number_only = _match_score("JUFD-001", wanted, "JUFD-001 アイデアポケット [DVD]")
    assert titled > number_only > 0


def test_match_score_rejects_number_hit_on_goods_or_non_disc_listing() -> None:
    assert _match_score("IPZZ-034", "禁欲の果て", "IPZZ-034 桜空もも 特典生写真") == 0
    assert _match_score("IPZZ-034", "禁欲の果て", "IPZZ-034 桜空もも") == 0


class _StubClient:
    """按顺序应答: 元素为异常时抛出, 否则当页面正文."""

    def __init__(self, items: list[object]) -> None:
        self._items = list(items)
        self.calls: list[str] = []

    async def get_text(self, url: str, **kwargs: Any) -> str:
        del kwargs
        self.calls.append(url)
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return str(item)


@pytest.mark.asyncio
async def test_fetch_retries_when_age_gate_blocks() -> None:
    """真正的 /age_check/ 是概率性的: 换一次请求就正常, 不该直接把整源判失败."""
    gate = '<form name="agecheck" action="/age_check/"><input class="driver-verify"></form>'
    page = (
        'data-asin="B00TEST001"><img alt="BEAUTY VENUS Ⅷ アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/81ABCDE.jpg">'
    )
    client = _StubClient([gate, page])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("IPZZ-034", content_type=ContentType.CENSORED)
    query.partial_result = SimpleNamespace(title="BEAUTY VENUS VIII")  # type: ignore[assignment]

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.poster_urls
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_fetch_raises_when_age_gate_persists() -> None:
    gate = '<form name="agecheck" action="/age_check/"></form>'
    client = _StubClient([gate, gate, gate])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("IPZZ-034", content_type=ContentType.CENSORED)
    query.partial_result = SimpleNamespace(title="BEAUTY VENUS VIII")  # type: ignore[assignment]

    with pytest.raises(SourceError) as excinfo:
        await crawler.fetch(query)

    assert excinfo.value.reason is FailureReason.AGE_VERIFICATION
    assert len(client.calls) == 3


def test_age_verification_modal_is_not_a_gate() -> None:
    """成人分区结果页每页都带 ``Age Verification`` 弹窗标题, 那不是拦截页 —— 判死就永远搜不到."""
    page = (
        '<span class="s-black-curtain-modal-content">data-a-modal="{&quot;header&quot;:&quot;Age Verification&quot;}"'
    )
    assert _GATE_RE.search(page) is None


@pytest.mark.asyncio
async def test_fetch_falls_back_to_next_keyword_when_first_misses() -> None:
    """标题搜不到时按 主标题 / 番号 继续试, 不能一次没命中就放弃整源."""
    miss = 'data-asin="B00OTHER01"><img alt="別の作品 [DVD]" src="https://m.media-amazon.com/images/I/71other.jpg">'
    hit = (
        'data-asin="B00TEST009"><img alt="義母 前編 アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/81HITJP.jpg">'
    )
    client = _StubClient([miss, hit])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("JUFD-001", content_type=ContentType.CENSORED)
    query.partial_result = SimpleNamespace(title="【視聴期限なし】義母 前編 アイデアポケット [DVD]")  # type: ignore[assignment]

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00TEST009"
    assert meta.poster_urls[0] == "https://m.media-amazon.com/images/I/81HITJP.jpg"
    assert len(client.calls) == 2


def test_parse_results_skips_placeholder_images() -> None:
    """成人卡片未过黑幕前只给 60x40 占位图, 取它就是给错封面."""
    html = (
        'data-asin="B00ADULT01"><img alt="BEAUTY VENUS Ⅷ アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/01RmK+J4pJL._AC_UL80_.gif">'
        'data-asin="B00OTHER02"><img alt="別の商品 [DVD]" src="https://m.media-amazon.com/images/I/71zzzQQ.jpg">'
    )
    assert [row[0] for row in _parse_results(html)] == ["B00OTHER02"]


@pytest.mark.asyncio
async def test_fetch_unlocks_black_curtain_for_adult_covers() -> None:
    """看到黑幕弹窗就点一次「Yes」, 然后重取: 换来的页面才有真封面."""
    curtain = (
        '<span class="s-black-curtain-modal-content">Age Verification</span>'
        'data-asin="B00CURTAIN"><img alt="BEAUTY VENUS Ⅷ アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/01RmK+J4pJL._AC_UL80_.gif">'
    )
    unlocked = (
        'data-asin="B00TEST001"><img alt="BEAUTY VENUS Ⅷ アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/91bhG1CxHUL._AC_UL320_.jpg">'
    )
    client = _StubClient([curtain, "<html>ok</html>", unlocked])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("IPZZ-034", content_type=ContentType.CENSORED)
    query.partial_result = SimpleNamespace(title="BEAUTY VENUS VIII")  # type: ignore[assignment]

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00TEST001"
    assert meta.poster_urls[0] == "https://m.media-amazon.com/images/I/91bhG1CxHUL.jpg"
    assert len(client.calls) == 3
    assert "/black-curtain/save-eligibility/" in client.calls[1]


@pytest.mark.asyncio
async def test_fetch_unlocks_black_curtain_only_once() -> None:
    """黑幕只点一次: 后面换检索词时不该每次都去打那个保存接口."""
    curtain = (
        '<span class="s-black-curtain">'
        'data-asin="B00CURTAIN"><img alt="べつの作品 [DVD]" '
        'src="https://m.media-amazon.com/images/I/01RmK+J4pJL._AC_UL80_.gif">'
    )
    hit = (
        'data-asin="B00TEST077"><img alt="義母 前編 アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/91ZYXW9HUL._AC_UL320_.jpg">'
    )
    # 检索词1: 黑幕页 → 解锁 → 还是黑幕页(无命中); 检索词2: 命中
    client = _StubClient([curtain, "<html>ok</html>", curtain, hit])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("JUFD-001", content_type=ContentType.CENSORED)
    query.partial_result = SimpleNamespace(title="義母 前編 アイデアポケット")  # type: ignore[assignment]

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00TEST077"
    assert sum(1 for call in client.calls if "save-eligibility" in call) == 1


def test_search_title_prefers_raw_japanese_title() -> None:
    """字段语言是中文时聚合标题是译名, 检索必须换成来源里的日文原题."""
    partial = SimpleNamespace(
        title="义母 前篇",
        raw={"r18dev:jp": {"title": "義母 前編"}, "dmm": {"title": "義母 前編 [DVD]"}},
    )
    assert _search_title(partial) == "義母 前編"


def test_search_title_falls_back_to_aggregated_title() -> None:
    assert _search_title(SimpleNamespace(title="BEAUTY VENUS VIII", raw={})) == "BEAUTY VENUS VIII"
    assert _search_title(None) == ""


@pytest.mark.asyncio
async def test_fetch_searches_japanese_title_when_field_is_translated() -> None:
    page = (
        'data-asin="B00TEST001"><img alt="BEAUTY VENUS Ⅷ アイデアポケット [DVD]" '
        'src="https://m.media-amazon.com/images/I/81ABCDE.jpg">'
    )
    client = _StubClient([page])
    crawler = AmazonCrawler(client)  # type: ignore[arg-type]
    query = SearchQuery("IPZZ-034", content_type=ContentType.CENSORED)
    query.partial_result = SimpleNamespace(  # type: ignore[assignment]
        title="美颜维纳斯 第8集",
        raw={"r18dev:jp": {"title": "BEAUTY VENUS VIII"}},
    )

    meta = await crawler.fetch(query)

    assert meta is not None
    assert meta.external_id == "B00TEST001"
    assert "BEAUTY%20VENUS%20VIII" in client.calls[0]
